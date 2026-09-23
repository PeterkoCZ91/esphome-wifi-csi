# ML Pipeline

## Overview

The fork includes a supervised ML pipeline that trains a presence/motion classifier on labeled CSI data collected from deployed nodes. The model runs inference at the firmware level (MLP weights baked into `ml_weights.h`) and additionally via a standalone MQTT service for multi-node fusion.

## Two models, two feature sets

| | On-device model | Multi-node fusion model |
|---|---|---|
| Runs in | firmware (`detection_algorithm: ml`), every packet | `ml_inference_service.py` (MQTT, off-device) |
| Trained by | `train_ml_model.py` | `train_multinode_ml.py` |
| Output | `components/espectre/ml_weights.h` | `models/multinode_mlp.pkl`, `models/multinode_scaler.pkl` |
| Input | 17 features from one node | 15 features per node × 3 RX nodes = 45 |
| Architecture | MLP 17→18→9→1 | MLP 45→32→16→1 (scikit-learn) |

### On-device features (17)

Computed by `extract_ml_features()` in `components/espectre/ml_features.h` from the node's turbulence window plus per-packet metrics:

| # | Feature | Source |
|---|---------|--------|
| 0 | `turb_mean` | Mean of the turbulence window |
| 1 | `turb_std` | Standard deviation |
| 2 | `turb_max` | Maximum |
| 3 | `turb_min` | Minimum |
| 4 | `turb_zcr` | Zero-crossing rate around the mean |
| 5 | `turb_skewness` | Fisher skewness |
| 6 | `turb_kurtosis` | Excess kurtosis |
| 7 | `turb_entropy` | Shannon entropy (10-bin histogram) |
| 8 | `turb_autocorr` | Lag-1 autocorrelation |
| 9 | `turb_mad` | Median absolute deviation |
| 10 | `turb_slope` | Linear-regression slope |
| 11 | `waveform_length` | Sum of absolute first differences |
| 12 | `phase_turbulence` | Std of inter-subcarrier phase differences |
| 13 | `ratio_turbulence` | SA-WiSense adjacent amplitude ratio std |
| 14 | `breathing_score` | 0.08–0.6 Hz bandpass energy |
| 15 | `dser` | Dynamic-to-Static Energy Ratio (Uni-Fi, arXiv 2601.10980) |
| 16 | `plcr` | Path-Length Change Rate proxy (Uni-Fi) |

> **Experimental:** the shipped `ml_weights.h` was trained on data logged every ~2 s (subcarrier amplitudes published by a template sensor), while the firmware computes these features over a per-packet window (~0.75 s at 100 pkt/s, after Hampel/low-pass filtering). The two feature distributions differ, so treat the on-device ML detector as experimental and prefer the default `mvs` detector. Retraining on matching data is on the roadmap. Phase turbulence values also changed when phase differences started being wrapped to [-π, π].

### Fusion features (15 per node)

Computed by `extract_features_from_state()` in `ml_inference_service.py` (and `extract_features()` in `train_multinode_ml.py`) from each node's latest MQTT state:

| # | Feature | Source |
|---|---------|--------|
| 0–4 | mean, std, max, min, range | 12 subcarrier amplitudes (`CSI Subcarrier Amplitudes` template sensor) |
| 5 | `movement_score` | Component movement sensor |
| 6 | `phase_turbulence` | `CSI Phase Turbulence` template sensor |
| 7 | `breathing_score` | Breathing score sensor |
| 8 | `presence` | Presence binary sensor (0/1) |
| 9 | `dser` | DSER sensor |
| 10 | `plcr` | PLCR sensor |
| 11–14 | CV, skewness, kurtosis, entropy | 12 subcarrier amplitudes |

## Data collection

### Single-session workflow

```bash
# Start a labeled session
docker compose -f docker-compose.tools.yml run --rm lab-session \
  start --room living_room --activity walk --note "pacing 2m corridor"

# Watch live logging
docker compose -f docker-compose.tools.yml logs -f csi-logger-session

# Stop the session
docker compose -f docker-compose.tools.yml run --rm lab-session stop
```

Activities: `presence`, `empty`, `walk`, `sit`, `fall`, `idle`  
Rooms: any alphanumeric string with underscores/hyphens (e.g. `living_room`, `bedroom`, `office`)

### Recommended session lengths

| Activity | Minimum | Recommended |
|----------|---------|-------------|
| empty | 5 min | 15 min |
| walk | 3 min | 8 min |
| sit | 5 min | 10 min |

Collect sessions at different times of day — WiFi interference varies with neighbor usage.

## Training

### On-device model

```bash
python3 train_ml_model.py                    # all nodes with labeled data in csi_log.db
python3 train_ml_model.py --output my_weights.h --window 75   # custom output / window
```

Output: `components/espectre/ml_weights.h` by default (compiled into the firmware on the next build).

### Multi-node fusion model (45 features, 3 RX nodes)

```bash
python3 train_multinode_ml.py \
  --nodes csi_node_1 csi_node_2 csi_node_3
```

Architecture: MLP 45→32→16→1 (scikit-learn `MLPClassifier`)  
Cross-validated F1 (sit/walk vs empty): **0.833**

Output: `models/multinode_mlp.pkl` + `models/multinode_scaler.pkl`, loaded by `ml_inference_service.py`

## Inference service

`ml_inference_service.py` runs as a persistent MQTT service, consuming sensor data from all nodes and publishing fused presence predictions:

```bash
docker compose -f docker-compose.tools.yml up -d ml-inference
```

Published topics:
```
esphome/ml_{room}/sensor/occupancy_probability/state  → 0.000–1.000
esphome/ml_{room}/binary_sensor/occupied/state        → "ON"/"OFF"
esphome/ml_{room}/availability                        → "online"/"offline" (retained, LWT)
```

## Dual-threshold hysteresis

The on-device `MLDetector` uses asymmetric thresholds (`ML_EXIT_FACTOR = 0.7`) plus the shared temporal smoothing:

```
Enter motion: probability > threshold (default 0.50), 4 of the last 6 decisions
Exit motion:  probability < threshold × 0.7 (0.35), 5 of the last 6 decisions
```

This prevents flickering in borderline cases (person sitting still, slow breathing-rate motion).

## Validating against CSI-Bench

```bash
python3 tools/validate_csibench.py --dataset /path/to/csibench
```

CSI-Bench provides 802.11n CSI captures from standard lab scenarios (sitting, walking, empty). The validator maps our features onto CSI-Bench data and reports per-class F1.
