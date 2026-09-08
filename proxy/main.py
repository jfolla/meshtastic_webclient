#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import signal
import socketserver
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from google.protobuf.json_format import MessageToDict
from pubsub import pub

import meshtastic.serial_interface
import meshtastic.tcp_interface
from meshtastic.protobuf import apponly_pb2

LOGGER = logging.getLogger("meshtastic_proxy")
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "proxy_messages.db"
BACKUPS_PATH = BASE_DIR / "room_backups.json"
MAX_BACKUPS = 20


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_save_json(path: Path, payload: Any, mode: int = 0o600) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


class MessageStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.lock = threading.RLock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self.lock, self._connect() as conn:
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proxy_messages_id_desc ON messages(id DESC)")

    def add(self, direction: str, from_id: str, to_id: str, text: str, raw_packet: Optional[dict[str, Any]] = None) -> int:
        raw_json = json.dumps(raw_packet, ensure_ascii=False, default=str) if raw_packet is not None else None
        with self.lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO messages (ts, direction, from_id, to_id, text, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
                (now_iso(), direction, from_id, to_id, text, raw_json),
            )
            return int(cur.lastrowid)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        with self.lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, direction, from_id, to_id, text FROM messages ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        rows.reverse()
        return [
            {"id": r[0], "ts": r[1], "direction": r[2], "from_id": r[3], "to_id": r[4], "text": r[5]}
            for r in rows
        ]

    def clear(self) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM messages")


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
    active_room_url: Optional[str] = None
    active_room_checked_at: Optional[str] = None

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


class UpstreamManager:
    def __init__(self, config: dict[str, Any], state: ProxyState, store: MessageStore):
        self.config = config
        self.state = state
        self.store = store
        self.iface = None
        self.iface_lock = threading.RLock()
        self.state_lock = threading.RLock()
        self.backup_lock = threading.RLock()
        self.room_operation_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True, name="proxy-poller")
        self.worker_thread = threading.Thread(target=self._worker, daemon=True, name="proxy-worker")
        self._subscribed = False
        self._subscribe_once()

    def start(self) -> None:
        self.worker_thread.start()
        self.poll_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.iface_lock:
            iface = self.iface
            self.iface = None
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass
        for thread in (self.worker_thread, self.poll_thread):
            if thread.is_alive():
                thread.join(timeout=2.0)

    def _subscribe_once(self) -> None:
        if self._subscribed:
            return
        pub.subscribe(self.on_text, "meshtastic.receive.text")
        pub.subscribe(self.on_receive, "meshtastic.receive")
        pub.subscribe(self.on_connection_established, "meshtastic.connection.established")
        pub.subscribe(self.on_connection_lost, "meshtastic.connection.lost")
        self._subscribed = True

    def on_text(self, packet, interface=None) -> None:
        text = self._decode_text(packet)
        if not text:
            return
        from_id = packet.get("fromId", str(packet.get("from", "unknown")))
        to_id = packet.get("toId", str(packet.get("to", "^all")))
        self.store.add("in", from_id, to_id, text, packet)
        with self.state_lock:
            self.state.messages_rx += 1
            self.state.last_packet_at = now_iso()

    def on_receive(self, packet, interface=None) -> None:
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
            except (TypeError, ValueError):
                pass

    def on_connection_established(self, interface, topic=pub.AUTO_TOPIC) -> None:
        with self.state_lock:
            self.state.upstream_connected = True
            self.state.last_connect_at = now_iso()
            self.state.last_error = None
        LOGGER.info("Connected to Meshtastic via %s -> %s", self.state.mode, self.state.target)

    def on_connection_lost(self, interface=None, topic=pub.AUTO_TOPIC) -> None:
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

    @staticmethod
    def _decode_text(packet: dict[str, Any]) -> Optional[str]:
        decoded = packet.get("decoded", {})
        text = decoded.get("text")
        if text:
            return str(text)
        payload = decoded.get("payload")
        if isinstance(payload, (bytes, bytearray)):
            return payload.decode("utf-8", errors="replace")
        return None

    def _connect(self):
        node_cfg = self.config["node"]
        mode = str(node_cfg["mode"]).lower()
        target = node_cfg["port"] if mode == "serial" else node_cfg["host"]
        LOGGER.info("Opening Meshtastic %s connection to %s", mode, target)
        if mode == "serial":
            iface = meshtastic.serial_interface.SerialInterface(devPath=target)
        elif mode == "tcp":
            iface = meshtastic.tcp_interface.TCPInterface(hostname=target)
        else:
            raise ValueError(f"Unsupported node mode: {mode}")
        with self.iface_lock:
            self.iface = iface
        with self.state_lock:
            self.state.mode = mode
            self.state.target = str(target)
            self.state.channel = int(node_cfg.get("channel", 0))
        return iface

    def _worker(self) -> None:
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
                    self.stop_event.wait(5.0)
                    continue
            self.stop_event.wait(1.0)

    def _poll_loop(self) -> None:
        counter = 0
        while not self.stop_event.is_set():
            with self.iface_lock:
                iface = self.iface
            if iface is not None:
                try:
                    nodes = getattr(iface, "nodes", {}) or {}
                    out = []
                    for node_id, node in nodes.items():
                        user = node.get("user", {}) if isinstance(node, dict) else {}
                        out.append(
                            {
                                "node_id": node_id,
                                "name": user.get("longName") or user.get("shortName") or node_id,
                                "short_name": user.get("shortName") or "",
                                "hw_model": user.get("hwModel") or "",
                                "last_heard": node.get("lastHeard") if isinstance(node, dict) else None,
                            }
                        )
                    out.sort(key=lambda item: (str(item["name"] or "").lower(), str(item["node_id"])))
                    with self.state_lock:
                        self.state.nodes = out

                    counter += 1
                    if counter % 6 == 0:
                        try:
                            self.refresh_active_room()
                        except Exception as exc:
                            LOGGER.debug("Active channel refresh failed: %s", exc)
                except Exception as exc:
                    with self.state_lock:
                        self.state.last_error = str(exc)
            self.stop_event.wait(5.0)

    def send_text(self, text: str, destination_id: Optional[str] = None) -> None:
        with self.iface_lock:
            iface = self.iface
        if iface is None:
            raise RuntimeError("Upstream not connected")
        kwargs: dict[str, Any] = {
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

    def _load_backups(self) -> list[dict[str, Any]]:
        with self.backup_lock:
            if not BACKUPS_PATH.exists():
                return []
            try:
                data = json.loads(BACKUPS_PATH.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
            except (OSError, json.JSONDecodeError) as exc:
                LOGGER.warning("Cannot read channel backups: %s", exc)
                return []

    def _save_backups(self, items: list[dict[str, Any]]) -> None:
        with self.backup_lock:
            atomic_save_json(BACKUPS_PATH, items[-MAX_BACKUPS:])

    @staticmethod
    def _canonical_url(url: str) -> str:
        raw = str(url or "").strip()
        if not raw:
            raise ValueError("Channel URL is required")
        fragment = raw.split("#", 1)[1] if "#" in raw else raw.lstrip("#")
        fragment = fragment.strip()
        if not fragment:
            raise ValueError("Channel hash is empty")
        return f"https://meshtastic.org/e/#{fragment}"

    @classmethod
    def preview_room(cls, url: str) -> dict[str, Any]:
        canonical = cls._canonical_url(url)
        encoded = canonical.split("#", 1)[1]
        if len(encoded) > 16384:
            raise ValueError("Channel hash is too large")
        padded = encoded + ("=" * ((4 - len(encoded) % 4) % 4))
        try:
            decoded = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
            channel_set = apponly_pb2.ChannelSet()
            channel_set.ParseFromString(decoded)
        except Exception as exc:
            raise ValueError(f"Invalid Meshtastic channel URL/hash: {exc}") from exc
        if len(channel_set.settings) == 0:
            raise ValueError("Channel URL does not contain channel settings")

        channels = []
        for index, settings in enumerate(channel_set.settings):
            channels.append(
                {
                    "index": index,
                    "role": "PRIMARY" if index == 0 else "SECONDARY",
                    "name": settings.name or "(default)",
                    "has_psk": bool(settings.psk),
                    "uplink_enabled": bool(settings.uplink_enabled),
                    "downlink_enabled": bool(settings.downlink_enabled),
                }
            )
        try:
            lora = MessageToDict(channel_set.lora_config, preserving_proto_field_name=True)
        except Exception:
            lora = {}
        return {
            "full_url": canonical,
            "hash": f"#{encoded}",
            "channel_count": len(channels),
            "primary_name": channels[0]["name"],
            "channels": channels,
            "lora": lora,
        }

    def _local_node(self):
        with self.iface_lock:
            iface = self.iface
            if iface is None:
                raise RuntimeError("Upstream not connected")
            node = getattr(iface, "localNode", None)
            if node is None:
                raise RuntimeError("Meshtastic local node is not ready")
            return node

    def _read_channel_urls(self) -> tuple[str, str]:
        node = self._local_node()
        try:
            primary_url = node.getURL(includeAll=False)
            complete_url = node.getURL(includeAll=True)
        except TypeError:
            # Compatibility fallback for older Meshtastic Python versions.
            primary_url = node.getURL()
            complete_url = primary_url
        return self._canonical_url(primary_url), self._canonical_url(complete_url)

    def refresh_active_room(self) -> dict[str, Any]:
        canonical, _complete = self._read_channel_urls()
        checked_at = now_iso()
        with self.state_lock:
            self.state.active_room_url = canonical
            self.state.active_room_checked_at = checked_at
            self.state.last_error = None
        return {
            "ok": True,
            "active_room": {
                "full_url": canonical,
                "hash": f"#{canonical.split('#', 1)[1]}",
                "checked_at": checked_at,
                "verified": True,
            },
        }

    def get_cached_active_room(self) -> dict[str, Any]:
        with self.state_lock:
            url = self.state.active_room_url
            checked_at = self.state.active_room_checked_at
        if not url:
            return {"ok": True, "active_room": None}
        return {
            "ok": True,
            "active_room": {
                "full_url": url,
                "hash": f"#{url.split('#', 1)[1]}" if "#" in url else None,
                "checked_at": checked_at,
                "verified": True,
            },
        }

    def list_backups(self) -> dict[str, Any]:
        return {"ok": True, "backups": self._load_backups()}

    def _set_room_url(self, url: str) -> None:
        node = self._local_node()
        try:
            node.setURL(url)
        except SystemExit as exc:
            raise RuntimeError(str(exc) or "Meshtastic rejected the channel URL") from exc
        # Give the device time to consume admin writes. The long-lived proxy connection remains open.
        time.sleep(1.0)

    def apply_room(self, url: str, name: str = "room") -> dict[str, Any]:
        if not self.room_operation_lock.acquire(blocking=False):
            return {"ok": False, "error": "Another channel operation is already running"}
        try:
            preview = self.preview_room(url)
            requested_url = preview["full_url"]
            current_url = None
            complete_current_url = None
            active: dict[str, Any] = {}
            try:
                current_url, complete_current_url = self._read_channel_urls()
                active = {"full_url": current_url, "hash": f"#{current_url.split('#', 1)[1]}"}
            except Exception:
                active = self.get_cached_active_room().get("active_room") or {}
                current_url = active.get("full_url")
                complete_current_url = current_url

            backups = self._load_backups()
            if current_url and current_url != requested_url:
                backups.append(
                    {
                        "ts": now_iso(),
                        "name": name,
                        # includeAll=True allows rollback to restore secondary channels too.
                        "previous_full_url": complete_current_url or current_url,
                        "previous_primary_url": current_url,
                        "previous_hash": active.get("hash"),
                    }
                )
                self._save_backups(backups)

            self._set_room_url(requested_url)
            try:
                active_after = self.refresh_active_room().get("active_room")
            except Exception as exc:
                LOGGER.info("Channel applied but immediate verification is pending: %s", exc)
                active_after = {
                    "full_url": requested_url,
                    "hash": preview["hash"],
                    "checked_at": now_iso(),
                    "verified": False,
                }
                with self.state_lock:
                    self.state.active_room_url = requested_url
                    self.state.active_room_checked_at = active_after["checked_at"]

            return {
                "ok": True,
                "message": "Channel applied to node",
                "active_room": active_after,
                "backup_created": bool(current_url and current_url != requested_url),
                "backups": self._load_backups(),
                "preview": preview,
            }
        finally:
            self.room_operation_lock.release()

    def rollback_room(self) -> dict[str, Any]:
        if not self.room_operation_lock.acquire(blocking=False):
            return {"ok": False, "error": "Another channel operation is already running"}
        try:
            backups = self._load_backups()
            if not backups:
                return {"ok": False, "error": "No backups available"}
            last = backups[-1]
            previous_url = last.get("previous_full_url")
            if not previous_url:
                return {"ok": False, "error": "Last backup is invalid"}

            preview = self.preview_room(previous_url)
            self._set_room_url(preview["full_url"])
            # Remove the backup only after the set operation succeeds.
            remaining = backups[:-1]
            self._save_backups(remaining)
            try:
                active_after = self.refresh_active_room().get("active_room")
            except Exception:
                active_after = {
                    "full_url": preview["full_url"],
                    "hash": preview["hash"],
                    "checked_at": now_iso(),
                    "verified": False,
                }
                with self.state_lock:
                    self.state.active_room_url = preview["full_url"]
                    self.state.active_room_checked_at = active_after["checked_at"]
            return {
                "ok": True,
                "message": "Previous channel restored",
                "active_room": active_after,
                "backups": remaining,
            }
        finally:
            self.room_operation_lock.release()

    def snapshot(self, limit: int = 100) -> dict[str, Any]:
        state = self.get_state()
        cached = self.get_cached_active_room().get("active_room")
        return {
            "ok": True,
            "state": state,
            "nodes": state.get("nodes", []),
            "messages": self.store.list(limit),
            "active_room": cached,
            "backups": self._load_backups(),
            "debug": state,
        }


class ThreadedJSONServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler_cls, manager: UpstreamManager):
        self.manager = manager
        super().__init__(server_address, handler_cls)


class JSONHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        while True:
            raw = self.rfile.readline(1024 * 1024)
            if not raw:
                return
            if len(raw) >= 1024 * 1024:
                self._send({"type": "error", "ok": False, "error": "request too large"})
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                self._send({"type": "error", "ok": False, "error": f"invalid json: {exc}"})
                continue
            if not isinstance(msg, dict):
                self._send({"type": "error", "ok": False, "error": "request must be a JSON object"})
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        typ = msg.get("type")
        manager: UpstreamManager = self.server.manager  # type: ignore[attr-defined]
        try:
            if typ == "ping":
                self._send({"type": "pong", "ok": True})
            elif typ == "snapshot":
                self._send(manager.snapshot(limit=msg.get("limit", 100)))
            elif typ == "get_state":
                self._send({"type": "state", "ok": True, "state": manager.get_state()})
            elif typ == "get_nodes":
                self._send({"type": "nodes", "ok": True, "nodes": manager.get_state().get("nodes", [])})
            elif typ == "get_messages":
                self._send({"type": "messages", "ok": True, "messages": manager.store.list(msg.get("limit", 100))})
            elif typ == "clear_messages":
                manager.store.clear()
                self._send({"type": "ack", "ok": True})
            elif typ == "send_text":
                text = str(msg.get("text") or "").strip()
                if not text:
                    self._send({"type": "error", "ok": False, "error": "empty text"})
                else:
                    manager.send_text(text=text, destination_id=msg.get("dest"))
                    self._send({"type": "ack", "ok": True})
            elif typ == "debug":
                self._send({"type": "debug", "ok": True, "state": manager.get_state()})
            elif typ == "room_preview":
                self._send({"ok": True, "preview": manager.preview_room(str(msg.get("url") or ""))})
            elif typ == "room_get_active":
                self._send(manager.get_cached_active_room())
            elif typ == "room_refresh":
                self._send(manager.refresh_active_room())
            elif typ == "room_apply":
                self._send(manager.apply_room(url=str(msg.get("url") or ""), name=str(msg.get("name") or "room")))
            elif typ == "room_rollback":
                self._send(manager.rollback_room())
            elif typ == "room_list_backups":
                self._send(manager.list_backups())
            else:
                self._send({"type": "error", "ok": False, "error": f"unknown command: {typ}"})
        except Exception as exc:
            LOGGER.exception("Proxy command failed: %s", typ)
            self._send({"type": "error", "ok": False, "error": str(exc)})

    def _send(self, payload: dict[str, Any]) -> None:
        try:
            self.wfile.write((json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")) + "\n").encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main() -> None:
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

    host = str(config["proxy"].get("host", "127.0.0.1"))
    port = int(config["proxy"]["port"])
    server = ThreadedJSONServer((host, port), JSONHandler, manager)
    LOGGER.info("Proxy ready on %s:%s", host, port)

    stopping = threading.Event()

    def _stop(signum, frame) -> None:
        if stopping.is_set():
            return
        stopping.set()
        LOGGER.info("Stopping proxy")
        manager.stop()
        # shutdown() must be called from a different thread than serve_forever().
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        manager.stop()
        server.server_close()


if __name__ == "__main__":
    main()
