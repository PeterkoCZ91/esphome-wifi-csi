# Future ESP-IDF Pairwise Firmware

This directory is reserved for true ESP-to-ESP CSI experiments if ESPHome is too
limited for peer filtering or controlled packet scheduling.

Candidate upstream references:

- `espressif/esp-csi` examples: `csi_send`, `csi_recv`, `csi_recv_router`
- `StevenMHernandez/ESP32-CSI-Tool`: active AP/STA/passive layouts

Expected first roles:

- `mesh_tx`: sends controlled UDP/broadcast/unicast traffic at a known rate.
- `mesh_rx`: enables CSI, filters/labels frames by peer MAC, publishes reduced metrics.

Keep this code separate until the proof of concept is repeatable.
