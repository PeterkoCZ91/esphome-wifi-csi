#!/usr/bin/env python3
"""
Train MLP model on raw CSI subcarrier data from csi_log.db.

Extracts 17 statistical features per sliding window (matching ml_features.h),
trains a 17→18→9→1 MLP with ReLU/Sigmoid, and exports weights to ml_weights.h.

Features must match C++ extract_ml_features() exactly:
 0. turb_mean        - Mean of turbulence buffer
 1. turb_std         - Standard deviation
 2. turb_max         - Maximum value
 3. turb_min         - Minimum value
 4. turb_zcr         - Zero-crossing rate around mean
 5. turb_skewness    - Fisher's skewness (3rd moment)
 6. turb_kurtosis    - Fisher's kurtosis (4th moment, excess)
 7. turb_entropy     - Shannon entropy (10 bins)
 8. turb_autocorr    - Lag-1 autocorrelation
 9. turb_mad         - Median absolute deviation
10. turb_slope       - Linear regression slope
11. waveform_length  - Sum of absolute first differences
12. phase_turbulence - Std of inter-subcarrier phase diffs
13. ratio_turbulence - SA-WiSense adjacent amplitude ratio std
14. breathing_score  - Bandpass-filtered amplitude energy (0.08-0.6 Hz)
15. dser             - Uni-Fi Dynamic-to-Static Energy Ratio (log, eq. 3)
16. plcr             - Uni-Fi Path-Length Change Rate proxy (eq. 4)

Usage:
    python3 train_ml_model.py [--db csi_log.db] [--window 75] [--stride 37]
"""

import argparse
import sqlite3
import numpy as np
import os
import sys
def median_abs_deviation(x, scale=1.0):
    """MAD without scipy dependency."""
    med = np.median(x)
    return np.median(np.abs(x - med)) * scale


# Breathing bandpass filter coefficients (must match csi_filters.cpp)
BREATH_HP_B0 = 0.99749
BREATH_HP_A1 = -0.99498
BREATH_LP_B0 = 0.01850
BREATH_LP_A1 = -0.96300
BREATH_ENERGY_ALPHA = 0.00333


def breathing_filter_series(amplitude_sums):
    """Apply breathing bandpass (HP 0.08Hz + LP 0.6Hz) and return energy series.

    Matches C++ breathing_filter_apply() exactly.
    """
    n = len(amplitude_sums)
    energy = np.zeros(n, dtype=np.float32)

    hp_x_prev = amplitude_sums[0]
    hp_y_prev = 0.0
    lp_x_prev = 0.0
    lp_y_prev = 0.0
    e = 0.0

    for i in range(n):
        x = amplitude_sums[i]
        if i == 0:
            hp_x_prev = x
            hp_y_prev = 0.0
            lp_x_prev = 0.0
            lp_y_prev = 0.0
            e = 0.0
            energy[i] = 0.0
            continue

        # High-pass
        hp_out = BREATH_HP_B0 * (x - hp_x_prev) - BREATH_HP_A1 * hp_y_prev
        hp_x_prev = x
        hp_y_prev = hp_out

        # Low-pass
        lp_out = BREATH_LP_B0 * (hp_out + lp_x_prev) - BREATH_LP_A1 * lp_y_prev
        lp_x_prev = hp_out
        lp_y_prev = lp_out

        # Energy (EMA of squared output)
        sq = lp_out * lp_out
        e = BREATH_ENERGY_ALPHA * sq + (1.0 - BREATH_ENERGY_ALPHA) * e
        energy[i] = np.sqrt(e)

    return energy


def extract_features(turb_window, phase_turb, ratio_turb, breathing_score, dser, plcr):
    """Extract 17 features matching C++ extract_ml_features()."""
    n = len(turb_window)
    if n < 2:
        return np.zeros(17, dtype=np.float32)

    turb = np.array(turb_window, dtype=np.float32)
    turb_mean = turb.mean()
    turb_std = turb.std()
    turb_var = turb.var()
    turb_max = turb.max()
    turb_min = turb.min()

    # Zero-crossing rate around mean
    above = turb >= turb_mean
    crossings = np.sum(above[1:] != above[:-1])
    turb_zcr = crossings / (n - 1) if n > 1 else 0.0

    # Fisher's skewness
    if turb_std > 1e-10:
        turb_skewness = np.mean(((turb - turb_mean) / turb_std) ** 3)
    else:
        turb_skewness = 0.0

    # Fisher's kurtosis (excess)
    if turb_std > 1e-10:
        turb_kurtosis = np.mean(((turb - turb_mean) / turb_std) ** 4) - 3.0
    else:
        turb_kurtosis = 0.0

    # Shannon entropy (10 bins)
    turb_range = turb_max - turb_min
    if turb_range > 1e-10:
        bins = np.histogram(turb, bins=10, range=(turb_min, turb_max))[0]
        probs = bins[bins > 0] / n
        turb_entropy = -np.sum(probs * np.log2(probs))
    else:
        turb_entropy = 0.0

    # Lag-1 autocorrelation
    if turb_var > 1e-10 and n > 2:
        autocovariance = np.mean((turb[:-1] - turb_mean) * (turb[1:] - turb_mean))
        turb_autocorr = autocovariance / turb_var
    else:
        turb_autocorr = 0.0

    # Median absolute deviation
    turb_mad = float(median_abs_deviation(turb, scale=1.0))

    # Linear regression slope
    x_idx = np.arange(n, dtype=np.float32)
    mean_i = (n - 1) / 2.0
    diff_i = x_idx - mean_i
    diff_x = turb - turb_mean
    denom = np.sum(diff_i ** 2)
    turb_slope = float(np.sum(diff_i * diff_x) / denom) if denom > 0 else 0.0

    # Waveform length (sum of absolute first differences)
    waveform_length = float(np.sum(np.abs(np.diff(turb))))

    return np.array([
        turb_mean,          # 0
        turb_std,           # 1
        turb_max,           # 2
        turb_min,           # 3
        turb_zcr,           # 4
        turb_skewness,      # 5
        turb_kurtosis,      # 6
        turb_entropy,       # 7
        turb_autocorr,      # 8
        turb_mad,           # 9
        turb_slope,         # 10
        waveform_length,    # 11
        phase_turb,         # 12
        ratio_turb,         # 13
        breathing_score,    # 14
        dser,               # 15
        plcr,               # 16
    ], dtype=np.float32)


def load_data(db_path, nodes, window_size=75, stride=37, max_windows_per_node=50000,
              min_distinct_dser=20):
    """Load raw subcarrier data and create sliding windows with labels.

    Data hygiene:
    - Requires dser IS NOT NULL (drops old-FW rows without Uni-Fi features).
    - Skips nodes whose DSER has fewer than `min_distinct_dser` distinct values
      across the filtered set (catches frozen-publish bugs like csi_chodba in 2026-04).
    """
    conn = sqlite3.connect(db_path)

    all_features = []
    all_labels = []

    for node in nodes:
        print(f"Loading {node}...")

        distinct_dser = conn.execute(
            "SELECT COUNT(DISTINCT dser) FROM raw_subcarriers "
            "WHERE node=? AND dser IS NOT NULL",
            (node,),
        ).fetchone()[0]
        if distinct_dser < min_distinct_dser:
            print(f"  SKIP: only {distinct_dser} distinct DSER values "
                  f"(min {min_distinct_dser}) — node is frozen or missing Uni-Fi features")
            continue

        has_gt = conn.execute(
            "SELECT COUNT(*) FROM raw_subcarriers WHERE node=? AND label IS NOT NULL "
            "AND dser IS NOT NULL",
            (node,)
        ).fetchone()[0]

        if has_gt > 0:
            print(f"  Using ground-truth labels ({has_gt} labeled samples)")
            query = (
                "SELECT sc0,sc1,sc2,sc3,sc4,sc5,sc6,sc7,sc8,sc9,sc10,sc11,"
                "movement_score, CASE WHEN label='occupied' THEN 1 ELSE 0 END, "
                "phase_turbulence, dser, plcr "
                "FROM raw_subcarriers WHERE node=? AND label IS NOT NULL "
                "AND dser IS NOT NULL ORDER BY ts"
            )
        else:
            print(f"  Using MVS motion flag (no ground-truth labels)")
            query = (
                "SELECT sc0,sc1,sc2,sc3,sc4,sc5,sc6,sc7,sc8,sc9,sc10,sc11,"
                "movement_score, motion, phase_turbulence, dser, plcr "
                "FROM raw_subcarriers WHERE node=? AND dser IS NOT NULL ORDER BY ts"
            )

        rows = conn.execute(query, (node,)).fetchall()
        print(f"  {len(rows)} samples")
        if len(rows) < window_size:
            continue

        # Pre-compute per-packet signals for entire node
        amplitudes_all = []
        turb_all = []
        phase_turb_all = []
        ratio_turb_all = []
        amp_sum_all = []
        dser_all = []
        plcr_all = []

        for row in rows:
            amps = np.array([row[i] for i in range(12)], dtype=np.float32)
            amplitudes_all.append(amps)
            turb_all.append(float(np.std(amps)))  # spatial turbulence = std of amplitudes

            # Phase turbulence from DB (or 0)
            pt = row[14] if row[14] is not None else 0.0
            phase_turb_all.append(float(pt))

            # SA-WiSense ratio turbulence: std of adjacent amplitude ratios
            ratios = []
            for j in range(len(amps) - 1):
                if amps[j + 1] > 0.1:
                    ratios.append(amps[j] / amps[j + 1])
            ratio_turb_all.append(float(np.std(ratios)) if len(ratios) > 1 else 0.0)

            # Amplitude sum for breathing filter
            amp_sum_all.append(float(np.sum(amps)))

            # Uni-Fi DSER/PLCR (pulled directly from DB — computed on ESP)
            dser_all.append(float(row[15]) if row[15] is not None else 0.0)
            plcr_all.append(float(row[16]) if row[16] is not None else 0.0)

        turb_all = np.array(turb_all, dtype=np.float32)
        phase_turb_all = np.array(phase_turb_all, dtype=np.float32)
        ratio_turb_all = np.array(ratio_turb_all, dtype=np.float32)
        dser_all = np.array(dser_all, dtype=np.float32)
        plcr_all = np.array(plcr_all, dtype=np.float32)

        # Run breathing filter over entire timeseries
        breathing_all = breathing_filter_series(np.array(amp_sum_all, dtype=np.float32))

        # Create sliding windows
        count = 0
        for start in range(0, len(rows) - window_size, stride):
            if count >= max_windows_per_node:
                break

            end = start + window_size
            turb_window = turb_all[start:end]

            # Use last packet's multi-domain signals
            last_phase = phase_turb_all[end - 1]
            last_ratio = ratio_turb_all[end - 1]
            last_breath = breathing_all[end - 1]
            last_dser = dser_all[end - 1]
            last_plcr = plcr_all[end - 1]

            features = extract_features(turb_window, last_phase, last_ratio, last_breath,
                                        last_dser, last_plcr)
            all_features.append(features)

            # Label: majority vote in window
            motion_votes = sum(1 for r in rows[start:end] if r[13] == 1)
            label = 1.0 if motion_votes > window_size / 2 else 0.0
            all_labels.append(label)
            count += 1

        print(f"  {count} windows (occupied: {sum(1 for l in all_labels[-count:] if l > 0.5)})")

    conn.close()
    return np.array(all_features, dtype=np.float32), np.array(all_labels, dtype=np.float32)


def train_mlp(X, y, hidden1=18, hidden2=9, epochs=100, lr=0.01, batch_size=256):
    """Train MLP with SGD. Architecture: N → hidden1 → hidden2 → 1."""
    n_features = X.shape[1]
    np.random.seed(42)

    # Xavier initialization
    W1 = np.random.randn(n_features, hidden1).astype(np.float32) * np.sqrt(2.0 / n_features)
    b1 = np.zeros(hidden1, dtype=np.float32)
    W2 = np.random.randn(hidden1, hidden2).astype(np.float32) * np.sqrt(2.0 / hidden1)
    b2 = np.zeros(hidden2, dtype=np.float32)
    W3 = np.random.randn(hidden2, 1).astype(np.float32) * np.sqrt(2.0 / hidden2)
    b3 = np.zeros(1, dtype=np.float32)

    def relu(x):
        return np.maximum(0, x)

    def sigmoid(x):
        return 1.0 / (1.0 + np.exp(-np.clip(x, -20, 20)))

    n_samples = len(X)
    indices = np.arange(n_samples)

    for epoch in range(epochs):
        np.random.shuffle(indices)
        total_loss = 0.0

        for batch_start in range(0, n_samples, batch_size):
            batch_idx = indices[batch_start:batch_start + batch_size]
            Xb = X[batch_idx]
            yb = y[batch_idx].reshape(-1, 1)
            bs = len(batch_idx)

            # Forward
            z1 = Xb @ W1 + b1
            a1 = relu(z1)
            z2 = a1 @ W2 + b2
            a2 = relu(z2)
            z3 = a2 @ W3 + b3
            a3 = sigmoid(z3)

            # Binary cross-entropy loss
            eps = 1e-7
            loss = -np.mean(yb * np.log(a3 + eps) + (1 - yb) * np.log(1 - a3 + eps))
            total_loss += loss * bs

            # Backward
            dz3 = (a3 - yb) / bs
            dW3 = a2.T @ dz3
            db3 = dz3.sum(axis=0)

            da2 = dz3 @ W3.T
            dz2 = da2 * (z2 > 0).astype(np.float32)
            dW2 = a1.T @ dz2
            db2 = dz2.sum(axis=0)

            da1 = dz2 @ W2.T
            dz1 = da1 * (z1 > 0).astype(np.float32)
            dW1 = Xb.T @ dz1
            db1 = dz1.sum(axis=0)

            # SGD update
            W1 -= lr * dW1
            b1 -= lr * db1
            W2 -= lr * dW2
            b2 -= lr * db2
            W3 -= lr * dW3
            b3 -= lr * db3

        avg_loss = total_loss / n_samples
        if (epoch + 1) % 10 == 0 or epoch == 0:
            z1 = X @ W1 + b1; a1 = relu(z1)
            z2 = a1 @ W2 + b2; a2 = relu(z2)
            z3 = a2 @ W3 + b3; preds = sigmoid(z3).flatten()
            acc = np.mean((preds > 0.5) == (y > 0.5))
            print(f"  Epoch {epoch+1:3d}/{epochs}: loss={avg_loss:.4f} acc={acc:.4f}")

    return W1, b1, W2, b2, W3, b3


def export_weights(W1, b1, W2, b2, W3, b3, feature_mean, feature_scale, output_path):
    """Export trained weights to ml_weights.h format."""

    def fmt_array(arr, decimals=6):
        return ", ".join(f"{v:.{decimals}f}f" for v in arr.flatten())

    def fmt_matrix(mat, decimals=6):
        rows = []
        for row in mat:
            vals = ", ".join(f"{v:.{decimals}f}f" for v in row)
            rows.append(f"    {{{vals}}}")
        return ",\n".join(rows)

    n_features = W1.shape[0]
    h1 = W1.shape[1]
    h2 = W2.shape[1]

    content = f"""/*
 * ESPectre - ML Model Weights
 *
 * Auto-generated neural network weights for motion detection.
 * Architecture: {n_features} -> {h1} -> {h2} -> 1
 *
 * This file is auto-generated by train_ml_model.py.
 * DO NOT EDIT - your changes will be overwritten!
 *
 * Trained on local csi_log.db data with ground-truth labels.
 *
 * Author: Francesco Pace <francesco.pace@gmail.com>
 * License: GPLv3
 */

#pragma once

namespace esphome {{
namespace espectre {{

// Feature normalization (StandardScaler)
constexpr float ML_FEATURE_MEAN[{n_features}] = {{{fmt_array(feature_mean)}}};
constexpr float ML_FEATURE_SCALE[{n_features}] = {{{fmt_array(feature_scale)}}};

// Layer 1: {n_features} -> {h1} (ReLU)
constexpr float ML_W1[{n_features}][{h1}] = {{
{fmt_matrix(W1)}
}};
constexpr float ML_B1[{h1}] = {{{fmt_array(b1)}}};

// Layer 2: {h1} -> {h2} (ReLU)
constexpr float ML_W2[{h1}][{h2}] = {{
{fmt_matrix(W2)}
}};
constexpr float ML_B2[{h2}] = {{{fmt_array(b2)}}};

// Layer 3: {h2} -> 1 (Sigmoid)
constexpr float ML_W3[{h2}][1] = {{
{fmt_matrix(W3)}
}};
constexpr float ML_B3[1] = {{{fmt_array(b3)}}};

}}  // namespace espectre
}}  // namespace esphome
"""
    with open(output_path, "w") as f:
        f.write(content)
    print(f"Weights exported to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Train MLP on CSI data")
    parser.add_argument("--db", default="csi_log.db", help="Path to csi_log.db")
    parser.add_argument("--window", type=int, default=75, help="Window size (packets)")
    parser.add_argument("--stride", type=int, default=37, help="Window stride")
    parser.add_argument("--epochs", type=int, default=100, help="Training epochs")
    parser.add_argument("--lr", type=float, default=0.01, help="Learning rate")
    parser.add_argument("--output", default="components/espectre/ml_weights.h")
    parser.add_argument("--max-windows", type=int, default=50000,
                        help="Max windows per node (balance classes)")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        sys.exit(1)

    nodes = [
        "csi_kuchyn",
        "csi_loznice",
        "csi_senzor",
        "csi_obyvak1",
        "csi_obyvak_c6",
        "csi_obyvak_c5",
        "csi_chodba",
    ]
    print(f"Loading data from {args.db}...")
    X, y = load_data(args.db, nodes, args.window, args.stride, args.max_windows)

    print(f"\nDataset: {len(X)} windows, {int(y.sum())} motion, {int(len(y) - y.sum())} idle")
    print(f"Motion ratio: {y.mean():.1%}")

    if len(X) < 100:
        print("Not enough data to train!")
        sys.exit(1)

    # Balance classes by undersampling majority class
    motion_idx = np.where(y > 0.5)[0]
    idle_idx = np.where(y <= 0.5)[0]
    n_minority = min(len(motion_idx), len(idle_idx))
    if n_minority < 50:
        print("Not enough samples in minority class!")
        sys.exit(1)
    rng = np.random.RandomState(42)
    motion_sampled = rng.choice(motion_idx, size=n_minority, replace=False)
    idle_sampled = rng.choice(idle_idx, size=n_minority, replace=False)
    balanced_idx = np.concatenate([motion_sampled, idle_sampled])
    rng.shuffle(balanced_idx)
    X = X[balanced_idx]
    y = y[balanced_idx]
    print(f"Balanced: {len(X)} windows ({int(y.sum())} motion, {int(len(y) - y.sum())} idle)")

    # Normalize features (StandardScaler)
    feature_mean = X.mean(axis=0)
    feature_scale = X.std(axis=0)
    feature_scale[feature_scale < 1e-10] = 1.0
    X_norm = (X - feature_mean) / feature_scale

    # Train/test split (80/20)
    n = len(X_norm)
    perm = np.random.RandomState(42).permutation(n)
    split = int(0.8 * n)
    X_train, y_train = X_norm[perm[:split]], y[perm[:split]]
    X_test, y_test = X_norm[perm[split:]], y[perm[split:]]

    print(f"Train: {len(X_train)}, Test: {len(X_test)}")
    print(f"\nTraining MLP ({X.shape[1]} -> 18 -> 9 -> 1)...")

    W1, b1, W2, b2, W3, b3 = train_mlp(
        X_train, y_train,
        epochs=args.epochs, lr=args.lr
    )

    # Test evaluation
    def relu(x): return np.maximum(0, x)
    def sigmoid(x): return 1.0 / (1.0 + np.exp(-np.clip(x, -20, 20)))

    z1 = X_test @ W1 + b1; a1 = relu(z1)
    z2 = a1 @ W2 + b2; a2 = relu(z2)
    z3 = a2 @ W3 + b3; preds = sigmoid(z3).flatten()

    acc = np.mean((preds > 0.5) == (y_test > 0.5))
    tp = np.sum((preds > 0.5) & (y_test > 0.5))
    fp = np.sum((preds > 0.5) & (y_test <= 0.5))
    fn = np.sum((preds <= 0.5) & (y_test > 0.5))
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    print(f"\nTest Results:")
    print(f"  Accuracy:  {acc:.4f}")
    print(f"  Precision: {precision:.4f}")
    print(f"  Recall:    {recall:.4f}")
    print(f"  F1 Score:  {f1:.4f}")

    # Export weights
    export_weights(W1, b1, W2, b2, W3, b3, feature_mean, feature_scale, args.output)
    print("Done!")


if __name__ == "__main__":
    main()
