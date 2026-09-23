# ESPectre Fork — WiFi CSI Presence & Breathing Detection

[![CI](https://github.com/PeterkoCZ91/esphome-wifi-csi/actions/workflows/ci.yml/badge.svg)](https://github.com/PeterkoCZ91/esphome-wifi-csi/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

> ESPHome external component for human presence detection using WiFi Channel State Information (CSI) on ESP32.  
> Fork of [ESPectre](https://github.com/francescopace/espectre) by Francesco Pace (GPLv3).  
> Based on upstream ESPectre v2.7 — extended with breathing rate, pairwise/multi-node sensing, a multi-node ML pipeline, UDP TX mode, and experimental 5 GHz on ESP32-C5.

## Quick Start — Level 1 (5 minutes)

Get presence detection running on any ESP32 with your home WiFi router.

**Prerequisites:** Python 3.12+ (required by current ESPHome), any ESP32 dev board with USB, your WiFi credentials, an MQTT broker on your network.

### 1. Clone and install ESPHome

```bash
git clone https://github.com/PeterkoCZ91/esphome-wifi-csi.git
cd esphome-wifi-csi
python3 -m venv .venv && .venv/bin/pip install esphome
```

### 2. Create secrets.yaml

```bash
cp secrets.yaml.example secrets.yaml
cp secrets.yaml examples/secrets.yaml   # ESPHome looks for secrets next to the YAML
```

Edit `secrets.yaml` (and `examples/secrets.yaml`) with your WiFi SSID/password, MQTT broker address and an API encryption key:

```yaml
wifi_ssid: "MyNetwork"
wifi_password: "mypassword"
mqtt_broker: "192.168.x.y"
mqtt_username: ""
mqtt_password: ""
api_key: <32-byte-base64-key>              # also used to encrypt OTA uploads
fallback_ap_password: change-this-pw       # hotspot password if WiFi is unreachable (min. 8 chars)
```

Generate the API key with:

```bash
python3 -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"
```

### 3. Flash your ESP32

Connect via USB, then:

```bash
.venv/bin/esphome run examples/minimal_self_sensing.yaml
```

ESPHome compiles the firmware, flashes over USB, and switches to OTA for future updates.

### 4. Watch the sensors

Subscribe to MQTT and watch motion data arrive within seconds of boot:

```bash
mosquitto_sub -h 192.168.x.y -t "esphome/csi-presence/#" -v
```

You should see `movement_score` values update every second and `presence_detected` flip ON
when you walk through the room.

**That's it for Level 1.** For pairwise sensing (Level 2) or experimental 5 GHz (Level 4), see the sections below.

---

## What you can build

**Level 1 — One ESP32, one router**  
Plug any ESP32 near your WiFi router. Get presence detection, motion scoring, and real-time breathing rate — no extra hardware.

**Level 2 — Two ESP32s: pairwise sensing**  
Add a dedicated TX node. Now the sensing zone is the direct path between two nodes — no cross-room interference, cleaner data, better ML.

**Level 3 — Mesh: 3–4 nodes, spatial coverage**  
Multiple RX nodes around a room. 45-feature ML classifier distinguishes sitting, walking, and empty with cross-validated F1 = 0.833.

**Level 4 — 5 GHz (ESP32-C5, experimental)**  
Pairwise CSI on the 5 GHz band via `band_mode: 5ghz`. No promiscuous mode — native 802.11ax STA receive path. Not yet validated with this release; see [Level 4](#level-4--5-ghz-pairwise-mesh-esp32-c5-experimental).

---

## Level 1 — Single node, AP-link CSI

The simplest setup. Your ESP32 captures CSI from your router's periodic beacon frames — the signal changes when a person walks through the room.

The core ESPectre config block registers sensors directly — no template sensors needed:

```yaml
espectre:
  id: espectre_csi
  movement_sensor:
    name: "Movement Score"
  breathing_rate_sensor:
    name: "Breathing Rate"
  presence_sensor:
    name: "Presence Detected"
```

See [`examples/minimal_self_sensing.yaml`](examples/minimal_self_sensing.yaml) for the complete config including `esphome:`, `esp32:`, `wifi:`, and `mqtt:` blocks.

### What this fork adds over upstream ESPectre

| Feature | Upstream v2.7 | This fork |
|---------|--------------|-----------|
| Breathing rate BPM | ✗ | ✓ DFT, timestamp-based sample rate |
| Phase turbulence | ✗ | ✓ |
| DSER / PLCR (Uni-Fi) | ✗ | ✓ |
| Pairwise sensing (`peer_mac` / `peer_macs`) | ✗ | ✓ up to 5 TX peers |
| MOTION→IDLE hysteresis, temporal smoothing, quiet-period auto-calibration | ✗ | ✓ |
| Idle-gated baselines, breathing-aware presence hold | ✗ | ✓ |
| Multi-node ML fusion (off-device MQTT service) | ✗ | ✓ |
| Data collection tooling | ✗ | ✓ Docker + SQLite + labeled sessions |
| UDP TX mode (pairwise on C5/C6) | ✗ | ✓ HT/VHT frames → 206 pkt/s vs 8 with ESP-NOW |

**Breathing rate** is the standout feature for single-node setups. The BPM estimate runs a DFT (6–30 BPM in 2-BPM steps) over the output of a 0.08–0.6 Hz bandpass and uses packet timestamps for the actual sample rate. Caveat: the bandpass coefficients themselves assume ~100 pkt/s, so at very different packet rates the passband shifts (tracked on the roadmap).

---

## Level 2 — Pairwise sensing (2 nodes)

Add a second ESP32 as a dedicated TX node. It sends probe packets (ESP-NOW broadcast or UDP, see below) at e.g. 200 packets/second. The RX node captures CSI from those packets — not from the router.

**Why this matters:** AP-link CSI sees everything in the RF path, including through walls and from adjacent rooms. Pairwise CSI is bounded by the direct path between your two nodes.

```
[TX node] ──200 pkt/s ESP-NOW──→ [RX node]
                                   captures CSI from TX packets only
```

### TX node config

```yaml
espectre:
  traffic_generator_mode: udp          # use udp if RX node is C5 or C6
  traffic_generator_rate: 200
  traffic_generator_udp_host: "192.168.x.y"   # RX node IP (udp mode only)
```

> **Note:** `espnow` also works, but only for classic ESP32 RX nodes. See [TX mode and PHY compatibility](#tx-mode-and-phy-compatibility) below.

### RX node config

```yaml
espectre:
  peer_mac: "AA:BB:CC:DD:EE:FF"   # TX node MAC address
```

See [`examples/pairwise_tx_node.yaml`](examples/pairwise_tx_node.yaml) and [`examples/pairwise_rx_node.yaml`](examples/pairwise_rx_node.yaml).

### Hardware note

On **classic ESP32** (Xtensa LX6), the MAC scheduler shares bandwidth between TX and CSI-RX. A TX node running at 200 pkt/s leaves little bandwidth for receiving — so a single node cannot reliably do both at full rate.

**ESP32-C5 and C6** (802.11ax) have a dedicated hardware channel estimation block that runs independently of the TX scheduler. Both TX at 200 pkt/s and RX at full CSI rate work simultaneously.

→ For a simple 2-node setup, use one TX-only node and one RX-only node regardless of chip.  
→ For relay nodes that must TX and RX simultaneously, use ESP32-C5 or C6.

See [docs/mesh_architecture.md](docs/mesh_architecture.md) for the full hardware analysis.

---

## Level 3 — 2.4 GHz mesh (3+ nodes)

Three nodes, three sensing links, spatial coverage of a room.

```
A (TX, 200 pkt/s)
    ├──→ B (RX from A, also TX)
    │        └──→ C (RX from A + B)
    └──→ C (RX from A + B)
```

Node C receives CSI from two TX nodes simultaneously using `peer_macs`:

```yaml
espectre:
  peer_macs:
    - "AA:BB:CC:DD:EE:FF"   # Node A MAC
    - "AA:BB:CC:DD:EE:FE"   # Node B MAC
```

Up to 5 entries in `peer_macs` (1 primary + 4 extra, enforced by the config schema). The original single `peer_mac` still works unchanged.

### Active links with classic ESP32

| Link | Status | Notes |
|------|--------|-------|
| A → B | ✓ | Clean pairwise |
| A → C | ✓ | via `peer_macs` |
| B → C | ✓ | via `peer_macs` |
| B → A | ✗ | Classic ESP32 TX+RX MAC starvation |

The B→A link requires simultaneous TX and RX on the same classic ESP32 — not reliably possible. Use ESP32-C5/C6 if you need all bidirectional links.

---

## TX mode and PHY compatibility

The traffic generator mode affects what kind of WiFi frames the TX node sends — and that matters for CSI quality on the RX node.

### ESP-NOW mode

ESP-NOW uses **legacy 802.11b/g PHY** (1–6 Mbps management-style frames). Classic ESP32 (Xtensa) extracts full CSI from these frames. **ESP32-C5 and C6 (802.11ax) do not** — their hardware CSI path is optimised for HT/VHT/HE frames. Legacy frames give ~8 pkt/s CSI on C6 instead of the expected ~200.

```
                        CSI pkt/s on ESP32-C6 RX node
ESP-NOW TX (legacy PHY)  →  7.7 pkt/s   ❌  (HE hw path, legacy frames ignored)
UDP TX    (HT/VHT via AP) →  206.5 pkt/s ✅
```

This was discovered by testing pairwise sensing between an ATOM S3 (TX) and FireBeetle C6 (RX) at the same distance: ESP-NOW gave 7.7 pkt/s regardless of placement; switching to UDP mode gave 206.5 pkt/s.

### UDP mode — recommended for C5/C6 RX nodes

The TX node sends standard UDP packets to the RX node's IP address at the configured rate. The AP forwards them as HT/VHT frames — exactly what C5/C6 need for full CSI extraction.

**TX node config:**
```yaml
espectre:
  traffic_generator_mode: udp
  traffic_generator_rate: 200
  traffic_generator_udp_host: "192.168.x.y"   # RX node IP
  traffic_generator_udp_port: 5000             # optional, default 5000
```

**RX node config** is unchanged — use `peer_mac` as before. The peer filter is a software filter in `CSIManager` on the MAC reported with each CSI frame. Note that it also accepts broadcast frames and frames from the AP BSSID, so on a busy AP some AP traffic reaches the detector as well.

### Which mode to use

| RX node chip | TX mode | CSI pkt/s |
|---|---|---|
| Classic ESP32 (D0WD, S3) | `espnow` | ~100–200 |
| Classic ESP32 (D0WD, S3) | `udp` | ~100–200 |
| ESP32-C5 / C6 (HE) | `espnow` | ~8 ❌ |
| ESP32-C5 / C6 (HE) | `udp` | ~200 ✅ |

ESP-NOW still works well for C5↔C5 links (both ends are HE and exchange native 802.11ax frames). For any link where the **RX node is a C5 or C6**, use `udp` mode on the TX side.

---

## Level 4 — 5 GHz pairwise mesh (ESP32-C5, experimental)

> **Status:** experimental. Up to and including v1.0.0 the component forced ESP32-C5 into 2.4 GHz-only mode, so 5 GHz was not reachable with the published code. The `band_mode` option now makes it selectable, but 5 GHz operation has **not been validated with this release** — verify the channel (`ch:` in the device log) on your hardware.

ESP32-C5 (RISC-V, 802.11ax) is the only supported dual-band chip. Enable 5 GHz on every C5 node of the mesh:

```yaml
espectre:
  band_mode: 5ghz        # 2.4ghz (default) | 5ghz | auto — ESP32-C5 only, config error on other chips
```

Target topology — a star on a fixed 5 GHz channel (e.g. channel 52, 5.26 GHz):

```
C5a (TX, 200 pkt/s)
    ├──→ C5b (RX)
    ├──→ C5c (RX)
    └──→ C5d (RX)
```

**Key implementation detail:** ESP32-C5 must NOT use promiscuous mode for CSI capture. Enabling promiscuous mode caused channel contention and the AP dropping other STA clients. The component uses the native STA receive path with hardware CSI extraction — no promiscuous mode.

### 5 GHz vs 2.4 GHz for CSI sensing (expected trade-offs)

| | 2.4 GHz | 5 GHz (C5) |
|--|---------|-----------|
| Wall penetration | Higher | Lower — better room isolation |
| Multipath richness | Higher | Lower — cleaner signal |
| Interference | More crowded | Less crowded |
| Hardware cost | Lower | ESP32-C5 ~€8 |
| Simultaneous TX+RX | C5 / C6 | C5 |

YAML template: [`espectre-c5-template.yaml`](espectre-c5-template.yaml)

---

## ML pipeline

Collect labeled CSI data, train a per-room MLP classifier, deploy as a standalone MQTT service.

### Quick start

Set your MQTT credentials before starting the tools:

```bash
export MQTT_BROKER=192.168.x.y
export MQTT_USER=youruser      # leave empty if no auth
export MQTT_PASS=yourpassword
```

```bash
# 1. Start continuous logger
docker compose -f docker-compose.tools.yml up -d csi-logger-session

# 2. Collect labeled sessions
docker compose -f docker-compose.tools.yml run --rm lab-session \
  start --room living_room --activity walk
# ... wait 5–10 minutes ...
docker compose -f docker-compose.tools.yml run --rm lab-session stop

# 3a. On-device model (all labeled nodes in csi_log.db) → components/espectre/ml_weights.h
python3 train_ml_model.py

# 3b. Multi-node fusion model (3 RX nodes) → models/multinode_mlp.pkl + multinode_scaler.pkl
python3 train_multinode_ml.py \
  --nodes csi_node_1 csi_node_2 csi_node_3

# 4. Deploy inference service
docker compose -f docker-compose.tools.yml up -d ml-inference
```

### Two different models

| Model | Where it runs | Input | Architecture | Result |
|---|---|---|---|---|
| On-device (`detection_algorithm: ml`) | firmware, every packet | 17 features from the node's turbulence window | MLP 17→18→9→1 (`ml_weights.h`) | experimental — see note |
| Multi-node fusion | `ml_inference_service.py` (MQTT) | 15 features per node × 3 RX nodes = 45 | MLP 45→32→16→1 (scikit-learn) | cross-validated F1 = **0.833** (walk/sit vs empty) |

**On-device features (17):** turbulence mean, std, max, min, zero-crossing rate, skewness, kurtosis, entropy, lag-1 autocorrelation, MAD, slope, waveform length + phase turbulence, ratio turbulence, breathing score, DSER, PLCR (`components/espectre/ml_features.h`).

**Fusion features (15 per node):** mean, std, max, min and range of the 12 subcarrier amplitudes, `movement_score`, phase turbulence, breathing score, presence, DSER, PLCR, amplitude CV, skewness, kurtosis and histogram entropy (`ml_inference_service.py`).

> **Note:** the shipped `ml_weights.h` was trained on logged data sampled every ~2 s, while the firmware computes the same features over a per-packet window (~0.75 s at 100 pkt/s). The feature distributions therefore differ and the on-device ML detector should be treated as experimental; the default `mvs` detector is recommended. Retraining on matching data is on the roadmap.

See [docs/ml_pipeline.md](docs/ml_pipeline.md) for full details.

---

## Entities published by the component

Entities are declared under `espectre:`. With MQTT they appear on `<topic_prefix>/<domain>/<object_id>/state`, where `object_id` is derived from `name` (e.g. `name: "Movement Score"` → `esphome/my-node/sensor/movement_score/state`). With the native API they show up in Home Assistant directly.

| YAML key | Type | Default | Description |
|----------|------|---------|-------------|
| `movement_sensor` | sensor | always created (`Movement Score`) | Detector metric: moving variance of turbulence (MVS) or motion probability (ML) |
| `motion_sensor` | binary_sensor | always created (`Motion Detected`) | Motion state after threshold, hysteresis and smoothing; also ON while the breathing-aware presence hold is active |
| `threshold_number` | number | always created (`Threshold`) | Runtime detection threshold (session-only, recalculated at boot) |
| `calibrate_switch` | switch | always created (`Calibrate`) | Turn ON to recalibrate; turns OFF when done |
| `presence_sensor` | binary_sensor | optional | Motion OR (breathing score and phase turbulence both elevated over idle baseline) |
| `breathing_sensor` | sensor | optional | Breathing score — RMS of 0.08–0.6 Hz bandpassed amplitude |
| `breathing_rate_sensor` | sensor (BPM) | optional | DFT breathing-rate estimate; `unknown` until an estimate exists |
| `phase_turbulence_sensor` | sensor | optional | Std of inter-subcarrier phase differences |
| `dser_sensor` | sensor | optional | Dynamic-to-Static Energy Ratio (Uni-Fi) |
| `plcr_sensor` | sensor | optional | Path-Length Change Rate proxy (Uni-Fi) |

The C5/C6 templates add more diagnostics as ESPHome `template` sensors using the public C++ API (`get_detector()`, `get_csi_manager()`): turbulence, variance, ratio turbulence, amplitude sum, subcarrier amplitudes, calibrating/ready flags, etc. There is no built-in packet-rate entity; add one if you need it:

```yaml
sensor:
  - platform: template
    name: "CSI Packet Rate"
    unit_of_measurement: "pkt/s"
    update_interval: 5s
    lambda: return (float) id(espectre_csi).get_csi_manager()->get_raw_packet_rate_pps();
```

The device log also prints the rate once per publish interval (`… | 100 pkt/s | ch:6 rssi:-52`).

---

## Configuration reference

All options of the `espectre:` block (defaults from `components/espectre/__init__.py`):

| Option | Default | Description |
|--------|---------|-------------|
| `segmentation_threshold` | `auto` | `auto` (P95 × 1.1 of calibration data), `min` (P100), or a number 0.001–10.0 (manual) |
| `segmentation_window_size` | `75` | Turbulence window in packets (10–200) |
| `detection_algorithm` | `mvs` | `mvs` (moving variance) or `ml` (on-device MLP, experimental) |
| `hysteresis_factor` | `0.7` | MOTION→IDLE when variance < threshold × factor (0.3–1.0; 1.0 = no hysteresis) |
| `auto_calibration_minutes` | `10` | Recalibrate once per boot after N quiet minutes; `0` disables |
| `traffic_generator_mode` | `dns` | `dns`, `ping`, `espnow` or `udp` |
| `traffic_generator_rate` | `100` | Packets/s (0–1000); `0` = no generator, rely on external traffic (UDP listener on port 5555) |
| `traffic_generator_udp_host` | — | IPv4 address of the RX node; required for `udp` mode |
| `traffic_generator_udp_port` | `5000` | UDP destination port |
| `publish_interval` | = `traffic_generator_rate` (100 if rate is 0) | Publish every N processed packets |
| `band_mode` | `2.4ghz` | `2.4ghz`, `5ghz` or `auto` — ESP32-C5 only (config error on other chips); 5 GHz is experimental |
| `gain_lock` | `auto` | `auto` (skip lock if AGC < 30), `enabled`, `disabled` |
| `selected_subcarriers` | auto (NBVI calibration) | Fixed list of 1–12 subcarrier indices (0–63) |
| `lowpass_enabled` / `lowpass_cutoff` | `false` / `11.0` Hz | Low-pass filter on turbulence (cutoff 5–20 Hz) |
| `hampel_enabled` / `hampel_window` / `hampel_threshold` | `true` / `7` / `5.0` | Hampel outlier filter (window 3–11, threshold 1–10 MAD) |
| `peer_mac` | — | MAC of the TX node for pairwise sensing |
| `peer_macs` | — | List of 1–5 TX MACs (multi-TX mesh) |
| `ble_channel_enabled` | `auto` | BLE telemetry/control channel (inherited from upstream); `auto` enables it when `esp32_ble_server` is configured |
| `ble_telemetry_interval_ms` | `40` | BLE notify interval (20–500 ms) |

Always-on behavior (no option): temporal smoothing requires 4 of the last 6 decisions to enter MOTION and 5 of 6 to leave it.

---

## Hardware

### Supported modules

| Module | Chip | Band | TX+RX simultaneously |
|--------|------|------|----------------------|
| ATOM S3 Lite | ESP32-S3 | 2.4 GHz | ✗ (MAC starvation) |
| ESP32-CAM | ESP32-D0WD | 2.4 GHz | ✗ |
| ESP32 DevKit | ESP32-D0WD-V3 | 2.4 GHz | ✗ |
| ESP32-C3 boards (DevKitM-1, XIAO, SuperMini) | ESP32-C3 | 2.4 GHz | ✗ — compile-tested only, see [`examples/minimal_self_sensing_c3.yaml`](examples/minimal_self_sensing_c3.yaml) |
| FireBeetle ESP32-C6 (DFR1075) | ESP32-C6 | 2.4 GHz WiFi 6 | ✓ |
| FireBeetle ESP32-C5 (DFR1222) | ESP32-C5 | 2.4 GHz (5 GHz experimental via `band_mode`) | ✓ |

See [docs/hardware_guide.md](docs/hardware_guide.md) for selection guide, antenna notes, and flashing instructions.

---

## Setup

```bash
# 1. Clone and configure
cp secrets.yaml.example secrets.yaml
# Fill in WiFi SSID/password, MQTT broker, credentials

# 2. Install ESPHome
python3 -m venv .venv
.venv/bin/pip install esphome

# 3. Flash a node (OTA after first serial flash)
.venv/bin/esphome upload examples/minimal_self_sensing.yaml --device 192.168.x.y
```

---

## Documentation

- [docs/mesh_architecture.md](docs/mesh_architecture.md) — pairwise sensing theory, mesh topologies, ESP32 TX+RX hardware analysis
- [docs/hardware_guide.md](docs/hardware_guide.md) — module selection, YAML reference, flashing
- [docs/ml_pipeline.md](docs/ml_pipeline.md) — features, data collection, training, inference

---

## Troubleshooting

### CSI packet rate = 0 or stays near zero

The rate is printed in the device log (`… pkt/s`); see [Entities](#entities-published-by-the-component) for an optional template sensor.

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Rate = 0 after boot | CSI not enabled — wrong board or framework | Verify `framework: type: esp-idf` in your YAML; Arduino framework is not supported |
| Rate ≈ 0 on C6 pairwise | TX node using `espnow` mode with C6 RX | Switch TX to `traffic_generator_mode: udp`; see [TX mode and PHY compatibility](#tx-mode-and-phy-compatibility) |
| Rate = 5–8 instead of ~100 | RSSI < −55 dBm, burst-mode delivery | Move node closer to TX or AP; RSSI > −50 dBm recommended for stable operation |
| Rate drops after a few minutes | AP channel change / roaming event | Set a fixed channel on your AP; the ESP reconnects but CSI resumes after ~10 s |

### Presence / motion sensors

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `presence_detected` always ON | Node near HVAC, fan, or vibrating surface | Relocate node, then toggle `Calibrate` in an empty room. Automatic recalibration runs once per boot after `auto_calibration_minutes` (default 10) of quiet |
| `presence_detected` never ON | `movement_score` below threshold | Compare `movement_score` with the `Threshold` entity; if it stays far below, check the CSI packet rate first |
| `movement_score` noisy (4–6 at rest) | Poor RSSI / multipath near metal objects | Increase distance from metal surfaces; use a directional antenna |
| `breathing_rate` unknown | Not enough CSI packets or signal too weak | Needs a steady packet rate (> 10 pkt/s); first estimate after 16 samples (~8 s at 100 pkt/s), full window ~32 s |
| `breathing_rate` jumps 6–30 BPM | Person moving, not resting | BPM is valid only when `movement_score` < 1.5; filter on your MQTT consumer side |

### Flashing and OTA

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `esptool.py: error: argument --port` | Wrong port or device not detected | Run `ls /dev/ttyUSB*` or `ls /dev/ttyACM*` and pass `--device /dev/ttyUSBx` |
| Compilation fails: `espectre` not found | `external_components` path wrong | Path must be relative to the YAML file; for examples use `path: ../components` |
| Duplicate sensor error on compile | Template sensors conflict with native sensors | Remove `sensor: platform: template` blocks; use native sensor keys under `espectre:` instead (e.g. `movement_sensor:`) |
| OTA upload hangs | Node not reachable | Ping the node IP; check it's on the same VLAN as your machine |
| C5 serial flash fails | C5 not in download mode | Hold BOOT button during power-on; release after `Connecting...` appears |

### MQTT

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No messages on broker | Wrong `mqtt_broker` in secrets.yaml | Verify with `mosquitto_sub -h BROKER -t '#' -v` from your machine |
| Topics show `esphome/csi-presence/` not your name | `node_name` substitution not set | Add `substitutions: node_name: my-node` or edit `name:` in `esphome:` block |
| Auth error in logs | MQTT credentials wrong | Check `mqtt_username` / `mqtt_password` in secrets.yaml |

---

## FAQ

**Do I need a dedicated MQTT broker?**  
For the examples and the Python tooling (logger, ML service), yes — Mosquitto on a Raspberry Pi, Docker, or any home server works. For Home Assistant alone the native ESPHome API (`api:`) is enough.

**What WiFi router/AP do I need?**  
Any standard 802.11n/ac AP works for Level 1–3. For the experimental Level 4 (5 GHz C5 mesh, `band_mode: 5ghz`), the AP must offer 5 GHz and must NOT have band steering that forces clients to 2.4 GHz. A dedicated IoT SSID on a fixed channel is strongly recommended.

**Does CSI sensing work through walls?**  
AP-link CSI (Level 1) sees through walls — this is a feature and a limitation. Pairwise sensing (Level 2+) is bounded to the direct TX↔RX path, so wall penetration depends on placement. 5 GHz (C5, experimental) should have lower wall penetration and therefore better room isolation.

**Why is my breathing rate unreliable?**  
BPM estimation requires: (1) a steady CSI rate > 10 pkt/s, (2) a stationary person (low `movement_score`), (3) enough data — one sample is taken every 50 packets into a 64-sample window (~32 s at 100 pkt/s); the first estimate comes after 16 samples, and the sensor stays `unknown` while the filtered signal is too weak. Resolution is 2 BPM (6–30 BPM). Sitting 1–2 m from the node gives the best results.

**What's the difference from upstream ESPectre v2.7?**  
See the [feature table above](#what-this-fork-adds-over-upstream-espectre). The main additions: breathing rate BPM sensor, UDP TX mode for C5/C6 nodes (206 pkt/s vs 8 with ESP-NOW), pairwise sensing with `peer_mac(s)`, multi-node ML pipeline, DSER/PLCR Uni-Fi metrics, hysteresis/auto-calibration, and experimental 5 GHz on ESP32-C5.

**Can I use this with Home Assistant?**  
Yes — either via the native ESPHome API (`api:` block, recommended) or via MQTT discovery. Enable only one of them for Home Assistant, otherwise every entity appears twice. MQTT state topics follow `<topic_prefix>/<domain>/<object_id>/state`.

**How many nodes do I need for good ML accuracy?**  
The included ML model is trained on 3 RX nodes (45 features). A single node gives presence/absence; 3+ nodes enable activity classification (walk vs. sit vs. empty). The model requires exactly the number of nodes it was trained on — retrain with `train_multinode_ml.py` if your node count changes.

**Why does ESP-NOW give only ~8 pkt/s on C6?**  
ESP32-C6 and C5 use an 802.11ax (HE) hardware CSI extraction path that only processes HT/VHT/HE frames. ESP-NOW sends legacy 802.11b/g management frames — the HE hardware discards them. UDP mode routes packets through the AP as HT/VHT data frames, which the hardware processes at full rate. Details: [TX mode and PHY compatibility](#tx-mode-and-phy-compatibility).

**Can I run nodes on battery power?**  
Yes, but WiFi + CSI capture is power-hungry (~100–150 mA at 3.3 V). Expect 6–12 hours from a 2000 mAh LiPo. Deep sleep is not compatible with continuous CSI capture. Lowering `traffic_generator_rate` reduces TX power draw but also the CSI rate and detection quality.

**Can I add my own ML features or sensors?**  
Yes. New statistical features go into `components/espectre/ml_features.h`. New sensor outputs are registered in `components/espectre/__init__.py` and exposed via the C++ sensor API. See [docs/ml_pipeline.md](docs/ml_pipeline.md) for the feature extraction pipeline.

---

## Related Projects

| Repository | Description |
|-----------|-------------|
| [francescopace/espectre](https://github.com/francescopace/espectre) | Upstream ESPectre — the original WiFi CSI ESPHome component this fork extends |
| [PeterkoCZ91/HLK-LD2412-POE-WiFi-CSI-security](https://github.com/PeterkoCZ91/HLK-LD2412-POE-WiFi-CSI-security) | Dual-sensor intrusion detection combining WiFi CSI with 24 GHz mmWave radar on a PoE ESP32 — alarm state machine, Telegram, dark-mode dashboard |

---

## License

GPLv3 — see [LICENSE](LICENSE). Original ESPectre by Francesco Pace; fork additions by Petr (details in [NOTICE](NOTICE)).
