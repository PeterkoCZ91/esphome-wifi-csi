# Hardware testing checklist

CI only proves that every configuration compiles. Use this checklist to verify a build on real
nodes. Everything is checked from the device log, so set `logger: level: DEBUG` for the test
run and follow it with `esphome logs <node>.yaml`.

Record the ESPHome version, chip, traffic mode and approximate RSSI with your results.

## 1. Boot and calibration

Expected order after boot:

1. `WiFi band mode: …` (ESP32-C5 only — shows the configured `band_mode`)
2. `WiFi connected - starting CSI (main loop)`
3. `Gain locked: AGC=…, FFT=…` (or `Gain calibration complete …` / `Gain lock not supported on this platform`)
4. `Gain lock done - starting calibration (main loop)`
5. `Calibration result applied in main loop (success)`

In Home Assistant the **Calibrate** switch turns OFF again and **Threshold** shows the new value.
The progress-bar status line then updates about once per second with a stable `pkt/s`.

Fail if: a reboot, `Guru Meditation`, `abort()`, `assert failed` or a task watchdog appears.

## 2. WiFi loss and recovery

Reboot the access point, or block the node's MAC for ~10 s.

- On loss: `WiFi disconnected - stopping CSI (main loop)`, then the traffic task stop line for
  the mode in use (`DNS traffic task stopped`, `UDP traffic task stopped` or, at DEBUG,
  `ESP-NOW traffic task stopped` + `ESP-NOW deinitialized`), then `Traffic generator stopped`.
- On recovery: section 1 repeats **without a device reboot**.
- Must not appear: `Traffic task did not exit within …`, `Previous traffic task is still running …`.
- Repeat 5–10 times; free heap in `[resources]` lines must stay stable.

If CSI or the generator cannot start, the node logs `CSI enable failed: … - retrying in 5 s` or
`Failed to start traffic generator - retrying in 5 s` and keeps retrying instead of rebooting.

## 3. ESP-NOW TX node (`traffic_generator_mode: espnow`)

- Boot: `ESP-NOW pre-initialized before CSI enable`, `ESP-NOW traffic task started at N pps`.
- Run section 2 on the TX node; the RX node's packet rate must recover afterwards.

## 4. Breathing and presence

- ~2–5 s after calibration: one `Breathing filter retuned: 100.0 -> N Hz (packet rate)` near the
  real packet rate (skipped if the rate is already ~100 pkt/s). No repeated retunes while the
  rate is stable.
- ~30 s later: `Idle breathing baseline seeded: …`. Presence from breathing is not reported
  before this line, so keep the room empty during the first 30 s after boot or calibration.
- Empty room, 5 min: `presence_sensor` stays OFF, `breathing_sensor` stays near the seeded value.
- Sit still 1–2 m from the node for **15 min**: `presence_sensor` stays ON the whole time and
  `breathing_rate_sensor` shows a plausible BPM. (Before this fix presence dropped after ~7 min.)
- Leave the room: presence goes OFF within ~10–20 s.
- `Presence hold: …` / `Presence hold released …` lines appear when a still person is held as
  motion; after `Presence hold auto-released after 300 intervals` the hold must not re-arm until
  the signals drop or you move.

## 5. Runtime controls

- MQTT `<topic_prefix>/filter/lowpass_cutoff/command` with payload `9` → `Low-pass filter enabled (cutoff=9.0 Hz)`, no crash.
- Toggle **Calibrate** in Home Assistant → section 1 steps 4–5 repeat.
- Ping mode with `traffic_generator_rate: 300` → `Ping interval is whole milliseconds: effective rate ~333 pps …`.

## 6. Long run

Leave the node running for several hours (ideally overnight) and check that there are no
resets (uptime keeps growing) and no `Guru Meditation` / `assert failed` lines.
