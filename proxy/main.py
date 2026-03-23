#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import signal
import socketserver
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pubsub import pub
import meshtastic.serial_interface
import meshtastic.tcp_interface

LOGGER = logging.getLogger("meshtastic_proxy")
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "proxy_messages.db"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class MessageStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute(
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

    def add(self, direction: str, from_id: str, to_id: str, text: str, raw_packet: Optional[dict[str, Any]] = None):
        raw_json = json.dumps(raw_packet, ensure_ascii=False, default=str) if raw_packet is not None else None
        with self.lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (ts, direction, from_id, to_id, text, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
                (now_iso(), direction, from_id, to_id, text, raw_json),
            )
            return cur.lastrowid

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, direction, from_id, to_id, text FROM messages ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
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


@dataclass
class ProxyState:
    upstream_connected: bool = False
    mode: str = "serial"
    target: str = ""
    channel: int = 0
    last_connect_at: Optional[str] = None
    last_disconnect_at: Optional[str] = None
    last_error: Optional[str] = None
    packets_rx_seen: int = 0
    messages_rx: int = 0
    messages_tx: int = 0
    relay_seen_estimate: int = 0
    multi_hop_seen_estimate: int = 0
    last_packet_at: Optional[str] = None
    nodes: list[dict[str, Any]] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


class UpstreamManager:
    def __init__(self, config: dict[str, Any], state: ProxyState, store: MessageStore):
        self.config = config
        self.state = state
        self.store = store
        self.iface = None
        self.iface_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="proxy-poller")
        self.worker_thread = threading.Thread(target=self._worker, daemon=True, name="proxy-worker")
        self._subscribed = False
        self._subscribe_once()

    def start(self):
        self.worker_thread.start()
        self.poll_thread.start()

    def stop(self):
        self.stop_event.set()
        with self.iface_lock:
            iface = self.iface
            self.iface = None
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass

    def _subscribe_once(self):
        if self._subscribed:
            return
        pub.subscribe(self.on_text, "meshtastic.receive.text")
        pub.subscribe(self.on_receive, "meshtastic.receive")
        pub.subscribe(self.on_connection_established, "meshtastic.connection.established")
        pub.subscribe(self.on_connection_lost, "meshtastic.connection.lost")
        self._subscribed = True

    def on_text(self, packet, interface=None):
        text = self._decode_text(packet)
        if not text:
            return
        from_id = packet.get("fromId", str(packet.get("from", "unknown")))
        to_id = packet.get("toId", str(packet.get("to", "^all")))
        self.store.add("in", from_id, to_id, text, packet)
        with self.state_lock:
            self.state.messages_rx += 1
            self.state.last_packet_at = now_iso()

    def on_receive(self, packet, interface=None):
        relay_node = packet.get("relayNode")
        hop_start = packet.get("hopStart")
        hop_limit = packet.get("hopLimit")
        with self.state_lock:
            self.state.packets_rx_seen += 1
            self.state.last_packet_at = now_iso()
            if relay_node is not None:
                self.state.relay_seen_estimate += 1
            try:
                if hop_start is not None and hop_limit is not None and int(hop_start) > int(hop_limit):
                    self.state.multi_hop_seen_estimate += 1
            except Exception:
                pass

    def on_connection_established(self, interface, topic=pub.AUTO_TOPIC):
        with self.state_lock:
            self.state.upstream_connected = True
            self.state.last_connect_at = now_iso()
            self.state.last_error = None
        LOGGER.info("Connected to Meshtastic via %s -> %s", self.state.mode, self.state.target)

    def on_connection_lost(self, interface=None, topic=pub.AUTO_TOPIC):
        with self.state_lock:
            self.state.upstream_connected = False
            self.state.last_disconnect_at = now_iso()
        LOGGER.warning("Meshtastic connection lost")
        with self.iface_lock:
            iface = self.iface
            self.iface = None
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass

    def _decode_text(self, packet: dict[str, Any]) -> Optional[str]:
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

    def _connect(self):
        node_cfg = self.config["node"]
        mode = node_cfg["mode"]
        target = node_cfg["port"] if mode == "serial" else node_cfg["host"]
        if mode == "serial":
            iface = meshtastic.serial_interface.SerialInterface(devPath=target)
        else:
            iface = meshtastic.tcp_interface.TCPInterface(hostname=target)
        with self.iface_lock:
            self.iface = iface
        with self.state_lock:
            self.state.mode = mode
            self.state.target = target
            self.state.channel = int(node_cfg.get("channel", 0))
        return iface

    def _worker(self):
        while not self.stop_event.is_set():
            with self.iface_lock:
                iface = self.iface
            if iface is None:
                try:
                    self._connect()
                except Exception as exc:
                    with self.state_lock:
                        self.state.upstream_connected = False
                        self.state.last_error = str(exc)
                    LOGGER.warning("Upstream connect failed: %s", exc)
                    time.sleep(5)
                    continue
            time.sleep(1)

    def _poll_loop(self):
        while not self.stop_event.is_set():
            with self.iface_lock:
                iface = self.iface
            if iface is not None:
                try:
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
                    with self.state_lock:
                        self.state.nodes = out
                except Exception as exc:
                    with self.state_lock:
                        self.state.last_error = str(exc)
            time.sleep(5)

    def send_text(self, text: str, destination_id: Optional[str] = None) -> None:
        with self.iface_lock:
            iface = self.iface
        if iface is None:
            raise RuntimeError("Upstream not connected")
        kwargs = {
            "text": text,
            "wantAck": False,
            "channelIndex": int(self.config["node"].get("channel", 0)),
        }
        if destination_id:
            kwargs["destinationId"] = destination_id
        iface.sendText(**kwargs)
        self.store.add("out", "io", destination_id or "^all", text)
        with self.state_lock:
            self.state.messages_tx += 1

    def get_state(self) -> dict[str, Any]:
        with self.state_lock:
            return self.state.snapshot()


class ThreadedJSONServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_cls, manager: UpstreamManager):
        self.manager = manager
        super().__init__(server_address, handler_cls)


class JSONHandler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception as exc:
                self._send({"type": "error", "error": f"invalid json: {exc}"})
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]):
        typ = msg.get("type")
        manager = self.server.manager  # type: ignore[attr-defined]
        if typ == "ping":
            self._send({"type": "pong", "ok": True})
        elif typ == "get_state":
            self._send({"type": "state", "state": manager.get_state()})
        elif typ == "get_nodes":
            self._send({"type": "nodes", "nodes": manager.get_state().get("nodes", [])})
        elif typ == "get_messages":
            limit = int(msg.get("limit", 100))
            self._send({"type": "messages", "messages": manager.store.list(limit=limit)})
        elif typ == "send_text":
            text = (msg.get("text") or "").strip()
            if not text:
                self._send({"type": "error", "error": "empty text"})
                return
            try:
                manager.send_text(text=text, destination_id=msg.get("dest"))
                self._send({"type": "ack", "ok": True})
            except Exception as exc:
                self._send({"type": "error", "error": str(exc)})
        elif typ == "debug":
            self._send({"type": "debug", "state": manager.get_state()})
        else:
            self._send({"type": "error", "error": f"unknown command: {typ}"})

    def _send(self, payload: dict[str, Any]):
        try:
            self.wfile.write((json.dumps(payload, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main():
    parser = argparse.ArgumentParser(description="Meshtastic local proxy")
    parser.add_argument("--config", required=True, help="Path to app_config.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    state = ProxyState(
        mode=config["node"]["mode"],
        target=config["node"]["port"] if config["node"]["mode"] == "serial" else config["node"]["host"],
        channel=int(config["node"].get("channel", 0)),
    )
    store = MessageStore(DB_PATH)
    manager = UpstreamManager(config=config, state=state, store=store)
    manager.start()

    host = config["proxy"]["host"]
    port = int(config["proxy"]["port"])
    server = ThreadedJSONServer((host, port), JSONHandler, manager)
    LOGGER.info("Proxy ready on %s:%s", host, port)

    def _stop(signum, frame):
        LOGGER.info("Stopping proxy")
        manager.stop()
        server.shutdown()
        server.server_close()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    server.serve_forever()


if __name__ == "__main__":
    main()
