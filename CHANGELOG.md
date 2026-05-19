# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [1.0.0] — 2026-05-19

First public release. Based on upstream [ESPectre v2.7](https://github.com/francescopace/espectre).

### Added over upstream ESPectre v2.7

- **Breathing rate BPM sensor** — DFT-based estimation over 0.08–0.6 Hz bandpass with timestamp-corrected sample rate; works on single-node AP-link setups
- **UDP TX mode** — traffic generator sends standard UDP packets routed as HT/VHT frames by the AP; gives 206 pkt/s CSI on ESP32-C5/C6 vs ~8 pkt/s with ESP-NOW legacy PHY
- **5 GHz pairwise mesh (ESP32-C5)** — 4-node star mesh on channel 52 (5.26 GHz); native STA receive path without promiscuous mode
- **Multi-node peer_macs** — single RX node can receive CSI from up to 4 TX nodes simultaneously
- **Multi-node ML pipeline** — 45-feature MLP (15 statistical features × 3 RX nodes), per-room training via labeled Docker sessions, cross-validated F1 = 0.833
- **DSER / PLCR metrics** — Dynamic-to-Static Energy Ratio and Path-Length Change Rate proxy from Uni-Fi research
- **Phase turbulence sensor** — standard deviation of inter-subcarrier phase differences
- **Ratio turbulence sensor** — SA-WiSense amplitude ratio metric
- **Hampel outlier filter** — median-based spike rejection on CSI turbulence buffer (enabled by default)
- **Idle-gated baseline calibration** — improved over upstream basic implementation
- **Breathing-aware presence hold** — suppresses presence exit during confirmed breathing activity
- **ESP32-C5 / C6 hardware path** — WiFiLifecycleManager configures HT20 + STA receive path for 802.11ax nodes without promiscuous mode

### Changed from upstream

- `temporal smoothing` opt-in (`smoothing_enabled: false` by default, backward-compatible)
- `hysteresis_factor` default 1.0 (identical to upstream behavior when not configured)
- `train_multinode_ml.py` default nodes updated to reflect 3-node RX mesh (`c5b`, `c5c`, `c5d`)

### Fixed

- ESP-NOW TX on ESP32-C6/S3 falling into DNS fallback mode (stale build cache issue)
- BPM estimation now uses actual packet timestamps instead of assumed fixed sample rate
- `espectre-c6-template.yaml` node name validation (uppercase caused ESPHome reject)

### Infrastructure

- CI: ESPHome compile check for both templates and all 3 examples on every push
- Docker: `csi-logger-session`, `ml-inference` compose services
- `secrets.yaml.example` — complete template with all required keys
