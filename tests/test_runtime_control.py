from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from orchestrator.runtime_control import ActivityGate, BusyError, InstanceLease
from orchestrator.web.server import BackgroundTasks, SessionManager


class ActivityGateTests(unittest.TestCase):
    def test_prepare_refuses_active_work(self):
        gate = ActivityGate()
        with gate.admit():
            self.assertFalse(gate.prepare("update"))
            self.assertEqual(gate.snapshot()["active"], 1)
        with gate.admit():
            self.assertEqual(gate.snapshot()["active"], 1)

    def test_prepare_closes_admission_atomically(self):
        for _ in range(20):
            gate = ActivityGate()
            barrier = threading.Barrier(2)
            release = threading.Event()
            result = []

            def admit():
                barrier.wait()
                try:
                    with gate.admit():
                        result.append("admitted")
                        release.wait(2)
                except BusyError:
                    result.append("refused")

            worker = threading.Thread(target=admit)
            worker.start()
            barrier.wait()
            prepared = gate.prepare("update")
            release.set()
            worker.join(3)
            self.assertEqual(result, ["refused"] if prepared else ["admitted"])

    def test_cancel_reopens_admission(self):
        gate = ActivityGate()
        self.assertTrue(gate.prepare("stop"))
        with self.assertRaises(BusyError):
            with gate.admit():
                self.fail("admitted while stopping")
        gate.cancel()
        with gate.admit():
            self.assertEqual(gate.snapshot()["active"], 1)

    def test_background_work_is_counted_until_it_finishes(self):
        gate = ActivityGate()
        tasks = BackgroundTasks(gate=gate)
        release = threading.Event()
        task = tasks.start(lambda: release.wait(3))
        try:
            self.assertFalse(gate.prepare("update"))
            tasks.KEEP_SECONDS = -1
            second = tasks.start(lambda: True)
            self.assertIsNotNone(tasks.get(task))
        finally:
            release.set()
        for _ in range(100):
            if tasks.get(task)["status"] != "running" and tasks.get(second)["status"] != "running":
                break
            threading.Event().wait(.01)
        self.assertTrue(gate.prepare("update"))

    def test_session_holds_admission_until_subprocess_exits(self):
        gate = ActivityGate()
        manager = SessionManager(gate=gate)
        with tempfile.TemporaryDirectory() as folder:
            session = manager.start("test", "Test", [sys.executable, "-c", "input()"],
                                    Path(folder), dict(os.environ), None)
            try:
                self.assertFalse(gate.prepare("update"))
                session.write(b"done\n")
                for _ in range(100):
                    if not session.running:
                        break
                    threading.Event().wait(.01)
                self.assertFalse(session.running)
                self.assertTrue(gate.prepare("update"))
            finally:
                session.stop()


class InstanceLeaseTests(unittest.TestCase):
    def test_second_instance_refused_and_crashed_holder_recoverable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "instance.lock"
            child = subprocess.Popen([sys.executable, "-c",
                "from pathlib import Path; from orchestrator.runtime_control import InstanceLease; "
                "import sys,time; lease=InstanceLease.acquire(Path(sys.argv[1])); "
                "print('ready',flush=True); time.sleep(30)", str(path)],
                stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaises(BusyError):
                    InstanceLease.acquire(path)
            finally:
                child.kill()
                child.wait()
                child.stdout.close()
            lease = InstanceLease.acquire(path)
            lease.close()
            self.assertTrue(path.is_file())

    def test_symlink_lock_does_not_overwrite_target(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "private"
            target.write_text("keep")
            path = Path(folder) / "instance.lock"
            path.symlink_to(target)
            with self.assertRaises(OSError):
                InstanceLease.acquire(path)
            self.assertEqual(target.read_text(), "keep")
