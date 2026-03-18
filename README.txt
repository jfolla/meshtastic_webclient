Meshtastic Web Chat locale

1) Attiva il venv:
   source ~/meshtastic-chat/.venv/bin/activate

2) Installa Flask nel venv se manca:
   python -m pip install flask meshtastic pypubsub

3) Avvia backend seriale (UFFICIO):
   python app.py --port /dev/ttyUSB0 --listen-host 0.0.0.0 --listen-port 8088

4) Avvia backend TCP (CASA):
   python app.py --host 192.168.0.18 --listen-host 0.0.0.0 --listen-port 8088

5) Apri dal browser:
   http://IP_DEL_PC:8088
   oppure sullo stesso PC:
   http://127.0.0.1:8088

Note:
- Un solo processo per volta può usare la stessa seriale.
- I messaggi vengono salvati in messages.db e messages.jsonl.
