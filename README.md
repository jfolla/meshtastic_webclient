# Meshtastic Web Chat v0.3.2

**Package version:** `v0.3.2`  
**Internal folder path:** `meshtastic_webchat`  
**Service name:** `meshtastic-webchat.service`

## English

### Overview
Meshtastic Web Chat is a lightweight browser UI for a Meshtastic node.

This release uses a **single-service / single-path** layout:
- one application path: `meshtastic_webchat`
- one service: `meshtastic-webchat.service`
- one main process: `app.py`

The application can connect to a node in two ways:
- **TCP node**: `--node-host 192.168.0.18`
- **Serial node**: `--node-port /dev/ttyUSB0`

It also supports:
- optional HTTPS with `--ssl-adhoc`
- English / Italian UI switch
- right sidebar statistics
- auto-reconnect loop for the Meshtastic connection
- cached HTTP responses to reduce UI blocking

---

### Why the built-in proxy/gateway function exists
Even though this package runs as a **single process**, it also acts as a **connection owner / gateway** between the browser and the real Meshtastic node.

In practice, the webchat behaves like a small local proxy:
- the browser talks only to the web application
- the web application keeps the Meshtastic connection
- the Meshtastic node sees only **one direct client**

This matters because some Meshtastic TCP/network nodes can become unstable when **multiple direct clients** connect at the same time.
Typical examples are:
- Android app connected directly to the same node
- CLI connected directly with `--host`
- browser/web backend connected directly too

When that happens, direct TCP sessions can interfere with each other.

#### What the proxy/gateway function helps with
The built-in gateway model helps to:
- reduce direct multi-client conflicts against the node
- keep a **single owner** of the Meshtastic connection
- expose a stable web UI to multiple browsers
- keep cached state even when the node connection drops temporarily
- reconnect automatically in background without killing the web UI
- avoid calling the node directly during every HTTP request

#### What it does **not** do
This is **not** yet a full standalone Meshtastic virtual-node proxy for the official mobile app.
It is a web application with an internal single-owner gateway role.

So:
- **yes**: multiple browsers can use the webchat
- **yes**: the webchat tries to shield the UI from TCP hiccups
- **no**: it is not a complete multi-client Meshtastic protocol proxy for Android/iOS app compatibility

---

### Recommended usage model
For best stability:
- let **Meshtastic Web Chat** be the only direct client connected to the node
- do **not** connect the Android app directly to the same TCP node at the same time
- do **not** use CLI `--host` to the same node while the webchat is running
- if using serial, make sure nothing else owns the serial port

Good model:

`Browser(s) -> Web Chat -> Meshtastic node`

Less stable model:

`Browser + Android app + CLI -> same Meshtastic TCP node`

---

### Installation
Create a virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub pyopenssl
```

If the service runs with a dedicated user, make sure that user owns the project directory.

---

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

---

### systemd
The included service file keeps the same service name:
- `meshtastic-webchat.service`

Copy it to:
- `/etc/systemd/system/meshtastic-webchat.service`

Then reload and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -f
```

If you use a serial node, the service user must be in `dialout`.

---

### Troubleshooting
#### The page opens but later freezes or times out
Possible causes:
- multiple direct clients connected to the same TCP node
- unstable Meshtastic TCP session
- old cached Python files after an update

Recommended checks:

```bash
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -f
find /home/meshtastic/meshtastic_webchat -type d -name '__pycache__' -exec rm -rf {} +
```

#### TCP node stays pingable but the UI becomes unresponsive
That usually means the IP link is still up, but the Meshtastic TCP/API session became unhealthy.
The built-in gateway/cache reduces the impact, but it cannot fully solve a bad upstream TCP session.

#### Serial works, TCP is unstable
That usually points to the network/TCP side of the Meshtastic node, not the browser UI itself.

---

## Italiano

**Versione pacchetto:** `v0.3.2`  
**Path interno cartella:** `meshtastic_webchat`  
**Nome servizio:** `meshtastic-webchat.service`

### Panoramica
Meshtastic Web Chat è una UI web leggera per un nodo Meshtastic.

Questa release usa un layout **single-service / single-path**:
- un solo path applicativo: `meshtastic_webchat`
- un solo servizio: `meshtastic-webchat.service`
- un solo processo principale: `app.py`

L’applicazione può collegarsi al nodo in due modi:
- **nodo TCP**: `--node-host 192.168.0.18`
- **nodo seriale**: `--node-port /dev/ttyUSB0`

Supporta anche:
- HTTPS opzionale con `--ssl-adhoc`
- selettore lingua Italiano / English
- statistiche nella barra destra
- reconnect automatico della connessione Meshtastic
- risposte HTTP basate su cache per ridurre i blocchi della UI

---

### Perché esiste la funzione proxy/gateway integrata
Anche se questo pacchetto gira come **processo singolo**, funziona anche come **owner della connessione / gateway** tra browser e nodo Meshtastic reale.

In pratica, la webchat si comporta come un piccolo proxy locale:
- il browser parla solo con l’applicazione web
- l’applicazione web mantiene la connessione Meshtastic
- il nodo Meshtastic vede un solo **client diretto**

Questo è importante perché alcuni nodi Meshtastic TCP/rete possono diventare instabili quando ci sono **più client diretti** contemporanei.
Esempi tipici:
- app Android collegata direttamente allo stesso nodo
- CLI collegata direttamente con `--host`
- backend web collegato direttamente allo stesso nodo

Quando succede, le sessioni TCP dirette possono interferire tra loro.

#### A cosa serve concretamente questa funzione proxy/gateway
Il modello gateway integrato aiuta a:
- ridurre i conflitti dovuti a più client diretti verso il nodo
- mantenere **un solo owner** della connessione Meshtastic
- esporre una UI web stabile a più browser
- mantenere uno stato cached anche se la connessione al nodo cade temporaneamente
- riconnettersi in background senza chiudere la UI web
- evitare chiamate dirette al nodo a ogni request HTTP

#### Cosa **non** fa
Questa applicazione **non** è ancora un proxy virtual-node completo del protocollo Meshtastic per l’app mobile ufficiale.
È una web application con una funzione interna di gateway single-owner.

Quindi:
- **sì**: più browser possono usare la webchat
- **sì**: la webchat prova a schermare la UI dai problemi TCP
- **no**: non è ancora un proxy completo del protocollo Meshtastic per compatibilità Android/iOS ufficiale

---

### Modello d’uso consigliato
Per la massima stabilità:
- lascia che **Meshtastic Web Chat** sia l’unico client diretto collegato al nodo
- **non** collegare contemporaneamente anche l’app Android allo stesso nodo TCP
- **non** usare la CLI `--host` verso lo stesso nodo mentre gira la webchat
- se usi la seriale, assicurati che nessun altro processo possieda la porta

Modello consigliato:

`Browser -> Web Chat -> nodo Meshtastic`

Modello meno stabile:

`Browser + app Android + CLI -> stesso nodo Meshtastic TCP`

---

### Installazione
Crea un ambiente virtuale e installa le dipendenze:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install flask meshtastic pypubsub pyopenssl
```

Se il servizio gira con un utente dedicato, assicurati che quell’utente sia proprietario della directory del progetto.

---

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

---

### systemd
Il file di servizio incluso mantiene lo stesso nome:
- `meshtastic-webchat.service`

Copialo in:
- `/etc/systemd/system/meshtastic-webchat.service`

Poi ricarica e avvia:

```bash
sudo systemctl daemon-reload
sudo systemctl enable meshtastic-webchat
sudo systemctl start meshtastic-webchat
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -f
```

Se usi un nodo seriale, l’utente del servizio deve essere nel gruppo `dialout`.

---

### Risoluzione problemi
#### La pagina si apre ma dopo un po’ si blocca o va in timeout
Cause possibili:
- più client diretti collegati allo stesso nodo TCP
- sessione TCP Meshtastic instabile
- file Python cache vecchi dopo un aggiornamento

Controlli consigliati:

```bash
sudo systemctl status meshtastic-webchat
journalctl -u meshtastic-webchat -f
find /home/meshtastic/meshtastic_webchat -type d -name '__pycache__' -exec rm -rf {} +
```

#### Il nodo TCP risponde al ping ma la UI non risponde più
Di solito significa che il link IP è ancora su, ma la sessione Meshtastic TCP/API è diventata instabile.
Il gateway/cache interno riduce l’impatto, ma non può eliminare del tutto un upstream TCP difettoso.

#### La seriale è stabile, il TCP no
Di solito questo indica un problema nel lato rete/TCP del nodo Meshtastic, non nella UI browser in sé.
