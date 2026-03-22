from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from pubsub import pub

import meshtastic.serial_interface
import meshtastic.tcp_interface

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("meshtastic-proxy")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_safe(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except Exception:
            return obj.hex()
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, deque)):
        return [json_safe(x) for x in obj]
    if hasattr(obj, "to_dict"):
        return json_safe(obj.to_dict())
    if hasattr(obj, "__dict__"):
        return json_safe(vars(obj))
    return str(obj)


@dataclass
class ProxyStats:
    messages_rx: int = 0
    messages_tx: int = 0
    packets_rx: int = 0
    relay_seen: int = 0
    multi_hop_seen: int = 0
    packets_rx_bad: int = 0
    tx_relay: int = 0
    tx_dropped: int = 0
    total_nodes: int = 0
    online_nodes: int = 0
    last_packet_at: str | None = None
    last_error: str | None = None
    upstream_connected: bool = False
    upstream_mode: str | None = None
    upstream_target: str | None = None
    clients_connected: int = 0
    channel_utilization: float | None = None
    air_util_tx: float | None = None
    battery_level: int | None = None
    voltage: float | None = None

    def to_dict(self):
        return asdict(self)


class Storage:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        with self.lock:
            conn = self._conn()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        direction TEXT NOT NULL,
                        from_id TEXT,
                        to_id TEXT,
                        text TEXT,
                        packet_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_json TEXT
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def add_message(self, direction: str, from_id: str, to_id: str, text: str, packet: dict | None = None):
        with self.lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO messages(ts,direction,from_id,to_id,text,packet_json) VALUES(?,?,?,?,?,?)",
                    (utc_now(), direction, from_id, to_id, text, json.dumps(json_safe(packet), ensure_ascii=False) if packet else None),
                )
                conn.commit()
            finally:
                conn.close()

    def get_messages(self, limit: int = 100):
        with self.lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT ts,direction,from_id,to_id,text FROM messages ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            finally:
                conn.close()
        return [
            {"ts": r[0], "direction": r[1], "from_id": r[2], "to_id": r[3], "text": r[4]}
            for r in reversed(rows)
        ]

    def clear_messages(self):
        with self.lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM messages")
                conn.commit()
            finally:
                conn.close()

    def add_event(self, event_type: str, payload: dict | None = None):
        with self.lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO events(ts,event_type,payload_json) VALUES(?,?,?)",
                    (utc_now(), event_type, json.dumps(json_safe(payload), ensure_ascii=False) if payload else None),
                )
                conn.commit()
            finally:
                conn.close()

    def get_events(self, limit: int = 100):
        with self.lock:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT ts,event_type,payload_json FROM events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            finally:
                conn.close()
        out = []
        for ts, event_type, payload_json in reversed(rows):
            out.append({"ts": ts, "event_type": event_type, "payload": json.loads(payload_json) if payload_json else None})
        return out


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.stats = ProxyStats()
        self.nodes: dict[str, dict] = {}
        self.my_info: dict = {}
        self.config: dict = {}
        self.channels: list[dict] = []
        self.clients: set[asyncio.StreamWriter] = set()

    def snapshot(self):
        with self.lock:
            return {
                "stats": self.stats.to_dict(),
                "nodes": json_safe(self.nodes),
                "my_info": json_safe(self.my_info),
                "config": json_safe(self.config),
                "channels": json_safe(self.channels),
                "client_count": len(self.clients),
            }

    def set_upstream(self, connected: bool, mode: str, target: str, error: str | None = None):
        with self.lock:
            self.stats.upstream_connected = connected
            self.stats.upstream_mode = mode
            self.stats.upstream_target = target
            self.stats.last_error = error

    def update_nodes(self, iface):
        with self.lock:
            self.nodes = json_safe(getattr(iface, "nodes", {}) or {})
            self.my_info = json_safe(getattr(iface, "myInfo", {}) or {})
            self.config = json_safe(getattr(iface, "localConfig", {}) or {})
            self.channels = json_safe(getattr(iface, "channels", []) or [])
            self.stats.total_nodes = len(self.nodes)
            if self.nodes:
                online = 0
                now = time.time()
                for node in self.nodes.values():
                    lh = node.get("lastHeard")
                    if isinstance(lh, (int, float)) and now - lh < 3600:
                        online += 1
                self.stats.online_nodes = online

    def add_client(self, writer: asyncio.StreamWriter):
        with self.lock:
            self.clients.add(writer)
            self.stats.clients_connected = len(self.clients)

    def remove_client(self, writer: asyncio.StreamWriter):
        with self.lock:
            self.clients.discard(writer)
            self.stats.clients_connected = len(self.clients)


class Upstream:
    def __init__(self, state: State, storage: Storage, mode: str, target: str, channel: int):
        self.state = state
        self.storage = storage
        self.mode = mode
        self.target = target
        self.channel = channel
        self.iface = None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="meshtastic-upstream", daemon=True)
        pub.subscribe(self.on_text, "meshtastic.receive.text")
        pub.subscribe(self.on_receive, "meshtastic.receive")

    def start(self):
        self.thread.start()

    def close_iface(self):
        iface, self.iface = self.iface, None
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass

    def _connect(self):
        self.close_iface()
        if self.mode == "serial":
            self.iface = meshtastic.serial_interface.SerialInterface(devPath=self.target)
        else:
            self.iface = meshtastic.tcp_interface.TCPInterface(hostname=self.target)
        self.state.set_upstream(True, self.mode, self.target)
        self.state.update_nodes(self.iface)
        self.storage.add_event("upstream_connected", {"mode": self.mode, "target": self.target})
        logger.info("Connected to Meshtastic via %s -> %s", self.mode, self.target)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self._connect()
                while not self.stop_event.is_set():
                    time.sleep(2)
                    if self.iface is None:
                        raise RuntimeError("Interface lost")
                    try:
                        self.state.update_nodes(self.iface)
                        self._refresh_local_stats()
                    except Exception:
                        pass
            except Exception as exc:
                logger.warning("Upstream loop failed: %s", exc)
                self.state.set_upstream(False, self.mode, self.target, str(exc))
                self.storage.add_event("upstream_error", {"error": str(exc)})
                self.close_iface()
                time.sleep(5)

    def _refresh_local_stats(self):
        if not self.iface:
            return
        nodes_by_num = getattr(self.iface, "nodesByNum", {}) or {}
        my_info = getattr(self.iface, "myInfo", {}) or {}
        if isinstance(my_info, dict):
            my_num = my_info.get("myNodeNum")
        else:
            my_num = getattr(my_info, "myNodeNum", None)
        node = nodes_by_num.get(my_num, {}) if my_num is not None else {}
        local = node.get("localStats", {}) or {}
        dm = node.get("deviceMetrics", {}) or {}
        with self.state.lock:
            s = self.state.stats
            s.tx_relay = int(local.get("numTxRelay") or 0)
            s.tx_dropped = int(local.get("numTxDropped") or 0)
            s.packets_rx_bad = int(local.get("numPacketsRxBad") or 0)
            s.channel_utilization = local.get("channelUtilization", dm.get("channelUtilization"))
            s.air_util_tx = local.get("airUtilTx", dm.get("airUtilTx"))
            s.battery_level = dm.get("batteryLevel")
            s.voltage = dm.get("voltage")

    def send_text(self, text: str, destination_id: str | None = None, channel_index: int | None = None):
        if not self.iface:
            raise RuntimeError("Upstream not connected")
        kwargs = {"text": text, "wantAck": False, "channelIndex": self.channel if channel_index is None else channel_index}
        if destination_id:
            kwargs["destinationId"] = destination_id
        self.iface.sendText(**kwargs)
        with self.state.lock:
            self.state.stats.messages_tx += 1
        self.storage.add_message("out", "me", destination_id or "^all", text)

    def on_text(self, packet, interface=None):
        text = packet.get("decoded", {}).get("text")
        if not text:
            payload = packet.get("decoded", {}).get("payload")
            if isinstance(payload, (bytes, bytearray)):
                try:
                    text = payload.decode("utf-8", errors="replace")
                except Exception:
                    text = None
        if not text:
            return
        from_id = packet.get("fromId", str(packet.get("from", "unknown")))
        to_id = packet.get("toId", str(packet.get("to", "^all")))
        self.storage.add_message("in", from_id, to_id, text, packet)
        with self.state.lock:
            self.state.stats.messages_rx += 1
            self.state.stats.last_packet_at = utc_now()

    def on_receive(self, packet, interface=None):
        with self.state.lock:
            self.state.stats.packets_rx += 1
            if packet.get("relayNode") is not None:
                self.state.stats.relay_seen += 1
            hop_start = packet.get("hopStart")
            hop_limit = packet.get("hopLimit")
            try:
                if hop_start is not None and hop_limit is not None and int(hop_start) > int(hop_limit):
                    self.state.stats.multi_hop_seen += 1
            except Exception:
                pass
            self.state.stats.last_packet_at = utc_now()


async def safe_close_writer(writer: asyncio.StreamWriter):
    try:
        writer.close()
    except Exception:
        return

    try:
        await writer.wait_closed()
    except (ConnectionResetError, BrokenPipeError):
        pass
    except Exception:
        pass


class JSONLServer:
    def __init__(self, state: State, storage: Storage, upstream: Upstream, host: str, port: int):
        self.state = state
        self.storage = storage
        self.upstream = upstream
        self.host = host
        self.port = port
        self.loop = asyncio.new_event_loop()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.state.add_client(writer)
        try:
            try:
                await self.send(writer, {"type": "hello", "peer": str(writer.get_extra_info("peername"))})
                await self.send(writer, {"type": "state", "state": self.state.snapshot()})
            except (ConnectionResetError, BrokenPipeError):
                await safe_close_writer(writer)
                return

            while True:
                raw = await reader.readline()
                if not raw:
                    break
                try:
                    msg = json.loads(raw.decode("utf-8").strip())
                except Exception as exc:
                    try:
                        await self.send(writer, {"type": "error", "error": f"invalid json: {exc}"})
                    except (ConnectionResetError, BrokenPipeError):
                        break
                    continue
                cmd = msg.get("type")
                try:
                    if cmd == "ping":
                        await self.send(writer, {"type": "ack", "command": "ping"})
                    elif cmd == "get_state":
                        await self.send(writer, {"type": "state", "state": self.state.snapshot()})
                    elif cmd == "get_nodes":
                        await self.send(writer, {"type": "nodes", "nodes": self.state.snapshot()["nodes"]})
                    elif cmd == "get_messages":
                        await self.send(writer, {"type": "messages", "messages": self.storage.get_messages(int(msg.get("limit", 100)) )})
                    elif cmd == "get_events":
                        await self.send(writer, {"type": "events", "events": self.storage.get_events(int(msg.get("limit", 100)) )})
                    elif cmd == "send_text":
                        self.upstream.send_text(msg.get("text", ""), msg.get("destination_id"), msg.get("channel_index"))
                        await self.send(writer, {"type": "ack", "command": "send_text"})
                    elif cmd == "clear_messages":
                        self.storage.clear_messages()
                        await self.send(writer, {"type": "ack", "command": "clear_messages"})
                    else:
                        await self.send(writer, {"type": "error", "error": f"unknown command: {cmd}"})
                except (ConnectionResetError, BrokenPipeError):
                    break
        finally:
            self.state.remove_client(writer)
            await safe_close_writer(writer)

    async def send(self, writer, payload):
        writer.write((json.dumps(json_safe(payload), ensure_ascii=False) + "\n").encode("utf-8"))
        try:
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            raise

    async def _run_server(self):
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        logger.info("Proxy ready on %s:%s", self.host, self.port)
        async with server:
            await server.serve_forever()

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._run_server())


def main():
    parser = argparse.ArgumentParser(description="Meshtastic JSONL proxy")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--port", help="Serial port, e.g. /dev/ttyUSB0")
    group.add_argument("--host", help="Node IP/hostname")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=4404)
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--db", default=os.path.join(os.getcwd(), "proxy.db"))
    args = parser.parse_args()

    mode = "serial" if args.port else "tcp"
    target = args.port or args.host
    state = State()
    storage = Storage(args.db)
    upstream = Upstream(state, storage, mode, target, args.channel)
    upstream.start()
    server = JSONLServer(state, storage, upstream, args.listen_host, args.listen_port)
    server.run()


if __name__ == "__main__":
    main()
