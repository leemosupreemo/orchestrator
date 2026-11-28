import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from orchestrator.verification_attempts import AttemptKey, AttemptStatus
from orchestrator.verification_candidate import Candidate
from orchestrator.verification_coordinator import run_remote_check
from orchestrator.verification_transport import ProbeRequest


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "attempt.json"
        self.key = AttemptKey("1" * 32, "tests", "2" * 40, "3" * 32, "4" * 32, "5" * 64, "6" * 64)
        self.request = ProbeRequest(self.key.repository, "candidate-ref", Candidate(self.key.candidate, "7" * 40),
                                    {self.key.check: self.key.command_digest})
        self.now = 100
        self.finished = AttemptStatus("finished", result={
            "name": "tests", "status": "passed", "output": "", "toolchain": "macOS | arm64",
        })

    def sleep(self, seconds):
        self.now += seconds

    def run_check(self, rpc, **kwargs):
        options = {"key": self.key, "request": self.request, "expected_toolchain": "macOS | arm64",
                   "timeout": 120, "clock": lambda: self.now, "sleep": self.sleep, "rpc": rpc} | kwargs
        return run_remote_check(self.path, {"name": "air"}, None, **options)

    def test_lost_start_reply_recovers_existing_result(self):
        operations = []

        def rpc(machine, allowed, key, operation, **kwargs):
            self.assertTrue(self.path.exists())
            self.assertEqual(key, self.key)
            operations.append(operation)
            return None if operation == "start" else self.finished

        self.assertEqual(self.run_check(rpc), self.finished)
        self.assertEqual(operations, ["start", "status"])
        never = Mock(side_effect=AssertionError("terminal result should be reused"))
        self.assertEqual(self.run_check(never), self.finished)
        never.assert_not_called()

    def test_owner_restart_reconciles_before_dispatching(self):
        interrupted = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.run_check(interrupted)
        rpc = Mock(return_value=self.finished)
        self.assertEqual(self.run_check(rpc), self.finished)
        self.assertEqual(rpc.call_args.args[3], "status")

    def test_restart_can_collect_completed_evidence_after_lease_window(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_check(Mock(side_effect=KeyboardInterrupt))
        self.now += 75
        rpc = Mock(return_value=self.finished)
        self.assertEqual(self.run_check(rpc), self.finished)
        self.assertEqual([call.args[3] for call in rpc.call_args_list], ["status"])

    def test_one_journal_cannot_accept_a_new_owner_generation(self):
        self.run_check(Mock(return_value=self.finished))
        newer = AttemptKey(self.key.invocation_id, self.key.check, self.key.candidate, "8" * 32,
                           self.key.attempt_id, self.key.repository, self.key.command_digest)
        rpc = Mock()
        self.assertEqual(self.run_check(rpc, key=newer).status, "conflict")
        rpc.assert_not_called()

    def test_toolchain_change_fences_success(self):
        rpc = Mock(return_value=AttemptStatus("finished", result=self.finished.result | {"toolchain": "different"}))
        self.assertEqual(self.run_check(rpc).status, "unavailable")

    def test_renews_active_attempt_and_preserves_test_failure(self):
        failed = AttemptStatus("finished", result=self.finished.result | {"status": "failed", "exit_code": 7})
        rpc = Mock(side_effect=[AttemptStatus("reserved"), AttemptStatus("active"), failed])
        self.assertEqual(self.run_check(rpc), failed)
        self.assertEqual([call.args[3] for call in rpc.call_args_list], ["start", "renew", "renew"])

    def test_owner_handoff_revokes_attempt(self):
        rpc = Mock(side_effect=[AttemptStatus("reserved"), AttemptStatus("cancelled")])
        self.assertEqual(self.run_check(rpc, is_current=lambda: self.now == 100).status, "cancelled")
        self.assertEqual([call.args[3] for call in rpc.call_args_list], ["start", "cancel"])
        self.assertEqual(json.loads(self.path.read_text())["terminal"]["status"], "cancelled")

    def test_unreachable_helper_gets_bounded_reconciliation(self):
        rpc = Mock(return_value=None)
        result = self.run_check(rpc)
        self.assertEqual(result.status, "unavailable")
        self.assertLessEqual(self.now, 220)
        operations = [call.args[3] for call in rpc.call_args_list]
        self.assertEqual(operations.count("start"), 1)
        self.assertEqual(operations[-1], "cancel")
