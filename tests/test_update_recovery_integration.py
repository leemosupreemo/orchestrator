"""Authenticated control-plane -> durable agent -> installation-gate recovery contract."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_control_plane import PlaneCase
from orchestrator.desktop_agent import AgentDependencies, DesktopAgent
from orchestrator.runtime_control import ActivityGate


class UpdateRecoveryIntegrationTests(PlaneCase):
    def setUp(self):
        super().setUp()
        # Use a real epoch so the agent's clock and the account clock agree.
        import time
        self.clock.t = time.time()
        self.mid, self.secret = self.pair()
        self.tmp = tempfile.TemporaryDirectory()
        self.machine = {'machine_id':self.mid, 'machine_secret':self.secret}
        self.machine_patch = patch('orchestrator.account.load_machine', return_value=self.machine)
        self.machine_patch.start()
        def beat(machine, endpoint, **body):
            body.update(endpoint=endpoint, packaged=True, capabilities={'preview_remote':False})
            status, reply = self.call('POST', '/cp/machine/heartbeat', body, machine_auth=f'Machine {self.mid}:{self.secret}')
            self.assertEqual(status, 200)
            return reply
        def report(machine, operation_id, state):
            status, reply = self.call('POST', '/cp/machine/update-progress', {'operation_id':operation_id,'state':state},
                                      machine_auth=f'Machine {self.mid}:{self.secret}')
            self.assertEqual(status, 200)
            return reply
        self.dependencies = AgentDependencies(heartbeat=beat, update_progress=report)
        self.agent = DesktopAgent(Path(self.tmp.name)/'state', Path(self.tmp.name)/'ctl', {}, self.dependencies)
        self.agent.local = SimpleNamespace(gate=ActivityGate())
        self.beat_agent()

    def tearDown(self):
        self.machine_patch.stop()
        self.tmp.cleanup()

    def beat_agent(self):
        self.agent._heartbeat(self.machine, 'https://runner.example.com', 0, None, {})

    def start_update(self):
        status, reply = self.call('POST','/cp/machines/update-recovery',{'machine_id':self.mid},user='alice-token')
        self.assertEqual(status,200)
        self.beat_agent()
        return reply['update_operation']['id']

    def test_cancel_delivered_before_idle_prevents_installation_and_is_acknowledged(self):
        op = self.start_update()
        self.assertTrue(self.agent.handle('update_progress', {'operation_id':op,'state':'downloading'})['accepted'])
        self.assertEqual(self.call('POST','/cp/machines/cancel-update',{'machine_id':self.mid,'operation_id':op},user='alice-token')[0],200)
        self.beat_agent()
        self.assertEqual(self.agent._recovery.snapshot()['action'],'cancel')
        self.assertFalse(self.agent.handle('prepare_update', {'operation_id':op})['accepted'])
        self.assertEqual(self.agent.local.gate.snapshot()['preparing'],'')
        self.assertTrue(self.agent.handle('update_progress', {'operation_id':op,'state':'cancelled'})['accepted'])
        self.beat_agent()
        self.assertTrue(self.store.get('machine_updates', self.mid)['acknowledged'])

    def test_busy_work_is_preserved_then_install_claim_blocks_cancel_and_new_work(self):
        op = self.start_update()
        release = self.agent.local.gate.hold()
        self.assertFalse(self.agent.handle('prepare_update', {'operation_id':op})['accepted'])
        self.assertEqual(self.agent.local.gate.snapshot()['active'],1)
        release()
        self.assertTrue(self.agent.handle('prepare_update', {'operation_id':op})['accepted'])
        self.assertEqual(self.call('POST','/cp/machines/cancel-update',{'machine_id':self.mid,'operation_id':op},user='alice-token')[0],409)
        from orchestrator.runtime_control import BusyError
        with self.assertRaises(BusyError):
            self.agent.local.gate.hold()
        # The new process restores the durable operation and reports successful relaunch.
        restored = DesktopAgent(Path(self.tmp.name)/'state',Path(self.tmp.name)/'other',{},self.dependencies)
        self.assertEqual(restored._recovery.snapshot()['id'],op)
        self.assertTrue(restored.handle('update_progress', {'operation_id':op,'state':'current'})['accepted'])
        self.assertEqual(self.store.get('machine_updates', self.mid)['state'],'current')
