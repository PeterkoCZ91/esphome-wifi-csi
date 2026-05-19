# ESPectre Fork — WiFi CSI Presence & Breathing Detection

[![CI](https://github.com/PeterkoCZ91/esphome-wifi-csi/actions/workflows/ci.yml/badge.svg)](https://github.com/PeterkoCZ91/esphome-wifi-csi/actions/workflows/ci.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

> ESPHome external component for human presence detection using WiFi Channel State Information (CSI) on ESP32.  
> Fork of [ESPectre](https://github.com/francescopace/espectre) by Francesco Pace (GPLv3).  
> Based on upstream ESPectre v2.7 — extended with 5 GHz, breathing rate, multi-node ML, and UDP TX mode.

## Quick Start — Level 1 (5 minutes)

Get presence detection running on any ESP32 with your home WiFi router.

**Prerequisites:** Python 3.8+, any ESP32 dev board with USB, your WiFi credentials, an MQTT broker on your network.

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

Edit `secrets.yaml` (and `examples/secrets.yaml`) with your WiFi SSID/password and MQTT broker address:

```yaml
wifi_ssid: "MyNetwork"
wifi_password: "mypassword"
mqtt_broker: "192.168.x.y"
mqtt_username: ""
mqtt_password: ""
ota_password: "changeme"
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

**That's it for Level 1.** For pairwise sensing (Level 2) or 5 GHz (Level 4), see the sections below.

---

## What you can build

**Level 1 — One ESP32, one router**  
Plug any ESP32 near your WiFi router. Get presence detection, motion scoring, and real-time breathing rate — no extra hardware.

**Level 2 — Two ESP32s: pairwise sensing**  
Add a dedicated TX node. Now the sensing zone is the direct path between two nodes — no cross-room interference, cleaner data, better ML.

**Level 3 — Mesh: 3–4 nodes, spatial coverage**  
Multiple RX nodes around a room. 45-feature ML classifier distinguishes sitting, walking, and empty with cross-validated F1 = 0.833.

**Level 4 — 5 GHz (ESP32-C5)**  
First open-source 5 GHz pairwise CSI sensing implementation on consumer hardware. No promiscuous mode hacks — native 802.11ax hardware path.

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
| Hampel outlier filter | ✗ | ✓ |
| Idle-gated baseline calibration | basic | improved |
| ML pipeline (15 features) | ✗ | ✓ |
| Data collection tooling | ✗ | ✓ Docker + SQLite + labeled sessions |
| UDP TX mode (pairwise on C5/C6) | ✗ | ✓ HT/VHT frames → 206 pkt/s vs 8 with ESP-NOW |

**Breathing rate** is the standout feature for single-node setups. The BPM estimate uses a DFT over the 0.08–0.6 Hz bandpass with timestamp-based sample rate compensation — more accurate than fixed-rate approaches used in most published CSI projects.

---

## Level 2 — Pairwise sensing (2 nodes)

Add a second ESP32 as a dedicated TX node. It broadcasts ESP-NOW probe packets at 200 packets/second. The RX node captures CSI from those packets — not from the router.

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

Up to 4 entries in `peer_macs`. The original single `peer_mac` still works unchanged.

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

### ESP-NOW mode (default)

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

**RX node config** is unchanged — use `peer_mac` as before. The CSI hardware filter matches by source MAC regardless of frame type.

### Which mode to use

| RX node chip | TX mode | CSI pkt/s |
|---|---|---|
| Classic ESP32 (D0WD, S3) | `espnow` | ~100–200 |
| Classic ESP32 (D0WD, S3) | `udp` | ~100–200 |
| ESP32-C5 / C6 (HE) | `espnow` | ~8 ❌ |
| ESP32-C5 / C6 (HE) | `udp` | ~200 ✅ |

ESP-NOW still works well for C5↔C5 links (both ends are HE and exchange native 802.11ax frames). For any link where the **RX node is a C5 or C6**, use `udp` mode on the TX side.

---

## Level 4 — 5 GHz pairwise mesh (ESP32-C5)

**First open-source 5 GHz CSI sensing on consumer hardware.**

ESP32-C5 (RISC-V, 802.11ax) operates on the 5 GHz band. This fork implements a 4-node star mesh on channel 52 (5.26 GHz):

```
C5a (TX, 200 pkt/s, ch52)
    ├──→ C5b (RX)
    ├──→ C5c (RX)
    └──→ C5d (RX)
```

**Key implementation detail:** ESP32-C5 must NOT use promiscuous mode for CSI capture on 5 GHz. Enabling promiscuous mode causes channel contention → the AP drops all STA clients. This fork uses the native STA receive path with hardware CSI extraction — no promiscuous mode required.

### 5 GHz vs 2.4 GHz for CSI sensing

| | 2.4 GHz | 5 GHz (C5) |
|--|---------|-----------|
| Wall penetration | Higher | Lower — better room isolation |
| Multipath richness | Higher | Lower — cleaner signal |
| Interference | More crowded | Less crowded |
| Hardware cost | Lower | ESP32-C5 ~€8 |
| Simultaneous TX+RX | C6 only | C5 native |

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

# 3a. Single-node model (1 RX node)
python3 train_ml_model.py --nodes csi_your_node

# 3b. Multi-node fusion model (3 RX nodes, better accuracy)
python3 train_multinode_ml.py \
  --nodes csi_node_1 csi_node_2 csi_node_3

# 4. Deploy inference service
docker compose -f docker-compose.tools.yml up -d ml-inference
```

### Features (15 per node)

11 statistical features from the turbulence buffer (mean, std, max, min, p25, p75, skew, kurtosis, IQR, range, trend) + phase turbulence + ratio turbulence + breathing score + DSER.

| Setup | Input | Architecture | F1 |
|---|---|---|---|
| Single-node | 15 features | MLP 15→16→8→1 | depends on environment |
| Multi-node (3× RX) | 45 features | MLP 45→16→8→1 | **0.833** (walk/sit vs empty) |

See [docs/ml_pipeline.md](docs/ml_pipeline.md) for full details.

---

## Sensors published via MQTT

| Sensor | Unit | Description |
|--------|------|-------------|
| `movement_score` | — | Composite motion metric |
| `breathing_score` | — | Bandpass amplitude energy (0.08–0.6 Hz) |
| `breathing_rate` | BPM | Real-time breathing rate (DFT) |
| `phase_turbulence` | — | Std of inter-subcarrier phase diffs |
| `ratio_turbulence` | — | SA-WiSense amplitude ratio |
| `dser` | — | Dynamic-to-Static Energy Ratio (Uni-Fi) |
| `plcr` | — | Path-Length Change Rate proxy (Uni-Fi) |
| `presence_detected` | ON/OFF | Presence with hysteresis |
| `motion_detected` | ON/OFF | Motion binary |
| `packet_rate` | pkt/s | CSI health metric |
| `rssi` | dBm | Signal strength |

---

## Hardware

### Supported modules

| Module | Chip | Band | TX+RX simultaneously |
|--------|------|------|----------------------|
| ATOM S3 Lite | ESP32-S3 | 2.4 GHz | ✗ (MAC starvation) |
| ESP32-CAM | ESP32-D0WD | 2.4 GHz | ✗ |
| ESP32 DevKit | ESP32-D0WD-V3 | 2.4 GHz | ✗ |
| FireBeetle ESP32-C6 (DFR1075) | ESP32-C6 | 2.4 GHz WiFi 6 | ✓ |
| FireBeetle ESP32-C5 (DFR1222) | ESP32-C5 | 2.4 / 5 GHz | ✓ |

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

### `packet_rate` = 0 or stays near zero

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `packet_rate` = 0 after boot | CSI not enabled — wrong board or framework | Verify `framework: type: esp-idf` in your YAML; Arduino framework is not supported |
| `packet_rate` ≈ 0 on C6 pairwise | TX node using `espnow` mode with C6 RX | Switch TX to `traffic_generator_mode: udp`; see [TX mode and PHY compatibility](#tx-mode-and-phy-compatibility) |
| `packet_rate` = 5–8 instead of ~100 | RSSI < −55 dBm, burst-mode delivery | Move node closer to TX or AP; RSSI > −50 dBm recommended for stable operation |
| `packet_rate` drops after a few minutes | AP channel change / roaming event | Set a fixed channel on your AP; the ESP reconnects but CSI resumes after ~10 s |

### Presence / motion sensors

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `presence_detected` always ON | Node near HVAC, fan, or vibrating surface | Relocate node; baseline resets after 30 s of true stillness |
| `presence_detected` never ON | `movement_score` below threshold | Check `movement_score` in MQTT — if it's consistently < 1.0, check `packet_rate` first |
| `movement_score` noisy (4–6 at rest) | Poor RSSI / multipath near metal objects | Increase distance from metal surfaces; use a directional antenna |
| `breathing_rate` = 0 | Not enough CSI packets | `packet_rate` must be > 10 pkt/s; breathing needs ~30 s warm-up |
| `breathing_rate` jumps 6–30 BPM | Person moving, not resting | BPM is valid only when `movement_score` < 1.5; filter on your MQTT consumer side |

### Flashing and OTA

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `esptool.py: error: argument --port` | Wrong port or device not detected | Run `ls /dev/ttyUSB*` or `ls /dev/ttyACM*` and pass `--device /dev/ttyUSBx` |
| Compilation fails: `espectre` not found | `external_components` path wrong | Path must be relative to the YAML file; for examples use `path: ../components` |
| Duplicate sensor error on compile | Template sensors conflict with native sensors | Remove `sensor: platform: template` blocks; use native sensor keys under `espectre:` instead (e.g. `movement_sensor:`) |
| OTA upload hangs | Node not reachable | Ping the node IP; check it's on the same VLAN as your machine |
| C5 serial flash fails | C5 requires GPIO 15 held LOW during boot | Hold BOOT button during power-on; release after `Connecting...` appears |

### MQTT

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| No messages on broker | Wrong `mqtt_broker` in secrets.yaml | Verify with `mosquitto_sub -h BROKER -t '#' -v` from your machine |
| Topics show `esphome/csi-presence/` not your name | `node_name` substitution not set | Add `substitutions: node_name: my-node` or edit `name:` in `esphome:` block |
| Auth error in logs | MQTT credentials wrong | Check `mqtt_username` / `mqtt_password` in secrets.yaml |

---

## FAQ

**Do I need a dedicated MQTT broker?**  
Yes. Mosquitto on a Raspberry Pi, Docker, or any home server works. The ESP nodes publish to MQTT; Home Assistant or a Python script subscribes. There is no direct ESPHome-to-HA integration in this component (by design — MQTT is broker-neutral).

**What WiFi router/AP do I need?**  
Any standard 802.11n/ac AP works for Level 1–3. For Level 4 (5 GHz C5 mesh), the AP must support 802.11ax on 5 GHz and must NOT have band steering that forces clients to 2.4 GHz. A dedicated IoT SSID on a fixed channel is strongly recommended.

**Does CSI sensing work through walls?**  
AP-link CSI (Level 1) sees through walls — this is a feature and a limitation. Pairwise sensing (Level 2+) is bounded to the direct TX↔RX path, so wall penetration depends on placement. 5 GHz (C5) has lower wall penetration, giving better room isolation.

**Why is my breathing rate unreliable?**  
BPM estimation requires: (1) `packet_rate` > 10 pkt/s, (2) person stationary — `movement_score` < 1.5, (3) at least 30 seconds of data. The DFT window is 30 s; values during the warm-up period are discarded. Sitting 1–2 m from the node gives the best results.

**What's the difference from upstream ESPectre v2.7?**  
See the [feature table above](#what-this-fork-adds-over-upstream-espectre). The main additions: breathing rate BPM sensor, UDP TX mode for C5/C6 nodes (206 pkt/s vs 8 with ESP-NOW), multi-node ML pipeline, DSER/PLCR Uni-Fi metrics, Hampel outlier filter, and 5 GHz support on ESP32-C5.

**Can I use this with Home Assistant?**  
Yes — via MQTT. Add an MQTT sensor in your `configuration.yaml` or use the MQTT integration auto-discovery. The nodes publish standard float/binary payloads on `esphome/<node_name>/<sensor_name>`.

**How many nodes do I need for good ML accuracy?**  
The included ML model is trained on 3 RX nodes (45 features). A single node gives presence/absence; 3+ nodes enable activity classification (walk vs. sit vs. empty). The model requires exactly the number of nodes it was trained on — retrain with `train_multinode_ml.py` if your node count changes.

**Why does ESP-NOW give only ~8 pkt/s on C6?**  
ESP32-C6 and C5 use an 802.11ax (HE) hardware CSI extraction path that only processes HT/VHT/HE frames. ESP-NOW sends legacy 802.11b/g management frames — the HE hardware discards them. UDP mode routes packets through the AP as HT/VHT data frames, which the hardware processes at full rate. Details: [TX mode and PHY compatibility](#tx-mode-and-phy-compatibility).

**Can I run nodes on battery power?**  
Yes, but WiFi + CSI capture is power-hungry (~100–150 mA at 3.3 V). Expect 6–12 hours from a 2000 mAh LiPo. Deep sleep is not compatible with continuous CSI capture. For battery use, configure a higher `update_interval` and accept reduced packet rate during sleep gaps.

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

GPLv3 — original ESPectre by Francesco Pace. Fork additions by Petr.
