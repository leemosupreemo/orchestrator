import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.web.server import UIServer


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(self.base / "state")})
        self.env.start()
        self.server = UIServer(("127.0.0.1", 0), None, token="owner")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)
        self.env.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, token="owner", origin=None):
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-Orchestrator-UI": "1"}
        if origin:
            headers["Origin"] = origin
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def test_fresh_install_serves_setup_without_dummy_project(self):
        status, result = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(result["needs_project"])
        status, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIsNone(state["project"])
        self.assertEqual(list(self.base.rglob("project.json")), [])

    def test_project_routes_need_project_before_dereference(self):
        for path in ("/api/jobs", "/api/config", "/api/file?path=config/settings.json"):
            status, result = self.request("GET", path)
            self.assertEqual(status, 409)
            self.assertEqual(result["code"], "project_required")

    def test_bootstrap_refuses_unauthorized_and_members(self):
        status, _ = self.request("GET", "/api/bootstrap", token="wrong")
        self.assertEqual(status, 401)
        token = self.server.sign_ins.create("member@example.com")
        status, _ = self.request("GET", "/api/bootstrap", token=token)
        self.assertEqual(status, 401)

    def test_apply_enters_project_mode_and_removed_project_returns_to_setup(self):
        root = self.base / "project"
        root.mkdir()
        status, result = self.request("POST", "/api/setup/apply", {
            "root": str(root), "values": {"project_name": "Example", "build_command": "true", "test_command": "true", "models": ["codex"]}})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        status, state = self.request("GET", "/api/state")
        self.assertEqual(state["project"]["name"], "Example")
        (root / ".orchestrator/project.json").unlink()
        status, result = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(result["needs_project"])

    def test_hostile_origin_cannot_apply_settings(self):
        root = self.base / "project"
        root.mkdir()
        status, _ = self.request("POST", "/api/setup/apply", {"root": str(root), "values": {}}, origin="https://evil.example")
        self.assertEqual(status, 403)
        self.assertFalse((root / ".orchestrator").exists())
