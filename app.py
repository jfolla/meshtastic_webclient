#!/usr/bin/env python3
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

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "messages.db"
LOG_JSONL = BASE_DIR / "messages.jsonl"

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

iface = None
iface_lock = threading.Lock()
recent_lock = threading.Lock()
stats_lock = threading.Lock()
recent_messages = []
MAX_RECENT = 300
running = True
channel_index = 0

STAT_KEYS = {
    "started_at": "",
    "rx_packets_total": 0,
    "rx_text_messages": 0,
    "tx_text_messages": 0,
    "rx_relay_seen": 0,
    "rx_multihop_seen": 0,
    "rx_bad_text": 0,
    "tx_failed": 0,
    "last_packet_at": "",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_conn()
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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS kv_stats (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    for key, default in STAT_KEYS.items():
        cur.execute(
            "INSERT OR IGNORE INTO kv_stats (key, value) VALUES (?, ?)",
            (key, str(default if key != "started_at" else now_iso())),
        )
    conn.commit()
    conn.close()



def load_stats():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM kv_stats")
    rows = cur.fetchall()
    conn.close()
    out = dict(STAT_KEYS)
    for key, value in rows:
        if key == "started_at":
            out[key] = value
        else:
            try:
                out[key] = int(value)
            except ValueError:
                out[key] = 0
    return out



def set_stat(key: str, value):
    with stats_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO kv_stats (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        conn.commit()
        conn.close()



def incr_stat(key: str, amount: int = 1):
    with stats_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT value FROM kv_stats WHERE key = ?", (key,))
        row = cur.fetchone()
        current = 0
        if row:
            try:
                current = int(row[0])
            except ValueError:
                current = 0
        current += amount
        cur.execute(
            "INSERT INTO kv_stats (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(current)),
        )
        conn.commit()
        conn.close()
        return current



def message_count() -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM messages")
    count = cur.fetchone()[0]
    conn.close()
    return count



def save_message(direction: str, from_id: str, to_id: str, text: str, raw_packet: Optional[dict] = None):
    payload = {
        "ts": now_iso(),
        "direction": direction,
        "from_id": from_id,
        "to_id": to_id,
        "text": text,
    }
    raw_json = json.dumps(raw_packet, ensure_ascii=False, default=str) if raw_packet is not None else None

    conn = get_conn()
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



def packet_is_multihop(packet: dict) -> bool:
    hop_start = packet.get("hopStart")
    hop_limit = packet.get("hopLimit")
    return isinstance(hop_start, int) and isinstance(hop_limit, int) and hop_start > hop_limit



def packet_has_relay(packet: dict) -> bool:
    relay_node = packet.get("relayNode")
    if relay_node not in (None, "", 0):
        return True
    return packet_is_multihop(packet)



def on_receive(packet, interface=None):
    incr_stat("rx_packets_total")
    set_stat("last_packet_at", now_iso())

    if packet_has_relay(packet):
        incr_stat("rx_relay_seen")
    if packet_is_multihop(packet):
        incr_stat("rx_multihop_seen")

    decoded = packet.get("decoded", {})
    portnum = str(decoded.get("portnum", ""))
    if "TEXT" in portnum and not decoded.get("text"):
        payload = decoded.get("payload")
        if payload not in (None, b"", ""):
            incr_stat("rx_bad_text")



def on_text(packet, interface=None):
    text = decode_text(packet)
    if not text:
        return
    from_id = packet.get("fromId", str(packet.get("from", "unknown")))
    to_id = packet.get("toId", str(packet.get("to", "^all")))
    incr_stat("rx_text_messages")
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





def get_health_status():
    '''Best-effort health probe for the local Meshtastic connection.
    This is not an RF ping; it verifies that the backend can still talk to the node.
    '''
    started = datetime.now()
    ok = False
    detail = "non raggiungibile"
    node_num = None
    node_name = None

    try:
        with iface_lock:
            if iface is None:
                raise RuntimeError("interfaccia non inizializzata")

            my_info = getattr(iface, "myInfo", None)
            nodes = getattr(iface, "nodes", {}) or {}
            nodes_by_num = getattr(iface, "nodesByNum", {}) or {}

            if isinstance(my_info, dict):
                node_num = my_info.get("myNodeNum")
            else:
                node_num = getattr(my_info, "myNodeNum", None)

            if node_num is not None and node_num in nodes_by_num:
                node = nodes_by_num.get(node_num, {}) or {}
                user = node.get("user", {}) or {}
                node_name = user.get("longName") or user.get("shortName")
            elif nodes:
                first_node = next(iter(nodes.values()), {}) or {}
                user = first_node.get("user", {}) or {}
                node_name = user.get("longName") or user.get("shortName")

            ok = bool(my_info) or bool(nodes)
            detail = "raggiungibile" if ok else "nessuna risposta valida"
    except Exception as e:
        detail = str(e)

    elapsed_ms = round((datetime.now() - started).total_seconds() * 1000, 1)
    last_packet_at = load_stats().get("last_packet_at")
    return {
        "online": ok,
        "probe_latency_ms": elapsed_ms,
        "detail": detail,
        "node_num": node_num,
        "node_name": node_name,
        "last_packet_at": last_packet_at,
    }

def get_local_mesh_stats():
    with iface_lock:
        try:
            my_num = getattr(iface, "myInfo", {}).get("myNodeNum")
            node = getattr(iface, "nodesByNum", {}).get(my_num, {}) if my_num is not None else {}
            local_stats = node.get("localStats", {}) or {}
            device_metrics = node.get("deviceMetrics", {}) or {}
        except Exception:
            local_stats = {}
            device_metrics = {}

    def val(key):
        return local_stats.get(key)

    return {
        "uptime_seconds": val("uptimeSeconds"),
        "channel_utilization": local_stats.get("channelUtilization", device_metrics.get("channelUtilization")),
        "air_util_tx": local_stats.get("airUtilTx", device_metrics.get("airUtilTx")),
        "num_packets_tx": val("numPacketsTx"),
        "num_packets_rx": val("numPacketsRx"),
        "num_packets_rx_bad": val("numPacketsRxBad"),
        "num_tx_relay": val("numTxRelay"),
        "num_tx_relay_canceled": val("numTxRelayCanceled"),
        "num_tx_dropped": val("numTxDropped"),
        "num_rx_dupe": val("numRxDupe"),
        "num_online_nodes": val("numOnlineNodes"),
        "num_total_nodes": val("numTotalNodes"),
        "noise_floor": val("noiseFloor"),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "ok": True,
        "mode": APP_MODE,
        "target": APP_TARGET,
        "channel": channel_index,
    })


@app.route("/api/messages")
def api_messages():
    limit = min(int(request.args.get("limit", 100)), 500)
    conn = get_conn()
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


@app.route("/api/stats")
def api_stats():
    stored = load_stats()
    local = get_local_mesh_stats()
    nodes = get_nodes()
    return jsonify(
        {
            "started_at": stored.get("started_at"),
            "health": get_health_status(),
            "stored_messages": message_count(),
            "known_nodes": len(nodes),
            "received_messages": stored.get("rx_text_messages", 0),
            "sent_messages": stored.get("tx_text_messages", 0),
            "received_packets": stored.get("rx_packets_total", 0),
            "relayed_packets_seen": stored.get("rx_relay_seen", 0),
            "multihop_packets_seen": stored.get("rx_multihop_seen", 0),
            "bad_text_packets_seen": stored.get("rx_bad_text", 0),
            "send_failures": stored.get("tx_failed", 0),
            "local": local,
        }
    )


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
            incr_stat("tx_failed")
            return jsonify({"ok": False, "error": str(e)}), 500

    to_id = dest or "^all"
    incr_stat("tx_text_messages")
    msg = save_message("out", "io", to_id, text)
    return jsonify({"ok": True, "message": msg})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    conn = get_conn()
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

    print(f"Web chat pronta su http://{args.listen_host}:{args.listen_port}")
    print(f"Backend Meshtastic: {APP_MODE} -> {APP_TARGET}")

    app.run(host=args.listen_host, port=args.listen_port, debug=False, threaded=True)
