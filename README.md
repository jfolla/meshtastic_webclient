# Meshtastic Web Chat

**Version:** v0.5.3  
**Internal folder path:** `meshtastic_webchat`  
**Service name:** `meshtastic-webchat.service`

Meshtastic Web Chat is a lightweight browser interface for Meshtastic nodes. This build uses a **proxy-first** architecture while still keeping deployment simple: you run **one single systemd service** and **one single project directory**.

The service starts:
1. a local JSONL proxy/connector that owns the only direct connection to the Meshtastic node, and then
2. the Flask web application that talks only to that local proxy.

This helps reduce issues seen with direct TCP access to some Wi-Fi/TCP nodes, especially when multiple clients compete for the same Meshtastic API session.

## Screenshot

![Meshtastic Web Chat UI](docs/screenshot-ui-v0.5.3.jpg)

## What this project is for

This project is designed to provide:
- a simple web chat for Meshtastic
- message history in the browser
- node list and basic statistics
- a cleaner browser experience than the stock web client in some setups
- a safer deployment model for TCP-only nodes by placing a local connector in front of the node

## Architecture

```text
Meshtastic node <-> local proxy (same service) <-> web app <-> browser
```

### Why the proxy exists

The local proxy helps by:
- keeping **one single upstream connection** to the node
- exposing a simple local API for the web app
- reducing direct client contention against the node
- making the web UI more stable when the node connection drops and reconnects

## Features

- single service deployment (`meshtastic-webchat.service`)
- single project path (`meshtastic_webchat`)
- serial or TCP node support
- SSL adhoc support for local HTTPS
- message send / receive view
- node list
- right sidebar with basic statistics
- debug endpoint
- config export/import through API
- UI languages: English, Italian, French

## Requirements

- Python 3.10+
- Linux with systemd
- a Meshtastic-compatible node
- for serial mode: serial device access (`dialout` usually required)

## Project files

```text
meshtastic_webchat/
├── app.py
├── app_config.json
├── start_webchat.sh
├── meshtastic-webchat.service
├── requirements.txt
├── docs/
│   └── screenshot-ui-v0.5.3.jpg
├── proxy/
│   ├── __init__.py
│   └── main.py
└── templates/
    └── index.html
```

## Installation

### 1. Extract the package

Extract the archive so that the project lives in:

```text
/home/meshtastic/meshtastic_webchat
```

### 2. Create and populate the virtual environment

```bash
cd /home/meshtastic/meshtastic_webchat
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Configure the node connection

Edit `app_config.json`.

#### Serial example

```json
{
  "version": "0.5.3",
  "node": {
    "mode": "serial",
    "host": "",
    "port": "/dev/ttyUSB0",
    "channel": 0
  },
  "proxy": {
    "host": "127.0.0.1",
    "port": 4404
  },
  "web": {
    "listen_host": "0.0.0.0",
    "listen_port": 8088,
    "ssl_adhoc": true
  },
  "ui": {
    "default_language": "en"
  }
}
```

#### TCP example

```json
{
  "version": "0.5.3",
  "node": {
    "mode": "tcp",
    "host": "192.168.0.18",
    "port": "",
    "channel": 0
  },
  "proxy": {
    "host": "127.0.0.1",
    "port": 4404
  },
  "web": {
    "listen_host": "0.0.0.0",
    "listen_port": 8088,
    "ssl_adhoc": true
  },
  "ui": {
    "default_language": "en"
  }
}
```

### 4. If using serial, allow access

```bash
sudo usermod -a -G dialout meshtastic
```

Log out and back in, or reboot.

## Manual start

```bash
cd /home/meshtastic/meshtastic_webchat
source .venv/bin/activate
./start_webchat.sh
```

## systemd installation

Copy the service file:

```bash
sudo cp /home/meshtastic/meshtastic_webchat/meshtastic-webchat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
```

Check status and logs:

```bash
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -f
```

## Web access

With the default config, the web app listens on port `8088`.

If `ssl_adhoc` is enabled in `app_config.json`, open:

```text
https://YOUR-HOST:8088
```

Your browser will warn about the self-signed certificate. That is expected.

## API endpoints

- `/api/status`
- `/api/stats`
- `/api/messages`
- `/api/nodes`
- `/api/debug`
- `/api/send`
- `/api/clear`
- `/api/config`

## Notes and limitations

- This package uses a local JSONL proxy, not the native Meshtastic mobile protocol.
- It is intended for browser usage, not direct mobile app compatibility.
- Some advanced Meshtastic settings are not exposed in the UI.
- For unstable TCP-only nodes, this approach is generally safer than direct browser-to-node patterns.

## GitHub publishing notes

This package is ready to publish as a repository baseline. The screenshot path in this README is already correct for a root-level project layout.
