import json
import re
import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_regressions import Regressions, TEST_HASH, app, proxy, login_client, manager, FakeNode, channel_url


class AuthDeliveryTests(unittest.TestCase):
    setUp = Regressions.setUp
    tearDown = Regressions.tearDown
    # Reuse isolated filesystem setup without duplicating the inherited test suite.
    def test_auth_no_default_account(self):
        client = app.app.test_client()
        self.assertEqual(client.get('/login').status_code,503)
        for path in ['/api/snapshot','/api/messages','/api/rooms','/api/config/export']:
            self.assertEqual(client.get(path).status_code,401)
        with patch.object(app,'proxy_request') as send:
            self.assertEqual(client.post('/api/send',json={'text':'x'}).status_code,401)
            send.assert_not_called()

    def test_real_login_logout_and_cookie_flags(self):
        app.access_control.secure = True
        app.access_control.path.write_text(json.dumps({'users':{'admin':{'role':'admin','password_hash':TEST_HASH}}}))
        client=app.app.test_client()
        response=client.get('/login')
        cookie=response.headers['Set-Cookie']
        for flag in ['Secure','HttpOnly','SameSite=Strict']:
            self.assertIn(flag,cookie)
        csrf=re.search('name="csrf" value="([^"]+)"',response.text)[1]
        old_cookie=client.get_cookie('webchat_session').value
        response=client.post('/login',data={'username':'admin','password':'test-password-123','csrf':csrf})
        self.assertEqual(response.status_code,302)
        self.assertNotEqual(client.get_cookie('webchat_session').value,old_cookie)
        self.assertEqual(client.get('/').status_code,200)
        sid=client.get_cookie('webchat_session').value
        token=app.access_control.sessions[sid]['csrf']
        self.assertEqual(client.post('/logout',headers={'X-CSRF-Token':token}).status_code,200)
        client.set_cookie('webchat_session',sid)
        self.assertEqual(client.get('/api/status').status_code,401)

    def test_csrf_and_viewer_enforced_server_side(self):
        client=login_client('viewer')
        self.assertEqual(client.get('/api/snapshot').status_code,200)
        for path in ['/api/config/export','/api/debug','/api/rooms/active?refresh=1']:
            self.assertEqual(client.get(path).status_code,403)
        for rule in app.app.url_map.iter_rules():
            if rule.rule.startswith('/api/'):
                for method in rule.methods & {'POST','PUT','DELETE','PATCH'}:
                    url=rule.rule.replace('<path:room_id>','test').replace('<path:node_id>','test')
                    self.assertEqual(client.open(url,method=method,json={}).status_code,403,(method,url))
        admin=login_client()
        with patch.object(app,'proxy_request') as send:
            self.assertEqual(admin.post('/api/send',json={'text':'x'},headers={'X-CSRF-Token':'wrong'}).status_code,403)
            send.assert_not_called()

    def test_session_expiry_and_password_change(self):
        client=login_client()
        sid=client.get_cookie('webchat_session').value
        app.access_control.sessions[sid]['created']-=9*3600
        self.assertEqual(client.get('/api/status').status_code,401)
        client=login_client()
        app.access_control.path.write_text(json.dumps({'users':{}}))
        self.assertEqual(client.get('/api/status').status_code,401)

    def test_login_csrf_and_throttle(self):
        login_client()
        client=app.app.test_client()
        response=client.get('/login')
        csrf=re.search('name="csrf" value="([^"]+)"',response.text)[1]
        self.assertEqual(client.post('/login',data={'username':'tester','password':'test-password-123'}).status_code,403)
        for _ in range(5):
            self.assertEqual(client.post('/login',data={'username':'tester','password':'wrong','csrf':csrf}).status_code,401)
        self.assertEqual(client.post('/login',data={'username':'tester','password':'test-password-123','csrf':csrf}).status_code,429)

    def delivery_manager(self):
        store=proxy.MessageStore(self.path/'proxy.db')
        m=manager(FakeNode(channel_url()))
        m.store=store
        m.config={'node':{'channel':0}}
        m.iface.sendText=lambda **kwargs:SimpleNamespace(id=99)
        return m

    def routing(self,sender,reason='NONE',request_id=99):
        return {'from':sender,'decoded':{'requestId':request_id,'routing':{'errorReason':reason}}}

    def test_ack_must_be_from_recipient_and_updates_cache(self):
        m=self.delivery_manager()
        m.send_text('hello','!00000123')
        app.db_add_proxy_messages(m.store.list())
        m.on_receive(self.routing(123),m.iface)  # Local ACK is insufficient.
        self.assertEqual(m.store.list()[0]['delivery_status'],'pending_ack')
        m.on_receive(self.routing(0x123,request_id=98),m.iface)
        self.assertEqual(m.store.list()[0]['delivery_status'],'pending_ack')
        m.on_receive(self.routing(0x123),m.iface)
        self.assertEqual(m.store.list()[0]['delivery_status'],'acknowledged')
        app.db_add_proxy_messages(m.store.list())
        self.assertEqual(app.db_list_messages()[0]['delivery_status'],'acknowledged')
        self.assertEqual(len(app.db_list_messages()),1)

    def test_broadcast_does_not_claim_delivery(self):
        m=self.delivery_manager()
        with patch.object(m.iface,'sendText',return_value=SimpleNamespace(id=99)) as send:
            m.send_text('broadcast')
            self.assertFalse(send.call_args.kwargs['wantAck'])
        self.assertEqual(m.store.list()[0]['delivery_status'],'broadcast_sent')
        m.on_receive(self.routing(123),m.iface)
        self.assertEqual(m.store.list()[0]['delivery_status'],'broadcast_sent')

    def test_early_ack_is_not_overwritten(self):
        m=self.delivery_manager()
        def send(**kwargs):
            self.assertTrue(kwargs['wantAck'])
            kwargs['onResponse'](self.routing(0x123))
            return SimpleNamespace(id=99)
        m.iface.sendText=send
        m.send_text('hello','!00000123')
        self.assertEqual(m.store.list()[0]['delivery_status'],'acknowledged')

    def test_timeout_late_ack_nak_and_restart(self):
        m=self.delivery_manager()
        m.send_text('hello','!00000123')
        with sqlite3.connect(m.store.db_path) as conn:
            conn.execute('UPDATE messages SET ack_deadline=0')
        self.assertEqual(m.store.list()[0]['delivery_status'],'no_confirmation')
        m.on_receive(self.routing(0x123),m.iface)
        self.assertEqual(m.store.list()[0]['delivery_status'],'acknowledged')
        m.send_text('again','!00000123')
        m.on_receive(self.routing(123,'NO_ROUTE'),m.iface)
        self.assertEqual(m.store.list()[-1]['delivery_status'],'failed')
        m.send_text('restart','!00000123')
        restarted=proxy.MessageStore(m.store.db_path)
        self.assertEqual(restarted.list()[-1]['delivery_status'],'no_confirmation')

    def test_local_send_failure(self):
        m=self.delivery_manager()
        with patch.object(m.iface,'sendText',side_effect=OSError):
            with self.assertRaises(OSError):m.send_text('x','!00000123')
        self.assertEqual(m.store.list()[0]['delivery_status'],'failed')


del Regressions
