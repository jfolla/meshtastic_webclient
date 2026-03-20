# Meshtastic Web Chat

**Version / Versione:** `0.4.0`  
**Internal folder path / Cartella interna:** `meshtastic_webchat`  
**Service name / Nome servizio:** `meshtastic-webchat.service`

A coherent single-service Meshtastic web chat package with:

- a single application path: `meshtastic_webchat`
- a single systemd service: `meshtastic-webchat.service`
- configuration stored in `app_config.json`
- direct connection to one Meshtastic node (serial or TCP)
- HTTPS adhoc support
- configuration export/import from the UI
- Italian, English, and French interface

---

## ENGLISH

### What this package does

This package provides a lightweight web interface for one Meshtastic node.

It can connect in one of two ways:

- **serial mode** using a device such as `/dev/ttyUSB0`
- **tcp mode** using a node IP/hostname such as `192.168.0.18`

The application stores messages locally in SQLite and exposes a simple web UI for:

- reading saved chat messages
- sending broadcast or direct messages
- checking current backend status
- seeing known nodes
- exporting and importing application configuration

### Important note about TCP nodes

For some nodes, especially Wi-Fi/TCP-only devices, the Meshtastic TCP session can be less stable than serial.

This package is designed to stay coherent and easy to operate, but it still uses a **single direct connection** to the node. Do not connect many direct clients to the same node at the same time.

### Files included

```text
meshtastic_webchat/
├── app.py
├── app_config.json
├── meshtastic-webchat.service
├── README.md
├── README.txt
├── docs/
│   └── screenshot-ui-v0.3.5.jpg
└── templates/
    └── index.html
```

### Installation

Create the target directory and extract the package there.

```bash
sudo mkdir -p /home/meshtastic
cd /home/meshtastic
unzip meshtastic_webchat_v0.4.0_single_service_appconfig_coherent.zip
```

Create the virtual environment:

```bash
cd /home/meshtastic/meshtastic_webchat
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub
python -m pip install pyopenssl
```

### Configure `app_config.json`

Default **serial** example:

```json
{
  "version": "0.4.0",
  "node": {
    "mode": "serial",
    "host": "",
    "port": "/dev/ttyUSB0",
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

TCP example:

```json
{
  "version": "0.4.0",
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
    "default_language": "en"
  }
}
```

### Start manually

```bash
cd /home/meshtastic/meshtastic_webchat
source .venv/bin/activate
python app.py --config /home/meshtastic/meshtastic_webchat/app_config.json
```

### systemd

Install the service file:

```bash
sudo cp /home/meshtastic/meshtastic_webchat/meshtastic-webchat.service /etc/systemd/system/meshtastic-webchat.service
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
```

Check status:

```bash
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -f
```

### Web UI features

- status line with current mode/target/channel
- message history
- direct destination field
- language selector (IT/EN/FR)
- export config
- import config
- clear history
- known nodes list
- right sidebar statistics

### API endpoints

- `/api/status`
- `/api/messages`
- `/api/nodes`
- `/api/send`
- `/api/clear`
- `/api/debug`
- `/api/config`

### Troubleshooting

If you use **serial**, ensure the `meshtastic` user can access the device:

```bash
sudo usermod -a -G dialout meshtastic
```

Then re-login or reboot.

If you change code and want a clean restart:

```bash
sudo systemctl stop meshtastic-webchat
find /home/meshtastic/meshtastic_webchat -type d -name '__pycache__' -exec rm -rf {} +
sudo systemctl start meshtastic-webchat
```

---

## ITALIANO

### Cosa fa questo pacchetto

Questo pacchetto fornisce una web chat leggera per un singolo nodo Meshtastic.

Può collegarsi in due modi:

- **seriale** usando una porta tipo `/dev/ttyUSB0`
- **tcp** usando IP/hostname del nodo, per esempio `192.168.0.18`

L'applicazione salva i messaggi localmente in SQLite ed espone una UI web per:

- leggere i messaggi di chat salvati
- inviare messaggi broadcast o diretti
- controllare lo stato del backend
- vedere i nodi noti
- esportare e importare la configurazione dell'applicazione

### Nota importante sui nodi TCP

Per alcuni nodi, soprattutto quelli solo Wi-Fi/TCP, la sessione Meshtastic su TCP può essere meno stabile rispetto alla seriale.

Questo pacchetto è pensato per essere coerente e facile da gestire, ma usa comunque **una sola connessione diretta** al nodo. Evita di collegare più client diretti allo stesso nodo contemporaneamente.

### Installazione

Crea la cartella di destinazione ed estrai il pacchetto:

```bash
sudo mkdir -p /home/meshtastic
cd /home/meshtastic
unzip meshtastic_webchat_v0.4.0_single_service_appconfig_coherent.zip
```

Crea il virtual environment:

```bash
cd /home/meshtastic/meshtastic_webchat
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub
python -m pip install pyopenssl
```

### Configurazione tramite `app_config.json`

Esempio seriale predefinito:

```json
{
  "version": "0.4.0",
  "node": {
    "mode": "serial",
    "host": "",
    "port": "/dev/ttyUSB0",
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

Per usare il nodo su TCP, imposta:

```json
{
  "version": "0.4.0",
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

### Avvio manuale

```bash
cd /home/meshtastic/meshtastic_webchat
source .venv/bin/activate
python app.py --config /home/meshtastic/meshtastic_webchat/app_config.json
```

### systemd

Installa il file di servizio:

```bash
sudo cp /home/meshtastic/meshtastic_webchat/meshtastic-webchat.service /etc/systemd/system/meshtastic-webchat.service
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
```

Log e stato:

```bash
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -f
```

### Funzioni della UI

- stato backend con modo/target/canale
- storico messaggi
- campo destinazione per messaggi diretti
- selettore lingua (IT/EN/FR)
- esporta configurazione
- importa configurazione
- pulizia storico
- elenco nodi noti
- sidebar destra con statistiche

### Endpoint disponibili

- `/api/status`
- `/api/messages`
- `/api/nodes`
- `/api/send`
- `/api/clear`
- `/api/debug`
- `/api/config`

### Risoluzione problemi

Se usi la **seriale**, assicurati che l'utente `meshtastic` abbia accesso alla porta:

```bash
sudo usermod -a -G dialout meshtastic
```

Poi fai logout/login oppure riavvia.

Per un riavvio pulito dopo modifiche al codice:

```bash
sudo systemctl stop meshtastic-webchat
find /home/meshtastic/meshtastic_webchat -type d -name '__pycache__' -exec rm -rf {} +
sudo systemctl start meshtastic-webchat
```
