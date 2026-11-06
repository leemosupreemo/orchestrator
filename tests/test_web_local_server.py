from __future__ import annotations

import base64
import http.client
import json
import tempfile
import threading
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
                "inputs": {
                    "title": "Local Test Job",
                    "plan": "Test plan content",
                    "artifact_id": "art-123",
                }
            })
            self.assertIn("id", cmd)
            self.assertIn(cmd["status"], ["queued", "running", "succeeded"])

    def test_get_static_dir_fallback(self) -> None:
        static_dir = get_static_dir()
        self.assertTrue(static_dir.is_dir() or str(static_dir).endswith("dist"))


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
            status, _, _ = self.request("/api/commands", "POST", {"Origin": origin}, json.dumps({"operation": "project.refresh"}))
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

    def test_tailscale_lookup_does_not_crash(self) -> None:
        from orchestrator.web.local_server import get_tailscale_ip
        with patch("orchestrator.web.local_server.shutil.which", return_value=None):
            self.assertIsNone(get_tailscale_ip())
