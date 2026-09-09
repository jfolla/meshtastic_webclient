# Meshtastic Web Chat — v0.7.5-beta

Interfaccia Flask con schede Chat, Channels, Address Book, Nodes, Config e Debug.
Un proxy locale mantiene la connessione seriale o TCP con il nodo Meshtastic.

## Modifiche della v0.7.5-beta

1. **SQLite in batch:** una connessione e una transazione per sincronizzazione,
   `INSERT OR IGNORE`, deduplicazione per ID proxy e migrazione delle righe legacy.
   Con 100 messaggi già presenti, il polling ogni 3 secondi apre circa 20 connessioni
   al minuto per sincronizzare, anziché fino a 2.000. Sono escluse da questo conteggio
   le letture richieste dalla UI. Una cache invariata non riscrive i messaggi.
   Le connessioni vengono chiuse esplicitamente.
2. **Apply/Rollback con read-back:** prima di Apply viene letto e salvato un backup
   completo dalla radio. Dopo la scrittura e un secondo di attesa, vengono richiesti
   tutti gli 8 slot canale e la configurazione LoRa tramite risposte admin correlate.
   Solo il confronto completo con il ChannelSet richiesto abilita `Verified`.
   Timeout o differenze producono `Applied / verification pending`.
   Il controllo periodico riprova la lettura; il target atteso è conservato anche
   dopo un riavvio. Il rollback conserva il backup fino a verifica riuscita.
   Gli slot secondari eccedenti vengono disabilitati: `setURL()` da solo li conserva.
   Se manca il backup iniziale riletto dalla radio, Apply si ferma prima di scrivere.
3. **Chiavi solo sul server:** le risposte JSON delle API vengono filtrate,
   comprese anteprima, importazione, Apply, Rollback, stato, backup e Debug.
   Il browser riceve nome, numero di canali e fingerprint; Apply usa il `room_id`.
   Il campo di importazione contiene naturalmente l'URL inserito dall'utente e viene
   svuotato dopo l'importazione. Le chiavi non vengono restituite dal server.
4. **Proxy solo loopback:** avvio, socket server, client Flask e importazione config
   rifiutano indirizzi non loopback. Sono ammessi `127.0.0.1`, `localhost` e `::1`.
   Il listener web resta configurabile separatamente per l'accesso dalla LAN.
5. **Disco e log:** entrambi i database mantengono gli ultimi 10.000 messaggi.
   `raw_json` non viene più scritto; i valori storici sono azzerati all'avvio.
   La riconnessione usa attese 2 → 5 → 10 → 20 → 30 secondi, con un solo avviso
   per sequenza di errori. Anche il polling Flask usa backoff quando il proxy manca.

## Aggiornamento da v0.7.4-beta

Il pacchetto contiene la cartella `meshtastic_webchat`. Non contiene database,
configurazione locale, rubrica, stanze o backup: questi file esistenti vengono
mantenuti. La retention elimina però automaticamente i messaggi oltre gli ultimi
10.000, in entrambi i database, al primo avvio.

Eseguire sul server, con lo ZIP scaricato nella directory corrente:

```bash
sudo systemctl stop meshtastic-webchat
sudo tar -czf /home/meshtastic/meshtastic_webchat_pre_v0.7.5_backup.tar.gz \
  --exclude=meshtastic_webchat/.venv \
  -C /home/meshtastic meshtastic_webchat
sudo chmod 600 /home/meshtastic/meshtastic_webchat_pre_v0.7.5_backup.tar.gz
sudo unzip -o meshtastic_webchat_v0.7.5_beta_optimized.zip -d /home/meshtastic
sudo chown -R meshtastic:meshtastic /home/meshtastic/meshtastic_webchat
sudo chmod +x /home/meshtastic/meshtastic_webchat/start_webchat.sh
```

Controllare che `proxy.host` in `app_config.json` sia `127.0.0.1` (oppure un altro
loopback ammesso). Una vecchia configurazione con `0.0.0.0` viene rifiutata
esplicitamente; non viene corretta silenziosamente.

```bash
cd /home/meshtastic/meshtastic_webchat
.venv/bin/python tools/static_selftest.py
.venv/bin/python -m unittest discover -s tools -p 'test_*.py' -v
sudo systemctl start meshtastic-webchat
journalctl -u meshtastic-webchat -n 50 --no-pager
```

Ricaricare la pagina con Ctrl+F5. La UI deve indicare `0.7.5-beta`.
Il backup di aggiornamento contiene anche i segreti dei canali.

## Prima installazione

```bash
cd /home/meshtastic/meshtastic_webchat
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp app_config.example.json app_config.json
chmod 600 app_config.json
chmod +x start_webchat.sh
```

Configurare `node.mode` (`serial` o `tcp`), `node.port` o `node.host` e la porta web.
Poi installare il servizio:

```bash
sudo cp meshtastic-webchat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshtastic-webchat
```

## Verifiche e limiti

Validato con Python e Meshtastic **2.7.11**: 12 test di regressione con protobuf
reali, SQLite temporaneo, client Flask e radio simulata; controlli statici Python,
JavaScript e shell. Sono coperti duplicati, migrazione legacy, retention, assenza
PSK nelle API, Apply tramite ID, read-back discordante/incompleto, pulizia secondari,
rollback, riavvio durante verifica pendente e backoff.

Non è stato eseguito un test su una radio fisica. Una risposta admin assente o
incompatibile non produce `Verified`. La verifica attesta la configurazione riletta
dal dispositivo, non la persistenza dopo un'interruzione improvvisa di alimentazione.
Il timeout complessivo del read-back è 12 secondi; la richiesta web Apply/Rollback
ha timeout 45 secondi. La libreria Meshtastic può normalizzare alcune impostazioni:
una differenza mantiene la verifica pendente.

La cancellazione SQLite rende le pagine riutilizzabili ma non riduce necessariamente
subito la dimensione fisica del file. Non viene eseguito `VACUUM` a ogni avvio.

La web UI non ha autenticazione: gli utenti che la raggiungono possono ancora
inviare messaggi e applicare stanze salvate. Nascondere le PSK non cambia questi permessi.
La scheda Config gestisce solo `app_config.json`, non un backup completo del firmware.

## File runtime

- `app_config.json`: collegamento al nodo e listener.
- `rooms.json`, `room_backups.json`: URL e segreti canale, solo lato server.
- `channel_verification.json`: target atteso, anche questo contiene materiale chiave.
- `address_book.json`: alias e note.
- `proxy_messages.db`, `webchat_cache.db`: ultimi 10.000 messaggi ciascuno.

Le scritture JSON sono atomiche con permessi 0600. I file runtime non fanno parte
 dello ZIP. Il codice parte dal pacchetto v0.7.4-beta ottimizzato, senza modificare
il branch stable su GitHub.
