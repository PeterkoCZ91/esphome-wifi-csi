#!/usr/bin/env python3
"""
Mesh lab MQTT logger.

This logger is intentionally separate from the production CSI logger. It records
only mesh_lab MQTT prefixes into mesh_lab/data/mesh_lab.db so AP/pairwise lab
data cannot leak into production training data by accident.
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

import paho.mqtt.client as mqtt


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(ROOT, "data", "mesh_lab.db")
DEFAULT_BROKER = "localhost"
DEFAULT_USER = os.environ.get("MQTT_USER", "")
DEFAULT_PASS = os.environ.get("MQTT_PASS", "")

NODES = {
    "mesh_lab_esp32_a": {
        "prefix": "esphome/mesh_lab/esp32_a",
        "mac": "AA:BB:CC:DD:EE:FE",
    },
    "mesh_lab_esp32_b": {
        "prefix": "esphome/mesh_lab/esp32_b",
        "mac": "AA:BB:CC:DD:EE:FF",
    },
    "mesh_lab_esp32_c": {
        "prefix": "esphome/mesh_lab/esp32_c",
        "mac": "AA:BB:CC:DD:EE:FD",
    },
}

NUM_SUBCARRIERS = 12
FLUSH_INTERVAL = 3.0
FLUSH_BATCH = 100


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def init_db(db):
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            node TEXT,
            topic TEXT NOT NULL,
            payload TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
        CREATE INDEX IF NOT EXISTS idx_messages_node_ts ON messages(node, ts);
        CREATE INDEX IF NOT EXISTS idx_messages_topic_ts ON messages(topic, ts);

        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            node TEXT NOT NULL,
            movement_score REAL,
            threshold REAL,
            motion INTEGER,
            presence INTEGER,
            phase_turbulence REAL,
            breathing_score REAL,
            dser REAL,
            plcr REAL
        );
        CREATE INDEX IF NOT EXISTS idx_samples_node_ts ON samples(node, ts);

        CREATE TABLE IF NOT EXISTS raw_subcarriers (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            node TEXT NOT NULL,
            sc0 REAL, sc1 REAL, sc2 REAL, sc3 REAL,
            sc4 REAL, sc5 REAL, sc6 REAL, sc7 REAL,
            sc8 REAL, sc9 REAL, sc10 REAL, sc11 REAL,
            movement_score REAL,
            motion INTEGER,
            presence INTEGER,
            phase_turbulence REAL,
            breathing_score REAL,
            dser REAL,
            plcr REAL,
            label TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_raw_node_ts ON raw_subcarriers(node, ts);
        CREATE INDEX IF NOT EXISTS idx_raw_label_ts ON raw_subcarriers(label, ts);

        CREATE TABLE IF NOT EXISTS diagnostics (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            node TEXT NOT NULL,
            key TEXT NOT NULL,
            value REAL,
            text_value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_diag_node_key_ts ON diagnostics(node, key, ts);

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            node TEXT NOT NULL,
            type TEXT NOT NULL,
            value TEXT,
            detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_node_ts ON events(node, ts);

        CREATE TABLE IF NOT EXISTS discoveries (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            node TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_discover_node_ts ON discoveries(node, ts);

        CREATE TABLE IF NOT EXISTS run_notes (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL
        );
    """)
    db.commit()


class MeshLabLogger:
    def __init__(self, db_path, broker, port, username, password, label=None, verbose=True):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        init_db(self.db)
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.label = label
        self.verbose = verbose
        self.pending = 0
        self.last_flush = time.time()
        self.prefix_to_node = {cfg["prefix"]: node for node, cfg in NODES.items()}
        self.state = {
            node: {
                "movement_score": None,
                "threshold": None,
                "motion": None,
                "presence": None,
                "phase_turbulence": None,
                "breathing_score": None,
                "dser": None,
                "plcr": None,
                "last_print": 0.0,
            }
            for node in NODES
        }

    def run(self):
        client = mqtt.Client()
        client.username_pw_set(self.username, self.password)
        client.on_connect = self._on_connect
        client.on_message = self._on_message

        print(f"Mesh lab logger DB: {self.db_path}")
        print(f"MQTT broker: {self.broker}:{self.port}")
        print("Nodes: " + ", ".join(NODES))
        if self.label:
            print(f"Manual label for raw rows: {self.label}")
        print()

        try:
            client.connect(self.broker, self.port)
            client.loop_forever()
        except KeyboardInterrupt:
            pass
        finally:
            print("\nStopping mesh lab logger...")
            self._flush()
            self.db.close()

    def _on_connect(self, client, _userdata, _flags, rc):
        print(f"MQTT connected rc={rc}")
        for node, cfg in NODES.items():
            client.subscribe(f"{cfg['prefix']}/#")
            client.subscribe(f"esphome/discover/{node}")
            print(f"  subscribed {cfg['prefix']}/#")
            print(f"  subscribed esphome/discover/{node}")
        print()

    def _on_message(self, _client, _userdata, msg):
        now = time.time()
        topic = msg.topic
        payload = msg.payload.decode(errors="replace").strip()
        node = self._node_from_topic(topic)

        self._write(
            "INSERT INTO messages (ts, node, topic, payload) VALUES (?,?,?,?)",
            (now, node, topic, payload),
        )

        if topic.startswith("esphome/discover/"):
            self._handle_discovery(now, topic, payload)
            return
        if node is None:
            return

        if topic.endswith("/sensor/movement_score/state"):
            self._handle_float_sample(now, node, "movement_score", payload)
        elif topic.endswith("/sensor/csi_active_threshold/state"):
            self._handle_float_sample(now, node, "threshold", payload)
        elif topic.endswith("/binary_sensor/motion_detected/state"):
            self._handle_bool_event(now, node, "motion", payload)
        elif topic.endswith("/binary_sensor/presence_detected/state"):
            self._handle_bool_event(now, node, "presence", payload)
        elif topic.endswith("/sensor/csi_phase_turbulence/state"):
            self._handle_float_sample(now, node, "phase_turbulence", payload)
        elif topic.endswith("/sensor/breathing_score/state"):
            self._handle_float_sample(now, node, "breathing_score", payload)
        elif topic.endswith("/sensor/dser/state"):
            self._handle_float_sample(now, node, "dser", payload)
        elif topic.endswith("/sensor/plcr/state"):
            self._handle_float_sample(now, node, "plcr", payload)
        elif topic.endswith("/sensor/csi_subcarrier_amplitudes/state"):
            self._handle_raw(now, node, payload)
        elif topic.endswith("/status"):
            self._handle_status(now, node, payload)
        elif "/sensor/" in topic or "/text_sensor/" in topic:
            self._handle_diagnostic(now, node, topic, payload)

    def _node_from_topic(self, topic):
        if topic.startswith("esphome/discover/"):
            node = topic.rsplit("/", 1)[-1]
            return node if node in NODES else None
        for prefix, node in self.prefix_to_node.items():
            if topic == prefix or topic.startswith(prefix + "/"):
                return node
        return None

    def _handle_discovery(self, now, topic, payload):
        node = topic.rsplit("/", 1)[-1]
        if node not in NODES:
            return
        try:
            parsed = json.loads(payload)
            payload_json = json.dumps(parsed, sort_keys=True)
            ip = parsed.get("ip", "unknown")
            mode = parsed.get("mode", "unknown")
            print(f"{fmt_ts(now)} {node} discovery ip={ip} mode={mode}")
        except json.JSONDecodeError:
            payload_json = payload
        self._write(
            "INSERT INTO discoveries (ts, node, payload_json) VALUES (?,?,?)",
            (now, node, payload_json),
        )

    def _handle_float_sample(self, now, node, key, payload):
        try:
            value = float(payload)
        except ValueError:
            return
        self.state[node][key] = value
        self._insert_sample(now, node)

        if key.startswith("csi_") or key in {"dser", "plcr"}:
            self._write(
                "INSERT INTO diagnostics (ts, node, key, value, text_value) VALUES (?,?,?,?,?)",
                (now, node, key, value, None),
            )

        if key == "movement_score" and self.verbose:
            self._maybe_print_score(now, node)

    def _handle_bool_event(self, now, node, key, payload):
        value = payload.lower() in {"on", "true", "1", "detected", "occupied"}
        old_value = self.state[node].get(key)
        self.state[node][key] = value
        self._insert_sample(now, node)

        if old_value is None or old_value != value:
            event_type = f"{key}_{'on' if value else 'off'}"
            self._write(
                "INSERT INTO events (ts, node, type, value, detail) VALUES (?,?,?,?,?)",
                (now, node, event_type, payload, None),
            )
            print(f"{fmt_ts(now)} {node} {event_type}")
            self._flush()

    def _handle_raw(self, now, node, payload):
        try:
            values = [float(part.strip()) for part in payload.split(",")]
        except ValueError:
            return
        values = (values + [None] * NUM_SUBCARRIERS)[:NUM_SUBCARRIERS]
        st = self.state[node]
        self._write(
            "INSERT INTO raw_subcarriers "
            "(ts, node, sc0, sc1, sc2, sc3, sc4, sc5, sc6, sc7, sc8, sc9, sc10, sc11, "
            "movement_score, motion, presence, phase_turbulence, breathing_score, dser, plcr, label) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now,
                node,
                *values,
                st["movement_score"],
                self._bool_to_db(st["motion"]),
                self._bool_to_db(st["presence"]),
                st["phase_turbulence"],
                st["breathing_score"],
                st["dser"],
                st["plcr"],
                self.label,
            ),
        )

    def _handle_status(self, now, node, payload):
        self._write(
            "INSERT INTO events (ts, node, type, value, detail) VALUES (?,?,?,?,?)",
            (now, node, "status", payload, None),
        )
        print(f"{fmt_ts(now)} {node} status={payload}")
        self._flush()

    def _handle_diagnostic(self, now, node, topic, payload):
        key = topic.split("/")[-2]
        try:
            value = float(payload)
            text_value = None
        except ValueError:
            value = None
            text_value = payload
        self._write(
            "INSERT INTO diagnostics (ts, node, key, value, text_value) VALUES (?,?,?,?,?)",
            (now, node, key, value, text_value),
        )

    def _insert_sample(self, now, node):
        st = self.state[node]
        self._write(
            "INSERT INTO samples "
            "(ts, node, movement_score, threshold, motion, presence, phase_turbulence, "
            "breathing_score, dser, plcr) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                now,
                node,
                st["movement_score"],
                st["threshold"],
                self._bool_to_db(st["motion"]),
                self._bool_to_db(st["presence"]),
                st["phase_turbulence"],
                st["breathing_score"],
                st["dser"],
                st["plcr"],
            ),
        )

    def _maybe_print_score(self, now, node):
        st = self.state[node]
        if now - st["last_print"] < 1.0:
            return
        st["last_print"] = now
        score = st["movement_score"]
        threshold = st["threshold"]
        motion = " motion" if st["motion"] else ""
        presence = " presence" if st["presence"] else ""
        thr_text = f"{threshold:.4f}" if threshold is not None else "-"
        print(f"{fmt_ts(now)} {node} score={score:.4f} threshold={thr_text}{motion}{presence}")

    def _write(self, sql, params):
        self.db.execute(sql, params)
        self.pending += 1
        now = time.time()
        if self.pending >= FLUSH_BATCH or now - self.last_flush >= FLUSH_INTERVAL:
            self._flush()

    def _flush(self):
        if self.pending:
            self.db.commit()
            self.pending = 0
            self.last_flush = time.time()

    @staticmethod
    def _bool_to_db(value):
        if value is None:
            return None
        return 1 if value else 0


def write_note(db_path, key, value):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    db = sqlite3.connect(db_path)
    init_db(db)
    db.execute(
        "INSERT INTO run_notes (ts, key, value) VALUES (?,?,?)",
        (time.time(), key, value),
    )
    db.commit()
    db.close()


def print_stats(db_path):
    db = sqlite3.connect(db_path)
    init_db(db)
    print(f"DB: {db_path}")
    for table in ("messages", "samples", "raw_subcarriers", "diagnostics", "events", "discoveries", "run_notes"):
        count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table:16s} {count}")
    print()
    rows = db.execute("""
        SELECT node, COUNT(*), MIN(ts), MAX(ts)
        FROM raw_subcarriers
        GROUP BY node
        ORDER BY node
    """).fetchall()
    if rows:
        print("Raw subcarriers:")
        for node, count, first_ts, last_ts in rows:
            print(f"  {node:16s} {count:8d}  {fmt_ts(first_ts)} -> {fmt_ts(last_ts)}")
    else:
        print("Raw subcarriers: none yet")
    print()
    rows = db.execute("""
        SELECT node, key, value, text_value, MAX(ts)
        FROM diagnostics
        GROUP BY node, key
        ORDER BY node, key
    """).fetchall()
    if rows:
        print("Latest diagnostics:")
        for node, key, value, text_value, ts in rows:
            shown = text_value if text_value is not None else value
            print(f"  {node:16s} {key:28s} {shown}  @ {fmt_ts(ts)}")
    db.close()


def print_tail(db_path, limit):
    db = sqlite3.connect(db_path)
    init_db(db)
    rows = db.execute("""
        SELECT ts, node, topic, payload
        FROM messages
        ORDER BY ts DESC
        LIMIT ?
    """, (limit,)).fetchall()
    for ts, node, topic, payload in reversed(rows):
        node_text = node or "-"
        print(f"{fmt_ts(ts)} {node_text:16s} {topic} {payload}")
    db.close()


def export_raw(db_path, output_path):
    db = sqlite3.connect(db_path)
    init_db(db)
    rows = db.execute("""
        SELECT ts, node, sc0, sc1, sc2, sc3, sc4, sc5, sc6, sc7, sc8, sc9, sc10, sc11,
               movement_score, motion, presence, phase_turbulence, breathing_score, dser, plcr, label
        FROM raw_subcarriers
        ORDER BY ts
    """)
    out = open(output_path, "w", newline="") if output_path else sys.stdout
    try:
        writer = csv.writer(out)
        writer.writerow([
            "ts", "datetime", "node",
            "sc0", "sc1", "sc2", "sc3", "sc4", "sc5", "sc6", "sc7", "sc8", "sc9", "sc10", "sc11",
            "movement_score", "motion", "presence", "phase_turbulence", "breathing_score", "dser", "plcr", "label",
        ])
        for row in rows:
            writer.writerow([row[0], fmt_ts(row[0]), *row[1:]])
    finally:
        if output_path:
            out.close()
    db.close()


def main():
    parser = argparse.ArgumentParser(description="Isolated MQTT/SQLite logger for mesh_lab ESP CSI data")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite DB path, default: {DEFAULT_DB_PATH}")
    parser.add_argument("--broker", default=DEFAULT_BROKER)
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--username", default=DEFAULT_USER)
    parser.add_argument("--password", default=DEFAULT_PASS)
    parser.add_argument("--label", help="Optional manual label stored on new raw_subcarriers rows")
    parser.add_argument("--quiet", action="store_true", help="Reduce live movement-score output")
    parser.add_argument("--init-db", action="store_true", help="Create/update DB schema and exit")
    parser.add_argument("--stats", action="store_true", help="Print DB counters and latest diagnostics")
    parser.add_argument("--tail", type=int, metavar="N", help="Show last N MQTT messages from DB")
    parser.add_argument("--note", nargs=2, metavar=("KEY", "VALUE"), help="Store a run note in DB")
    parser.add_argument("--export-raw", metavar="CSV", help="Export raw_subcarriers to CSV, use - for stdout")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)

    if args.init_db:
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        db = sqlite3.connect(db_path)
        init_db(db)
        db.close()
        print(f"Initialized {db_path}")
        return
    if args.stats:
        print_stats(db_path)
        return
    if args.tail is not None:
        print_tail(db_path, args.tail)
        return
    if args.note:
        write_note(db_path, args.note[0], args.note[1])
        print(f"Stored note {args.note[0]} in {db_path}")
        return
    if args.export_raw:
        output = None if args.export_raw == "-" else args.export_raw
        export_raw(db_path, output)
        return

    MeshLabLogger(
        db_path=db_path,
        broker=args.broker,
        port=args.port,
        username=args.username,
        password=args.password,
        label=args.label,
        verbose=not args.quiet,
    ).run()


if __name__ == "__main__":
    main()
