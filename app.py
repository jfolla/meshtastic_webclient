#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import queue
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request
from pubsub import pub
import meshtastic.serial_interface
import meshtastic.tcp_interface

VERSION = "0.3.3"

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "messages.db"
LOG_JSONL = BASE_DIR / "messages.jsonl"

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
logger = logging.getLogger("meshtastic_webchat")

iface = None
iface_lock = threading.Lock()
db_lock = threading.Lock()
cache_lock = threading.Lock()
outbound_queue: "queue.Queue[dict]" = queue.Queue()
running = True

APP_MODE = None
APP_TARGET = None
CHANNEL_INDEX = 0


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class RuntimeState:
    backend_connected: bool = False
    connection_detail: str = "disconnected"
    connection_mode: str = ""
    connection_target: str = ""
    messages_rx: int = 0
    messages_tx: int = 0
    send_failures: int = 0
    packets_rx_seen: int = 0
    relay_seen: int = 0
    multi_hop_seen: int = 0
    bad_packets_seen: int = 0
    last_packet_at: str = ""
    last_text_at: str = ""
    last_error: Optional[str] = None
    last_connect_at: str = ""
    last_disconnect_at: str = ""
    backend_probe_latency_ms: Optional[float] = None
    online_nodes: int = 0
    total_nodes: int = 0
    channel_utilization: Optional[float] = None
    air_util_tx: Optional[float] = None
    battery_level: Optional[int] = None
    voltage: Optional[float] = None
    num_tx_relay: Optional[int] = None
    num_rx_bad: Optional[int] = None
    num_tx_dropped: Optional[int] = None
    node_name: str = ""
    node_id: str = ""


runtime = RuntimeState()
cached_nodes: list[dict] = []


def _connect_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    with db_lock:
        conn = _connect_db()
        conn.execute(
            '''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                direction TEXT NOT NULL,
                from_id TEXT,
                to_id TEXT,
                text TEXT NOT NULL,
                raw_json TEXT
            )
            '''
        )
        conn.commit()
        conn.close()


def save_message(direction: str, from_id: str, to_id: str, text: str, raw_packet: Optional[dict] = None):
    ts = now_iso()
    raw_json = json.dumps(raw_packet, ensure_ascii=False, default=str) if raw_packet is not None else None
    with db_lock:
        conn = _connect_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (ts, direction, from_id, to_id, text, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, direction, from_id, to_id, text, raw_json),
        )
        conn.commit()
        msg_id = cur.lastrowid
        conn.close()

    payload = {
        "id": msg_id,
        "ts": ts,
        "direction": direction,
        "from_id": from_id,
        "to_id": to_id,
        "text": text,
    }
    with open(LOG_JSONL, "a", encoding="utf-8") as f:
        json.dump({**payload, "raw_packet": raw_packet}, f, ensure_ascii=False, default=str)
        f.write("\n")
    return payload


def load_messages(limit: int = 200):
    limit = max(1, min(limit, 500))
    with db_lock:
        conn = _connect_db()
        rows = conn.execute(
            "SELECT id, ts, direction, from_id, to_id, text FROM messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
    rows.reverse()
    return [
        {
            "id": r[0],
            "ts": r[1],
            "direction": r[2],
            "from_id": r[3],
            "to_id": r[4],
            "text": r[5],
        }
        for r in rows
    ]


def clear_messages():
    with db_lock:
        conn = _connect_db()
        conn.execute("DELETE FROM messages")
        conn.commit()
        conn.close()


def update_runtime(**kwargs):
    with cache_lock:
        for k, v in kwargs.items():
            setattr(runtime, k, v)


def snapshot_runtime():
    with cache_lock:
        return asdict(runtime)


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
    update_runtime(
        messages_rx=state["messages_rx"] + 1,
        last_packet_at=now_iso(),
        last_text_at=now_iso(),
    )


def on_receive(packet, interface=None):
    state = snapshot_runtime()
    relay_seen = state["relay_seen"]
    multi_hop_seen = state["multi_hop_seen"]
    bad_packets_seen = state["bad_packets_seen"]

    if packet.get("relayNode") is not None:
        relay_seen += 1

    hop_start = packet.get("hopStart")
    hop_limit = packet.get("hopLimit")
    try:
        if hop_start is not None and hop_limit is not None and int(hop_start) > int(hop_limit):
            multi_hop_seen += 1
    except Exception:
        pass

    if packet.get("rxSnr") is None and packet.get("rxRssi") is None and packet.get("decoded") is None:
        bad_packets_seen += 1

    update_runtime(
        packets_rx_seen=state["packets_rx_seen"] + 1,
        relay_seen=relay_seen,
        multi_hop_seen=multi_hop_seen,
        bad_packets_seen=bad_packets_seen,
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
        local = iface
        iface = None
    if local is not None:
        try:
            local.close()
        except Exception:
            pass


def connect_meshtastic(mode: str, target: str):
    logger.info("Connecting to Meshtastic via %s -> %s", mode, target)
    if mode == "serial":
        return meshtastic.serial_interface.SerialInterface(devPath=target)
    return meshtastic.tcp_interface.TCPInterface(hostname=target)


def connector_loop():
    global iface
    backoff = 3
    while running:
        try:
            if iface is None:
                update_runtime(connection_detail="connecting")
                new_iface = connect_meshtastic(APP_MODE, APP_TARGET)
                with iface_lock:
                    iface = new_iface
                update_runtime(
                    backend_connected=True,
                    connection_detail="connected",
                    connection_mode=APP_MODE,
                    connection_target=APP_TARGET,
                    last_connect_at=now_iso(),
                    last_error=None,
                )
                logger.info("Connected to Meshtastic via %s -> %s", APP_MODE, APP_TARGET)
                backoff = 3
            time.sleep(2)
        except Exception as exc:
            logger.warning("Meshtastic connector loop error: %s", exc)
            update_runtime(
                backend_connected=False,
                connection_detail="reconnect pending",
                last_error=str(exc),
                last_disconnect_at=now_iso(),
            )
            close_iface()
            time.sleep(backoff)
            backoff = min(backoff * 2, 20)


def cache_worker():
    global cached_nodes
    while running:
        started = time.time()
        nodes_out = []
        local_updates = {}
        try:
            with iface_lock:
                local_iface = iface

            if local_iface is not None:
                nodes = getattr(local_iface, "nodes", {}) or {}
                my_info = getattr(local_iface, "myInfo", {}) or {}
                nodes_by_num = getattr(local_iface, "nodesByNum", {}) or {}
                my_num = my_info.get("myNodeNum") if isinstance(my_info, dict) else getattr(my_info, "myNodeNum", None)

                for node_id, node in nodes.items():
                    user = node.get("user", {}) or {}
                    nodes_out.append(
                        {
                            "node_id": node_id,
                            "name": user.get("longName") or user.get("shortName") or node_id,
                            "short_name": user.get("shortName") or "",
                            "hw_model": user.get("hwModel") or "",
                            "last_heard": node.get("lastHeard"),
                        }
                    )
                nodes_out.sort(key=lambda x: (x["name"], x["node_id"]))

                if my_num is not None and my_num in nodes_by_num:
                    node = nodes_by_num.get(my_num, {}) or {}
                    user = node.get("user", {}) or {}
                    metrics = node.get("deviceMetrics", {}) or {}
                    local_stats = node.get("localStats", {}) or {}
                    local_updates = {
                        "node_name": user.get("longName") or user.get("shortName") or "",
                        "node_id": user.get("id") or "",
                        "battery_level": metrics.get("batteryLevel"),
                        "voltage": metrics.get("voltage"),
                        "channel_utilization": local_stats.get("channelUtilization", metrics.get("channelUtilization")),
                        "air_util_tx": local_stats.get("airUtilTx", metrics.get("airUtilTx")),
                        "online_nodes": local_stats.get("numOnlineNodes", len(nodes_out)),
                        "total_nodes": local_stats.get("numTotalNodes", len(nodes_out)),
                        "num_tx_relay": local_stats.get("numTxRelay"),
                        "num_rx_bad": local_stats.get("numPacketsRxBad"),
                        "num_tx_dropped": local_stats.get("numTxDropped"),
                    }

                update_runtime(
                    backend_probe_latency_ms=round((time.time() - started) * 1000, 1),
                    connection_detail="connected",
                    backend_connected=True,
                    **local_updates,
                )
        except Exception as exc:
            logger.warning("Cache refresh issue: %s", exc)
            update_runtime(
                backend_connected=False,
                connection_detail="cache refresh failed",
                last_error=str(exc),
            )

        with cache_lock:
            cached_nodes = nodes_out
        time.sleep(5)


def sender_worker():
    while running:
        try:
            item = outbound_queue.get(timeout=1)
        except queue.Empty:
            continue

        text = item["text"]
        dest = item.get("dest") or ""
        try:
            with iface_lock:
                local_iface = iface
                if local_iface is None:
                    raise RuntimeError("Meshtastic node not connected")
                kwargs = {"text": text, "wantAck": False, "channelIndex": CHANNEL_INDEX}
                if dest:
                    kwargs["destinationId"] = dest
                local_iface.sendText(**kwargs)
            to_id = dest or "^all"
            save_message("out", "io", to_id, text)
            state = snapshot_runtime()
            update_runtime(messages_tx=state["messages_tx"] + 1, last_packet_at=now_iso())
        except Exception as exc:
            state = snapshot_runtime()
            update_runtime(send_failures=state["send_failures"] + 1, last_error=str(exc))
            logger.warning("Send failed: %s", exc)


def stop_handler(signum, frame):
    global running
    running = False
    close_iface()


@app.route("/")
def index():
    return render_template("index.html", version=VERSION)


@app.route("/api/status")
def api_status():
    state = snapshot_runtime()
    return jsonify(
        {
            "ok": True,
            "version": VERSION,
            "mode": APP_MODE,
            "target": APP_TARGET,
            "channel": CHANNEL_INDEX,
            "backend_connected": state["backend_connected"],
            "connection_detail": state["connection_detail"],
        }
    )


@app.route("/api/messages")
def api_messages():
    limit = request.args.get("limit", default=200, type=int)
    return jsonify(load_messages(limit))


@app.route("/api/nodes")
def api_nodes():
    with cache_lock:
        nodes = list(cached_nodes)
    return jsonify(nodes)


@app.route("/api/stats")
def api_stats():
    return jsonify(snapshot_runtime())


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    dest = (data.get("dest") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message"}), 400
    outbound_queue.put({"text": text, "dest": dest})
    return jsonify({"ok": True, "queued": True})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    clear_messages()
    return jsonify({"ok": True})


def main():
    global APP_MODE, APP_TARGET, CHANNEL_INDEX

    parser = argparse.ArgumentParser(description="Meshtastic Web Chat")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--node-port", help="Serial port, for example /dev/ttyUSB0")
    group.add_argument("--node-host", help="Node IP/hostname, for example 192.168.0.18")
    parser.add_argument("--listen-host", default="127.0.0.1", help="Local web bind host")
    parser.add_argument("--listen-port", type=int, default=8088, help="Local web bind port")
    parser.add_argument("--channel", type=int, default=0, help="Meshtastic channel index")
    parser.add_argument("--ssl-adhoc", action="store_true", help="Enable simple self-signed HTTPS")
    args = parser.parse_args()

    APP_MODE = "serial" if args.node_port else "tcp"
    APP_TARGET = args.node_port or args.node_host
    CHANNEL_INDEX = args.channel

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    init_db()
    pub.subscribe(on_text, "meshtastic.receive.text")
    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_connection_established, "meshtastic.connection.established")
    pub.subscribe(on_connection_lost, "meshtastic.connection.lost")

    threading.Thread(target=connector_loop, name="meshtastic-connector", daemon=True).start()
    threading.Thread(target=cache_worker, name="meshtastic-cache", daemon=True).start()
    threading.Thread(target=sender_worker, name="meshtastic-sender", daemon=True).start()

    logger.info("Meshtastic Web Chat v%s ready", VERSION)
    logger.info("Listen on %s:%s", args.listen_host, args.listen_port)
    logger.info("Backend node: %s -> %s", APP_MODE, APP_TARGET)

    ssl_context = "adhoc" if args.ssl_adhoc else None
    if args.ssl_adhoc:
        try:
            import OpenSSL  # noqa: F401
        except Exception:
            logger.warning("PyOpenSSL not available, HTTPS adhoc may fail until installed")

    app.run(host=args.listen_host, port=args.listen_port, debug=False, threaded=True, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
