#!/usr/bin/env python3
import argparse
import json
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

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "messages.db"
LOG_JSONL = BASE_DIR / "messages.jsonl"

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

iface = None
iface_lock = threading.Lock()
recent_lock = threading.Lock()
running = True
channel_index = 0
APP_MODE = ""
APP_TARGET = ""
MAX_RECENT = 300
recent_messages = []

stats_lock = threading.Lock()
stats = {
    "messages_rx": 0,
    "messages_tx": 0,
    "packets_rx": 0,
    "relay_seen": 0,
    "multihop_seen": 0,
    "last_packet_at": None,
    "backend_online": False,
    "probe_latency_ms": None,
    "online_nodes": 0,
    "known_nodes": 0,
    "channel_utilization": None,
    "air_util_tx": None,
    "battery_level": None,
    "voltage": None,
    "tx_relay": None,
    "rx_bad": None,
    "tx_dropped": None,
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db():
    conn = sqlite3.connect(DB_PATH)
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

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (ts, direction, from_id, to_id, text, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
        (payload["ts"], direction, from_id, to_id, text, raw_json),
    )
    conn.commit()
    msg_id = cur.lastrowid
    conn.close()

    payload["id"] = msg_id
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


def _safe_get_localstats(nodes: dict):
    for _node_id, node in (nodes or {}).items():
        ls = node.get("localStats") or node.get("localstats")
        if ls:
            return ls
    return {}


def update_runtime_stats():
    with iface_lock:
        my_info = getattr(iface, "myInfo", None) if iface is not None else None
        nodes = getattr(iface, "nodes", {}) if iface is not None else {}

    started = time.time()
    backend_online = bool(my_info) or bool(nodes)
    probe_latency_ms = round((time.time() - started) * 1000, 1)

    online_nodes = 0
    known_nodes = 0
    chan_util = None
    air_util = None
    batt = None
    volt = None

    for _node_id, node in (nodes or {}).items():
        known_nodes += 1
        if node.get("lastHeard"):
            online_nodes += 1
        dm = node.get("deviceMetrics") or {}
        if chan_util is None and dm.get("channelUtilization") is not None:
            chan_util = dm.get("channelUtilization")
        if air_util is None and dm.get("airUtilTx") is not None:
            air_util = dm.get("airUtilTx")
        if batt is None and dm.get("batteryLevel") is not None:
            batt = dm.get("batteryLevel")
        if volt is None and dm.get("voltage") is not None:
            volt = dm.get("voltage")

    ls = _safe_get_localstats(nodes)

    with stats_lock:
        stats["backend_online"] = backend_online
        stats["probe_latency_ms"] = probe_latency_ms
        stats["online_nodes"] = online_nodes
        stats["known_nodes"] = known_nodes
        stats["channel_utilization"] = chan_util
        stats["air_util_tx"] = air_util
        stats["battery_level"] = batt
        stats["voltage"] = volt
        stats["tx_relay"] = ls.get("numTxRelay", stats.get("tx_relay")) if isinstance(ls, dict) else stats.get("tx_relay")
        stats["rx_bad"] = ls.get("numPacketsRxBad", stats.get("rx_bad")) if isinstance(ls, dict) else stats.get("rx_bad")
        stats["tx_dropped"] = ls.get("numTxDropped", stats.get("tx_dropped")) if isinstance(ls, dict) else stats.get("tx_dropped")


def on_receive(packet, interface=None):
    with stats_lock:
        stats["packets_rx"] += 1
        stats["last_packet_at"] = now_iso()
        if packet.get("relayNode") is not None:
            stats["relay_seen"] += 1
        if packet.get("hopStart") is not None and packet.get("hopLimit") is not None:
            try:
                if int(packet.get("hopStart", 0)) > int(packet.get("hopLimit", 0)):
                    stats["multihop_seen"] += 1
            except Exception:
                pass
    update_runtime_stats()


def on_text(packet, interface=None):
    text = decode_text(packet)
    if not text:
        return
    from_id = packet.get("fromId", str(packet.get("from", "unknown")))
    to_id = packet.get("toId", str(packet.get("to", "^all")))
    with stats_lock:
        stats["messages_rx"] += 1
    save_message("in", from_id, to_id, text, packet)
    update_runtime_stats()


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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    update_runtime_stats()
    with stats_lock:
        s = dict(stats)
    return jsonify({
        "ok": True,
        "mode": APP_MODE,
        "target": APP_TARGET,
        "channel": channel_index,
        "backend_online": s["backend_online"],
        "probe_latency_ms": s["probe_latency_ms"],
        "last_packet_at": s["last_packet_at"],
    })


@app.route("/api/stats")
def api_stats():
    update_runtime_stats()
    with stats_lock:
        return jsonify(dict(stats))


@app.route("/api/messages")
def api_messages():
    limit = min(int(request.args.get("limit", 100)), 500)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, ts, direction, from_id, to_id, text FROM messages ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    rows.reverse()
    return jsonify([
        {
            "id": r[0],
            "ts": r[1],
            "direction": r[2],
            "from_id": r[3],
            "to_id": r[4],
            "text": r[5],
        }
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
        return jsonify({"ok": False, "error": "Messaggio vuoto"}), 400

    with iface_lock:
        try:
            kwargs = {"text": text, "wantAck": False, "channelIndex": channel_index}
            if dest:
                kwargs["destinationId"] = dest
            iface.sendText(**kwargs)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    to_id = dest or "^all"
    with stats_lock:
        stats["messages_tx"] += 1
    msg = save_message("out", "io", to_id, text)
    update_runtime_stats()
    return jsonify({"ok": True, "message": msg})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM messages")
    conn.commit()
    conn.close()
    with recent_lock:
        recent_messages.clear()
    return jsonify({"ok": True})


def stop_handler(signum, frame):
    global running
    running = False
    try:
        if iface is not None:
            iface.close()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Meshtastic local web chat")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--port", help="Porta seriale, es. /dev/ttyUSB0")
    group.add_argument("--host", help="IP/hostname del nodo, es. 192.168.0.18")
    parser.add_argument("--listen-host", default="127.0.0.1", help="Host web locale")
    parser.add_argument("--listen-port", type=int, default=8088, help="Porta web locale")
    parser.add_argument("--channel", type=int, default=0, help="Indice canale")
    parser.add_argument("--ssl-adhoc", action="store_true", help="Abilita HTTPS con certificato self-signed adhoc di Flask")
    args = parser.parse_args()

    APP_MODE = "serial" if args.port else "tcp"
    APP_TARGET = args.port or args.host
    channel_index = args.channel

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    init_db()
    pub.subscribe(on_text, "meshtastic.receive.text")
    pub.subscribe(on_receive, "meshtastic.receive")
    connect_meshtastic(APP_MODE, APP_TARGET)
    update_runtime_stats()

    scheme = "https" if args.ssl_adhoc else "http"
    print(f"Web chat pronta su {scheme}://{args.listen_host}:{args.listen_port}")
    if args.ssl_adhoc:
        app.run(host=args.listen_host, port=args.listen_port, ssl_context="adhoc", debug=False)
    else:
        app.run(host=args.listen_host, port=args.listen_port, debug=False)
