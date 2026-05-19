#!/usr/bin/env python3
"""
Multi-node ML trainer — concatenates feature vectors from C5b + C5c nodes and trains a presence MLP.

Outputs:
  models/multinode_mlp.pkl    — sklearn MLP model
  models/multinode_scaler.pkl — StandardScaler

Usage:
  python3 train_multinode_ml.py [--db csi_log.db] [--bucket 2.0] [--min-pairs 50]
  python3 train_multinode_ml.py --nodes csi_obyvak_c5b csi_obyvak_c5d  # only 2 nodes
  python3 train_multinode_ml.py --analyze      # statistics only, no training
  python3 train_multinode_ml.py --session 3    # only data from session #3
"""

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime

import numpy as np

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csi_log.db")
MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

DEFAULT_NODES = ["csi_obyvak_c5b", "csi_obyvak_c5c", "csi_obyvak_c5d"]

OCCUPIED_LABELS = frozenset({
    "occupied", "walk", "sit",
    "lab:obyvak:presence", "lab:kuchyn:presence", "lab:chodba:presence",
    "lab:loznice:presence", "lab:pokoj:presence", "lab:all:presence",
    "lab:obyvak:walk", "lab:kuchyn:walk", "lab:chodba:walk",
    "lab:loznice:walk", "lab:pokoj:walk", "lab:all:walk",
    "lab:obyvak:sit", "lab:kuchyn:sit", "lab:chodba:sit",
    "lab:loznice:sit", "lab:pokoj:sit", "lab:all:sit",
})
EMPTY_LABELS = frozenset({
    "empty", "idle",
    "lab:obyvak:empty", "lab:kuchyn:empty", "lab:chodba:empty",
    "lab:loznice:empty", "lab:pokoj:empty", "lab:all:empty", "lab:all:idle",
})


def _safe(v, default=0.0):
    return float(v) if v is not None else default


def extract_features(row):
    """Extract 15 features from a raw_subcarriers row dict."""
    sc = np.array([_safe(row[f"sc{i}"]) for i in range(12)], dtype=np.float32)
    mean = float(np.mean(sc))
    std  = float(np.std(sc))
    mx   = float(np.max(sc))
    mn   = float(np.min(sc))
    rng  = mx - mn
    cv   = std / mean if mean > 1e-6 else 0.0

    # Skewness (Fisher's)
    if std > 1e-8:
        skew = float(np.mean(((sc - mean) / std) ** 3))
    else:
        skew = 0.0

    # Kurtosis (Fisher's excess)
    if std > 1e-8:
        kurt = float(np.mean(((sc - mean) / std) ** 4)) - 3.0
    else:
        kurt = 0.0

    # Shannon entropy (8 bins)
    counts, _ = np.histogram(sc, bins=8)
    counts = counts.astype(float)
    total = counts.sum()
    if total > 0:
        probs = counts[counts > 0] / total
        entropy = float(-np.sum(probs * np.log2(probs + 1e-12)) / np.log2(8))
    else:
        entropy = 0.0

    return [
        mean,                              #  0 sc_mean
        std,                               #  1 sc_std
        mx,                                #  2 sc_max
        mn,                                #  3 sc_min
        rng,                               #  4 sc_range
        _safe(row["movement_score"]),      #  5 movement_score
        _safe(row["phase_turbulence"]),    #  6 phase_turbulence
        _safe(row["breathing_score"]),     #  7 breathing_score
        _safe(row["presence"]),            #  8 presence
        _safe(row["dser"]),                #  9 dser
        _safe(row["plcr"]),                # 10 plcr
        cv,                                # 11 sc_cv
        skew,                              # 12 sc_skewness
        kurt,                              # 13 sc_kurtosis
        entropy,                           # 14 sc_entropy
    ]


def load_data(db_path, bucket_s, session_id=None, nodes=None):
    """Load and align data from N nodes using time bucketing.
    
    Requires at least 2 nodes to have data in a bucket.
    Missing nodes are imputed with the mean of available nodes.
    """
    if nodes is None:
        nodes = DEFAULT_NODES
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    if isinstance(session_id, list):
        placeholders = ",".join("?" * len(session_id))
        where_session = f"AND session_id IN ({placeholders})"
    elif session_id:
        where_session = "AND session_id = ?"
        session_id = [session_id]
    else:
        where_session = ""
        session_id = []
    sql = (
        "SELECT ts, sc0, sc1, sc2, sc3, sc4, sc5, sc6, sc7, sc8, sc9, sc10, sc11, "
        "movement_score, phase_turbulence, breathing_score, presence, dser, plcr, label "
        f"FROM raw_subcarriers WHERE node=? AND label IS NOT NULL {where_session} "
        "ORDER BY ts"
    )

    def bucket_map(rows):
        buckets = {}
        for row in rows:
            b = int(row["ts"] / bucket_s)
            if b not in buckets:
                buckets[b] = []
            buckets[b].append(row)
        return buckets

    bmaps = {}
    for node in nodes:
        params = [node] + session_id
        rows = db.execute(sql, params).fetchall()
        bmaps[node] = bucket_map(rows)
        print(f"  Raw rows: {node}={len(rows)}")
    db.close()

    # Buckets where at least 2 nodes have data
    all_buckets = set()
    for bm in bmaps.values():
        all_buckets.update(bm.keys())
    
    X, y, labels_list = [], [], []
    for b in sorted(all_buckets):
        available = {n: bmaps[n][b][0] for n in nodes if b in bmaps[n]}
        if len(available) < min(2, len(nodes)):
            continue

        # Determine label from first available node
        label = ""
        for n in nodes:
            if n in available:
                label = available[n]["label"] or ""
                if label in OCCUPIED_LABELS | EMPTY_LABELS:
                    break

        if label in OCCUPIED_LABELS:
            y_val = 1
        elif label in EMPTY_LABELS:
            y_val = 0
        else:
            continue

        # Extract features per node; impute missing nodes with mean of available
        node_feats = {n: extract_features(available[n]) for n in available}
        mean_feat = [sum(node_feats[n][i] for n in node_feats) / len(node_feats)
                     for i in range(15)]
        
        combined = []
        for n in nodes:
            combined.extend(node_feats.get(n, mean_feat))
        X.append(combined)
        y.append(y_val)
        labels_list.append(label)

    n_nodes = len(nodes)
    print(f"  Aligned buckets (≥2 nodes present): {len(X)}  ({n_nodes*15}-feature vector)")
    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int32), labels_list


def analyze(X, y, labels_list):
    """Print dataset statistics."""
    n_occ = int(np.sum(y == 1))
    n_emp = int(np.sum(y == 0))
    print(f"\n  Dataset: {len(y)} samples  (occupied={n_occ}, empty={n_emp})")

    from collections import Counter
    label_counts = Counter(labels_list)
    print("\n  Label distribution:")
    for lbl, cnt in sorted(label_counts.items(), key=lambda x: -x[1]):
        print(f"    {lbl:<35s}: {cnt:>6d}")

    n_feats = X.shape[1] if len(X) else 0
    n_nodes = n_feats // 15
    feat_names_base = ["sc_mean","sc_std","sc_max","sc_min","sc_range",
                       "movement","phase_turb","breathing","presence","dser","plcr",
                       "cv","skewness","kurtosis","entropy"]
    prefixes = [chr(ord('a')+i) for i in range(n_nodes)]
    feature_names = [f"{p}_{name}" for p in prefixes for name in feat_names_base]
    print(f"\n  Features ({n_feats} = 15\u00d7{n_nodes} nodes):")
    print(f"  {'#':>3s}  {'Feature':<18s}  {'Mean':>8s}  {'Std':>8s}  {'Min':>8s}  {'Max':>8s}")
    print("  " + "─" * 60)
    for i, name in enumerate(feature_names):
        col = X[:, i]
        print(f"  {i:>3d}  {name:<18s}  {col.mean():>8.4f}  {col.std():>8.4f}  {col.min():>8.4f}  {col.max():>8.4f}")


def train(X, y):
    """Train StandardScaler + MLP, return (model, scaler, metrics)."""
    try:
        from sklearn.neural_network import MLPClassifier
        from sklearn.preprocessing import StandardScaler
        from sklearn.model_selection import StratifiedKFold, cross_val_score
        from sklearn.metrics import classification_report, confusion_matrix
    except ImportError:
        print("ERROR: scikit-learn is not installed. Run: pip install scikit-learn", file=sys.stderr)
        sys.exit(1)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    model = MLPClassifier(
        hidden_layer_sizes=(32, 16),
        activation="relu",
        max_iter=500,
        random_state=42,
        early_stopping=True,
        validation_fraction=0.1,
        n_iter_no_change=20,
        verbose=False,
    )

    # Cross-validation (stratified 5-fold)
    print("\n  Cross-validation (5-fold stratified)...")
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    scores = cross_val_score(model, X_scaled, y, cv=cv, scoring="f1_weighted")
    print(f"  F1 (weighted): {scores.mean():.4f} ± {scores.std():.4f}")
    print(f"  Per-fold: {[f'{s:.3f}' for s in scores]}")

    # Final fit on all data
    print("\n  Training on all data...")
    model.fit(X_scaled, y)

    y_pred = model.predict(X_scaled)
    print("\n  Classification report (train set):")
    print(classification_report(y, y_pred, target_names=["empty", "occupied"]))

    cm = confusion_matrix(y, y_pred)
    print(f"  Confusion matrix (train):")
    print(f"    TN={cm[0,0]:4d}  FP={cm[0,1]:4d}")
    print(f"    FN={cm[1,0]:4d}  TP={cm[1,1]:4d}")

    return model, scaler, {"cv_f1_mean": float(scores.mean()), "cv_f1_std": float(scores.std())}


def save_models(model, scaler, metrics):
    """Save model and scaler to models/ directory."""
    try:
        import joblib
    except ImportError:
        print("ERROR: joblib is not installed (part of scikit-learn).", file=sys.stderr)
        sys.exit(1)

    os.makedirs(MODELS_DIR, exist_ok=True)
    model_path = os.path.join(MODELS_DIR, "multinode_mlp.pkl")
    scaler_path = os.path.join(MODELS_DIR, "multinode_scaler.pkl")
    joblib.dump(model, model_path)
    joblib.dump(scaler, scaler_path)

    print(f"\n  Model saved: {model_path}")
    print(f"  Scaler saved: {scaler_path}")
    print(f"  CV F1: {metrics['cv_f1_mean']:.4f} ± {metrics['cv_f1_std']:.4f}")


def main():
    p = argparse.ArgumentParser(description="Multi-node ML trainer (N C5 RX nodes)")
    p.add_argument("--db", default=DB_PATH, metavar="PATH", help="Path to csi_log.db")
    p.add_argument("--bucket", type=float, default=2.0, metavar="S",
                   help="Feature bucket width in seconds (default: 2.0)")
    p.add_argument("--min-pairs", type=int, default=50,
                   help="Minimum number of aligned bucket pairs for training (default: 50)")
    p.add_argument("--analyze", action="store_true", help="Print dataset statistics without training")
    p.add_argument("--session", type=int, nargs="+", metavar="ID", help="Restrict to session_id(s) from DB")
    p.add_argument("--nodes", nargs="+", default=DEFAULT_NODES, metavar="NODE",
                   help="MQTT node names to include (default: %(default)s)")
    args = p.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    print(f"\n=== Multi-node ML Trainer ({len(args.nodes)} nodes) ===")
    print(f"  Nodes   : {args.nodes}")
    print(f"  DB      : {args.db}")
    print(f"  Bucket  : {args.bucket}s")
    if args.session:
        print(f"  Session : {args.session}")
    print()

    print("  Loading data from DB...")
    X, y, labels_list = load_data(args.db, args.bucket, session_id=args.session, nodes=args.nodes)

    if len(y) == 0:
        print("\n  ERROR: no aligned data found. Check labels in DB:")
        print("  sqlite3 csi_log.db \"SELECT label, COUNT(*) FROM raw_subcarriers WHERE node IN ('csi_obyvak_c5b','csi_obyvak_c5c') AND label IS NOT NULL GROUP BY label;\"")
        sys.exit(1)

    analyze(X, y, labels_list)

    if args.analyze:
        print("\n  (--analyze: training skipped)")
        return

    n_occ = int(np.sum(y == 1))
    n_emp = int(np.sum(y == 0))
    if min(n_occ, n_emp) < args.min_pairs:
        print(f"\n  ERROR: insufficient data. Need at least {args.min_pairs} samples per class.")
        print(f"  Current: occupied={n_occ}, empty={n_emp}")
        print(f"  Collect labels using: python lab_session.py start --room X --activity Y")
        print(f"  (To only analyze the dataset: add --analyze)")
        sys.exit(1)

    model, scaler, metrics = train(X, y)
    save_models(model, scaler, metrics)

    print("\nDone.")


if __name__ == "__main__":
    main()
