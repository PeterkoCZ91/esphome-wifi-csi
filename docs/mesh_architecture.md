# Pairwise CSI Mesh Architecture

WiFi Channel State Information (CSI) captures the amplitude and phase of each OFDM subcarrier across the wireless channel. When a person enters the channel, their body reflects and scatters the signal — the CSI changes. This fork extends single-node self-sensing to **multi-node pairwise sensing**, where dedicated TX nodes send probe packets that RX nodes capture as CSI.

## Why pairwise sensing?

In standard (single-node) mode, the ESP32 captures CSI from its own AP-link beacons — the router's periodic transmissions bouncing off walls and people. This works, but has two limitations:

1. **No spatial diversity** — one node, one coverage zone
2. **AP-link pollution** — the AP-link path goes through many walls and picks up motion from adjacent rooms

Pairwise sensing uses short-range ESP-NOW packets between nodes you place deliberately. The CSI captured is only the path between those two nodes — a clean, controlled sensing zone.

## Mesh topologies

### 5 GHz star mesh (ESP32-C5, production)

```
C5a (TX, 200 pkt/s, ch52)
    ├──→ C5b (RX, peer_mac=C5a)
    ├──→ C5c (RX, peer_mac=C5a)
    └──→ C5d (RX, peer_mac=C5a)
```

- 3 RX nodes cover the room from different angles
- 45-feature ML input (15 features × 3 RX nodes)
- Cross-validated F1 = 0.833 on sit/walk/empty

### 2.4 GHz triangle mesh (ESP32-D0WD + ATOM S3, experimental)

```
A (TX, 200 pkt/s)
    ├──→ B (RX from A) → also TX 200 pkt/s
    │        └──→ C (RX from A + B, peer_macs=[A,B])
    └──→ C (RX from A + B)
```

3 active links from 3 nodes:

| Link | Status | Notes |
|------|--------|-------|
| A → B | ✓ working | Clean indoor sensing |
| A → C | ✓ working | Via `peer_macs` multi-TX support |
| B → C | ✓ working | Via `peer_macs` multi-TX support |
| B → A | ✗ impossible | Classic ESP32 TX+CSI-RX MAC starvation |

## ESP32 TX+CSI-RX hardware limitation

Classic ESP32 (Xtensa LX6, 2.4 GHz) shares one MAC scheduler between TX and CSI capture. When a node transmits at 200 pkt/s, the MAC allocates nearly all bandwidth to TX, starving the CSI-RX path. Result: **~5–15 CSI packets/second received instead of 200** — enough for detection, but the link is degraded.

This is why B→A does not work: both would be starved simultaneously.

### Why ESP32-C5 and ESP32-C6 handle this better

ESP32-C5 (RISC-V, 802.11ax / WiFi 6) has a **dedicated hardware channel estimation block** separate from the MAC TX scheduler:

- HE (High-Efficiency) preamble processing runs in parallel to TX
- The channel estimation path is not blocked by the MAC TX queue
- Result: full 200 CSI packets/second even while transmitting

ESP32-C6 (802.11ax, 2.4 GHz) has the same advantage. In practice, C5 and C6 nodes support both TX and RX simultaneously with no measurable performance loss.

## Multi-peer MAC (`peer_macs`)

To receive CSI from multiple TX nodes on a single RX node, use `peer_macs` instead of `peer_mac`:

```yaml
espectre:
  peer_macs:
    - "AA:BB:CC:DD:EE:FF"   # Node A TX
    - "AA:BB:CC:DD:EE:FE"   # Node B TX
```

This calls `CSIManager::add_extra_peer_mac()` for each additional MAC. The CSI callback filters packets by MAC against both the primary peer and up to 4 extra peers (`MAX_EXTRA_PEER_MACS = 4`).

Single `peer_mac` still works and remains the primary configuration for star-topology setups.

## AP-link CSI and through-wall detection

When a node has no `peer_mac`/`peer_macs` configured, it falls back to AP-link CSI (standard beacon-based sensing). This mode is sensitive to any motion in the RF path — including through multiple walls and across large distances.

**Observed:** Node A (TX-only, no peer_mac configured), placed in a neighboring apartment 8 m away through two closed doors, detects movement in the main apartment via AP-link CSI.

**Practical consequence for ML training:** Do not mix AP-link CSI data with pairwise CSI data in the same model. AP-link captures ambient room state; pairwise captures the controlled inter-node sensing zone. Use only pairwise nodes (B and C) for room-specific ML models.

## Sensing geometry recommendations

- **Coverage:** place RX nodes at different corners/angles relative to the TX node to maximize spatial diversity
- **Distance:** ESP-NOW pairwise sensing works at 2–10 m indoor. Walls attenuate but do not block CSI unless signal drops below -85 dBm
- **Line-of-sight preferred** but not required; NLOS sensing still captures diffraction patterns around human bodies
- **Antenna orientation:** internal PCB antenna is sufficient; external antenna increases AP-link pollution at the cost of pairwise link quality for RX nodes
