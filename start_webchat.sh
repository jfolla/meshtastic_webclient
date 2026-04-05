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

read_proxy_value() {
  local key="$1"
  "$PY" - "$CFG" "$key" <<'PY'
import json, sys
cfg_path, key = sys.argv[1], sys.argv[2]
with open(cfg_path, 'r', encoding='utf-8') as f:
    cfg = json.load(f)
proxy = cfg.get('proxy', {})
if key == 'host':
    print(proxy.get('host', '127.0.0.1'))
elif key == 'port':
    print(int(proxy.get('port', 4404)))
else:
    raise SystemExit(2)
PY
}

PROXY_HOST="$(read_proxy_value host)"
PROXY_PORT="$(read_proxy_value port)"

"$PY" -m proxy.main --config "$CFG" &
PROXY_PID=$!

for _ in $(seq 1 30); do
  if PROXY_HOST="$PROXY_HOST" PROXY_PORT="$PROXY_PORT" "$PY" - <<'PY'
import json, os, socket, sys
host = os.environ['PROXY_HOST']
port = int(os.environ['PROXY_PORT'])
try:
    with socket.create_connection((host, port), timeout=1.5) as s:
        s.sendall((json.dumps({'type': 'ping'}) + '\n').encode())
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
