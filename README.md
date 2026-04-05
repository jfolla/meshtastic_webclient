# Meshtastic Web Chat

**Version:** v0.6.9  
**Project root:** `meshtastic_webchat`  
**Service unit:** `meshtastic-webchat.service`

Meshtastic Web Chat is a browser-based interface for a Meshtastic node, built around a **proxy-first architecture**. The browser never connects to the radio directly. Instead, the web app talks to a local proxy, and the proxy owns the single upstream connection to the Meshtastic node.

![Meshtastic Web Chat UI](docs/screenshot-ui-v0.6.9.jpg)

This makes the project especially useful when:

- the node is connected over **serial** and the port must not be opened by multiple clients
- the node is reachable over **TCP** and you want one stable backend session
- you want a simple local UI for messaging, node visibility, address-book aliases, and channel handling

---

## Architecture

```text
Browser  <->  Flask app (`app.py`)  <->  local proxy (`proxy/main.py`)  <->  Meshtastic node
```

### Detailed flow

```text
                +----------------------+
                |       Browser        |
                |   templates/index    |
                +----------+-----------+
                           |
                           v
                +----------------------+
                |        app.py        |
                |   Flask web server   |
                +----------+-----------+
                           |
                           v
                +----------------------+
                |    proxy/main.py     |
                |  Meshtastic backend  |
                +----------+-----------+
                           |
          +----------------+----------------+
          |                                 |
          v                                 v
   Serial interface                    TCP interface
   /dev/ttyUSB0, etc.                  host + tcp_port
```

### Why the proxy matters

Only the proxy owns the real connection to the node.

That means:

- with a **serial** node, the serial port is busy **for the backend proxy**, not for a second CLI session
- with a **TCP** node, the TCP session is owned by the backend proxy
- the browser stays stateless and talks only to the web app

This avoids the classic conflict where multiple clients compete for the same serial port or network endpoint.

---

## Features

### Core

- single-service startup via `meshtastic-webchat.service`
- local proxy launched by `start_webchat.sh`
- support for:
  - **serial** nodes
  - **TCP** nodes
- message polling and caching in local SQLite
- node discovery and node list rendering
- local address book (`node_id -> alias`)
- broadcast and direct-message sending
- configuration export/import
- backend status and debug endpoints

### UI

Tabbed interface with:

- **Messages**
- **Statistics**
- **Nodes**
- **Address Book**
- **Configuration**
- **Channels**

### Channels

- list channels from the connected node
- select the channel used for **TX**
- join a channel by Meshtastic URL
- delete removable channels
- post-action verification through the backend to reduce silent failures

---

## Repository layout

```text
meshtastic_webchat/
├── app.py
├── app_config.json
├── address_book.json
├── requirements.txt
├── start_webchat.sh
├── meshtastic-webchat.service
├── README.md
├── CHANGELOG.md
├── LICENSE
├── proxy/
│   ├── __init__.py
│   └── main.py
├── templates/
│   └── index.html
└── docs/
    ├── screenshot-ui-v0.6.0.jpg
    ├── screenshot-ui-v0.6.1.jpg
    ├── screenshot-ui-v0.6.4.jpg
    └── screenshot-ui-v0.6.9.jpg
    ├── screenshot-ui-v0.6.1.jpg
    └── screenshot-ui-v0.6.4.jpg
```

---

## Requirements

- Linux with `systemd`
- Python 3.10 or newer recommended
- a Meshtastic node reachable either by:
  - serial device, for example `/dev/ttyUSB0`
  - TCP host, for example `192.168.1.50:4403`
- user permissions to access the serial device when using serial mode

Python dependencies are declared in `requirements.txt`:

- `Flask`
- `meshtastic`
- `pypubsub`
- `pyOpenSSL`

---

## Installation

### 1. Clone or copy the project

Recommended deployment path:

```text
/home/meshtastic/meshtastic_webchat
```

### 2. Create the virtual environment and install dependencies

```bash
cd /home/meshtastic/meshtastic_webchat
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
chmod +x start_webchat.sh
```

### 3. Edit the configuration

Main config file:

```text
/home/meshtastic/meshtastic_webchat/app_config.json
```

---

## Configuration

The project supports both **serial** and **TCP** upstream node modes.

### Serial example

```json
{
  "version": "0.6.9",
  "node": {
    "mode": "serial",
    "host": "",
    "port": "/dev/ttyUSB0",
    "channel": 0,
    "tcp_port": 4403
  },
  "proxy": {
    "host": "127.0.0.1",
    "port": 4404
  },
  "web": {
    "listen_host": "0.0.0.0",
    "listen_port": 8088,
    "ssl_adhoc": true
  }
}
```

### TCP example

```json
{
  "version": "0.6.9",
  "node": {
    "mode": "tcp",
    "host": "192.168.1.50",
    "port": "",
    "channel": 0,
    "tcp_port": 4403
  },
  "proxy": {
    "host": "127.0.0.1",
    "port": 4404
  },
  "web": {
    "listen_host": "0.0.0.0",
    "listen_port": 8088,
    "ssl_adhoc": true
  }
}
```

### Config fields

#### `node.mode`

Accepted values:

- `serial`
- `tcp`

#### `node.port`

Used for **serial** mode. Example:

```json
"/dev/ttyUSB0"
```

#### `node.host`

Used for **tcp** mode. Example:

```json
"192.168.1.50"
```

#### `node.tcp_port`

Used for **tcp** mode. Typical Meshtastic TCP port:

```json
4403
```

#### `node.channel`

Default TX channel index used by the web app and proxy.

#### `proxy.host` / `proxy.port`

Bind address and port used by the internal backend proxy.

#### `web.listen_host` / `web.listen_port`

Address and port used by the Flask web server.

#### `web.ssl_adhoc`

When `true`, Flask starts with an ad-hoc HTTPS certificate.

---

## Systemd setup

Install the service:

```bash
cd /home/meshtastic/meshtastic_webchat
sudo cp meshtastic-webchat.service /etc/systemd/system/meshtastic-webchat.service
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
```

Check status:

```bash
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -f
```

---

## Accessing the UI

Open the configured web endpoint in your browser.

Default example:

```text
https://YOUR_SERVER:8088
```

If `ssl_adhoc` is enabled, your browser will show a certificate warning the first time.

---

## Usage guide

### Messages tab

Use this tab to:

- read cached messages
- send broadcast messages
- send direct messages to selected nodes

### Statistics tab

Shows runtime information such as:

- application version
- proxy connectivity
- backend connectivity
- cached message count
- RX/TX counters
- known node count
- selected TX channel
- address book size

### Nodes tab

Use this tab to:

- inspect discovered nodes
- select a message recipient
- save aliases through the address book workflow

### Address Book tab

The address book is a local alias map between a Meshtastic node ID and a friendly label.

Example:

```text
!9ee86a74 -> OFFICE
```

Aliases are local to this project and do not change the remote node name stored in firmware.

### Configuration tab

Use this tab to:

- export the active JSON config
- import a replacement JSON config

After importing a new config, restart the service.

### Channels tab

Use this tab to:

- refresh the channel list from the node
- set the TX channel used by the web app
- join a channel using a Meshtastic URL
- delete non-primary channels when supported

### Channel verification logic

After a TX-channel change or URL join operation, the backend refreshes channel information from the node and returns a validated result. This reduces the chance of showing a false success in the UI.

---

## API overview

The Flask app exposes browser-facing API endpoints such as:

- `GET /api/status`
- `GET /api/state`
- `GET /api/nodes`
- `GET /api/messages?limit=100`
- `POST /api/send`
- `POST /api/clear`
- `GET /api/debug`
- `GET /api/config/export`
- `POST /api/config/import`
- `GET /api/address-book`
- `POST /api/address-book`
- `DELETE /api/address-book/<node_id>`
- `GET /api/channels`
- `POST /api/channels/select`
- `POST /api/channels/join`
- `DELETE /api/channels/<index>`

The web app uses these endpoints to communicate with the proxy and the local cache.

---

## Startup sequence

`start_webchat.sh` performs the following steps:

1. resolves project paths
2. reads proxy host and port from `app_config.json`
3. starts `proxy.main`
4. waits until the proxy answers `ping` with `pong`
5. launches `app.py --config app_config.json`

This prevents the web app from starting before the proxy is ready.

---

## Troubleshooting

### Service does not start

```bash
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -n 100 --no-pager
```

### Serial port busy

Likely causes:

- wrong serial path
- another process already using the serial port
- missing permissions

Useful checks:

```bash
ls -l /dev/ttyUSB*
sudo lsof /dev/ttyUSB0
```

### TCP node unreachable

Check host, route, firewall, and port:

```bash
nc -vz 192.168.1.50 4403
```

### Empty response from proxy

Usually means the proxy failed before returning JSON.

Common causes:

- old proxy process still running
- mismatch between `app.py` and `proxy/main.py`
- unsupported installed `meshtastic` library API
- runtime exception during channel operations

Useful checks:

```bash
sudo systemctl status meshtastic-webchat --no-pager
journalctl -u meshtastic-webchat -n 100 --no-pager
```

### Check installed Meshtastic Python package

```bash
cd /home/meshtastic/meshtastic_webchat
source .venv/bin/activate
python -m pip show meshtastic
```

---

## Development notes

### Suggested update workflow

Before updating a production deployment:

1. back up the current directory
2. update files
3. install or refresh dependencies
4. restart the service
5. check logs
6. test messages, nodes, address book, and channels

Example backup:

```bash
cd /home/meshtastic
cp -a meshtastic_webchat meshtastic_webchat.bak.$(date +%Y%m%d_%H%M%S)
```

### Local data files

Runtime data may include:

- `webchat_cache.db`
- `address_book.json`

These are not build artifacts for release packaging and should normally stay out of Git commits unless you intentionally want to ship sample data.

---

## Screenshots

Reference screenshots from earlier UI revisions are stored under `docs/`.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## Security notes

- intended primarily for trusted or internal environments
- ad-hoc HTTPS is convenient, but not a replacement for proper production TLS
- if you expose the UI on a network, use firewall rules and proper certificate handling

---

## License

See [LICENSE](LICENSE).
