#!/usr/bin/env python3
import argparse
import json
import logging
import os
import signal
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request
from pubsub import pub
import meshtastic.serial_interface
import meshtastic.tcp_interface

VERSION = "0.3.2"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "messages.db"
LOG_JSONL = BASE_DIR / "messages.jsonl"

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("meshtastic_webchat")

iface = None
iface_lock = threading.Lock()
storage_lock = threading.Lock()
state_lock = threading.Lock()
stop_event = threading.Event()
connected_once = False

APP_MODE = None
APP_TARGET = None
channel_index = 0

runtime = {
    "backend_connected": False,
    "connection_detail": "starting",
    "last_error": None,
    "last_connect_at": None,
    "last_disconnect_at": None,
    "last_packet_at": None,
    "last_text_at": None,
    "messages_rx": 0,
    "messages_tx": 0,
    "packets_rx_seen": 0,
    "relay_seen": 0,
    "multi_hop_seen": 0,
    "nodes_known": 0,
    "online_nodes": None,
    "channel_utilization": None,
    "air_util_tx": None,
    "battery_level": None,
    "voltage": None,
}

cached_nodes = []
MAX_RECENT = 300
recent_messages = []


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def update_runtime(**kwargs):
    with state_lock:
        runtime.update(kwargs)


def snapshot_runtime():
    with state_lock:
        return dict(runtime)


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    with storage_lock:
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


def load_messages(limit: int = 100):
    with storage_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, ts, direction, from_id, to_id, text FROM messages ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        conn.close()
    rows.reverse()
    return [
        {"id": r[0], "ts": r[1], "direction": r[2], "from_id": r[3], "to_id": r[4], "text": r[5]}
        for r in rows
    ]


def save_message(direction: str, from_id: str, to_id: str, text: str, raw_packet: Optional[dict] = None):
    payload = {
        "ts": now_iso(),
        "direction": direction,
        "from_id": from_id,
        "to_id": to_id,
        "text": text,
    }
    raw_json = json.dumps(raw_packet, ensure_ascii=False, default=str) if raw_packet is not None else None
    with storage_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (ts, direction, from_id, to_id, text, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
            (payload["ts"], direction, from_id, to_id, text, raw_json),
        )
        conn.commit()
        msg_id = cur.lastrowid
        conn.close()
    payload["id"] = msg_id
    with state_lock:
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


def safe_node_list(nodes: dict):
    out = []
    for node_id, node in (nodes or {}).items():
        user = node.get("user", {}) or {}
        out.append(
            {
                "node_id": node_id,
                "name": user.get("longName") or user.get("shortName") or node_id,
                "short_name": user.get("shortName") or "",
                "hw_model": user.get("hwModel") or "",
                "last_heard": node.get("lastHeard"),
                "battery_level": (node.get("deviceMetrics") or {}).get("batteryLevel"),
                "voltage": (node.get("deviceMetrics") or {}).get("voltage"),
            }
        )
    out.sort(key=lambda x: (x["name"] or "", x["node_id"]))
    return out


def refresh_cache_from_iface():
    global cached_nodes
    with iface_lock:
        local_iface = iface
        if not local_iface:
            return
        nodes = getattr(local_iface, "nodes", {}) or {}
        my_info = getattr(local_iface, "myInfo", None)
        nodes_by_num = getattr(local_iface, "nodesByNum", {}) or {}

    nodes_list = safe_node_list(nodes)
    local_stats = {}
    device_metrics = {}
    if isinstance(my_info, dict):
        my_num = my_info.get("myNodeNum")
    else:
        my_num = getattr(my_info, "myNodeNum", None)
    node = nodes_by_num.get(my_num, {}) if my_num is not None else {}
    local_stats = node.get("localStats", {}) or {}
    device_metrics = node.get("deviceMetrics", {}) or {}

    with state_lock:
        cached_nodes = nodes_list
    update_runtime(
        nodes_known=len(nodes_list),
        online_nodes=local_stats.get("numOnlineNodes"),
        channel_utilization=local_stats.get("channelUtilization", device_metrics.get("channelUtilization")),
        air_util_tx=local_stats.get("airUtilTx", device_metrics.get("airUtilTx")),
        battery_level=device_metrics.get("batteryLevel"),
        voltage=device_metrics.get("voltage"),
    )


def on_text(packet, interface=None):
    text = decode_text(packet)
    if not text:
        return
    from_id = packet.get("fromId", str(packet.get("from", "unknown")))
    to_id = packet.get("toId", str(packet.get("to", "^all")))
    save_message("in", from_id, to_id, text, packet)
    state = snapshot_runtime()
    update_runtime(
        messages_rx=state["messages_rx"] + 1,
        last_packet_at=now_iso(),
        last_text_at=now_iso(),
    )


def on_receive(packet, interface=None):
    state = snapshot_runtime()
    relay_seen = state["relay_seen"]
    multi_hop_seen = state["multi_hop_seen"]
    if packet.get("relayNode") is not None:
        relay_seen += 1
    hop_start = packet.get("hopStart")
    hop_limit = packet.get("hopLimit")
    try:
        if hop_start is not None and hop_limit is not None and int(hop_start) > int(hop_limit):
            multi_hop_seen += 1
    except Exception:
        pass
    update_runtime(
        packets_rx_seen=state["packets_rx_seen"] + 1,
        relay_seen=relay_seen,
        multi_hop_seen=multi_hop_seen,
        last_packet_at=now_iso(),
    )


def on_connection_established(interface, topic=pub.AUTO_TOPIC):
    logger.info("Meshtastic connection established")
    update_runtime(
        backend_connected=True,
        connection_detail="connected",
        last_connect_at=now_iso(),
        last_error=None,
    )
    try:
        refresh_cache_from_iface()
    except Exception:
        pass


def on_connection_lost(interface=None, topic=pub.AUTO_TOPIC):
    logger.warning("Meshtastic connection lost")
    update_runtime(
        backend_connected=False,
        connection_detail="connection lost",
        last_disconnect_at=now_iso(),
    )
    close_iface()


def close_iface():
    global iface
    with iface_lock:
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass
            iface = None


def connect_once() -> bool:
    global iface, connected_once
    logger.info("Connecting to Meshtastic via %s -> %s", APP_MODE, APP_TARGET)
    with iface_lock:
        if APP_MODE == "serial":
            iface = meshtastic.serial_interface.SerialInterface(devPath=APP_TARGET)
        else:
            iface = meshtastic.tcp_interface.TCPInterface(hostname=APP_TARGET)
    connected_once = True
    update_runtime(backend_connected=True, connection_detail="connected", last_error=None)
    refresh_cache_from_iface()
    logger.info("Connected to Meshtastic via %s -> %s", APP_MODE, APP_TARGET)
    return True


def connector_worker():
    backoff = 3
    while not stop_event.is_set():
        try:
            if iface is None:
                connect_once()
            refresh_cache_from_iface()
            time.sleep(5)
        except Exception as exc:
            update_runtime(
                backend_connected=False,
                connection_detail="reconnecting",
                last_error=str(exc),
                last_disconnect_at=now_iso(),
            )
            logger.warning("Meshtastic connector issue: %s", exc)
            close_iface()
            stop_event.wait(backoff)
            backoff = min(backoff + 2, 15)
        else:
            backoff = 3


@app.route("/")
def index():
    return render_template("index.html", version=VERSION)


@app.route("/api/health")
def api_health():
    state = snapshot_runtime()
    return jsonify(
        {
            "ok": True,
            "version": VERSION,
            "backend_connected": state["backend_connected"],
            "connection_detail": state["connection_detail"],
            "last_error": state["last_error"],
            "last_connect_at": state["last_connect_at"],
            "last_disconnect_at": state["last_disconnect_at"],
            "last_packet_at": state["last_packet_at"],
            "mode": APP_MODE,
            "target": APP_TARGET,
        }
    )


@app.route("/api/status")
def api_status():
    state = snapshot_runtime()
    return jsonify({
        "ok": True,
        "version": VERSION,
        "mode": APP_MODE,
        "target": APP_TARGET,
        "channel": channel_index,
        **state,
    })


@app.route("/api/stats")
def api_stats():
    return jsonify(snapshot_runtime())


@app.route("/api/messages")
def api_messages():
    limit = min(int(request.args.get("limit", 100)), 500)
    return jsonify(load_messages(limit))


@app.route("/api/nodes")
def api_nodes():
    with state_lock:
        return jsonify(list(cached_nodes))


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    dest = (data.get("dest") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message"}), 400

    with iface_lock:
        local_iface = iface
    if local_iface is None:
        return jsonify({"ok": False, "error": "Backend not connected to node"}), 503

    try:
        kwargs = {"text": text, "wantAck": False, "channelIndex": channel_index}
        if dest:
            kwargs["destinationId"] = dest
        local_iface.sendText(**kwargs)
    except Exception as exc:
        update_runtime(last_error=str(exc))
        return jsonify({"ok": False, "error": str(exc)}), 500

    to_id = dest or "^all"
    msg = save_message("out", "io", to_id, text)
    state = snapshot_runtime()
    update_runtime(messages_tx=state["messages_tx"] + 1, last_text_at=now_iso())
    return jsonify({"ok": True, "message": msg})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    with storage_lock:
        conn = db_connect()
        cur = conn.cursor()
        cur.execute("DELETE FROM messages")
        conn.commit()
        conn.close()
    with state_lock:
        recent_messages.clear()
    return jsonify({"ok": True})


def stop_handler(signum, frame):
    stop_event.set()
    close_iface()
    os._exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Meshtastic Web Chat (single-service)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--node-port", help="Serial port of the node, e.g. /dev/ttyUSB0")
    group.add_argument("--node-host", help="IP/hostname of the node, e.g. 192.168.0.18")
    # backward compatibility
    group.add_argument("--port", help=argparse.SUPPRESS)
    group.add_argument("--host", help=argparse.SUPPRESS)
    parser.add_argument("--listen-host", default="127.0.0.1", help="HTTP listen host")
    parser.add_argument("--listen-port", type=int, default=8088, help="HTTP listen port")
    parser.add_argument("--channel", type=int, default=0, help="Meshtastic channel index")
    parser.add_argument("--ssl-adhoc", action="store_true", help="Enable Flask adhoc HTTPS")
    args = parser.parse_args()

    node_port = args.node_port or args.port
    node_host = args.node_host or args.host
    APP_MODE = "serial" if node_port else "tcp"
    APP_TARGET = node_port or node_host
    channel_index = args.channel

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    init_db()
    pub.subscribe(on_text, "meshtastic.receive.text")
    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_connection_established, "meshtastic.connection.established")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")

    threading.Thread(target=connector_worker, daemon=True, name="meshtastic-connector").start()

    logger.info("Meshtastic Web Chat v%s starting", VERSION)
    logger.info("Node backend: %s -> %s", APP_MODE, APP_TARGET)
    logger.info("Web UI: %s://%s:%s", "https" if args.ssl_adhoc else "http", args.listen_host, args.listen_port)

    ssl_context = "adhoc" if args.ssl_adhoc else None
    app.run(host=args.listen_host, port=args.listen_port, threaded=True, ssl_context=ssl_context)
