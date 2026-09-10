"""Run with: python -m unittest discover -s tools -p 'test_*.py' -v"""
import base64
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from proxy import main as proxy
from security import loopback_host
from werkzeug.security import generate_password_hash

TEST_HASH = generate_password_hash("test-password-123")

def login_client(role="admin"):
    client = app.app.test_client()
    account = {"role":role, "password_hash":TEST_HASH}
    app.access_control.path.write_text(json.dumps({"users":{"tester":account}}))
    with app.app.test_request_context():
        sid, entry = app.access_control.new_session("tester", account)
    client.set_cookie("webchat_session", sid)
    client.environ_base["HTTP_X_CSRF_TOKEN"] = entry["csrf"]
    return client



def channel_url(name='Test', count=1):
    cs = proxy.apponly_pb2.ChannelSet()
    for i in range(count):
        cs.settings.add(name=name + str(i), psk=b'private-test-key!')
    cs.lora_config.use_preset = True
    return proxy.UpstreamManager._encode_set(cs)


class FakeNode:
    nodeNum = 123
    def __init__(self, url):
        self.localConfig = SimpleNamespace(lora=proxy.apponly_pb2.ChannelSet().lora_config)
        self.channels = [proxy.channel_pb2.Channel(index=i) for i in range(8)]
        cs = proxy.UpstreamManager._decode_url(url)
        for i, settings in enumerate(cs.settings):
            self.channels[i].role = 1 if i == 0 else 2
            self.channels[i].settings.CopyFrom(settings)
        self.localConfig.lora.CopyFrom(cs.lora_config)
        self.device = [type(c).FromString(c.SerializeToString()) for c in self.channels]
        self.device_lora = type(cs.lora_config).FromString(cs.lora_config.SerializeToString())
        self.apply_writes = True
        self.requests = 0

    def setURL(self, url):
        cs = proxy.UpstreamManager._decode_url(url)
        for i, settings in enumerate(cs.settings):
            self.channels[i].settings.CopyFrom(settings)
            self.channels[i].role = 1 if i == 0 else 2
            self.writeChannel(i)
        if self.apply_writes:
            self.device_lora.CopyFrom(cs.lora_config)

    def writeChannel(self, index):
        if self.apply_writes:
            self.device[index].CopyFrom(self.channels[index])

    def _sendAdmin(self, message, wantResponse, onResponse):
        self.requests += 1
        response = proxy.admin_pb2.AdminMessage()
        if message.HasField('get_channel_request'):
            response.get_channel_response.CopyFrom(self.device[message.get_channel_request - 1])
        else:
            response.get_config_response.lora.CopyFrom(self.device_lora)
        onResponse({'from': self.nodeNum, 'decoded': {'admin': {'raw': response}}})


def manager(node):
    with patch.object(proxy.UpstreamManager, '_subscribe_once'):
        m = proxy.UpstreamManager({}, proxy.ProxyState(upstream_connected=True), None)
    m.iface = SimpleNamespace(localNode=node)
    # Skip only the post-write settling delay; request events remain real.
    m.stop_event = SimpleNamespace(wait=lambda seconds: False)
    return m


class Regressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.backups = patch.object(proxy, 'BACKUPS_PATH', self.path/'backups.json')
        self.backups.start()
        self.verification = patch.object(proxy, "VERIFICATION_PATH", self.path/"verification.json")
        self.verification.start()
        self.db = patch.object(app, 'DB_PATH', self.path/'web.db')
        self.db.start()
        app.init_db()
        app.access_control.configure(self.path/"auth.json", secure=False)

    def tearDown(self):
        self.db.stop(); self.backups.stop(); self.verification.stop(); self.tmp.cleanup()

    def test_batch_single_connection_duplicates_and_retention(self):
        rows = [{'id': i, 'ts': '2026', 'text': str(i)} for i in range(100)]
        real_connect = sqlite3.connect
        with patch.object(app.sqlite3, 'connect', wraps=real_connect) as connect:
            for _ in range(20):
                app.db_add_proxy_messages(rows)
            self.assertEqual(connect.call_count, 20)
        with real_connect(app.DB_PATH) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0],100)
        for i in range(100,10200,100):
            app.db_add_proxy_messages([{'id': j, 'ts': '2026', 'text': str(j)} for j in range(i,i+100)])
        with real_connect(app.DB_PATH) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0],10000)
            self.assertEqual(conn.execute('SELECT MIN(proxy_id) FROM messages').fetchone()[0],200)

    def test_legacy_migration(self):
        with sqlite3.connect(app.DB_PATH) as conn:
            conn.execute("INSERT INTO messages(ts,direction,text) VALUES ('2026','in','hello')")
        app.db_add_proxy_messages([{'id': 9, 'ts':'2026','text':'hello'}])
        with sqlite3.connect(app.DB_PATH) as conn:
            self.assertEqual(conn.execute('SELECT proxy_id FROM messages').fetchall(),[(9,)])

    def test_proxy_retention_and_no_raw_packet(self):
        path = self.path/'proxy.db'
        store = proxy.MessageStore(path)
        with sqlite3.connect(path) as conn:
            conn.executemany("INSERT INTO messages(ts,direction,text,raw_json) VALUES ('x','in','x','secret')", [()]*10010)
        store = proxy.MessageStore(path)
        store.add('in','a','b','hello',{'secret':'unused'})
        with sqlite3.connect(path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0],10000)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages WHERE raw_json IS NOT NULL').fetchone()[0],0)

    def test_apply_readback_and_secondary_cleanup(self):
        node = FakeNode(channel_url('Old',3)); m = manager(node)
        result = m.apply_room(channel_url('New'))
        self.assertTrue(result['active_room']['verified'])
        self.assertEqual(node.requests,18)
        self.assertEqual(node.device[1].role,0)
        result = m.rollback_room()
        self.assertTrue(result['active_room']['verified'])
        self.assertEqual(len(m._load_backups()),0)
        self.assertEqual(node.device[2].role,2)

    def test_local_cache_change_never_verifies_device_mismatch(self):
        node = FakeNode(channel_url('Old')); node.apply_writes = False
        m = manager(node)
        result = m.apply_room(channel_url('New'))
        self.assertFalse(result['active_room']['verified'])
        self.assertEqual(len(m._load_backups()),1)
        self.assertFalse(m.refresh_active_room()['active_room']['verified'])

    def test_timeout_and_pending_rollback_preserve_backup(self):
        node = FakeNode(channel_url('Old')); m = manager(node)
        m.apply_room(channel_url('New'))
        with patch.object(m, '_read_channel_urls', side_effect=TimeoutError):
            result = m.rollback_room()
        self.assertFalse(result['active_room']['verified'])
        self.assertEqual(len(m._load_backups()),1)
        self.assertTrue(m.refresh_active_room()['active_room']['verified'])
        self.assertEqual(len(m._load_backups()),0)

    def test_backup_read_failure_prevents_apply(self):
        node = FakeNode(channel_url()); m = manager(node)
        with patch.object(m, '_read_channel_urls', side_effect=TimeoutError), patch.object(node, 'setURL') as write:
            with self.assertRaises(TimeoutError): m.apply_room(channel_url('New'))
            write.assert_not_called()

    def test_missing_responses_are_not_cached_reads(self):
        node = FakeNode(channel_url()); m = manager(node)
        with patch.object(node, '_sendAdmin'), patch.object(threading.Event, 'wait', return_value=False):
            with self.assertRaises(TimeoutError): m._read_channel_urls()

    def test_public_api_removes_nested_secrets_and_apply_uses_id(self):
        url = channel_url(); fragment = url.split('#')[1]
        room = {**app.normalize_room_input(url), 'name':'Test', 'preview':m_preview(url)}
        with patch.object(app, 'rooms_cache', [room]), patch.dict(app.proxy_cache, {
            'active_room':room, 'backups':[{'previous_full_url':url}],
            'state':{'active_room_url':url}, 'debug':{'nested':{'psk':'private-test-key!', 'full_url':url}}}):
            client = login_client()
            for endpoint in ['/api/snapshot','/api/rooms','/api/rooms/active','/api/rooms/backups','/api/state','/api/debug']:
                response = client.get(endpoint)
                self.assertEqual(response.status_code,200)
                self.assertNotIn(fragment,response.text)
                self.assertNotIn('private-test-key!',response.text)
            with patch.object(app,'proxy_request',return_value={'ok':True,'active_room':room}) as send:
                response = client.post('/api/rooms/apply',json={'room_id':room['id']})
                self.assertEqual(response.status_code,200)
                self.assertEqual(send.call_args.args[0]['url'],url)
                self.assertNotIn(fragment,response.text)
            with patch.object(app,'proxy_request',return_value={'ok':True,'preview':m_preview(url)}):
                for endpoint in ['/api/rooms/preview','/api/rooms/import']:
                    with patch.object(app,'save_rooms'):
                        response=client.post(endpoint,json={'value':url})
                    self.assertEqual(response.status_code,200)
                    self.assertNotIn(fragment,response.text)

    def test_pending_verification_survives_restart(self):
        node = FakeNode(channel_url('Old')); node.apply_writes = False
        m = manager(node)
        m.apply_room(channel_url('New'))
        restarted = manager(node)
        self.assertFalse(restarted.refresh_active_room()['active_room']['verified'])

    def test_reconnect_backoff(self):
        node = FakeNode(channel_url()); m = manager(node); m.iface = None
        delays = []
        class Stop:
            def is_set(self): return len(delays) == 6
            def wait(self, delay): delays.append(delay)
        m.stop_event = Stop()
        with patch.object(m, '_connect', side_effect=OSError):
            m._worker()
        self.assertEqual(delays, [2,5,10,20,30,30])

    def test_proxy_loopback_only(self):
        for host in ['0.0.0.0','::','192.168.1.3','example.com','127.0.0.1.evil']:
            with self.assertRaises(ValueError): loopback_host(host)
            with self.assertRaises(ValueError): proxy.ThreadedJSONServer((host,0),proxy.JSONHandler,None)
        for host in ['127.0.0.1','localhost','::1']:
            self.assertTrue(loopback_host(host))
        with proxy.ThreadedJSONServer(('localhost',0),proxy.JSONHandler,None) as server:
            self.assertEqual(server.server_address[0],'127.0.0.1')


def m_preview(url):
    return proxy.UpstreamManager.preview_room(url)

if __name__ == '__main__': unittest.main()
