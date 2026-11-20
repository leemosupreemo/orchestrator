import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.update_recovery import UpdateRecovery, clean_operation


class UpdateDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'update.json'
        self.now = time.time()
        self.op = dict(id='a' * 24, action='update', state='requested', created=self.now, expires=self.now + 1800)
        self.calls = []
        self.accepted = True
        def report(operation_id, state):
            self.calls.append((operation_id, state))
            return {'accepted': self.accepted, 'update_operation': {**self.op, 'state': state} if self.accepted else self.op}
        self.delivery = UpdateRecovery(self.path, report)

    def tearDown(self):
        self.tmp.cleanup()

    def test_delivery_survives_restart_and_stale_progress_is_rejected(self):
        self.assertTrue(self.delivery.accept(self.op))
        self.assertEqual(UpdateRecovery(self.path, None).snapshot(), self.op)
        self.assertFalse(self.delivery.report('b' * 24, 'checking'))
        self.assertEqual(self.calls, [])
        self.assertTrue(self.delivery.report(self.op['id'], 'checking'))
        self.assertEqual(self.calls, [(self.op['id'], 'checking')])

    def test_cancel_does_not_regress_to_late_download_or_install(self):
        self.delivery.accept({**self.op, 'action': 'cancel', 'state': 'cancelling'})
        self.assertFalse(self.delivery.accept(self.op))
        self.assertFalse(self.delivery.report(self.op['id'], 'downloading'))
        self.assertFalse(self.delivery.report(self.op['id'], 'updating'))
        self.assertEqual(self.calls, [])

    def test_rejected_install_claim_is_not_reported_as_installed(self):
        self.delivery.accept(self.op)
        self.accepted = False
        self.assertFalse(self.delivery.report(self.op['id'], 'updating'))
        self.assertEqual(self.delivery.snapshot()['state'], 'requested')

    def test_malformed_and_expired_delivery_is_not_installable(self):
        for op in ({}, {**self.op, 'expires': float('inf')}, {**self.op, 'expires': self.now - 1}):
            self.assertFalse(self.delivery.accept(op))
        self.assertEqual(self.delivery.snapshot(), None)

    def test_terminal_ack_and_installer_relaunch_reconciliation(self):
        self.delivery.accept({**self.op, 'state': 'updating'})
        self.delivery = UpdateRecovery(self.path, lambda operation_id, state: {
            'accepted': True, 'update_operation': {**self.op, 'state': state}})
        self.assertEqual(self.delivery.ack(), None)
        self.assertTrue(self.delivery.report(self.op['id'], 'current'))
        self.assertEqual(self.delivery.ack(), self.op['id'])

    def test_terminal_progress_is_retried_after_lost_connection_and_agent_restart(self):
        self.delivery.accept({**self.op, 'action': 'cancel', 'state': 'cancelling'})
        def offline(*args):
            raise OSError('offline')
        self.delivery._report = offline
        self.assertFalse(self.delivery.report(self.op['id'], 'cancelled'))
        restored = UpdateRecovery(self.path, lambda operation_id, state: {
            'accepted': True, 'update_operation': {**self.op, 'action': 'cancel', 'state': state}})
        self.assertTrue(restored.retry_pending())
        self.assertEqual(restored.ack(), self.op['id'])

    def test_terminal_progress_from_previous_request_cannot_complete_new_request(self):
        self.delivery.accept(self.op)
        self.delivery._report = lambda *args: (_ for _ in ()).throw(OSError('offline'))
        self.delivery.report(self.op['id'], 'failed')
        self.delivery.accept({**self.op, 'state': 'current'})
        new = {**self.op, 'id': 'b' * 24}
        self.assertTrue(self.delivery.accept(new))
        calls = []
        self.delivery._report = lambda *args: calls.append(args)
        self.assertFalse(self.delivery.retry_pending())
        self.assertEqual(calls, [])

    def test_validation_and_transition_guards(self):
        for value in (None, [], {**self.op, 'id': 123}, {**self.op, 'action': 'shell'},
                      {**self.op, 'action': {}}, {**self.op, 'state': []},
                      {**self.op, 'state': 'unknown'}, {**self.op, 'created': True},
                      {**self.op, 'created': self.now + 90}, {**self.op, 'expires': self.now + 2000}):
            self.assertIsNone(clean_operation(value))
        self.assertFalse(self.delivery.report('a' * 24, 'checking'))
        self.assertFalse(self.delivery.retry_pending())
        self.delivery.accept(self.op)
        self.assertFalse(self.delivery.accept({**self.op, 'id': 'b' * 24}))
        self.delivery.accept({**self.op, 'state': 'downloading'})
        self.assertFalse(self.delivery.accept(self.op))
        self.delivery.accept({**self.op, 'state': 'updating'})
        self.assertFalse(self.delivery.accept({**self.op, 'action':'cancel', 'state':'cancelling'}))
        self.delivery.accept({**self.op, 'state': 'current'})
        self.assertFalse(self.delivery.accept(self.op))
        self.assertTrue(self.delivery.report(self.op['id'], 'current'))
        self.assertFalse(self.delivery.report(self.op['id'], 'failed'))
        self.assertFalse(self.delivery.report(self.op['id'], 'nonsense'))
        self.assertEqual(self.calls, [])

    def test_invalid_persisted_pending_progress_does_not_break_startup(self):
        import json
        self.path.write_text(json.dumps({**self.op, 'pending_progress': []}))
        restored = UpdateRecovery(self.path, None)
        self.assertEqual(restored.snapshot(), self.op)
        self.assertFalse(restored.retry_pending())

    def test_expired_local_operation_cannot_claim_installation(self):
        self.delivery.accept(self.op)
        with patch('orchestrator.update_recovery.time.time', return_value=self.now + 1801):
            self.assertFalse(self.delivery.report(self.op['id'], 'updating'))
        self.assertEqual(self.calls, [])

    def test_malformed_status_and_rejected_progress_never_grant_installation(self):
        self.delivery.accept(self.op)
        self.delivery._report = lambda *args: {'accepted': False}
        self.assertFalse(self.delivery.report(self.op['id'], 'updating'))
        self.delivery._report = lambda *args: None
        self.assertFalse(self.delivery.report(self.op['id'], 'updating'))

    def test_late_reply_after_replacement_does_not_change_new_operation(self):
        self.delivery.accept(self.op)
        new = {**self.op, 'id': 'b' * 24}
        def report(*args):
            self.delivery.accept({**self.op, 'state':'current'})
            self.delivery.accept(new)
            return {'accepted':True, 'update_operation': {**self.op, 'state':'current'}}
        self.delivery._report = report
        self.assertFalse(self.delivery.report(self.op['id'], 'current'))
        self.assertEqual(self.delivery.snapshot(), new)
