# Mesh Lab Test Protocol

## Naming

Use lab names only:

- `mesh_lab_esp32_a`
- `mesh_lab_esp32_b`
- `mesh_lab_esp32_c`

MQTT topic prefix format:

- `esphome/mesh_lab/esp32_a`
- `esphome/mesh_lab/esp32_b`
- `esphome/mesh_lab/esp32_c`

Do not add these names to production `csi_logger.py` until explicitly promoted.

## Baseline Run

For every board and placement:

1. Record board name, MAC, IP, chip, antenna, and physical label.
2. Record room, height, orientation, distance to AP, and wall/door state.
3. Wait at least 2 minutes after boot/calibration before judging data.
4. Record:
   - RSSI
   - CSI packets total increase
   - movement score min/max
   - DSER/PLCR if available
   - visible false positives
   - visible false negatives

## Event Script

Minimum manual test sequence:

1. Empty/quiet: 5 minutes.
2. Walk across expected link/path 10 times.
3. Stand still in the path: 3 minutes.
4. Leave the area: 5 minutes.
5. Repeat with one changed placement variable only.

## Pairwise Run

For TX/RX tests, record:

- TX node name/MAC.
- RX node name/MAC.
- distance and line-of-sight state.
- TX rate.
- accepted packet rate on RX.
- ignored packet rate if peer filtering exists.
- RSSI per peer.
- movement score range.
- whether motion is stronger than AP-link baseline.

## Promotion Rule

Pairwise data can move toward the main project only if:

- it is repeatable across at least two sessions;
- packet rate is stable;
- link labels are explicit;
- the logger can distinguish router-link vs pairwise-link data;
- the production system can be left untouched during failures.
