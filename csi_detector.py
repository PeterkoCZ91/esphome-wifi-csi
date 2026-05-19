#!/usr/bin/env python3
"""
CSI Real-time Presence Detector — dual-node dashboard + direction detection.

Custom detection logic on raw ESPectre data (movement_score / active_threshold).
FSM: IDLE → DETECTING → PRESENT → CLEARING → IDLE
Walk direction determined by threshold-crossing order of both nodes.

Usage:
  python3 csi_detector.py              # live dashboard
  python3 csi_detector.py --no-clear   # no ANSI clear (scrolling mode)
"""
import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import paho.mqtt.client as mqtt

BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

NODES = {
    "csi_obyvak": "obyvak",
    "csi_chodba": "chodba",
}

# Detection thresholds
DETECT_RATIO = 1.0       # ratio > this → detecting
CONFIRM_TIME = 1.0        # seconds above threshold to confirm
CLEAR_RATIO = 0.5         # ratio < this on BOTH → start clearing
CLEAR_TIME = 3.0          # seconds below clear threshold → idle
SIMULTANEOUS_WINDOW = 1.0 # seconds — if both trigger within this → "simultaneous"

MAX_EVENTS = 12           # event log lines shown


class State(Enum):
    IDLE = "IDLE"
    DETECTING = "DETECTING"
    PRESENT = "PRESENT"
    CLEARING = "CLEARING"


@dataclass
class NodeData:
    score: float = 0.0
    threshold: float = 1.0
    turbulence: float = 0.0
    ratio: float = 0.0
    last_update: float = 0.0
    crossed_at: float = 0.0      # when ratio first exceeded DETECT_RATIO
    peak_ratio: float = 0.0


@dataclass
class EventRecord:
    ts: float
    duration: float
    direction: str
    peak: float

    def fmt(self) -> str:
        t = time.strftime("%H:%M:%S", time.localtime(self.ts))
        return f"  {t}  PRESENT  {self.duration:4.1f}s  {self.direction:<20s}  peak={self.peak:.1f}\u00d7"


class CSIDetector:
    def __init__(self, no_clear: bool = False):
        self.no_clear = no_clear
        self.nodes: dict[str, NodeData] = {k: NodeData() for k in NODES}
        self.state = State.IDLE
        self.state_since: float = time.time()
        self.detect_start: float = 0.0
        self.clear_start: float = 0.0
        self.first_node: Optional[str] = None
        self.events: list[EventRecord] = []
        self.present_start: float = 0.0
        self.global_peak: float = 0.0

    # ── MQTT ──────────────────────────────────────────────────────────────

    def run(self):
        client = mqtt.Client()
        client.username_pw_set(MQTT_USER, MQTT_PASS)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect(BROKER, 1883)
        print(f"CSI Detector — připojuji k {BROKER}...")
        try:
            client.loop_forever()
        except KeyboardInterrupt:
            # Restore terminal
            if not self.no_clear:
                print("\033[?25h", end="")  # show cursor
            print("\nUkončeno.")

    def _on_connect(self, client, ud, flags, rc):
        for node in NODES:
            client.subscribe(f"esphome/{node}/sensor/movement_score/state")
            client.subscribe(f"esphome/{node}/sensor/csi_active_threshold/state")
            client.subscribe(f"esphome/{node}/sensor/csi_turbulence/state")
        if not self.no_clear:
            print("\033[?25l", end="")  # hide cursor

    def _on_message(self, client, ud, msg):
        parts = msg.topic.split("/")
        node_id = parts[1]
        if node_id not in self.nodes:
            return

        val = msg.payload.decode(errors="replace").strip()
        try:
            fval = float(val)
        except ValueError:
            return

        nd = self.nodes[node_id]
        nd.last_update = time.time()

        if "movement_score" in msg.topic:
            nd.score = fval
            self._recompute_ratio(nd)
            self._update_fsm()
            self._render()
        elif "csi_active_threshold" in msg.topic:
            nd.threshold = fval
            self._recompute_ratio(nd)
        elif "csi_turbulence" in msg.topic:
            nd.turbulence = fval

    @staticmethod
    def _recompute_ratio(nd: NodeData):
        thr = nd.threshold if nd.threshold > 0 else 1.0
        nd.ratio = nd.score / thr
        nd.peak_ratio = max(nd.peak_ratio, nd.ratio)

    # ── FSM ───────────────────────────────────────────────────────────────

    def _any_above(self, ratio: float) -> bool:
        return any(nd.ratio > ratio for nd in self.nodes.values())

    def _all_below(self, ratio: float) -> bool:
        return all(nd.ratio < ratio for nd in self.nodes.values())

    def _set_state(self, s: State):
        self.state = s
        self.state_since = time.time()

    def _update_fsm(self):
        now = time.time()

        if self.state == State.IDLE:
            if self._any_above(DETECT_RATIO):
                self._set_state(State.DETECTING)
                self.detect_start = now
                self.first_node = None
                self.global_peak = 0.0
                # Track which node triggered first
                for nid, nd in self.nodes.items():
                    nd.peak_ratio = nd.ratio
                    if nd.ratio > DETECT_RATIO:
                        nd.crossed_at = now
                        if self.first_node is None:
                            self.first_node = nid

        elif self.state == State.DETECTING:
            # Track crossings
            for nid, nd in self.nodes.items():
                if nd.ratio > DETECT_RATIO and nd.crossed_at == 0:
                    nd.crossed_at = now
                    if self.first_node is None:
                        self.first_node = nid
            # Update global peak
            self.global_peak = max(self.global_peak, *(nd.ratio for nd in self.nodes.values()))

            if not self._any_above(DETECT_RATIO):
                # Lost signal before confirmation
                self._reset_nodes()
                self._set_state(State.IDLE)
            elif (now - self.detect_start) >= CONFIRM_TIME:
                # Confirmed!
                self._set_state(State.PRESENT)
                self.present_start = now

        elif self.state == State.PRESENT:
            self.global_peak = max(self.global_peak, *(nd.ratio for nd in self.nodes.values()))
            if self._all_below(CLEAR_RATIO):
                self._set_state(State.CLEARING)
                self.clear_start = now

        elif self.state == State.CLEARING:
            self.global_peak = max(self.global_peak, *(nd.ratio for nd in self.nodes.values()))
            if self._any_above(CLEAR_RATIO):
                # Back to present
                self._set_state(State.PRESENT)
            elif (now - self.clear_start) >= CLEAR_TIME:
                # Fully cleared — log event
                direction = self._compute_direction()
                duration = now - self.present_start
                ev = EventRecord(
                    ts=self.present_start,
                    duration=duration,
                    direction=direction,
                    peak=self.global_peak,
                )
                self.events.append(ev)
                if len(self.events) > MAX_EVENTS:
                    self.events = self.events[-MAX_EVENTS:]
                self._reset_nodes()
                self._set_state(State.IDLE)

    def _compute_direction(self) -> str:
        crossed = {}
        for nid, nd in self.nodes.items():
            if nd.crossed_at > 0:
                crossed[nid] = nd.crossed_at

        names = {nid: NODES[nid] for nid in crossed}

        if len(crossed) == 0:
            return "neznámý"
        elif len(crossed) == 1:
            nid = list(crossed.keys())[0]
            return f"pouze {names[nid]}"
        else:
            sorted_nodes = sorted(crossed.items(), key=lambda x: x[1])
            first_id, first_t = sorted_nodes[0]
            second_id, second_t = sorted_nodes[1]
            if abs(second_t - first_t) < SIMULTANEOUS_WINDOW:
                return "oba současně"
            return f"{names[first_id]}\u2192{names[second_id]}"

    def _reset_nodes(self):
        for nd in self.nodes.values():
            nd.crossed_at = 0.0
            nd.peak_ratio = 0.0

    # ── Render ────────────────────────────────────────────────────────────

    def _render(self):
        now = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(now))

        # State color
        state_colors = {
            State.IDLE: "\033[37m",       # white
            State.DETECTING: "\033[33m",  # yellow
            State.PRESENT: "\033[31m",    # red
            State.CLEARING: "\033[36m",   # cyan
        }
        sc = state_colors.get(self.state, "\033[0m")
        reset = "\033[0m"
        bold = "\033[1m"
        dim = "\033[2m"

        lines = []
        lines.append("")
        lines.append(f"  {bold}\u2550\u2550 CSI Detector \u2550\u2550{reset}  State: {sc}{bold}{self.state.value}{reset}  \u2550\u2550  {ts}  \u2550\u2550")
        lines.append("")

        # Node bars
        bar_width = 20
        for nid, name in NODES.items():
            nd = self.nodes[nid]
            ratio = min(nd.ratio, 3.0)
            pct = ratio / 3.0
            filled = int(pct * bar_width)
            bar = "\u2588" * filled + "\u2591" * (bar_width - filled)

            # Color bar based on ratio
            if nd.ratio > DETECT_RATIO:
                bar_color = "\033[31m"  # red
            elif nd.ratio > CLEAR_RATIO:
                bar_color = "\033[33m"  # yellow
            else:
                bar_color = "\033[32m"  # green

            pct_str = f"{nd.ratio * 100:5.0f}%"
            stale = " ?" if (now - nd.last_update) > 5 else ""

            lines.append(
                f"  {name:8s} {bar_color}[{bar}]{reset}  "
                f"{nd.score:.2f} / {nd.threshold:.2f}  ({pct_str})  "
                f"turb: {nd.turbulence:.2f}{stale}"
            )

        lines.append("")

        # State-specific info
        if self.state == State.DETECTING:
            elapsed = now - self.detect_start
            lines.append(f"  {dim}Detecting... {elapsed:.1f}s / {CONFIRM_TIME:.0f}s{reset}")
        elif self.state == State.PRESENT:
            elapsed = now - self.present_start
            direction = self._compute_direction()
            lines.append(f"  {bold}Přítomnost: {elapsed:.0f}s  směr: {direction}  peak: {self.global_peak:.1f}\u00d7{reset}")
        elif self.state == State.CLEARING:
            elapsed = now - self.clear_start
            lines.append(f"  {dim}Clearing... {elapsed:.1f}s / {CLEAR_TIME:.0f}s{reset}")

        lines.append("")
        lines.append(f"  {dim}\u2500\u2500\u2500 Events \u2500\u2500\u2500{reset}")

        if self.events:
            for ev in reversed(self.events[-8:]):
                lines.append(ev.fmt())
        else:
            lines.append(f"  {dim}(žádné události){reset}")

        lines.append("")

        if self.no_clear:
            # Scrolling mode — just print the dashboard once per update
            print("\n".join(lines))
        else:
            # ANSI refresh — move to top-left and overwrite
            output = "\033[H\033[2J" + "\n".join(lines)
            sys.stdout.write(output)
            sys.stdout.flush()


def main():
    p = argparse.ArgumentParser(description="CSI Real-time Presence Detector")
    p.add_argument("--no-clear", action="store_true",
                   help="Scrolling mode (no ANSI screen clear)")
    args = p.parse_args()
    CSIDetector(no_clear=args.no_clear).run()


if __name__ == "__main__":
    main()
