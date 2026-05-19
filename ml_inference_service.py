#!/usr/bin/env python3
"""
ML Inference Service — real-time CSI occupancy detection via MQTT.

Subscribes to ESPectre MQTT topics, builds 2s feature buckets from N nodes,
runs multinode_mlp.pkl inference, publishes results back to MQTT.

Compatible with any room/node set — configured via YAML or env vars.
Can be consumed by fusion_service as a regular MQTT binary/numeric sensor.

Usage:
  python3 ml_inference_service.py                          # obyvak defaults
  python3 ml_inference_service.py --room chodba --nodes csi_obyvak_c5b csi_obyvak_c5d
  MQTT_BROKER=192.168.x.y python3 ml_inference_service.py
"""
import argparse
import math
import os
import sys
import time
import threading

import numpy as np
import joblib
import paho.mqtt.client as mqtt

BROKER      = os.environ.get("MQTT_BROKER", "localhost")
MQTT_USER   = os.environ.get("MQTT_USER", "")
MQTT_PASS   = os.environ.get("MQTT_PASS", "")
BUCKET_S    = 2.0          # feature window (matches training)
PUBLISH_HZ  = 0.5         # once per 2s
STALE_S     = 6.0         # discard node state older than this
MIN_NODES   = 2           # minimum nodes needed for inference

DEFAULT_NODES = ["csi_obyvak_c5b", "csi_obyvak_c5c", "csi_obyvak_c5d"]
DEFAULT_ROOM  = "obyvak"

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


def _safe(v, default=0.0):
    try:
        f = float(v)
        return default if not math.isfinite(f) else f
    except Exception:
        return default


def extract_features_from_state(st: dict) -> list | None:
    """Build 15-element feature vector from latest node MQTT state."""
    amps_raw = st.get("amplitudes")
    if not amps_raw:
        return None
    try:
        sc = np.array([float(x) for x in amps_raw.split(",") if x.strip()], dtype=np.float32)
    except Exception:
        return None
    if len(sc) < 3:
        return None

    mean  = float(np.mean(sc))
    std   = float(np.std(sc))
    mx    = float(np.max(sc))
    mn    = float(np.min(sc))
    rng   = mx - mn
    cv    = std / mean if mean > 1e-6 else 0.0
    skew  = float(np.mean(((sc - mean) / std) ** 3)) if std > 1e-8 else 0.0
    kurt  = float(np.mean(((sc - mean) / std) ** 4)) - 3.0 if std > 1e-8 else 0.0
    counts, _ = np.histogram(sc, bins=8)
    counts = counts.astype(float)
    total  = counts.sum()
    if total > 0:
        probs   = counts[counts > 0] / total
        entropy = float(-np.sum(probs * np.log2(probs + 1e-12)) / np.log2(8))
    else:
        entropy = 0.0

    return [
        mean, std, mx, mn, rng,
        _safe(st.get("movement_score")),
        _safe(st.get("phase_turbulence")),
        _safe(st.get("breathing_score")),
        _safe(st.get("presence")),
        _safe(st.get("dser")),
        _safe(st.get("plcr")),
        cv, skew, kurt, entropy,
    ]


class MLInferenceService:
    def __init__(self, room: str, nodes: list[str], model_path: str, scaler_path: str):
        self.room   = room
        self.nodes  = nodes
        self.model  = joblib.load(model_path)
        self.scaler = joblib.load(scaler_path)

        # Per-node latest state from MQTT
        self.state: dict[str, dict] = {n: {} for n in nodes}
        self.state_ts: dict[str, float] = {n: 0.0 for n in nodes}
        self._lock = threading.Lock()

        # MQTT output topics
        self._pub_prob   = f"esphome/ml_{room}/sensor/occupancy_probability/state"
        self._pub_binary = f"esphome/ml_{room}/binary_sensor/occupied/state"
        self._pub_status = f"esphome/ml_{room}/availability"

        self._client: mqtt.Client | None = None
        self._last_result: str = "empty"
        self._last_prob: float = 0.0

    def _subscribe(self, client: mqtt.Client):
        for node in self.nodes:
            base = f"esphome/{node}"
            client.subscribe(f"{base}/sensor/movement_score/state")
            client.subscribe(f"{base}/sensor/csi_phase_turbulence/state")
            client.subscribe(f"{base}/sensor/breathing_score/state")
            client.subscribe(f"{base}/binary_sensor/presence_detected/state")
            client.subscribe(f"{base}/sensor/dser/state")
            client.subscribe(f"{base}/sensor/plcr/state")
            client.subscribe(f"{base}/sensor/csi_subcarrier_amplitudes/state")
        print(f"  Subscribed to {len(self.nodes)} nodes: {self.nodes}")

    def _on_connect(self, client, ud, flags, rc):
        if rc == 0:
            print(f"MQTT connected to {BROKER}")
            client.publish(self._pub_status, "online", retain=True)
            self._subscribe(client)
        else:
            print(f"MQTT connect failed rc={rc}")

    def _on_message(self, client, ud, msg):
        now = time.time()
        parts = msg.topic.split("/")
        if len(parts) < 3:
            return
        node = parts[1]
        if node not in self.state:
            return

        val = msg.payload.decode(errors="replace").strip()
        sub = "/".join(parts[2:])

        with self._lock:
            st = self.state[node]
            self.state_ts[node] = now

            if "movement_score" in sub:
                st["movement_score"] = val
            elif "csi_phase_turbulence" in sub:
                st["phase_turbulence"] = val
            elif "breathing_score" in sub:
                st["breathing_score"] = val
            elif "presence_detected" in sub:
                st["presence"] = "1" if val.lower() in ("on", "true", "1") else "0"
            elif "dser" in sub:
                st["dser"] = val
            elif "plcr" in sub:
                st["plcr"] = val
            elif "csi_subcarrier_amplitudes" in sub:
                st["amplitudes"] = val

    def _infer(self) -> tuple[float, str]:
        """Build feature vector from current node states, run inference."""
        now = time.time()
        with self._lock:
            node_feats = {}
            for node in self.nodes:
                if now - self.state_ts[node] > STALE_S:
                    continue
                feats = extract_features_from_state(self.state[node])
                if feats is not None:
                    node_feats[node] = feats

        if len(node_feats) < MIN_NODES:
            return self._last_prob, self._last_result  # hold last value

        mean_feat = [
            sum(node_feats[n][i] for n in node_feats) / len(node_feats)
            for i in range(15)
        ]
        combined = []
        for node in self.nodes:
            combined.extend(node_feats.get(node, mean_feat))

        X = self.scaler.transform([combined])
        prob = float(self.model.predict_proba(X)[0, 1])
        label = "OCCUPIED" if prob >= 0.5 else "empty"
        return prob, label

    def _publish_loop(self, client: mqtt.Client):
        while True:
            time.sleep(BUCKET_S)
            prob, label = self._infer()
            self._last_prob   = prob
            self._last_result = label
            client.publish(self._pub_prob,   f"{prob:.3f}", retain=False)
            client.publish(self._pub_binary, "ON" if label == "OCCUPIED" else "OFF", retain=False)
            ts = time.strftime("%H:%M:%S")
            bar = "#" * int(prob * 20)
            print(f"\r{ts}  {label:10s}  {bar:<20s}  {prob*100:.0f}%    ", end="", flush=True)

    def run(self):
        client = mqtt.Client()
        self._client = client
        if MQTT_USER:
            client.username_pw_set(MQTT_USER, MQTT_PASS)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.will_set(self._pub_status, "offline", retain=True)

        client.connect(BROKER, 1883, keepalive=60)
        client.loop_start()

        # Wait for connection
        time.sleep(1.5)

        pub_thread = threading.Thread(target=self._publish_loop, args=(client,), daemon=True)
        pub_thread.start()

        print(f"\nML Inference Service — room={self.room}, nodes={self.nodes}")
        print(f"  Output: {self._pub_prob}")
        print(f"  Output: {self._pub_binary}")
        print("  Ctrl+C pro ukončení\n")

        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nUkončeno.")
        finally:
            client.publish(self._pub_status, "offline", retain=True)
            client.loop_stop()
            client.disconnect()


def main():
    p = argparse.ArgumentParser(description="ML Inference Service — CSI occupancy via MQTT")
    p.add_argument("--room",   default=DEFAULT_ROOM,  help="Room name (default: obyvak)")
    p.add_argument("--nodes",  nargs="+", default=DEFAULT_NODES, metavar="NODE",
                   help="MQTT node names (default: csi_obyvak_c5 c5c c6)")
    p.add_argument("--model",  default=os.path.join(MODELS_DIR, "multinode_mlp.pkl"))
    p.add_argument("--scaler", default=os.path.join(MODELS_DIR, "multinode_scaler.pkl"))
    args = p.parse_args()

    for path in (args.model, args.scaler):
        if not os.path.exists(path):
            print(f"ERROR: model nenalezen: {path}", file=sys.stderr)
            sys.exit(1)

    svc = MLInferenceService(args.room, args.nodes, args.model, args.scaler)
    svc.run()


if __name__ == "__main__":
    main()
