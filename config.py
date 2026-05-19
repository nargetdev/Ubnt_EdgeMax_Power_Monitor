"""Shared configuration loader.

Resolution order for each setting:
  1. Environment variable (highest priority).
  2. Value from the JSON config file.
  3. Hard-coded default (lowest priority).

A field set to ``null`` (or empty string) in the JSON file is treated as
unset, so the env var / default takes over. This keeps the repo free of
secrets: ship the example file with ``"password": null`` and provide the
real password via ``POE_PASSWORD``.

Recognised env vars:
  POE_CONFIG_PATH         Absolute path to the JSON config file.
  POE_PASSWORD            EdgeSwitch admin/enable password.
  MQTT_BROKER             MQTT broker hostname or IP.
  MQTT_PORT               MQTT broker TCP port (integer).
  PORT_LABELS_PATH        Where the web UI stores port -> name mapping.
  WEB_UI_HOST             Bind address for the web UI Flask server.
  WEB_UI_PORT             TCP port for the web UI Flask server.
  STATUS_POLL_INTERVAL    Seconds between background SSH status polls
                          (0 disables the poller).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_FILENAME = "poe_control_config.json"
DEFAULT_MQTT_PORT = 1883
DEFAULT_SUB_TOPIC = "ubnt24/poe/+"
DEFAULT_STATUS_TOPIC = "ubnt24/poe/status"


def _coerce(value: Any) -> Any:
    """Treat ``null``/empty-string as 'not set'."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _resolve_config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    env_path = os.environ.get("POE_CONFIG_PATH")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return (Path(__file__).parent / DEFAULT_CONFIG_FILENAME).resolve()


def load_config(path: str | os.PathLike[str] | None = None) -> dict:
    """Load and normalise the configuration.

    Returns a dict with the same shape as the JSON file, with env-var
    overrides applied and missing optional fields filled with defaults.
    Raises ``RuntimeError`` if required secrets are still missing after
    consulting the environment.
    """
    config_path = _resolve_config_path(path)
    if not config_path.is_file():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. Copy "
            f"poe_control_config.example.json -> {DEFAULT_CONFIG_FILENAME} "
            f"or set POE_CONFIG_PATH."
        )
    with config_path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)

    mqtt_cfg = dict(raw.get("mqtt") or {})
    mqtt_cfg["broker"] = (
        os.environ.get("MQTT_BROKER")
        or _coerce(mqtt_cfg.get("broker"))
    )
    if not mqtt_cfg["broker"]:
        raise RuntimeError(
            "MQTT broker is not set. Set 'mqtt.broker' in the config "
            "file or export MQTT_BROKER."
        )
    port_env = os.environ.get("MQTT_PORT")
    mqtt_cfg["port"] = int(
        port_env
        if port_env
        else (_coerce(mqtt_cfg.get("port")) or DEFAULT_MQTT_PORT)
    )
    mqtt_cfg["topic"] = _coerce(mqtt_cfg.get("topic")) or DEFAULT_SUB_TOPIC
    mqtt_cfg["status_topic"] = (
        _coerce(mqtt_cfg.get("status_topic")) or DEFAULT_STATUS_TOPIC
    )

    env_password = os.environ.get("POE_PASSWORD")
    servers = []
    for server in raw.get("servers") or []:
        server = dict(server)
        password = _coerce(server.get("password")) or env_password
        if not password:
            raise RuntimeError(
                f"No password for server {server.get('host')!r}. Set "
                f"'password' in the config or export POE_PASSWORD."
            )
        server["password"] = password
        server.setdefault("user", "admin")
        if not server.get("host"):
            raise RuntimeError("Server entry missing 'host'.")
        server.setdefault("commands", [])
        servers.append(server)

    if not servers:
        raise RuntimeError("Config has no 'servers' entries.")

    # Optional web_ui section. Anything missing here is filled in with
    # defaults inside web_ui.py / its env-var overrides.
    web_ui_cfg = dict(raw.get("web_ui") or {})

    return {
        "mqtt": mqtt_cfg,
        "servers": servers,
        "web_ui": web_ui_cfg,
        "_config_path": str(config_path),
    }
