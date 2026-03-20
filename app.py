#!/usr/bin/env python3
import argparse
import json
import logging
import os
import signal
import sqlite3
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request
from pubsub import pub
import meshtastic.serial_interface
import meshtastic.tcp_interface

VERSION = "0.4.0"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "messages.db"
LOG_JSONL = BASE_DIR / "messages.jsonl"
DEFAULT_CONFIG_PATH = BASE_DIR / "app_config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("meshtastic-webchat")

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

iface = None
iface_lock = threading.Lock()
db_lock = threading.Lock()
cache_lock = threading.Lock()

running = True
config_path = DEFAULT_CONFIG_PATH
app_config = {}
cached_nodes = []
runtime = {
    "backend_connected": False,
    "connection_detail": "not connected",
    "last_connect_at": None,
    "last_disconnect_at": None,
    "last_error": None,
    "last_packet_at": None,
    "messages_received": 0,
    "messages_sent": 0,
    "messages_saved": 0,
    "nodes_known": 0,
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def deep_merge(base_obj: dict, override_obj: dict) -> dict:
    out = deepcopy(base_obj)
    for k, v in override_obj.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def default_config() -> dict:
    return {
        "version": VERSION,
        "node": {
            "mode": "serial",
            "host": "",
            "port": "/dev/ttyUSB0",
            "channel": 0
        },
        "web": {
            "listen_host": "0.0.0.0",
            "listen_port": 8088,
            "ssl_adhoc": True
        },
        "ui": {
            "default_language": "it"
        }
    }


def load_config(path: Path) -> dict:
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return deep_merge(default_config(), data)
    return default_config()


def save_config(cfg: dict) -> None:
    cfg = deepcopy(cfg)
    cfg["version"] = VERSION
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def node_mode() -> str:
    return app_config["node"]["mode"]


def node_target() -> str:
    return app_config["node"]["port"] if node_mode() == "serial" else app_config["node"]["host"]


def channel_index() -> int:
    try:
        return int(app_config["node"].get("channel", 0))
    except Exception:
        return 0


def update_runtime(**kwargs) -> None:
    with cache_lock:
        runtime.update(kwargs)


def snapshot_runtime() -> dict:
    with cache_lock:
        return deepcopy(runtime)


def init_db() -> None:
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    from_id TEXT,
                    to_id TEXT,
                    text TEXT NOT NULL,
                    raw_json TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def save_message(direction: str, from_id: str, to_id: str, text: str, raw_packet: Optional[dict] = None) -> dict:
    payload = {
        "ts": now_iso(),
        "direction": direction,
        "from_id": from_id,
        "to_id": to_id,
        "text": text,
    }
    raw_json = json.dumps(raw_packet, ensure_ascii=False, default=str) if raw_packet is not None else None

    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("PRAGMA busy_timeout=5000;")
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO messages (ts, direction, from_id, to_id, text, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
                (payload["ts"], direction, from_id, to_id, text, raw_json),
            )
            conn.commit()
            msg_id = cur.lastrowid
            cur.execute("SELECT COUNT(*) FROM messages")
            total = int(cur.fetchone()[0])
        finally:
            conn.close()

    payload["id"] = msg_id
    with open(LOG_JSONL, "a", encoding="utf-8") as f:
        json.dump({**payload, "raw_packet": raw_packet}, f, ensure_ascii=False, default=str)
        f.write("\n")

    update_runtime(messages_saved=total, last_packet_at=payload["ts"])
    return payload


def decode_text(packet: dict) -> Optional[str]:
    decoded = packet.get("decoded", {}) or {}
    text = decoded.get("text")
    if text:
        return text
    payload = decoded.get("payload")
    if isinstance(payload, (bytes, bytearray)):
        try:
            return payload.decode("utf-8", errors="replace")
        except Exception:
            return repr(payload)
    return None


def on_text(packet, interface=None):
    text = decode_text(packet)
    if not text:
        return
    from_id = packet.get("fromId", str(packet.get("from", "unknown")))
    to_id = packet.get("toId", str(packet.get("to", "^all")))
    save_message("in", from_id, to_id, text, packet)
    state = snapshot_runtime()
    update_runtime(messages_received=state["messages_received"] + 1, last_packet_at=now_iso())


def close_iface() -> None:
    global iface
    with iface_lock:
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass
            iface = None


def connect_meshtastic() -> None:
    global iface
    mode = node_mode()
    target = node_target()
    logger.info("Connecting to Meshtastic via %s -> %s", mode, target)
    new_iface = (
        meshtastic.serial_interface.SerialInterface(devPath=target)
        if mode == "serial"
        else meshtastic.tcp_interface.TCPInterface(hostname=target)
    )
    with iface_lock:
        iface = new_iface
    update_runtime(
        backend_connected=True,
        connection_detail="connected",
        last_connect_at=now_iso(),
        last_error=None,
    )
    logger.info("Connected to Meshtastic via %s -> %s", mode, target)


def refresh_nodes_cache() -> None:
    with iface_lock:
        current = iface
        nodes = getattr(current, "nodes", {}) or {} if current is not None else {}
    out = []
    for node_id, node in nodes.items():
        user = node.get("user", {}) or {}
        out.append({
            "node_id": node_id,
            "name": user.get("longName") or user.get("shortName") or node_id,
            "short_name": user.get("shortName") or "",
            "hw_model": user.get("hwModel") or "",
            "last_heard": node.get("lastHeard"),
        })
    out.sort(key=lambda x: (x["name"], x["node_id"]))
    global cached_nodes
    with cache_lock:
        cached_nodes = out
        runtime["nodes_known"] = len(out)


def connection_worker() -> None:
    while running:
        try:
            with iface_lock:
                current = iface
            if current is None:
                connect_meshtastic()
            refresh_nodes_cache()
            time.sleep(5)
        except Exception as exc:
            logger.warning("Meshtastic worker error: %s", exc)
            update_runtime(
                backend_connected=False,
                connection_detail="disconnected",
                last_disconnect_at=now_iso(),
                last_error=str(exc),
            )
            close_iface()
            time.sleep(5)


@app.route("/")
def index():
    return render_template("index.html", version=VERSION)


@app.route("/api/status")
def api_status():
    return jsonify({
        "ok": True,
        "version": VERSION,
        "mode": node_mode(),
        "target": node_target(),
        "channel": channel_index(),
        **snapshot_runtime(),
    })


@app.route("/api/messages")
def api_messages():
    limit = min(int(request.args.get("limit", 100)), 500)
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("PRAGMA busy_timeout=5000;")
            cur = conn.cursor()
            cur.execute(
                "SELECT id, ts, direction, from_id, to_id, text FROM messages ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    rows.reverse()
    return jsonify([
        {"id": r[0], "ts": r[1], "direction": r[2], "from_id": r[3], "to_id": r[4], "text": r[5]}
        for r in rows
    ])


@app.route("/api/nodes")
def api_nodes():
    with cache_lock:
        return jsonify(deepcopy(cached_nodes))


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    dest = (data.get("dest") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message"}), 400

    with iface_lock:
        current = iface
        if current is None:
            return jsonify({"ok": False, "error": "Meshtastic backend not connected"}), 503
        kwargs = {"text": text, "wantAck": False, "channelIndex": channel_index()}
        if dest:
            kwargs["destinationId"] = dest
        try:
            current.sendText(**kwargs)
        except Exception as exc:
            update_runtime(backend_connected=False, connection_detail="send failed", last_error=str(exc))
            return jsonify({"ok": False, "error": str(exc)}), 500

    to_id = dest or "^all"
    msg = save_message("out", "io", to_id, text)
    state = snapshot_runtime()
    update_runtime(messages_sent=state["messages_sent"] + 1)
    return jsonify({"ok": True, "message": msg})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("DELETE FROM messages")
            conn.commit()
        finally:
            conn.close()
    if LOG_JSONL.exists():
        LOG_JSONL.unlink()
    update_runtime(messages_saved=0)
    return jsonify({"ok": True})


@app.route("/api/debug")
def api_debug():
    return jsonify({
        "version": VERSION,
        "config_path": str(config_path),
        "node": deepcopy(app_config.get("node", {})),
        "web": deepcopy(app_config.get("web", {})),
        "ui": deepcopy(app_config.get("ui", {})),
        "runtime": snapshot_runtime(),
    })


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    global app_config
    if request.method == "GET":
        return jsonify(deepcopy(app_config))

    new_cfg = request.get_json(force=True, silent=True)
    if not isinstance(new_cfg, dict):
        return jsonify({"ok": False, "error": "Invalid JSON config"}), 400

    try:
        merged = deep_merge(default_config(), new_cfg)
        mode = merged["node"]["mode"]
        if mode not in {"serial", "tcp"}:
            raise ValueError("node.mode must be 'serial' or 'tcp'")
        if mode == "serial" and not merged["node"]["port"]:
            raise ValueError("node.port is required in serial mode")
        if mode == "tcp" and not merged["node"]["host"]:
            raise ValueError("node.host is required in tcp mode")
        save_config(merged)
        app_config = merged
        close_iface()
        update_runtime(connection_detail="config updated; reconnect pending")
        return jsonify({"ok": True, "config": merged})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


def stop_handler(signum, frame):
    global running
    running = False
    close_iface()
    os._exit(0)


def apply_cli_overrides(cfg: dict, args) -> dict:
    if args.port:
        cfg["node"]["mode"] = "serial"
        cfg["node"]["port"] = args.port
        cfg["node"]["host"] = ""
    if args.host:
        cfg["node"]["mode"] = "tcp"
        cfg["node"]["host"] = args.host
        cfg["node"]["port"] = ""
    if args.channel is not None:
        cfg["node"]["channel"] = args.channel
    if args.listen_host:
        cfg["web"]["listen_host"] = args.listen_host
    if args.listen_port is not None:
        cfg["web"]["listen_port"] = args.listen_port
    if args.ssl_adhoc:
        cfg["web"]["ssl_adhoc"] = True
    return cfg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Meshtastic Web Chat")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to app_config.json")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--port", help="Serial device, e.g. /dev/ttyUSB0")
    group.add_argument("--host", help="Node host/IP, e.g. 192.168.0.18")
    parser.add_argument("--listen-host", help="Web listen host override")
    parser.add_argument("--listen-port", type=int, help="Web listen port override")
    parser.add_argument("--channel", type=int, help="Channel index override")
    parser.add_argument("--ssl-adhoc", action="store_true", help="Enable Flask adhoc HTTPS")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    app_config = apply_cli_overrides(load_config(config_path), args)

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    init_db()
    pub.subscribe(on_text, "meshtastic.receive.text")

    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM messages")
            total = int(cur.fetchone()[0])
        finally:
            conn.close()
    update_runtime(messages_saved=total)

    threading.Thread(target=connection_worker, daemon=True, name="meshtastic-connection-worker").start()

    logger.info("Starting web server on %s:%s", app_config["web"]["listen_host"], app_config["web"]["listen_port"])
    logger.info("Meshtastic target %s -> %s (ch %s)", node_mode(), node_target(), channel_index())

    ssl_context = "adhoc" if app_config["web"].get("ssl_adhoc") else None
    app.run(
        host=app_config["web"]["listen_host"],
        port=int(app_config["web"]["listen_port"]),
        debug=False,
        threaded=True,
        ssl_context=ssl_context,
    )
