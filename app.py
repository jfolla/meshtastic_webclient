#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import signal
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from flask import Flask, jsonify, render_template, request, send_file
from pubsub import pub
import meshtastic.serial_interface
import meshtastic.tcp_interface

VERSION = "0.3.5"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "messages.db"
LOG_JSONL = BASE_DIR / "messages.jsonl"
DEFAULT_CONFIG_PATH = BASE_DIR / "app_config.json"

DEFAULT_CONFIG = {
    "version": VERSION,
    "node": {
        "mode": "tcp",
        "host": "192.168.0.18",
        "port": "",
        "channel": 0,
    },
    "web": {
        "listen_host": "0.0.0.0",
        "listen_port": 8088,
        "ssl_adhoc": True,
    },
    "ui": {
        "default_language": "it",
    },
}

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config["JSON_AS_ASCII"] = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("meshtastic-webchat")

iface = None
iface_lock = threading.Lock()
config_lock = threading.Lock()
cache_lock = threading.Lock()
storage_lock = threading.Lock()
runtime_lock = threading.Lock()
running = True

current_config: dict[str, Any] = {}
reconnect_event = threading.Event()
outbound_queue: queue.Queue[dict[str, Any]] = queue.Queue()
recent_messages: deque[dict[str, Any]] = deque(maxlen=300)

runtime_state: dict[str, Any] = {
    "backend_connected": False,
    "connection_detail": "starting",
    "last_connect_at": None,
    "last_disconnect_at": None,
    "last_packet_at": None,
    "last_text_at": None,
    "last_sync_at": None,
    "last_error": None,
    "last_probe_ms": None,
    "connect_attempts": 0,
    "reconnects": 0,
    "rx_packets_total": 0,
    "rx_text_messages": 0,
    "tx_text_messages": 0,
    "rx_relay_seen": 0,
    "rx_multihop_seen": 0,
    "rx_bad_text": 0,
    "tx_failed": 0,
    "queue_depth": 0,
}

cached_nodes: list[dict[str, Any]] = []
cached_health: dict[str, Any] = {
    "backend_connected_to_node": False,
    "probe_latency_ms": None,
    "detail": "starting",
    "node_num": None,
    "node_name": None,
    "last_packet_at": None,
}
cached_local_stats: dict[str, Any] = {}


# ---------- config ----------
def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(base))
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            cfg = deep_merge(DEFAULT_CONFIG, loaded)
            cfg["version"] = VERSION
            return cfg
        except Exception as exc:
            logger.warning("Invalid config file %s: %s", path, exc)
    path.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(path: Path, cfg: dict[str, Any]) -> None:
    cfg = deep_merge(DEFAULT_CONFIG, cfg)
    cfg["version"] = VERSION
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def get_config_snapshot() -> dict[str, Any]:
    with config_lock:
        return json.loads(json.dumps(current_config))


def set_config(new_cfg: dict[str, Any]) -> None:
    global current_config
    with config_lock:
        current_config = deep_merge(DEFAULT_CONFIG, new_cfg)
        current_config["version"] = VERSION


# ---------- helpers ----------
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def jsonable(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, deque)):
        return [jsonable(v) for v in obj]
    return str(obj)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def update_runtime(**kwargs: Any) -> None:
    with runtime_lock:
        runtime_state.update(kwargs)
        runtime_state["queue_depth"] = outbound_queue.qsize()


def incr_stat(key: str, delta: int = 1) -> None:
    with runtime_lock:
        runtime_state[key] = int(runtime_state.get(key, 0)) + delta
        runtime_state["queue_depth"] = outbound_queue.qsize()


def snapshot_runtime() -> dict[str, Any]:
    with runtime_lock:
        out = dict(runtime_state)
    out["queue_depth"] = outbound_queue.qsize()
    return out


# ---------- storage ----------
def init_db() -> None:
    with storage_lock:
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
        conn.commit()
        conn.close()


def bootstrap_runtime_from_db() -> None:
    with storage_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM messages WHERE direction='in'")
        rx = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM messages WHERE direction='out'")
        tx = cur.fetchone()[0]
        conn.close()
    update_runtime(rx_text_messages=rx, tx_text_messages=tx)


def save_message(direction: str, from_id: str, to_id: str, text: str, raw_packet: Optional[dict] = None) -> dict[str, Any]:
    payload = {
        "ts": now_iso(),
        "direction": direction,
        "from_id": from_id,
        "to_id": to_id,
        "text": text,
    }
    raw_json = json.dumps(raw_packet, ensure_ascii=False, default=str) if raw_packet is not None else None
    with storage_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO messages (ts, direction, from_id, to_id, text, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
            (payload["ts"], direction, from_id, to_id, text, raw_json),
        )
        conn.commit()
        payload["id"] = cur.lastrowid
        conn.close()
    recent_messages.append(payload)
    with open(LOG_JSONL, "a", encoding="utf-8") as f:
        json.dump({**payload, "raw_packet": raw_packet}, f, ensure_ascii=False, default=str)
        f.write("\n")
    return payload


def load_messages(limit: int = 100) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    with storage_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, ts, direction, from_id, to_id, text FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
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


def clear_messages() -> None:
    with storage_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM messages")
        conn.commit()
        conn.close()
    recent_messages.clear()
    update_runtime(rx_text_messages=0, tx_text_messages=0)


def message_count() -> int:
    with storage_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM messages")
        count = cur.fetchone()[0]
        conn.close()
    return count


# ---------- packet parsing ----------
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


# ---------- meshtastic callbacks ----------
def on_receive(packet, interface=None):
    incr_stat("rx_packets_total")
    update_runtime(last_packet_at=now_iso())
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
    update_runtime(last_text_at=now_iso())
    save_message("in", from_id, to_id, text, packet)


def close_iface() -> None:
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
    global iface
    if mode == "serial":
        local = meshtastic.serial_interface.SerialInterface(devPath=target)
    else:
        local = meshtastic.tcp_interface.TCPInterface(hostname=target)
    with iface_lock:
        iface = local
    return local


# ---------- background workers ----------
def connection_worker() -> None:
    connected_once = False
    while running:
        cfg = get_config_snapshot()
        node_cfg = cfg["node"]
        mode = node_cfg.get("mode", "tcp")
        target = node_cfg.get("port") if mode == "serial" else node_cfg.get("host")

        if iface is None:
            try:
                incr_stat("connect_attempts")
                connect_meshtastic(mode, target)
                if connected_once:
                    incr_stat("reconnects")
                connected_once = True
                update_runtime(
                    backend_connected=True,
                    connection_detail=f"connected via {mode} -> {target}",
                    last_connect_at=now_iso(),
                    last_error=None,
                )
                logger.info("Connected to Meshtastic via %s -> %s", mode, target)
                reconnect_event.clear()
            except Exception as exc:
                update_runtime(
                    backend_connected=False,
                    connection_detail=f"connect failed via {mode} -> {target}",
                    last_error=str(exc),
                )
                logger.warning("Meshtastic connect failed via %s -> %s: %s", mode, target, exc)
                time.sleep(5)
                continue

        if reconnect_event.is_set():
            update_runtime(backend_connected=False, connection_detail="reconnecting")
            close_iface()
            reconnect_event.clear()
            time.sleep(1)
            continue

        time.sleep(1)


def refresh_cached_state() -> None:
    global cached_nodes, cached_health, cached_local_stats
    started = datetime.now()
    new_nodes = []
    new_health = {
        "backend_connected_to_node": False,
        "probe_latency_ms": None,
        "detail": snapshot_runtime().get("connection_detail"),
        "node_num": None,
        "node_name": None,
        "last_packet_at": snapshot_runtime().get("last_packet_at"),
    }
    new_local = {}

    local_iface = None
    with iface_lock:
        local_iface = iface
    if local_iface is None:
        with cache_lock:
            cached_nodes = []
            cached_health = new_health
            cached_local_stats = {}
        return

    try:
        nodes = getattr(local_iface, "nodes", {}) or {}
        nodes_by_num = getattr(local_iface, "nodesByNum", {}) or {}
        my_info = getattr(local_iface, "myInfo", {}) or {}
        if not isinstance(my_info, dict):
            my_info = {}
        for node_id, node in nodes.items():
            user = node.get("user", {})
            new_nodes.append(
                {
                    "node_id": node_id,
                    "name": user.get("longName") or user.get("shortName") or node_id,
                    "short_name": user.get("shortName") or "",
                    "hw_model": user.get("hwModel") or "",
                    "last_heard": node.get("lastHeard"),
                }
            )
        new_nodes.sort(key=lambda x: (x["name"] or "", x["node_id"]))

        node_num = my_info.get("myNodeNum")
        node = nodes_by_num.get(node_num, {}) if node_num is not None else {}
        user = node.get("user", {}) or {}
        local_stats = node.get("localStats", {}) or {}
        device_metrics = node.get("deviceMetrics", {}) or {}

        def val(key: str):
            return local_stats.get(key)

        new_health = {
            "backend_connected_to_node": bool(my_info) or bool(nodes),
            "probe_latency_ms": round((datetime.now() - started).total_seconds() * 1000, 1),
            "detail": "connected" if (bool(my_info) or bool(nodes)) else "no valid response",
            "node_num": node_num,
            "node_name": user.get("longName") or user.get("shortName"),
            "last_packet_at": snapshot_runtime().get("last_packet_at"),
        }
        new_local = {
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
            "battery_level": device_metrics.get("batteryLevel"),
            "voltage": device_metrics.get("voltage"),
        }
        update_runtime(backend_connected=True, connection_detail="connected", last_probe_ms=new_health["probe_latency_ms"], last_sync_at=now_iso())
    except Exception as exc:
        update_runtime(backend_connected=False, connection_detail="snapshot failed", last_error=str(exc))
        new_health["detail"] = str(exc)
        reconnect_event.set()

    with cache_lock:
        cached_nodes = new_nodes
        cached_health = new_health
        cached_local_stats = new_local


def cache_worker() -> None:
    while running:
        refresh_cached_state()
        time.sleep(5)


def sender_worker() -> None:
    while running:
        try:
            item = outbound_queue.get(timeout=1)
        except queue.Empty:
            continue
        try:
            local_iface = None
            with iface_lock:
                local_iface = iface
            if local_iface is None:
                raise RuntimeError("backend not connected")
            node_cfg = get_config_snapshot()["node"]
            kwargs = {
                "text": item["text"],
                "wantAck": False,
                "channelIndex": int(node_cfg.get("channel", 0)),
            }
            if item.get("dest"):
                kwargs["destinationId"] = item["dest"]
            local_iface.sendText(**kwargs)
            incr_stat("tx_text_messages")
            save_message("out", "io", item.get("dest") or "^all", item["text"])
        except Exception as exc:
            incr_stat("tx_failed")
            update_runtime(last_error=str(exc))
            logger.warning("Send failed: %s", exc)
        finally:
            outbound_queue.task_done()
            update_runtime(queue_depth=outbound_queue.qsize())


# ---------- API ----------
@app.route("/")
def index():
    return render_template("index.html", version=VERSION)


@app.route("/api/status")
def api_status():
    cfg = get_config_snapshot()
    return jsonify({
        "ok": True,
        "version": VERSION,
        "node": cfg["node"],
        "web": cfg["web"],
        "ui": cfg["ui"],
    })


@app.route("/api/messages")
def api_messages():
    limit = min(int(request.args.get("limit", 100)), 500)
    return jsonify(load_messages(limit=limit))


@app.route("/api/nodes")
def api_nodes():
    with cache_lock:
        return jsonify(jsonable(cached_nodes))


@app.route("/api/stats")
def api_stats():
    stored = snapshot_runtime()
    with cache_lock:
        local = dict(cached_local_stats)
        health = dict(cached_health)
        known_nodes = len(cached_nodes)
    return jsonify({
        "version": VERSION,
        "started_at": None,
        "stored_messages": message_count(),
        "known_nodes": known_nodes,
        "received_messages": stored.get("rx_text_messages", 0),
        "sent_messages": stored.get("tx_text_messages", 0),
        "received_packets": stored.get("rx_packets_total", 0),
        "relayed_packets_seen": stored.get("rx_relay_seen", 0),
        "multihop_packets_seen": stored.get("rx_multihop_seen", 0),
        "bad_text_packets_seen": stored.get("rx_bad_text", 0),
        "send_failures": stored.get("tx_failed", 0),
        "queue_depth": stored.get("queue_depth", 0),
        "health": health,
        "local": local,
    })


@app.route("/api/debug")
def api_debug():
    cfg = get_config_snapshot()
    with cache_lock:
        health = dict(cached_health)
        local = dict(cached_local_stats)
    return jsonify({
        "version": VERSION,
        "config": cfg,
        "runtime": snapshot_runtime(),
        "health": health,
        "local_stats": local,
        "recent_messages": list(recent_messages)[-20:],
    })


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    dest = (data.get("dest") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message"}), 400
    outbound_queue.put({"text": text, "dest": dest or None})
    update_runtime(queue_depth=outbound_queue.qsize())
    return jsonify({"ok": True, "queued": True})


@app.route("/api/clear", methods=["POST"])
def api_clear():
    clear_messages()
    return jsonify({"ok": True})


@app.route("/api/config/export")
def api_config_export():
    path = BASE_DIR / f"meshtastic_webchat_config_{VERSION}.json"
    path.write_text(json.dumps(get_config_snapshot(), indent=2, ensure_ascii=False), encoding="utf-8")
    return send_file(path, mimetype="application/json", as_attachment=True, download_name=path.name)


@app.route("/api/config/import", methods=["POST"])
def api_config_import():
    uploaded = request.files.get("file")
    if not uploaded:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    try:
        new_cfg = json.loads(uploaded.read().decode("utf-8"))
        old_cfg = get_config_snapshot()
        merged = deep_merge(DEFAULT_CONFIG, new_cfg)
        save_config(Path(app.config["CONFIG_PATH"]), merged)
        set_config(merged)

        restart_required = (
            old_cfg["web"]["listen_host"] != merged["web"]["listen_host"] or
            int(old_cfg["web"]["listen_port"]) != int(merged["web"]["listen_port"]) or
            bool(old_cfg["web"]["ssl_adhoc"]) != bool(merged["web"]["ssl_adhoc"])
        )
        reconnect_needed = (
            old_cfg["node"]["mode"] != merged["node"]["mode"] or
            old_cfg["node"].get("host") != merged["node"].get("host") or
            old_cfg["node"].get("port") != merged["node"].get("port") or
            int(old_cfg["node"].get("channel", 0)) != int(merged["node"].get("channel", 0))
        )
        if reconnect_needed:
            reconnect_event.set()
        return jsonify({
            "ok": True,
            "reconnect_needed": reconnect_needed,
            "restart_required": restart_required,
            "config": merged,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


# ---------- shutdown ----------
def stop_handler(signum, frame):
    global running
    running = False
    close_iface()
    os._exit(0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Meshtastic local web chat")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to app config JSON")
    parser.add_argument("--node-host", help="TCP host of node")
    parser.add_argument("--node-port", help="Serial port of node, e.g. /dev/ttyUSB0")
    parser.add_argument("--listen-host", help="HTTP listen host")
    parser.add_argument("--listen-port", type=int, help="HTTP listen port")
    parser.add_argument("--channel", type=int, help="Meshtastic channel index")
    parser.add_argument("--ssl-adhoc", action="store_true", help="Enable ad-hoc SSL")
    parser.add_argument("--ui-language", choices=["it", "en", "fr"], help="Default UI language")
    return parser.parse_args()


def build_effective_config(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    cfg_path = Path(args.config).expanduser().resolve()
    cfg = load_config(cfg_path)
    if args.node_host:
        cfg["node"]["mode"] = "tcp"
        cfg["node"]["host"] = args.node_host
        cfg["node"]["port"] = ""
    if args.node_port:
        cfg["node"]["mode"] = "serial"
        cfg["node"]["port"] = args.node_port
        cfg["node"]["host"] = ""
    if args.listen_host:
        cfg["web"]["listen_host"] = args.listen_host
    if args.listen_port:
        cfg["web"]["listen_port"] = args.listen_port
    if args.channel is not None:
        cfg["node"]["channel"] = args.channel
    if args.ssl_adhoc:
        cfg["web"]["ssl_adhoc"] = True
    if args.ui_language:
        cfg["ui"]["default_language"] = args.ui_language
    save_config(cfg_path, cfg)
    return cfg, cfg_path


if __name__ == "__main__":
    args = parse_args()
    cfg, cfg_path = build_effective_config(args)
    set_config(cfg)
    app.config["CONFIG_PATH"] = str(cfg_path)

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    init_db()
    bootstrap_runtime_from_db()
    pub.subscribe(on_text, "meshtastic.receive.text")
    pub.subscribe(on_receive, "meshtastic.receive")

    threading.Thread(target=connection_worker, daemon=True, name="connection-worker").start()
    threading.Thread(target=cache_worker, daemon=True, name="cache-worker").start()
    threading.Thread(target=sender_worker, daemon=True, name="sender-worker").start()

    web_cfg = cfg["web"]
    ssl_context = "adhoc" if web_cfg.get("ssl_adhoc") else None
    logger.info("Meshtastic Web Chat v%s starting", VERSION)
    logger.info("Web UI on %s://%s:%s", "https" if ssl_context else "http", web_cfg.get("listen_host"), web_cfg.get("listen_port"))
    logger.info("Node mode=%s target=%s", cfg["node"].get("mode"), cfg["node"].get("port") or cfg["node"].get("host"))

    app.run(
        host=web_cfg.get("listen_host", "0.0.0.0"),
        port=int(web_cfg.get("listen_port", 8088)),
        debug=False,
        threaded=True,
        ssl_context=ssl_context,
    )
