# Ubnt_EdgeMax_Power_Monitor

A small Python service that talks to a Ubiquiti EdgeSwitch over SSH (via
[paramiko](https://www.paramiko.org/)) to:

- **Publish PoE status** for every port over MQTT (`show poe status` parsed
  into JSON),
- **Control PoE on/off per port** in response to MQTT commands, and
- **Drive both from a small built-in web UI** with editable per-port
  labels (e.g. `port 13 → TSO_0 camera`), live status and on/off buttons.

Originally built to remotely power-cycle PoE cameras and edge nodes that
sit behind a `UBNT EdgeSwitch ES-24-500W` from any MQTT-speaking client
(Home Assistant, Node-RED, a balena fleet variable, a `mosquitto_pub`
one-liner, …).

> **Tested hardware:** Ubiquiti `ES-24-500W` running EdgeSwitch firmware
> with the CLI prompt style `(<name>) >` / `(<name>) #`. Other EdgeSwitch
> models that share the same CLI grammar (`enable`, `configure`,
> `interface 0/X`, `poe opmode auto|shutdown`, `show poe status`) should
> work without modification.

## How it works

```
                       ┌──────────────────────┐
 MQTT ubnt24/poe/07 ─▶ │ poe_control_mqtt.py  │ ─SSH─▶ EdgeSwitch:
   payload "0" / "1"   │  (subscriber loop)   │       configure
                       └──────────────────────┘        interface 0/7
                                                       poe opmode auto|shutdown

                       ┌──────────────────────┐
 cron / timer       ─▶ │ poe_status_to_mqtt.py│ ─SSH─▶ EdgeSwitch:
                       │  (one-shot publisher)│        show poe status …
                       └──────────────────────┘
                              │
                              ▼
                    MQTT ubnt24/poe/status
                    {"server": "…", "timestamp": …, "poe_status": [{…}, …]}
```

## Repository layout

| File                                | Purpose                                                                  |
| ----------------------------------- | ------------------------------------------------------------------------ |
| `poe_control_mqtt.py`               | MQTT subscriber → SSH → toggle PoE per port. Long-running.               |
| `poe_status_to_mqtt.py`             | Reads `show poe status` from the switch and publishes JSON. One-shot.    |
| `web_ui.py`                         | Flask web UI: port labels, live status, on/off buttons. Long-running.    |
| `templates/`, `static/`             | HTML, CSS, JS for the web UI.                                            |
| `mqtt_test.py`                      | Read-only MQTT monitor that prints every message on `ubnt24/poe/#`.      |
| `config.py`                         | Shared config loader (handles env-var overrides, gives clear errors).    |
| `poe_control_config.example.json`   | Example config. Copy to `poe_control_config.json`; the real file is gitignored. |
| `port_labels.example.json`          | Example port → name mapping for the web UI. Real `port_labels.json` is gitignored. |
| `.env.example`                      | Example secrets file for docker-compose. Copy to `.env`.                 |
| `Dockerfile`, `docker-compose.yml`  | Self-contained deploy with bundled EMQX broker (optional).               |
| `requirements.txt`                  | `paramiko`, `paho-mqtt`, `flask`.                                        |

## Quick start (host Python)

```bash
git clone https://github.com/<you>/Ubnt_EdgeMax_Power_Monitor.git
cd Ubnt_EdgeMax_Power_Monitor

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp poe_control_config.example.json poe_control_config.json
$EDITOR poe_control_config.json   # set mqtt.broker, servers[].host, etc.

export POE_PASSWORD='your-edgeswitch-admin-password'

# one-shot status publish
python poe_status_to_mqtt.py

# in another shell: watch the bus
python mqtt_test.py

# start the control listener
python poe_control_mqtt.py
```

Send a command from anywhere with `mosquitto_pub`:

```bash
# Power port 7 off
mosquitto_pub -h <broker-host> -p 1883 -t ubnt24/poe/07 -m 0

# Power port 7 back on (PoE "auto")
mosquitto_pub -h <broker-host> -p 1883 -t ubnt24/poe/07 -m 1
```

## Configuration

`config.py` resolves settings with the following priority (highest wins):

1. **Environment variable** — listed below.
2. **Field in the JSON config file**.
3. **Hard-coded default**.

A field set to `null` or `""` in the JSON file is treated as unset, so the
env var (or default) takes over. This lets you commit a config template
with `"password": null` and supply the actual password via the
environment.

### Environment variables

| Variable               | What it overrides                                       | Required |
| ---------------------- | ------------------------------------------------------- | -------- |
| `POE_CONFIG_PATH`      | Path to the JSON config file.                           | No (defaults to `./poe_control_config.json`). |
| `POE_PASSWORD`         | `servers[*].password` (admin + enable secret).          | Yes, unless set in JSON. |
| `MQTT_BROKER`          | `mqtt.broker`.                                          | No, but required *somewhere*. |
| `MQTT_PORT`            | `mqtt.port`.                                            | No (defaults to 1883). |
| `LOG_LEVEL`            | Logger level for `poe_control_mqtt.py` / `web_ui.py`.   | No (defaults to `INFO`). |
| `WEB_UI_HOST`          | `web_ui.host` (Flask bind address).                     | No (defaults to `0.0.0.0`). |
| `WEB_UI_PORT`          | `web_ui.port` (Flask TCP port).                         | No (defaults to `8080`). |
| `PORT_LABELS_PATH`     | `web_ui.port_labels_path` (where labels are stored).    | No (defaults to `./port_labels.json`). |
| `STATUS_POLL_INTERVAL` | `web_ui.status_poll_interval_seconds` (`0` disables it). | No (defaults to `30`). |

### Config schema

```jsonc
{
  "mqtt": {
    "broker": "broker.example.lan",   // or null + MQTT_BROKER
    "port": 1883,                     // optional
    "topic": "ubnt24/poe/+",          // subscribe filter (control listener)
    "status_topic": "ubnt24/poe/status" // publish topic (status publisher)
  },
  "servers": [
    {
      "host": "192.0.2.1",
      "user": "admin",
      "password": null,               // or null + POE_PASSWORD
      "commands": [                   // used by poe_status_to_mqtt.py
        "show poe status 0/1-0/12",
        "show poe status 0/13-0/24"
      ]
    }
  ],
  "web_ui": {                         // optional; only used by web_ui.py
    "host": "0.0.0.0",
    "port": 8080,
    "port_count": 24,
    "status_poll_interval_seconds": 30,
    "port_labels_path": "./port_labels.json"
  }
}
```

### MQTT topic conventions

- **Control (subscribed by `poe_control_mqtt.py`):**
  `ubnt24/poe/<port>` with payload `0` (shutdown) or `1` (auto).
  The numeric `<port>` is mapped to interface `0/<port>` on the switch.
- **Status (published by `poe_status_to_mqtt.py`):**
  `ubnt24/poe/status` with a JSON payload of the form

  ```json
  {
    "server": "192.0.2.1",
    "timestamp": 1779224542.6,
    "poe_status": [
      {"intf": "0/1", "detection": "Open Circuit", "class": "Unknown", "consumed": "0.00", "voltage": "0.00", "current": "0.00", "meter": "0.00", "temp": "45"},
      {"intf": "0/2", "detection": "Good", "class": "Class3", "consumed": "5.43", "voltage": "52.18", "current": "104.12", "meter": "2.78", "temp": "45"}
    ]
  }
  ```

  The parser reads the dashed-separator line under the CLI header to
  determine column widths, so multi-word values such as `Open Circuit`
  are preserved correctly.

## Web UI

`web_ui.py` is a small Flask app that bundles three things for humans:

- An editable **port → friendly-name mapping** persisted to
  `port_labels.json` (gitignored; copy `port_labels.example.json` if you
  want a starting point).
- **Live PoE status per port** (detection / class / consumed W / V & mA).
  It listens to `ubnt24/poe/status` *and* runs its own background SSH
  poll every `status_poll_interval_seconds` so it works even when
  `poe_status_to_mqtt.py` is not running on a schedule.
- **ON / OFF buttons per port** that publish to `ubnt24/poe/<port>`.
  The toggle path goes through MQTT, so the existing
  `poe_control_mqtt.py` is what actually SSHes to the switch — start it
  alongside the web UI.

```bash
# Standalone (host Python):
python web_ui.py            # browse http://<host>:8080

# With docker-compose:
docker compose up -d --build
# poe_web_ui, poe_control and poe_status all share the bundled broker.
```

The labels file is a flat JSON object keyed by port number:

```json
{
  "13": "TSO_0 camera",
  "14": "TSO_1 camera"
}
```

You can also edit this file by hand; the UI reloads it on every page
load.

## Running scheduled status publishes

`poe_status_to_mqtt.py` is one-shot. Wire it into your scheduler of
choice:

```cron
*/5 * * * * cd /opt/Ubnt_EdgeMax_Power_Monitor && \
    POE_PASSWORD=... .venv/bin/python poe_status_to_mqtt.py >> /var/log/poe_status.log 2>&1
```

…or with a systemd timer, balena pulse, GitHub Actions cron, etc.

## Docker / docker-compose

```bash
cp poe_control_config.example.json poe_control_config.json
cp .env.example .env
$EDITOR poe_control_config.json .env

docker compose up -d --build
```

The bundled `emqx` service is optional — point `MQTT_BROKER` at an
existing broker and remove the `emqx` service if you already have one.

## Safety notes

- The control listener will happily power-cycle whatever is plugged in.
  Restrict who can publish to `ubnt24/poe/+` on your broker
  (ACLs / TLS / username+password).
- The EdgeSwitch admin user has full CLI access. Treat the credentials
  the same way you would treat any other infrastructure password — keep
  them in `.env` or a secret manager, never in committed config.
- Be aware of inrush current: rapidly toggling many high-draw PoE+
  cameras can trip the switch's PSU. Sequence shutdowns/restarts.

## License

[MIT](./LICENSE)
