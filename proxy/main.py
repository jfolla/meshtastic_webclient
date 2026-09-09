#!/usr/bin/env python3
from __future__ import annotations

from contextlib import closing
from security import loopback_host

import argparse
import base64
import json
import logging
import os
import signal
import socket
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
from meshtastic.protobuf import apponly_pb2, admin_pb2, channel_pb2

LOGGER = logging.getLogger("meshtastic_proxy")
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "proxy_messages.db"
BACKUPS_PATH = BASE_DIR / "room_backups.json"
MAX_BACKUPS = 20
VERIFICATION_PATH = BASE_DIR / "channel_verification.json"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    loopback_host(config.get("proxy", {}).get("host", "127.0.0.1"))
    return config


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
        with self.lock, closing(self._connect()) as conn, conn:
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

            conn.execute("UPDATE messages SET raw_json = NULL WHERE raw_json IS NOT NULL")
            self._prune(conn)

    @staticmethod
    def _prune(conn):
        conn.execute("DELETE FROM messages WHERE id < COALESCE("
                     "(SELECT id FROM messages ORDER BY id DESC LIMIT 1 OFFSET 9999), 0)")

    def add(self, direction: str, from_id: str, to_id: str, text: str, raw_packet: Optional[dict[str, Any]] = None) -> int:
        with self.lock, closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO messages (ts, direction, from_id, to_id, text) VALUES (?, ?, ?, ?, ?)",
                (now_iso(), direction, from_id, to_id, text),
            )
            self._prune(conn)
            return int(cur.lastrowid)

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        with self.lock, closing(self._connect()) as conn, conn:
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
        with self.lock, closing(self._connect()) as conn, conn:
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
    active_room_verified: bool = False

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
        self.room_operation_lock = threading.RLock()
        self.expected_room_url = None
        if VERIFICATION_PATH.exists():
            saved = json.loads(VERIFICATION_PATH.read_text(encoding="utf-8"))
            self.expected_room_url = saved.get("expected_url")
            if self.expected_room_url:
                self.preview_room(self.expected_room_url)
        self.pending_rollback_url = None
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
            self.state.active_room_verified = False
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
        LOGGER.debug("Opening Meshtastic %s connection to %s", mode, target)
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
        failures = 0
        while not self.stop_event.is_set():
            with self.iface_lock:
                iface = self.iface
            if iface is None:
                try:
                    self._connect()
                    failures = 0
                except Exception as exc:
                    delay = (2, 5, 10, 20, 30)[min(failures, 4)]
                    failures += 1
                    with self.state_lock:
                        self.state.upstream_connected = False
                        self.state.last_error = type(exc).__name__
                    if failures == 1:
                        LOGGER.warning("Upstream unavailable; reconnect backoff 2/5/10/20/30 seconds (%s)", type(exc).__name__)
                    self.stop_event.wait(delay)
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
            raise ValueError("Invalid Meshtastic channel URL/hash") from exc
        if not 1 <= len(channel_set.settings) <= 8:
            raise ValueError("Channel URL must contain 1 to 8 channels")

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

    @staticmethod
    def _decode_url(url):
        encoded = url.split("#", 1)[1]
        result = apponly_pb2.ChannelSet()
        result.ParseFromString(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        return result

    @staticmethod
    def _encode_set(channel_set):
        return "https://meshtastic.org/e/#" + base64.urlsafe_b64encode(
            channel_set.SerializeToString(deterministic=True)).decode().rstrip("=")

    def _read_channel_urls(self) -> tuple[str, str]:
        """Read correlated admin responses, never trust the mutable Node cache."""
        node = self._local_node()
        deadline = time.monotonic() + 12.0

        def request_admin(message, field, index=None):
            ready = threading.Event()
            result = []

            def received(packet):
                try:
                    if int(packet.get("from", -1)) != int(node.nodeNum):
                        return
                    raw = packet.get("decoded", {}).get("admin", {}).get("raw")
                    if raw is None or not raw.HasField(field):
                        return
                    value = getattr(raw, field)
                    if index is not None and value.index != index:
                        return
                    copy = type(value)()
                    copy.CopyFrom(value)
                    result.append(copy)
                    ready.set()
                except (ValueError, TypeError, AttributeError):
                    return

            node._sendAdmin(message, wantResponse=True, onResponse=received)
            if not ready.wait(max(0, deadline - time.monotonic())) or not result:
                raise TimeoutError("Device read-back incomplete")
            if self._local_node() is not node:
                raise RuntimeError("Device disconnected during read-back")
            return result[0]

        channels = []
        for index in range(8):
            message = admin_pb2.AdminMessage()
            message.get_channel_request = index + 1
            channels.append(request_admin(message, "get_channel_response", index))
        message = admin_pb2.AdminMessage()
        message.get_config_request = admin_pb2.AdminMessage.LORA_CONFIG
        config = request_admin(message, "get_config_response")
        if not config.HasField("lora"):
            raise RuntimeError("Missing LoRa read-back")
        full = apponly_pb2.ChannelSet()
        primary = apponly_pb2.ChannelSet()
        for channel in channels:
            if channel.role in (channel_pb2.Channel.PRIMARY, channel_pb2.Channel.SECONDARY):
                full.settings.append(channel.settings)
            if channel.role == channel_pb2.Channel.PRIMARY:
                primary.settings.append(channel.settings)
        if len(primary.settings) != 1:
            raise RuntimeError("Device has no unique primary channel")
        full.lora_config.CopyFrom(config.lora)
        primary.lora_config.CopyFrom(config.lora)
        # Only publish a complete device read; callbacks never mutate cached settings.
        node.channels = channels
        node.localConfig.lora.CopyFrom(config.lora)
        return self._encode_set(primary), self._encode_set(full)

    def refresh_active_room(self) -> dict[str, Any]:
        if not self.room_operation_lock.acquire(blocking=False):
            return self.get_cached_active_room()
        try:
            try:
                _primary, complete = self._read_channel_urls()
            except Exception:
                with self.state_lock:
                    self.state.active_room_verified = False
                raise
            verified = (self.expected_room_url is None or
                        self._decode_url(complete) == self._decode_url(self.expected_room_url))
            with self.state_lock:
                self.state.active_room_url = complete
                self.state.active_room_checked_at = now_iso()
                self.state.active_room_verified = verified
            if verified and self.pending_rollback_url:
                backups = self._load_backups()
                if backups and backups[-1].get("previous_full_url") == self.pending_rollback_url:
                    self._save_backups(backups[:-1])
                self.pending_rollback_url = None
            return self.get_cached_active_room()
        finally:
            self.room_operation_lock.release()

    def get_cached_active_room(self) -> dict[str, Any]:
        with self.state_lock:
            url = self.state.active_room_url
            checked_at = self.state.active_room_checked_at
            verified = self.state.active_room_verified and self.state.upstream_connected
        if not url:
            return {"ok": True, "active_room": None}
        active = self.preview_room(url)
        active.update(checked_at=checked_at, verified=verified,
                      status="verified" if verified else "verification_pending")
        return {"ok": True, "active_room": active}

    def list_backups(self) -> dict[str, Any]:
        return {"ok": True, "backups": self._load_backups()}

    def _set_room_url(self, url: str) -> None:
        node = self._local_node()
        count = len(self._decode_url(url).settings)
        if not 1 <= count <= 8:
            raise ValueError("Channel count must be between 1 and 8")
        with self.state_lock:
            self.state.active_room_verified = False
        atomic_save_json(VERIFICATION_PATH, {"expected_url": url})
        self.expected_room_url = url
        try:
            node.setURL(url)
            # setURL overwrites the supplied slots but leaves extra channels enabled.
            for index in range(count, 8):
                channel = channel_pb2.Channel(index=index, role=channel_pb2.Channel.DISABLED)
                node.channels[index] = channel
                node.writeChannel(index)
        except SystemExit as exc:
            raise RuntimeError("Meshtastic rejected the channel URL") from exc
        self.stop_event.wait(1.0)

    def _verify_after_apply(self, requested_url):
        try:
            return self.refresh_active_room()["active_room"]
        except Exception as exc:
            LOGGER.info("Applied; device verification pending (%s)", type(exc).__name__)
            with self.state_lock:
                self.state.active_room_url = requested_url
                self.state.active_room_checked_at = None
                self.state.active_room_verified = False
            return self.get_cached_active_room()["active_room"]

    def apply_room(self, url: str, name: str = "room") -> dict[str, Any]:
        if not self.room_operation_lock.acquire(blocking=False):
            return {"ok": False, "error": "Another channel operation is already running"}
        try:
            preview = self.preview_room(url)
            requested = preview["full_url"]
            # A complete fresh backup is required before the first write.
            primary, complete = self._read_channel_urls()
            backups = self._load_backups()
            changed = self._decode_url(complete) != self._decode_url(requested)
            if changed:
                backups.append({"ts": now_iso(), "name": name, "previous_full_url": complete,
                                "previous_primary_url": primary})
                self._save_backups(backups)
            self.pending_rollback_url = None
            self._set_room_url(requested)
            active = self._verify_after_apply(requested)
            return {"ok": True, "message": "Verified" if active["verified"] else "Applied / verification pending",
                    "active_room": active, "backup_created": changed,
                    "backups": self._load_backups(), "preview": preview}
        finally:
            self.room_operation_lock.release()

    def rollback_room(self) -> dict[str, Any]:
        if not self.room_operation_lock.acquire(blocking=False):
            return {"ok": False, "error": "Another channel operation is already running"}
        try:
            backups = self._load_backups()
            if not backups:
                return {"ok": False, "error": "No backups available"}
            previous = backups[-1].get("previous_full_url")
            preview = self.preview_room(previous)
            self.pending_rollback_url = previous
            self._set_room_url(preview["full_url"])
            active = self._verify_after_apply(preview["full_url"])
            return {"ok": True, "message": "Verified" if active["verified"] else "Applied / verification pending",
                    "active_room": active, "backups": self._load_backups()}
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
        host = loopback_host(server_address[0])
        self.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        self.manager = manager
        super().__init__((host, server_address[1]), handler_cls)


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
            LOGGER.warning("Proxy command failed: %s (%s)", typ, type(exc).__name__)
            self._send({"type": "error", "ok": False, "error": "Command failed: " + type(exc).__name__})

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

    host = loopback_host(config["proxy"].get("host", "127.0.0.1"))
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
