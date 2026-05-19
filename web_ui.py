#!/usr/bin/env python3
"""Flask-based web UI for the EdgeSwitch PoE controller.

Three things, all served from one Flask process:

1. Browse and edit a port -> friendly-name mapping persisted to
   ``port_labels.json`` (path configurable via ``web_ui.port_labels_path``
   or ``PORT_LABELS_PATH``).
2. Toggle PoE per port. Toggle commands are published as MQTT messages
   to ``ubnt24/poe/<port>`` so the existing ``poe_control_mqtt.py``
   subscriber performs the actual SSH work.
3. Display periodic per-port status. The UI listens to
   ``ubnt24/poe/status`` and *also* runs its own background SSH poller
   so it works even when ``poe_status_to_mqtt.py`` is not scheduled
   externally.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from flask import Flask, jsonify, request, send_from_directory

from config import load_config
from poe_status_to_mqtt import get_command_output, parse_poe_output

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("web_ui")
logging.getLogger("paramiko").setLevel(logging.WARNING)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT_COUNT = 24
DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 8080
DEFAULT_POLL_INTERVAL = 30  # seconds


class WebUIState:
    """Thread-safe container for shared mutable state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.latest_status: dict[str, Any] = {
            "server": None,
            "timestamp": None,
            "poe_status": [],
            "source": None,  # "ssh" | "mqtt" | None
        }
        self.last_command: dict[str, Any] | None = None
        self.mqtt_connected: bool = False

    def set_status(self, payload: dict[str, Any], source: str) -> None:
        with self._lock:
            self.latest_status = {**payload, "source": source}

    def snapshot_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.latest_status)

    def set_last_command(self, info: dict[str, Any]) -> None:
        with self._lock:
            self.last_command = info

    def snapshot_last_command(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self.last_command) if self.last_command else None

    def set_mqtt_connected(self, connected: bool) -> None:
        with self._lock:
            self.mqtt_connected = connected

    def snapshot_mqtt_connected(self) -> bool:
        with self._lock:
            return self.mqtt_connected


def _resolve_labels_path(web_ui_cfg: dict[str, Any]) -> Path:
    """Where to read/write the port -> name mapping."""
    env_path = os.environ.get("PORT_LABELS_PATH")
    if env_path:
        return Path(env_path).expanduser().resolve()
    cfg_path = web_ui_cfg.get("port_labels_path")
    if cfg_path:
        return Path(cfg_path).expanduser().resolve()
    return (BASE_DIR / "port_labels.json").resolve()


def _load_labels(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read labels from %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        logger.warning("Labels file %s is not a JSON object; ignoring.", path)
        return {}
    # Drop comment-only / non-string entries; coerce keys to str.
    cleaned: dict[str, str] = {}
    for k, v in raw.items():
        if str(k).startswith("_"):
            continue
        if v is None:
            continue
        cleaned[str(k)] = str(v)
    return cleaned


def _save_labels_atomic(path: Path, labels: dict[str, str]) -> None:
    """Write labels JSON atomically (tmp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Sort by integer port number where possible for stable diffs.
    def _sort_key(item: tuple[str, str]) -> tuple[int, str]:
        try:
            return (int(item[0]), "")
        except ValueError:
            return (10_000, item[0])

    ordered = dict(sorted(labels.items(), key=_sort_key))
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(ordered, fh, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def _build_mqtt_client(
    config: dict[str, Any], state: WebUIState
) -> mqtt.Client:
    """Background MQTT client used for both publishing and listening."""
    status_topic = config["mqtt"]["status_topic"]

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            logger.info("Web UI MQTT connected; subscribing to %r", status_topic)
            client.subscribe(status_topic)
            state.set_mqtt_connected(True)
        else:
            logger.error("Web UI MQTT connect failed: rc=%s", rc)
            state.set_mqtt_connected(False)

    def on_disconnect(client, userdata, rc):
        state.set_mqtt_connected(False)
        if rc != 0:
            logger.warning("Web UI MQTT unexpectedly disconnected: rc=%s", rc)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("Bad status payload on %s: %s", msg.topic, exc)
            return
        if not isinstance(payload, dict):
            return
        state.set_status(payload, source="mqtt")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    return client


def _poll_status_once(
    config: dict[str, Any], state: WebUIState, mqtt_client: mqtt.Client
) -> dict[str, Any] | None:
    """SSH the first configured server, parse status, publish to MQTT."""
    server = config["servers"][0]
    full_output = ""
    try:
        for cmd in server.get("commands") or []:
            full_output += get_command_output(
                server["host"], server["user"], server["password"], cmd
            )
            time.sleep(0.3)
    except Exception as exc:  # paramiko raises a zoo of exceptions
        logger.error("SSH status poll failed against %s: %s", server.get("host"), exc)
        return None

    parsed = parse_poe_output(full_output)
    payload = {
        "server": server["host"],
        "timestamp": time.time(),
        "poe_status": parsed,
    }
    state.set_status(payload, source="ssh")
    try:
        mqtt_client.publish(
            config["mqtt"]["status_topic"], json.dumps(payload), qos=0
        )
    except Exception as exc:
        logger.warning("Could not publish polled status to MQTT: %s", exc)
    return payload


def _start_status_poller(
    config: dict[str, Any],
    state: WebUIState,
    mqtt_client: mqtt.Client,
    interval: int,
) -> threading.Event:
    """Run ``_poll_status_once`` in a daemon loop. Returns a stop event."""
    stop_event = threading.Event()

    def run():
        # A small initial delay so MQTT has a chance to connect first.
        if stop_event.wait(2.0):
            return
        while not stop_event.is_set():
            try:
                _poll_status_once(config, state, mqtt_client)
            except Exception:
                logger.exception("Unexpected error in status poller")
            stop_event.wait(interval)

    t = threading.Thread(target=run, name="poe-status-poller", daemon=True)
    t.start()
    return stop_event


def create_app(config: dict[str, Any] | None = None) -> Flask:
    config = config if config is not None else load_config()
    web_ui_cfg = dict(config.get("web_ui") or {})
    port_count = int(web_ui_cfg.get("port_count") or DEFAULT_PORT_COUNT)
    labels_path = _resolve_labels_path(web_ui_cfg)
    poll_interval = int(
        os.environ.get("STATUS_POLL_INTERVAL")
        or web_ui_cfg.get("status_poll_interval_seconds")
        or DEFAULT_POLL_INTERVAL
    )

    state = WebUIState()
    mqtt_client = _build_mqtt_client(config, state)

    broker = config["mqtt"]["broker"]
    broker_port = config["mqtt"]["port"]
    try:
        mqtt_client.connect_async(broker, broker_port)
        mqtt_client.loop_start()
        logger.info("Started MQTT loop towards %s:%s", broker, broker_port)
    except Exception as exc:
        logger.error("Initial MQTT connect failed (will retry): %s", exc)

    if poll_interval > 0:
        _start_status_poller(config, state, mqtt_client, poll_interval)
        logger.info("Status poller enabled; interval=%ss", poll_interval)
    else:
        logger.info("Status poller disabled (interval <= 0)")

    app = Flask(
        __name__,
        static_folder=str(BASE_DIR / "static"),
        template_folder=str(BASE_DIR / "templates"),
    )
    app.config["JSON_SORT_KEYS"] = False

    @app.route("/")
    def index():
        return send_from_directory(BASE_DIR / "templates", "index.html")

    @app.route("/api/meta")
    def api_meta():
        return jsonify(
            {
                "port_count": port_count,
                "mqtt_broker": broker,
                "mqtt_port": broker_port,
                "mqtt_connected": state.snapshot_mqtt_connected(),
                "control_topic_template": "ubnt24/poe/{port}",
                "status_topic": config["mqtt"]["status_topic"],
                "poll_interval_seconds": poll_interval,
                "labels_path": str(labels_path),
                "switch_host": (config["servers"][0] or {}).get("host"),
            }
        )

    @app.route("/api/labels", methods=["GET"])
    def api_labels_get():
        return jsonify(_load_labels(labels_path))

    @app.route("/api/labels", methods=["PUT", "POST"])
    def api_labels_put():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "expected JSON object"}), 400
        cleaned: dict[str, str] = {}
        for k, v in payload.items():
            try:
                port = int(k)
            except (TypeError, ValueError):
                return jsonify({"error": f"invalid port key {k!r}"}), 400
            if port < 1 or port > port_count:
                return (
                    jsonify(
                        {"error": f"port {port} out of range 1..{port_count}"}
                    ),
                    400,
                )
            label = "" if v is None else str(v).strip()
            if label:
                cleaned[str(port)] = label
        try:
            _save_labels_atomic(labels_path, cleaned)
        except OSError as exc:
            logger.error("Failed to save labels: %s", exc)
            return jsonify({"error": str(exc)}), 500
        logger.info("Saved %d label(s) to %s", len(cleaned), labels_path)
        return jsonify(cleaned)

    @app.route("/api/status")
    def api_status():
        return jsonify(state.snapshot_status())

    @app.route("/api/status/refresh", methods=["POST"])
    def api_status_refresh():
        payload = _poll_status_once(config, state, mqtt_client)
        if payload is None:
            return jsonify({"error": "SSH poll failed; check logs"}), 502
        return jsonify(payload)

    @app.route("/api/poe/<int:port>", methods=["POST"])
    def api_poe_toggle(port: int):
        if port < 1 or port > port_count:
            return (
                jsonify({"error": f"port {port} out of range 1..{port_count}"}),
                400,
            )
        body = request.get_json(silent=True) or {}
        action = str(body.get("action", "")).lower().strip()
        if action in ("on", "1", "auto", "enable"):
            mqtt_payload = "1"
            human_action = "on"
        elif action in ("off", "0", "shutdown", "disable"):
            mqtt_payload = "0"
            human_action = "off"
        else:
            return (
                jsonify({"error": "action must be 'on' or 'off'"}),
                400,
            )
        topic = f"ubnt24/poe/{port:02d}"
        result = mqtt_client.publish(topic, mqtt_payload, qos=1)
        # `publish` returns immediately; rc tells us if it was queued.
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error(
                "MQTT publish failed for %s payload=%s rc=%s",
                topic,
                mqtt_payload,
                result.rc,
            )
            return jsonify({"error": f"MQTT publish failed (rc={result.rc})"}), 502
        info = {
            "port": port,
            "action": human_action,
            "topic": topic,
            "payload": mqtt_payload,
            "timestamp": time.time(),
        }
        state.set_last_command(info)
        logger.info("Published %s -> %s", topic, mqtt_payload)
        return jsonify(info)

    @app.route("/api/last_command")
    def api_last_command():
        cmd = state.snapshot_last_command()
        return jsonify(cmd or {})

    return app


def main() -> None:
    try:
        config = load_config()
    except (FileNotFoundError, RuntimeError) as exc:
        logger.error("%s", exc)
        raise SystemExit(2)

    web_ui_cfg = dict(config.get("web_ui") or {})
    host = (
        os.environ.get("WEB_UI_HOST")
        or web_ui_cfg.get("host")
        or DEFAULT_HTTP_HOST
    )
    port = int(
        os.environ.get("WEB_UI_PORT")
        or web_ui_cfg.get("port")
        or DEFAULT_HTTP_PORT
    )

    app = create_app(config)
    logger.info("Web UI listening on http://%s:%s", host, port)
    # Single-process so the background MQTT/poller threads are usable.
    app.run(host=host, port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
