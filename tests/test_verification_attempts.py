import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from orchestrator.verification_attempts import AttemptKey, WorkerJournal
from orchestrator.verification_execution import LeaseLost, execute


def key(attempt="a", generation="owner", check="protocol"):
    return AttemptKey("invocation", check, "candidate", generation, attempt)


class WorkerJournalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.now = 100
        self.journal = WorkerJournal(self.path, clock=lambda: self.now)

    def test_duplicate_start_and_reconnect_return_the_same_terminal_result(self):
        self.assertEqual(self.journal.reserve(key()).status, "reserved")
        self.assertEqual(self.journal.reserve(key()).status, "active")
        self.assertTrue(self.journal.finish(key(), {"status": "failed", "exit_code": 1}))
        reopened = WorkerJournal(self.path, clock=lambda: self.now)
        retry = reopened.reserve(key())
        self.assertEqual(retry.status, "finished")
        self.assertEqual(retry.result, {"status": "failed", "exit_code": 1})
        self.assertFalse(reopened.finish(key(), {"status": "passed"}))

    def test_at_most_one_concurrent_reservation_across_journal_instances(self):
        def reserve(attempt):
            return WorkerJournal(self.path, clock=lambda: self.now).reserve(key(attempt)).status

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertCountEqual(executor.map(reserve, ("a", "b")), ["reserved", "busy"])

    def test_renewal_expiry_and_late_result(self):
        self.journal.reserve(key())
        self.now = 159
        self.assertTrue(self.journal.renew(key()))
        self.now = 160
        self.assertEqual(self.journal.reserve(key("b")).status, "busy")
        self.now = 219
        self.assertEqual(self.journal.reserve(key("b", "new-owner")).status, "reserved")
        self.assertEqual(self.journal.reserve(key()).status, "expired")
        self.assertFalse(self.journal.renew(key()))
        self.assertFalse(self.journal.finish(key(), {"status": "passed"}))
        self.assertTrue(self.journal.finish(key("b", "new-owner"), {"status": "failed"}))

    def test_cancellation_does_not_change_another_attempt(self):
        self.journal.reserve(key())
        self.assertFalse(self.journal.cancel(key("a", "new-owner")))
        self.assertTrue(self.journal.cancel(key()))
        self.assertEqual(self.journal.reserve(key()).status, "cancelled")
        self.assertEqual(self.journal.reserve(key("b")).status, "reserved")
        self.assertFalse(self.journal.finish(key(), {"status": "passed"}))

    def test_attempt_id_reuse_with_different_identity_is_rejected(self):
        self.journal.reserve(key())
        self.assertEqual(self.journal.reserve(key(check="server")).status, "conflict")

    def test_expiry_fences_result_but_does_not_allow_overlapping_processes(self):
        self.journal.reserve(key())
        with self.journal.execution_slot(key()) as acquired:
            self.assertTrue(acquired)
            self.now = 160
            next_worker = WorkerJournal(self.path, clock=lambda: self.now)
            self.assertEqual(next_worker.reserve(key("b")).status, "reserved")
            with next_worker.execution_slot(key("b")) as available:
                self.assertFalse(available)
            self.assertFalse(self.journal.finish(key(), {"status": "passed"}))
        with next_worker.execution_slot(key("b")) as available:
            self.assertTrue(available)
        with next_worker.execution_slot(key("b")) as duplicate:
            self.assertIsNone(duplicate)

    def test_child_holds_the_slot_after_supervisor_closes_its_descriptor(self):
        self.journal.reserve(key())
        with self.journal.execution_slot(key()) as descriptor:
            self.assertIsNotNone(descriptor)
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                stdin=subprocess.PIPE, pass_fds=(descriptor,),
            )
        try:
            self.now = 160
            self.journal.reserve(key("b"))
            with self.journal.execution_slot(key("b")) as available:
                self.assertIsNone(available)
        finally:
            child.communicate(timeout=5)
        with self.journal.execution_slot(key("b")) as available:
            self.assertIsNotNone(available)

    def test_cancel_interrupts_running_check(self):
        attempt = key()
        self.journal.reserve(attempt)
        marker = self.path / "started"
        with self.journal.execution_slot(attempt) as slot, ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(execute, "touch started; sleep 30", self.path, 5,
                                      journal=self.journal, key=attempt, slot=slot)
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(marker.exists())
            self.assertTrue(self.journal.cancel(attempt))
            with self.assertRaises(LeaseLost):
                running.result(timeout=2)

    def test_command_output_is_bounded_and_keeps_the_exit_status(self):
        attempt = key()
        self.journal.reserve(attempt)
        with self.journal.execution_slot(attempt) as slot:
            code, output, timed_out = execute(
                "printf '%04096d' 0; echo end; exit 7", self.path, 5,
                journal=self.journal, key=attempt, slot=slot,
            )
        self.assertEqual((code, timed_out), (7, False))
        self.assertEqual(len(output), 2048)
        self.assertTrue(output.endswith("end\n"))
