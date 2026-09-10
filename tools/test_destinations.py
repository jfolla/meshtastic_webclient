import unittest
from types import SimpleNamespace
from unittest.mock import patch
import meshtastic.mesh_interface
from test_regressions import Regressions, proxy

class DestinationTests(unittest.TestCase):
    setUp = Regressions.setUp
    tearDown = Regressions.tearDown

    def radio(self):
        # Exercise the real library's sendText/sendData/_sendPacket conversion.
        iface = meshtastic.mesh_interface.MeshInterface.__new__(meshtastic.mesh_interface.MeshInterface)
        iface.myInfo = None
        iface.noProto = False
        iface.nodes = {'OFFICE': {'num': 2666031732}}
        iface.localNode = SimpleNamespace(localConfig=SimpleNamespace(lora=SimpleNamespace(hop_limit=3)))
        iface._generatePacketId = lambda: 99
        iface._addResponseHandler = lambda *args, **kwargs: None
        iface._sendToRadio = lambda packet: None
        with patch.object(proxy.UpstreamManager,'_subscribe_once'):
            m = proxy.UpstreamManager({'node':{'channel':0}},proxy.ProxyState(),proxy.MessageStore(self.path/'proxy.db'))
        m.iface = iface
        return m

    def test_recipient_forms_use_real_library(self):
        m=self.radio()
        for destination in ['!9ee86a74','0x9ee86a74','9ee86a74','2666031732',2666031732,'OFFICE',' !9ee86a74 ']:
            with self.subTest(destination=destination), patch.object(m.iface,'_sendToRadio') as send:
                m.send_text('hello',destination)
                packet=send.call_args.args[0].packet
                self.assertEqual(packet.to,2666031732)
                self.assertTrue(packet.want_ack)
                self.assertEqual(m.store.list()[-1]['to_id'],'!9ee86a74')

    def test_broadcast_forms(self):
        m=self.radio()
        for destination in [None,'','^all','!ffffffff','0xffffffff','4294967295']:
            with self.subTest(destination=destination), patch.object(m.iface,'_sendToRadio') as send:
                m.send_text('hello',destination)
                packet=send.call_args.args[0].packet
                self.assertEqual(packet.to,0xffffffff)
                self.assertFalse(packet.want_ack)

    def test_invalid_values_do_not_send(self):
        m=self.radio()
        with patch.object(m.iface,'_sendToRadio') as send:
            for value in ['LongFast','!zzzzzzzz','0','4294967296','-1']:
                with self.assertRaisesRegex(proxy.SendValidationError,'Invalid recipient'):
                    m.send_text('hello',value)
            m.config['node']['channel']='LongFast'
            with self.assertRaisesRegex(proxy.SendValidationError,'channel index'):
                m.send_text('hello','!9ee86a74')
            send.assert_not_called()

    def test_safe_validation_message_reaches_socket_client(self):
        m=self.radio()
        handler=proxy.JSONHandler.__new__(proxy.JSONHandler)
        handler.server=SimpleNamespace(manager=m)
        with patch.object(handler,'_send') as send:
            handler._dispatch({'type':'send_text','text':'hello','dest':'LongFast'})
            self.assertIn('Invalid recipient',send.call_args.args[0]['error'])
            self.assertFalse(send.call_args.args[0]['ok'])

del Regressions
