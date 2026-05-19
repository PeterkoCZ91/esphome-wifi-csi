# Hardware Guide

## Supported hardware

| Module | Chip | Band | Role | Notes |
|--------|------|------|------|-------|
| ESP32-CAM | ESP32-D0WD | 2.4 GHz | TX or RX | No external antenna needed for pairwise |
| M5Stack ATOM S3 Lite | ESP32-S3 | 2.4 GHz | TX or RX | Compact form factor, no PSRAM needed |
| ESP32-D0WD-V3 (DevKit) | ESP32-D0WD-V3 | 2.4 GHz | TX or RX | Classic ESP32, TX+RX limitation applies |
| DFRobot FireBeetle ESP32-C5 (DFR1222) | ESP32-C5 | 2.4 / 5 GHz | TX or RX | Preferred for 5 GHz and dual TX+RX |
| DFRobot FireBeetle ESP32-C6 (DFR1075) | ESP32-C6 | 2.4 GHz | TX or RX | WiFi 6 (802.11ax), full TX+RX support |

## Choosing hardware for your deployment

### For best pairwise sensing performance: ESP32-C5 or ESP32-C6

Both chips have a dedicated hardware channel estimation path that runs independently of the MAC TX scheduler. This means:

- You can configure a node as both TX (200 pkt/s ESP-NOW) and RX simultaneously
- CSI capture rate remains at the full TX rate (200 CSI packets/second received)
- Enables bidirectional links in the mesh (every node can be both TX and RX)

Use ESP32-C5 for 5 GHz operation (channel 52, 5.26 GHz, O2/AVM Fritz!Box compatible).
Use ESP32-C6 for 2.4 GHz WiFi 6 environments.

### For budget 2.4 GHz deployments: classic ESP32 (ATOM S3, DevKit, ESP32-CAM)

Classic ESP32 works well as **dedicated TX** or **dedicated RX** nodes. Avoid configuring the same node as both TX and RX — the MAC scheduler starvation reduces CSI throughput to ~10 pkt/s (from 200).

Recommended 2.4 GHz topology with classic ESP32:
```
One dedicated TX node (200 pkt/s, no peer_mac)
  └──→ 2–3 dedicated RX nodes (peer_mac = TX MAC)
```

If you need a triangle where one RX node also serves as TX for another RX node:
- The TX+RX node will have degraded sensing (~10 CSI pkt/s from the first TX)
- It will still TX at full 200 pkt/s for downstream RX nodes
- Plan accordingly: the TX+RX link will be noisier than dedicated links

## ESPHome YAML configuration

### Minimal RX node (single TX source)

```yaml
espectre:
  peer_mac: "AA:BB:CC:DD:EE:FF"   # TX node MAC address
```

### Multi-source RX node (receives from 2+ TX nodes)

```yaml
espectre:
  peer_macs:
    - "AA:BB:CC:DD:EE:FF"   # First TX node MAC
    - "11:22:33:44:55:66"   # Second TX node MAC
```

Maximum 4 entries in `peer_macs` (hardware limit: `MAX_EXTRA_PEER_MACS = 4`).

### TX-only node (broadcasts ESP-NOW, no peer filtering)

```yaml
espectre:
  traffic_generator_mode: espnow
  traffic_generator_rate: 200
  # No peer_mac — listens to AP-link CSI instead
```

### Combined TX+RX node (only recommended for C5/C6)

```yaml
espectre:
  traffic_generator_mode: espnow
  traffic_generator_rate: 200
  peer_mac: "AA:BB:CC:DD:EE:FF"
```

## Finding MAC addresses

Each node publishes its MAC on the MQTT debug topic at boot:

```bash
mosquitto_sub -h YOUR_BROKER -u USER -P PASS \
  -t 'esphome/YOUR_NODE_NAME/debug' -C 5
```

Or from ESPHome logs:

```
[I][wifi:489]: WiFi STA connected to 'YourSSID'
[I][wifi:490]: IPv4: 192.168.1.100
[I][wifi:491]: MAC: AA:BB:CC:DD:EE:FF
```

## Antenna notes

- **Internal PCB antenna** (all modules above): sufficient for pairwise sensing at ≤10 m indoor
- **External antenna (U.FL/IPEX)**: increases range but also increases sensitivity to AP-link CSI from other rooms — avoid on RX nodes if you want clean pairwise-only data
- For **TX-only nodes** with no peer_mac, external antenna makes no difference to pairwise quality (it only transmits)

## Flashing

Standard ESPHome OTA (after first flash):
```bash
.venv/bin/esphome upload espectre-YOUR-NODE.yaml --device 192.168.x.y
```

ESP32-C5 serial flash (requires holding BOOT during RST):
```bash
.venv/bin/esphome upload espectre-your-c5.yaml --device /dev/ttyACM0 --no-logs
```

ESP32-C5 is picky about the BOOT timing — if the flash fails, hold BOOT, press RST, release RST, then release BOOT.
