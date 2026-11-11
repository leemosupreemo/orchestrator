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
        self.legacy = {"state": "none", "manual_server": False}
        self.migrations = []
        self.reply = {"ok": True}
        self.reported = []
        self.readiness_sent = []
        self.opened = []
        self.connections_sent = []
        self.acks_sent = []

        def beat(machine, endpoint, running=0, update_state=None, readiness=None, connection=None, connection_ack=None,
                 update_control=0, update_ack=None):
            self.connections_sent.append(connection)
            self.acks_sent.append(connection_ack)
            self.beats.append(endpoint)
            self.readiness_sent.append(readiness)
            self.reported.append(update_state)
            reply, self.reply = self.reply, {"ok": True}
            return reply

        def start(port):
            self.gate.wait(3)
            return None, None

        self.dependencies = AgentDependencies(
            pair_start=lambda name=None: {"name": name or "Mac", "code": "12345678", "poll_secret": "private-poll", "expires_in": 600},
            pair_poll=lambda pairing: {"status": "pending"}, heartbeat=beat, open_app=lambda: self.opened.append(True),
            readiness=lambda control_dir: {"auto_login": "off", "sleep": "ok", "bogus": "ok", "power_restart": "<x>"},
            public_probe=lambda endpoint, secret, **kwargs: True, tunnel_start=start, tunnel_stop=lambda proc: None, legacy_inspect=lambda state_dir: dict(self.legacy),
            legacy_migrate=lambda state_dir, consent: self.migrations.append(consent) or {"migrated": consent, "state": "none"})
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

    def test_live_tunnel_without_public_proof_is_not_reported_connected(self):
        from unittest.mock import Mock
        proc = Mock()
        proc.poll.return_value = None
        self.dependencies.tunnel_start = lambda port: (proc, "https://unreachable.trycloudflare.com")
        self.dependencies.public_probe = lambda endpoint, secret, **kwargs: False
        self.project()
        self.start()
        self.wait(lambda: any(self.connections_sent))
        self.assertNotIn("https://unreachable.trycloudflare.com", self.beats)
        self.assertNotEqual(self.request("status")["result"]["remote_access"], "connected")

    def test_repair_command_is_acknowledged_without_stopping_local_work(self):
        from unittest.mock import Mock
        launches = []
        def connect(port):
            launches.append(port)
            proc = Mock()
            proc.poll.return_value = None
            return proc, f"https://verified-{len(launches)}.trycloudflare.com"
        self.dependencies.tunnel_start = connect
        self.dependencies.public_probe = lambda endpoint, secret, **kwargs: True
        self.project()
        self.start()
        self.wait(lambda: "https://verified-1.trycloudflare.com" in self.beats)
        done = threading.Event()
        task = self.agent.local.tasks.start(lambda: done.wait(5))
        try:
            self.reply = {"connection_command": {"id": "repair-1", "action": "repair", "created": time.time() - 1, "expires": time.time() + 60}}
            self.agent._heartbeat(account.load_machine(), self.agent._endpoint, 0, "", {})
            self.wait(lambda: "repair-1" in self.acks_sent)
            self.wait(lambda: "https://verified-2.trycloudflare.com" in self.beats)
            self.assertEqual(self.agent.local.tasks.get(task)["status"], "running")
            self.assertEqual(set(launches), {self.agent.remote.server_address[1]})
        finally:
            done.set()

    def test_expired_incoming_repair_is_not_acknowledged_or_started(self):
        self.project()
        self.start()
        self.wait(lambda: self.agent._connection_manager is not None)
        self.reply = {"connection_command": {"id": "expired-repair", "action": "repair",
                                             "created": time.time() - 120, "expires": time.time() - 60}}
        self.agent._heartbeat(account.load_machine(), "", 0, "", {})
        self.assertNotEqual(self.agent._connection_manager.ack_id, "expired-repair")

    def test_named_cloudflare_restarts_with_private_token_and_saved_url(self):
        from unittest.mock import Mock
        from orchestrator.connection_manager import ConnectionStore
        store = ConnectionStore()
        store.data.update(selected="cloudflare", providers={"cloudflare": {
            "enabled": True, "url": "https://named.example.com", "token": "private-named-token"}})
        store.save()
        proc = Mock()
        proc.poll.return_value = None
        started = []
        def connect(port, token=None):
            started.append((port, token))
            return proc, None
        self.dependencies.tunnel_start = connect
        self.dependencies.public_probe = lambda endpoint, secret, **kwargs: bool(started)
        self.project()
        self.start()
        self.wait(lambda: "https://named.example.com" in self.beats)
        self.assertEqual(started, [(self.agent.remote.server_address[1], "private-named-token")])
        self.assertNotIn("private-named-token", json.dumps(self.connections_sent))
        self.assertNotIn("private-named-token", json.dumps(self.request("status")))

    def test_local_connection_progress_uses_active_manager_and_clears_when_off(self):
        self.project()
        self.start()
        self.wait(lambda: self.agent._connection_manager is not None)
        self.assertIs(self.agent.local.connection_manager, self.agent._connection_manager)
        self.request("remote_access", {"enabled": False})
        self.assertIsNone(self.agent.local.connection_manager)
        self.assertIsNone(self.agent.remote.connection_manager)

    def test_public_probe_uses_remote_listener_secret_and_allowed_host(self):
        from unittest.mock import Mock
        proc = Mock()
        proc.poll.return_value = None
        self.dependencies.tunnel_start = lambda port: (proc, "https://verified.trycloudflare.com")
        checked = []
        def probe(endpoint, secret, **kwargs):
            checked.append((secret, self.agent.remote.allowed_hosts is None or "verified.trycloudflare.com" in self.agent.remote.allowed_hosts))
            return True
        self.dependencies.public_probe = probe
        self.project()
        self.start()
        self.wait(lambda: "https://verified.trycloudflare.com" in self.beats)
        self.assertTrue(checked)
        self.assertTrue(all(secret == self.agent.remote.token and allowed for secret, allowed in checked))

    def test_repeated_remote_enable_keeps_connection_updates_live(self):
        from unittest.mock import Mock
        proc = Mock()
        proc.poll.return_value = None
        started = threading.Event()
        def connect(port):
            started.set()
            self.gate.wait(3)
            return proc, "https://enabled.trycloudflare.com"
        self.dependencies.tunnel_start = connect
        self.project()
        self.start()
        self.wait(started.is_set)
        self.request("remote_access", {"enabled": True})
        self.gate.set()
        self.wait(lambda: "https://enabled.trycloudflare.com" in self.beats)

    def test_connection_store_failure_keeps_local_agent_available(self):
        def denied(**kwargs):
            raise OSError("connection store unavailable")
        self.dependencies.connection_factory = denied
        self.project()
        self.start()
        self.wait(lambda: self.beats)
        self.assertEqual(self.request("status")["result"]["local_interface"], "ready")
        self.assertTrue(self.thread.is_alive())

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

    def test_update_gate_refuses_new_authenticated_http_mutation(self):
        import urllib.error
        import urllib.request
        self.start()
        self.assertTrue(self.request("prepare_update")["result"]["accepted"])
        request = urllib.request.Request(self.agent.local.base_url + "api/setup/inspect", data=json.dumps({"root": str(self.base)}).encode(),
            headers={"Authorization": "Bearer " + self.agent.local.token, "Content-Type": "application/json", "X-Orchestrator-UI": "1"})
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request)
        self.assertEqual(refused.exception.code, 409)
        self.assertTrue(self.request("cancel_update")["result"]["accepted"])

    def test_background_project_work_prevents_update_preparation(self):
        self.start()
        work = threading.Event()
        task = self.agent.local.tasks.start(lambda: work.wait(3))
        try:
            self.assertFalse(self.request("prepare_update")["result"]["accepted"])
            self.assertEqual(self.agent.local.tasks.get(task)["status"], "running")
            with self.agent.local.gate.admit():
                pass
        finally:
            work.set()

    def test_running_legacy_service_pauses_remote_access_and_heartbeats(self):
        self.project()
        self.legacy = {"state": "running", "manual_server": False}
        self.start()
        time.sleep(.4)
        result = self.request("status")["result"]
        self.assertEqual(result["remote_access"], "waiting_for_legacy")
        self.assertEqual(result["legacy"], "running")
        self.assertEqual(self.beats, [])
        self.legacy = {"state": "none", "manual_server": False}
        self.assertEqual(self.request("legacy_status")["result"]["state"], "none")
        self.wait(lambda: self.beats)

    def test_hosted_update_request_reaches_status_and_progress_goes_back(self):
        self.project()
        self.reply = {"ok": True, "update_requested": True}
        self.start()
        self.wait(lambda: self.request("status")["result"]["update_request"])
        self.assertEqual(self.request("update_progress", {"state": "<b>"})["ok"], False)
        self.assertTrue(self.request("update_progress", {"state": "waiting_for_work"})["result"]["accepted"])
        self.assertFalse(self.request("status")["result"]["update_request"])
        self.wait(lambda: "waiting_for_work" in self.reported)
        self.assertEqual(self.opened, [])  # the menu app has been asking for status

    def test_update_request_starts_a_quit_menu_app_once(self):
        self.project()
        self.reply = {"ok": True, "update_requested": True}
        self.start()
        self.wait(lambda: self.agent._update_request)
        self.agent._status_at = time.monotonic() - 120  # no menu app has checked in for two minutes
        self.wait(lambda: self.opened)
        time.sleep(.3)
        self.assertEqual(self.opened, [True])

    def test_recovery_operation_requires_matching_progress_and_survives_restart(self):
        now = time.time()
        operation = dict(id='x' * 24, state='requested', action='update', created=now, expires=now + 1800)
        self.reply = {'ok': True, 'update_operation': operation}
        reports = []
        def report(machine, operation_id, state):
            reports.append((operation_id, state))
            return {'accepted': True, 'update_operation': {**operation, 'state': state}}
        self.dependencies.update_progress = report
        self.project()
        self.start()
        self.wait(lambda: self.request('status')['result'].get('update_operation'))
        self.assertFalse(self.request('update_progress', {'state': 'checking', 'operation_id': 'z' * 24})['result']['accepted'])
        self.assertFalse(self.request('update_progress', {'state': 'current'})['result']['accepted'])
        self.assertTrue(self.request('update_progress', {'state': 'checking', 'operation_id': operation['id']})['result']['accepted'])
        self.assertEqual(reports, [(operation['id'], 'checking')])
        restored = DesktopAgent(self.base / 'state', self.base / 'other', {}, self.dependencies)
        self.assertEqual(restored.status()['update_operation']['id'], operation['id'])

    def test_rejected_remote_install_claim_releases_work_gate(self):
        self.project()
        self.start()
        now = time.time()
        self.agent._recovery.accept(dict(id='x' * 24, state='requested', action='update', created=now, expires=now + 1800))
        self.dependencies.update_progress = lambda *args: {'accepted': False}
        result = self.request('prepare_update', {'operation_id': 'x' * 24})['result']
        self.assertFalse(result['accepted'])
        self.assertEqual(self.agent.local.gate.snapshot()['preparing'], '')

    def test_cancel_operation_does_not_accept_late_install_or_start_other_work(self):
        self.project()
        self.start()
        now = time.time()
        operation = dict(id='x' * 24, state='cancelling', action='cancel', created=now, expires=now + 1800)
        self.agent._recovery.accept(operation)
        result = self.request('prepare_update', {'operation_id': operation['id']})['result']
        self.assertFalse(result['accepted'])
        self.assertEqual(self.request('status')['result']['update_operation']['action'], 'cancel')

    def test_readiness_is_checked_cleaned_and_reported(self):
        self.project()
        self.start()
        self.wait(lambda: self.request("status")["result"]["readiness"])
        self.assertEqual(self.request("status")["result"]["readiness"], {"auto_login": "off", "sleep": "ok"})
        self.wait(lambda: {"auto_login": "off", "sleep": "ok"} in self.readiness_sent)
        self.assertEqual(self.request("diagnostics")["result"]["readiness"], {"auto_login": "off", "sleep": "ok"})

    def test_legacy_migration_needs_explicit_consent(self):
        self.start()
        refused = self.request("legacy_migrate")
        self.assertFalse(refused["ok"])
        self.assertEqual(self.request("legacy_migrate", {"consent": "yes"})["ok"], False)
        self.assertEqual(self.migrations, [])
        self.assertTrue(self.request("legacy_migrate", {"consent": True})["result"]["migrated"])
        self.assertEqual(self.migrations, [True])

    def test_diagnostics_exclude_paths_accounts_and_secrets(self):
        root = self.project()
        self.start()
        report = self.request("diagnostics")["result"]
        text = json.dumps(report)
        for private in (str(root), "owner@example.com", "private-machine", self.agent.local.token):
            self.assertNotIn(private, text)
        self.assertTrue(report["project_selected"])
        self.assertGreaterEqual({check["name"] for check in report["checks"]}, {"git", "cloudflared"})

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
        self.assertFalse(self.agent._endpoint)
        self.assertEqual(self.request("status")["result"]["remote_access"], "unavailable")

    def test_tunnel_reset_renews_address_without_stopping_local_work(self):
        from unittest.mock import Mock
        processes = [Mock(), Mock()]
        for proc in processes:
            proc.poll.return_value = None
        launches = []
        stopped = []
        def connect(port):
            index = len(launches)
            launches.append(port)
            return processes[index], f"https://tunnel-{index}.trycloudflare.com"
        self.dependencies.tunnel_start = connect
        self.dependencies.tunnel_stop = stopped.append
        self.project()
        self.start()
        self.wait(lambda: "https://tunnel-0.trycloudflare.com" in self.beats)
        done = threading.Event()
        task = self.agent.local.tasks.start(lambda: done.wait(5))
        try:
            self.reply = {"ok": True, "reset_tunnel": True}
            self.agent._heartbeat(account.load_machine(), self.agent._endpoint, 0, "", {})
            self.wait(lambda: "https://tunnel-1.trycloudflare.com" in self.beats)
            self.assertIn(processes[0], stopped)
            self.assertEqual(self.request("status")["result"]["remote_access"], "connected")
            self.assertEqual(self.agent.local.tasks.get(task)["status"], "running")
            self.assertEqual(len(launches), 2)
        finally:
            done.set()

    def test_tunnel_reset_does_not_turn_remote_access_back_on(self):
        self.project()
        self.agent.remote_enabled = False
        self.start()
        self.reply = {"ok": True, "reset_tunnel": True}
        self.agent._heartbeat(account.load_machine(), "", 0, "", {})
        time.sleep(.3)
        self.assertEqual(self.request("status")["result"]["remote_access"], "off")
        self.assertIsNone(self.agent._connection_manager)

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
        self.wait(lambda: proc in stopped)
        self.assertFalse(self.agent.remote.accepting_requests)
        self.assertIn(proc, stopped)
