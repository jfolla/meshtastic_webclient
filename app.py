#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import socket
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file

VERSION = "0.7.4-beta"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "webchat_cache.db"
CONFIG_PATH = BASE_DIR / "app_config.json"
ADDRESS_BOOK_PATH = BASE_DIR / "address_book.json"
ROOMS_PATH = BASE_DIR / "rooms.json"
MAX_PROXY_RESPONSE = 4 * 1024 * 1024

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("meshtastic_webchat")

cache_lock = threading.RLock()
store_lock = threading.RLock()
address_book_lock = threading.RLock()
rooms_lock = threading.RLock()
stop_event = threading.Event()
config_path_runtime = CONFIG_PATH
address_book_cache: dict[str, dict[str, Any]] = {}
rooms_cache: list[dict[str, Any]] = []

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
    "active_room": None,
    "backups": [],
    "debug": {},
}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read %s: %s", path, exc)
        return default


def atomic_save_json(path: Path, payload: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_config(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    validate_config(cfg)
    return cfg


def validate_config(cfg: Any) -> None:
    if not isinstance(cfg, dict):
        raise ValueError("Configuration must be a JSON object")
    for section in ("node", "proxy", "web"):
        if not isinstance(cfg.get(section), dict):
            raise ValueError(f"Missing or invalid '{section}' section")

    node = cfg["node"]
    mode = str(node.get("mode", "")).lower()
    if mode not in {"serial", "tcp"}:
        raise ValueError("node.mode must be 'serial' or 'tcp'")
    if mode == "serial" and not str(node.get("port", "")).strip():
        raise ValueError("node.port is required in serial mode")
    if mode == "tcp" and not str(node.get("host", "")).strip():
        raise ValueError("node.host is required in tcp mode")

    proxy_port = int(cfg["proxy"].get("port", 0))
    web_port = int(cfg["web"].get("listen_port", 0))
    if not (1 <= proxy_port <= 65535 and 1 <= web_port <= 65535):
        raise ValueError("proxy.port and web.listen_port must be valid TCP ports")


def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def init_db() -> None:
    with store_lock, sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                direction TEXT NOT NULL,
                from_id TEXT,
                to_id TEXT,
                text TEXT NOT NULL,
                proxy_id INTEGER
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        if "proxy_id" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN proxy_id INTEGER")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_proxy_id "
            "ON messages(proxy_id) WHERE proxy_id IS NOT NULL"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_id_desc ON messages(id DESC)")


def init_address_book() -> None:
    global address_book_cache
    if not ADDRESS_BOOK_PATH.exists():
        atomic_save_json(ADDRESS_BOOK_PATH, {})
    with address_book_lock:
        data = load_json(ADDRESS_BOOK_PATH, {})
        address_book_cache = data if isinstance(data, dict) else {}


def save_address_book() -> None:
    with address_book_lock:
        payload = dict(address_book_cache)
    atomic_save_json(ADDRESS_BOOK_PATH, payload)


def init_rooms() -> None:
    global rooms_cache
    if not ROOMS_PATH.exists():
        atomic_save_json(ROOMS_PATH, {"rooms": []})
    with rooms_lock:
        data = load_json(ROOMS_PATH, {"rooms": []})
        rooms_cache = data["rooms"] if isinstance(data, dict) and isinstance(data.get("rooms"), list) else []


def save_rooms() -> None:
    with rooms_lock:
        payload = {"rooms": [dict(room) for room in rooms_cache]}
    atomic_save_json(ROOMS_PATH, payload)


def get_alias_entry(node_id: str | None) -> dict[str, Any] | None:
    if not node_id:
        return None
    with address_book_lock:
        entry = address_book_cache.get(node_id)
        return dict(entry) if isinstance(entry, dict) else None


def resolve_label(node_id: str | None, fallback_name: str | None = None) -> str:
    entry = get_alias_entry(node_id)
    if entry and entry.get("alias"):
        return str(entry["alias"])
    return str(fallback_name or node_id or "")


def address_book_list() -> list[dict[str, Any]]:
    with address_book_lock:
        items = [
            {
                "node_id": node_id,
                "alias": entry.get("alias", ""),
                "notes": entry.get("notes", ""),
                "updated_at": entry.get("updated_at", ""),
            }
            for node_id, entry in address_book_cache.items()
            if isinstance(entry, dict)
        ]
    items.sort(key=lambda item: (item["alias"].lower() if item["alias"] else "~", item["node_id"]))
    return items


def rooms_list() -> list[dict[str, Any]]:
    with rooms_lock:
        items = [dict(room) for room in rooms_cache]
    return sorted(items, key=lambda room: (str(room.get("name", "")).lower(), str(room.get("id", ""))))


def normalize_room_input(value: str) -> dict[str, str]:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Channel URL or hash is required")

    if raw.startswith(("https://", "http://")):
        parsed = urlparse(raw)
        if not parsed.fragment:
            raise ValueError("URL does not contain a channel hash")
        fragment = parsed.fragment.strip()
    else:
        fragment = raw[1:].strip() if raw.startswith("#") else raw

    if not fragment:
        raise ValueError("Channel hash is empty")
    if len(fragment) > 16384:
        raise ValueError("Channel hash is too large")

    hash_value = f"#{fragment}"
    full_url = f"https://meshtastic.org/e/{hash_value}"
    room_id = hashlib.sha256(fragment.encode("utf-8")).hexdigest()[:16]
    return {"id": room_id, "hash": hash_value, "full_url": full_url}


def db_add_proxy_message(msg: dict[str, Any]) -> None:
    proxy_id = msg.get("id") if isinstance(msg.get("id"), int) else None
    ts = str(msg.get("ts") or now_iso())
    direction = str(msg.get("direction") or "in")
    from_id = msg.get("from_id")
    to_id = msg.get("to_id")
    text = str(msg.get("text") or "")

    with store_lock, sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        if proxy_id is not None:
            exists = conn.execute("SELECT 1 FROM messages WHERE proxy_id = ? LIMIT 1", (proxy_id,)).fetchone()
            if exists:
                return
            # Upgrade path: avoid re-importing the same legacy row once after moving to proxy IDs.
            legacy = conn.execute(
                """
                SELECT id FROM messages
                WHERE proxy_id IS NULL AND ts = ? AND direction = ?
                  AND COALESCE(from_id, '') = COALESCE(?, '')
                  AND COALESCE(to_id, '') = COALESCE(?, '')
                  AND text = ?
                ORDER BY id DESC LIMIT 1
                """,
                (ts, direction, from_id, to_id, text),
            ).fetchone()
            if legacy:
                conn.execute("UPDATE messages SET proxy_id = ? WHERE id = ?", (proxy_id, legacy[0]))
                return
        conn.execute(
            "INSERT OR IGNORE INTO messages (ts, direction, from_id, to_id, text, proxy_id) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, direction, from_id, to_id, text, proxy_id),
        )


def decorate_message(msg: dict[str, Any]) -> dict[str, Any]:
    out = dict(msg)
    out["from_label"] = resolve_label(out.get("from_id"), out.get("from_id"))
    out["to_label"] = resolve_label(out.get("to_id"), out.get("to_id"))
    return out


def db_list_messages(limit: int = 100) -> list[dict[str, Any]]:
    limit = clamp_int(limit, 100, 1, 500)
    with store_lock, sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        rows = conn.execute(
            "SELECT id, ts, direction, from_id, to_id, text FROM messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    rows.reverse()
    return [
        decorate_message({"id": r[0], "ts": r[1], "direction": r[2], "from_id": r[3], "to_id": r[4], "text": r[5]})
        for r in rows
    ]


def db_clear_messages() -> None:
    with store_lock, sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("DELETE FROM messages")


def proxy_request(payload: dict[str, Any], timeout: float = 3.0) -> dict[str, Any]:
    with cache_lock:
        host = str(proxy_cache["status"]["proxy_host"])
        port = int(proxy_cache["status"]["proxy_port"])

    connect_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    with socket.create_connection((connect_host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_PROXY_RESPONSE:
                raise RuntimeError("proxy response too large")
    if not data:
        raise RuntimeError("empty response from proxy")
    return json.loads(bytes(data).decode("utf-8", errors="replace").strip())


def sync_proxy_loop() -> None:
    while not stop_event.is_set():
        try:
            snap = proxy_request({"type": "snapshot", "limit": 100}, timeout=5.0)
            state = snap.get("state", {}) if isinstance(snap.get("state"), dict) else {}
            raw_nodes = snap.get("nodes", []) if isinstance(snap.get("nodes"), list) else []
            messages = snap.get("messages", []) if isinstance(snap.get("messages"), list) else []
            active_room = snap.get("active_room")
            backups = snap.get("backups", []) if isinstance(snap.get("backups"), list) else []

            for msg in messages:
                if isinstance(msg, dict):
                    db_add_proxy_message(msg)

            nodes: list[dict[str, Any]] = []
            for raw_node in raw_nodes:
                if not isinstance(raw_node, dict):
                    continue
                node_id = raw_node.get("node_id")
                fallback_name = raw_node.get("name") or node_id
                alias_entry = get_alias_entry(node_id)
                alias = alias_entry.get("alias") if alias_entry else ""
                nodes.append({**raw_node, "alias": alias, "display_name": alias or fallback_name or node_id})

            with cache_lock:
                proxy_cache["state"] = state
                proxy_cache["nodes"] = nodes
                proxy_cache["active_room"] = active_room
                proxy_cache["backups"] = backups
                proxy_cache["debug"] = snap.get("debug", state)
                status = proxy_cache["status"]
                status["proxy_connected"] = True
                status["backend_connected"] = bool(state.get("upstream_connected"))
                status["proxy_error"] = None
                status["last_sync"] = now_iso()
        except Exception as exc:
            with cache_lock:
                status = proxy_cache["status"]
                status["proxy_connected"] = False
                status["backend_connected"] = False
                status["proxy_error"] = str(exc)
                status["last_sync"] = now_iso()
            logger.warning("Proxy poll failed: %s", exc)
        stop_event.wait(3.0)


def snapshot_payload(message_limit: int = 100) -> dict[str, Any]:
    with cache_lock:
        status = dict(proxy_cache["status"])
        state = dict(proxy_cache["state"])
        nodes = [dict(item) for item in proxy_cache["nodes"]]
        active_room = dict(proxy_cache["active_room"]) if isinstance(proxy_cache.get("active_room"), dict) else None
        backups = [dict(item) for item in proxy_cache.get("backups", []) if isinstance(item, dict)]
        debug_info = dict(proxy_cache.get("debug") or {})
    return {
        "version": VERSION,
        "status": status,
        "state": state,
        "nodes": nodes,
        "messages": db_list_messages(message_limit),
        "address_book": address_book_list(),
        "rooms": rooms_list(),
        "active_room": active_room,
        "backups": backups,
        "debug": debug_info,
    }


@app.before_request
def reject_cross_origin_mutations():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    origin = request.headers.get("Origin")
    if not origin:
        return None
    try:
        parsed = urlparse(origin)
        if parsed.netloc != request.host:
            return jsonify({"ok": False, "error": "Cross-origin request rejected"}), 403
    except Exception:
        return jsonify({"ok": False, "error": "Invalid Origin header"}), 403
    return None


@app.after_request
def harden_response(response):
    if request.path == "/" or request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Meshtastic-Webchat-Version"] = VERSION
    return response


@app.route("/")
def index():
    return render_template("index.html", version=VERSION)


@app.route("/api/snapshot")
def api_snapshot():
    return jsonify(snapshot_payload(clamp_int(request.args.get("limit"), 100, 1, 500)))


@app.route("/api/status")
def api_status():
    with cache_lock:
        return jsonify(dict(proxy_cache["status"]))


@app.route("/api/state")
def api_state():
    with cache_lock:
        return jsonify(dict(proxy_cache["state"]))


@app.route("/api/nodes")
def api_nodes():
    with cache_lock:
        return jsonify([dict(item) for item in proxy_cache["nodes"]])


@app.route("/api/messages")
def api_messages():
    return jsonify(db_list_messages(clamp_int(request.args.get("limit"), 100, 1, 500)))


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    dest = str(data.get("dest") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message"}), 400
    if len(text) > 1000:
        return jsonify({"ok": False, "error": "Message is too long"}), 400
    try:
        payload: dict[str, Any] = {"type": "send_text", "text": text}
        if dest:
            payload["dest"] = dest
        resp = proxy_request(payload, timeout=8.0)
        if not resp.get("ok", resp.get("type") == "ack"):
            return jsonify({"ok": False, "error": resp.get("error", "Send failed")}), 502
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/clear", methods=["POST"])
def api_clear():
    db_clear_messages()
    proxy_error = None
    try:
        resp = proxy_request({"type": "clear_messages"}, timeout=5.0)
        if not resp.get("ok"):
            proxy_error = resp.get("error", "Proxy cache clear failed")
    except Exception as exc:
        proxy_error = str(exc)
    return jsonify({"ok": True, "proxy_error": proxy_error})


@app.route("/api/debug")
def api_debug():
    snap = snapshot_payload(100)
    return jsonify(
        {
            "version": VERSION,
            "status": snap["status"],
            "state": snap["state"],
            "active_room": snap["active_room"],
            "debug": snap["debug"],
            "address_book_count": len(snap["address_book"]),
            "rooms_count": len(snap["rooms"]),
            "backups_count": len(snap["backups"]),
        }
    )


@app.route("/api/config/export")
def api_config_export():
    return send_file(config_path_runtime, as_attachment=True, download_name="app_config.json")


@app.route("/api/config/import", methods=["POST"])
def api_config_import():
    uploaded = request.files.get("file")
    if uploaded is None:
        return jsonify({"ok": False, "error": "Missing file"}), 400
    try:
        cfg = json.loads(uploaded.read().decode("utf-8"))
        validate_config(cfg)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Invalid configuration: {exc}"}), 400
    cfg["version"] = VERSION
    atomic_save_json(config_path_runtime, cfg)
    return jsonify({"ok": True, "message": "Configuration imported. Restart the service to apply changes."})


@app.route("/api/address-book")
def api_address_book_list():
    return jsonify(address_book_list())


@app.route("/api/address-book", methods=["POST"])
def api_address_book_upsert():
    data = request.get_json(silent=True) or {}
    node_id = str(data.get("node_id") or "").strip()
    alias = str(data.get("alias") or "").strip()
    notes = str(data.get("notes") or "").strip()
    if not node_id or not alias:
        return jsonify({"ok": False, "error": "node_id and alias are required"}), 400
    if len(node_id) > 128 or len(alias) > 80 or len(notes) > 500:
        return jsonify({"ok": False, "error": "Address book field is too long"}), 400
    with address_book_lock:
        address_book_cache[node_id] = {"alias": alias, "notes": notes, "updated_at": now_iso()}
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


@app.route("/api/rooms")
def api_rooms_list():
    return jsonify(rooms_list())


def preview_room(normalized: dict[str, str]) -> dict[str, Any]:
    resp = proxy_request({"type": "room_preview", "url": normalized["full_url"]}, timeout=5.0)
    if not resp.get("ok"):
        raise ValueError(str(resp.get("error") or "Invalid channel URL"))
    preview = resp.get("preview")
    return preview if isinstance(preview, dict) else {}


@app.route("/api/rooms/preview", methods=["POST"])
def api_rooms_preview():
    data = request.get_json(silent=True) or {}
    try:
        normalized = normalize_room_input(str(data.get("value") or ""))
        preview = preview_room(normalized)
        return jsonify({"ok": True, "normalized": normalized, "preview": preview})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/rooms/import", methods=["POST"])
def api_rooms_import():
    data = request.get_json(silent=True) or {}
    value = str(data.get("value") or "").strip()
    name = str(data.get("name") or "").strip()
    if len(name) > 80:
        return jsonify({"ok": False, "error": "Room name is too long"}), 400
    try:
        normalized = normalize_room_input(value)
        preview = preview_room(normalized)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    room = {
        "id": normalized["id"],
        "name": name or preview.get("primary_name") or normalized["id"],
        "hash": normalized["hash"],
        "full_url": normalized["full_url"],
        "preview": preview,
        "created_at": now_iso(),
        "beta": True,
    }
    with rooms_lock:
        existing = {str(item.get("id")): item for item in rooms_cache if isinstance(item, dict)}
        old = existing.get(room["id"], {})
        room["created_at"] = old.get("created_at", room["created_at"])
        existing[room["id"]] = room
        rooms_cache[:] = list(existing.values())
    save_rooms()
    return jsonify({"ok": True, "room": room})


@app.route("/api/rooms/<path:room_id>", methods=["DELETE"])
def api_rooms_delete(room_id: str):
    with rooms_lock:
        before = len(rooms_cache)
        rooms_cache[:] = [room for room in rooms_cache if room.get("id") != room_id]
        deleted = len(rooms_cache) != before
    if deleted:
        save_rooms()
    return jsonify({"ok": True, "deleted": deleted})


@app.route("/api/rooms/active")
def api_rooms_active():
    refresh = str(request.args.get("refresh", "")).lower() in {"1", "true", "yes"}
    if refresh:
        try:
            resp = proxy_request({"type": "room_refresh"}, timeout=8.0)
            if resp.get("ok"):
                with cache_lock:
                    proxy_cache["active_room"] = resp.get("active_room")
            return jsonify(resp)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502
    with cache_lock:
        active = proxy_cache.get("active_room")
    return jsonify({"ok": True, "active_room": active})


@app.route("/api/rooms/backups")
def api_rooms_backups():
    with cache_lock:
        backups = [dict(item) for item in proxy_cache.get("backups", []) if isinstance(item, dict)]
    return jsonify({"ok": True, "backups": backups})


@app.route("/api/rooms/apply", methods=["POST"])
def api_rooms_apply():
    data = request.get_json(silent=True) or {}
    room_id = str(data.get("room_id") or "").strip()
    raw_value = str(data.get("value") or "").strip()
    room: dict[str, Any] | None = None

    if room_id:
        room = next((item for item in rooms_list() if item.get("id") == room_id), None)
    elif raw_value:
        try:
            room = normalize_room_input(raw_value)
            room["name"] = room["id"]
            room["preview"] = preview_room(room)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    if not room:
        return jsonify({"ok": False, "error": "Room not found"}), 404

    try:
        resp = proxy_request(
            {"type": "room_apply", "url": room["full_url"], "name": room.get("name", room.get("id", "room"))},
            timeout=20.0,
        )
        if resp.get("ok"):
            with cache_lock:
                proxy_cache["active_room"] = resp.get("active_room")
                proxy_cache["backups"] = resp.get("backups", proxy_cache.get("backups", []))
        return jsonify(resp), (200 if resp.get("ok") else 502)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/rooms/rollback", methods=["POST"])
def api_rooms_rollback():
    try:
        resp = proxy_request({"type": "room_rollback"}, timeout=20.0)
        if resp.get("ok"):
            with cache_lock:
                proxy_cache["active_room"] = resp.get("active_room")
                proxy_cache["backups"] = resp.get("backups", proxy_cache.get("backups", []))
        return jsonify(resp), (200 if resp.get("ok") else 400)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


def main() -> None:
    parser = argparse.ArgumentParser(description="Meshtastic Web Chat")
    parser.add_argument("--config", required=True, help="Path to app_config.json")
    args = parser.parse_args()

    global config_path_runtime
    config_path_runtime = Path(args.config).resolve()
    cfg = load_config(config_path_runtime)

    proxy_host = str(cfg["proxy"].get("host", "127.0.0.1"))
    proxy_port = int(cfg["proxy"]["port"])
    listen_host = str(cfg["web"].get("listen_host", "0.0.0.0"))
    listen_port = int(cfg["web"]["listen_port"])
    ssl_adhoc = bool(cfg["web"].get("ssl_adhoc", False))

    with cache_lock:
        proxy_cache["status"]["proxy_host"] = proxy_host
        proxy_cache["status"]["proxy_port"] = proxy_port

    init_db()
    init_address_book()
    init_rooms()
    threading.Thread(target=sync_proxy_loop, daemon=True, name="proxy-sync").start()

    logger.info("Starting web chat v%s on %s:%s (proxy %s:%s)", VERSION, listen_host, listen_port, proxy_host, proxy_port)
    kwargs: dict[str, Any] = {"host": listen_host, "port": listen_port, "debug": False, "use_reloader": False, "threaded": True}
    if ssl_adhoc:
        kwargs["ssl_context"] = "adhoc"
    app.run(**kwargs)


if __name__ == "__main__":
    main()
