#!/usr/bin/env python3
"""SSH into the EdgeSwitch, query PoE status, publish JSON over MQTT.

Designed to be invoked on a schedule (cron / balena pulse / systemd timer).
"""
import json
import time

import paho.mqtt.client as mqtt
import paramiko

from config import load_config

def get_command_output(host, user, password, command):
    """Connects via SSH, executes a command, and returns the output."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    print(f"Connecting to {host}...")
    ssh.connect(host, username=user, password=password, look_for_keys=False)
    
    # Get an interactive shell
    shell = ssh.invoke_shell()
    
    # Wait for initial prompt
    output = shell.recv(1000).decode()
    print("Initial prompt:", output)
    
    # Send enable command
    print("Sending 'enable' command...")
    shell.send('enable\n')
    time.sleep(0.1)  # Wait for password prompt
    output = shell.recv(1000).decode()
    print("After enable command:", output)
    
    # Send enable password
    print("Sending enable password...")
    shell.send(password + '\n')
    time.sleep(0.1)  # Wait for command to process
    output = shell.recv(1000).decode()
    print("After enable password:", output)
    
    # Send the actual command
    print(f"Sending command: {command}")
    shell.send(command + '\n')
    time.sleep(0.3)  # Wait for command to complete
    
    # Get the command output
    output = shell.recv(4096).decode()
    print("Command output:", output)
    
    shell.close()
    ssh.close()
    return output

FIELD_NAMES = [
    'intf', 'detection', 'class', 'consumed',
    'voltage', 'current', 'meter', 'temp',
]


def _column_ranges_from_separator(sep_line):
    """Return [(start, end_exclusive), ...] from a dashed separator line.

    The EdgeSwitch CLI prints a separator like:
        --------- -------------- ------- ----------- ...
    Each dash-run defines one column's character span.
    """
    ranges = []
    start = None
    for i, ch in enumerate(sep_line):
        if ch == '-' and start is None:
            start = i
        elif ch != '-' and start is not None:
            ranges.append((start, i))
            start = None
    if start is not None:
        ranges.append((start, len(sep_line)))
    return ranges


def parse_poe_output(output):
    """Parse `show poe status ...` output by column-width.

    EdgeSwitch fields can contain spaces ("Open Circuit"), so whitespace
    splitting corrupts the column mapping. We instead anchor on the dashed
    separator line that the CLI emits beneath the header.
    """
    lines = output.splitlines()
    results = []
    column_ranges = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Capture column widths from the first separator line we see.
        if set(stripped) <= {'-', ' '} and '-' in stripped:
            column_ranges = _column_ranges_from_separator(line)
            continue
        # Skip echoed prompts/commands/headers.
        if column_ranges is None:
            continue
        if 'Intf' in line or '#' in line or '>' in line:
            continue
        # Data row: slice by column.
        values = [line[s:e].strip() for s, e in column_ranges]
        if len(values) < len(FIELD_NAMES):
            continue
        if not values[0] or '/' not in values[0]:
            continue
        results.append(dict(zip(FIELD_NAMES, values[: len(FIELD_NAMES)])))

    return results

def publish_to_mqtt(broker, port, topic, message):
    """Publishes a message to an MQTT broker."""
    print(f"Connecting to MQTT broker at {broker}:{port}...")
    client = mqtt.Client()
    try:
        client.connect(broker, port)
        print(f"Publishing to topic {topic}")
        result = client.publish(topic, message)
        print(f"Publish result: {result.rc}")
        client.disconnect()
    except Exception as e:
        print(f"Error publishing to MQTT: {e}")

def main():
    try:
        config = load_config()
    except (FileNotFoundError, RuntimeError) as e:
        print(f"ERROR: {e}")
        raise SystemExit(2)

    for server in config["servers"]:
        print(f"\nProcessing server: {server['host']}")
        full_output = ""
        for cmd in server["commands"]:
            full_output += get_command_output(
                server["host"],
                server["user"],
                server["password"],
                cmd,
            )
            time.sleep(0.5)

        print("==========")
        print(full_output)
        print("==========")

        parsed_data = parse_poe_output(full_output)

        message_data = {
            "server": server["host"],
            "timestamp": time.time(),
            "poe_status": parsed_data,
        }
        message = json.dumps(message_data)

        publish_to_mqtt(
            config["mqtt"]["broker"],
            config["mqtt"]["port"],
            config["mqtt"]["status_topic"],
            message,
        )

        print("Data published to MQTT:")
        print(message)
        time.sleep(1)


if __name__ == "__main__":
    main()