"""Runner boundaries for managed connection proof and heartbeat repairs."""
import hashlib
import hmac
import threading
import time
import unittest
from unittest.mock import patch
from tests.test_web_ui import ServerTestCase, UI_HEADERS
from orchestrator import account


class FakeConnectionManager:
    endpoint = 'https://mac.example.test'
    ack_id = ''
    def __init__(self):
        self.requests = []
    def status(self):
        return {'state': 'connected', 'provider': 'external', 'endpoint': self.endpoint, 'detail': 'Connected', 'checked_at': 123}
    def request(self, command_id, action, **kwargs):
        self.requests.append((command_id, action))
        self.ack_id = command_id
        return True


class ManagedConnectionWebTests(ServerTestCase):
    def test_public_health_challenge_proves_this_runner_without_exposing_credentials(self):
        nonce = 'a' * 32
        res, data = self.request('GET', '/api/connection-health?nonce='+nonce, auth=False)
        self.assertEqual(res.status, 200)
        self.assertEqual(data, {'nonce': nonce, 'proof': hmac.new(b'test-token', ('connection-health:'+nonce).encode(), hashlib.sha256).hexdigest()})
        self.assertNotIn('test-token', str(data))
        for query in ('nonce=bad', 'nonce='+nonce+'&nonce='+nonce, ''):
            res, _ = self.request('GET', '/api/connection-health?'+query, auth=False)
            self.assertEqual(res.status, 400)
        res, _ = self.request('GET', '/api/state', auth=False)
        self.assertEqual(res.status, 401)

    def test_authenticated_state_exposes_connection_progress(self):
        self.server.connection_manager = FakeConnectionManager()
        _, data = self.request('GET', '/api/state')
        self.assertEqual(data['connection']['state'], 'connected')
        self.assertEqual(data['connection']['provider'], 'external')

    def test_heartbeat_delivers_command_and_acknowledges_acceptance_without_starting_a_job(self):
        manager = self.server.connection_manager = FakeConnectionManager()
        calls = []
        def heartbeat(machine, endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            if len(calls) == 1:
                return {'connection_command': {'id': 'repair-1', 'action': 'repair', 'created': time.time(), 'expires': time.time()+899}}
            self.server._stopping.set()
            return {}
        with patch.object(account, 'load_machine', return_value={'machine_id': 'one'}), patch.object(account, 'heartbeat', side_effect=heartbeat), patch.object(account, 'sync_snapshot'):
            thread = self.server.start_heartbeat(lambda: manager.endpoint, interval=.01)
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(manager.requests, [('repair-1', 'repair')])
        self.assertEqual(calls[0][1]['connection']['state'], 'connected')
        self.assertEqual(calls[1][1]['connection_ack'], 'repair-1')
        self.assertEqual(self.server.sessions.list(), [])

    def test_local_repair_requires_owner_and_fixed_action(self):
        manager = self.server.connection_manager = FakeConnectionManager()
        res, _ = self.request('POST', '/api/connection', body={'action': 'repair'}, headers=UI_HEADERS)
        self.assertEqual(res.status, 202)
        self.assertEqual(manager.requests[-1][1], 'repair')
        res, _ = self.request('POST', '/api/connection', body={'action': 'shell'}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)
        res, _ = self.request('POST', '/api/connection', body={'action': 'repair'}, headers=UI_HEADERS, auth=False)
        self.assertEqual(res.status, 401)


class HeartbeatConnectionPayloadTests(unittest.TestCase):
    def test_connection_progress_and_ack_are_sent_over_existing_machine_auth(self):
        status = {'state': 'needs_setup', 'provider': 'funnel', 'detail': 'Enable Funnel'}
        with patch.object(account, 'call', return_value={}) as call:
            account.heartbeat({'machine_id': 'one'}, '', connection=status, connection_ack='repair-1')
        body = call.call_args.args[1]
        self.assertEqual(body['connection'], status)
        self.assertEqual(body['connection_ack'], 'repair-1')

class ManagedStartupTests(ServerTestCase):
    def test_startup_adopts_public_url_and_prepares_health_host_before_probe(self):
        from orchestrator.web import server as ui
        class Manager(FakeConnectionManager):
            def __init__(self, **kwargs):
                super().__init__();self.kwargs=kwargs;self.started=False
            def start(self):
                self.started=True
                self.kwargs['prepare_endpoint']('https://mac.example.test')
                self.kwargs['notify']()
            def stop(self): pass
        with patch('orchestrator.connection_manager.ConnectionManager', Manager), patch.object(account, 'load_machine', return_value=None):
            manager=ui.start_managed_connection(self.server, public_url='https://mac.example.test')
        self.assertTrue(manager.started)
        self.assertEqual(manager.kwargs['initial_url'],'https://mac.example.test')
        self.assertEqual(manager.kwargs['secret'],'test-token')
        self.assertEqual(manager.kwargs['port'],self.port)
        self.assertIn('mac.example.test',self.server.allowed_hosts)
        self.assertIs(self.server.connection_manager,manager)
        self.assertTrue(self.server.on_reset_tunnel())
        self.assertEqual(manager.requests[-1][1],'repair')

    def test_cancelled_tunnel_discovery_terminates_its_real_child(self):
        import subprocess, sys
        from orchestrator.web import server as ui
        real_popen=subprocess.Popen
        owned=[]
        def launch(*args,**kwargs):
            child=real_popen([sys.executable,'-u','-c','import time; time.sleep(30)'],**kwargs)
            owned.append(child)
            return child
        cancelled=threading.Event()
        timer=threading.Timer(.05,cancelled.set)
        timer.start()
        try:
            with patch.object(ui.shutil,'which',return_value=sys.executable),patch.object(ui.subprocess,'Popen',side_effect=launch):
                process,url=ui.start_tunnel(8765,discovery_timeout=2,cancel_event=cancelled)
            self.assertIsNone(process)
            self.assertIsNone(url)
            self.assertIsNotNone(owned[0].poll())
        finally:
            timer.cancel()
            for child in owned:
                if child.poll() is None:child.kill();child.wait()
                child.stdout.close()

    def test_cli_forwards_managed_connection_flag(self):
        from orchestrator import cli
        with patch('orchestrator.web.server.main',return_value=0) as start:
            self.assertEqual(cli.main(['ui','--no-open','--managed-connection']),0)
        self.assertIn('--managed-connection',start.call_args.args[0])

class ConnectionCommandValidationTests(unittest.TestCase):
    def test_only_live_bounded_commands_are_executable(self):
        good={'id':'repair-1','action':'repair','created':100,'expires':1000}
        self.assertTrue(account.valid_connection_command(good,now=101))
        for change in ({'expires':101},{'expires':1001},{'created':True},{'expires':float('inf')},{'created':200},{'id':'x'*101},{'action':'shell'}):
            with self.subTest(change=change):
                self.assertFalse(account.valid_connection_command({**good,**change},now=101))
