#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import socket
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_file

VERSION = "0.6.0"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "webchat_cache.db"
CONFIG_PATH = BASE_DIR / "app_config.json"

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("meshtastic_webchat")

cache_lock = threading.Lock()
proxy_cache: dict[str, Any] = {
    "status": {
        "version": VERSION,
        "backend_connected": False,
        "proxy_connected": False,
        "proxy_error": "not initialized",
        "last_sync": None,
        "proxy_host": "127.0.0.1",
        "proxy_port": 4404,
    },
    "state": {},
    "nodes": [],
}
store_lock = threading.Lock()
stop_event = threading.Event()
config_path_runtime = CONFIG_PATH


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_config(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            direction TEXT NOT NULL,
            from_id TEXT,
            to_id TEXT,
            text TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def db_add_message(msg: dict[str, Any]):
    with store_lock:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute(
            "INSERT INTO messages (ts, direction, from_id, to_id, text) VALUES (?, ?, ?, ?, ?)",
            (
                msg.get("ts") or now_iso(),
                msg.get("direction") or "in",
                msg.get("from_id"),
                msg.get("to_id"),
                msg.get("text") or "",
            ),
        )
        conn.commit()
        conn.close()


def db_list_messages(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    with store_lock:
        conn = sqlite3.connect(DB_PATH, timeout=10)
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


def proxy_request(payload: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
    with cache_lock:
        host = proxy_cache["status"]["proxy_host"]
        port = int(proxy_cache["status"]["proxy_port"])
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    if not data:
        raise RuntimeError("empty response from proxy")
    return json.loads(data.decode("utf-8", errors="replace").strip())


def sync_proxy_loop():
    last_seen_ids: set[int] = set()
    while not stop_event.is_set():
        try:
            state_resp = proxy_request({"type": "get_state"})
            nodes_resp = proxy_request({"type": "get_nodes"})
            msgs_resp = proxy_request({"type": "get_messages", "limit": 100})
            state = state_resp.get("state", {})
            nodes = nodes_resp.get("nodes", [])
            messages = msgs_resp.get("messages", [])

            for msg in messages:
                msg_id = msg.get("id")
                if isinstance(msg_id, int) and msg_id not in last_seen_ids:
                    db_add_message(msg)
                    last_seen_ids.add(msg_id)
            if len(last_seen_ids) > 1000:
                last_seen_ids = set(sorted(last_seen_ids)[-500:])

            with cache_lock:
                proxy_cache["state"] = state
                proxy_cache["nodes"] = nodes
                proxy_cache["status"]["proxy_connected"] = True
                proxy_cache["status"]["backend_connected"] = bool(state.get("upstream_connected"))
                proxy_cache["status"]["proxy_error"] = None
                proxy_cache["status"]["last_sync"] = now_iso()
        except Exception as exc:
            with cache_lock:
                proxy_cache["status"]["proxy_connected"] = False
                proxy_cache["status"]["backend_connected"] = False
                proxy_cache["status"]["proxy_error"] = str(exc)
                proxy_cache["status"]["last_sync"] = now_iso()
            logger.warning("Proxy poll failed: %s", exc)
        time.sleep(3)


@app.route("/")
def index():
    return render_template("index.html", version=VERSION)


@app.route("/api/status")
def api_status():
    with cache_lock:
        return jsonify(proxy_cache["status"])


@app.route("/api/state")
def api_state():
    with cache_lock:
        return jsonify(proxy_cache["state"])


@app.route("/api/nodes")
def api_nodes():
    with cache_lock:
        return jsonify(proxy_cache["nodes"])


@app.route("/api/messages")
def api_messages():
    limit = int(request.args.get("limit", 100))
    return jsonify(db_list_messages(limit=limit))


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    dest = (data.get("dest") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message"}), 400
    try:
        payload = {"type": "send_text", "text": text}
        if dest:
            payload["dest"] = dest
        resp = proxy_request(payload, timeout=4.0)
        if resp.get("type") == "error":
            return jsonify({"ok": False, "error": resp.get("error")}), 500
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/clear", methods=["POST"])
def api_clear():
    with store_lock:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("DELETE FROM messages")
        conn.commit()
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/debug")
def api_debug():
    with cache_lock:
        status = dict(proxy_cache["status"])
        state = dict(proxy_cache["state"])
    return jsonify({"version": VERSION, "status": status, "state": state})


@app.route("/api/config/export")
def api_config_export():
    return send_file(config_path_runtime, as_attachment=True, download_name="app_config.json")


@app.route("/api/config/import", methods=["POST"])
def api_config_import():
    f = request.files.get("file")
    if f is None:
        return jsonify({"ok": False, "error": "Missing file"}), 400
    raw = f.read()
    try:
        cfg = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Invalid JSON: {exc}"}), 400
    if not isinstance(cfg, dict) or "node" not in cfg or "proxy" not in cfg or "web" not in cfg:
        return jsonify({"ok": False, "error": "Invalid configuration structure"}), 400
    with open(config_path_runtime, "w", encoding="utf-8") as out:
        json.dump(cfg, out, indent=2, ensure_ascii=False)
    return jsonify({"ok": True, "message": "Configuration imported. Restart the service to apply changes."})


def main():
    parser = argparse.ArgumentParser(description="Meshtastic Web Chat")
    parser.add_argument("--config", required=True, help="Path to app_config.json")
    args = parser.parse_args()

    global config_path_runtime
    config_path_runtime = Path(args.config).resolve()
    cfg = load_config(config_path_runtime)

    proxy_host = cfg["proxy"]["host"]
    proxy_port = int(cfg["proxy"]["port"])
    listen_host = cfg["web"]["listen_host"]
    listen_port = int(cfg["web"]["listen_port"])
    ssl_adhoc = bool(cfg["web"].get("ssl_adhoc", False))

    with cache_lock:
        proxy_cache["status"]["proxy_host"] = proxy_host
        proxy_cache["status"]["proxy_port"] = proxy_port

    init_db()
    threading.Thread(target=sync_proxy_loop, daemon=True, name="proxy-sync").start()

    logger.info("Starting web chat on %s:%s (proxy %s:%s)", listen_host, listen_port, proxy_host, proxy_port)
    if ssl_adhoc:
        app.run(host=listen_host, port=listen_port, ssl_context="adhoc", debug=False, use_reloader=False)
    else:
        app.run(host=listen_host, port=listen_port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
