#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$BASE_DIR/.venv/bin/python"
CFG="$BASE_DIR/app_config.json"
APP="$BASE_DIR/app.py"

cleanup() {
  if [[ -n "${PROXY_PID:-}" ]]; then
    kill "$PROXY_PID" 2>/dev/null || true
    wait "$PROXY_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

"$PY" -m proxy.main --config "$CFG" &
PROXY_PID=$!

# Wait until the local proxy answers a JSONL ping.
for _ in $(seq 1 30); do
  if "$PY" - <<'PY'
import json, socket, sys
host = "127.0.0.1"
port = 4404
try:
    with socket.create_connection((host, port), timeout=1.5) as s:
        s.sendall((json.dumps({"type": "ping"}) + "\n").encode())
        s.settimeout(1.5)
        data = s.recv(4096)
        if b'"type": "pong"' in data:
            sys.exit(0)
except Exception:
    pass
sys.exit(1)
PY
  then
    break
  fi
  sleep 1
done

exec "$PY" "$APP" --config "$CFG"
