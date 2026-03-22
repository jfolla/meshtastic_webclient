#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$BASE_DIR/.venv/bin/python"
CFG="$BASE_DIR/app_config.json"
APP="$BASE_DIR/app.py"
PROXY="$BASE_DIR/proxy/main.py"

cleanup() {
  if [[ -n "${PROXY_PID:-}" ]]; then
    kill "$PROXY_PID" 2>/dev/null || true
    wait "$PROXY_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

MODE=$($VENV - <<PY
import json
with open(r"$CFG", "r", encoding="utf-8") as f:
    cfg = json.load(f)
print(cfg.get("node", {}).get("mode", "serial"))
PY
)
PROXY_HOST=$($VENV - <<PY
import json
with open(r"$CFG", "r", encoding="utf-8") as f:
    cfg = json.load(f)
print(cfg.get("proxy", {}).get("host", "127.0.0.1"))
PY
)
PROXY_PORT=$($VENV - <<PY
import json
with open(r"$CFG", "r", encoding="utf-8") as f:
    cfg = json.load(f)
print(cfg.get("proxy", {}).get("port", 4404))
PY
)
CHANNEL=$($VENV - <<PY
import json
with open(r"$CFG", "r", encoding="utf-8") as f:
    cfg = json.load(f)
print(cfg.get("node", {}).get("channel", 0))
PY
)

if [[ "$MODE" == "serial" ]]; then
  NODE_PORT=$($VENV - <<PY
import json
with open(r"$CFG", "r", encoding="utf-8") as f:
    cfg = json.load(f)
print(cfg.get("node", {}).get("port", "/dev/ttyUSB0"))
PY
)
  "$VENV" -m proxy.main --port "$NODE_PORT" --listen-host "$PROXY_HOST" --listen-port "$PROXY_PORT" --channel "$CHANNEL" --db "$BASE_DIR/proxy.db" &
elif [[ "$MODE" == "tcp" ]]; then
  NODE_HOST=$($VENV - <<PY
import json
with open(r"$CFG", "r", encoding="utf-8") as f:
    cfg = json.load(f)
print(cfg.get("node", {}).get("host", ""))
PY
)
  "$VENV" -m proxy.main --host "$NODE_HOST" --listen-host "$PROXY_HOST" --listen-port "$PROXY_PORT" --channel "$CHANNEL" --db "$BASE_DIR/proxy.db" &
else
  echo "Unsupported node.mode: $MODE"
  exit 1
fi
PROXY_PID=$!

for i in $(seq 1 30); do
  if $VENV - <<PY
import socket
s = socket.socket()
s.settimeout(1)
try:
    s.connect(("$PROXY_HOST", int("$PROXY_PORT")))
    s.close()
    raise SystemExit(0)
except Exception:
    raise SystemExit(1)
PY
  then
    break
  fi
  sleep 1
done

exec "$VENV" "$APP" --config "$CFG"
