#!/usr/bin/env python3
"""MQTT-driven PoE control for Ubiquiti EdgeSwitch.

Subscribes to an MQTT topic of the form ``ubnt24/poe/<port>`` and toggles
the matching PoE interface (``poe opmode auto`` / ``poe opmode shutdown``)
via SSH on the switch.
"""
import logging
import os
import time

import paho.mqtt.client as mqtt
import paramiko

from config import load_config

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)


def _redact(cfg: dict) -> dict:
    """Return a copy of the config safe to log (passwords masked)."""
    redacted = {
        "mqtt": dict(cfg.get("mqtt", {})),
        "servers": [
            {**s, "password": "***"} for s in cfg.get("servers", [])
        ],
    }
    return redacted

def send_poe_command(host, user, password, interface, command):
    """Connects via SSH and sends PoE commands."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    logger.info(f"Connecting to {host}...")
    ssh.connect(host, username=user, password=password, look_for_keys=False)
    
    # Get an interactive shell
    shell = ssh.invoke_shell()
    
    # Wait for initial prompt
    shell.recv(1000).decode()
    
    # Send enable command and password
    shell.send('enable\n')
    time.sleep(0.1)
    shell.recv(1000).decode()
    
    shell.send(password + '\n')
    time.sleep(0.1)
    shell.recv(1000).decode()
    
    # Enter configuration mode
    shell.send('configure\n')
    time.sleep(0.1)
    shell.recv(1000).decode()
    
    # Enter interface configuration
    shell.send(f'interface {interface}\n')
    time.sleep(0.1)
    shell.recv(1000).decode()
    
    # Send the PoE command
    logger.info(f"Sending command: {command}")
    shell.send(command + '\n')
    time.sleep(0.3)
    output = shell.recv(1000).decode()
    logger.debug(f"Command output: {output}")
    
    # Exit configuration
    shell.send('exit\n')
    time.sleep(0.1)
    shell.send('exit\n')
    time.sleep(0.1)
    
    shell.close()
    ssh.close()

def on_connect(client, userdata, flags, rc):
    """Callback when connected to MQTT broker."""
    logger.debug(f"Connection callback - rc: {rc}, flags: {flags}")
    if rc == 0:
        sub_topic = userdata["mqtt"]["topic"]
        logger.info("Connected to MQTT broker successfully")
        logger.info(f"Subscribing to {sub_topic!r}...")
        client.subscribe(sub_topic)
        logger.info(f"Successfully subscribed to {sub_topic!r}")
        logger.info("Ready to receive PoE control commands")
    else:
        logger.error(f"Failed to connect to MQTT broker with result code {rc}")

def on_disconnect(client, userdata, rc):
    """Callback when disconnected from MQTT broker."""
    logger.debug(f"Disconnect callback - rc: {rc}")
    logger.warning(f"Disconnected from MQTT broker with result code {rc}")
    if rc != 0:
        logger.info("Unexpected disconnection. Will attempt to reconnect...")

def on_message(client, userdata, msg):
    """Callback when message is received."""
    try:
        topic = msg.topic
        command = msg.payload.decode()
        
        # Extract port number from topic (e.g., "ubnt24/poe/07" -> "0/7")
        port_match = topic.split('/')[-1]
        interface = f"0/{int(port_match):d}"
        
        logger.info(f"Received command {command} for interface {interface}")
        
        # Convert command to PoE operation mode
        if command.strip() == "0":
            poe_command = "poe opmode shutdown"
        elif command.strip() == "1":
            poe_command = "poe opmode auto"
        else:
            logger.error(f"Invalid command received: {command}")
            return
        
        # Send command to switch
        config = userdata  # Config passed as userdata
        send_poe_command(
            config["servers"][0]["host"],  # Assuming first server in config
            config["servers"][0]["user"],
            config["servers"][0]["password"],
            interface,
            poe_command
        )
        
    except Exception as e:
        logger.error(f"Error processing message: {e}")

def main():
    try:
        config = load_config()
    except (FileNotFoundError, RuntimeError) as e:
        logger.error(str(e))
        raise SystemExit(2)
    logger.info(f"Loaded config from {config.get('_config_path')}")
    logger.debug(f"Config: {_redact(config)}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, userdata=config)
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    client.reconnect_delay_set(min_delay=1, max_delay=120)

    max_retries = 5
    retry_delay = 5  # seconds

    broker = config["mqtt"]["broker"]
    port = config["mqtt"]["port"]
    logger.info(f"Starting MQTT client; broker={broker}:{port}")

    for attempt in range(max_retries):
        try:
            logger.info(
                f"Attempting to connect to MQTT broker at {broker}:{port} "
                f"(attempt {attempt + 1}/{max_retries})..."
            )
            client.connect(broker, port)
            logger.info("Connection attempt successful, starting network loop...")
            break
        except Exception as e:
            logger.error(f"Connection attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error("Max retries reached. Exiting...")
                return

    logger.info("Starting MQTT network loop...")
    client.loop_forever()

if __name__ == "__main__":
    main() 