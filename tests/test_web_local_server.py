from __future__ import annotations

import base64
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.web.local_server import (
    LocalCommandRunner,
    LocalHTTPServer,
    discover_local_projects,
    get_static_dir,
    make_handler,
    server_urls,
)


class LocalServerTests(unittest.TestCase):
    def test_discover_local_projects_explicit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orch-proj-") as tmp:
            p = Path(tmp)
            projects = discover_local_projects(p)
            self.assertIn(p.name, projects)
            self.assertEqual(projects[p.name], p.resolve())

    def test_local_command_runner_job_flow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orch-proj-") as tmp:
            p = Path(tmp)
            runtime = p / ".orchestrator"
            runtime.mkdir()
            (runtime / "project.json").write_text(json.dumps({"project_name": "TestProj", "base_branch": "main"}))
            (runtime / "config").mkdir()
            (runtime / "config" / "machines.json").write_text(json.dumps({"machines": [{"name": "mac1", "repo_path": str(p)}]}))
            (runtime / "config" / "settings.json").write_text(json.dumps({}))

            runner = LocalCommandRunner({"test": p})

            # 1. Store artifact
            runner.store_artifact("art-123", "spec.md", b"# Test Spec\nDo something.")
            self.assertIn("art-123", runner.artifacts)

            # 2. Add job.create command
            cmd = runner.add_command({
                "operation": "job.create",
                "project_id": "test",
                "inputs": {"job_type": "feature", "prompt": "Test plan content", "artifact_id": "art-123"},
            })
            self.assertIn("id", cmd)
            self.assertIn(cmd["status"], ["queued", "running", "succeeded"])

    def test_get_static_dir_fallback(self) -> None:
        static_dir = get_static_dir()
        self.assertTrue(static_dir.is_dir() or str(static_dir).endswith("dist"))

    def test_discovered_project_names_become_valid_ids(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orch-proj-") as tmp:
            p = Path(tmp) / "My App"
            p.mkdir()
            projects = discover_local_projects(p)
            self.assertEqual(projects["My-App"], p.resolve())


class LocalCommandRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="orch-runner-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runner = LocalCommandRunner({"test": self.root})

    def wait_for(self, record: dict, *statuses: str) -> None:
        deadline = time.monotonic() + 5
        while record["status"] not in statuses and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn(record["status"], statuses)

    def test_invalid_commands_are_rejected_before_running(self) -> None:
        for payload in (
            ["not", "an", "object"],
            {"project_id": "test", "operation": "shell.exec"},
            {"operation": "tool.audit"},
            {"project_id": "../etc", "operation": "tool.audit"},
            {"project_id": "test", "operation": "job.execute", "job_id": "j1"},
            {"project_id": "test", "operation": "tool.audit", "inputs": "x"},
            {"project_id": "test", "operation": "tool.docs", "inputs": {"evil": 1}},
            {"project_id": "test", "operation": "job.create", "inputs": {"_trusted_source_file": "/etc/passwd"}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.runner.add_command(payload)
        self.assertEqual(self.runner.commands, [])

    def test_client_cannot_set_id_status_or_requester(self) -> None:
        with patch("orchestrator.web.local_server.threading.Thread"):
            record = self.runner.add_command({"id": "chosen", "status": "succeeded", "requested_by": "x",
                                              "created_at": {}, "project_id": "test", "operation": "tool.audit"})
        self.assertNotEqual(record["id"], "chosen")
        self.assertEqual(record["status"], "queued")
        self.assertEqual(record["requested_by"], "local@localhost")

    def test_uploaded_spec_reaches_job_without_exposing_temp_path(self) -> None:
        seen: dict = {}
        def fake_create(root, options):
            seen.update(options, content=Path(options["_trusted_source_file"]).read_bytes())
            return {"job_id": "j1"}
        self.runner.store_artifact("art", "spec.md", b"# spec")
        with patch("orchestrator.web.local_server.create_job", fake_create):
            record = self.runner.add_command({"project_id": "test", "operation": "job.create",
                                              "inputs": {"prompt": "p", "artifact_id": "art"}})
            self.wait_for(record, "succeeded", "failed")
        self.assertEqual(record["status"], "succeeded", record.get("error"))
        self.assertEqual(seen["content"], b"# spec")
        self.assertNotIn("_trusted_source_file", record["inputs"])

    def test_attach_reference_receives_uploaded_file(self) -> None:
        seen: dict = {}
        def fake_action(root, job_id, operation, inputs, revision):
            seen.update(inputs, content=Path(inputs["_trusted_source_file"]).read_bytes())
            return {"job_id": job_id, "revision": 2}
        self.runner.store_artifact("art", "shot.png", b"png")
        with patch("orchestrator.web.local_server.run_job_action", fake_action):
            record = self.runner.add_command({"project_id": "test", "operation": "job.attach_reference", "job_id": "j1",
                                              "expected_revision": 1, "inputs": {"artifact_id": "art"}})
            self.wait_for(record, "succeeded", "failed")
        self.assertEqual(record["status"], "succeeded", record.get("error"))
        self.assertEqual(seen["content"], b"png")
        self.assertEqual(seen["_trusted_file_name"], "shot.png")

    def test_missing_upload_fails_clearly(self) -> None:
        record = self.runner.add_command({"project_id": "test", "operation": "job.create",
                                          "inputs": {"prompt": "p", "artifact_id": "gone"}})
        self.wait_for(record, "failed")
        self.assertIn("Upload it again", record["error"])

    def test_new_project_id_is_validated(self) -> None:
        record = self.runner.add_command({"project_id": "test", "operation": "tool.project",
                                          "inputs": {"action": "add", "project_id": "../bad", "path": str(self.root)}})
        self.wait_for(record, "failed")
        self.assertNotIn("../bad", self.runner.projects)

    def test_same_project_runs_one_at_a_time_and_queued_command_can_cancel(self) -> None:
        release = threading.Event()
        calls: list[str] = []
        def fake_tool(root, operation, inputs, progress=None):
            calls.append(operation.value)
            release.wait(5)
            return {}
        self.addCleanup(release.set)
        with patch("orchestrator.web.local_server.run_tool", fake_tool):
            running = self.runner.add_command({"project_id": "test", "operation": "tool.audit"})
            self.wait_for(running, "running")
            waiting = self.runner.add_command({"project_id": "test", "operation": "tool.docs"})
            cancelled = self.runner.add_command({"project_id": "test", "operation": "tool.fleet"})
            time.sleep(0.05)
            self.assertEqual(waiting["status"], "queued")
            self.assertEqual(self.runner.cancel_command(cancelled["id"])["status"], "cancelled")
            with self.assertRaisesRegex(ValueError, "Only queued"):
                self.runner.cancel_command(running["id"])
            release.set()
            self.wait_for(running, "succeeded")
            self.wait_for(waiting, "succeeded")
            time.sleep(0.05)
        self.assertEqual(sorted(calls), ["tool.audit", "tool.docs"])
        self.assertEqual(cancelled["status"], "cancelled")

    def test_cancel_unknown_command(self) -> None:
        with self.assertRaises(LookupError):
            self.runner.cancel_command("missing")


class LocalServerSecurityTests(unittest.TestCase):
    TOKEN = "test-token"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="orch-web-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "secret.txt").write_text("top secret")
        self.static = self.root / "a" / "dist"
        self.static.mkdir(parents=True)
        (self.static / "index.html").write_text("<html>app</html>")
        self.handler = make_handler({"test": self.root}, self.static, self.TOKEN)
        self.httpd = LocalHTTPServer(("127.0.0.1", 0), self.handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, path: str, method: str = "GET", headers: dict[str, str] | None = None, body: str | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8", "replace"), response

    def command_count(self) -> int:
        return len(self.handler.runner.commands)

    def test_static_paths_cannot_escape_dist(self) -> None:
        for path in ("/../../secret.txt", "/../../../" + self.root.name + "/secret.txt", "/a/../../../secret.txt"):
            status, body, _ = self.request(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn("top secret", body)

    def test_static_app_still_served_with_spa_fallback(self) -> None:
        for path in ("/", "/index.html", "/jobs/123"):
            status, body, _ = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertIn("app", body)

    def test_loopback_browser_needs_no_token(self) -> None:
        status, body, response = self.request("/api/worker")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["local_mode"])
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_same_origin_post_is_allowed(self) -> None:
        origin = f"http://127.0.0.1:{self.port}"
        with patch("orchestrator.web.local_server.threading.Thread"):
            status, _, _ = self.request("/api/commands", "POST", {"Origin": origin}, json.dumps({"project_id": "test", "operation": "project.refresh"}))
        self.assertEqual(status, 201)

    def test_other_website_cannot_post_commands(self) -> None:
        for origin in ("https://evil.example", "null"):
            status, _, _ = self.request("/api/commands", "POST", {"Origin": origin, "Content-Type": "text/plain"}, "{}")
            self.assertEqual(status, 403, origin)
        self.assertEqual(self.command_count(), 0)

    def test_valid_token_does_not_bypass_origin_check(self) -> None:
        status, _, _ = self.request("/api/worker", headers={"Origin": "https://evil.example", "Authorization": f"Bearer {self.TOKEN}"})
        self.assertEqual(status, 403)

    def test_dns_rebinding_needs_token(self) -> None:
        host = f"evil.example:{self.port}"
        status, _, _ = self.request("/api/worker", headers={"Host": host, "Origin": f"http://{host}"})
        self.assertEqual(status, 401)

    def test_remote_client_requires_valid_token(self) -> None:
        self.handler._client_is_loopback = lambda _self: False
        host = {"Host": f"192.168.1.5:{self.port}"}
        for headers in (host, {**host, "Authorization": "Bearer wrong"}, {**host, "Authorization": "Bearer caf\u00e9"}):
            self.assertEqual(self.request("/api/worker", headers=headers)[0], 401)
        self.assertEqual(self.request("/api/commands", "POST", host, "{}")[0], 401)
        self.assertEqual(self.command_count(), 0)
        self.assertEqual(self.request("/api/worker", headers={**host, "Authorization": f"Bearer {self.TOKEN}"})[0], 200)

    def test_remote_client_can_load_app_to_present_token(self) -> None:
        self.handler._client_is_loopback = lambda _self: False
        self.assertEqual(self.request("/", headers={"Host": f"192.168.1.5:{self.port}"})[0], 200)

    def test_token_only_appears_in_network_urls(self) -> None:
        self.assertEqual(server_urls("127.0.0.1", 4040, "abc"), ("http://127.0.0.1:4040", None, None))
        with patch("orchestrator.web.local_server.get_lan_ip", return_value="192.168.1.5"), patch(
            "orchestrator.web.local_server.get_tailscale_ip", return_value="100.1.2.3"
        ):
            local, network, tailscale = server_urls("0.0.0.0", 4040, "abc")
        self.assertEqual(local, "http://127.0.0.1:4040")
        self.assertEqual(network, "http://192.168.1.5:4040/?token=abc")
        self.assertEqual(tailscale, "http://100.1.2.3:4040/?token=abc")
        self.assertEqual(server_urls("100.1.2.3", 4040, "abc")[1], "http://100.1.2.3:4040/?token=abc")

    def test_invalid_command_returns_400_with_reason(self) -> None:
        status, body, _ = self.request("/api/commands", "POST", body=json.dumps({"project_id": "test", "operation": "shell.exec"}))
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "Unknown operation")
        self.assertEqual(self.command_count(), 0)

    def test_cancel_endpoint_status_codes(self) -> None:
        self.assertEqual(self.request("/api/commands/missing/cancel", "POST")[0], 404)
        with patch("orchestrator.web.local_server.threading.Thread"):
            record = self.handler.runner.add_command({"project_id": "test", "operation": "tool.audit"})
        path = f"/api/commands/{record['id']}/cancel"
        self.assertEqual(self.request(path, "POST", {"Origin": "https://evil.example"})[0], 403)
        status, body, _ = self.request(path, "POST")
        self.assertEqual((status, json.loads(body)["status"]), (200, "cancelled"))
        self.assertEqual(self.request(path, "POST")[0], 409)

    def test_artifact_upload_is_validated(self) -> None:
        url = "/api/projects/test/artifacts"
        good = base64.b64encode(b"hello").decode()
        self.assertEqual(self.request(url, "POST", body=json.dumps({"id": "ok-1", "name": "a.md", "data": good}))[0], 201)
        self.assertEqual(self.handler.runner.artifacts["ok-1"], ("a.md", b"hello"))
        self.assertEqual(self.request(url, "POST", body=json.dumps({"id": "../x", "data": good}))[0], 400)
        self.assertEqual(self.request(url, "POST", body=json.dumps({"id": "bad-b64", "data": "@@@"}))[0], 400)
        huge = base64.b64encode(b"x" * (10 * 1024 * 1024 + 1)).decode()
        self.assertEqual(self.request(url, "POST", body=json.dumps({"id": "huge", "data": huge}))[0], 400)
        self.assertEqual(self.request(url, "POST", {"Content-Length": str(17 * 1024 * 1024)})[0], 413)

    def test_tailscale_lookup_does_not_crash(self) -> None:
        from orchestrator.web.local_server import get_tailscale_ip
        with patch("orchestrator.web.local_server.shutil.which", return_value=None):
            self.assertIsNone(get_tailscale_ip())
