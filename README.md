# Meshtastic Web Chat

**Version:** v0.6.9  
**Project root:** `meshtastic_webchat`  
**Service:** `meshtastic-webchat.service`

---

# Overview

Meshtastic Web Chat provides a **browser-based interface** for interacting with a Meshtastic node using a **proxy-first architecture**.

Unlike the standard Meshtastic CLI or mobile apps, this project ensures:

- a **single connection** to the node (serial or TCP)
- a **stable backend proxy**
- a **web UI for interaction**

---

# Architecture

```
                +---------------------+
                |      Browser        |
                |  (UI - index.html) |
                +----------+----------+
                           |
                           v
                +---------------------+
                |       app.py        |
                |   (Flask Web App)   |
                +----------+----------+
                           |
                           v
                +---------------------+
                |    proxy/main.py    |
                |  (Meshtastic API)   |
                +----------+----------+
                           |
          +----------------+----------------+
          |                                 |
          v                                 v
   Serial Interface                   TCP Interface
 (/dev/ttyUSB0)                   (host:tcp_port)
```

---

# Key Concepts

## Proxy-first model

- Only **one connection** exists to the node
- Proxy manages:
  - messaging
  - channel operations
  - node state
- UI communicates only with backend

## Why this matters

- prevents serial conflicts
- avoids multiple clients fighting for the node
- allows safe channel configuration from web

---

# Features

## Core
- systemd-managed service
- automatic proxy startup
- serial + TCP support
- message send/receive
- node discovery
- address book

## UI
Tabbed interface:
- Messages
- Statistics
- Nodes
- Address Book
- Configuration
- Channels

## Channels (extended)
- list channels
- set TX channel
- join via URL
- delete secondary channels
- **post-operation verification**

---

# Repository Structure

```
meshtastic_webchat/
├── app.py
├── app_config.json
├── address_book.json
├── start_webchat.sh
├── meshtastic-webchat.service
├── requirements.txt
├── README.md
├── proxy/
│   └── main.py
├── templates/
│   └── index.html
└── docs/
```

---

# Installation

```
cd /home/meshtastic
cp -a meshtastic_webchat meshtastic_webchat.bak.$(date +%Y%m%d_%H%M%S)

cd meshtastic_webchat
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
chmod +x start_webchat.sh
```

---

# Configuration

Edit:

```
/home/meshtastic/meshtastic_webchat/app_config.json
```

## Serial node

```
"node": {
  "mode": "serial",
  "port": "/dev/ttyUSB0",
  "channel": 0
}
```

## TCP node

```
"node": {
  "mode": "tcp",
  "host": "192.168.1.50",
  "tcp_port": 4403,
  "channel": 0
}
```

---

# Service setup

```
sudo cp meshtastic-webchat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
```

---

# Access UI

```
https://SERVER_IP:8088
```

---

# Usage

## Messages
- send broadcast
- send direct messages

## Nodes
- view discovered nodes
- select recipients

## Address Book
- map node_id → alias
- simplify messaging

## Channels
- change TX channel
- join channel via URL
- verify channel applied

---

# Channel Logic (Important)

When setting a channel:

1. proxy updates internal TX index
2. proxy requests node channel list
3. verifies index exists
4. returns validated result

This avoids:
- silent failures
- inconsistent UI state
- broken serial workflows

---

# Troubleshooting

## Proxy empty response

Usually means:
- proxy crash
- API mismatch
- wrong meshtastic library version

Check:

```
journalctl -u meshtastic-webchat -n 100
```

---

## Serial busy

```
lsof /dev/ttyUSB0
```

---

## TCP issues

```
nc -vz HOST PORT
```

---

# Version Compatibility

Requires compatible `meshtastic` Python library.

Check:

```
pip show meshtastic
```

---

# Changelog

## v0.6.9
- tabbed UI integrated
- channels tab added
- verified channel selection
- TCP support extended
- proxy error handling fixed
- start script bug fixed (${PORT})
- full documentation

## v0.6.8
- startup stabilization
- proxy reliability improvements

## v0.6.7
- channel verification added

## v0.6.6
- initial channel management

## v0.6.5
- tabbed UI introduction

---

# Security Notes

- designed for internal/trusted networks
- use firewall rules if exposed
- ad-hoc SSL is not production-grade

---

# License

See LICENSE file
