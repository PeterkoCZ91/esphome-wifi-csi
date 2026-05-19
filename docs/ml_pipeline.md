# ML Pipeline

## Overview

The fork includes a supervised ML pipeline that trains a presence/motion classifier on labeled CSI data collected from deployed nodes. The model runs inference at the firmware level (MLP weights baked into `ml_weights.h`) and additionally via a standalone MQTT service for multi-node fusion.

## Features (15 per node)

The `BaseDetector` computes 15 statistical features from the circular turbulence buffer and per-packet CSI:

| # | Feature | Source |
|---|---------|--------|
| 1 | `turbulence_mean` | Mean of amplitude turbulence buffer |
| 2 | `turbulence_std` | Std dev of turbulence buffer |
| 3 | `turbulence_max` | Max turbulence in buffer |
| 4 | `turbulence_min` | Min turbulence in buffer |
| 5 | `turbulence_p25` | 25th percentile |
| 6 | `turbulence_p75` | 75th percentile |
| 7 | `turbulence_skew` | Skewness of buffer distribution |
| 8 | `turbulence_kurtosis` | Kurtosis of buffer distribution |
| 9 | `turbulence_iqr` | Interquartile range |
| 10 | `turbulence_range` | Max − Min |
| 11 | `turbulence_trend` | Linear trend coefficient |
| 12 | `phase_turbulence` | Std of inter-subcarrier phase differences per packet |
| 13 | `ratio_turbulence` | SA-WiSense ratio metric |
| 14 | `breathing_score` | 0.08–0.6 Hz bandpass energy (breathing-rate band) |
| 15 | `dser` | Dynamic-to-Static Energy Ratio (Uni-Fi, arXiv 2601.10980) |

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

### Single-node model

```bash
python3 train_ml_model.py --nodes csi_your_node
```

Output: `models/ml_weights_{room}.h` (baked into firmware via ESPHome YAML)

### Multi-node fusion model (45 features, 3 RX nodes)

```bash
python3 train_multinode_ml.py \
  --nodes csi_node_1 csi_node_2 csi_node_3
```

Architecture: MLP 45→16→8→1  
Cross-validated F1 (sit/walk vs empty): **0.833**

Output: `models/multinode_ml_{room}.pkl` used by `ml_inference_service.py`

## Inference service

`ml_inference_service.py` runs as a persistent MQTT service, consuming sensor data from all nodes and publishing fused presence predictions:

```bash
docker compose -f docker-compose.tools.yml up -d ml-inference
```

Published topics:
```
esphome/ml_{room}/sensor/presence/state    → "ON"/"OFF"
esphome/ml_{room}/sensor/confidence/state  → 0.0–1.0
esphome/ml_{room}/sensor/motion_label/state → "walk"/"sit"/"empty"
```

## Dual-threshold hysteresis

The `MLDetector` uses asymmetric thresholds to reduce false positives:

```
Enter presence: score > 0.50  (4 of last 6 frames)
Exit presence:  score < 0.35  (5 of last 6 frames)
```

This prevents flickering in borderline cases (person sitting still, slow breathing-rate motion).

## Validating against CSI-Bench

```bash
python3 tools/validate_csibench.py --dataset /path/to/csibench
```

CSI-Bench provides 802.11n CSI captures from standard lab scenarios (sitting, walking, empty). The validator maps our 15 features onto CSI-Bench data and reports per-class F1.
