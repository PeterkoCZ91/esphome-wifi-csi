#!/usr/bin/env python3
"""
CSI Logger — SQLite recording + radar autolabeling + analysis.

Records:
- Each movement_score sample (1/s per node)
- Motion ON/OFF events with timestamps
- Walk events (motion ON→OFF sequences) with duration and max score
- Threshold changes, calibration
- Raw subcarrier amplitudes (12 values CSV) for ML training
- Automatic labels from radars (LD2412, LD2450)

Usage:
  python3 csi_logger.py              # log + live output
  python3 csi_logger.py --autolabel  # headless with radar autolabeling
  python3 csi_logger.py --record     # manual labeling (e/w/s/f/q)
  python3 csi_logger.py --query      # last 20 walks
  python3 csi_logger.py --stats      # summary statistics
  python3 csi_logger.py --raw-stats  # raw subcarrier data statistics
  python3 csi_logger.py --export     # export labeled data to CSV
  python3 csi_logger.py --db-info    # DB info (size, rows, date range)
  python3 csi_logger.py --cleanup 7  # delete unlabeled data older than 7 days
  python3 csi_logger.py --analyze    # data quality report
  python3 csi_logger.py --tail 50    # last 50 samples
"""
import argparse
import csv
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta

import paho.mqtt.client as mqtt

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csi_log.db")
SESSION_LOCKFILE = "/tmp/csi_lab_session.json"
BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

NODES = ["csi_obyvak1", "csi_chodba_zasuvka", "csi_kuchyn", "csi_senzor", "csi_loznice", "csi_ezotv", "csi_pracovna", "csi_pracovna_b", "csi_pracovna_c", "csi_obyvak_c6", "csi_obyvak_c5", "csi_obyvak_c5b", "csi_obyvak_c5c", "csi_obyvak_c5d"]
NUM_SUBCARRIERS = 12

# Phase 1: Data quality constants
MAX_WALK_DURATION = 300.0   # 5 min cap
MIN_WALK_DURATION = 0.5     # ignore blips
MAX_MOVEMENT_SCORE = 10.0   # outlier flag

# Phase 2: Radar autolabel config
RADAR_ROOM_MAP = {
    "ld2412_obyvak": ["csi_obyvak1", "csi_obyvak_c6", "csi_obyvak_c5"],
    "ld2450_pokoj_obyvak": ["csi_obyvak1", "csi_obyvak_c6", "csi_obyvak_c5"],
    "mw1_4BA4": ["csi_kuchyn"],
}
RADAR_TOPICS = {
    "ld2412_obyvak": {"availability": "security/ld2412_obyvak/availability",
                      "presence": "security/ld2412_obyvak/presence/state"},
    "ld2450_pokoj_obyvak": {"availability": "security/ld2450_pokoj_obyvak/availability",
                            "presence": "security/ld2450_pokoj_obyvak/presence/state",
                            "count": "security/ld2450_pokoj_obyvak/tracking/count"},
    "mw1_4BA4": {"presence": "security/mw1_4BA4/presence/state",
                 "count": "security/mw1_4BA4/tracking/count"},
}
LABEL_TRANSITION_GRACE = 5.0   # don't label during transitions
LABEL_EMPTY_DELAY = 10.0       # radar must be clear 10s before "empty" label
STUCK_CHECK_INTERVAL = 30.0    # check stuck motion every 30s


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(db: sqlite3.Connection):
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            node TEXT NOT NULL,
            movement_score REAL,
            threshold REAL,
            motion INTEGER  -- 1/0
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            node TEXT NOT NULL,
            type TEXT NOT NULL,      -- motion_on, motion_off, walk, stuck_motion, calibration
            duration_s REAL,         -- for walk events
            max_score REAL,          -- peak score during walk
            detail TEXT              -- JSON extra info
        );
        CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
        CREATE INDEX IF NOT EXISTS idx_samples_node ON samples(node, ts);
        CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
        CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, ts);

        CREATE TABLE IF NOT EXISTS raw_subcarriers (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            node TEXT NOT NULL,
            sc0 REAL, sc1 REAL, sc2 REAL, sc3 REAL,
            sc4 REAL, sc5 REAL, sc6 REAL, sc7 REAL,
            sc8 REAL, sc9 REAL, sc10 REAL, sc11 REAL,
            movement_score REAL,
            motion INTEGER,
            label TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_subcarriers(ts);
        CREATE INDEX IF NOT EXISTS idx_raw_label ON raw_subcarriers(label);
    """)
    db.commit()
    # Migration: add phase_turbulence column if missing (ALTER TABLE fails silently if exists)
    try:
        db.execute("ALTER TABLE raw_subcarriers ADD COLUMN phase_turbulence REAL")
        db.commit()
    except Exception:
        pass  # already exists
    try:
        db.execute("ALTER TABLE raw_subcarriers ADD COLUMN breathing_score REAL")
        db.commit()
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE raw_subcarriers ADD COLUMN presence INTEGER")
        db.commit()
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE raw_subcarriers ADD COLUMN dser REAL")
        db.commit()
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE raw_subcarriers ADD COLUMN plcr REAL")
        db.commit()
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE raw_subcarriers ADD COLUMN session_id INTEGER")
        db.commit()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# MQTT Logger
# ---------------------------------------------------------------------------

class CSILogger:
    LABEL_KEYS = {"e": "empty", "w": "walk", "s": "sit", "f": "fall"}

    FLUSH_INTERVAL = 5.0  # seconds between DB commits
    FLUSH_BATCH = 50      # or commit after this many pending writes

    def __init__(self, record=False, autolabel=False, session=False):
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        init_db(self.db)
        self.record = record
        self.autolabel = autolabel
        self.current_label = None  # None = no labeling (manual mode)
        self.session_mode = session
        self.current_session_id = None   # set from lockfile when --session active
        self._session_lock = threading.Lock()
        self._db_lock = threading.Lock()
        self._pending = 0
        self._last_flush = time.time()

        # Fusion score: average movement_score across C5 RX nodes (max 5s stale)
        self.FUSION_NODES = ["csi_obyvak_c5b", "csi_obyvak_c5c", "csi_obyvak_c5d"]
        self.FUSION_STALE_S = 5.0
        self._fusion_scores: dict = {}  # {node: (ts, score)}

        # Per-node state tracking
        self.state = {}
        for node in NODES:
            self.state[node] = {
                "motion": False,
                "motion_start": 0.0,
                "max_score": 0.0,
                "threshold": 0.0,
                "last_score": 0.0,
                "phase_turbulence": None,
                "breathing_score": None,
                "presence": None,
                "dser": None,
                "plcr": None,
            }

        # Phase 2: Radar state for autolabeling
        self.radar_state = {}
        if self.autolabel:
            for radar_id in RADAR_TOPICS:
                self.radar_state[radar_id] = {
                    "presence": None,       # True/False/None
                    "count": None,          # int or None
                    "available": None,      # True/False/None if topic is not published
                    "last_change": 0.0,     # timestamp of last state change
                    "last_seen": 0.0,       # timestamp of last MQTT message
                    "clear_since": None,    # timestamp when radar went clear
                }

    def _db_write(self, sql, params):
        """Thread-safe buffered INSERT. Commits are batched."""
        with self._db_lock:
            self.db.execute(sql, params)
            self._pending += 1
            now = time.time()
            if self._pending >= self.FLUSH_BATCH or now - self._last_flush >= self.FLUSH_INTERVAL:
                self.db.commit()
                self._pending = 0
                self._last_flush = now

    def _flush(self):
        """Force commit any pending writes."""
        with self._db_lock:
            if self._pending > 0:
                self.db.commit()
                self._pending = 0
                self._last_flush = time.time()

    def _load_session_(self):
        """Read /tmp/csi_lab_session.json and update label + session_id."""
        if not self.session_mode:
            return
        if not os.path.exists(SESSION_LOCKFILE):
            with self._session_lock:
                self.current_label = None
                self.current_session_id = None
            return
        try:
            with open(SESSION_LOCKFILE) as f:
                data = json.load(f)
            with self._session_lock:
                self.current_label = data.get("label")
                self.current_session_id = data.get("session_id")
        except Exception:
            pass

    def run(self):
        client = mqtt.Client()
        client.username_pw_set(MQTT_USER, MQTT_PASS)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.connect(BROKER, 1883)
        print(f"CSI Logger — {DB_PATH}")
        print(f"Connecting to {BROKER}...")
        if self.record:
            print("\n  LABELING mode — keys: [e]mpty [w]alk [s]it [f]all [q]uit")
            print(f"  Active label: (none)\n")
        elif self.autolabel:
            print("\n  AUTOLABEL mode — labels from radars (headless)\n")
        else:
            print()

        old_settings = None
        try:
            if self.record:
                import select
                import termios
                import tty
                old_settings = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())

            client.loop_start()
            # Start session polling thread (reads lockfile every 5s)
            if self.session_mode:
                self._load_session_()   # initial load

                def _session_poll():
                    while True:
                        time.sleep(5.0)
                        self._load_session_()

                t = threading.Thread(target=_session_poll, daemon=True)
                t.start()
                lock = None
                if os.path.exists(SESSION_LOCKFILE):
                    try:
                        with open(SESSION_LOCKFILE) as f:
                            lock = json.load(f)
                    except Exception:
                        pass
                if lock:
                    print(f"\n  SESSION mode — active session #{lock.get('session_id')} [{lock.get('room')}:{lock.get('activity')}]")
                    print(f"  Label: {lock.get('label')}\n")
                else:
                    print("\n  SESSION mode — waiting for session (run: python lab_session.py start ...)\n")

            last_stuck_check = time.time()

            while True:
                if self.record:
                    import select as sel_mod
                    if sel_mod.select([sys.stdin], [], [], 0.1)[0]:
                        ch = sys.stdin.read(1).lower()
                        if ch == "q":
                            break
                        if ch in self.LABEL_KEYS:
                            self.current_label = self.LABEL_KEYS[ch]
                            print(f"\n  >>> LABEL: {self.current_label} <<<\n")
                else:
                    time.sleep(0.1)

                # Phase 1: Periodic stuck motion check
                now = time.time()
                if now - last_stuck_check >= STUCK_CHECK_INTERVAL:
                    last_stuck_check = now
                    self._check_stuck_motion(now)

        except KeyboardInterrupt:
            pass
        finally:
            if old_settings is not None:
                import termios
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            print("\nShutting down...")
            client.loop_stop()
            self._flush()
            self.db.close()

    def _check_stuck_motion(self, now):
        """Detect nodes stuck in motion state > MAX_WALK_DURATION and synthesize events."""
        for node, st in self.state.items():
            if st["motion"] and (now - st["motion_start"]) > MAX_WALK_DURATION:
                duration = now - st["motion_start"]
                short_node = node.replace("csi_", "")
                detail = json.dumps({
                    "max_score": round(st["max_score"], 4),
                    "threshold": round(st["threshold"], 4),
                    "reason": "stuck_timeout",
                })
                self._db_write(
                    "INSERT INTO events (ts, node, type, duration_s, max_score, detail) VALUES (?,?,?,?,?,?)",
                    (now, node, "stuck_motion", duration, st["max_score"], detail),
                )
                self._db_write(
                    "INSERT INTO events (ts, node, type) VALUES (?,?,?)",
                    (now, node, "motion_off"),
                )
                st["motion"] = False
                self._flush()
                ts_str = datetime.fromtimestamp(now).strftime("%H:%M:%S")
                print(f"  ⚠ {ts_str} {short_node}: STUCK MOTION reset ({duration:.0f}s)")

    def _on_connect(self, client, ud, flags, rc):
        print(f"MQTT connected (rc={rc})")
        for node in NODES:
            client.subscribe(f"esphome/{node}/sensor/movement_score/state")
            client.subscribe(f"esphome/{node}/sensor/csi_active_threshold/state")
            client.subscribe(f"esphome/{node}/binary_sensor/motion_detected/state")
            client.subscribe(f"esphome/{node}/binary_sensor/csi_calibrating/state")
            client.subscribe(f"esphome/{node}/sensor/csi_subcarrier_amplitudes/state")
            client.subscribe(f"esphome/{node}/sensor/csi_phase_turbulence/state")
            client.subscribe(f"esphome/{node}/sensor/breathing_score/state")
            client.subscribe(f"esphome/{node}/binary_sensor/presence_detected/state")
            client.subscribe(f"esphome/{node}/sensor/dser/state")
            client.subscribe(f"esphome/{node}/sensor/plcr/state")

        # Phase 2: Subscribe to radar topics for autolabeling
        if self.autolabel:
            for radar_id, topics in RADAR_TOPICS.items():
                for key, topic in topics.items():
                    client.subscribe(topic)
                    print(f"  Radar subscribe: {topic}")

        print("Listening for CSI data...\n")

    def _on_message(self, client, ud, msg):
        now = time.time()
        topic = msg.topic
        val = msg.payload.decode(errors="replace").strip()

        # Phase 2: Radar messages
        if self.autolabel and topic.startswith("security/"):
            self._handle_radar_message(topic, val, now)
            return

        parts = topic.split("/")
        node = parts[1]  # csi_obyvak / csi_chodba

        if node not in self.state:
            return

        st = self.state[node]
        short_node = node.replace("csi_", "")

        # Movement score
        if "movement_score" in topic:
            try:
                score = float(val)
            except ValueError:
                return
            st["last_score"] = score
            if st["motion"]:
                # Phase 1: Cap outlier tracking
                if score <= MAX_MOVEMENT_SCORE:
                    st["max_score"] = max(st["max_score"], score)

            self._db_write(
                "INSERT INTO samples (ts, node, movement_score, threshold, motion) VALUES (?,?,?,?,?)",
                (now, node, score, st["threshold"], 1 if st["motion"] else 0),
            )

            # Update fusion score if this is a C5 RX node
            if node in self.FUSION_NODES:
                self._fusion_scores[node] = (now, score)

            # Live output — bar
            thr = st["threshold"] if st["threshold"] > 0 else 1.0
            ratio = min(score / thr, 2.0)
            bar_len = 30
            filled = int(ratio / 2.0 * bar_len)
            bar = "\u2588" * filled + "\u2591" * (bar_len - filled)
            motion_str = " \u25cf MOTION" if st["motion"] else ""
            ts_str = datetime.fromtimestamp(now).strftime("%H:%M:%S")
            # Phase 1: Flag outliers
            outlier_str = " OUTLIER!" if score > MAX_MOVEMENT_SCORE else ""
            fresh = [(ts, s) for ts, s in self._fusion_scores.values() if now - ts <= self.FUSION_STALE_S]
            fusion_str = f"  fusion={sum(s for _, s in fresh)/len(fresh):.3f}[{len(fresh)}n]" if len(fresh) >= 2 else ""
            print(f"  {ts_str} {short_node:7s} [{bar}] {score:.4f} / {st['threshold']:.4f} ({ratio:.0%}){motion_str}{outlier_str}{fusion_str}")

        # Threshold
        elif "csi_active_threshold" in topic:
            try:
                st["threshold"] = float(val)
            except ValueError:
                pass

        # Motion ON/OFF
        elif "motion_detected" in topic:
            is_on = val.upper() == "ON"

            if is_on and not st["motion"]:
                # Motion started
                st["motion"] = True
                st["motion_start"] = now
                st["max_score"] = st["last_score"]
                self._db_write(
                    "INSERT INTO events (ts, node, type) VALUES (?,?,?)",
                    (now, node, "motion_on"),
                )
                ts_str = datetime.fromtimestamp(now).strftime("%H:%M:%S")
                print(f"\n  \u25b6 {ts_str} {short_node}: MOTION START")

            elif not is_on and st["motion"]:
                # Motion ended
                st["motion"] = False
                duration = now - st["motion_start"]

                # Phase 1: Always log motion_off
                self._db_write(
                    "INSERT INTO events (ts, node, type) VALUES (?,?,?)",
                    (now, node, "motion_off"),
                )

                ts_str = datetime.fromtimestamp(now).strftime("%H:%M:%S")

                if duration < MIN_WALK_DURATION:
                    # Phase 1: Skip blips — don't log walk event
                    print(f"  \u25a0 {ts_str} {short_node}: MOTION BLIP ({duration:.2f}s) — skipped")
                elif duration > MAX_WALK_DURATION:
                    # Phase 1: Stuck motion — log as stuck_motion
                    detail = json.dumps({
                        "max_score": round(st["max_score"], 4),
                        "threshold": round(st["threshold"], 4),
                        "reason": "over_cap",
                    })
                    self._db_write(
                        "INSERT INTO events (ts, node, type, duration_s, max_score, detail) VALUES (?,?,?,?,?,?)",
                        (now, node, "stuck_motion", duration, st["max_score"], detail),
                    )
                    print(f"  \u26a0 {ts_str} {short_node}: STUCK MOTION — {duration:.0f}s, peak={st['max_score']:.4f}")
                else:
                    # Normal walk
                    detail = json.dumps({
                        "max_score": round(st["max_score"], 4),
                        "threshold": round(st["threshold"], 4),
                        "ratio": round(st["max_score"] / max(st["threshold"], 0.001), 1),
                    })
                    self._db_write(
                        "INSERT INTO events (ts, node, type, duration_s, max_score, detail) VALUES (?,?,?,?,?,?)",
                        (now, node, "walk", duration, st["max_score"], detail),
                    )
                    print(f"  \u25a0 {ts_str} {short_node}: MOTION END  — {duration:.1f}s, peak={st['max_score']:.4f}\n")

            self._flush()  # Events are rare — flush immediately

        # Calibration
        elif "csi_calibrating" in topic:
            is_cal = val.upper() == "ON"
            if is_cal:
                self._db_write(
                    "INSERT INTO events (ts, node, type) VALUES (?,?,?)",
                    (now, node, "calibration"),
                )
                self._flush()
                ts_str = datetime.fromtimestamp(now).strftime("%H:%M:%S")
                print(f"  \u2699 {ts_str} {short_node}: CALIBRATION")

        # Phase turbulence — std of inter-subcarrier phase diffs
        elif "csi_phase_turbulence" in topic:
            try:
                st["phase_turbulence"] = float(val)
            except ValueError:
                pass

        # Breathing score — RMS of bandpass-filtered amplitude (0.08-0.6 Hz)
        elif "breathing_score" in topic:
            try:
                st["breathing_score"] = float(val)
            except ValueError:
                pass

        # Presence — breathing + phase combo detection
        elif "presence_detected" in topic:
            st["presence"] = val.lower() in ("on", "true", "1")

        # Uni-Fi DSER — Dynamic-to-Static Energy Ratio (arXiv 2601.10980 eq. 3)
        elif topic.endswith("/sensor/dser/state"):
            try:
                st["dser"] = float(val)
            except ValueError:
                pass

        # Uni-Fi PLCR — Path-Length Change Rate proxy (arXiv 2601.10980 eq. 4)
        elif topic.endswith("/sensor/plcr/state"):
            try:
                st["plcr"] = float(val)
            except ValueError:
                pass

        # Subcarrier amplitudes (CSV: "12.3,45.6,...")
        elif "csi_subcarrier_amplitudes" in topic:
            try:
                values = [float(x) for x in val.split(",")]
            except ValueError:
                return
            # Pad/truncate to NUM_SUBCARRIERS
            values = (values + [None] * NUM_SUBCARRIERS)[:NUM_SUBCARRIERS]

            # Determine label: autolabel from radar, manual, or None
            label = self._get_autolabel(node) if self.autolabel else self.current_label

            pres_val = 1 if st["presence"] else 0 if st["presence"] is not None else None
            with self._session_lock:
                session_id = self.current_session_id if self.session_mode else None
            self._db_write(
                "INSERT INTO raw_subcarriers "
                "(ts, node, sc0, sc1, sc2, sc3, sc4, sc5, sc6, sc7, sc8, sc9, sc10, sc11, "
                "movement_score, motion, label, phase_turbulence, breathing_score, presence, "
                "dser, plcr, session_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, node, *values, st["last_score"],
                 1 if st["motion"] else 0, label, st["phase_turbulence"],
                 st["breathing_score"], pres_val, st["dser"], st["plcr"], session_id),
            )

            ts_str = datetime.fromtimestamp(now).strftime("%H:%M:%S")
            sc_preview = " ".join(f"{v:.1f}" if v is not None else "-" for v in values[:6])
            label_str = f" [{label}]" if label else ""
            pt = st["phase_turbulence"]
            pt_str = f" pt:{pt:.3f}" if pt is not None else ""
            bs = st["breathing_score"]
            bs_str = f" br:{bs:.3f}" if bs is not None else ""
            pres_str = " P" if st["presence"] else ""
            print(f"  {ts_str} {short_node:7s} SC: {sc_preview} ...{pt_str}{bs_str}{pres_str}{label_str}")

    # -------------------------------------------------------------------
    # Phase 2: Radar autolabel
    # -------------------------------------------------------------------

    def _handle_radar_message(self, topic, val, now):
        """Parse radar MQTT message and update radar_state."""
        for radar_id, topics in RADAR_TOPICS.items():
            for key, t in topics.items():
                if topic == t:
                    rs = self.radar_state[radar_id]
                    rs["last_seen"] = now

                    if key == "availability":
                        rs["available"] = val.lower() == "online"

                    elif key == "presence":
                        # LD2412: Detected/idle, LD2450: Detected/Clear
                        new_presence = val.lower() in ("detected", "on", "true", "1", "occupied")
                        old_presence = rs["presence"]

                        if new_presence != old_presence:
                            rs["presence"] = new_presence
                            rs["last_change"] = now

                            if new_presence:
                                rs["clear_since"] = None
                            else:
                                rs["clear_since"] = now

                            nodes = RADAR_ROOM_MAP.get(radar_id, [])
                            short_radar = radar_id
                            node_str = ",".join(n.replace("csi_", "") for n in nodes)
                            state_str = "DETECTED" if new_presence else "CLEAR"
                            ts_str = datetime.fromtimestamp(now).strftime("%H:%M:%S")
                            print(f"  \U0001f4e1 {ts_str} radar/{short_radar}: {state_str} -> {node_str}")

                    elif key == "count":
                        try:
                            rs["count"] = int(val)
                        except ValueError:
                            pass
                    return

    def _get_autolabel(self, node):
        """Get autolabel for a CSI node based on mapped radar state.

        Returns 'occupied', 'empty', or None (during grace/transition).
        """
        if not self.autolabel:
            return None

        now = time.time()

        mapped_radars = [
            radar_id for radar_id, csi_nodes in RADAR_ROOM_MAP.items()
            if node in csi_nodes
        ]
        if not mapped_radars:
            return None

        active_states = []
        for radar_id, csi_nodes in RADAR_ROOM_MAP.items():
            if node not in csi_nodes:
                continue

            rs = self.radar_state[radar_id]

            # Ignore radars that explicitly report offline. Their retained presence
            # value may be stale and would otherwise poison labels for days.
            if rs["available"] is False:
                continue

            if rs["presence"] is None:
                continue

            # Grace period after state change
            if (now - rs["last_change"]) < LABEL_TRANSITION_GRACE:
                return None

            active_states.append(rs)

        if not active_states:
            return None  # No online/current radar data yet

        if any(rs["presence"] for rs in active_states):
            return "occupied"

        # Empty delay: every active radar must be clear long enough.
        all_clear_stable = all(
            rs["clear_since"] and (now - rs["clear_since"]) >= LABEL_EMPTY_DELAY
            for rs in active_states
        )
        return "empty" if all_clear_stable else None


# ---------------------------------------------------------------------------
# Query modes
# ---------------------------------------------------------------------------

def query_walks(limit=20):
    db = sqlite3.connect(DB_PATH)
    print(f"\n{'Time':>10s}  {'Node':>8s}  {'Duration':>7s}  {'Peak':>8s}  {'Threshold':>10s}  {'Ratio':>6s}  {'Type':>12s}")
    print("\u2500" * 72)
    rows = db.execute(
        "SELECT ts, node, duration_s, max_score, detail, type FROM events "
        "WHERE type IN ('walk', 'stuck_motion') ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    for ts, node, dur, peak, detail, etype in reversed(rows):
        t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        short = node.replace("csi_", "")
        d = json.loads(detail) if detail else {}
        ratio = d.get("ratio", 0)
        thr = d.get("threshold", 0)
        print(f"  {t:>8s}  {short:>8s}  {dur:>5.1f}s  {peak:>8.4f}  {thr:>10.4f}  {ratio:>5.1f}x  {etype:>12s}")
    if not rows:
        print("  (no walks)")
    db.close()


def query_stats():
    db = sqlite3.connect(DB_PATH)
    now = time.time()
    h1 = now - 3600
    h24 = now - 86400

    print("\n=== CSI Statistics ===\n")

    for node in NODES:
        short = node.replace("csi_", "")
        total_walks = db.execute(
            "SELECT COUNT(*) FROM events WHERE node=? AND type='walk'", (node,)
        ).fetchone()[0]
        walks_1h = db.execute(
            "SELECT COUNT(*) FROM events WHERE node=? AND type='walk' AND ts>?", (node, h1)
        ).fetchone()[0]
        walks_24h = db.execute(
            "SELECT COUNT(*) FROM events WHERE node=? AND type='walk' AND ts>?", (node, h24)
        ).fetchone()[0]
        stuck = db.execute(
            "SELECT COUNT(*) FROM events WHERE node=? AND type='stuck_motion'", (node,)
        ).fetchone()[0]

        avg_dur = db.execute(
            "SELECT AVG(duration_s) FROM events WHERE node=? AND type='walk'", (node,)
        ).fetchone()[0]
        avg_peak = db.execute(
            "SELECT AVG(max_score) FROM events WHERE node=? AND type='walk'", (node,)
        ).fetchone()[0]
        max_peak = db.execute(
            "SELECT MAX(max_score) FROM events WHERE node=? AND type='walk'", (node,)
        ).fetchone()[0]

        samples = db.execute(
            "SELECT COUNT(*) FROM samples WHERE node=?", (node,)
        ).fetchone()[0]

        print(f"  {short}:")
        print(f"    Walks: {total_walks} total, {walks_24h} in 24h, {walks_1h} in 1h")
        if stuck:
            print(f"    Stuck motion: {stuck}")
        if avg_dur:
            print(f"    Avg duration: {avg_dur:.1f}s")
        if avg_peak:
            print(f"    Avg peak: {avg_peak:.4f}, max: {max_peak:.4f}")
        print(f"    Samples: {samples}")
        print()

    db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    print(f"  DB size: {db_size / 1024 / 1024:.1f} MB")
    db.close()


def query_tail(n=50):
    db = sqlite3.connect(DB_PATH)
    print(f"\n{'Time':>10s}  {'Node':>8s}  {'Score':>8s}  {'Threshold':>10s}  {'Motion':>7s}")
    print("\u2500" * 55)
    rows = db.execute(
        "SELECT ts, node, movement_score, threshold, motion FROM samples "
        "ORDER BY ts DESC LIMIT ?", (n,)
    ).fetchall()
    for ts, node, score, thr, motion in reversed(rows):
        t = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        short = node.replace("csi_", "")
        m = "\u25cf" if motion else ""
        print(f"  {t:>8s}  {short:>8s}  {score:>8.4f}  {thr:>10.4f}  {m:>4s}")
    db.close()


def query_raw_stats():
    db = sqlite3.connect(DB_PATH)
    init_db(db)
    print("\n=== Raw Subcarrier Statistics ===\n")

    total = db.execute("SELECT COUNT(*) FROM raw_subcarriers").fetchone()[0]
    labeled = db.execute("SELECT COUNT(*) FROM raw_subcarriers WHERE label IS NOT NULL").fetchone()[0]
    unlabeled = total - labeled
    print(f"  Total: {total}  (labeled: {labeled}, unlabeled: {unlabeled})\n")

    # Per node
    print(f"  {'Node':>10s}  {'Total':>8s}  {'Labeled':>8s}")
    print("  " + "\u2500" * 32)
    for node in NODES:
        short = node.replace("csi_", "")
        cnt = db.execute("SELECT COUNT(*) FROM raw_subcarriers WHERE node=?", (node,)).fetchone()[0]
        lab = db.execute("SELECT COUNT(*) FROM raw_subcarriers WHERE node=? AND label IS NOT NULL", (node,)).fetchone()[0]
        if cnt > 0:
            print(f"  {short:>10s}  {cnt:>8d}  {lab:>8d}")
    print()

    # Label distribution
    rows = db.execute(
        "SELECT label, COUNT(*) FROM raw_subcarriers WHERE label IS NOT NULL GROUP BY label ORDER BY COUNT(*) DESC"
    ).fetchall()
    if rows:
        print("  Label distribution:")
        for label, cnt in rows:
            print(f"    {label:>10s}: {cnt}")
    print()
    db.close()


# ---------------------------------------------------------------------------
# Phase 3: DB info + cleanup
# ---------------------------------------------------------------------------

def db_info():
    if not os.path.exists(DB_PATH):
        print("DB does not exist.")
        return
    db = sqlite3.connect(DB_PATH)

    db_size = os.path.getsize(DB_PATH)
    # Include WAL/SHM files
    wal_path = DB_PATH + "-wal"
    shm_path = DB_PATH + "-shm"
    total_size = db_size
    if os.path.exists(wal_path):
        total_size += os.path.getsize(wal_path)
    if os.path.exists(shm_path):
        total_size += os.path.getsize(shm_path)

    print(f"\n=== DB Info ===\n")
    print(f"  Path: {DB_PATH}")
    print(f"  Size: {total_size / 1024 / 1024:.1f} MB (db={db_size / 1024 / 1024:.1f} MB)")

    for table in ["samples", "events", "raw_subcarriers"]:
        cnt = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        min_ts = db.execute(f"SELECT MIN(ts) FROM {table}").fetchone()[0]
        max_ts = db.execute(f"SELECT MAX(ts) FROM {table}").fetchone()[0]
        if min_ts and max_ts:
            date_from = datetime.fromtimestamp(min_ts).strftime("%Y-%m-%d %H:%M")
            date_to = datetime.fromtimestamp(max_ts).strftime("%Y-%m-%d %H:%M")
            days = (max_ts - min_ts) / 86400
            daily = cnt / max(days, 1)
            print(f"\n  {table}:")
            print(f"    Rows: {cnt:,}")
            print(f"    Range: {date_from} .. {date_to} ({days:.1f} days)")
            print(f"    Daily avg: {daily:,.0f} rows/day")
        else:
            print(f"\n  {table}: {cnt} rows (empty)")

    # Labeled vs unlabeled in raw_subcarriers
    labeled = db.execute("SELECT COUNT(*) FROM raw_subcarriers WHERE label IS NOT NULL").fetchone()[0]
    total_raw = db.execute("SELECT COUNT(*) FROM raw_subcarriers").fetchone()[0]
    print(f"\n  Labeled data: {labeled:,} / {total_raw:,} ({100 * labeled / max(total_raw, 1):.1f}%)")

    # Label distribution
    rows = db.execute(
        "SELECT label, COUNT(*) FROM raw_subcarriers WHERE label IS NOT NULL GROUP BY label"
    ).fetchall()
    if rows:
        for label, cnt in rows:
            print(f"    {label}: {cnt:,}")

    print()
    db.close()


def db_cleanup(days, archive=False):
    if not os.path.exists(DB_PATH):
        print("DB does not exist.")
        return

    db = sqlite3.connect(DB_PATH)
    cutoff = time.time() - days * 86400
    cutoff_str = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d %H:%M")

    # Count what will be deleted
    del_samples = db.execute(
        "SELECT COUNT(*) FROM samples WHERE ts < ?", (cutoff,)
    ).fetchone()[0]
    del_events = db.execute(
        "SELECT COUNT(*) FROM events WHERE ts < ?", (cutoff,)
    ).fetchone()[0]
    del_raw = db.execute(
        "SELECT COUNT(*) FROM raw_subcarriers WHERE ts < ? AND label IS NULL", (cutoff,)
    ).fetchone()[0]

    print(f"\n=== Cleanup: delete unlabeled data older than {days} days ===")
    print(f"  Cutoff: {cutoff_str}")
    print(f"  Samples to delete: {del_samples:,}")
    print(f"  Events to delete: {del_events:,}")
    print(f"  Raw (unlabeled) to delete: {del_raw:,}")

    if del_samples == 0 and del_events == 0 and del_raw == 0:
        print("  Nothing to delete.")
        db.close()
        return

    # Confirm
    try:
        ans = input("\n  Proceed? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = ""
    if ans != "y":
        print("  Cancelled.")
        db.close()
        return

    # Archive
    if archive:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = DB_PATH.replace(".db", f"_archive_{ts}.db")
        print(f"  Archiving to {archive_path} ...")
        db.close()
        shutil.copy2(DB_PATH, archive_path)
        # Copy WAL too if exists
        if os.path.exists(DB_PATH + "-wal"):
            shutil.copy2(DB_PATH + "-wal", archive_path + "-wal")
        db = sqlite3.connect(DB_PATH)
        print("  Archive done.")

    # Delete
    print("  Deleting ...")
    db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM raw_subcarriers WHERE ts < ? AND label IS NULL", (cutoff,))
    db.commit()

    print("  VACUUM ...")
    db.execute("VACUUM")
    db.commit()

    new_size = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"  Done. New DB size: {new_size:.1f} MB\n")
    db.close()


# ---------------------------------------------------------------------------
# Phase 4: Analysis + Enhanced export
# ---------------------------------------------------------------------------

def analyze():
    if not os.path.exists(DB_PATH):
        print("DB does not exist.")
        return
    db = sqlite3.connect(DB_PATH)
    now = time.time()
    h24 = now - 86400

    print("\n=== Data Quality Report ===\n")

    # Per-node freshness
    print("  --- Per-node freshness ---\n")
    print(f"  {'Node':>10s}  {'24h samples':>12s}  {'24h SC':>8s}  {'Last seen':>20s}")
    print("  " + "\u2500" * 56)
    for node in NODES:
        short = node.replace("csi_", "")
        cnt_24h = db.execute(
            "SELECT COUNT(*) FROM samples WHERE node=? AND ts>?", (node, h24)
        ).fetchone()[0]
        sc_24h = db.execute(
            "SELECT COUNT(*) FROM raw_subcarriers WHERE node=? AND ts>?", (node, h24)
        ).fetchone()[0]
        last_ts = db.execute(
            "SELECT MAX(ts) FROM samples WHERE node=?", (node,)
        ).fetchone()[0]
        if last_ts:
            age = now - last_ts
            if age < 60:
                last_str = f"{age:.0f}s ago"
            elif age < 3600:
                last_str = f"{age / 60:.0f}m ago"
            elif age < 86400:
                last_str = f"{age / 3600:.1f}h ago"
            else:
                last_str = f"{age / 86400:.1f}d ago"
            last_str += " " + datetime.fromtimestamp(last_ts).strftime("%m-%d %H:%M")
        else:
            last_str = "never"
        status = "" if cnt_24h > 0 else " DEAD"
        print(f"  {short:>10s}  {cnt_24h:>12,}  {sc_24h:>8,}  {last_str:>20s}{status}")

    # Gap detection (>5min gaps in samples, last 24h)
    print("\n  --- Gap detection (>5min, last 24h) ---\n")
    found_gaps = False
    for node in NODES:
        short = node.replace("csi_", "")
        rows = db.execute(
            "SELECT ts FROM samples WHERE node=? AND ts>? ORDER BY ts", (node, h24)
        ).fetchall()
        if len(rows) < 2:
            continue
        gaps = []
        for i in range(1, len(rows)):
            gap = rows[i][0] - rows[i - 1][0]
            if gap > 300:  # >5min
                gap_start = datetime.fromtimestamp(rows[i - 1][0]).strftime("%H:%M")
                gap_end = datetime.fromtimestamp(rows[i][0]).strftime("%H:%M")
                gaps.append((gap, gap_start, gap_end))
        if gaps:
            found_gaps = True
            print(f"  {short}: {len(gaps)} gaps")
            for gap, gs, ge in gaps[:5]:
                print(f"    {gs} - {ge} ({gap / 60:.0f}min)")
            if len(gaps) > 5:
                print(f"    ... and {len(gaps) - 5} more")
    if not found_gaps:
        print("  No gaps found.")

    # Walk analysis
    print("\n  --- Walk analysis ---\n")
    for node in NODES:
        short = node.replace("csi_", "")
        walks = db.execute(
            "SELECT duration_s, max_score FROM events WHERE node=? AND type='walk'", (node,)
        ).fetchall()
        stuck = db.execute(
            "SELECT COUNT(*) FROM events WHERE node=? AND type='stuck_motion'", (node,)
        ).fetchone()[0]
        if not walks:
            continue

        durations = [w[0] for w in walks if w[0] is not None]
        scores = [w[1] for w in walks if w[1] is not None]

        blips = sum(1 for d in durations if d < MIN_WALK_DURATION)
        over_cap = sum(1 for d in durations if d > MAX_WALK_DURATION)
        outlier_scores = sum(1 for s in scores if s > MAX_MOVEMENT_SCORE)

        if durations:
            durations_sorted = sorted(durations)
            avg_d = sum(durations) / len(durations)
            med_d = durations_sorted[len(durations_sorted) // 2]
        else:
            avg_d = med_d = 0

        print(f"  {short}: {len(walks)} walks")
        print(f"    Duration: avg={avg_d:.1f}s, median={med_d:.1f}s")
        print(f"    Blips (<{MIN_WALK_DURATION}s): {blips}, over cap (>{MAX_WALK_DURATION}s): {over_cap}, stuck_motion events: {stuck}")
        if scores:
            avg_s = sum(scores) / len(scores)
            max_s = max(scores)
            print(f"    Score: avg={avg_s:.4f}, max={max_s:.4f}, outliers (>{MAX_MOVEMENT_SCORE}): {outlier_scores}")

    # Score distribution per node (last 24h)
    print("\n  --- Score distribution (last 24h) ---\n")
    for node in NODES:
        short = node.replace("csi_", "")
        rows = db.execute(
            "SELECT movement_score FROM samples WHERE node=? AND ts>? AND movement_score IS NOT NULL",
            (node, h24)
        ).fetchall()
        if not rows:
            continue
        scores = sorted(r[0] for r in rows)
        n = len(scores)
        p25 = scores[n // 4]
        p50 = scores[n // 2]
        p75 = scores[3 * n // 4]
        p95 = scores[int(n * 0.95)]
        p99 = scores[int(n * 0.99)]
        print(f"  {short} (n={n:,}): p25={p25:.4f} p50={p50:.4f} p75={p75:.4f} p95={p95:.4f} p99={p99:.4f}")

    print()
    db.close()


def query_export(node_filter=None, days=None, include_unlabeled=False):
    db = sqlite3.connect(DB_PATH)
    init_db(db)
    writer = csv.writer(sys.stdout)
    header = ["ts", "datetime", "node"] + [f"sc{i}" for i in range(NUM_SUBCARRIERS)] + ["movement_score", "motion", "label"]
    writer.writerow(header)

    conditions = []
    params = []

    if not include_unlabeled:
        conditions.append("label IS NOT NULL")

    if node_filter:
        # Allow short name (obyvak) or full name (csi_obyvak)
        if not node_filter.startswith("csi_"):
            node_filter = "csi_" + node_filter
        conditions.append("node = ?")
        params.append(node_filter)

    if days:
        cutoff = time.time() - days * 86400
        conditions.append("ts > ?")
        params.append(cutoff)

    where = " AND ".join(conditions) if conditions else "1=1"
    sql = (
        f"SELECT ts, node, sc0, sc1, sc2, sc3, sc4, sc5, sc6, sc7, sc8, sc9, sc10, sc11, "
        f"movement_score, motion, label FROM raw_subcarriers "
        f"WHERE {where} ORDER BY ts"
    )

    rows = db.execute(sql, params).fetchall()
    for row in rows:
        ts = row[0]
        dt_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        writer.writerow([row[0], dt_str] + list(row[1:]))

    print(f"# Exported {len(rows)} rows", file=sys.stderr)
    db.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="CSI Logger + Autolabel + Analysis")
    p.add_argument("--query", action="store_true", help="Show recent walks")
    p.add_argument("--stats", action="store_true", help="Summary statistics")
    p.add_argument("--tail", type=int, metavar="N", help="Last N samples")
    p.add_argument("--record", action="store_true", help="Manual labeling mode (e/w/s/f/q)")
    p.add_argument("--autolabel", action="store_true", help="Headless autolabel from radar (for systemd)")
    p.add_argument("--session", action="store_true", help="Session mode — labels from lab_session.py lockfile")
    p.add_argument("--raw-stats", action="store_true", help="Raw subcarrier statistics")
    p.add_argument("--export", action="store_true", help="Export labeled data to CSV")
    p.add_argument("--export-node", type=str, metavar="NODE", help="Filter export by node")
    p.add_argument("--export-days", type=int, metavar="N", help="Export only last N days")
    p.add_argument("--export-all", action="store_true", help="Export all data (including unlabeled)")
    p.add_argument("--db-info", action="store_true", help="DB size, row counts, date range")
    p.add_argument("--cleanup", type=int, metavar="DAYS", help="Delete unlabeled data older than N days")
    p.add_argument("--archive", action="store_true", help="Archive DB before cleanup")
    p.add_argument("--analyze", action="store_true", help="Data quality report")
    args = p.parse_args()

    if args.query:
        query_walks()
    elif args.stats:
        query_stats()
    elif args.tail:
        query_tail(args.tail)
    elif args.raw_stats:
        query_raw_stats()
    elif args.export or args.export_all:
        query_export(
            node_filter=args.export_node,
            days=args.export_days,
            include_unlabeled=args.export_all,
        )
    elif args.db_info:
        db_info()
    elif args.cleanup is not None:
        db_cleanup(args.cleanup, archive=args.archive)
    elif args.analyze:
        analyze()
    else:
        if sum([args.record, args.autolabel, args.session]) > 1:
            print("ERROR: --record, --autolabel and --session are mutually exclusive.", file=sys.stderr)
            sys.exit(1)
        CSILogger(record=args.record, autolabel=args.autolabel, session=args.session).run()
