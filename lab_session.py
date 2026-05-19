#!/usr/bin/env python3
"""
Lab Session Manager — structured CSI data collection for ML training.

Commands:
  python3 lab_session.py start --room living_room --activity walk [--note "text"] [--nodes node1 node2]
  python3 lab_session.py stop
  python3 lab_session.py status
  python3 lab_session.py list [--last 20]

Activities: presence, empty, walk, sit, fall, idle
Rooms: any string (e.g. living_room, bedroom, office)

Session state is shared with csi_logger.py via /tmp/csi_lab_session.json.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csi_log.db")
SESSION_LOCKFILE = "/tmp/csi_lab_session.json"

VALID_ACTIVITIES = {"presence", "empty", "walk", "sit", "fall", "idle"}
VALID_ROOMS = None  # any string accepted — room name becomes an ML label


def init_sessions_table(db: sqlite3.Connection):
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id        INTEGER PRIMARY KEY,
            room      TEXT NOT NULL,
            activity  TEXT NOT NULL,
            note      TEXT,
            nodes     TEXT,          -- JSON list, NULL = all
            start_ts  REAL NOT NULL,
            end_ts    REAL,
            rows_written INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(start_ts);
    """)
    # Migration: add session_id to raw_subcarriers if missing
    try:
        db.execute("ALTER TABLE raw_subcarriers ADD COLUMN session_id INTEGER REFERENCES sessions(id)")
        db.commit()
    except Exception:
        pass  # already exists
    db.commit()


def load_lockfile():
    if not os.path.exists(SESSION_LOCKFILE):
        return None
    try:
        with open(SESSION_LOCKFILE) as f:
            return json.load(f)
    except Exception:
        return None


def write_lockfile(data):
    with open(SESSION_LOCKFILE, "w") as f:
        json.dump(data, f, indent=2)


def remove_lockfile():
    if os.path.exists(SESSION_LOCKFILE):
        os.remove(SESSION_LOCKFILE)


def cmd_start(room, activity, note, nodes):
    if not room or not room.replace("_", "").replace("-", "").isalnum():
        print(f"ERROR: invalid room '{room}'. Use alphanumeric + underscores/hyphens (e.g. living_room)", file=sys.stderr)
        sys.exit(1)
    if activity not in VALID_ACTIVITIES:
        print(f"ERROR: invalid activity '{activity}'. Valid: {', '.join(sorted(VALID_ACTIVITIES))}", file=sys.stderr)
        sys.exit(1)

    existing = load_lockfile()
    if existing:
        print(f"WARNING: active session #{existing['session_id']} ({existing['room']}:{existing['activity']}) will be stopped.", file=sys.stderr)
        cmd_stop(silent=True)

    now = time.time()
    db = sqlite3.connect(DB_PATH, timeout=30)
    init_sessions_table(db)

    nodes_json = json.dumps(nodes) if nodes else None
    cur = db.execute(
        "INSERT INTO sessions (room, activity, note, nodes, start_ts) VALUES (?,?,?,?,?)",
        (room, activity, note, nodes_json, now),
    )
    session_id = cur.lastrowid
    db.commit()
    db.close()

    lockdata = {
        "session_id": session_id,
        "room": room,
        "activity": activity,
        "note": note or "",
        "nodes": nodes or [],
        "start_ts": now,
        "label": f"lab:{room}:{activity}",
    }
    write_lockfile(lockdata)

    ts_str = datetime.fromtimestamp(now).strftime("%H:%M:%S")
    print(f"  Session #{session_id} started [{ts_str}]")
    print(f"  Room     : {room}")
    print(f"  Activity : {activity}")
    if note:
        print(f"  Note     : {note}")
    if nodes:
        print(f"  Nodes    : {', '.join(nodes)}")
    print(f"  Label    : lab:{room}:{activity}")
    print()
    print(f"  Run in parallel: python3 csi_logger.py --session")
    print(f"  Stop            : python3 lab_session.py stop")


def cmd_stop(silent=False):
    lock = load_lockfile()
    if not lock:
        if not silent:
            print("Žádná aktivní session.")
        return

    session_id = lock["session_id"]
    now = time.time()
    duration = now - lock["start_ts"]
    rows = 0

    try:
        db = sqlite3.connect(DB_PATH, timeout=30)
        init_sessions_table(db)
        rows = db.execute(
            "SELECT COUNT(*) FROM raw_subcarriers WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        db.execute(
            "UPDATE sessions SET end_ts=?, rows_written=? WHERE id=?",
            (now, rows, session_id),
        )
        db.commit()
        db.close()
    except sqlite3.OperationalError as e:
        if not silent:
            print(f"  WARN: DB update failed ({e}), lockfile removed anyway.", file=sys.stderr)
    finally:
        remove_lockfile()

    if not silent:
        ts_str = datetime.fromtimestamp(now).strftime("%H:%M:%S")
        print(f"  Session #{session_id} stopped [{ts_str}]")
        print(f"  Duration : {duration:.0f}s ({duration/60:.1f} min)")
        print(f"  Written  : {rows} rows in raw_subcarriers")
        print(f"  Label    : {lock['label']}")


def cmd_status():
    lock = load_lockfile()
    if not lock:
        print("No active session.")

        # Show last session from DB
        db = sqlite3.connect(DB_PATH, timeout=30)
        try:
            init_sessions_table(db)
            row = db.execute(
                "SELECT id, room, activity, note, start_ts, end_ts, rows_written "
                "FROM sessions ORDER BY start_ts DESC LIMIT 1"
            ).fetchone()
            if row:
                sid, room, act, note, s_ts, e_ts, rows = row
                s_str = datetime.fromtimestamp(s_ts).strftime("%Y-%m-%d %H:%M:%S")
                e_str = datetime.fromtimestamp(e_ts).strftime("%H:%M:%S") if e_ts else "---"
                dur = (e_ts - s_ts) if e_ts else 0
                print(f"\n  Last session #{sid}: {room}:{act}  {s_str} → {e_str}  ({dur:.0f}s)  {rows} rows")
        finally:
            db.close()
        return

    now = time.time()
    duration = now - lock["start_ts"]
    ts_str = datetime.fromtimestamp(lock["start_ts"]).strftime("%H:%M:%S")
    print(f"  ACTIVE Session #{lock['session_id']}  [{ts_str}  +{duration:.0f}s]")
    print(f"  Room     : {lock['room']}")
    print(f"  Activity : {lock['activity']}")
    print(f"  Label    : {lock['label']}")
    if lock.get("note"):
        print(f"  Note     : {lock['note']}")

    # Live row count
    db = sqlite3.connect(DB_PATH, timeout=30)
    try:
        init_sessions_table(db)
        rows = db.execute(
            "SELECT COUNT(*) FROM raw_subcarriers WHERE session_id=?", (lock["session_id"],)
        ).fetchone()[0]
        print(f"  Written  : {rows} rows (so far)")
    finally:
        db.close()


def cmd_list(last):
    db = sqlite3.connect(DB_PATH, timeout=30)
    try:
        init_sessions_table(db)
        rows = db.execute(
            "SELECT id, room, activity, note, start_ts, end_ts, rows_written "
            "FROM sessions ORDER BY start_ts DESC LIMIT ?", (last,)
        ).fetchall()
    finally:
        db.close()

    if not rows:
        print("No sessions in DB.")
        return

    print(f"\n  {'#':>4s}  {'Room':>8s}  {'Activity':>10s}  {'Start':>17s}  {'Dur':>7s}  {'Rows':>7s}  Note")
    print("  " + "─" * 75)
    for sid, room, act, note, s_ts, e_ts, row_cnt in reversed(rows):
        s_str = datetime.fromtimestamp(s_ts).strftime("%m-%d %H:%M:%S")
        if e_ts:
            dur = e_ts - s_ts
            dur_str = f"{dur:.0f}s" if dur < 120 else f"{dur/60:.1f}m"
        else:
            dur_str = "active"
        rows_str = str(row_cnt) if row_cnt else "-"
        note_str = (note[:20] + "…") if note and len(note) > 20 else (note or "")
        print(f"  {sid:>4d}  {room:>8s}  {act:>10s}  {s_str:>17s}  {dur_str:>7s}  {rows_str:>7s}  {note_str}")
    print()


def main():
    p = argparse.ArgumentParser(description="Lab Session Manager")
    sub = p.add_subparsers(dest="cmd")

    s_start = sub.add_parser("start", help="Start a new session")
    s_start.add_argument("--room", required=True, choices=sorted(VALID_ROOMS))
    s_start.add_argument("--activity", required=True, choices=sorted(VALID_ACTIVITIES))
    s_start.add_argument("--note", default="", help="Optional note")
    s_start.add_argument("--nodes", nargs="*", help="Filter to specific nodes (default: all)")

    sub.add_parser("stop", help="Stop the active session")
    sub.add_parser("status", help="Show current session status")

    s_list = sub.add_parser("list", help="List session history")
    s_list.add_argument("--last", type=int, default=20, metavar="N")

    args = p.parse_args()

    if args.cmd == "start":
        cmd_start(args.room, args.activity, args.note, args.nodes)
    elif args.cmd == "stop":
        cmd_stop()
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "list":
        cmd_list(args.last)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
