# Ubnt_EdgeMax_Power_Monitor

A small Python service that talks to a Ubiquiti EdgeSwitch over SSH (via
[paramiko](https://www.paramiko.org/)) to:

- **Publish PoE status** for every port over MQTT (`show poe status` parsed
  into JSON), and
- **Control PoE on/off per port** in response to MQTT commands.

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
| `mqtt_test.py`                      | Read-only MQTT monitor that prints every message on `ubnt24/poe/#`.      |
| `config.py`                         | Shared config loader (handles env-var overrides, gives clear errors).    |
| `poe_control_config.example.json`   | Example config. Copy to `poe_control_config.json`; the real file is gitignored. |
| `.env.example`                      | Example secrets file for docker-compose. Copy to `.env`.                 |
| `Dockerfile`, `docker-compose.yml`  | Self-contained deploy with bundled EMQX broker (optional).               |
| `requirements.txt`                  | `paramiko`, `paho-mqtt`.                                                 |

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

| Variable          | What it overrides                              | Required |
| ----------------- | ---------------------------------------------- | -------- |
| `POE_CONFIG_PATH` | Path to the JSON config file.                  | No (defaults to `./poe_control_config.json`). |
| `POE_PASSWORD`    | `servers[*].password` (admin + enable secret). | Yes, unless set in JSON. |
| `MQTT_BROKER`     | `mqtt.broker`.                                 | No, but required *somewhere*. |
| `MQTT_PORT`       | `mqtt.port`.                                   | No (defaults to 1883). |
| `LOG_LEVEL`       | Logger level for `poe_control_mqtt.py`.        | No (defaults to `INFO`). |

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
  ]
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
