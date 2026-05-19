#!/usr/bin/env python3
"""Quick ESPectre MQTT monitor — live view of ESPectre sensor values."""
import os
import paho.mqtt.client as mqtt
import time

BROKER = os.environ.get("MQTT_BROKER", "localhost")
TOPICS = [
    "esphome/csi_obyvak/#",
    "esphome/csi_chodba/#",
]

def on_connect(client, ud, flags, rc):
    print(f"Connected to {BROKER} (rc={rc})")
    for t in TOPICS:
        client.subscribe(t)
    print("Waiting for ESPectre data...\n")

def on_message(client, ud, msg):
    topic = msg.topic
    val = msg.payload.decode(errors="replace").strip()
    # Strip MQTT prefix for readability
    short = topic.replace("esphome/", "")
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {short:55s} = {val}")

c = mqtt.Client()
mqtt_user = os.environ.get("MQTT_USER", "")
mqtt_pass = os.environ.get("MQTT_PASS", "")
if mqtt_user:
    c.username_pw_set(mqtt_user, mqtt_pass)
c.on_connect = on_connect
c.on_message = on_message
c.connect(BROKER, 1883)
c.loop_forever()
