import hashlib
import hmac
import json
import os
import tempfile
import subprocess
import sys
import time
import threading
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from orchestrator import connection_manager as cm

class Connections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = cm.ConnectionStore(Path(self.tmp.name) / 'private' / 'connection.json')

    def manager(self, **kw):
        return cm.ConnectionManager(8765, 'secret', store=self.store, probe=lambda *a, **k: True, **kw)

    def test_store_persists_selected_and_fallbacks_privately(self):
        self.store.configure('external', 'https://runner.example')
        self.store.configure('cloudflare', enabled=True, select=False)
        loaded = cm.ConnectionStore(self.store.path)
        self.assertEqual(loaded.data['selected'], 'external')
        self.assertTrue(loaded.data['providers']['cloudflare']['enabled'])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.path.parent.stat().st_mode & 0o777, 0o700)

    def test_adoption_and_commands_survive_restart(self):
        m = self.manager(initial_url='https://machine.tail123.ts.net')
        self.assertEqual(m.store.data['selected'], 'funnel')
        self.assertTrue(m.request('command-1', 'repair'))
        again = self.manager()
        self.assertFalse(again.request('command-1', 'repair'))
        self.assertEqual(again.ack_id, 'command-1')

    def test_selects_only_proven_endpoint_and_configured_fallback(self):
        self.store.configure('external', 'https://broken.example')
        self.store.configure('cloudflare', select=False)
        process = type('Process', (), {'terminate': lambda s: None, 'poll': lambda s: None})()
        m = cm.ConnectionManager(8765, 'secret', store=self.store,
            cloudflare_start=lambda port: (process, 'https://good.example'),
            probe=lambda url, *a, **kw: url == 'https://good.example')
        m.check_once()
        self.assertEqual(m.endpoint, 'https://good.example')
        self.assertEqual(m.status()['provider'], 'cloudflare')
        self.assertEqual(self.store.data['selected'], 'cloudflare')
        m.stop()

    def test_repair_forces_cloudflare_renewal_and_cleans_unproven_process(self):
        self.store.configure('cloudflare', 'https://old.example')
        stopped=[]
        process=object()
        m=self.manager(cloudflare_start=lambda port: (process, None), cloudflare_stop=stopped.append)
        m._endpoint='https://old.example'
        m.request('renew-one', 'repair')
        m.check_once()
        self.assertEqual(stopped, [process])
        self.assertEqual(m.status()['state'], 'failed')

    def test_new_cloudflare_tunnel_can_finish_starting_before_health_probe_passes(self):
        process = type('Process', (), {'poll': lambda s: None})()
        stopped, probes = [], []
        m = cm.ConnectionManager(8765, 'secret', store=self.store,
            cloudflare_start=lambda port: (process, 'https://starting.example'), cloudflare_stop=stopped.append,
            probe=lambda *a, **kw: probes.append(a) or len(probes) > 1)
        with patch.object(m._stop, 'wait', return_value=False):
            self.assertTrue(m.check_once())
        self.assertEqual(m.endpoint, 'https://starting.example')
        self.assertEqual(m.status()['state'], 'connected')
        self.assertEqual(stopped, [])
        m.stop(wait=True)
        self.assertEqual(stopped, [process])

    def test_new_cloudflare_tunnel_readiness_wait_is_bounded_and_cancellable(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                process = type('Process', (), {'poll': lambda s: None})()
                stopped, probes = [], []
                m = cm.ConnectionManager(8765, 'secret', store=self.store,
                    cloudflare_start=lambda port: (process, 'https://unavailable.example'), cloudflare_stop=stopped.append,
                    probe=lambda *a, **kw: probes.append(a) or False)
                def wait(timeout):
                    if cancel:
                        m._stop.set()
                    return cancel
                with patch.object(m._stop, 'wait', side_effect=wait):
                    self.assertFalse(m.check_once())
                self.assertEqual(stopped, [process])
                self.assertLess(len(probes), 20)
                self.assertEqual(m.endpoint, '')
                if cancel:
                    self.assertEqual(len(probes), 1)

    def test_unconfigured_funnel_is_not_attempted(self):
        m = self.manager()
        with patch.object(m, '_funnel', side_effect=AssertionError('unexpected setup')):
            m.check_once()
        self.assertEqual(m.status()['state'], 'failed')

    def test_single_flight_check(self):
        entered, release = threading.Event(), threading.Event()
        self.store.configure('external', 'https://runner.example')
        m = cm.ConnectionManager(1, 's', store=self.store, probe=lambda *a, **k: (entered.set(), release.wait(2), True)[-1])
        thread = threading.Thread(target=m.check_once)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertFalse(m.check_once())
        release.set(); thread.join()
        self.assertEqual(m.status()['state'], 'connected')

    def test_funnel_route_conflict_preserved(self):
        self.store.configure('funnel', 'https://machine.tail123.ts.net')
        calls=[]
        def run(argv, **kw):
            calls.append(argv)
            return CompletedProcess(argv, 0, json.dumps({'Web': {'machine.tail123.ts.net:443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:9999'}}}}}), '')
        m = self.manager(native_runner=run)
        with self.assertRaisesRegex(RuntimeError, 'another service'):
            m._funnel(setup=True)
        self.assertFalse(any('--bg' in a for a in calls))

    def test_setup_links_are_strict(self):
        self.assertEqual(cm.setup_url('Enable at https://login.tailscale.com/f/funnel?node=x'), 'https://login.tailscale.com/f/funnel?node=x')
        for value in ('https://login.tailscale.com.evil/f/funnel', 'https://evil/f/funnel', 'https://login.tailscale.com/other'):
            self.assertEqual(cm.setup_url(value), '')

    def test_existing_funnel_grant_repairs_without_owner_prompt(self):
        self.store.configure('funnel', 'https://machine.tail123.ts.net')
        calls=[]
        def run(argv, **kw):
            calls.append(argv)
            if argv[1:3] == ['serve', 'status']:
                return CompletedProcess(argv, 0, '{}', '')
            if argv[1:3] == ['status', '--json']:
                return CompletedProcess(argv, 0, json.dumps({'Self': {'DNSName':'machine.tail123.ts.net.', 'CapMap': {'https://tailscale.com/cap/funnel': []}}}), '')
            return CompletedProcess(argv, 0, '', '')
        m = self.manager(native_runner=run)
        self.assertEqual(m._funnel(), 'https://machine.tail123.ts.net')
        self.assertTrue(any('--bg' in argv for argv in calls))

    def test_pending_owner_url_and_grant_resume_survive_restart(self):
        granted=[False]
        def run(argv, **kw):
            if argv[1:3] == ['serve', 'status']:
                return CompletedProcess(argv, 0, '{}', '')
            if argv[1:3] == ['status', '--json']:
                return CompletedProcess(argv, 0, json.dumps({'Self': {'DNSName':'machine.tail123.ts.net.', 'Capabilities': ['funnel'] if granted[0] else []}}), '')
            if not granted[0]:
                raise __import__('subprocess').TimeoutExpired(argv, 12, output=b'Enable https://login.tailscale.com/f/funnel?node=x')
            return CompletedProcess(argv, 0, '', '')
        m = cm.ConnectionManager(8765, 's', store=self.store, native_runner=run, probe=lambda *a, **k: granted[0])
        self.assertTrue(m.request('setup-one', 'setup_funnel'))
        m.check_once()
        self.assertEqual(m.status()['state'], 'needs_setup')
        self.assertTrue(cm.ConnectionStore(self.store.path).data['pending_setup'])
        granted[0]=True
        again=cm.ConnectionManager(8765, 's', store=cm.ConnectionStore(self.store.path), native_runner=run, probe=lambda *a, **k: True)
        again.check_once()
        self.assertEqual(again.endpoint, 'https://machine.tail123.ts.net')
        self.assertFalse(again.store.data['pending_setup'])

    def test_failed_repair_retries_are_bounded_until_new_command(self):
        self.store.configure('external', 'https://runner.example')
        probes=[]
        m=cm.ConnectionManager(8765, 's', store=self.store, probe=lambda *a, **k: probes.append(a) or False)
        for unused in range(10):
            m.check_once()
        self.assertEqual(len(probes), 10)
        m.request('retry-one', 'repair')
        m.check_once()
        self.assertEqual(len(probes), 11)

    def test_legacy_provider_migrates_and_https_ports_survive(self):
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text(json.dumps({'version':1,'selected':'tailscale','providers':{'tailscale':{'enabled':True,'url':'https://machine.tail123.ts.net:8443'}}}))
        loaded=cm.ConnectionStore(self.store.path)
        self.assertEqual(loaded.data['selected'], 'funnel')
        self.assertIn('funnel', loaded.data['providers'])
        self.assertEqual(cm.endpoint_url('https://runner.example:8443'), 'https://runner.example:8443')
        self.assertEqual(cm.endpoint_url('https://runner.example:443'), 'https://runner.example')
        self.assertEqual(cm.endpoint_url('https://runner.example:0'), '')

    def test_setup_url_rejects_untrusted_query_or_fragment(self):
        for suffix in ('', '?node=x&extra=y', '?node=x#oops', '?node=%78', '?node=x&node=y', '?node=é', '?node=x@y', '?node='+'x'*201):
            self.assertEqual(cm.setup_url('https://login.tailscale.com/f/funnel'+suffix), '')

    def test_setup_url_matches_control_plane_exact_host_without_port(self):
        self.assertEqual(cm.setup_url('https://login.tailscale.com:443/f/funnel?node=x'), '')

    def test_recovers_when_network_returns_after_bounded_restarts(self):
        self.store.configure('cloudflare', 'https://runner.example')
        online=[False]
        starts=[]
        m=cm.ConnectionManager(8765, 's', store=self.store, cloudflare_start=lambda port: starts.append(port) or (None, 'https://runner.example'), probe=lambda *a, **k: online[0])
        for unused in range(6): m.check_once()
        self.assertEqual(len(starts), 3)
        online[0]=True
        self.assertTrue(m.check_once())
        self.assertEqual(m.status()['state'], 'connected')
        self.assertEqual(len(starts), 3)

    def test_cloudflare_restart_burst_resumes_only_after_long_cooldown(self):
        self.store.configure('cloudflare', 'https://runner.example')
        now=[0]; starts=[]
        m=cm.ConnectionManager(8765, 's', store=self.store, clock=lambda: now[0], cloudflare_start=lambda port: starts.append(now[0]) or (None, 'https://runner.example'), probe=lambda *a, **k: False)
        for unused in range(3): m.check_once()
        now[0]=299
        m.check_once()
        self.assertEqual(starts, [0,0,0])
        now[0]=300
        m.check_once()
        self.assertEqual(starts, [0,0,0,300])

    def test_healthy_checks_probe_once_without_transient_checking_status(self):
        self.store.configure('external', 'https://runner.example')
        probes=[]; states=[]
        m=cm.ConnectionManager(8765, 's', store=self.store, probe=lambda *a, **k: probes.append(a) or True, notify=lambda: states.append(m.status()['state']))
        m.check_once()
        states.clear(); probes.clear()
        m.check_once()
        self.assertEqual(len(probes), 1)
        self.assertNotIn('checking', states)

    def test_stop_during_launch_cleans_returned_process_without_publication(self):
        entered, release=threading.Event(), threading.Event()
        process=object(); stopped=[]; prepared=[]
        def launch(port):
            entered.set(); release.wait(2)
            return process, 'https://runner.example'
        m=self.manager(cloudflare_start=launch, cloudflare_stop=stopped.append, prepare_endpoint=prepared.append)
        thread=threading.Thread(target=m.check_once); thread.start()
        self.assertTrue(entered.wait(1)); m.stop(); release.set(); thread.join()
        self.assertEqual(stopped, [process])
        self.assertEqual(prepared, [])
        self.assertEqual(m.endpoint, '')
        self.assertNotEqual(m.status()['state'], 'connected')

    def test_stop_during_native_read_prevents_funnel_write(self):
        entered, release=threading.Event(), threading.Event(); calls=[]
        def run(argv, **kw):
            calls.append(argv); entered.set(); release.wait(2)
            return CompletedProcess(argv,0,'{}','')
        m=self.manager(native_runner=run)
        m.request('setup-stop', 'setup_funnel')
        thread=threading.Thread(target=m.check_once); thread.start()
        self.assertTrue(entered.wait(1)); m.stop(); release.set(); thread.join()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(m.status()['state'], 'connected')

    def test_public_proof_uses_verified_ca_and_custom_https_port(self):
        contexts=[]
        marker=object()
        class HTTPS:
            def __init__(self, host, **kw): contexts.append(kw)
            def request(self, *a, **kw): pass
            def getresponse(self): return type('Response', (), {'status':302})()
            def close(self): pass
        with patch.object(cm, '_ssl_context', return_value=marker), patch.object(cm, 'PublicHTTPSConnection', HTTPS):
            self.assertFalse(cm.public_probe('https://runner.example:8443', 's', resolver=lambda *a: ['8.8.8.8']))
        self.assertIs(contexts[0]['context'], marker)
        self.assertEqual(contexts[0]['port'], 8443)

    def test_doh_uses_verified_ca_context(self):
        marker=object(); contexts=[]
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, limit): return b'{"Answer":[{"type":1,"data":"8.8.8.8"}]}'
        def open_url(url, **kw):
            contexts.append(kw.get('context')); return Response()
        with patch.object(cm, '_ssl_context', return_value=marker), patch.object(cm, 'urlopen', side_effect=open_url):
            self.assertEqual(cm.public_addresses('runner.example', True), ['8.8.8.8', '8.8.8.8'])
        self.assertEqual(contexts, [marker, marker])

    def test_acknowledged_repair_intent_survives_crash_before_execution(self):
        self.store.configure('cloudflare', 'https://old.example')
        first=self.manager()
        first.request('durable-repair','repair')
        starts=[]
        restored=cm.ConnectionManager(8765,'s',store=cm.ConnectionStore(self.store.path),cloudflare_start=lambda port: starts.append(port) or (None,'https://new.example'),probe=lambda *a,**k: True)
        restored.check_once()
        self.assertEqual(starts,[8765])
        self.assertEqual(restored.endpoint,'https://new.example')
        self.assertFalse(restored.store.data.get('pending_action'))

    def test_waiting_stop_reaps_actual_owned_child_before_return(self):
        marker=Path(self.tmp.name)/'terminated'
        child=subprocess.Popen([sys.executable,'-u','-c',"import signal,time,sys; signal.signal(signal.SIGTERM,lambda *a: (open(sys.argv[1],'w').write('stopped'),sys.exit(0))); print('ready',flush=True); time.sleep(60)",str(marker)],stdout=subprocess.PIPE,text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(),'ready')
        m=self.manager(); m._process=child
        m.stop(wait=True)
        self.assertIsNotNone(child.poll())
        self.assertEqual(marker.read_text(),'stopped')
        child.stdout.close()

    def test_waiting_stop_cancels_default_native_cli_child(self):
        marker=Path(self.tmp.name)/'native-terminated'
        binary=Path(self.tmp.name)/'tailscale'
        binary.write_text('#!'+sys.executable+'\nimport signal,time,sys\nsignal.signal(signal.SIGTERM,lambda *a: (open('+repr(str(marker))+',"w").write("stopped"),sys.exit(0)))\nopen('+repr(str(marker)+'.ready')+',"w").write("ready")\ntime.sleep(60)\n')
        binary.chmod(0o700)
        m=self.manager(); errors=[]
        def work():
            try: m._native(['status'])
            except cm.ConnectionStopped: errors.append('cancelled')
        with patch.dict(os.environ,{'PATH':str(binary.parent)+os.pathsep+os.environ['PATH']}):
            thread=threading.Thread(target=work); thread.start()
            deadline=time.monotonic()+2
            while not getattr(m,'_native_process',None) and time.monotonic()<deadline: time.sleep(.01)
            ready=Path(str(marker)+'.ready')
            while not ready.exists() and time.monotonic()<deadline: time.sleep(.01)
            self.assertTrue(ready.exists())
            m.stop(wait=True); thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,['cancelled'])
        self.assertEqual(marker.read_text(),'stopped')

    def test_private_token_survives_successful_url_updates(self):
        self.store.configure('cloudflare', token='private-token')
        self.store.configure('cloudflare', 'https://runner.example')
        self.assertEqual(cm.ConnectionStore(self.store.path).data['providers']['cloudflare']['token'],'private-token')
        self.assertNotIn('private-token',str(self.manager().status()))

    def test_expired_queued_repair_is_not_executed_after_restart(self):
        now=[10]; starts=[]
        self.store.configure('cloudflare','https://old.example')
        first=cm.ConnectionManager(8765,'s',store=self.store,clock=lambda:now[0])
        self.assertTrue(first.request('deadline-one','repair',expires=20))
        now[0]=21
        restored=cm.ConnectionManager(8765,'s',store=cm.ConnectionStore(self.store.path),clock=lambda:now[0],cloudflare_start=lambda port:starts.append(port) or (None,'https://runner.example'),probe=lambda *a,**k:True)
        restored.check_once()
        self.assertEqual(starts,[])
        self.assertFalse(restored.store.data.get('pending_action'))

    def test_failed_acceptance_does_not_publish_an_undurable_ack(self):
        m=self.manager()
        with patch.object(self.store,'save',side_effect=OSError('disk full')), self.assertRaises(OSError):
            m.request('not-saved','repair')
        self.assertEqual(m.ack_id,'')

    def test_expired_unstarted_setup_does_not_create_pending_owner_workflow(self):
        now=[10]
        first=cm.ConnectionManager(8765,'s',store=self.store,clock=lambda:now[0])
        first.request('expired-setup','setup_funnel',expires=20)
        now[0]=21
        restored=cm.ConnectionManager(8765,'s',store=cm.ConnectionStore(self.store.path),clock=lambda:now[0],native_runner=lambda *a,**k: self.fail('expired setup ran native CLI'))
        restored.check_once()
        self.assertFalse(restored.store.data.get('pending_setup'))

    def test_secure_probe_pins_public_ip_and_uses_nonce_without_token(self):
        calls=[]
        class Response:
            status=200
            def read(self, limit):
                nonce=calls[-1][1].split('nonce=')[1]
                return json.dumps({'nonce':nonce,'proof':hmac.new(b'secret', ('connection-health:'+nonce).encode(), hashlib.sha256).hexdigest()}).encode()
        class HTTPS:
            def __init__(self, host, **kw): calls.append(('host',host))
            def request(self, method, path, **kw): calls.append((method,path,kw))
            def getresponse(self): return Response()
            def close(self): pass
        with patch.object(cm, 'PublicHTTPSConnection', HTTPS):
            self.assertTrue(cm.public_probe('https://runner.example', 'secret', resolver=lambda host, public: ['8.8.8.8']))
        self.assertNotIn('secret', str(calls))
        self.assertFalse(cm.public_probe('https://runner.example', 'secret', resolver=lambda *a: ['127.0.0.1']))
        self.assertFalse(cm.public_probe('http://runner.example', 'secret'))

if __name__ == '__main__': unittest.main()
