#!/usr/bin/env python3
"""Live ML inference — reads DB every 2s and displays occupied/empty prediction."""
import os
import sqlite3
import time

import numpy as np
import joblib

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csi_log.db")
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
NODES = ["csi_obyvak_c5", "csi_obyvak_c5c", "csi_obyvak_c6"]
BUCKET_S = 2.0
WINDOW_S = 6.0  # use last 3 buckets for stability


def _safe(v):
    try:
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def extract_features(row):
    sc = np.array([_safe(row[f"sc{i}"]) for i in range(12)], dtype=np.float32)
    mean = float(np.mean(sc))
    std  = float(np.std(sc))
    mx   = float(np.max(sc))
    mn   = float(np.min(sc))
    rng  = mx - mn
    cv   = std / mean if mean > 1e-6 else 0.0
    skew = float(np.mean(((sc - mean) / std) ** 3)) if std > 1e-8 else 0.0
    kurt = float(np.mean(((sc - mean) / std) ** 4)) - 3.0 if std > 1e-8 else 0.0
    counts, _ = np.histogram(sc, bins=8)
    counts = counts.astype(float)
    total = counts.sum()
    if total > 0:
        probs = counts[counts > 0] / total
        entropy = float(-np.sum(probs * np.log2(probs + 1e-12)) / np.log2(8))
    else:
        entropy = 0.0
    return [mean, std, mx, mn, rng,
            _safe(row["movement_score"]), _safe(row["phase_turbulence"]),
            _safe(row["breathing_score"]), _safe(row["presence"]),
            _safe(row["dser"]), _safe(row["plcr"]),
            cv, skew, kurt, entropy]


def infer_now(db, model, scaler):
    now = time.time()
    since = now - WINDOW_S
    cur_bucket = int(now / BUCKET_S)

    sql = (
        "SELECT ts, sc0,sc1,sc2,sc3,sc4,sc5,sc6,sc7,sc8,sc9,sc10,sc11,"
        "movement_score, phase_turbulence, breathing_score, presence, dser, plcr "
        "FROM raw_subcarriers WHERE node=? AND ts>=? ORDER BY ts"
    )

    bmaps = {}
    node_counts = {}
    for node in NODES:
        rows = db.execute(sql, [node, since]).fetchall()
        node_counts[node] = len(rows)
        bm = {}
        for row in rows:
            b = int(row["ts"] / BUCKET_S)
            bm.setdefault(b, []).append(row)
        bmaps[node] = bm

    # Use most recent complete bucket
    all_buckets = set()
    for bm in bmaps.values():
        all_buckets.update(bm.keys())

    results = []
    for b in sorted(all_buckets):
        available = {n: bmaps[n][b][0] for n in NODES if b in bmaps[n]}
        if len(available) < 2:
            continue
        node_feats = {n: extract_features(available[n]) for n in available}
        mean_feat = [sum(nf[i] for nf in node_feats.values()) / len(node_feats) for i in range(15)]
        combined = []
        for n in NODES:
            combined.extend(node_feats.get(n, mean_feat))
        results.append(combined)

    if not results:
        return None, None, node_counts

    X = scaler.transform(results)
    probs = model.predict_proba(X)[:, 1]  # occupied probability
    avg_prob = float(np.mean(probs))
    label = "OCCUPIED" if avg_prob >= 0.5 else "empty"
    return label, avg_prob, node_counts


def main():
    model = joblib.load(os.path.join(MODELS_DIR, "multinode_mlp.pkl"))
    scaler = joblib.load(os.path.join(MODELS_DIR, "multinode_scaler.pkl"))
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    print(f"ML Live Inference — nodes: {NODES}")
    print(f"Window: {WINDOW_S}s, bucket: {BUCKET_S}s | Ctrl+C to exit\n")

    try:
        while True:
            label, prob, counts = infer_now(db, model, scaler)
            ts = time.strftime("%H:%M:%S")
            if label is None:
                status = "--- waiting for data ---"
            else:
                bar = "#" * int((prob or 0) * 20)
                pct = f"{(prob or 0)*100:.0f}%"
                status = f"{'[OCCUPIED]' if label == 'OCCUPIED' else '[empty]   ':12s}  {bar:<20s} {pct:>4s}"
            counts_str = "  ".join(f"{n.split('_')[-1]}:{v}" for n, v in counts.items())
            print(f"\r{ts}  {status}  | rows: {counts_str}    ", end="", flush=True)
            time.sleep(BUCKET_S)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
