import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import account
from orchestrator.desktop_agent import AgentDependencies, DesktopAgent, control_socket_path
from orchestrator.project_setup import apply_project_setup


class DesktopAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-")
        self.base = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(self.base / "state")})
        self.env.start()
        self.gate = threading.Event()
        self.beats = []

        def start(port):
            self.gate.wait(3)
            return None, None

        self.dependencies = AgentDependencies(
            pair_start=lambda name=None: {"name": name or "Mac", "code": "12345678", "poll_secret": "private-poll", "expires_in": 600},
            pair_poll=lambda pairing: {"status": "pending"}, heartbeat=lambda machine, endpoint, running=0: self.beats.append(endpoint),
            tunnel_start=start, tunnel_stop=lambda proc: None)
        self.agent = DesktopAgent(self.base / "state", self.base / "ctl", {}, self.dependencies)
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.agent.run, daemon=True)
        self.thread.start()
        self.wait(lambda: (self.base / "ctl/agent.sock").exists())
        self.wait(lambda: self.agent.local is not None and self.agent.remote is not None)

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(predicate())

    def tearDown(self):
        self.agent.stop_event.set()
        self.gate.set()
        if self.thread:
            self.thread.join(5)
            self.assertFalse(self.thread.is_alive())
        self.env.stop()
        self.tmp.cleanup()

    def request(self, command, params=None):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(3)
            client.connect(str(self.base / "ctl/agent.sock"))
            client.sendall(json.dumps({"version": 1, "request_id": "test", "command": command, "params": params or {}}).encode() + b"\n")
            with client.makefile("rb") as stream:
                return json.loads(stream.readline())

    def project(self):
        root = self.base / "project"
        root.mkdir()
        apply_project_setup(root, {"build_command": "true", "test_command": "true", "models": ["codex"]})
        account.save_machine({"machine_id": "one", "machine_secret": "private-machine", "owner_email": "owner@example.com"})
        return root

    def test_local_ready_before_slow_tunnel(self):
        self.project()
        self.start()
        result = self.request("status")["result"]
        self.assertEqual(result["local_interface"], "ready")
        self.assertNotEqual(result["remote_access"], "connected")
        self.assertNotIn("private-machine", json.dumps(result))

    def test_busy_stop_refused(self):
        self.start()
        with self.agent.local.gate.admit():
            result = self.request("stop_if_idle")["result"]
            self.assertFalse(result["accepted"])
        self.assertTrue(self.thread.is_alive())

    def test_prepared_update_can_stop_without_reopening_admissions(self):
        self.start()
        self.assertTrue(self.request("prepare_update")["result"]["accepted"])
        self.assertTrue(self.request("stop_if_idle")["result"]["accepted"])
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def test_remote_off_leaves_running_session_alive(self):
        self.project()
        self.start()
        task_done = threading.Event()
        task = self.agent.local.tasks.start(lambda: task_done.wait(3))
        try:
            self.assertTrue(self.request("remote_access", {"enabled": False})["ok"])
            self.assertEqual(self.agent.local.tasks.get(task)["status"], "running")
            self.assertEqual(self.request("status")["result"]["remote_access"], "off")
            self.assertFalse(json.loads((self.base / "state/desktop.json").read_text())["remote_enabled"])
        finally:
            task_done.set()

    def test_pair_cancel_does_not_save_secrets(self):
        self.start()
        self.assertTrue(self.request("pair_start")["ok"])
        self.assertTrue(self.request("pair_cancel")["ok"])
        self.wait(lambda: self.request("status")["result"]["pairing"]["state"] == "cancelled")
        self.assertIsNone(account.load_machine())
        self.assertNotIn("private-poll", json.dumps(self.request("status")))

    def test_successful_pairing_updates_status_without_restart(self):
        self.dependencies.pair_poll = lambda pairing: {"status": "claimed", "machine_id": "one", "machine_secret": "private-machine", "owner_email": "owner@example.com"}
        self.start()
        self.request("pair_start")
        self.wait(lambda: self.request("status")["result"]["pairing"]["state"] == "connected")
        self.assertEqual(account.load_machine()["owner_email"], "owner@example.com")

    def test_unrelated_port_listener_uses_available_port(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            self.agent.preferred_port = occupied.getsockname()[1]
            self.start()
            self.assertNotEqual(self.agent.local.server_address[1], occupied.getsockname()[1])

    def test_stale_socket_cleanup_is_owned_only(self):
        (self.base / "ctl").mkdir()
        target = self.base / "keep"
        target.write_text("keep")
        (self.base / "ctl/agent.sock").symlink_to(target)
        with self.assertRaises(ValueError):
            self.agent.run()
        self.assertEqual(target.read_text(), "keep")

    def test_socket_path_too_long_is_refused(self):
        with self.assertRaises(ValueError):
            control_socket_path(Path("/" + "x" * 120))

    def test_peer_uid_mismatch_is_refused(self):
        self.start()
        with patch("orchestrator.desktop_agent.peer_uid", return_value=os.getuid() + 1):
            result = self.request("status")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unauthorized")

    def test_unusable_live_tunnel_is_stopped_and_retried(self):
        from unittest.mock import Mock
        proc = Mock()
        proc.poll.return_value = None
        self.dependencies.tunnel_start = lambda port: (proc, None)
        stopped = []
        self.dependencies.tunnel_stop = stopped.append
        self.project()
        self.start()
        self.wait(lambda: proc in stopped)
        self.assertIn(proc, stopped)
        self.assertIsNone(self.agent._tunnel_proc)
        self.assertEqual(self.request("status")["result"]["remote_access"], "unavailable")

    def test_project_removed_while_tunnel_connects_cannot_enable_remote(self):
        from unittest.mock import Mock
        proc = Mock()
        proc.poll.return_value = None
        started = threading.Event()
        def connect(port):
            started.set()
            self.gate.wait(3)
            return proc, "https://fixture.trycloudflare.com"
        self.dependencies.tunnel_start = connect
        stopped = []
        self.dependencies.tunnel_stop = stopped.append
        root = self.project()
        self.start()
        self.wait(started.is_set)
        (root / ".orchestrator/project.json").unlink()
        self.wait(lambda: self.agent.local.root is None)
        self.gate.set()
        self.wait(lambda: not self.agent._remote_thread.is_alive())
        self.assertFalse(self.agent.remote.accepting_requests)
        self.assertIn(proc, stopped)
