#!/usr/bin/env python3
"""
Offline validation of our 17-feature MLP against CSI-Bench dataset.

Purpose:
- Cross-home generalization (26 environments, 35 users in CSI-Bench)
- Motion Source Recognition: check if our binary MLP is fooled by pet/robot/fan

Inputs:
- components/espectre/ml_weights.h   (17-feature MLP trained on our csi_log.db)
- CSI-Bench dataset (HDF5), default /tmp/csi-bench-data
- CSI-Bench repo (for format reference), default /tmp/csi-bench

Output:
- tools/results/csibench_validation.md (markdown report)

Design notes:
- CSI-Bench samples shape (time_index, feature_size=232, 1), CSI_amps already
  amplitude (not complex). We must downsample to our 12-subcarrier pipeline.
- Simple strategy: even stride pick 12 of 232 (~every 19th). Better would be
  NBVI-equivalent band selection but that needs calibration data we don't have.
- Our MLP uses turbulence (std across subcarriers) as primary signal — we compute
  it online per packet from their amplitudes, then slide 75-packet windows.
- phase_turbulence, ratio_turbulence, breathing_score — paper provides only
  amplitudes, so phase_turb=0 (best-effort); ratio_turb and breathing_score
  computed from amplitude series as in our pipeline.

Usage:
    python3 tools/validate_csibench.py --smoke          # synthetic data test
    python3 tools/validate_csibench.py --dataset /path  # real CSI-Bench run
    python3 tools/validate_csibench.py --help
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

# Make train_ml_model importable as a sibling (reuse extract_features + breathing filter)
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
try:
    from train_ml_model import extract_features, breathing_filter_series  # noqa: E402
except ImportError as e:
    print(f"ERROR: cannot import train_ml_model: {e}", file=sys.stderr)
    sys.exit(1)

ML_WEIGHTS_H = REPO_ROOT / "components" / "espectre" / "ml_weights.h"
DEFAULT_DATASET = Path("/tmp/csi-bench-data")
DEFAULT_REPO = Path("/tmp/csi-bench")
REPORT_PATH = REPO_ROOT / "tools" / "results" / "csibench_validation.md"

WINDOW_SIZE = 75
STRIDE = 37
TARGET_SUBCARRIERS = 12  # our NBVI-filtered pipeline


# ---------------------------------------------------------------------------
# 1. Parse ml_weights.h into numpy arrays
# ---------------------------------------------------------------------------

def _parse_float_array(text: str, name: str) -> np.ndarray:
    """Extract a 1D or 2D C float array declaration by name."""
    # Match `constexpr float NAME[dims...] = { ... };`
    pattern = rf"constexpr\s+float\s+{re.escape(name)}\s*(?:\[\s*\d+\s*\])+\s*=\s*(\{{.*?\}});"
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        raise ValueError(f"Cannot find array '{name}' in ml_weights.h")
    body = m.group(1)
    # Find all floats (accept "0.123f", "-1.234e-5f", "1.0f")
    nums = re.findall(r"-?\d+\.\d+(?:[eE][+-]?\d+)?f?", body)
    # Detect dims: count "{ ... }" nesting level
    row_groups = re.findall(r"\{([^{}]*)\}", body)
    if len(row_groups) > 1:
        rows = []
        for r in row_groups:
            row_nums = re.findall(r"-?\d+\.\d+(?:[eE][+-]?\d+)?f?", r)
            rows.append([float(x.rstrip("f")) for x in row_nums])
        return np.array(rows, dtype=np.float32)
    return np.array([float(x.rstrip("f")) for x in nums], dtype=np.float32)


def load_ml_weights() -> dict:
    """Parse ml_weights.h into a dict of numpy arrays."""
    text = ML_WEIGHTS_H.read_text()
    return {
        "mean": _parse_float_array(text, "ML_FEATURE_MEAN"),
        "scale": _parse_float_array(text, "ML_FEATURE_SCALE"),
        "W1": _parse_float_array(text, "ML_W1"),  # (15, 16)
        "B1": _parse_float_array(text, "ML_B1"),  # (16,)
        "W2": _parse_float_array(text, "ML_W2"),  # (16, 8)
        "B2": _parse_float_array(text, "ML_B2"),  # (8,)
        "W3": _parse_float_array(text, "ML_W3"),  # (8, 1)
        "B3": _parse_float_array(text, "ML_B3"),  # (1,)
    }


def mlp_forward(features: np.ndarray, w: dict) -> float:
    """Forward pass through 15->16->8->1 MLP. Returns sigmoid(logit)."""
    x = (features - w["mean"]) / w["scale"]
    h1 = np.maximum(0.0, x @ w["W1"] + w["B1"])        # ReLU
    h2 = np.maximum(0.0, h1 @ w["W2"] + w["B2"])       # ReLU
    logit = (h2 @ w["W3"] + w["B3"])[0]
    return float(1.0 / (1.0 + np.exp(-logit)))


# ---------------------------------------------------------------------------
# 2. CSI-Bench HDF5 loading + amplitude downsample
# ---------------------------------------------------------------------------

def decimate_to_12(amplitudes: np.ndarray) -> np.ndarray:
    """Downsample feature_size to our 12-subcarrier pipeline.

    Input:  (T, F) or (T, F, 1)
    Output: (T, 12)
    """
    if amplitudes.ndim == 3 and amplitudes.shape[-1] == 1:
        amplitudes = amplitudes.squeeze(-1)
    T, F = amplitudes.shape
    if F < TARGET_SUBCARRIERS:
        # Pad by repeating last column
        pad = np.repeat(amplitudes[:, -1:], TARGET_SUBCARRIERS - F, axis=1)
        return np.concatenate([amplitudes, pad], axis=1)
    # Pick 12 evenly-spaced indices
    idx = np.linspace(0, F - 1, TARGET_SUBCARRIERS).astype(int)
    return amplitudes[:, idx]


def load_h5_sample(path: Path, data_key: str = "CSI_amps") -> np.ndarray:
    """Load one HDF5 sample, return (T, F) amplitudes downsampled to 12."""
    import h5py
    with h5py.File(path, "r") as f:
        if data_key not in f:
            # Fall back to first dataset
            data_key = list(f.keys())[0]
        data = np.asarray(f[data_key][()], dtype=np.float32)
    return decimate_to_12(data)


# ---------------------------------------------------------------------------
# 3. Per-packet metrics (mirror BaseDetector::process_packet)
# ---------------------------------------------------------------------------

def packet_metrics(amps_12: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """From (T, 12) amplitudes compute per-packet:
    - turbulence: std across 12 subcarriers
    - ratio_turbulence: std of adjacent subcarrier amplitude ratios
    - amplitude_sum: sum across 12
    Returns three (T,) arrays.
    """
    T = amps_12.shape[0]
    turb = amps_12.std(axis=1)
    amp_sum = amps_12.sum(axis=1)
    # Adjacent ratios per packet
    ratios = np.zeros(T, dtype=np.float32)
    for t in range(T):
        row = amps_12[t]
        safe = row[1:] > 0.1
        if safe.sum() > 1:
            r = row[:-1][safe] / row[1:][safe]
            ratios[t] = r.std()
    return turb, ratios, amp_sum


def windows_to_features(turb, ratios, amp_sum):
    """Slide (window=75, stride=37) and extract 15 features per window.
    phase_turbulence is 0 (no phase data in CSI_amps).
    """
    breath = breathing_filter_series(amp_sum.astype(np.float32))
    out = []
    n = len(turb)
    for start in range(0, n - WINDOW_SIZE + 1, STRIDE):
        stop = start + WINDOW_SIZE
        feats = extract_features(
            turb_window=turb[start:stop],
            phase_turb=0.0,  # amplitude-only dataset
            ratio_turb=float(ratios[start:stop].mean()),
            breathing_score=float(breath[start:stop].mean()),
            dser=0.0,  # Uni-Fi features not available in CSI-Bench (amplitude-only)
            plcr=0.0,
        )
        out.append(feats)
    return np.stack(out) if out else np.empty((0, 17), dtype=np.float32)


# ---------------------------------------------------------------------------
# 4. CSI-Bench metadata discovery
# ---------------------------------------------------------------------------

def discover_task_samples(dataset_root: Path, task_name: str, max_files: int = 200):
    """Walk dataset_root looking for .h5 files under task_name subtree.

    Returns list of dicts {path, label}. label inferred from directory name
    after 'act_' prefix (common CSI-Bench convention).
    """
    task_dir = None
    for candidate in (dataset_root / task_name, dataset_root / task_name.lower()):
        if candidate.is_dir():
            task_dir = candidate
            break
    if task_dir is None:
        return []

    # Load label_mapping if present
    label_map_path = task_dir / "metadata" / "label_mapping.json"
    label_map = {}
    if label_map_path.exists():
        try:
            label_map = json.loads(label_map_path.read_text())
        except Exception:
            pass

    samples = []
    for h5 in task_dir.rglob("*.h5"):
        # Infer label from ancestor directory prefixed with 'act_'
        label = None
        for part in h5.parts:
            if part.startswith("act_"):
                label = part[len("act_"):]
                break
        samples.append({"path": h5, "label": label})
        if len(samples) >= max_files:
            break
    return samples


# ---------------------------------------------------------------------------
# 5. Evaluation entry points
# ---------------------------------------------------------------------------

def evaluate_samples(samples, weights, threshold=0.5):
    """Run MLP inference on each sample's windows. Return list of records."""
    records = []
    for s in samples:
        try:
            amps = load_h5_sample(s["path"])
        except Exception as e:
            print(f"  skip {s['path'].name}: {e}", file=sys.stderr)
            continue
        if amps.shape[0] < WINDOW_SIZE:
            continue
        turb, ratios, amp_sum = packet_metrics(amps)
        feats = windows_to_features(turb, ratios, amp_sum)
        if feats.shape[0] == 0:
            continue
        scores = np.array([mlp_forward(f, weights) for f in feats])
        records.append({
            "path": str(s["path"]),
            "label": s["label"],
            "n_windows": int(feats.shape[0]),
            "score_mean": float(scores.mean()),
            "score_max": float(scores.max()),
            "motion_frac": float((scores > threshold).mean()),
        })
    return records


def summarize(records, out_path: Path):
    """Write markdown report grouped by label."""
    if not records:
        out_path.write_text("# CSI-Bench validation\n\nNo valid samples.\n")
        return
    by_label = {}
    for r in records:
        by_label.setdefault(r["label"] or "unlabeled", []).append(r)

    lines = ["# CSI-Bench validation report", ""]
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Samples: {len(records)} | Labels: {len(by_label)}")
    lines.append("")
    lines.append("## Score distribution by label")
    lines.append("")
    lines.append("| Label | N | mean(score) | std | mean(motion_frac) |")
    lines.append("|---|---|---|---|---|")
    for lbl in sorted(by_label):
        rs = by_label[lbl]
        scores = np.array([r["score_mean"] for r in rs])
        mfrac = np.array([r["motion_frac"] for r in rs])
        lines.append(
            f"| {lbl} | {len(rs)} | {scores.mean():.3f} | {scores.std():.3f} | {mfrac.mean():.3f} |"
        )
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append("- High mean(score) + high motion_frac on **human** class = model still detects people")
    lines.append("- Same metrics on **pet/robot/fan** = false-positive leakage (confirms retrain need)")
    lines.append("- Large std across human samples = poor cross-home generalization")
    lines.append("")
    out_path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# 6. Smoke / synthetic test
# ---------------------------------------------------------------------------

def smoke_test(weights):
    """Run the pipeline on a synthetic sample (verify wiring, no dataset)."""
    rng = np.random.default_rng(42)
    # Fake motion: larger variance + drift
    T, F = 500, 232
    base = 20.0 + rng.normal(0, 0.5, size=(T, F))  # IDLE-like
    motion = base + rng.normal(0, 4.0, size=(T, F)) + np.sin(np.arange(T))[:, None] * 3
    for name, data in [("synthetic_idle", base), ("synthetic_motion", motion)]:
        amps = decimate_to_12(data)
        turb, ratios, amp_sum = packet_metrics(amps)
        feats = windows_to_features(turb, ratios, amp_sum)
        scores = np.array([mlp_forward(f, weights) for f in feats])
        print(
            f"  {name}: {feats.shape[0]} windows, "
            f"score mean={scores.mean():.3f} max={scores.max():.3f} "
            f"motion_frac={(scores > 0.5).mean():.3f}"
        )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", default=str(DEFAULT_DATASET),
                   help=f"CSI-Bench dataset root (default: {DEFAULT_DATASET})")
    p.add_argument("--task", default="MotionSourceRecognition",
                   help="Task subfolder to evaluate (default: MotionSourceRecognition)")
    p.add_argument("--max-files", type=int, default=200,
                   help="Cap the number of H5 samples to evaluate (default: 200)")
    p.add_argument("--smoke", action="store_true",
                   help="Run synthetic smoke test only (no dataset needed)")
    p.add_argument("--report", default=str(REPORT_PATH),
                   help=f"Output report path (default: {REPORT_PATH})")
    args = p.parse_args()

    print("Loading MLP weights...")
    weights = load_ml_weights()
    print(f"  mean shape {weights['mean'].shape}, W1 shape {weights['W1'].shape}")

    if args.smoke:
        print("Running smoke test on synthetic data...")
        smoke_test(weights)
        print("OK — pipeline end-to-end works.")
        return 0

    dataset = Path(args.dataset)
    if not dataset.is_dir():
        print(f"ERROR: dataset root {dataset} does not exist.", file=sys.stderr)
        print("Download CSI-Bench from Kaggle (link in arXiv 2505.21866) and unpack to that path.",
              file=sys.stderr)
        return 1

    print(f"Discovering samples under {dataset}/{args.task} ...")
    samples = discover_task_samples(dataset, args.task, max_files=args.max_files)
    print(f"  found {len(samples)} samples")
    if not samples:
        return 2

    print("Evaluating...")
    records = evaluate_samples(samples, weights)
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    summarize(records, out)
    print(f"Report written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
