#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import os
import signal
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request
from pubsub import pub
import meshtastic.serial_interface
import meshtastic.tcp_interface

VERSION = "0.4.3"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "messages.db"
LOG_JSONL = BASE_DIR / "messages.jsonl"

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

iface = None
iface_lock = threading.Lock()
recent_lock = threading.Lock()
recent_messages = []
MAX_RECENT = 300
channel_index = 0
APP_MODE = "unknown"
APP_TARGET = ""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
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
    conn.close()


def save_message(direction: str, from_id: str, to_id: str, text: str, raw_packet: Optional[dict] = None):
    payload = {
        "ts": now_iso(),
        "direction": direction,
        "from_id": from_id,
        "to_id": to_id,
        "text": text,
    }
    raw_json = json.dumps(raw_packet, ensure_ascii=False, default=str) if raw_packet is not None else None

    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (ts, direction, from_id, to_id, text, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
        (payload["ts"], direction, from_id, to_id, text, raw_json),
    )
    conn.commit()
    payload["id"] = cur.lastrowid
    conn.close()

    with recent_lock:
        recent_messages.append(payload)
        if len(recent_messages) > MAX_RECENT:
            del recent_messages[:-MAX_RECENT]

    with open(LOG_JSONL, "a", encoding="utf-8") as f:
        json.dump({**payload, "raw_packet": raw_packet}, f, ensure_ascii=False, default=str)
        f.write("\n")

    return payload


def decode_text(packet: dict) -> Optional[str]:
    decoded = packet.get("decoded", {})
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


def connect_meshtastic(mode: str, target: str):
    global iface
    if mode == "serial":
        iface = meshtastic.serial_interface.SerialInterface(devPath=target)
    else:
        iface = meshtastic.tcp_interface.TCPInterface(hostname=target)


def get_nodes():
    with iface_lock:
        nodes = getattr(iface, "nodes", {}) or {}
    out = []
    for node_id, node in nodes.items():
        user = node.get("user", {})
        out.append(
            {
                "node_id": node_id,
                "name": user.get("longName") or user.get("shortName") or node_id,
                "short_name": user.get("shortName") or "",
                "hw_model": user.get("hwModel") or "",
                "last_heard": node.get("lastHeard"),
            }
        )
    out.sort(key=lambda x: (x["name"] or "", x["node_id"]))
    return out


def get_status_payload():
    return {"ok": True, "version": VERSION, "mode": APP_MODE, "target": APP_TARGET, "channel": channel_index}


@app.route("/")
def index():
    return render_template("index.html", version=VERSION)


@app.route("/api/status")
def api_status():
    return jsonify(get_status_payload())


@app.route("/api/messages")
def api_messages():
    limit = min(int(request.args.get("limit", 100)), 500)
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT id, ts, direction, from_id, to_id, text FROM messages ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    rows.reverse()
    return jsonify([
        {"id": r[0], "ts": r[1], "direction": r[2], "from_id": r[3], "to_id": r[4], "text": r[5]}
        for r in rows
    ])


@app.route("/api/nodes")
def api_nodes():
    return jsonify(get_nodes())


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    dest = (data.get("dest") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message"}), 400
    with iface_lock:
        try:
            kwargs = {"text": text, "wantAck": False, "channelIndex": channel_index}
            if dest:
                kwargs["destinationId"] = dest
            iface.sendText(**kwargs)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    msg = save_message("out", "io", dest or "^all", text)
    return jsonify({"ok": True, "message": msg})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    conn = db_connect()
    conn.execute("DELETE FROM messages")
    conn.commit()
    conn.close()
    with recent_lock:
        recent_messages.clear()
    return jsonify({"ok": True})


def load_config(path: Optional[str]) -> dict:
    cfg = {}
    if path:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    return cfg


def build_settings(args) -> dict:
    cfg = load_config(args.config)
    node_cfg = cfg.get("node", {})
    web_cfg = cfg.get("web", {})

    mode = node_cfg.get("mode")
    host = node_cfg.get("host") or ""
    port = node_cfg.get("port") or ""
    channel = int(node_cfg.get("channel", 0))
    listen_host = web_cfg.get("listen_host", "127.0.0.1")
    listen_port = int(web_cfg.get("listen_port", 8088))
    ssl_adhoc = bool(web_cfg.get("ssl_adhoc", False))

    if args.host:
        mode = "tcp"
        host = args.host
    if args.port:
        mode = "serial"
        port = args.port
    if args.channel is not None:
        channel = args.channel
    if args.listen_host:
        listen_host = args.listen_host
    if args.listen_port is not None:
        listen_port = args.listen_port
    if args.ssl_adhoc:
        ssl_adhoc = True

    if mode == "serial":
        if not port:
            raise SystemExit("serial mode requires node.port or --port")
        target = port
    elif mode == "tcp":
        if not host:
            raise SystemExit("tcp mode requires node.host or --host")
        target = host
    else:
        raise SystemExit("node.mode must be 'serial' or 'tcp'")

    return {
        "mode": mode,
        "target": target,
        "channel": channel,
        "listen_host": listen_host,
        "listen_port": listen_port,
        "ssl_adhoc": ssl_adhoc,
    }


def stop_handler(signum, frame):
    try:
        with iface_lock:
            if iface is not None:
                iface.close()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Meshtastic local web chat")
    parser.add_argument("--config", help="Path to JSON config file")
    parser.add_argument("--port", help="Serial port, e.g. /dev/ttyUSB0")
    parser.add_argument("--host", help="Node IP/hostname, e.g. 192.168.0.18")
    parser.add_argument("--listen-host", help="Web listen host")
    parser.add_argument("--listen-port", type=int, help="Web listen port")
    parser.add_argument("--channel", type=int, help="Channel index")
    parser.add_argument("--ssl-adhoc", action="store_true", help="Enable Flask adhoc HTTPS")
    args = parser.parse_args()

    settings = build_settings(args)
    APP_MODE = settings["mode"]
    APP_TARGET = settings["target"]
    channel_index = settings["channel"]

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    init_db()
    pub.subscribe(on_text, "meshtastic.receive.text")
    connect_meshtastic(APP_MODE, APP_TARGET)

    print(f"Meshtastic Web Chat v{VERSION}")
    print(f"Backend Meshtastic: {APP_MODE} -> {APP_TARGET}")
    print(f"Web UI: {'https' if settings['ssl_adhoc'] else 'http'}://{settings['listen_host']}:{settings['listen_port']}")

    if settings["ssl_adhoc"]:
        try:
            import OpenSSL  # noqa: F401
        except Exception as exc:
            raise SystemExit("ssl_adhoc requires pyopenssl in the active environment") from exc
        app.run(host=settings["listen_host"], port=settings["listen_port"], debug=False, threaded=True, ssl_context="adhoc")
    else:
        app.run(host=settings["listen_host"], port=settings["listen_port"], debug=False, threaded=True)
