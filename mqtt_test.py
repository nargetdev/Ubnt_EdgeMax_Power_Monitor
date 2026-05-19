#!/usr/bin/env python3
"""Subscribe to the PoE MQTT topics and pretty-print every message.

Useful for verifying that ``poe_status_to_mqtt.py`` is publishing and that
``poe_control_mqtt.py`` is receiving. Read-only by design: this tool never
publishes (so it can't accidentally toggle a port).
"""
from __future__ import annotations

import argparse
import logging
import sys

import paho.mqtt.client as mqtt

from config import load_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


def on_connect(client, userdata, flags, rc):
    if rc != 0:
        logger.error(f"Failed to connect to MQTT broker, rc={rc}")
        return
    topic = userdata["topic"]
    logger.info(f"Connected. Subscribing to {topic!r}")
    client.subscribe(topic)


def on_message(_client, _userdata, msg):
    payload = msg.payload.decode(errors="replace")
    logger.info(f"{msg.topic} : {payload}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--topic",
        default="ubnt24/poe/#",
        help="Topic filter to subscribe to (default: ubnt24/poe/#).",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except (FileNotFoundError, RuntimeError) as e:
        logger.error(str(e))
        return 2

    broker = config["mqtt"]["broker"]
    port = config["mqtt"]["port"]
    logger.info(f"Connecting to MQTT broker at {broker}:{port}...")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, userdata={"topic": args.topic})
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(broker, port)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
