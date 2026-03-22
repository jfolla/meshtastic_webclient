from __future__ import annotations

import argparse
import json
import logging
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

VERSION = "0.5.3"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("meshtastic-webchat")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "app_config.json"
DB_PATH = BASE_DIR / "messages.jsonl"

app = Flask(__name__)
cache_lock = threading.Lock()
messages = deque(maxlen=500)
nodes_cache: list[dict] = []
status_cache: dict[str, Any] = {
    "backend_connected": False,
    "proxy_connected": False,
    "proxy_error": None,
    "last_sync": None,
    "stats": {},
}
current_config: dict[str, Any] = {}

I18N = {
    "en": {"title": "Meshtastic Web Chat", "send": "Send", "message": "Message", "nodes": "Nodes", "stats": "Statistics", "backend": "Backend connected to node", "messages": "Messages", "debug": "Debug", "config": "Configuration", "export": "Export config", "import": "Import config", "language": "Language", "status": "Status", "online": "Yes", "offline": "No", "proxyError": "Proxy error", "lastPacket": "Last packet", "clear": "Clear", "battery": "Battery", "voltage": "Voltage", "relaySeen": "Relay observed", "relayTx": "Relay sent by node", "packetsRx": "Packets received", "packetsBad": "Corrupted packets", "dropped": "Dropped"},
    "it": {"title": "Meshtastic Web Chat", "send": "Invia", "message": "Messaggio", "nodes": "Nodi", "stats": "Statistiche", "backend": "Backend connesso al nodo", "messages": "Messaggi", "debug": "Debug", "config": "Configurazione", "export": "Esporta config", "import": "Importa config", "language": "Lingua", "status": "Stato", "online": "Sì", "offline": "No", "proxyError": "Errore proxy", "lastPacket": "Ultimo pacchetto", "clear": "Pulisci", "battery": "Batteria", "voltage": "Tensione", "relaySeen": "Relay osservati", "relayTx": "Relay inviati dal nodo", "packetsRx": "Pacchetti ricevuti", "packetsBad": "Pacchetti corrotti", "dropped": "Scartati"},
    "fr": {"title": "Meshtastic Web Chat", "send": "Envoyer", "message": "Message", "nodes": "Nœuds", "stats": "Statistiques", "backend": "Backend connecté au nœud", "messages": "Messages", "debug": "Debug", "config": "Configuration", "export": "Exporter config", "import": "Importer config", "language": "Langue", "status": "État", "online": "Oui", "offline": "Non", "proxyError": "Erreur proxy", "lastPacket": "Dernier paquet", "clear": "Effacer", "battery": "Batterie", "voltage": "Tension", "relaySeen": "Relais observés", "relayTx": "Relais envoyés par le nœud", "packetsRx": "Paquets reçus", "packetsBad": "Paquets corrompus", "dropped": "Abandonnés"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(path: Path, data: dict[str, Any]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def proxy_request(payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    proxy = current_config["proxy"]
    host = proxy["host"]
    port = int(proxy["port"])
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        s.settimeout(timeout)
        f = s.makefile("rwb")
        # hello
        _ = f.readline()
        # initial state
        _ = f.readline()
        f.write((json.dumps(payload) + "\n").encode("utf-8"))
        f.flush()
        line = f.readline().decode("utf-8").strip()
        if not line:
            raise RuntimeError("Empty proxy response")
        return json.loads(line)
    finally:
        s.close()


def append_message(msg: dict[str, Any]):
    with cache_lock:
        messages.append(msg)
    with open(DB_PATH, "a", encoding="utf-8") as f:
        json.dump(msg, f, ensure_ascii=False)
        f.write("\n")


def load_messages():
    if not DB_PATH.exists():
        return
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()[-500:]
        for line in lines:
            try:
                messages.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass


def sync_worker():
    while True:
        try:
            state = proxy_request({"type": "get_state"}, timeout=4)
            nodes = proxy_request({"type": "get_nodes"}, timeout=4)
            recent_messages = proxy_request({"type": "get_messages", "limit": 100}, timeout=4)
            with cache_lock:
                status_cache["proxy_connected"] = True
                status_cache["backend_connected"] = bool(state.get("state", {}).get("stats", {}).get("upstream_connected"))
                status_cache["proxy_error"] = None
                status_cache["last_sync"] = utc_now()
                status_cache["stats"] = state.get("state", {}).get("stats", {})
                nodes_cache.clear()
                raw_nodes = nodes.get("nodes", {})
                if isinstance(raw_nodes, dict):
                    for node_id, node in raw_nodes.items():
                        user = node.get("user", {}) or {}
                        nodes_cache.append({
                            "node_id": node_id,
                            "name": user.get("longName") or user.get("shortName") or node_id,
                            "short_name": user.get("shortName", ""),
                            "hw_model": user.get("hwModel", ""),
                            "last_heard": node.get("lastHeard"),
                        })
                messages.clear()
                for msg in recent_messages.get("messages", []):
                    messages.append(msg)
        except Exception as exc:
            with cache_lock:
                status_cache["proxy_connected"] = False
                status_cache["proxy_error"] = str(exc)
        time.sleep(5)


@app.route("/")
def index():
    return render_template("index.html", version=VERSION, i18n=json.dumps(I18N, ensure_ascii=False))


@app.route("/api/status")
def api_status():
    with cache_lock:
        return jsonify(status_cache)


@app.route("/api/stats")
def api_stats():
    with cache_lock:
        return jsonify(status_cache.get("stats", {}))


@app.route("/api/messages")
def api_messages():
    with cache_lock:
        return jsonify(list(messages))


@app.route("/api/nodes")
def api_nodes():
    with cache_lock:
        return jsonify(list(nodes_cache))


@app.route("/api/debug")
def api_debug():
    with cache_lock:
        return jsonify({
            "version": VERSION,
            "config_path": str(CONFIG_PATH),
            "proxy": current_config.get("proxy", {}),
            "node": current_config.get("node", {}),
            "status": status_cache,
            "cached_nodes": len(nodes_cache),
            "cached_messages": len(messages),
        })


@app.route("/api/send", methods=["POST"])
def api_send():
    payload = request.get_json(force=True, silent=True) or {}
    text = (payload.get("text") or "").strip()
    destination_id = payload.get("destination_id") or None
    if not text:
        return jsonify({"ok": False, "error": "Missing text"}), 400
    try:
        resp = proxy_request({"type": "send_text", "text": text, "destination_id": destination_id})
        append_message({"ts": utc_now(), "direction": "out", "from_id": "me", "to_id": destination_id or "^all", "text": text})
        return jsonify({"ok": True, "response": resp})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/clear", methods=["POST"])
def api_clear():
    try:
        proxy_request({"type": "clear_messages"})
    except Exception:
        pass
    with cache_lock:
        messages.clear()
    try:
        DB_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    global current_config
    if request.method == "GET":
        return jsonify(current_config)
    data = request.get_json(force=True, silent=True) or {}
    save_config(CONFIG_PATH, data)
    current_config = data
    return jsonify({"ok": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Meshtastic Web Chat")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--host")
    parser.add_argument("--port")
    parser.add_argument("--listen-host")
    parser.add_argument("--listen-port", type=int)
    parser.add_argument("--channel", type=int)
    parser.add_argument("--ssl-adhoc", action="store_true")
    args = parser.parse_args()

    current_config = load_config(Path(args.config))
    if args.host:
        current_config["node"]["mode"] = "tcp"
        current_config["node"]["host"] = args.host
    if args.port:
        current_config["node"]["mode"] = "serial"
        current_config["node"]["port"] = args.port
    if args.listen_host:
        current_config["web"]["listen_host"] = args.listen_host
    if args.listen_port:
        current_config["web"]["listen_port"] = args.listen_port
    if args.channel is not None:
        current_config["node"]["channel"] = args.channel
    if args.ssl_adhoc:
        current_config["web"]["ssl_adhoc"] = True

    load_messages()
    threading.Thread(target=sync_worker, daemon=True).start()
    ssl_context = "adhoc" if current_config.get("web", {}).get("ssl_adhoc") else None
    app.run(host=current_config["web"]["listen_host"], port=int(current_config["web"]["listen_port"]), ssl_context=ssl_context, threaded=True)
