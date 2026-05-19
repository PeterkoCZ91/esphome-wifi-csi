# Contributing

## Hardware requirements

- ESP32-S3 (ATOM S3 Lite, M5Stack) — 2.4 GHz self-sensing
- ESP32-C6 (DFRobot DFR1075) — WiFi 6 2.4 GHz self-sensing
- ESP32-C5 (DFRobot FireBeetle 2 DFR1222) — 5 GHz pairwise sensing via ESP-NOW
- MQTT broker (e.g. Mosquitto) on your local network

## Getting started

1. Clone the repo
2. Copy `secrets.yaml.example` → `secrets.yaml` and fill in your values
3. Install ESPHome: `pip install esphome`
4. Flash a node:
   ```bash
   esphome run espectre-c5-template.yaml
   ```
   For ESP32-C5 (requires manual BOOT+RST into bootloader):
   ```bash
   esphome run espectre-c5-template.yaml --device /dev/ttyACM0 --no-logs
   ```

## Adding a new node

1. Copy the appropriate template (`espectre-c5-template.yaml` or `espectre-c6-template.yaml`)
2. Change `name:` and `friendly_name:` in the `esphome:` block
3. For pairwise C5 RX nodes, set `peer_mac:` to the TX node's MAC:
   ```yaml
   espectre:
     peer_mac: "AA:BB:CC:DD:EE:FF"
   ```
4. Add the node name to `NODES` in `csi_logger.py`

## Data collection

Start the always-on logger (Docker):
```bash
docker compose -f docker-compose.tools.yml up -d csi-logger-session
```

Label a session:
```bash
python3 lab_session.py start --room living_room --activity walk \
  --nodes csi_node_1 csi_node_2 --note "1 person walking"
# ... collect data ...
python3 lab_session.py stop
```

Available activities: `walk`, `presence`, `sit`, `empty`, `fall`, `idle`

## Training the ML model

```bash
# Single-node
python3 train_ml_model.py

# Multi-node (pairwise C5 mesh)
python3 train_multinode_ml.py
```

Weights are baked into `components/espectre/ml_weights.h` — copy the output there.

## Running the mesh monitor

```bash
python3 mesh_monitor.py --nodes csi_node_1 csi_node_2 csi_node_3 csi_node_4
```

## Environment variables (Python tools)

| Variable | Default | Description |
|---|---|---|
| `MQTT_BROKER` | `localhost` | MQTT broker IP |
| `MQTT_USER` | *(empty)* | MQTT username |
| `MQTT_PASS` | *(empty)* | MQTT password |

## Pull requests

- Keep firmware changes (`components/espectre/`) separate from tooling changes
- For ESP32-C5 changes: test with at least one TX + one RX node
- For ML changes: include CSI-Bench validation score (`tools/validate_csibench.py`)
- No credentials, IPs, or MAC addresses in committed files — use `!secret` or env vars
