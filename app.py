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

VERSION = "0.6.9"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "webchat_cache.db"
CONFIG_PATH = BASE_DIR / "app_config.json"
ADDRESS_BOOK_PATH = BASE_DIR / "address_book.json"

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
address_book_lock = threading.Lock()
stop_event = threading.Event()
config_path_runtime = CONFIG_PATH
address_book_cache: dict[str, dict[str, Any]] = {}


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


def init_address_book():
    global address_book_cache
    if not ADDRESS_BOOK_PATH.exists():
        ADDRESS_BOOK_PATH.write_text("{}\n", encoding="utf-8")
    with address_book_lock:
        try:
            data = json.loads(ADDRESS_BOOK_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                address_book_cache = data
            else:
                address_book_cache = {}
        except Exception:
            address_book_cache = {}


def save_address_book():
    with address_book_lock:
        ADDRESS_BOOK_PATH.write_text(json.dumps(address_book_cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def get_alias_entry(node_id: str | None) -> dict[str, Any] | None:
    if not node_id:
        return None
    with address_book_lock:
        return dict(address_book_cache.get(node_id, {})) if node_id in address_book_cache else None


def resolve_label(node_id: str | None, fallback_name: str | None = None) -> str:
    entry = get_alias_entry(node_id)
    if entry and entry.get("alias"):
        return str(entry["alias"])
    if fallback_name:
        return str(fallback_name)
    return str(node_id or "")


def address_book_list() -> list[dict[str, Any]]:
    with address_book_lock:
        items = []
        for node_id, entry in address_book_cache.items():
            items.append(
                {
                    "node_id": node_id,
                    "alias": entry.get("alias", ""),
                    "notes": entry.get("notes", ""),
                    "updated_at": entry.get("updated_at", ""),
                }
            )
        items.sort(key=lambda x: (x["alias"].lower() if x["alias"] else "~", x["node_id"]))
        return items


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


def decorate_message(msg: dict[str, Any]) -> dict[str, Any]:
    out = dict(msg)
    out["from_label"] = resolve_label(out.get("from_id"), out.get("from_id"))
    out["to_label"] = resolve_label(out.get("to_id"), out.get("to_id"))
    return out


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
    base = [
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
    return [decorate_message(m) for m in base]


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
            raw_nodes = nodes_resp.get("nodes", [])
            messages = msgs_resp.get("messages", [])

            for msg in messages:
                msg_id = msg.get("id")
                if isinstance(msg_id, int) and msg_id not in last_seen_ids:
                    db_add_message(msg)
                    last_seen_ids.add(msg_id)
            if len(last_seen_ids) > 1000:
                last_seen_ids = set(sorted(last_seen_ids)[-500:])

            nodes = []
            for n in raw_nodes:
                node_id = n.get("node_id")
                fallback_name = n.get("name") or node_id
                alias_entry = get_alias_entry(node_id)
                alias = alias_entry.get("alias") if alias_entry else ""
                nodes.append(
                    {
                        **n,
                        "alias": alias,
                        "display_name": alias or fallback_name or node_id,
                    }
                )

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
    return jsonify({"version": VERSION, "status": status, "state": state, "address_book_count": len(address_book_list())})


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


@app.route("/api/address-book")
def api_address_book_list():
    return jsonify(address_book_list())


@app.route("/api/address-book", methods=["POST"])
def api_address_book_upsert():
    data = request.get_json(force=True, silent=True) or {}
    node_id = (data.get("node_id") or "").strip()
    alias = (data.get("alias") or "").strip()
    notes = (data.get("notes") or "").strip()
    if not node_id:
        return jsonify({"ok": False, "error": "node_id is required"}), 400
    if not alias:
        return jsonify({"ok": False, "error": "alias is required"}), 400
    with address_book_lock:
        address_book_cache[node_id] = {
            "alias": alias,
            "notes": notes,
            "updated_at": now_iso(),
        }
    save_address_book()
    return jsonify({"ok": True})


@app.route("/api/address-book/<path:node_id>", methods=["DELETE"])
def api_address_book_delete(node_id: str):
    with address_book_lock:
        existed = node_id in address_book_cache
        if existed:
            del address_book_cache[node_id]
    if existed:
        save_address_book()
    return jsonify({"ok": True, "deleted": existed})


@app.route("/api/channels")
def api_channels():
    try:
        resp = proxy_request({"type": "get_channels"}, timeout=6.0)
        if resp.get("type") == "error":
            return jsonify({"ok": False, "error": resp.get("error")}), 500
        return jsonify(resp)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/channels/select", methods=["POST"])
def api_channels_select():
    data = request.get_json(force=True, silent=True) or {}
    try:
        channel_index = int(data.get("channel_index", 0))
    except Exception:
        return jsonify({"ok": False, "error": "invalid channel_index"}), 400
    try:
        resp = proxy_request({"type": "set_tx_channel", "channel_index": channel_index}, timeout=6.0)
        if resp.get("type") == "error" or not resp.get("ok"):
            return jsonify({"ok": False, "error": resp.get("error") or "proxy rejected channel selection", "details": resp}), 500
        cfg = load_config(config_path_runtime)
        cfg.setdefault("node", {})["channel"] = channel_index
        with open(config_path_runtime, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return jsonify({"ok": True, **resp})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/channels/join", methods=["POST"])
def api_channels_join():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    add_only = bool(data.get("add_only", False))
    if not url:
        return jsonify({"ok": False, "error": "missing URL"}), 400
    try:
        resp = proxy_request({"type": "join_channel_url", "url": url, "add_only": add_only}, timeout=12.0)
        if resp.get("type") == "error" or not resp.get("ok"):
            return jsonify({"ok": False, "error": resp.get("error") or "proxy join failed", "details": resp}), 500
        return jsonify(resp)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/channels/<int:channel_index>", methods=["DELETE"])
def api_channels_delete(channel_index: int):
    try:
        resp = proxy_request({"type": "delete_channel", "channel_index": channel_index}, timeout=8.0)
        if resp.get("type") == "error" or not resp.get("ok"):
            return jsonify({"ok": False, "error": resp.get("error") or "proxy delete failed", "details": resp}), 500
        return jsonify(resp)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


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
    init_address_book()
    threading.Thread(target=sync_proxy_loop, daemon=True, name="proxy-sync").start()

    logger.info("Starting web chat on %s:%s (proxy %s:%s)", listen_host, listen_port, proxy_host, proxy_port)
    if ssl_adhoc:
        app.run(host=listen_host, port=listen_port, ssl_context="adhoc", debug=False, use_reloader=False)
    else:
        app.run(host=listen_host, port=listen_port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
