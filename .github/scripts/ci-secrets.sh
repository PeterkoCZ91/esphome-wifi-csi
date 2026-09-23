#!/usr/bin/env bash
# Create secrets.yaml (root + examples/) from the example with a valid random API key.
set -euo pipefail
api_key=$(python3 -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())")
for dest in secrets.yaml examples/secrets.yaml; do
  sed "s|your_esphome_api_key_base64==|${api_key}|" secrets.yaml.example > "$dest"
done
