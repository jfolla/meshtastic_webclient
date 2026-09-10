# Meshtastic Web Chat

**Version:** v0.7.6-beta  
**Status:** Beta  
**Current branch:** `main`  
**Application folder:** `meshtastic_webchat`  
**systemd service:** `meshtastic-webchat.service`

[Download the v0.7.6-beta upgrade ZIP](https://github.com/jfolla/meshtastic_webclient/raw/refs/heads/main/downloads/meshtastic_webchat_v0.7.6_beta_optimized.zip)

A Flask interface with Chat, Channels, Address Book, Nodes, Config and Debug tabs.
A local proxy maintains the serial or TCP connection to the Meshtastic node.
The interface, application messages and documentation are in English.

## Changes in v0.7.6-beta

- Required login for the UI and all application APIs; no default account or password.
- Local `admin` and `viewer` accounts with scrypt password hashes.
- Opaque server-side sessions, sign-out, a 30-minute idle timeout and an eight-hour
  absolute lifetime. Background polling counts as activity. Restarting the web process
  signs everyone out. Changing an account password or role invalidates its sessions.
- HttpOnly, SameSite=Strict cookies, HTTPS-only cookies by default, CSRF protection
  and a five-attempt login limit per IP in a five-minute window.
- Viewer accounts can inspect cached data but cannot send messages, change channels,
  trigger device refreshes, edit aliases, import/export configuration or clear history.
  Permission checks apply to direct API calls as well as the interface.
- Direct messages request an ACK. Delivery status is synchronized to the UI without
  duplicating messages. Only an ACK from the addressed node confirms recipient delivery;
  a local or implicit ACK does not. This is not a read receipt.
- Broadcasts display `Broadcast sent · delivery unconfirmed`, never a recipient ACK.
- A 180-second ACK timeout displays `No confirmation`; a later recipient ACK can update
  it. NAKs and local send errors display `Send failed`. Restarted pending sends become
  unconfirmed and are not automatically resent.

## Account setup — required after upgrading

Create an administrator on the server before starting the updated service:

```bash
cd /home/meshtastic/meshtastic_webchat
sudo -u meshtastic .venv/bin/python auth.py --user admin --role admin
```

The command prompts for a password twice; use at least 12 characters. Passwords are
not passed in command-line arguments. Only a scrypt hash is stored in `auth.json`
with mode 0600. Run the same command to reset a password. To create a read-only account:

```bash
sudo -u meshtastic .venv/bin/python auth.py --user observer --role viewer
```

Without configured accounts the login page remains locked and displays setup guidance.
No browser-based first-user registration or default credentials are provided.
Account files are preserved during upgrades and excluded from the archive and Git.

Use HTTPS, either the existing `web.ssl_adhoc: true` listener or an HTTPS reverse proxy.
`auth.secure_cookie` defaults to `true`, including for old configuration files. If you
intentionally use plain HTTP on a trusted network, set `"auth": {"secure_cookie": false}`
and restart the service. Plain HTTP does not protect passwords in transit.

## Storage and channel handling

1. **Batch SQLite synchronization:** one connection and transaction per batch,
   `INSERT OR IGNORE`, proxy ID deduplication and migration of legacy rows.
   With 100 previously imported messages, polling every three seconds opens about
   20 synchronization connections per minute instead of up to 2,000. This excludes
   reads requested by the UI. Unchanged snapshots do not rewrite messages.
   Connections are explicitly closed.
2. **Apply/Rollback with device read-back:** Apply first reads and saves a complete
   channel backup from the radio. After writing and waiting one second, it requests
   all eight channel slots and the LoRa configuration through correlated admin
   responses. Only a complete match with the requested ChannelSet produces
   `Verified`. Timeouts or differences produce `Applied / verification pending`.
   Periodic polling retries verification, and the expected target survives service
   restarts. Rollback retains its backup until verification succeeds. Extra secondary
   slots are disabled because `setURL()` alone leaves them enabled. If the initial
   device backup cannot be read, Apply stops before writing.
3. **Server-side channel secrets:** JSON API responses are filtered, including
   preview, import, Apply, Rollback, state, backups and Debug. The browser receives
   channel names, counts and fingerprints; Apply uses `room_id`. The import field
   contains the URL entered by the user and is cleared after import. The server
   does not return channel URLs or PSKs.
4. **Loopback-only proxy:** startup, the socket server, the Flask client and config
   import reject non-loopback proxy addresses. `127.0.0.1`, `localhost` and `::1`
   are supported. The web listener remains separately configurable for LAN access.
5. **Bounded storage and quieter logs:** both databases retain the latest 10,000
   messages. New packets no longer store `raw_json`, and existing values are cleared
   at startup. Reconnection waits follow 2 → 5 → 10 → 20 → 30 seconds, with one
   warning per failure sequence. Flask polling also backs off when the proxy is down.

## Upgrading from v0.7.5-beta or earlier

The ZIP contains the `meshtastic_webchat` folder. It does not include runtime
configuration, databases, address book entries, saved channels or backups, so these
existing files are preserved. Retention does automatically delete messages older
than the latest 10,000 in each database on the first startup.

Run these commands on the server, with the downloaded ZIP in the current directory:

```bash
sudo systemctl stop meshtastic-webchat
sudo tar -czf /home/meshtastic/meshtastic_webchat_pre_v0.7.6_backup.tar.gz \
  --exclude=meshtastic_webchat/.venv \
  -C /home/meshtastic meshtastic_webchat
sudo chmod 600 /home/meshtastic/meshtastic_webchat_pre_v0.7.6_backup.tar.gz
sudo unzip -o meshtastic_webchat_v0.7.6_beta_optimized.zip -d /home/meshtastic
sudo chown -R meshtastic:meshtastic /home/meshtastic/meshtastic_webchat
sudo chmod +x /home/meshtastic/meshtastic_webchat/start_webchat.sh
```

Check that `proxy.host` in `app_config.json` is `127.0.0.1` or another supported
loopback address. An old configuration using `0.0.0.0` is explicitly rejected;
it is not silently rewritten.

```bash
cd /home/meshtastic/meshtastic_webchat
sudo -u meshtastic .venv/bin/python auth.py --user admin --role admin
.venv/bin/python tools/static_selftest.py
.venv/bin/python -m unittest discover -s tools -p 'test_*.py' -v
sudo systemctl start meshtastic-webchat
journalctl -u meshtastic-webchat -n 50 --no-pager
```

Reload the browser with Ctrl+F5. The UI should display `0.7.6-beta`.
The upgrade backup also contains channel secrets.

## First installation

```bash
cd /home/meshtastic/meshtastic_webchat
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp app_config.example.json app_config.json
chmod 600 app_config.json
chmod +x start_webchat.sh
.venv/bin/python auth.py --user admin --role admin
```

Set `node.mode` to `serial` or `tcp`, configure `node.port` or `node.host`, and
choose the web listening port. Then install the service:

```bash
sudo cp meshtastic-webchat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshtastic-webchat
```

## Validation and limitations

Validated with Meshtastic **2.7.11**: 22 regression tests using real protobuf
messages, temporary SQLite databases, the Flask test client and a simulated radio,
plus Python, JavaScript and shell syntax checks. Coverage includes deduplication,
legacy migration, retention, PSK filtering, Apply by ID, mismatched or missing
read-back responses, secondary channel cleanup, rollback, restart during pending
verification reconnection backoff, login/logout, CSRF, session expiry, viewer API restrictions,
ACK correlation, early/late ACKs, NAKs and broadcast handling.

No physical radio test has been performed for device read-back or delivery ACKs. Missing or incompatible admin responses
do not produce `Verified`. Verification confirms configuration read from the device,
not persistence after a sudden power loss. Read-back has a total 12-second timeout;
the web Apply/Rollback request has a 45-second timeout. If the firmware normalizes
settings and the comparison differs, verification remains pending.

SQLite deletion makes pages reusable but does not necessarily shrink the physical
database file immediately. `VACUUM` is not run on every startup.

Authentication is mandatory. Administrator accounts can operate the radio; viewer
accounts have read-only access. The local proxy remains restricted to loopback.
The Config tab manages only `app_config.json`, not a complete radio backup.

## Runtime files

- `app_config.json`: node connection, listener and secure-cookie configuration.
- `auth.json`: local usernames, roles and password hashes; keep this file private.
- `rooms.json`, `room_backups.json`: channel URLs and secrets stored on the server.
- `channel_verification.json`: expected target, also containing channel key material.
- `address_book.json`: aliases and notes.
- `proxy_messages.db`, `webchat_cache.db`: the latest 10,000 messages in each database.

JSON writes are atomic with mode 0600. Runtime files are excluded from the ZIP.
v0.7.6-beta is the current version published on `main`.
