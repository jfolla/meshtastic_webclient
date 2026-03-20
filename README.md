# Meshtastic Web Chat

**Package version:** v0.3.5  
**Internal folder path:** `meshtastic_webchat`  
**Service name:** `meshtastic-webchat.service`

---

## ITA — Panoramica

Meshtastic Web Chat è una web UI locale per leggere e inviare messaggi Meshtastic dal browser.

Questa applicazione gira come **unico servizio** (`meshtastic-webchat.service`) dentro la cartella `meshtastic_webchat` e fa da **gateway web** tra il nodo Meshtastic e uno o più browser.

### A cosa serve la funzione gateway/proxy integrata

L'applicazione mantiene **una sola connessione diretta** al nodo Meshtastic (via TCP oppure seriale) e poi espone una UI web locale.

Questo aiuta a:

- ridurre i problemi quando più client si collegano direttamente allo stesso nodo;
- centralizzare storico, statistiche e debug;
- usare più browser senza aprire più connessioni dirette al nodo;
- importare/esportare facilmente la configurazione dell'app.

Questa app **non è un virtual node completo compatibile con l'app mobile ufficiale**. È una web UI/gateway locale pensata per stabilità e gestione semplificata.

---

## ENG — Overview

Meshtastic Web Chat is a local web UI to read and send Meshtastic messages from a browser.

This application runs as a **single service** (`meshtastic-webchat.service`) inside the `meshtastic_webchat` folder and acts as a **web gateway** between the Meshtastic node and one or more browsers.

### What the integrated gateway/proxy role is for

The application keeps **one direct connection** to the Meshtastic node (via TCP or serial) and then exposes a local web UI.

This helps to:

- reduce issues when multiple clients connect directly to the same node;
- centralize history, statistics, and debug information;
- allow multiple browsers without opening multiple direct node connections;
- easily import/export application configuration.

This app is **not a full virtual node compatible with the official mobile app**. It is a local web UI/gateway focused on stability and simpler operations.

---

## Features / Funzionalità

- Browser UI for chat / Interfaccia browser per chat
- Integrated gateway / Gateway integrato
- TCP node support / Supporto nodo TCP
- Serial node support / Supporto nodo seriale
- Message history in SQLite / Storico messaggi in SQLite
- JSONL raw message log / Log messaggi JSONL
- Right sidebar statistics / Statistiche nella sidebar destra
- Debug endpoint `/api/debug`
- Config export/import endpoints / Esportazione e importazione configurazione
- UI languages: Italian, English, French / Lingue UI: italiano, inglese, francese
- Optional ad-hoc HTTPS / HTTPS adhoc opzionale

---

## Requirements / Requisiti

- Python 3.11+ recommended
- A Meshtastic node reachable via:
  - TCP/IP (`host`)
  - or serial (`port`)
- Linux recommended for systemd deployment

---

## Installation / Installazione

### 1. Create a virtual environment / Crea un ambiente virtuale

```bash
cd /home/meshtastic
mkdir -p meshtastic_webchat
cd meshtastic_webchat
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub pyopenssl
```

If you do not want HTTPS ad-hoc, `pyopenssl` is optional.

---

### 2. Copy files / Copia i file

Copy into `/home/meshtastic/meshtastic_webchat`:

- `app.py`
- `app_config.json`
- `meshtastic-webchat.service`
- `templates/index.html`
- `README.md`
- `README.txt`

---

## Configuration / Configurazione

The main configuration file is:

```text
/home/meshtastic/meshtastic_webchat/app_config.json
```

Example:

```json
{
  "version": "0.3.5",
  "node": {
    "mode": "tcp",
    "host": "192.168.0.18",
    "port": "",
    "channel": 0
  },
  "web": {
    "listen_host": "0.0.0.0",
    "listen_port": 8088,
    "ssl_adhoc": true
  },
  "ui": {
    "default_language": "it"
  }
}
```

### Notes / Note

- `node.mode` can be `tcp` or `serial`
- for `tcp`, use `host`
- for `serial`, use `port`
- `ssl_adhoc: true` enables self-signed HTTPS

---

## Manual startup / Avvio manuale

### TCP node

```bash
cd /home/meshtastic/meshtastic_webchat
source .venv/bin/activate
python app.py --config /home/meshtastic/meshtastic_webchat/app_config.json
```

### Serial node

Edit `app_config.json`:

```json
"node": {
  "mode": "serial",
  "host": "",
  "port": "/dev/ttyUSB0",
  "channel": 0
}
```

Then start the same way.

### One-shot CLI override examples

```bash
python app.py --node-host 192.168.0.18
python app.py --node-port /dev/ttyUSB0
python app.py --node-host 192.168.0.18 --ssl-adhoc
```

These override the config file and save the updated values back to `app_config.json`.

---

## systemd setup

Install the service file:

```bash
sudo cp meshtastic-webchat.service /etc/systemd/system/meshtastic-webchat.service
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
```

Check status:

```bash
sudo systemctl status meshtastic-webchat
```

View logs:

```bash
journalctl -u meshtastic-webchat -f
```

If using serial, ensure the `meshtastic` user can access the serial port:

```bash
sudo usermod -a -G dialout meshtastic
```

---

## Web UI usage / Uso interfaccia web

Open the UI:

### HTTP

```text
http://IP_DEL_SERVER:8088
```

### HTTPS ad-hoc

```text
https://IP_DEL_SERVER:8088
```

The browser will warn about a self-signed certificate. This is expected.

### Main actions / Azioni principali

- send broadcast messages / invio broadcast
- send direct messages by destination ID / invio diretto con ID destinazione
- see known nodes / vedere nodi noti
- see message history / vedere storico messaggi
- clear stored history / pulire storico locale
- switch UI language IT/EN/FR / cambiare lingua UI IT/EN/FR
- export app config / esportare configurazione app
- import app config / importare configurazione app

### Import/export configuration

Buttons in the top bar:

- **Export config** downloads the current effective app configuration as JSON
- **Import config** uploads a JSON config file

After import:

- if node mode/target/channel changes, the backend requests a reconnect;
- if listen host/port or SSL changes, a restart is required.

---

## API endpoints

### `GET /api/status`
Basic status and effective configuration.

### `GET /api/messages`
Returns stored messages.

### `GET /api/nodes`
Returns cached node list.

### `GET /api/stats`
Returns UI statistics and health data.

### `GET /api/debug`
Returns extended debug information:

- version
- config snapshot
- runtime counters
- cached health
- cached local stats
- recent messages

### `POST /api/send`
Send a message.

Request body example:

```json
{
  "text": "hello",
  "dest": "!9ee783b4"
}
```

Leave `dest` empty for broadcast.

### `POST /api/clear`
Clear local message history.

### `GET /api/config/export`
Download current config JSON.

### `POST /api/config/import`
Import a config JSON file.

---

## Files created at runtime

Inside `meshtastic_webchat`:

- `messages.db` → SQLite history
- `messages.jsonl` → raw JSONL message log
- `app_config.json` → persistent application config

---

## Notes about stability / Note sulla stabilità

### ITA

Se possibile, evita di avere troppi client che si collegano direttamente allo stesso nodo Meshtastic via TCP. Questa applicazione è pensata per essere il punto web centrale verso il nodo.

### ENG

If possible, avoid having too many clients connect directly to the same Meshtastic TCP node. This application is intended to be the central web-facing gateway for the node.

---

## Troubleshooting

### The UI opens but does not update

Check service logs:

```bash
journalctl -u meshtastic-webchat -f
```

Check debug endpoint:

```bash
curl -k https://127.0.0.1:8088/api/debug
```

Or without HTTPS:

```bash
curl http://127.0.0.1:8088/api/debug
```

### Serial mode cannot open the port

Check if another process is already using the serial device.

### The imported config does not fully apply

If web listen host/port or SSL changed, restart the service.

---

## Version history / Storico versione

### v0.3.5

- added `/api/debug`
- added configuration export/import
- added persistent `app_config.json`
- added French UI language
- cleaned up single-service deployment model
- updated README and installation instructions

