# Meshtastic Web Chat v0.3.2

## English

### What changed in v0.3.2
This release goes back to a **single-service / single-path** deployment model.

There is now:
- one application path: `meshtastic_webchat`
- one service: `meshtastic-webchat.service`
- one main process: `app.py`

The application keeps a **single Meshtastic connection owner** inside the webchat process itself and exposes the web UI from the same process.

It supports:
- **TCP node**: `--node-host 192.168.0.18`
- **Serial node**: `--node-port /dev/ttyUSB0`
- optional HTTPS with `--ssl-adhoc`
- English / Italian UI switch
- right sidebar statistics
- auto-reconnect loop for Meshtastic connection
- cached HTTP responses to reduce UI blocking

### Installation
Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub pyopenssl
```

### Manual start
#### TCP node
```bash
python app.py --node-host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
```

#### Serial node
```bash
python app.py --node-port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
```

Open the UI at:
- `https://IP_OF_THE_SERVER:8088`

### systemd
The included service file keeps the same service name:
- `meshtastic-webchat.service`

You can copy it to:
- `/etc/systemd/system/meshtastic-webchat.service`

Then reload and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -f
```

### Notes
- Only one direct client should talk to the node at a time.
- If you use TCP, avoid connecting the Android app directly to the same node while the webchat is running.
- If you use serial, ensure the service user is in `dialout`.

---

## Italiano

### Novità della v0.3.2
Questa release torna a un modello **single-service / single-path**.

Ora c’è:
- un solo path applicativo: `meshtastic_webchat`
- un solo servizio: `meshtastic-webchat.service`
- un solo processo principale: `app.py`

L’applicazione mantiene **un solo owner della connessione Meshtastic** dentro il processo webchat e serve la UI web dallo stesso processo.

Supporta:
- **nodo TCP**: `--node-host 192.168.0.18`
- **nodo seriale**: `--node-port /dev/ttyUSB0`
- HTTPS opzionale con `--ssl-adhoc`
- selettore lingua Italiano / English
- statistiche nella barra destra
- reconnect automatico della connessione Meshtastic
- risposte HTTP basate su cache per ridurre i blocchi della UI

### Installazione
Crea un ambiente virtuale e installa le dipendenze:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub pyopenssl
```

### Avvio manuale
#### Nodo TCP
```bash
python app.py --node-host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
```

#### Nodo seriale
```bash
python app.py --node-port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
```

Apri la UI su:
- `https://IP_DEL_SERVER:8088`

### systemd
Il file di servizio incluso mantiene lo stesso nome:
- `meshtastic-webchat.service`

Puoi copiarlo in:
- `/etc/systemd/system/meshtastic-webchat.service`

Poi ricarica e avvia:

```bash
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -f
```

### Note
- Solo un client diretto dovrebbe parlare con il nodo alla volta.
- Se usi TCP, evita di collegare anche l’app Android direttamente allo stesso nodo mentre gira la webchat.
- Se usi la seriale, assicurati che l’utente del servizio sia nel gruppo `dialout`.
