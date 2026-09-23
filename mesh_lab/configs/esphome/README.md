# ESPHome Mesh Lab Configs

These configs live under `mesh_lab/` and are experimental. The `mesh-lab-*.yaml` files are **local-only** — they are gitignored and not published in this repository. Use the commands below with your own configs, or start from [`examples/`](../../../examples/).

Before compiling from this directory, provide secrets:

```bash
ln -sfn ../../../secrets.yaml mesh_lab/configs/esphome/secrets.yaml
```

Compile example:

```bash
.venv/bin/esphome compile mesh_lab/configs/esphome/mesh-lab-esp32-a.yaml
```

Flash first USB board:

```bash
.venv/bin/esphome upload mesh_lab/configs/esphome/mesh-lab-esp32-a.yaml --device /dev/ttyUSB0
```

This first config is not true ESP-WIFI-MESH. It is a safe AP-connected
2.4 GHz baseline node with lab-only names and MQTT topics. True pairwise
firmware work starts after baseline data and at least two boards.
