# Meshtastic Web Chat v0.3.3

**Internal folder path:** `meshtastic_webchat`  
**Service name:** `meshtastic-webchat.service`

---

## English

### What this project is
Meshtastic Web Chat is a lightweight local web interface for a single Meshtastic node.

It runs as **one service** and **one path**:
- folder: `meshtastic_webchat`
- service: `meshtastic-webchat.service`

You can connect it either:
- to a **serial node** with `--node-port /dev/ttyUSB0`
- to a **network node** with `--node-host 192.168.x.x`

### Integrated gateway / proxy function
This release keeps everything in **one process**, but the application still acts like an **integrated gateway**.

That means:
- the browser never talks directly to the Meshtastic node
- the backend owns the **single direct connection** to the node
- the browser only talks to Flask/HTTP locally

This helps because Meshtastic TCP/network nodes can become unstable when multiple clients connect directly at the same time. Keeping a single owner connection in the backend improves stability and makes the browser UI safer.

### What it helps with
The integrated gateway approach helps to:
- reduce direct multi-client pressure on TCP/network nodes
- keep the web UI responsive even if the node reconnects
- isolate browser requests from the Meshtastic connection
- keep local history and statistics available through cached data

### Supported UI languages
The interface supports:
- Italian
- English
- French

Language selection is available from the top-right menu and is saved in the browser with `localStorage`.

### Main features
- browser-based chat UI
- local SQLite message history
- JSONL append-only log
- known node list
- right sidebar with statistics
- integrated gateway behavior
- reconnect loop for Meshtastic backend
- optional HTTPS with Flask adhoc SSL

### Requirements
Recommended:
- Linux
- Python 3.10+
- one Meshtastic node reachable by serial or IP
- a virtual environment

Python packages:
- `flask`
- `meshtastic`
- `pypubsub`
- optional: `pyopenssl` for `--ssl-adhoc`

### Installation
```bash
sudo apt update
sudo apt install -y python3-full python3-venv

mkdir -p ~/meshtastic_webchat
cd ~/meshtastic_webchat

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub
```

Optional HTTPS:
```bash
python -m pip install pyopenssl
```

Copy into `~/meshtastic_webchat`:
- `app.py`
- `templates/index.html`
- `meshtastic-webchat.service`
- `README.md`

### Run manually

#### Serial node
```bash
cd ~/meshtastic_webchat
source .venv/bin/activate
python app.py --node-port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088
```

#### TCP/IP node
```bash
cd ~/meshtastic_webchat
source .venv/bin/activate
python app.py --node-host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088
```

#### HTTPS adhoc
```bash
python app.py --node-host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
```

### Systemd
The included service file is already set up for the same single-folder structure.

Edit:
`meshtastic-webchat.service`

For TCP/IP:
```ini
ExecStart=/home/meshtastic/meshtastic_webchat/.venv/bin/python /home/meshtastic/meshtastic_webchat/app.py --node-host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
```

For serial:
```ini
ExecStart=/home/meshtastic/meshtastic_webchat/.venv/bin/python /home/meshtastic/meshtastic_webchat/app.py --node-port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
```

Install service:
```bash
sudo cp meshtastic-webchat.service /etc/systemd/system/meshtastic-webchat.service
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
sudo systemctl status meshtastic-webchat
```

Logs:
```bash
journalctl -u meshtastic-webchat -f
```

### Web UI usage
- top-right language selector: IT / EN / FR
- message field: write text
- optional destination field: direct message to a node like `!9ee783b4`
- leave destination empty for broadcast
- right sidebar shows:
  - backend connected to node
  - backend ping
  - last packet
  - RX/TX message counts
  - packets seen
  - relays seen
  - multi-hop seen
  - bad packets
  - send failures
  - known nodes / online nodes
  - channel utilization
  - TX air utilization
  - battery / voltage if available

### Important note about TCP/network nodes
If you use a network node, try to avoid multiple direct clients connected to the node at the same time.  
Best practice:
- let **this webchat backend** own the connection
- do not connect other tools directly to the same node unless needed

### Data files
The backend creates:
- `messages.db`
- `messages.jsonl`

### Troubleshooting
If the service starts but the UI behaves strangely:
```bash
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -f
```

If using serial and permissions fail:
```bash
sudo usermod -a -G dialout meshtastic
```

Then re-login or reboot.

---

## Italiano

### Cos'è questo progetto
Meshtastic Web Chat è un'interfaccia web locale leggera per un singolo nodo Meshtastic.

Funziona con **un solo servizio** e **un solo path**:
- cartella: `meshtastic_webchat`
- servizio: `meshtastic-webchat.service`

Può collegarsi:
- a un **nodo seriale** con `--node-port /dev/ttyUSB0`
- a un **nodo di rete** con `--node-host 192.168.x.x`

### Funzione gateway / proxy integrata
Questa release tiene tutto in **un solo processo**, ma l'applicazione si comporta comunque come un **gateway integrato**.

Questo significa:
- il browser non parla mai direttamente al nodo Meshtastic
- il backend possiede **l'unica connessione diretta** verso il nodo
- il browser parla solo con Flask/HTTP in locale

Questo aiuta perché i nodi Meshtastic TCP/rete possono diventare instabili se più client si collegano direttamente nello stesso momento. Mantenere una sola connessione “proprietaria” nel backend migliora la stabilità e rende la UI più sicura.

### Cosa aiuta a fare
L'approccio gateway integrato aiuta a:
- ridurre la pressione di più client diretti sui nodi TCP/rete
- mantenere reattiva la UI anche quando il nodo si riconnette
- isolare le richieste del browser dalla connessione Meshtastic
- mantenere storico e statistiche locali tramite dati in cache

### Lingue supportate dalla UI
L'interfaccia supporta:
- Italiano
- Inglese
- Francese

La lingua si sceglie dal menu in alto a destra e viene salvata nel browser con `localStorage`.

### Funzioni principali
- chat da browser
- storico locale in SQLite
- log append-only in JSONL
- elenco nodi noti
- sidebar destra con statistiche
- comportamento da gateway integrato
- loop di riconnessione del backend Meshtastic
- HTTPS opzionale con SSL adhoc di Flask

### Requisiti
Consigliati:
- Linux
- Python 3.10+
- un nodo Meshtastic raggiungibile via seriale o IP
- ambiente virtuale `venv`

Pacchetti Python:
- `flask`
- `meshtastic`
- `pypubsub`
- opzionale: `pyopenssl` per `--ssl-adhoc`

### Installazione
```bash
sudo apt update
sudo apt install -y python3-full python3-venv

mkdir -p ~/meshtastic_webchat
cd ~/meshtastic_webchat

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub
```

HTTPS opzionale:
```bash
python -m pip install pyopenssl
```

Copia dentro `~/meshtastic_webchat`:
- `app.py`
- `templates/index.html`
- `meshtastic-webchat.service`
- `README.md`

### Avvio manuale

#### Nodo seriale
```bash
cd ~/meshtastic_webchat
source .venv/bin/activate
python app.py --node-port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088
```

#### Nodo TCP/IP
```bash
cd ~/meshtastic_webchat
source .venv/bin/activate
python app.py --node-host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088
```

#### HTTPS adhoc
```bash
python app.py --node-host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
```

### Systemd
Il file servizio incluso è già impostato per la stessa struttura a cartella singola.

Modifica:
`meshtastic-webchat.service`

Per TCP/IP:
```ini
ExecStart=/home/meshtastic/meshtastic_webchat/.venv/bin/python /home/meshtastic/meshtastic_webchat/app.py --node-host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
```

Per seriale:
```ini
ExecStart=/home/meshtastic/meshtastic_webchat/.venv/bin/python /home/meshtastic/meshtastic_webchat/app.py --node-port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
```

Installazione del servizio:
```bash
sudo cp meshtastic-webchat.service /etc/systemd/system/meshtastic-webchat.service
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
sudo systemctl status meshtastic-webchat
```

Log:
```bash
journalctl -u meshtastic-webchat -f
```

### Uso della UI web
- selettore lingua in alto a destra: IT / EN / FR
- campo messaggio: scrivi il testo
- campo destinatario opzionale: messaggio diretto a un nodo come `!9ee783b4`
- lasciando vuoto il destinatario fai broadcast
- la sidebar destra mostra:
  - backend connesso al nodo
  - ping backend
  - ultimo pacchetto
  - contatori messaggi RX/TX
  - pacchetti visti
  - relay visti
  - multi-hop visti
  - pacchetti corrotti
  - invii falliti
  - nodi noti / online
  - utilizzo canale
  - utilizzo TX
  - batteria / tensione se disponibili

### Nota importante sui nodi TCP/rete
Se usi un nodo di rete, cerca di evitare più client diretti collegati contemporaneamente allo stesso nodo.  
Buona pratica:
- lascia che **questa webchat** possieda la connessione
- evita di collegare altri strumenti direttamente allo stesso nodo se non necessario

### File dati
Il backend crea:
- `messages.db`
- `messages.jsonl`

### Troubleshooting
Se il servizio parte ma la UI si comporta in modo anomalo:
```bash
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -f
```

Se usi la seriale e hai problemi di permessi:
```bash
sudo usermod -a -G dialout meshtastic
```

Poi esegui di nuovo login o riavvia il sistema.
