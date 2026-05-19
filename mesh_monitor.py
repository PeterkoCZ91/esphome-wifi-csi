#!/usr/bin/env python3
"""
Mesh Range Monitor — live terminal dashboard for 3-node ESP32-C5 mesh.

Usage:
  python3 mesh_monitor.py               # default 3 nodes
  python3 mesh_monitor.py --nodes c5b c5c   # RX nodes only

Ctrl+C to exit.
"""

import argparse
import os
import sys
import time
import threading
from datetime import datetime

import paho.mqtt.client as mqtt

BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

ALL_NODES = ["csi_obyvak_c5", "csi_obyvak_c5b", "csi_obyvak_c5c"]

# MQTT topics per node (suffix after node prefix)
TOPICS = {
    "raw_rate":     "sensor/csi_raw_packet_rate/state",
    "turbulence":   "sensor/csi_turbulence/state",
    "rssi":         "sensor/csi_packet_rssi/state",
    "channel":      "sensor/csi_packet_channel/state",
    "packets":      "sensor/csi_raw_packets_total/state",
    "valid_len":    "sensor/csi_valid_length/state",
    "status":       "status",
}

STALE_TIMEOUT = 15.0   # seconds without update → mark stale


class NodeState:
    def __init__(self, name):
        self.name = name
        self.short = name.replace("csi_obyvak_", "").upper()
        self.raw_rate = None
        self.turbulence = None
        self.rssi = None
        self.channel = None
        self.packets = None
        self.valid_len = None
        self.online = None
        self.last_update = 0.0

    def is_stale(self):
        return (time.time() - self.last_update) > STALE_TIMEOUT and self.last_update > 0


def _bar(value, max_val, width=20, char="█", empty="░"):
    if value is None or max_val == 0:
        return empty * width
    filled = min(int(value / max_val * width), width)
    return char * filled + empty * (width - filled)


def _rate_color(rate):
    if rate is None:
        return "\033[90m"   # grey
    if rate >= 10:
        return "\033[92m"   # green
    if rate >= 3:
        return "\033[93m"   # yellow
    return "\033[91m"       # red


RESET = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"
GREEN = "\033[92m"
YELLOW= "\033[93m"
RED   = "\033[91m"
GREY  = "\033[90m"
CYAN  = "\033[96m"


def render(states, start_ts):
    lines = []
    now = datetime.now().strftime("%H:%M:%S")
    elapsed = int(time.time() - start_ts)
    lines.append(f"{BOLD}=== ESP32-C5 Mesh Range Monitor  {now}  (+{elapsed}s) ==={RESET}")
    lines.append("")

    header = f"  {'Node':<8s}  {'Status':<8s}  {'Rate':>7s}  {'Turb':>6s}  {'RSSI':>6s}  {'Ch':>4s}  {'Len':>5s}  {'Packets':>9s}  Bar"
    lines.append(BOLD + header + RESET)
    lines.append("  " + "─" * 84)

    for st in states:
        status_str = GREY + "?" + RESET
        if st.online is True:
            status_str = (GREY + "STALE" + RESET) if st.is_stale() else (GREEN + "online" + RESET)
        elif st.online is False:
            status_str = RED + "offline" + RESET

        rate = st.raw_rate
        rate_str = f"{rate:.0f} p/s" if rate is not None else "  ---"
        col = _rate_color(rate)

        turb = st.turbulence
        turb_str = f"{turb:.4f}" if turb is not None else "  ----"
        turb_col = GREEN if (turb is not None and turb > 0.05) else GREY

        rssi = st.rssi
        rssi_str = f"{rssi:.0f}" if rssi is not None else " ---"
        rssi_col = GREEN if (rssi is not None and rssi > -70) else (YELLOW if rssi is not None else GREY)

        ch = st.channel
        ch_str = f"{ch:.0f}" if ch is not None else " --"

        vl = st.valid_len
        vl_str = f"{vl:.0f}B" if vl is not None else " ---"

        pkts = st.packets
        pkts_str = f"{pkts:,.0f}" if pkts is not None else "       ---"

        bar = col + _bar(rate, 50) + RESET

        lines.append(
            f"  {BOLD}{st.short:<8s}{RESET}"
            f"  {status_str:<8s}"
            f"  {col}{rate_str:>7s}{RESET}"
            f"  {turb_col}{turb_str:>6s}{RESET}"
            f"  {rssi_col}{rssi_str:>6s}{RESET}"
            f"  {ch_str:>4s}"
            f"  {vl_str:>5s}"
            f"  {pkts_str:>9s}"
            f"  {bar}"
        )

    lines.append("")
    lines.append(f"  {DIM}Bar = raw packet rate (max 50 pkt/s)  |  Ctrl+C to exit{RESET}")
    return "\n".join(lines)


def run(nodes):
    states = {n: NodeState(n) for n in nodes}
    lock = threading.Lock()
    start_ts = time.time()

    def on_connect(client, ud, flags, rc):
        if rc != 0:
            print(f"MQTT connect failed rc={rc}", file=sys.stderr)
            return
        for node in nodes:
            for key, suffix in TOPICS.items():
                topic = f"esphome/{node}/{suffix}"
                client.subscribe(topic)

    def on_message(client, ud, msg):
        topic = msg.topic
        val = msg.payload.decode(errors="replace").strip()
        # parse node name from topic: esphome/<node>/...
        parts = topic.split("/")
        if len(parts) < 3:
            return
        node = parts[1]
        if node not in states:
            return
        suffix = "/".join(parts[2:])
        with lock:
            st = states[node]
            st.last_update = time.time()
            try:
                if suffix == TOPICS["raw_rate"]:
                    st.raw_rate = float(val)
                elif suffix == TOPICS["turbulence"]:
                    st.turbulence = float(val)
                elif suffix == TOPICS["rssi"]:
                    st.rssi = float(val)
                elif suffix == TOPICS["channel"]:
                    st.channel = float(val)
                elif suffix == TOPICS["packets"]:
                    st.packets = float(val)
                elif suffix == TOPICS["valid_len"]:
                    st.valid_len = float(val)
                elif suffix == TOPICS["status"]:
                    st.online = (val.lower() == "online")
            except ValueError:
                pass

    client = mqtt.Client(client_id=f"mesh-monitor-{os.getpid()}")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, 1883)
    client.loop_start()

    try:
        while True:
            with lock:
                ordered = [states[n] for n in nodes]
                output = render(ordered, start_ts)
            # Clear screen and redraw
            print("\033[2J\033[H" + output, end="", flush=True)
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\n\nStopped.")
    finally:
        client.loop_stop()
        client.disconnect()


def main():
    p = argparse.ArgumentParser(description="Mesh Range Monitor")
    p.add_argument(
        "--nodes", nargs="+", default=ALL_NODES,
        help="Node names to monitor (default: all 3 C5 nodes)"
    )
    args = p.parse_args()

    # Normalize: allow short names like c5, c5b, c5c
    resolved = []
    for n in args.nodes:
        if not n.startswith("csi_"):
            n = "csi_obyvak_" + n if n.startswith("c5") else "csi_" + n
        resolved.append(n)

    run(resolved)


if __name__ == "__main__":
    main()
