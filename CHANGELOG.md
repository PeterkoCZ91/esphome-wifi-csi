# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Added

- **ESP32-C3 example** — `examples/minimal_self_sensing_c3.yaml` (board + `variant: ESP32C3`), compiled in CI ([#1](https://github.com/PeterkoCZ91/esphome-wifi-csi/issues/1))
- **`band_mode` option** (ESP32-C5 only): `2.4ghz` (default, previous behavior), `5ghz`, `auto`. 5 GHz is experimental and not yet validated on hardware; other chips reject non-2.4 GHz values at config time
- README configuration reference with all `espectre:` options and defaults

### Changed

- `peer_macs` limited to 5 entries (1 primary + 4 extra) at config time — extra entries used to be dropped silently
- `traffic_generator_udp_host` validated as an IPv4 address (hostnames were never resolved)
- Examples now include `api:` (encrypted), `ota:` and `logger:`, so OTA updates work after the first USB flash
- Templates: OTA uses `encryption` instead of the deprecated password; fallback AP password moved to `!secret fallback_ap_password`; `mqtt: discovery: false` because `api:` already feeds Home Assistant (avoids duplicate entities); duplicate "CSI Breathing Score" and "Phase Turbulence" entities removed ("CSI Phase Turbulence" stays — the Python tools subscribe to it)
- `secrets.yaml.example`: `ota_password` removed, `fallback_ap_password` added
- Docs corrected to match the code: ML feature sets and architectures, sensor list, defaults, Python ≥ 3.12, 5 GHz status; on-device ML documented as experimental (trained on 2 s-sampled data, inferred on a per-packet window)

### Fixed

- Build with ESPHome ≥ 2026.8 — C5/C6 templates used `IPAddress::str()`, which was removed; now use `str_to()`
- `LICENSE` now contains the full GPLv3 text; copyright notices moved to `NOTICE`
- Out-of-scope stack buffer used for remapped short (114-byte) HT20 CSI frames on ESP32-C5 (`-Wdangling-pointer`)
- Phase turbulence: inter-subcarrier phase differences are now wrapped to [-π, π]; previously 2π jumps inflated the value (affects `phase_turbulence_sensor`, presence logic and ML feature 12)
- Breathing-aware presence hold no longer re-arms immediately after its 300-interval safety timeout; it re-arms only after the signals drop or real motion occurs
- All compiler warnings in the component (`-Wreorder`, `-Wformat` for `uint32_t`, zero-length log formats on classic ESP32, unused function)
- **Thread safety:** all ESPHome API calls (sensor/switch/number publish, calibration start, threshold updates, BLE notify) now run in the main loop; the CSI/WiFi task, WiFi event handlers and the calibration task only hand state over via atomics. Runtime low-pass changes from the templates go through `request_lowpass_cutoff()`
- Failing CSI enable or traffic generator start after WiFi connect is logged and retried every 5 s instead of aborting (`ESP_ERROR_CHECK`)
- Traffic generator tasks (DNS/UDP/ESP-NOW) shut down cooperatively; no more `vTaskDelete` of a task that may be inside `esp_now_send()` or polling a freed task handle; ping interval clamped to ≥ 1 ms and the effective rate is logged
- Breathing bandpass coefficients follow the measured packet rate (were fixed for 100 pkt/s); retuned when the rate changes by > 10 %
- Breathing presence uses an idle-gated breathing baseline instead of `amplitude_sum × 0.01`: seeded after a 30 s warm-up, follows drops quickly, rises slowly and freezes while breathing is elevated, so a still person is not absorbed; re-seeded after recalibration
- `detection_algorithm: ml` never initialised the idle baselines, so breathing/phase presence never worked in ML mode
- CSI packet callback no longer cleared on disconnect while a WiFi-task callback may still be running

### Infrastructure

- `docs/hardware_testing.md` — log-based checklist for verifying a build on real nodes
- CI: ESPHome pinned via `requirements-ci.txt` (bumped by Dependabot), configs compiled as a parallel matrix with toolchain cache, Python lint job (`ruff --select E9,F`)
- CI: weekly `esphome-latest.yml` canary compiles against the newest ESPHome release
- Actions bumped to `checkout@v7`, `setup-python@v7`, `cache@v6`

---

## [1.0.0] — 2026-05-19

First public release. Based on upstream [ESPectre v2.7](https://github.com/francescopace/espectre).

### Added over upstream ESPectre v2.7

- **Breathing rate BPM sensor** — DFT-based estimation over 0.08–0.6 Hz bandpass with timestamp-corrected sample rate; works on single-node AP-link setups
- **UDP TX mode** — traffic generator sends standard UDP packets routed as HT/VHT frames by the AP; gives 206 pkt/s CSI on ESP32-C5/C6 vs ~8 pkt/s with ESP-NOW legacy PHY
- **Pairwise mesh without promiscuous mode (ESP32-C5)** — native STA receive path. *Correction:* this release announced a 5 GHz mesh on channel 52, but the component forced 2.4 GHz on ESP32-C5 (`WIFI_BAND_MODE_2G_ONLY`), so 5 GHz was not reachable; see `band_mode` under Unreleased
- **Multi-node peer_macs** — single RX node can receive CSI from up to 5 TX nodes (`peer_macs`: 1 primary + 4 extra)
- **Multi-node ML pipeline** — 45-feature MLP 45→32→16→1 (15 per-node features × 3 RX nodes: amplitude statistics plus movement score, phase turbulence, breathing, presence, DSER, PLCR), per-room training via labeled Docker sessions, cross-validated F1 = 0.833
- **DSER / PLCR metrics** — Dynamic-to-Static Energy Ratio and Path-Length Change Rate proxy from Uni-Fi research
- **Phase turbulence sensor** — standard deviation of inter-subcarrier phase differences
- **Ratio turbulence sensor** — SA-WiSense amplitude ratio metric
- **Hampel outlier filter** enabled by default (*correction:* the filter itself exists upstream)
- **Idle-gated baseline calibration** — improved over upstream basic implementation
- **Breathing-aware presence hold** — suppresses presence exit during confirmed breathing activity
- **ESP32-C5 / C6 hardware path** — WiFiLifecycleManager configures HT20 + STA receive path for 802.11ax nodes without promiscuous mode

### Changed from upstream

- Temporal smoothing (4/6 to enter MOTION, 5/6 to exit) is always on (*correction:* this release documented a `smoothing_enabled` option that does not exist)
- `hysteresis_factor` default 0.7 (*correction:* this release documented 1.0)
- `train_multinode_ml.py` default nodes updated to reflect 3-node RX mesh (`c5b`, `c5c`, `c5d`)

### Fixed

- ESP-NOW TX on ESP32-C6/S3 falling into DNS fallback mode (stale build cache issue)
- BPM estimation now uses actual packet timestamps instead of assumed fixed sample rate
- `espectre-c6-template.yaml` node name validation (uppercase caused ESPHome reject)

### Infrastructure

- CI: ESPHome compile check for both templates and all 3 examples on every push
- Docker: `csi-logger-session`, `ml-inference` compose services
- `secrets.yaml.example` — complete template with all required keys
