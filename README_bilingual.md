# Meshtastic Web Chat / Web Chat Meshtastic

## Quick Start / Avvio rapido

### English

```bash
sudo apt update
sudo apt install -y python3-full python3-venv

mkdir -p ~/meshtastic-chat/webchat
cd ~/meshtastic-chat
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub
# Opzionale per HTTPS/self-signed adhoc SSL
python -m pip install pyopenssl
# Optional for HTTPS/self-signed adhoc SSL
python -m pip install pyopenssl
```

Copy `app.py` and `templates/` into `~/meshtastic-chat/webchat`, then start:

**By IP/TCP**
```bash
cd ~/meshtastic-chat/webchat
source ../.venv/bin/activate
python app.py --host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088
```

**By serial**
```bash
cd ~/meshtastic-chat/webchat
source ../.venv/bin/activate
python app.py --port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088
```

Open:

```text
http://127.0.0.1:8088
```

### Italiano

```bash
sudo apt update
sudo apt install -y python3-full python3-venv

mkdir -p ~/meshtastic-chat/webchat
cd ~/meshtastic-chat
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub
```

Copia `app.py` e `templates/` dentro `~/meshtastic-chat/webchat`, poi avvia:

**Via IP/TCP**
```bash
cd ~/meshtastic-chat/webchat
source ../.venv/bin/activate
python app.py --host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088
```

**Via seriale**
```bash
cd ~/meshtastic-chat/webchat
source ../.venv/bin/activate
python app.py --port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088
```

Apri:

```text
http://127.0.0.1:8088
```

---

## English

### Overview
Meshtastic Web Chat is a lightweight local web interface for a Meshtastic node.
It connects to **one** node either:
- via **serial** (`/dev/ttyUSB0`, `/dev/ttyACM0`, etc.)
- via **TCP/IP** (`192.168.x.x`)

It provides:
- a browser-based chat UI
- message history stored locally in SQLite
- a JSONL log file for raw archival
- known nodes list
- backend/node health status
- sidebar statistics (messages, packets, relays, channel utilization, etc.)

The web app is designed to be simple and local-first. The backend is the component that talks to the Meshtastic node; the browser only talks to the backend.

---

### Project files
Main files:
- `app.py` — Flask backend + Meshtastic connection
- `templates/index.html` — browser UI
- `messages.db` — SQLite database created automatically
- `messages.jsonl` — append-only JSON log created automatically

---

### Requirements
Recommended:
- Linux
- Python 3.10+ (tested in Python 3.12 environments)
- one Meshtastic node reachable by serial or IP

Python packages used:
- `flask`
- `meshtastic`
- `pypubsub`
- optional: `pyopenssl` for HTTPS adhoc mode

---

### Installation

#### 1. Create a working directory
Example:

```bash
mkdir -p ~/meshtastic-chat/webchat
cd ~/meshtastic-chat/webchat
```

Copy into this directory:
- `app.py`
- the `templates/` folder

#### 2. Create a virtual environment
Recommended on Debian/Ubuntu/Raspberry Pi OS because system Python is often externally managed.

```bash
sudo apt update
sudo apt install -y python3-full python3-venv

cd ~/meshtastic-chat
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub
```

Optional for HTTPS with Flask adhoc SSL:

```bash
python -m pip install pyopenssl
```

---

### Running the web chat

#### A. Connect to a node over serial
Example using `/dev/ttyUSB0`:

```bash
cd ~/meshtastic-chat/webchat
source ../.venv/bin/activate
python app.py --port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088
```

#### B. Connect to a node over IP/TCP
Example using node `192.168.0.18`:

```bash
cd ~/meshtastic-chat/webchat
source ../.venv/bin/activate
python app.py --host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088
```

#### C. Select a different Meshtastic channel index
Default is channel `0`.

```bash
python app.py --host 192.168.0.18 --channel 1 --listen-host 0.0.0.0 --listen-port 8088
```

When the app starts, you should see output similar to:

```text
Web chat pronta su http://0.0.0.0:8088
Backend Meshtastic: tcp -> 192.168.0.18
```

Then open the browser at:

```text
http://127.0.0.1:8088
```

Or from another device on the same LAN:

```text
http://IP_OF_THE_PC:8088
```

---

### Optional HTTPS (simple mode)
The simplest HTTPS option is already built into this package.
It uses Flask adhoc SSL with a self-signed certificate. Browsers will show a certificate warning, but traffic is encrypted.

1. Install PyOpenSSL:

```bash
source ~/meshtastic-chat/.venv/bin/activate
python -m pip install pyopenssl
```

2. Start the app with `--ssl-adhoc`:

```bash
python app.py --host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
```

or on serial:

```bash
python app.py --port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
```

3. Open:

```text
https://IP_OF_THE_PC:8088
```

---

### How to use the web interface

#### Top bar
- **Meshtastic Web Chat**: application title
- **status line**: shows backend mode (`serial` or `tcp`), target, and channel index
- **Refresh**: reloads status, nodes, messages, and statistics
- **Clear history**: deletes the stored message history from SQLite

#### Main chat area
- incoming messages are shown in dark blocks
- outgoing messages are shown in blue blocks
- each message shows:
  - timestamp
  - sender
  - destination
  - text

#### Message input fields
- **message field**: write the text to send
- **destination field**: optional Meshtastic destination ID (for example `!9ee783b4`)
  - leave it empty to send a broadcast on the selected channel
- **Send**: sends the message through the backend

#### Right sidebar — Statistics
Depending on available data, the sidebar shows:
- backend connected to node
- backend probe latency
- last packet time
- received/sent text messages
- total received packets
- relayed packets seen
- multi-hop packets seen
- bad/corrupted text packets seen
- send failures
- known nodes / online nodes
- channel utilization
- TX air utilization

Notes:
- **Backend connected to node** is **not** an RF ping. It only means the backend can still talk to the connected node.
- **Ping backend** is backend-to-node application latency, not ICMP ping and not radio ping.

#### Right sidebar — Nodes
The known nodes list is loaded from the Meshtastic backend cache.
Clicking a node automatically fills the destination field so you can send a direct message more easily.

---

### Data persistence
The backend stores data in two files:

#### `messages.db`
SQLite database containing chat history.
Messages are stored with:
- timestamp
- direction (`in` / `out`)
- source ID
- destination ID
- text
- optional raw JSON packet

#### `messages.jsonl`
Append-only JSONL file for archival/debugging.
Each line is a JSON object containing the saved message plus optional raw packet data.

Important behavior:
- if the **browser is closed** but `app.py` is still running, messages are still received and stored
- if the **backend is stopped**, messages arriving while it is offline cannot be stored by this app

---

### API endpoints
The web app exposes a small local API.

#### `GET /api/status`
Returns backend mode, target and channel.

#### `GET /api/messages?limit=200`
Returns the latest stored messages.

#### `GET /api/nodes`
Returns known nodes.

#### `GET /api/stats`
Returns statistics and health information.

#### `POST /api/send`
Sends a message.
Example payload:

```json
{
  "text": "Hello",
  "dest": "!9ee783b4"
}
```

#### `POST /api/clear`
Clears the stored history from the SQLite database.

---

### Troubleshooting

#### 1. `ModuleNotFoundError: No module named 'flask'`
The virtual environment is missing packages.

```bash
source ~/meshtastic-chat/.venv/bin/activate
python -m pip install flask meshtastic pypubsub
```

#### 2. `externally-managed-environment`
Do not install packages into system Python. Use a venv.

```bash
python3 -m venv ~/meshtastic-chat/.venv
source ~/meshtastic-chat/.venv/bin/activate
python -m pip install flask meshtastic pypubsub
```

#### 3. Serial port busy / locked
Another process is already using the node.
Check:

```bash
lsof /dev/ttyUSB0
fuser -v /dev/ttyUSB0
```

Stop the conflicting process, then restart the web chat.

#### 4. Browser works but node shows as offline
This usually means the backend health probe cannot read the expected Meshtastic information even though the process is still running.
Check backend logs and verify that the node is reachable and stable.

#### 5. Messages do not appear in the UI
Check:
- the backend is running
- the backend is connected to the correct node
- the node/channel is correct
- the browser console does not show API errors

#### 6. Instability when multiple clients connect directly to the same node
For best stability, let **one backend** talk to the node and let browsers/other users connect to the backend, not directly to the node.

---

### Running as a systemd service
Example unit file:

```ini
[Unit]
Description=Meshtastic Web Chat
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=meshtastic
Group=meshtastic
WorkingDirectory=/home/meshtastic/meshtastic-chat/webchat
Environment="PATH=/home/meshtastic/meshtastic-chat/.venv/bin"
ExecStart=/home/meshtastic/meshtastic-chat/.venv/bin/python /home/meshtastic/meshtastic-chat/webchat/app.py --host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable it with:

```bash
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
sudo systemctl status meshtastic-webchat
```

Follow logs:

```bash
journalctl -u meshtastic-webchat -f
```

---

### Typical workflows

#### Use case 1: CASA over IP
```bash
cd ~/meshtastic-chat/webchat
source ../.venv/bin/activate
python app.py --host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088
```
Then open the browser and use the chat.

#### Use case 2: UFFICIO over serial
```bash
cd ~/meshtastic-chat/webchat
source ../.venv/bin/activate
python app.py --port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088
```

#### Use case 3: Headless backend + browser from phone/PC
Run the backend on a Raspberry Pi or Linux box and open the web UI from any device in your LAN.

---

### Limitations
- one backend instance should control one node connection
- this app is a practical local chat frontend, not a full replacement for every Meshtastic client feature
- backend/node health is not an RF link test
- history is only stored while the backend is running

---

## Italiano

### Panoramica
Meshtastic Web Chat è un’interfaccia web locale leggera per un nodo Meshtastic.
Si collega a **un solo** nodo in uno di questi modi:
- via **seriale** (`/dev/ttyUSB0`, `/dev/ttyACM0`, ecc.)
- via **TCP/IP** (`192.168.x.x`)

Fornisce:
- chat da browser
- storico messaggi salvato in SQLite
- file JSONL per archivio/debug
- elenco nodi conosciuti
- stato backend/nodo
- statistiche nella sidebar (messaggi, pacchetti, relay, utilizzo canale, ecc.)

La web app è pensata per essere semplice e locale: il backend parla con il nodo Meshtastic, il browser parla solo con il backend.

---

### File del progetto
File principali:
- `app.py` — backend Flask + connessione Meshtastic
- `templates/index.html` — interfaccia browser
- `messages.db` — database SQLite creato automaticamente
- `messages.jsonl` — log JSON creato automaticamente

---

### Requisiti
Consigliati:
- Linux
- Python 3.10+ (testato in ambienti Python 3.12)
- un nodo Meshtastic raggiungibile via seriale o IP

Pacchetti Python usati:
- `flask`
- `meshtastic`
- `pypubsub`
- opzionale: `pyopenssl` per HTTPS adhoc

---

### Installazione

#### 1. Crea la cartella di lavoro
Esempio:

```bash
mkdir -p ~/meshtastic-chat/webchat
cd ~/meshtastic-chat/webchat
```

Copia in questa cartella:
- `app.py`
- la cartella `templates/`

#### 2. Crea un virtual environment
Consigliato su Debian/Ubuntu/Raspberry Pi OS perché il Python di sistema è spesso gestito dalla distribuzione.

```bash
sudo apt update
sudo apt install -y python3-full python3-venv

cd ~/meshtastic-chat
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub
```

Opzionale per HTTPS con SSL adhoc di Flask:

```bash
python -m pip install pyopenssl
```

---

### Avvio della web chat

#### A. Collegamento a un nodo via seriale
Esempio con `/dev/ttyUSB0`:

```bash
cd ~/meshtastic-chat/webchat
source ../.venv/bin/activate
python app.py --port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088
```

#### B. Collegamento a un nodo via IP/TCP
Esempio con il nodo `192.168.0.18`:

```bash
cd ~/meshtastic-chat/webchat
source ../.venv/bin/activate
python app.py --host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088
```

#### C. Selezione di un canale Meshtastic diverso
Il canale predefinito è `0`.

```bash
python app.py --host 192.168.0.18 --channel 1 --listen-host 0.0.0.0 --listen-port 8088
```

Quando l’app parte, dovresti vedere una cosa simile:

```text
Web chat pronta su http://0.0.0.0:8088
Backend Meshtastic: tcp -> 192.168.0.18
```

Poi apri il browser su:

```text
http://127.0.0.1:8088
```

Oppure da un altro dispositivo nella stessa LAN:

```text
http://IP_DEL_PC:8088
```

---

### HTTPS opzionale (modalità semplice)
La modalità più semplice è usare l’HTTPS adhoc di Flask.
È utile per test locali, ma il browser mostrerà un avviso sul certificato.

1. Installa PyOpenSSL:

```bash
source ~/meshtastic-chat/.venv/bin/activate
python -m pip install pyopenssl
```

2. In `app.py`, cambia l’ultima riga da:

```python
app.run(host=args.listen_host, port=args.listen_port, debug=False, threaded=True)
```

a:

```python
app.run(host=args.listen_host, port=args.listen_port, debug=False, threaded=True, ssl_context="adhoc")
```

3. Avvia l’app e apri:

```text
https://IP_DEL_PC:8088
```

---

### Guida all’uso dell’interfaccia web

#### Barra superiore
- **Meshtastic Web Chat**: titolo applicazione
- **riga di stato**: mostra modalità backend (`serial` o `tcp`), target e indice canale
- **Aggiorna**: ricarica stato, nodi, messaggi e statistiche
- **Pulisci storico**: elimina lo storico dal database SQLite

#### Area chat principale
- i messaggi in ingresso sono mostrati in blocchi scuri
- i messaggi in uscita sono mostrati in blu
- ogni messaggio mostra:
  - timestamp
  - mittente
  - destinatario
  - testo

#### Campi di invio
- **campo messaggio**: testo da inviare
- **campo destinatario**: ID Meshtastic opzionale (esempio `!9ee783b4`)
  - se lasciato vuoto, il messaggio viene inviato in broadcast sul canale selezionato
- **Invia**: spedisce il messaggio tramite il backend

#### Sidebar destra — Statistiche
In base ai dati disponibili, la sidebar mostra:
- backend connesso al nodo
- latenza del controllo backend
- ora dell’ultimo pacchetto
- messaggi di testo ricevuti/inviati
- pacchetti ricevuti totali
- pacchetti ruotati visti
- pacchetti multi-hop visti
- pacchetti testo corrotti/visti male
- errori invio
- nodi conosciuti / nodi online
- utilizzo canale
- utilizzo TX air

Note:
- **Backend connesso al nodo** **non** è un ping RF. Significa solo che il backend riesce ancora a parlare con il nodo collegato.
- **Ping backend** è la latenza applicativa backend→nodo, non è un ping ICMP e non è un ping radio.

#### Sidebar destra — Nodi
L’elenco nodi viene letto dalla cache del backend Meshtastic.
Cliccando su un nodo, il suo ID viene copiato automaticamente nel campo destinatario per facilitare l’invio di un messaggio diretto.

---

### Persistenza dati
Il backend salva i dati in due file:

#### `messages.db`
Database SQLite contenente lo storico della chat.
I messaggi vengono salvati con:
- timestamp
- direzione (`in` / `out`)
- ID sorgente
- ID destinazione
- testo
- JSON raw opzionale del pacchetto

#### `messages.jsonl`
File JSONL append-only per archivio/debug.
Ogni riga contiene il messaggio salvato più eventuali dati raw del pacchetto.

Comportamento importante:
- se il **browser è chiuso** ma `app.py` è ancora in esecuzione, i messaggi continuano a essere ricevuti e salvati
- se il **backend è spento**, i messaggi arrivati mentre il backend è offline non possono essere salvati da questa app

---

### Endpoint API
La web app espone una piccola API locale.

#### `GET /api/status`
Restituisce modalità backend, target e canale.

#### `GET /api/messages?limit=200`
Restituisce gli ultimi messaggi salvati.

#### `GET /api/nodes`
Restituisce i nodi conosciuti.

#### `GET /api/stats`
Restituisce statistiche e informazioni di stato.

#### `POST /api/send`
Invia un messaggio.
Payload di esempio:

```json
{
  "text": "Ciao",
  "dest": "!9ee783b4"
}
```

#### `POST /api/clear`
Cancella lo storico salvato nel database SQLite.

---

### Risoluzione problemi

#### 1. `ModuleNotFoundError: No module named 'flask'`
Nel virtual environment mancano i pacchetti.

```bash
source ~/meshtastic-chat/.venv/bin/activate
python -m pip install flask meshtastic pypubsub
```

#### 2. `externally-managed-environment`
Non installare i pacchetti nel Python di sistema. Usa un venv.

```bash
python3 -m venv ~/meshtastic-chat/.venv
source ~/meshtastic-chat/.venv/bin/activate
python -m pip install flask meshtastic pypubsub
```

#### 3. Porta seriale occupata / bloccata
Un altro processo sta già usando il nodo.
Controlla con:

```bash
lsof /dev/ttyUSB0
fuser -v /dev/ttyUSB0
```

Ferma il processo in conflitto, poi riavvia la web chat.

#### 4. Il browser funziona ma il nodo risulta offline
Di solito significa che la probe del backend non riesce a leggere le informazioni Meshtastic attese, anche se il processo è ancora attivo.
Controlla i log del backend e verifica che il nodo sia raggiungibile e stabile.

#### 5. I messaggi non compaiono nell’interfaccia
Controlla che:
- il backend sia in esecuzione
- il backend sia collegato al nodo corretto
- il nodo/canale sia corretto
- la console del browser non mostri errori API

#### 6. Instabilità quando più client si collegano direttamente allo stesso nodo
Per la massima stabilità, fai parlare **un solo backend** con il nodo e fai collegare browser/altri utenti al backend, non direttamente al nodo.

---

### Esecuzione come servizio systemd
Esempio di unit file:

```ini
[Unit]
Description=Meshtastic Web Chat
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=meshtastic
Group=meshtastic
WorkingDirectory=/home/meshtastic/meshtastic-chat/webchat
Environment="PATH=/home/meshtastic/meshtastic-chat/.venv/bin"
ExecStart=/home/meshtastic/meshtastic-chat/.venv/bin/python /home/meshtastic/meshtastic-chat/webchat/app.py --host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088 --ssl-adhoc
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Abilitalo con:

```bash
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
sudo systemctl status meshtastic-webchat
```

Per seguire i log:

```bash
journalctl -u meshtastic-webchat -f
```

---

### Flussi tipici

#### Caso d’uso 1: CASA via IP
```bash
cd ~/meshtastic-chat/webchat
source ../.venv/bin/activate
python app.py --host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088
```
Poi apri il browser e usa la chat.

#### Caso d’uso 2: UFFICIO via seriale
```bash
cd ~/meshtastic-chat/webchat
source ../.venv/bin/activate
python app.py --port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088
```

#### Caso d’uso 3: backend headless + browser da telefono/PC
Fai girare il backend su Raspberry Pi o Linux box e apri la UI web da qualunque dispositivo della LAN.

---

### Limiti
- una sola istanza backend dovrebbe controllare una sola connessione nodo
- questa app è un frontend pratico locale, non sostituisce tutte le funzioni di ogni client Meshtastic
- lo stato backend/nodo non è un test RF
- lo storico viene salvato solo mentre il backend è in esecuzione
