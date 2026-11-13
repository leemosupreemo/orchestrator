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


class TaskProgressTests(unittest.TestCase):
    def test_a_task_reports_its_stage_and_how_long_it_has_run(self):
        tasks = BackgroundTasks()
        reached, release = threading.Event(), threading.Event()

        def work(progress):
            progress.update("Asking the model", "claude with web search")
            reached.set()
            release.wait(3)
            return "ok"

        task = tasks.start(work, progress=True)
        self.assertTrue(reached.wait(3))
        view = tasks.get(task)
        self.assertEqual((view["stage"], view["detail"]), ("Asking the model", "claude with web search"))
        self.assertGreaterEqual(view["elapsed"], 0)
        release.set()
        for _ in range(100):
            if tasks.get(task)["status"] == "done":
                break
            threading.Event().wait(0.02)
        self.assertEqual(tasks.get(task)["result"], "ok")

    def test_work_sees_a_cancel(self):
        tasks = BackgroundTasks()
        seen, started = threading.Event(), threading.Event()

        def work(progress):
            started.set()
            for _ in range(200):
                if progress.cancelled():
                    seen.set()
                    return None
                threading.Event().wait(0.01)

        task = tasks.start(work, progress=True)
        self.assertTrue(started.wait(3))
        tasks.cancel(task)
        self.assertTrue(seen.wait(3))
        self.assertEqual(tasks.get(task)["status"], "canceled")


class ResearchCallTests(unittest.TestCase):
    """research_call runs the model as a child process: it reports steps, keeps to its deadline and stops the whole group."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = type("S", (), {"child_env": lambda self: dict(os.environ)})()

    def script(self, body):
        path = self.root / "fake_chat.py"
        path.write_text(body)
        return [sys.executable, "-u", str(path)]

    def call(self, body, timeout, progress=None):
        from unittest.mock import patch
        from orchestrator.web import server as ui
        with patch.object(ui, "orchestrator_argv", return_value=self.script(body)):
            return ui.research_call(self.server, self.root, "prompt", timeout, progress)

    def progress(self):
        from orchestrator.web.server import TaskProgress
        item = {"status": "running"}
        return item, TaskProgress(item, threading.Lock())

    def test_steps_are_reported_and_the_reply_is_returned(self):
        item, progress = self.progress()
        reply, searched = self.call(
            "import sys\nsys.stdin.read()\n"
            "print('<<<ORCHESTRATOR-STAGE>>>claude-x with web search')\n"
            "print('      - Running LLM (claude-x, timeout=60s)...')\n"
            "print('        [output] Received 12 chars in 1.0s')\n"
            "print('<<<ORCHESTRATOR-WEB>>>on')\nprint('<<<ORCHESTRATOR-REPLY>>>')\nprint('{\"packages\": []}')\n", 20, progress)
        self.assertEqual(reply.strip(), '{"packages": []}')
        self.assertTrue(searched)
        self.assertEqual(item["stage"], "Reading the answer")

    def test_a_slow_model_is_stopped_at_the_deadline_with_its_grandchild(self):
        from orchestrator.web.server import UIError
        marker = self.root / "grandchild.pid"
        item, progress = self.progress()
        body = ("import subprocess, sys, time\nsys.stdin.read()\n"
                f"p = subprocess.Popen(['sleep', '60'])\nopen({str(marker)!r}, 'w').write(str(p.pid))\n"
                "print('      - Running LLM (claude-x, timeout=60s)...', flush=True)\ntime.sleep(60)\n")
        started = __import__("time").monotonic()
        with self.assertRaises(UIError) as caught:
            self.call(body, 2, progress)
        self.assertLess(__import__("time").monotonic() - started, 15)
        self.assertIn("No answer after", str(caught.exception))
        self.assertIn("claude-x is researching", str(caught.exception))
        pid = int(marker.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            threading.Event().wait(0.1)
        else:
            self.fail("the model's own process was left running")

    def test_a_cancel_stops_the_model(self):
        from orchestrator.web.server import UIError
        item, progress = self.progress()
        threading.Timer(0.5, lambda: item.update(status="canceled")).start()
        with self.assertRaises(UIError) as caught:
            self.call("import sys, time\nsys.stdin.read()\ntime.sleep(60)\n", 30, progress)
        self.assertIn("Stopped", str(caught.exception))

    def test_cli_noise_is_not_shown_as_a_step(self):
        from orchestrator.web.server import research_stage
        self.assertIsNone(research_stage("        [stderr] ERROR rmcp::transport::worker: invalid_token"))
        self.assertEqual(research_stage("⚠️  claude-x failed (rate limited). Attempting fallback..."),
                         ("Trying another model", "claude-x failed: rate limited"))
        self.assertEqual(research_stage("⚠️  claude-x timed out. Attempting fallback..."),
                         ("Trying another model", "claude-x didn't answer in time"))


class ActivityGateTests(unittest.TestCase):
    def test_exclusive_removal_requires_idle_and_blocks_new_work_until_released(self):
        gate = ActivityGate()
        normal = gate.hold()
        with self.assertRaises(BusyError):
            gate.hold_exclusive("AI tool removal")
        normal()
        release = gate.hold_exclusive("AI tool removal")
        with self.assertRaises(BusyError):
            gate.hold()
        with self.assertRaises(BusyError):
            gate.hold_exclusive("Another removal")
        release(); release()
        self.assertEqual(gate.snapshot(), {"active": 0, "preparing": ""})
        gate.hold()()

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

    def test_cli_ui_refuses_while_another_instance_holds_the_lease(self):
        import contextlib
        import io
        from unittest import mock
        from orchestrator.project_setup import apply_project_setup
        from orchestrator.web import server
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            root = base / "project"
            root.mkdir()
            with mock.patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(base / "state")}):
                apply_project_setup(root, {"build_command": "true", "test_command": "true", "models": ["codex"]})
                lease = InstanceLease.acquire(base / "state/ui-instance.lock")
                output = io.StringIO()
                try:
                    with mock.patch.object(server, "find_project_root", return_value=root), \
                            mock.patch.object(server, "UIServer", side_effect=AssertionError("must not listen")), \
                            contextlib.redirect_stdout(output):
                        self.assertEqual(server.main(["--no-open"]), 1)
                finally:
                    lease.close()
                self.assertIn("already running", output.getvalue())

    def test_symlink_lock_does_not_overwrite_target(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "private"
            target.write_text("keep")
            path = Path(folder) / "instance.lock"
            path.symlink_to(target)
            with self.assertRaises(OSError):
                InstanceLease.acquire(path)
            self.assertEqual(target.read_text(), "keep")
