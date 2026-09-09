#!/usr/bin/env python3
from __future__ import annotations

import json
import py_compile
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
VERSION = "0.7.5-beta"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")


for path in (BASE / "app.py", BASE / "security.py", BASE / "proxy" / "main.py"):
    py_compile.compile(str(path), doraise=True)

cfg = json.loads((BASE / "app_config.example.json").read_text(encoding="utf-8"))
html = (BASE / "templates" / "index.html").read_text(encoding="utf-8")
app = (BASE / "app.py").read_text(encoding="utf-8")
proxy = (BASE / "proxy" / "main.py").read_text(encoding="utf-8")
start = (BASE / "start_webchat.sh").read_text(encoding="utf-8")

require(cfg.get("version") == VERSION, "app_config.example.json version mismatch")
require(f'VERSION = "{VERSION}"' in app, "app.py version mismatch")
require("nav-channels" in html and "tab-channels" in html, "Channels tab missing")
require("Paste channel URL / hash" in html, "Channel URL field missing")
require("previewRoomBtn" in html and "/api/rooms/preview" in app, "Channel preview workflow missing")
require('"type": "snapshot"' in app and 'typ == "snapshot"' in proxy, "Consolidated snapshot path missing")
require("node.setURL(url)" in proxy and "node._sendAdmin" in proxy and "get_channel_request" in proxy, "Direct Meshtastic channel API missing")
require("subprocess" not in proxy, "Proxy still uses subprocess/CLI")
require("wait -n" in start, "Child-process supervision missing")
require("cfg.get(\"proxy\"" in start, "Startup proxy check is not config-driven")
require(not re.search(r"datetime\.utcnow\s*\(", app + proxy), "Deprecated datetime.utcnow() found")
print(f"OK: static self-test passed for {VERSION}")
