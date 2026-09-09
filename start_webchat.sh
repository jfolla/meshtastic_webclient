#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$BASE_DIR/.venv/bin/python"
CFG="$BASE_DIR/app_config.json"
CFG_EXAMPLE="$BASE_DIR/app_config.example.json"
APP="$BASE_DIR/app.py"
export PATH="$BASE_DIR/.venv/bin:$PATH"
export PYTHONUNBUFFERED=1

if [[ ! -x "$PY" ]]; then
  echo "ERROR: virtualenv Python not found: $PY" >&2
  exit 1
fi
if [[ ! -e "$CFG" ]]; then
  if [[ ! -r "$CFG_EXAMPLE" ]]; then
    echo "ERROR: neither app_config.json nor app_config.example.json is available" >&2
    exit 1
  fi
  cp "$CFG_EXAMPLE" "$CFG"
  chmod 600 "$CFG"
  echo "Created $CFG from app_config.example.json" >&2
fi
if [[ ! -r "$CFG" ]]; then
  echo "ERROR: configuration not readable: $CFG" >&2
  exit 1
fi

cd "$BASE_DIR"

PROXY_PID=""
WEB_PID=""
cleanup() {
  trap - EXIT INT TERM
  for pid in "$WEB_PID" "$PROXY_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$WEB_PID" "$PROXY_PID"; do
    if [[ -n "$pid" ]]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
}
trap 'cleanup; exit 143' TERM
trap 'cleanup; exit 130' INT
trap cleanup EXIT

"$PY" -m proxy.main --config "$CFG" &
PROXY_PID=$!

proxy_ready=0
for _ in $(seq 1 30); do
  if ! kill -0 "$PROXY_PID" 2>/dev/null; then
    echo "ERROR: proxy exited during startup" >&2
    wait "$PROXY_PID" || true
    exit 1
  fi
  if "$PY" - "$CFG" <<'PY'
import json, socket, sys
from security import loopback_host
cfg_path = sys.argv[1]
with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)
host = str(cfg.get("proxy", {}).get("host", "127.0.0.1"))
port = int(cfg.get("proxy", {}).get("port", 4404))
host = loopback_host(host)
try:
    with socket.create_connection((host, port), timeout=1.0) as s:
        s.sendall(b'{"type":"ping"}\n')
        s.settimeout(1.0)
        data = s.recv(4096)
        if b'"type":"pong"' in data and b'"ok":true' in data:
            raise SystemExit(0)
except Exception:
    pass
raise SystemExit(1)
PY
  then
    proxy_ready=1
    break
  fi
  sleep 1
done

if [[ "$proxy_ready" -ne 1 ]]; then
  echo "ERROR: proxy did not become ready within 30 seconds" >&2
  exit 1
fi

"$PY" "$APP" --config "$CFG" &
WEB_PID=$!

# Supervise both children. If either component exits, stop the other one and let systemd restart the service.
set +e
wait -n "$PROXY_PID" "$WEB_PID"
status=$?
set -e
exit "$status"
