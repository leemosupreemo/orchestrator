from __future__ import annotations

import base64
import http.client
import shutil
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from orchestrator import prd as prd_mod
from orchestrator.web import server as ui  # noqa: E402

UI_HEADERS = {"Content-Type": "application/json", "X-Orchestrator-UI": "1"}
# Page scripts escape through html.js (loaded first on the page), so node loads it first too.
HTML_JS = str(PACKAGE_ROOT / "orchestrator" / "web" / "static" / "html.js")


def make_project(root: Path) -> None:
    runtime = root / ".orchestrator"
    (runtime / "jobs").mkdir(parents=True)
    (runtime / "project.json").write_text(json.dumps({"project_name": "Demo", "scheme": "Demo"}))
    (runtime / "jobs" / "20260922-bug-1.json").write_text(json.dumps({
        "title": "Lobby seat stays empty", "type": "bug", "status": "debugging", "branch": "ai/issue-1",
        "tasks": ["repro", "fix"], "completed_tasks": ["0"],
    }))
    out = runtime / "output" / "20260922-bug-1"
    out.mkdir(parents=True)
    (out / "brief.md").write_text("# Brief\n")


def isolate_state(test: unittest.TestCase) -> None:
    """Point ~/.orchestrator at a throwaway folder for this test."""
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # a reader thread may still be writing its log
    test.addCleanup(tmp.cleanup)
    env = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": tmp.name})
    env.start()
    test.addCleanup(env.stop)


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "project"
        make_project(self.root)
        self.env = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(base / "state")})
        self.env.start()
        self.server = ui.UIServer(("127.0.0.1", 0), self.root, token="test-token")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.sessions.stop_all()
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, auth=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if auth:
            hdrs["Authorization"] = "Bearer test-token"
        if host is not None:
            hdrs["Host"] = host
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=hdrs)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        try:
            data = json.loads(raw) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = raw
        return res, data


class AuthTests(ServerTestCase):
    def test_api_requires_token(self):
        res, _ = self.request("GET", "/api/state", auth=False)
        self.assertEqual(res.status, 401)
        res, data = self.request("GET", "/api/state")
        self.assertEqual(res.status, 200)
        self.assertEqual(data["project"]["name"], "Demo")

    def test_changes_without_the_page_header_are_refused_even_with_no_body(self):
        # These routes never read a body, so a check made only when reading one used to miss them.
        for method, path in (("POST", "/api/product/draft"), ("POST", "/api/product/dismiss"), ("POST", "/api/features/propose"),
                             ("POST", "/api/auth/logout"), ("DELETE", "/api/jobs/20260922-bug-1")):
            with self.subTest(path=path):
                res, data = self.request(method, path)
                self.assertEqual(res.status, 403)
                self.assertEqual(data["error"], "Missing UI headers")
        self.assertTrue((ui.jobs_dir(self.root) / "20260922-bug-1.json").exists())  # nothing was deleted
        res, _ = self.request("GET", "/api/state")
        self.assertEqual(res.status, 200)  # reading needs no header

    def test_query_token_authenticates(self):
        res, data = self.request("GET", "/api/state?token=test-token", auth=False)
        self.assertEqual(res.status, 200)
        self.assertEqual(data["project"]["name"], "Demo")

    def test_options_cors_preflight(self):
        res, _ = self.request("OPTIONS", "/api/state", auth=False, headers={"Origin": ui.HOSTED_APP_URL})
        self.assertEqual(res.status, 204)
        self.assertEqual(res.getheader("Access-Control-Allow-Origin"), ui.HOSTED_APP_URL)
        self.assertIn("Authorization", res.getheader("Access-Control-Allow-Headers", ""))

    def test_cors_refuses_other_sites(self):
        res, _ = self.request("OPTIONS", "/api/state", auth=False, headers={"Origin": "https://example.com"})
        self.assertEqual(res.status, 403)
        self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))
        res, _ = self.request("GET", "/api/state", headers={"Origin": "https://example.com"})
        self.assertIsNone(res.getheader("Access-Control-Allow-Origin"))

    def test_cors_allows_the_servers_own_name_and_this_computer(self):
        for origin, host in ((f"http://127.0.0.1:{self.port}", None), ("http://localhost:5000", None),
                             ("https://mac.example.ts.net", "mac.example.ts.net")):
            res, _ = self.request("GET", "/api/state", headers={"Origin": origin}, host=host)
            self.assertEqual(res.getheader("Access-Control-Allow-Origin"), origin, origin)

    def test_extra_origins_come_from_the_environment(self):
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_ORIGINS": "https://app.example.com/, https://b.example.com"}):
            server = ui.UIServer(("127.0.0.1", 0), self.root, token="t2")
        self.addCleanup(server.server_close)
        self.assertIn("https://app.example.com", server.allowed_origins)
        self.assertIn("https://b.example.com", server.allowed_origins)


class SignInTests(ServerTestCase):
    def sign_in(self, email="tester@example.com", source="git"):
        with patch("orchestrator.web.server.verify_firebase_id_token", return_value={"email": email}), \
             patch("orchestrator.web.server.allowed_auth_sources", return_value={email: source}):
            res, data = self.request("POST", "/api/auth", body={"id_token": "id"}, headers=UI_HEADERS, auth=False)
        self.assertEqual(res.status, 200, data)
        return data["token"]

    def as_user(self, token, method, path, body=None, allowed=("tester@example.com",), sources=None):
        headers = {"Authorization": f"Bearer {token}", **(UI_HEADERS if body is not None else {})}
        auth_sources = sources or {e: "git" for e in allowed}
        with patch("orchestrator.web.server.allowed_auth_sources", return_value=auth_sources):
            self.server.forget_allowed_emails()
            return self.request(method, path, body=body, headers=headers, auth=False)

    def test_sign_in_gets_its_own_token_never_the_servers(self):
        token = self.sign_in()
        self.assertNotEqual(token, "test-token")
        res, data = self.as_user(token, "GET", "/api/state")
        self.assertEqual(res.status, 200)
        self.assertEqual(data["token"], token)  # state echoes the caller's credential, not the access token
        self.assertEqual(data["you"], {"kind": "sign_in", "email": "tester@example.com", "role": "owner"})
        self.assertNotIn("test-token", json.dumps(data))

    def test_owner_state_reports_the_owner(self):
        _, data = self.request("GET", "/api/state")
        self.assertEqual(data["you"]["kind"], "owner")

    def test_member_state_reports_member_and_keeps_safe_project_settings(self):
        token = self.sign_in(source="settings")
        member = {"tester@example.com": "settings"}
        res, state = self.as_user(token, "GET", "/api/state", sources=member)
        self.assertEqual(res.status, 200)
        self.assertEqual(state["you"]["role"], "member")

        res, _ = self.as_user(token, "POST", "/api/config/setup-seen", body={}, sources=member)
        self.assertEqual(res.status, 200)
        self.assertTrue(ui.read_settings(self.root)["setup_seen"])

    def test_member_cannot_change_secrets_or_start_owner_only_tools(self):
        token = self.sign_in(source="settings")
        member = {"tester@example.com": "settings"}
        res, data = self.as_user(token, "POST", "/api/config/keys", body={"id": "openai", "value": "secret"}, sources=member)
        self.assertEqual(res.status, 403)
        self.assertIn("Only the owner", data["error"])

        res, data = self.as_user(token, "POST", "/api/runs", body={"action": "console"}, sources=member)
        self.assertEqual(res.status, 403)
        self.assertIn("Only the owner", data["error"])
        denied = [entry for entry in self.server.audit.recent() if entry["event"] == "denied"]
        self.assertEqual([entry["what"] for entry in denied], ["use interactive console", "change keys settings"])

    def test_member_cannot_change_connected_app_credentials_or_create_projects(self):
        token = self.sign_in(source="settings")
        member = {"tester@example.com": "settings"}
        for path, body in (
            ("/api/integrations/jira/connect", {"values": {}}),
            ("/api/integrations/jira/options", {"options": {}}),
            ("/api/integrations/jira/disconnect", {}),
            ("/api/new-project/draft", {"name": "Private idea"}),
        ):
            with self.subTest(path=path):
                res, data = self.as_user(token, "POST", path, body=body, sources=member)
                self.assertEqual(res.status, 403)
                self.assertIn("Only the owner", data["error"])

    def test_member_cannot_scan_or_add_project_folders(self):
        token = self.sign_in(source="settings")
        member = {"tester@example.com": "settings"}
        for path, body in (("/api/projects/scan", {}), ("/api/projects/add", {"root": str(self.root)})):
            with self.subTest(path=path):
                res, data = self.as_user(token, "POST", path, body=body, sources=member)
                self.assertEqual(res.status, 403)
                self.assertIn("Only the owner", data["error"])

    def test_member_sees_only_their_access_record_and_not_the_audit_log(self):
        ui.config_update(self.root, "allowed-email", {"op": "add", "email": "other@example.com"})
        self.server.audit.record("settings_changed", "access token", part="keys")
        token = self.sign_in(source="settings")
        res, config = self.as_user(token, "GET", "/api/config", sources={"tester@example.com": "settings", "other@example.com": "settings"})
        self.assertEqual(res.status, 200)
        self.assertEqual(config["viewer"], {"role": "member", "email": "tester@example.com"})
        self.assertEqual(config["allowed_emails"], [{"email": "tester@example.com", "source": "settings"}])
        self.assertNotIn("audit", config)

    def test_member_config_omits_owner_only_machine_and_credential_metadata(self):
        token = self.sign_in(source="settings")
        res, config = self.as_user(
            token, "GET", "/api/config", sources={"tester@example.com": "settings"},
        )
        self.assertEqual(res.status, 200)
        for field in ("keys", "ollama_host", "email", "analytics", "webhook", "instructions", "machines", "firebase", "role_prompts", "menus"):
            self.assertNotIn(field, config)
        for field in ("base_branch", "branches", "archived", "docs", "models", "xcode_cloud"):
            self.assertIn(field, config)

    def test_member_cannot_read_runtime_configuration_files_but_can_read_job_output(self):
        ui.write_settings(self.root, {"openai_api_key": "do-not-leak"})
        output = self.root / ".orchestrator" / "output" / "report.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("safe report")
        token = self.sign_in(source="settings")
        member = {"tester@example.com": "settings"}

        res, data = self.as_user(token, "GET", "/api/file?path=config/settings.json", sources=member)
        self.assertEqual(res.status, 403)
        self.assertNotIn("do-not-leak", json.dumps(data))
        res, data = self.as_user(token, "GET", "/api/file?path=output/report.txt", sources=member)
        self.assertEqual(res.status, 200)
        self.assertEqual(data["text"], "safe report")

    def test_member_cannot_see_or_control_an_owner_only_run(self):
        session = self.server.sessions.start(
            "console", "Interactive console", [sys.executable, "-c", "import time; time.sleep(30)"],
            self.root, dict(os.environ), None,
        )
        with session._cond:
            session._buffer.extend(b"PRIVATE owner prompt?")
            session.last_output = time.time() - ui.IDLE_PROMPT_SECONDS - 1
        token = self.sign_in(source="settings")
        member = {"tester@example.com": "settings"}

        _, state = self.as_user(token, "GET", "/api/state", sources=member)
        self.assertNotIn(session.id, [run["id"] for run in state["runs"]])
        self.assertNotIn("PRIVATE owner prompt", json.dumps(state["inbox"]))
        _, inbox = self.as_user(token, "GET", "/api/inbox", sources=member)
        self.assertNotIn("PRIVATE owner prompt", json.dumps(inbox))
        _, runs = self.as_user(token, "GET", "/api/runs", sources=member)
        self.assertNotIn(session.id, [run["id"] for run in runs["runs"]])
        for method, endpoint, body in (
            ("GET", "output?offset=0", None),
            ("POST", "input", {"data": "echo arbitrary-command\r"}),
            ("POST", "stop", {}),
        ):
            with self.subTest(endpoint=endpoint):
                res, data = self.as_user(token, method, f"/api/runs/{session.id}/{endpoint}", body=body, sources=member)
                self.assertEqual(res.status, 403)
                self.assertIn("Only the owner", data["error"])

    def test_owner_config_includes_recent_audit_events(self):
        self.server.audit.record("settings_changed", "access token", part="keys")
        res, config = self.request("GET", "/api/config")
        self.assertEqual(res.status, 200)
        self.assertEqual(config["viewer"]["role"], "owner")
        self.assertEqual(config["audit"][0]["event"], "settings_changed")

    def test_state_reports_the_runner_version(self):
        _, data = self.request("GET", "/api/state")
        self.assertEqual(data["runner"]["api_version"], ui.account.API_VERSION)
        self.assertTrue(data["runner"]["version"])

    def test_state_identifies_owner_only_actions_for_the_ui(self):
        _, data = self.request("GET", "/api/state")
        self.assertTrue(data["actions"]["console"]["owner_only"])
        self.assertTrue(data["actions"]["wizard"]["owner_only"])
        self.assertTrue(data["actions"]["logs_setup"]["owner_only"])
        self.assertFalse(data["actions"]["test"]["owner_only"])

    def test_sign_ins_survive_a_restart_and_only_hashes_are_stored(self):
        token = self.sign_in()
        stored = (Path(os.environ["ORCHESTRATOR_USER_STATE_DIR"]) / "ui_sign_ins.json").read_text()
        self.assertNotIn(token, stored)
        again = ui.SignInStore(self.server.sign_ins.path)
        self.assertEqual(again.lookup(token)["email"], "tester@example.com")

    def test_logout_ends_the_sign_in(self):
        token = self.sign_in()
        res, _ = self.as_user(token, "POST", "/api/auth/logout", body={})
        self.assertEqual(res.status, 200)
        res, _ = self.as_user(token, "GET", "/api/state")
        self.assertEqual(res.status, 401)

    def test_owner_logout_leaves_the_access_token_working(self):
        self.request("POST", "/api/auth/logout", body={}, headers=UI_HEADERS)
        res, _ = self.request("GET", "/api/state")
        self.assertEqual(res.status, 200)

    def test_sign_ins_are_listed_and_can_be_revoked(self):
        mine, other = self.sign_in(), self.sign_in()
        _, config = self.as_user(mine, "GET", "/api/config")
        self.assertEqual(len(config["sign_ins"]), 2)
        self.assertEqual(sum(s["current"] for s in config["sign_ins"]), 1)
        other_id = next(s["id"] for s in config["sign_ins"] if not s["current"])
        res, data = self.as_user(mine, "POST", "/api/sign-ins/revoke", body={"id": other_id})
        self.assertEqual(res.status, 200)
        self.assertEqual(len(data["sign_ins"]), 1)
        self.assertEqual(self.as_user(other, "GET", "/api/state")[0].status, 401)
        self.assertEqual(self.as_user(mine, "GET", "/api/state")[0].status, 200)
        res, _ = self.as_user(mine, "POST", "/api/sign-ins/revoke", body={"id": other_id})
        self.assertEqual(res.status, 404)

    def test_a_sign_in_stops_working_once_its_email_is_no_longer_allowed(self):
        token = self.sign_in()
        res, _ = self.as_user(token, "GET", "/api/state", allowed=("someone-else@example.com",))
        self.assertEqual(res.status, 401)

    def test_removing_an_allowed_email_ends_its_sign_ins(self):
        ui.config_update(self.root, "allowed-email", {"op": "add", "email": "tester@example.com"})
        token = self.sign_in()
        res, _ = self.request("POST", "/api/config/allowed-email", body={"op": "remove", "email": "tester@example.com"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertIsNone(self.server.sign_ins.lookup(token))

    def test_expired_sign_ins_are_refused(self):
        store = ui.SignInStore(Path(self.tmp.name) / "s.json", ttl=-1)
        self.assertIsNone(store.lookup(store.create("tester@example.com")))

    def test_url_token_is_exchanged_for_http_only_cookie(self):
        res, _ = self.request("GET", "/?token=test-token", auth=False)
        self.assertEqual(res.status, 303)
        cookie = res.getheader("Set-Cookie")
        self.assertIn("orchestrator_ui=test-token", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        res, _ = self.request("GET", "/api/jobs", auth=False, headers={"Cookie": "orchestrator_ui=test-token"})
        self.assertEqual(res.status, 200)

    def test_wrong_url_token_rejected(self):
        res, _ = self.request("GET", "/?token=nope", auth=False)
        self.assertEqual(res.status, 401)

    def test_foreign_host_header_rejected(self):
        res, _ = self.request("GET", "/api/state", host="evil.example:80")
        self.assertEqual(res.status, 403)

    def test_post_requires_ui_header(self):
        res, _ = self.request("POST", "/api/runs", body={"action": "check"},
                              headers={"Content-Type": "application/json"})
        self.assertEqual(res.status, 403)

    def test_static_index_served_with_csp(self):
        res, body = self.request("GET", "/", auth=False)
        self.assertEqual(res.status, 200)
        self.assertIn(b"Orchestrator", body)
        self.assertIn("frame-ancestors 'none'", res.getheader("Content-Security-Policy"))
        self.assertIn("img-src 'self' data: blob:", res.getheader("Content-Security-Policy"))  # fetched design images are shown from blob: URLs

    def test_html_helper_is_served_and_loaded_before_the_scripts_that_use_it(self):
        res, body = self.request("GET", "/html.js", auth=False)
        self.assertEqual(res.status, 200)
        html = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "index.html").read_text()
        for script in ("project-picker.js", "configuration.js", "feature-plan.js", "account.js", "app.js"):
            self.assertLess(html.index('src="html.js"'), html.index(f'src="{script}"'), script)

    def test_static_traversal_blocked(self):
        res, _ = self.request("GET", "/../server.py", auth=False)
        self.assertEqual(res.status, 404)


class ProjectPickerUiTests(unittest.TestCase):
    def test_discovered_project_add_request_tracks_without_switching(self):
        picker = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "project-picker.js"
        script = """
let request = null;
try {
  require(process.argv[1]);
  request = globalThis.ProjectPicker.addRequest({root: '/code/demo', name: 'Demo'});
} catch (error) {}
process.stdout.write(JSON.stringify(request));
"""
        result = subprocess.run(
            ["node", "--require", HTML_JS, "-e", script, str(picker)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout), {
            "path": "projects/add",
            "options": {
                "method": "POST",
                "body": {"root": "/code/demo", "name": "Demo", "active": False},
            },
        })

    def test_project_picker_excludes_tracked_projects_from_discovery_rows(self):
        picker = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "project-picker.js"
        script = """
require(process.argv[1]);
const rows = globalThis.ProjectPicker.availableProjects(
  [
    {root: '/code/alpha', name: 'Alpha'},
    {root: '/code/beta', name: 'Beta', configured: true, type: 'orchestrator'}
  ],
  [{root: '/code/alpha'}]
);
process.stdout.write(JSON.stringify(rows));
"""
        result = subprocess.run(
            ["node", "--require", HTML_JS, "-e", script, str(picker)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            {"root": "/code/beta", "name": "Beta", "configured": True, "type": "orchestrator"},
        ])

    def test_project_picker_renders_compact_list_rows_with_add_actions(self):
        picker = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "project-picker.js"
        script = """
require(process.argv[1]);
const html = globalThis.ProjectPicker.renderRows([
  {root: '/code/demo', name: 'Demo <App>', configured: false, type: 'swift'}
]);
process.stdout.write(JSON.stringify({
  listRow: html.includes('class="project-discovery-row"'),
  addAction: html.includes('data-add-project-path="/code/demo"') && html.includes('>Add</button>'),
  escapedName: html.includes('Demo &lt;App&gt;'),
  cardLayout: html.includes('project-card')
}));
"""
        result = subprocess.run(
            ["node", "--require", HTML_JS, "-e", script, str(picker)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "listRow": True,
            "addAction": True,
            "escapedName": True,
            "cardLayout": False,
        })

    def test_project_picker_serializes_add_requests(self):
        picker = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "project-picker.js"
        script = """
require(process.argv[1]);
let active = 0;
let maxActive = 0;
const completed = [];
const enqueue = globalThis.ProjectPicker.createAddQueue(async (name) => {
  active += 1;
  maxActive = Math.max(maxActive, active);
  await new Promise((resolve) => setTimeout(resolve, name === 'first' ? 20 : 1));
  completed.push(name);
  active -= 1;
});
Promise.all([enqueue('first'), enqueue('second')]).then(() => {
  process.stdout.write(JSON.stringify({maxActive, completed}));
});
"""
        result = subprocess.run(
            ["node", "--require", HTML_JS, "-e", script, str(picker)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "maxActive": 1,
            "completed": ["first", "second"],
        })

    def test_project_select_change_navigates_to_home(self):
        app_js = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        start = app_js.index('matches("#project-select")')
        end = app_js.index("showSigningIn", start)
        handler_code = app_js[start:end]
        self.assertIn('location.hash = "#/";', handler_code)


class FakeTunnelProcess:
    def __init__(self):
        self.exit_code = None
        self.terminated = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = -15

    def wait(self, timeout=None):
        return self.exit_code

    def kill(self):
        self.exit_code = -9


class ProjectTitleTests(unittest.TestCase):
    def test_home_names_the_project_without_a_second_switcher_and_branch_is_labelled(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        js, css, html = (static / "app.js").read_text(), (static / "style.css").read_text(), (static / "index.html").read_text()
        home = js[js.index("  return {\n    title: p.name,"):][:400]
        self.assertNotIn("select", home)  # switching projects lives in the menu's picker and on Projects
        self.assertNotIn("project-select-inline", js + css)
        status = js[js.index("function statusLine"):][:1600]
        self.assertIn('<span class="status-branch"><span class="label">Branch</span>', status)
        header = js[js.index("function setHeader"):][:600]
        self.assertIn("heading.dataset.title = title", header)  # the tab title stays the name
        self.assertIn('$("#page-title")?.dataset.title', js)

    def test_the_phone_header_centres_the_project_name_with_the_logo_and_no_wordmark(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        js, css, html = (static / "app.js").read_text(), (static / "style.css").read_text(), (static / "index.html").read_text()
        header = html[html.index('<header class="mobile-header">'):html.index("</header>")]
        self.assertIn('<span class="mobile-title" id="mobile-title"></span>', header)
        self.assertIn('role="img" aria-label="Orchestrator"', header)  # the logo keeps the name for screen readers
        self.assertNotIn("<span>Orchestrator</span>", html)
        self.assertIn('mobileTitle.textContent = state.project?.name', js)
        self.assertIn("grid-template-columns: 1fr minmax(0, auto) 1fr;", css)  # truly centred
        self.assertIn('body[data-page="home"] #page-title { display: none; }', css)  # not said twice on phones


class ProductStripTests(unittest.TestCase):
    def test_the_card_itself_opens_the_product_requirements(self):
        js = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        strip = js[js.index("function productStripHtml"):][:1400]
        self.assertIn('<h2><a class="card-title-link" href="#/product">Product</a></h2>', strip)
        self.assertIn('<a class="product-pitch" href="#/product"', strip)
        self.assertNotIn(">Open</a>", strip)  # no separate Open link

    def test_product_strip_and_start_here_include_model_dropdown(self):
        js = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn('id="prd-home-model-select"', js)
        self.assertIn('id="prd-model-select"', js)
        self.assertIn('prd-model-picker', js)
        self.assertIn('api("product/model"', js)


class UiShutdownTests(unittest.TestCase):
    """Stopping `orchestrator ui` the way a service manager or `kill` does must close its tunnel, as Ctrl-C does."""

    SCRIPT = """
import sys
from pathlib import Path
from orchestrator.web import server

class FakeKeeper:
    def __init__(self, *args, **kwargs): pass
    def start(self): return "https://fake.trycloudflare.com"
    def stop(self): Path(sys.argv[2]).write_text("stopped")

server.TunnelKeeper = FakeKeeper
server.find_project_root = lambda *a, **k: Path(sys.argv[1])
raise SystemExit(server.main(["--no-open", "--tunnel", "--port", "0"]))
"""

    def run_until_ready(self, folder):
        from orchestrator.project_setup import apply_project_setup
        base = Path(folder).resolve()
        root = base / "project"
        root.mkdir()
        env = {**os.environ, "ORCHESTRATOR_USER_STATE_DIR": str(base / "state"), "PYTHONPATH": str(PACKAGE_ROOT)}
        with patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(base / "state")}):
            apply_project_setup(root, {"build_command": "true", "test_command": "true", "models": ["codex"]})
        marker = base / "tunnel-stopped"
        proc = subprocess.Popen([sys.executable, "-c", self.SCRIPT, str(root), str(marker)], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            if "Ctrl-C to stop" in line:
                return proc, marker
        proc.wait(5)
        self.fail("orchestrator ui didn't start")

    def test_terminate_and_hangup_close_the_tunnel(self):
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=sig.name), tempfile.TemporaryDirectory() as folder:
                proc, marker = self.run_until_ready(folder)
                proc.send_signal(sig)
                self.assertEqual(proc.wait(10), 0)
                proc.stdout.close()
                self.assertEqual(marker.read_text(), "stopped")


class TunnelKeeperTests(unittest.TestCase):
    def setUp(self):
        isolate_state(self)  # cloudflared's output is copied to the state folder's logs

    def keeper(self, results, **kwargs):
        """A keeper whose starter hands out `results` in order: (process, address) pairs."""
        self.calls = []
        queue = list(results)

        def starter(port, token=None):
            self.calls.append((port, token))
            return queue.pop(0) if queue else (None, None)

        self.changes = []
        keeper = ui.TunnelKeeper(8765, starter=starter, on_url=self.changes.append, interval=0.01, max_wait=0.05, **kwargs)
        self.addCleanup(keeper.stop)
        return keeper

    def wait_for(self, condition, seconds=3):
        deadline = time.time() + seconds
        while time.time() < deadline and not condition():
            time.sleep(0.01)
        return condition()

    def test_a_healthy_tunnel_is_left_alone(self):
        first = FakeTunnelProcess()
        keeper = self.keeper([(first, "https://one.trycloudflare.com")])
        self.assertEqual(keeper.start(), "https://one.trycloudflare.com")
        time.sleep(0.15)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.changes, [])

    def test_a_tunnel_that_exits_is_started_again_and_the_new_address_reported(self):
        first, second = FakeTunnelProcess(), FakeTunnelProcess()
        keeper = self.keeper([(first, "https://one.trycloudflare.com"), (second, "https://two.trycloudflare.com")])
        keeper.start()
        first.exit_code = 1
        self.assertTrue(self.wait_for(lambda: self.changes))
        self.assertEqual(self.changes, ["https://two.trycloudflare.com"])
        self.assertEqual(keeper.url, "https://two.trycloudflare.com")

    def test_a_fixed_address_is_kept_when_the_tunnel_restarts(self):
        first, second = FakeTunnelProcess(), FakeTunnelProcess()
        keeper = self.keeper([(first, "https://random.trycloudflare.com"), (second, "https://other.trycloudflare.com")],
                             fixed_url="https://mac.example.com/")
        self.assertEqual(keeper.start(), "https://mac.example.com")
        first.exit_code = 1
        self.assertTrue(self.wait_for(lambda: len(self.calls) == 2))
        time.sleep(0.05)
        self.assertEqual(self.changes, [])  # same address as before: nothing new to report
        self.assertEqual(keeper.url, "https://mac.example.com")

    def test_a_named_tunnel_needs_no_address_to_be_healthy(self):
        proc = FakeTunnelProcess()
        keeper = self.keeper([(proc, None)], fixed_url="")
        keeper.token = "named-token"
        keeper.start()
        time.sleep(0.1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0], (8765, "named-token"))

    def test_it_keeps_trying_when_cloudflared_cannot_start(self):
        late = FakeTunnelProcess()
        keeper = self.keeper([(None, None), (None, None), (late, "https://late.trycloudflare.com")])
        self.assertEqual(keeper.start(), "")
        self.assertTrue(self.wait_for(lambda: self.changes))
        self.assertEqual(self.changes, ["https://late.trycloudflare.com"])

    def test_a_quick_tunnel_that_never_prints_an_address_is_restarted(self):
        stuck, good = FakeTunnelProcess(), FakeTunnelProcess()
        keeper = self.keeper([(stuck, None), (good, "https://ok.trycloudflare.com")])
        keeper.start()
        self.assertTrue(self.wait_for(lambda: self.changes))
        self.assertTrue(stuck.terminated)

    def test_stop_ends_the_process_and_the_watching(self):
        proc = FakeTunnelProcess()
        keeper = self.keeper([(proc, "https://one.trycloudflare.com")])
        keeper.start()
        keeper.stop()
        self.assertTrue(proc.terminated)
        count = len(self.calls)
        time.sleep(0.1)
        self.assertEqual(len(self.calls), count)

    def test_cloudflared_output_is_read_so_it_can_never_fill_the_pipe(self):
        # A child that writes far more than a pipe holds (about 64 KB) finishes only if somebody reads what it writes.
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdout.write('x' * 1_000_000)"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        ui._drain(proc)
        self.assertEqual(proc.wait(timeout=10), 0)


class AccountUiTests(unittest.TestCase):
    def test_mac_setup_requires_a_complete_https_release_and_preserves_pairing(self):
        result = self.run_account_script("""
const A = globalThis.Account;
const release = {version: '0.2.0', build: 2, commit: 'a'.repeat(40), minimum_macos: '13.0',
 arm64: {url: 'https://example.com/arm.dmg', sha256: 'a'.repeat(64)},
 x86_64: {url: 'https://example.com/intel.dmg', sha256: 'b'.repeat(64)}};
process.stdout.write(JSON.stringify({
 good: A.macRelease(release),
 bad: [null, {}, {...release, x86_64: null}, {...release, arm64: {...release.arm64, url: 'javascript:alert(1)'}},
 {...release, arm64: {...release.arm64, url: 'https://user:password@example.com/app.dmg'}},
 {...release, arm64: {...release.arm64, sha256: '0'.repeat(64)}}].map(A.macRelease),
 ready: A.renderMacSetup(release), missing: A.renderMacSetup(null)
}));""")
        self.assertEqual(result['good']['version'], '0.2.0')
        self.assertEqual(result['bad'], [None] * 6)
        self.assertIn('Apple Silicon', result['ready'])
        self.assertIn('https://example.com/intel.dmg', result['ready'])
        self.assertIn('Applications', result['ready'])
        self.assertNotIn('download href=', result['missing'])
        self.assertIn('not available yet', result['missing'])
        self.assertIn('data-account-form="code"', result['missing'])
        self.assertIn('<details', result['missing'])

    def run_account_script(self, source: str, preload: str = ""):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        result = subprocess.run(
            ["node", "--require", HTML_JS, "-e", f"{preload}\nrequire(process.argv[1]);\nrequire(process.argv[2]);\n{source}",
             str(static / "account.js"), str(static / "configuration.js")],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_account_mode_is_the_hosted_app_without_an_explicit_backend(self):
        result = self.run_account_script("""
const A = globalThis.Account;
const hosted = A.HOSTED_ORIGINS[0];
process.stdout.write(JSON.stringify([
  A.active({origin: hosted, search: ""}),
  A.active({origin: hosted, search: "?backend=https://x.example.com"}),
  A.active({origin: "http://127.0.0.1:8765", search: ""}),
  A.active({origin: "https://abc.trycloudflare.com", search: ""}),
]));""")
        self.assertEqual(result, [True, False, False, False])

    def test_codes_from_links_are_kept_for_the_visit(self):
        result = self.run_account_script("""
const A = globalThis.Account;
const store = {}; const storage = {setItem: (k, v) => { store[k] = v; }, getItem: (k) => store[k] ?? null, removeItem: (k) => { delete store[k]; }};
const fromLink = A.pendingCode(storage, "#/connect?code=abcd-2345");
const later = A.pendingCode(storage, "#/");
A.clearPendingCode(storage);
const cleared = A.pendingCode(storage, "#/");
const tooShort = A.pendingCode(storage, "#/connect?code=AB");
process.stdout.write(JSON.stringify({fromLink, later, cleared, tooShort, formatted: A.formatCode("ABCD2345")}));""")
        self.assertEqual(result, {"fromLink": "ABCD2345", "later": "ABCD2345", "cleared": "", "tooShort": "", "formatted": "ABCD-2345"})

    def test_pick_machine_opens_the_last_used_or_the_only_reachable_one(self):
        result = self.run_account_script("""
const A = globalThis.Account;
const a = {id: "a", reachable: true}, b = {id: "b", reachable: true}, off = {id: "c", reachable: false};
process.stdout.write(JSON.stringify([
  A.pickMachine([a, b], "b")?.id ?? null,
  A.pickMachine([a, b], null)?.id ?? null,
  A.pickMachine([a, off], null)?.id ?? null,
  A.pickMachine([off], "c")?.id ?? null,
  A.pickMachine([], null)?.id ?? null,
]));""")
        self.assertEqual(result, ["b", None, "a", None, None])

    def test_machine_list_and_add_steps(self):
        result = self.run_account_script("""
const A = globalThis.Account;
const machines = [
  {id: "m1", name: "<Studio>", os: "macOS 26", version: "0.1.0", reachable: true, online: true, last_seen: 100},
  {id: "m2", name: "Laptop", reachable: false, online: true, last_seen: 100},
  {id: "m3", name: "Old", reachable: false, online: false, last_seen: 0},
];
process.stdout.write(JSON.stringify({
  list: A.renderMachines(machines, {email: "a@x.com", now: 160}),
  empty: A.renderMachines([], {email: "a@x.com", message: "Run `orchestrator connect`", idea: {pitch: "A word game", name: "", existing: false}}),
  pair: A.renderPair({name: "<Mac>", os: "macOS", version: "1"}, "ABCD2345", {email: "a@x.com"}),
}));""")
        self.assertEqual(result["list"].count('data-account-action="open"'), 1)  # only the reachable one opens
        self.assertEqual(result["list"].count('data-account-action="remove"'), 3)
        self.assertIn("&lt;Studio&gt;", result["list"])
        self.assertNotIn("<Studio>", result["list"])
        self.assertIn("not reachable from the web", result["list"])
        self.assertIn("<code>orchestrator ui --tunnel</code>", result["list"])
        self.assertIn("Set up where it runs", result["empty"])  # once they've said what they're building
        self.assertIn("pipx install", result["empty"])
        self.assertIn('data-account-form="code"', result["empty"])
        self.assertIn("<code>orchestrator connect</code>", result["empty"])  # the message's `code` is shown as code
        self.assertIn("ABCD-2345", result["pair"])
        self.assertIn("&lt;Mac&gt;", result["pair"])
        self.assertIn('data-account-action="claim"', result["pair"])

    def test_an_unreachable_computer_names_its_host_in_a_wrapping_address(self):
        endpoint = "https://very-long-generated-name-for-a-quick-tunnel.trycloudflare.com/x?token=secret"
        result = self.run_account_script(f"""
const html = globalThis.Account.renderMachines([], {{troubleshoot: {{error: "Load failed", endpoint: "{endpoint}", machine: {{id: "m1", name: "Mac"}}}}}});
process.stdout.write(JSON.stringify({{html}}));""")
        self.assertIn('<code class="account-address" title="', result["html"])
        self.assertIn(">very-long-generated-name-for-a-quick-tunnel.trycloudflare.com</code>", result["html"])  # the host, not the token
        css = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()
        self.assertIn(".account-card code.account-address { white-space: normal; overflow-wrap: anywhere; }", css)

    def test_your_computers_is_listed_only_on_the_hosted_app(self):
        source = """
const ids = globalThis.ConfigurationPages.groups().flatMap((g) => g.entries.map((e) => e.id));
process.stdout.write(JSON.stringify({ids, menu: globalThis.ConfigurationPages.renderMenu().includes("#/computers")}));"""
        local = self.run_account_script(source)
        self.assertNotIn("computers", local["ids"])
        self.assertFalse(local["menu"])
        hosted = self.run_account_script(source, preload='globalThis.location = {origin: "https://swift-orch-web-20260923.web.app", search: ""};')
        self.assertIn("computers", hosted["ids"])
        self.assertTrue(hosted["menu"])

    def test_the_hosted_app_can_be_installed_and_receive_push(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        html = (static / "index.html").read_text()
        manifest = json.loads((static / "manifest.webmanifest").read_text())
        self.assertIn('rel="manifest"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn("firebase-messaging-compat.js", html)
        self.assertEqual(manifest["display"], "standalone")  # iOS only allows web push for home-screen apps
        for icon in manifest["icons"]:
            self.assertTrue((static / icon["src"]).is_file(), icon["src"])
        worker = (static / "firebase-messaging-sw.js").read_text()
        self.assertIn("onBackgroundMessage", worker)
        self.assertIn("tag: data.tag", worker)  # replaces the open tab's alert for the same event
        self.assertLess(worker.index('addEventListener("notificationclick"'), worker.index("importScripts("))
        js = (static / "app.js").read_text()
        self.assertIn('serviceWorker.register("firebase-messaging-sw.js")', js)
        self.assertIn("await disablePush()", js[js.index("async function lockSession"):][:300])  # signing out stops alerts
        follow = js[js.index("function followAlertLink"):][:900]
        self.assertIn('params.get("machine")', follow)
        self.assertLess(js.index("function followAlertLink"), js.index("refreshState().then(() => { if (!signingIn) route(); });"))

    def test_computers_behind_the_page_are_flagged(self):
        result = self.run_account_script("""
const A = globalThis.Account;
const mk = (api, reachable = true) => ({id: "m", name: "Mac", version: "0.0.9", api_version: api, reachable, online: true, last_seen: 100});
process.stdout.write(JSON.stringify({
  old: A.renderMachines([mk(1)], {email: "a@x.com", now: 160}),
  none: A.renderMachines([mk(undefined)], {email: "a@x.com", now: 160}),
  current: A.renderMachines([mk(A.REQUIRED_RUNNER_API)], {email: "a@x.com", now: 160}),
  newer: A.outdated(A.REQUIRED_RUNNER_API + 1),
}));""")
        self.assertIn("older Orchestrator (0.0.9)", result["old"])
        self.assertIn("orchestrator update", result["old"])
        self.assertIn("older Orchestrator", result["none"])  # a computer that never reported one is older than any that does
        self.assertNotIn("older Orchestrator", result["current"])
        self.assertFalse(result["newer"])

    def test_app_installs_offer_update_and_show_its_progress(self):
        result = self.run_account_script("""
const A = globalThis.Account;
const mk = (extra) => ({id: "m1", name: "Closet mini", version: "0.2.0", api_version: A.REQUIRED_RUNNER_API, reachable: true,
                        online: true, last_seen: 100, ...extra});
process.stdout.write(JSON.stringify({
  app: A.renderMachines([mk({updatable: true})], {email: "a@x.com", now: 160}),
  pipx: A.renderMachines([mk({updatable: false})], {email: "a@x.com", now: 160}),
  waiting: A.renderMachines([mk({updatable: true, update_state: "waiting_for_work"})], {email: "a@x.com", now: 160}),
  offline: A.renderMachines([mk({updatable: true, online: false, reachable: false})], {email: "a@x.com", now: 1000}),
  oldApp: A.renderMachines([mk({updatable: true, api_version: 1})], {email: "a@x.com", now: 160}),
  hostile: A.renderMachines([mk({updatable: true, update_state: "<img src=x>"})], {email: "a@x.com", now: 160}),
  added: A.renderMachines([mk({added_with_command: true})], {email: "a@x.com", now: 160}),
}));""")
        self.assertIn('data-account-action="update"', result["app"])
        self.assertNotIn('data-account-action="update"', result["pipx"])
        self.assertNotIn('data-account-action="update"', result["waiting"])
        self.assertIn("installs when the current work finishes", result["waiting"])
        self.assertNotIn('data-account-action="update"', result["offline"])
        self.assertIn("Choose Update", result["oldApp"])
        self.assertNotIn("<img", result["hostile"])
        self.assertIn("Added with a command", result["added"])

    def test_readiness_warnings_show_their_fixes_and_match_the_mac(self):
        result = self.run_account_script("""
const A = globalThis.Account;
const mk = (readiness) => ({id: "m1", name: "Closet mini", api_version: A.REQUIRED_RUNNER_API, reachable: true, online: true,
                            last_seen: 100, readiness});
process.stdout.write(JSON.stringify({
  two: A.renderMachines([mk({auto_login: "off", power_restart: "off", sleep: "ok"})], {email: "a@x.com", now: 160}),
  none: A.renderMachines([mk({auto_login: "ok", power_restart: "unknown"})], {email: "a@x.com", now: 160}),
  keys: Object.keys(A.READINESS_FIXES),
}));""")
        self.assertIn("2 settings need changing", result["two"])
        self.assertIn("<code>sudo pmset -a autorestart 1</code>", result["two"])
        self.assertNotIn("account-readiness", result["none"])
        from orchestrator import mac_readiness
        self.assertEqual(sorted(result["keys"]), sorted(f"{check}:{state}" for check, state in mac_readiness.FIXES))

    def test_add_a_mac_stays_hidden_until_releases_are_published_with_it(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        result = self.run_account_script("""
const A = globalThis.Account;
process.stdout.write(JSON.stringify({
  released: A.MAC_APP_RELEASED,
  hidden: A.renderMachines([], {email: "a@x.com", now: 1}),
  shown: A.renderMachines([], {email: "a@x.com", now: 1, macAppReleased: true}),
}));""")
        published = (static / "install-mac.sh").exists() and (static / "releases" / "macos.json").exists()
        self.assertEqual(result["released"], published)  # turn Add a Mac on only together with what it points at
        self.assertNotIn('data-account-form="enroll"', result["hidden"])
        self.assertIn('data-account-form="enroll"', result["shown"])

    def test_enroll_command_quotes_what_people_type(self):
        result = self.run_account_script("""
const A = globalThis.Account;
process.stdout.write(JSON.stringify({
  url: A.enrollCommand("enroll_abc-_1", "git@github.com:me/app.git", "https://host.example"),
  folder: A.enrollCommand("enroll_abc", "~/My Projects/it's here; rm -rf ~", "https://host.example"),
  empty: A.enrollCommand("enroll_abc", "", "https://host.example"),
  live: A.renderEnroll("cmd <b>", 125),
  expired: A.renderEnroll("cmd", 0),
}));""")
        self.assertEqual(result["url"], "curl -fsSL https://host.example/install-mac.sh | sh -s -- --token enroll_abc-_1 --project git@github.com:me/app.git")
        self.assertTrue(result["folder"].endswith("""--project '~/My Projects/it'"'"'s here; rm -rf ~'"""))
        self.assertIn("--project PROJECT", result["empty"])
        self.assertIn("3 more minutes", result["live"])
        self.assertIn("cmd &lt;b&gt;", result["live"])
        self.assertIn("expired", result["expired"])
        self.assertNotIn("cmd", result["expired"])

    def test_provider_sign_in_is_offered_only_where_firebase_allows_it(self):
        result = self.run_account_script("""
const A = globalThis.Account;
const at = (href) => { const u = new URL(href); return {origin: u.origin, hostname: u.hostname}; };
process.stdout.write(JSON.stringify([
  "https://swift-orch-web-20260923.web.app/", "https://swift-orch-web-20260923.firebaseapp.com/#/",
  "http://localhost:8765/", "http://127.0.0.1:8765/", "https://some-words-here.trycloudflare.com/",
].map((href) => A.providerSignInWorks(at(href)))));""")
        self.assertEqual(result, [True, True, True, False, False])
        js = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        gate = js[js.index("function showSignInGate"):][:4000]
        self.assertIn("Account.providerSignInWorks()", gate)
        self.assertIn("Sign in on the Orchestrator site", gate)
        self.assertIn('class="sso-buttons" ${providers ? "" : "hidden"}', gate)
        self.assertIn('err.code === "auth/unauthorized-domain"', js)  # explained, not Firebase's raw message

    def test_first_screen_asks_what_to_build_before_any_setup(self):
        result = self.run_account_script("""
const A = globalThis.Account;
const store = () => { const m = {}; return {getItem: (k) => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }, removeItem: (k) => { delete m[k]; }}; };
const s = store();
const saved = A.saveIdea({pitch: "  A word game <script>x</script>  ", name: "Word Duel"}, s);
process.stdout.write(JSON.stringify({
  ask: A.renderMachines([], {email: "a@x.com", now: 1, idea: null}),
  recap: A.renderMachines([], {email: "a@x.com", now: 1, idea: saved}),
  existing: A.renderMachines([], {email: "a@x.com", now: 1, idea: {existing: true, pitch: "", name: ""}}),
  withComputers: A.renderMachines([{id: "m", name: "Mac", online: true, reachable: true, last_seen: 1, api_version: A.REQUIRED_RUNNER_API}], {email: "a@x.com", now: 1}),
  roundTrip: A.pendingIdea(s),
  empty: A.saveIdea({pitch: "   "}, store()),
  route: A.ideaRoute(saved),
  existingRoute: A.ideaRoute({existing: true}),
  cleared: (A.clearIdea(s), A.pendingIdea(s)),
}));""")
        self.assertIn('data-account-form="idea"', result["ask"])
        self.assertIn("What do you want to build?", result["ask"])
        self.assertNotIn("pipx install", result["ask"])  # no setup chores before the idea
        self.assertIn("A word game &lt;script&gt;", result["recap"])
        self.assertIn("pipx install", result["recap"])  # then the computer steps
        self.assertNotIn('data-account-form="idea"', result["recap"])
        self.assertIn("bringing code you already have", result["existing"])
        self.assertNotIn('data-account-form="idea"', result["withComputers"])
        self.assertEqual(result["roundTrip"], {"pitch": "A word game <script>x</script>", "name": "Word Duel", "existing": False})
        self.assertIsNone(result["empty"])
        self.assertEqual(result["route"], "#/new-project?pitch=A+word+game+%3Cscript%3Ex%3C%2Fscript%3E&name=Word+Duel")
        self.assertEqual(result["existingRoute"], "#/projects")
        self.assertIsNone(result["cleared"])

    def test_new_project_form_starts_from_a_pending_idea_without_overwriting_a_draft(self):
        js = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        page = js[js.index('pages["new-project"]'):][:5000]
        self.assertIn('query.get("pitch")', page)
        self.assertIn("Account.pendingIdea()", page)
        arrive = page[page.index("if (!data.draft) {"):page.index("} else if (current === ideaAnswers.pitch)")]
        self.assertIn("await npSave(draft)", arrive)  # saved as the draft on arrival, not only on Next
        self.assertIn("forgetIdea()", arrive)
        self.assertIn("You were already starting", page)  # a different draft in progress: ask, don't drop the idea
        choice = page[page.index("const wireIdeaChoice"):][:600]
        self.assertLess(choice.index("new-project/discard"), choice.index("npSave({ answers: ideaAnswers"))
        self.assertEqual(js.count("wireIdeaChoice();"), 2)  # describe and where steps both show the choice
        enter = js[js.index("async function enterMachine"):][:1500]
        self.assertLess(enter.index("Account.ideaRoute(idea)"), enter.index("await unlockWith"))  # routed before the page renders

    def test_projectless_setup_leads_with_the_idea(self):
        script = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "setup.js"
        source = """
globalThis.window = globalThis;
require(process.argv[1]);
const esc = (v) => String(v).replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const api = async (path) => path === "bootstrap" ? {selected_root: ""} : {stack: "Python", inferred: {project_name: "App"}};
(async () => {
  const fresh = await window.DesktopSetup.render({api, esc, root: "", onReady: () => {}});
  const folder = await window.DesktopSetup.render({api, esc, root: "/tmp/app", onReady: () => {}});
  process.stdout.write(JSON.stringify({fresh: fresh.title + fresh.html, folder: folder.title + folder.html}));
})();"""
        result = subprocess.run(["node", "--require", HTML_JS, "-e", source, str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        pages = json.loads(result.stdout)
        self.assertIn('id="desktop-idea"', pages["fresh"])
        self.assertLess(pages["fresh"].index('id="desktop-idea"'), pages["fresh"].index('id="desktop-folder"'))
        self.assertNotIn('id="desktop-idea"', pages["folder"])
        self.assertIn("Set up your project", pages["folder"])

    def test_the_pages_required_version_is_what_this_runner_provides(self):
        # Bump both together: the page asks for exactly what the runner in this repo reports.
        js = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "account.js").read_text()
        import re
        required = int(re.search(r"REQUIRED_RUNNER_API = (\d+);", js).group(1))
        self.assertEqual(required, ui.account.API_VERSION)

    def test_app_loads_account_before_app(self):
        html = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "index.html").read_text()
        self.assertLess(html.index('src="account.js"'), html.index('src="app.js"'))
        hosted = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "account.js").read_text()
        for origin in ui.HOSTED_ORIGINS:  # the page and the server agree on where the hosted app lives
            self.assertIn(origin, hosted)


class ConfigurationPagesUiTests(unittest.TestCase):
    def run_configuration_script(self, source: str):
        helper = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "configuration.js"
        result = subprocess.run(
            ["node", "--require", HTML_JS, "-e", f"require(process.argv[1]);\n{source}", str(helper)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_add_an_ai_page_shows_steps_only_for_what_is_missing(self):
        result = self.run_configuration_script("""
const P = globalThis.ConfigurationPages;
const ai = {checked: "October 2026", any_ready: false, plugins: [{name: "Playwright MCP", why: "Drive <web> apps"}], providers: [
  {id: "opencode", name: "OpenCode", cost_label: "Free", what: "Free models", install: "curl -fsSL https://opencode.ai/install | bash",
   install_alt: "brew install opencode", sign_in: "Nothing to sign in to", link: "https://opencode.ai/docs", installed: false, ready: false},
  {id: "codex", name: "Codex <OpenAI>", cost_label: "Subscription", what: "Plans", install: "x", install_alt: "", sign_in: "Run codex once",
   link: "https://developers.openai.com/codex/cli", key: "openai_api_key", installed: true, ready: false},
  {id: "claude", name: "Claude Code", cost_label: "Subscription", what: "Plans", install: "y", install_alt: "", sign_in: "z",
   link: "https://docs.claude.com", installed: true, ready: true},
]};
const page = P.render("ai", {ai, keys: []});
const member = P.render("ai", {ai});  // members' config carries no keys
process.stdout.write(JSON.stringify({title: page.title, sub: page.sub, html: page.html, member: member.html, entry: P.resolve("ai", "member")?.route || null}));
""")
        html = result["html"]
        self.assertEqual(result["title"], "Add an AI")
        self.assertIn("Free options are first", result["sub"])
        self.assertIn('data-setup-copy="curl -fsSL https://opencode.ai/install | bash"', html)
        self.assertLess(html.index('data-provider="opencode"'), html.index('data-provider="codex"'))
        codex = html[html.index('data-provider="codex"'):html.index('data-provider="claude"')]
        self.assertIn("Installed, not signed in", codex)
        self.assertNotIn("Install it", codex)  # already installed: only the sign-in step
        self.assertIn("Add an API key instead", codex)
        self.assertNotIn("Add an API key instead", result["member"])
        self.assertNotIn('id="api-keys"', result["member"])
        claude = html[html.index('data-provider="claude"'):]
        self.assertIn("Ready", claude)
        self.assertNotIn("Run", claude.split("</section>")[0])
        self.assertIn("Codex &lt;OpenAI&gt;", html)
        self.assertIn("Drive &lt;web&gt; apps", html)
        self.assertEqual(result["entry"], "#/config/ai")  # members can use it too

    def test_build_the_plan_views(self):
        helper = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "feature-plan.js"
        source = """
const P = globalThis.FeaturePlan;
const form = P.renderStartForm([
  {id: "chat", name: "Chat", status: "planned", jobs_total: 0, layer: 1},
  {id: "play", name: "Play <a>", status: "planned", jobs_total: 0, layer: 0},
  {id: "old", name: "Old", status: "in-progress", jobs_total: 2, layer: 0}]);
const plan = {auto_approve: false, paused: false, stopped: false, finished: false, rows: [
  {feature: "play", name: "Play", state: "done", job: "j1", detail: ""},
  {feature: "chat", name: "Chat <b>", state: "waiting", job: null, detail: "Waiting on Play"}]};
process.stdout.write(JSON.stringify({
  form, chosen: P.chosenFeatures({f_play: "on", f_chat: "on", auto_approve: "on"}),
  run: P.renderRun(plan), paused: P.renderRun({...plan, paused: true}), finished: P.renderRun({...plan, finished: true}),
  none: P.renderRun(null)}));"""
        result = subprocess.run(["node", "--require", HTML_JS, "-e", f"require(process.argv[1]);\n{source}", str(helper)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertLess(out["form"].index("Play &lt;a&gt;"), out["form"].index(">Chat<"))  # build order
        self.assertIn('name="f_play"\n        checked', out["form"])
        self.assertIn("has 2 jobs", out["form"])
        self.assertNotIn('name="f_old"\n        checked', out["form"])
        self.assertEqual(out["chosen"], ["play", "chat"])
        self.assertIn("1/2", out["run"])
        self.assertIn('href="#/jobs/j1"', out["run"])
        self.assertIn("Chat &lt;b&gt;", out["run"])
        self.assertIn('data-plan-action="pause"', out["run"])
        self.assertIn('data-plan-action="resume"', out["paused"])
        self.assertIn("The plan is built", out["finished"])
        self.assertEqual(out["none"], "")

    def test_feature_proposal_view_groups_by_build_order_and_escapes(self):
        helper = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "feature-plan.js"
        source = """
const P = globalThis.FeaturePlan;
const html = P.renderProposal({warnings: ["\\"<b>\\" overlaps"], features: [
  {name: "Play a round", summary: "One game", stories: ["As a player I can play."], depends_on: [], paths: ["app/game/"], layer: 0},
  {name: "Chat <script>", summary: "", stories: [], depends_on: ["Play a round"], paths: [], layer: 1}]});
process.stdout.write(JSON.stringify({html, stories: P.renderStories(["As <a>"]), none: P.renderStories([])}));"""
        result = subprocess.run(["node", "--require", HTML_JS, "-e", f"require(process.argv[1]);\n{source}", str(helper)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        html = out["html"]
        self.assertLess(html.index("Start with these"), html.index("Then these (layer 2)"))
        self.assertLess(html.index("Play a round"), html.index("Chat &lt;script&gt;"))
        self.assertIn('value="Play a round" checked', html)
        self.assertIn("&quot;&lt;b&gt;&quot; overlaps", html)
        self.assertNotIn("<script>", html)
        self.assertIn("Builds on Play a round", html)
        self.assertIn("As &lt;a&gt;", out["stories"])
        self.assertEqual(out["none"], "")

    def test_registry_exposes_phase_one_native_routes(self):
        entries = self.run_configuration_script("""
const pages = globalThis.ConfigurationPages;
const ids = ["ai", "base-branch", "projects", "email"];
process.stdout.write(JSON.stringify(Object.fromEntries(ids.map((id) => [id, pages.find(id)]))));
""")
        self.assertEqual({key: entry["route"] for key, entry in entries.items()}, {
            "ai": "#/config/ai", "base-branch": "#/config/base-branch", "projects": "#/projects", "email": "#/config/email"})
        self.assertTrue(all(entry["enabled"] for entry in entries.values()))

    def test_settings_that_moved_are_gone_from_configuration(self):
        found = self.run_configuration_script("""
const ids = ["api-keys", "archived-jobs", "documentation", "chat", "audit", "self-tests", "setup-wizard", "ui-review"];
process.stdout.write(JSON.stringify(ids.filter((id) => globalThis.ConfigurationPages.find(id))));
""")
        self.assertEqual(found, [])  # each has one home now; routes.js sends their old addresses there

    def test_registry_exposes_all_configuration_entries_enabled(self):
        result = self.run_configuration_script("""
const ids = ['models', 'ai-instructions', 'fleet', 'firebase', 'xcode-cloud', 'updates'];
const active = Object.fromEntries(ids.map((id) => [id, globalThis.ConfigurationPages.resolve(id)]));
process.stdout.write(JSON.stringify({active, menu: globalThis.ConfigurationPages.renderMenu()}));
""")
        for entry_id, entry in result["active"].items():
            self.assertTrue(entry["enabled"])
            self.assertEqual(entry["route"], f"#/config/{entry_id}")
            self.assertIsNone(entry["status"])
        self.assertEqual(result["menu"].count(" disabled"), 0)
        self.assertNotIn('data-config-route="null"', result["menu"])
        self.assertNotIn("Coming next", result["menu"])

    def test_member_registry_omits_owner_only_configuration(self):
        result = self.run_configuration_script("""
const pages = globalThis.ConfigurationPages;
const ids = pages.groups('member').flatMap((group) => group.entries.map((entry) => entry.id));
process.stdout.write(JSON.stringify({ids, menu: pages.renderMenu('member'), access: pages.resolve('access', 'member')}));
""")
        for entry_id in ("ai-instructions", "fleet", "access", "email", "firebase", "updates"):
            self.assertNotIn(entry_id, result["ids"])
        for entry_id in ("ai", "models", "base-branch", "xcode-cloud"):
            self.assertIn(entry_id, result["ids"])
        self.assertNotIn("Who can sign in", result["menu"])
        self.assertIsNone(result["access"])

    def test_member_configuration_chooser_uses_the_filtered_registry(self):
        html = self.run_configuration_script("""
const page = globalThis.ConfigurationPages.render(undefined, {viewer: {role: 'member'}});
process.stdout.write(JSON.stringify(page.html));
""")
        self.assertNotIn("Instructions for AI helpers", html)
        self.assertNotIn("Who can sign in", html)
        self.assertIn("Base branch", html)
        self.assertIn("Add an AI", html)

    def test_role_ui_hides_owner_controls_and_can_restore_them(self):
        result = self.run_configuration_script("""
const elements = [{hidden: false}, {hidden: false}];
const document = {querySelectorAll: (selector) => selector === '[data-owner-only]' ? elements : []};
globalThis.ConfigurationPages.applyRole('member', document);
const hidden = elements.map((element) => element.hidden);
globalThis.ConfigurationPages.applyRole('owner', document);
process.stdout.write(JSON.stringify({hidden, restored: elements.map((element) => element.hidden)}));
""")
        self.assertEqual(result["hidden"], [True, True])
        self.assertEqual(result["restored"], [False, False])

    def test_action_permissions_follow_server_metadata(self):
        result = self.run_configuration_script("""
const state = {you: {role: 'member'}, actions: {wizard: {owner_only: true}, test: {owner_only: false}}};
const allowed = globalThis.ConfigurationPages.canRunAction;
process.stdout.write(JSON.stringify({wizard: allowed('wizard', state), test: allowed('test', state), unknown: allowed('unknown', state)}));
""")
        self.assertEqual(result, {"wizard": False, "test": True, "unknown": True})

    def test_access_page_lists_emails_and_sign_ins(self):
        html = self.run_configuration_script("""
const page = globalThis.ConfigurationPages.render('access', {
  allowed_emails: [{email: 'owner@x.com', source: 'git'}, {email: 'friend@x.com', source: 'settings'}],
  sign_ins: [{id: 'a1', email: 'friend@x.com', created: 1, last_seen: 2, expires: 3, current: false},
             {id: 'b2', email: 'owner@x.com', created: 1, last_seen: 2, expires: 3, current: true}],
});
process.stdout.write(JSON.stringify(page.html));
""")
        self.assertIn("from git config", html)
        self.assertEqual(html.count('data-config-action="access-remove"'), 1)  # only emails added here can be removed here
        self.assertIn('data-email="friend@x.com"', html)
        self.assertEqual(html.count('data-config-action="access-revoke"'), 2)
        self.assertEqual(html.count("data-current"), 1)
        self.assertIn("This browser", html)
        empty = self.run_configuration_script("process.stdout.write(JSON.stringify(globalThis.ConfigurationPages.render('access', {}).html));")
        self.assertIn("sign-in is closed", empty)

    def test_access_page_shows_recent_security_activity_without_sensitive_values(self):
        html = self.run_configuration_script("""
const page = globalThis.ConfigurationPages.render('access', {
  allowed_emails: [], sign_ins: [], viewer: {role: 'owner'},
  audit: [
    {at: 1760000000, event: 'sign_in', who: 'owner@x.com', ip: '127.0.0.1', how: 'Google'},
    {at: 1760000001, event: 'settings_changed', who: 'owner@x.com', part: 'keys'},
    {at: 1760000002, event: 'denied', who: 'friend@x.com', what: 'use full console'},
  ],
});
process.stdout.write(JSON.stringify(page.html));
""")
        self.assertIn("Recent security activity", html)
        self.assertIn("owner@x.com signed in", html)
        self.assertIn("owner@x.com changed keys settings", html)
        self.assertIn("friend@x.com was denied permission to use full console", html)
        self.assertNotIn("secret", html.lower())

    def test_resolver_accepts_only_enabled_native_configuration_pages(self):
        result = self.run_configuration_script("""
const pages = globalThis.ConfigurationPages;
process.stdout.write(JSON.stringify({
  enabled: pages.resolve('email'),
  models: pages.resolve('models'),
  external: pages.resolve('projects'),
  unknown: pages.resolve('unknown')
}));
""")
        self.assertEqual(result["enabled"]["route"], "#/config/email")
        self.assertEqual(result["models"]["route"], "#/config/models")
        self.assertIsNone(result["external"])
        self.assertIsNone(result["unknown"])

    def test_menu_controller_closes_accessibly_and_navigates_enabled_entries(self):
        result = self.run_configuration_script("""
class EventTarget {
  constructor() {
    this.listeners = {};
    this.attributes = {};
    this.hidden = true;
    this.focusCount = 0;
  }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  removeEventListener(type, listener) {
    this.listeners[type] = (this.listeners[type] || []).filter((item) => item !== listener);
  }
  dispatch(type, event = {}) {
    event.type = type;
    event.target ||= this;
    event.preventDefault ||= () => {};
    for (const listener of this.listeners[type] || []) listener(event);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  contains(target) { return target === this || target.owner === this; }
  focus() { this.focusCount += 1; }
}
const trigger = new EventTarget();
const menu = new EventTarget();
const documentTarget = new EventTarget();
const routes = [];
const controller = globalThis.ConfigurationPages.createMenuController({
  trigger,
  menu,
  document: documentTarget,
  navigate: (route) => routes.push(route)
});
controller.open();
const expanded = trigger.attributes['aria-expanded'];
documentTarget.dispatch('keydown', {key: 'Escape'});
const escapeClosed = menu.hidden;
controller.open();
documentTarget.dispatch('pointerdown', {target: {}});
const outsideClosed = menu.hidden;
const enabled = {
  disabled: false,
  closest: (selector) => selector === '[data-config-route]' ? enabled : null,
  getAttribute: () => '#/config/api-keys'
};
controller.open();
menu.dispatch('click', {target: enabled});
const disabled = {
  disabled: true,
  closest: (selector) => selector === '[data-config-route]' ? disabled : null,
  getAttribute: () => '#/config/models'
};
controller.open();
menu.dispatch('click', {target: disabled});
controller.destroy();
process.stdout.write(JSON.stringify({
  expanded,
  escapeClosed,
  outsideClosed,
  focusCount: trigger.focusCount,
  routes,
  disabledStayedOpen: !menu.hidden
}));
""")
        self.assertEqual(result, {
            "expanded": "true",
            "escapeClosed": True,
            "outsideClosed": True,
            "focusCount": 1,
            "routes": ["#/config/api-keys"],
            "disabledStayedOpen": True,
        })

    def test_configuration_chooser_uses_registry_without_terminal_actions(self):
        result = self.run_configuration_script("""
const page = globalThis.ConfigurationPages.render(undefined, {});
process.stdout.write(JSON.stringify(page));
""")
        self.assertEqual(result["title"], "Configuration")
        for group in ("This project", ">AI<", "Computers &amp; access", "Alerts"):
            self.assertIn(group, result["html"])
        self.assertNotIn("Troubleshooting", result["html"])  # its checks are on Readiness now
        for route in ("#/config/ai", "#/config/base-branch", "#/config/email", "#/config/ai-instructions",
                      "#/config/models", "#/config/fleet", "#/config/firebase"):
            self.assertIn(route, result["html"])
        for route in ("#/readiness", "#/connections", "#/projects"):  # pages of their own, reached from Settings
            self.assertIn(route, result["html"])
        for route in ("#/config/documentation", "#/config/setup-wizard", "#/config/audit", "#/ux-review"):
            self.assertNotIn(route, result["html"])  # reachable elsewhere, not repeated
        self.assertEqual(result["html"].count(" disabled"), 0)
        self.assertNotIn("Coming next", result["html"])
        self.assertNotIn('data-action="config_menu"', result["html"])

    def test_api_keys_page_shows_status_without_rendering_secrets(self):
        result = self.run_configuration_script("""
const page = globalThis.ConfigurationPages.render('ai', {
  ai: {providers: [], plugins: []},
  keys: [
    {id: 'anthropic_api_key', label: 'Anthropic', saved: true, env: true, value: 'sk-do-not-render'},
    {id: 'openai_api_key', label: 'OpenAI', saved: false, env: true},
    {id: 'ollama_api_key', label: 'Ollama', saved: false, env: false}
  ],
  ollama_host: 'http://ollama.example'
});
process.stdout.write(JSON.stringify(page));
""")
        self.assertEqual(result["title"], "Add an AI")
        self.assertIn('id="api-keys"', result["html"])  # the keys card, on the page for adding an AI
        for label in ("Anthropic", "OpenAI", "Ollama", "Saved", "From environment", "Not set"):
            self.assertIn(label, result["html"])
        self.assertNotIn("sk-do-not-render", result["html"])
        self.assertEqual(result["html"].count("Host URL"), 1)
        self.assertIn("http://ollama.example", result["html"])

    def test_mutation_guard_blocks_duplicates_and_recovers(self):
        result = self.run_configuration_script("""
(async () => {
  let release;
  let calls = 0;
  const run = globalThis.ConfigurationPages.createMutationGuard((request) => {
    calls += 1;
    if (request.mode === 'pending') return new Promise((resolve) => { release = resolve; });
    if (request.mode === 'reject') return Promise.reject(new Error('nope'));
    return Promise.resolve(request.mode);
  });
  const firstPromise = run({mode: 'pending'});
  const blocked = await run({mode: 'blocked'});
  release('done');
  const first = await firstPromise;
  const afterResolve = await run({mode: 'after-resolve'});
  let rejection = '';
  try { await run({mode: 'reject'}); } catch (error) { rejection = error.message; }
  const afterReject = await run({mode: 'after-reject'});
  process.stdout.write(JSON.stringify({calls, blocked, first, afterResolve, rejection, afterReject}));
})();
""")
        self.assertEqual(result, {
            "calls": 4,
            "blocked": None,
            "first": "done",
            "afterResolve": "after-resolve",
            "rejection": "nope",
            "afterReject": "after-reject",
        })

    def test_route_match_rejects_responses_for_pages_the_user_left(self):
        result = self.run_configuration_script("""
const matches = globalThis.ConfigurationPages.routeMatches;
process.stdout.write(JSON.stringify({
  sameSection: matches({page: 'config', args: ['api-keys']}, 'api-keys'),
  chooser: matches({page: 'config', args: []}, undefined),
  otherSection: matches({page: 'config', args: ['email']}, 'api-keys'),
  otherPage: matches({page: 'projects', args: []}, 'api-keys')
}));
""")
        self.assertEqual(result, {
            "sameSection": True,
            "chooser": True,
            "otherSection": False,
            "otherPage": False,
        })

    def test_base_branch_page_uses_only_escaped_backend_branches(self):
        result = self.run_configuration_script("""
const page = globalThis.ConfigurationPages.render('base-branch', {
  base_branch: 'feature/one',
  branches: ['main', 'feature/one', '\"><script>bad()</script>']
});
process.stdout.write(JSON.stringify(page));
""")
        self.assertEqual(result["title"], "Base branch")
        self.assertIn('<option value="feature/one" selected>', result["html"])
        self.assertIn('&quot;&gt;&lt;script&gt;bad()&lt;/script&gt;', result["html"])
        self.assertNotIn('<script>bad()</script>', result["html"])
        self.assertNotIn("develop", result["html"])

    def test_archived_jobs_are_a_filter_on_home_with_restore(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        home = app[app.index("pages.home = async"):app.index("function jobHeaderActions")]
        self.assertIn('href="#/?filter=archived"', home)
        self.assertIn("data-restore-job", home)
        self.assertIn('api("config/archived-restore"', home)
        self.assertIn("this archive can't be read", home)  # a corrupt archive is listed, without Restore
        self.assertIn("No archived jobs", home)

    def test_email_page_reports_sender_readiness_without_secret_values(self):
        result = self.run_configuration_script("""
const gmail = globalThis.ConfigurationPages.render('email', {email: {
  provider: 'gmail', providers: ['gmail', 'resend'], recipients: ['person<one>@example.com'],
  smtp_email: 'sender@example.com', smtp_password_set: true, smtp_password: 'mail-secret',
  resend_from_email: '', resend_display_name: '', resend_api_key_set: false
}});
const resend = globalThis.ConfigurationPages.render('email', {email: {
  provider: 'resend', providers: ['gmail', 'resend'], recipients: ['person@example.com'],
  smtp_email: '', smtp_password_set: false, resend_from_email: 'updates@example.com',
  resend_display_name: 'Updates', resend_api_key_set: true, resend_api_key: 'resend-secret'
}});
const empty = globalThis.ConfigurationPages.render('email', {email: {
  provider: 'gmail', providers: ['gmail', 'resend'], recipients: [], smtp_email: '',
  smtp_password_set: false, resend_from_email: '', resend_display_name: '', resend_api_key_set: false
}});
process.stdout.write(JSON.stringify({gmail, resend, empty}));
""")
        gmail = result["gmail"]["html"]
        resend = result["resend"]["html"]
        empty = result["empty"]["html"]
        self.assertEqual(result["gmail"]["title"], "Email alerts")
        self.assertIn("Gmail is ready", gmail)
        self.assertIn("App password saved", gmail)
        self.assertIn("person&lt;one&gt;@example.com", gmail)
        self.assertIn('data-config-action="email-test"', gmail)
        self.assertIn("Resend is ready", resend)
        self.assertIn("API key saved", resend)
        self.assertNotIn("mail-secret", gmail)
        self.assertNotIn("resend-secret", resend)
        self.assertIn("No recipients yet", empty)
        self.assertNotIn('data-config-action="email-test"', empty)

    def test_orchestrator_guides_are_on_the_docs_page_and_settings_have_no_terminal_handoff(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        docs = app[app.index("pages.docs = async"):app.index("pages.product = async")]
        self.assertIn('g.section === "Orchestrator docs"', docs)
        self.assertIn("Orchestrator guides", docs)
        self.assertIn("config/doc?id=", docs)  # read by opaque id: no path is ever sent or shown
        result = self.run_configuration_script("""
const R = globalThis.ConfigurationPages.render;
const pages = [R(undefined, {}), R('ai', {ai: {providers: [], plugins: []}, keys: []}), R('base-branch', {branches: []}), R('email', {email: {recipients: []}})];
process.stdout.write(JSON.stringify(pages.map((page) => page.html).join('')));
""")
        for terminal_handoff in ('data-action="config_menu"', "Open Full CLI Menu"):
            self.assertNotIn(terminal_handoff, result)


class PlanRunApiTests(ServerTestCase):
    """Build the plan, through the API, with job runs replaced by recorders: nothing real is started."""

    class FakeRun:
        def __init__(self, n):
            self.id, self.running, self.job_id = f"run-{n}", True, None

    def setUp(self):
        super().setUp()
        self.started = []

        def start(action, title, argv, cwd, env, transcript_dir, cols=110, rows=32):
            run = self.FakeRun(len(self.started))
            self.started.append({"action": action, "argv": argv, "run": run})
            return run

        self.start_patch = patch.object(self.server.sessions, "start", side_effect=start)
        self.start_patch.start()
        for name, deps in (("Play a round", ""), ("Chat", "play-a-round"), ("Lobby", "")):
            self.request("POST", "/api/features", {"name": name, "depends_on": deps}, headers=UI_HEADERS)

    def tearDown(self):
        self.start_patch.stop()
        super().tearDown()

    def plan_argv(self):
        return [entry["argv"] for entry in self.started if entry["action"] == "new_job"]

    def write_job(self, job_id, feature, run_id, status, archived=False, **extra):
        folder = ui.jobs_dir(self.root) / ("archive" if archived else "")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{job_id}.json").write_text(json.dumps({"job_id": job_id, "title": feature, "type": "feature-plan", "status": status,
                                                          "feature": feature, "plan_run": run_id, **extra}))

    def wait_started(self, count):
        for _ in range(100):
            if len(self.plan_argv()) >= count:
                return
            time.sleep(0.02)

    def test_first_layer_starts_planning_and_dependents_wait(self):
        res, data = self.request("POST", "/api/plan-run", {}, headers=UI_HEADERS)
        self.assertEqual(res.status, 201, data)
        self.wait_started(2)
        self.server.plan_tick()
        argvs = self.plan_argv()
        machines = json.loads((ui.runtime_dir(self.root) / "config" / "machines.json").read_text())["machines"] \
            if (ui.runtime_dir(self.root) / "config" / "machines.json").exists() else []
        self.assertEqual(len(argvs), min(2, ui.plan_run.capacity(machines)))  # Play and Lobby are both ready; machines limit it
        self.assertIn("--no-dispatch", argvs[0])
        self.assertIn("play-a-round", argvs[0][argvs[0].index("--feature") + 1:][:1])
        self.assertIn("--plan-run", argvs[0])
        plan = self.request("GET", "/api/features")[1]["plan"]
        rows = {r["feature"]: r for r in plan["rows"]}
        self.assertEqual(rows["chat"]["state"], "waiting")
        self.assertIn("Play a round", rows["chat"]["detail"])

    def test_light_poll_reports_the_run_without_starting_anything(self):
        res, data = self.request("GET", "/api/plan-run")
        self.assertEqual((res.status, data["plan"]), (200, None))
        self.request("POST", "/api/plan-run", {"features": ["play-a-round", "chat"]}, headers=UI_HEADERS)
        self.wait_started(1)
        before = len(self.started)
        _, data = self.request("GET", "/api/plan-run")
        self.assertEqual([r["feature"] for r in data["plan"]["rows"]], ["play-a-round", "chat"])
        self.assertEqual(len(self.started), before)  # looking never starts work

    def test_a_finished_feature_releases_its_dependents(self):
        self.request("POST", "/api/plan-run", {"features": ["play-a-round", "chat"]}, headers=UI_HEADERS)
        self.wait_started(1)
        run_id = ui.plan_run.load(ui.runtime_dir(self.root))["id"]
        self.write_job("j-play", "play-a-round", run_id, "completed", archived=True)
        self.server.plan_tick()
        last = self.plan_argv()[-1]
        self.assertEqual(last[last.index("--feature") + 1], "chat")
        self.write_job("j-chat", "chat", run_id, "completed", archived=True)
        self.server.plan_tick()
        self.assertTrue(ui.plan_run.load(ui.runtime_dir(self.root))["finished"])

    def test_pause_stops_new_starts_and_resume_carries_on(self):
        self.request("POST", "/api/plan-run", {"features": ["play-a-round", "chat"]}, headers=UI_HEADERS)
        self.wait_started(1)
        run_id = ui.plan_run.load(ui.runtime_dir(self.root))["id"]
        res, data = self.request("POST", "/api/plan-run/pause", {"paused": True}, headers=UI_HEADERS)
        self.assertTrue(data["plan"]["paused"])
        self.write_job("j-play", "play-a-round", run_id, "completed", archived=True)
        self.server.plan_tick()
        self.assertEqual(len(self.plan_argv()), 1)  # chat is ready but the run is paused
        self.request("POST", "/api/plan-run/pause", {"paused": False}, headers=UI_HEADERS)
        self.server.plan_tick()
        self.assertEqual(len(self.plan_argv()), 2)

    def test_stop_ends_new_starts_and_a_new_run_can_begin(self):
        self.request("POST", "/api/plan-run", {"features": ["play-a-round"]}, headers=UI_HEADERS)
        res, _ = self.request("POST", "/api/plan-run", {"features": ["lobby"]}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)  # one plan at a time
        res, data = self.request("POST", "/api/plan-run/stop", {}, headers=UI_HEADERS)
        self.assertIsNone(data["plan"])  # stopping removes the run; running work carries on
        res, _ = self.request("POST", "/api/plan-run", {"features": ["lobby"]}, headers=UI_HEADERS)
        self.assertEqual(res.status, 201)

    def test_planning_that_ends_without_a_job_is_reported_not_retried(self):
        self.request("POST", "/api/plan-run", {"features": ["play-a-round"]}, headers=UI_HEADERS)
        self.wait_started(1)
        self.server.plan_tick()
        self.started[0]["run"].running = False  # the planning run ended and no job appeared
        self.server.plan_tick()
        self.server.plan_tick()
        self.assertEqual(len(self.plan_argv()), 1)
        row = self.request("GET", "/api/features")[1]["plan"]["rows"][0]
        self.assertEqual(row["state"], "failed_start")
        self.request("POST", "/api/plan-run/pause", {"paused": False}, headers=UI_HEADERS)  # resume retries it
        self.server.plan_tick()
        self.assertEqual(len(self.plan_argv()), 2)

    def test_auto_approve_uses_the_approve_action(self):
        self.request("POST", "/api/plan-run", {"features": ["play-a-round"], "auto_approve": True}, headers=UI_HEADERS)
        self.wait_started(1)
        run_id = ui.plan_run.load(ui.runtime_dir(self.root))["id"]
        self.write_job("j-play", "play-a-round", run_id, "planned")
        self.server.plan_tick()
        approvals = [e for e in self.started if e["action"] == "approve"]
        self.assertEqual(len(approvals), 1)
        self.assertIn("approve", approvals[0]["argv"])
        self.assertEqual(approvals[0]["run"].job_id, "j-play")


class ReadApiTests(ServerTestCase):
    def write_prd(self):
        from orchestrator import prd as prd_doc
        path = self.root / prd_doc.PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prd_doc.replace_section(prd_doc.template("Word Duel"), "pitch", "A turn-based word game to play with friends."))

    def wait_task(self, task):
        for _ in range(100):
            _, data = self.request("GET", f"/api/product/task/{task}")
            if data["status"] != "running":
                return data
            time.sleep(0.05)
        self.fail("the task never finished")

    def test_feature_draft_needs_product_requirements(self):
        res, data = self.request("POST", "/api/features/propose", {}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)
        res, _ = self.request("POST", "/api/features/propose", {})
        self.assertEqual(res.status, 403)  # another site can't start a model run
        self.assertIn("product requirements", data["error"])

    def test_feature_draft_proposes_then_accept_creates_chosen_in_order(self):
        self.write_prd()
        answer = json.dumps({"features": [
            {"name": "Challenge a friend", "summary": "Invite someone", "stories": ["As a player I can invite a friend."],
             "paths": ["app/social/"], "depends_on": ["Play a round"], "serves": "Playing with friends"},
            {"name": "Play a round", "summary": "One game", "stories": ["As a player I can play a round."], "paths": ["app/game/"],
             "depends_on": [], "serves": "Play"}]})
        calls = []
        with patch.object(ui.UIHandler, "_model_call", lambda handler, root, prompt, model="", timeout=150: calls.append(prompt) or answer):
            res, started = self.request("POST", "/api/features/propose", {}, headers=UI_HEADERS)
            self.assertEqual(res.status, 202)
            task = self.wait_task(started["task"])
        self.assertEqual(task["status"], "done", task)
        self.assertIn("A turn-based word game", calls[0])
        proposal = task["result"]
        self.assertEqual([f["name"] for f in proposal["features"]], ["Play a round", "Challenge a friend"])
        self.assertEqual(self.request("GET", "/api/features")[1]["features"], [])  # nothing saved yet

        res, data = self.request("POST", "/api/features/accept", {"features": proposal["features"], "chosen": ["Challenge a friend"]}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)  # it builds on a feature that wasn't chosen
        res, data = self.request("POST", "/api/features/accept", {"features": proposal["features"], "chosen": ["Play a round", "Challenge a friend"]}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200, data)
        saved = {f["name"]: f for f in data["features"]}
        self.assertEqual(saved["Challenge a friend"]["depends_on"], [saved["Play a round"]["id"]])
        self.assertEqual(saved["Play a round"]["stories"], ["As a player I can play a round."])

    def test_feature_accept_rechecks_what_the_page_sends(self):
        tampered = [{"name": "A", "depends_on": ["B"], "layer": 0}, {"name": "B", "depends_on": ["A"], "layer": 0}]
        res, data = self.request("POST", "/api/features/accept", {"features": tampered, "chosen": ["A", "B"]}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)
        self.assertIn("circle", data["error"])
        self.assertEqual(self.request("GET", "/api/features")[1]["features"], [])

    def test_ai_providers_lists_free_first_with_status_here(self):
        with patch("orchestrator.setup_checklist.ready_llm_providers", return_value=["claude"]), \
                patch("orchestrator.web.server.shutil.which", side_effect=lambda cli, path=None: "/bin/x" if cli == "claude" else None):
            res, data = self.request("GET", "/api/ai-providers")
        self.assertEqual(res.status, 200)
        self.assertEqual(data["providers"][0]["cost"], "free")
        claude = next(p for p in data["providers"] if p["id"] == "claude")
        self.assertTrue(claude["installed"] and claude["ready"])
        self.assertTrue(data["any_ready"])
        self.assertTrue(data["plugins"])

    def test_jobs_list_and_detail(self):
        _, data = self.request("GET", "/api/jobs")
        job = data["jobs"][0]
        self.assertEqual((job["id"], job["status"], job["tasks_total"], job["tasks_done"]),
                         ("20260922-bug-1", "debugging", 2, 1))
        res, detail = self.request("GET", "/api/jobs/20260922-bug-1")
        self.assertEqual(res.status, 200)
        self.assertEqual(detail["outputs"][0]["path"], "output/20260922-bug-1/brief.md")

    def test_the_workers_per_task_scratch_copy_is_not_listed_as_a_second_job(self):
        jobs = self.root / ".orchestrator" / "jobs"
        (jobs / "20260922-bug-1_task_1.json").write_text((jobs / "20260922-bug-1.json").read_text())
        _, data = self.request("GET", "/api/jobs")
        self.assertEqual([j["id"] for j in data["jobs"]].count("20260922-bug-1"), 1)

    def test_job_detail_says_why_a_planned_job_cannot_start(self):
        runtime = self.root / ".orchestrator"
        (runtime / "config").mkdir(exist_ok=True)
        (runtime / "config" / "machines.json").write_text('{"machines": []}')
        jobs = runtime / "jobs"
        (jobs / "20260922-planned-1.json").write_text(json.dumps({"job_id": "20260922-planned-1", "status": "planned", "title": "Planned", "type": "feature-plan"}))
        _, detail = self.request("GET", "/api/jobs/20260922-planned-1")
        self.assertEqual([b["id"] for b in detail["blockers"]], ["no-machine"])
        _, done = self.request("GET", "/api/jobs/20260922-bug-1")
        self.assertEqual(done["blockers"], [])

    def test_undoing_a_task_is_refused_cleanly_when_it_cannot_be_done(self):
        jobs = self.root / ".orchestrator" / "jobs"
        (jobs / "20260922-undo-1.json").write_text(json.dumps({"job_id": "20260922-undo-1", "status": "review-needed", "title": "U", "type": "feature-plan", "branch": "ai/x",
                                                               "completed_task_indices": [0], "task_commits": {"0": "0" * 40}}))
        _, detail = self.request("GET", "/api/jobs/20260922-undo-1")
        self.assertEqual(detail["undoable_task"], 0)
        res, data = self.request("POST", "/api/jobs/20260922-undo-1/revert-task", {"index": 0}, headers=UI_HEADERS)
        self.assertEqual(res.status, 409)  # not on the job's branch (the test project is not even a git repo)
        self.assertIn("Switch to ai/x", data["error"])
        res, _ = self.request("POST", "/api/jobs/20260922-undo-1/revert-task", {"index": "0"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)
        res, _ = self.request("POST", "/api/jobs/20260922-undo-1/revert-task", {"index": 5}, headers=UI_HEADERS)
        self.assertEqual(res.status, 409)

    def test_rerunning_a_failed_ci_run_asks_github_and_reports_its_refusal(self):
        import stat
        bin_dir = self.root / "fakebin"
        bin_dir.mkdir()
        log = self.root / "gh-calls.txt"
        gh = bin_dir / "gh"
        gh.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n[ "$3" = "99" ] && {{ echo "run 99 cannot be rerun" >&2; exit 1; }}\nexit 0\n')
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        with patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
            res, data = self.request("POST", "/api/delivery/rerun", {"run_id": 12}, headers=UI_HEADERS)
            self.assertEqual((res.status, data["ok"]), (200, True))
            self.assertEqual(log.read_text().strip(), "run rerun 12 --failed")  # only the failed jobs
            res, data = self.request("POST", "/api/delivery/rerun", {"run_id": 99}, headers=UI_HEADERS)
            self.assertEqual(res.status, 502)
            self.assertIn("cannot be rerun", data["error"])
        for bad in ("12", -1, 0, True, None):
            res, _ = self.request("POST", "/api/delivery/rerun", {"run_id": bad}, headers=UI_HEADERS)
            self.assertEqual(res.status, 400, bad)
        res, _ = self.request("POST", "/api/delivery/rerun", {"run_id": 12}, headers={"Content-Type": "application/json"})
        self.assertEqual(res.status, 403)  # needs the UI headers like every other write

    def test_mobile_app_detection_decides_whether_device_tools_are_offered(self):
        _, state = self.request("GET", "/api/state")
        self.assertIs(state["project"]["mobile_app"], False)  # the test project is a plain Python project
        (self.root / "ios").mkdir()
        _, state = self.request("GET", "/api/state")
        self.assertIs(state["project"]["mobile_app"], True)
        self.assertIn('state.project?.mobile_app === false', (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text())

    def test_features_carry_the_combined_check_result_and_say_when_it_is_stale(self):
        import subprocess as sp
        run = lambda *a: sp.run(["git", *a], cwd=self.root, capture_output=True, text=True, check=True, env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()
        run("init", "-q", "-b", "main")
        (self.root / "x.txt").write_text("x")
        run("add", "x.txt")
        run("commit", "-q", "-m", "base")
        for b in ("ai/one", "ai/two"):
            run("branch", b)
        runtime = self.root / ".orchestrator"
        (runtime / "features.json").write_text(json.dumps({"features": [{"id": "feat", "name": "Feat", "status": "in-progress", "paths": [], "depends_on": []}]}))
        for n, b in ((1, "ai/one"), (2, "ai/two")):
            (runtime / "jobs" / f"20260922-comb-{n}.json").write_text(json.dumps({"job_id": f"20260922-comb-{n}", "status": "review-needed", "title": f"C{n}", "type": "feature-plan", "branch": b, "feature": "feat"}))
        _, data = self.request("GET", "/api/features")
        combine = next(f for f in data["features"] if f["id"] == "feat")["combine"]
        self.assertEqual((sorted(combine["branches"]), combine["result"], combine["stale"]), (["ai/one", "ai/two"], None, False))
        from orchestrator import integration_check as ic
        heads = ic.branch_heads(self.root, ["ai/one", "ai/two"])
        ic.save_result(runtime, "feat", {"status": "pass", "base_head": run("rev-parse", "main"), "heads": heads, "merged": ["ai/one", "ai/two"]})
        _, data = self.request("GET", "/api/features")
        self.assertFalse(next(f for f in data["features"] if f["id"] == "feat")["combine"]["stale"])
        run("checkout", "-q", "ai/one")
        (self.root / "y.txt").write_text("y")
        run("add", "y.txt")
        run("commit", "-q", "-m", "moved")
        run("checkout", "-q", "main")
        _, data = self.request("GET", "/api/features")
        self.assertTrue(next(f for f in data["features"] if f["id"] == "feat")["combine"]["stale"])
        res, data = self.request("POST", "/api/runs", body={"action": "verify_feature", "params": {"feature": "nope"}}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)  # an unknown feature is refused before anything runs
        res, _ = self.request("POST", "/api/runs", body={"action": "verify_feature", "params": {}}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)

    def test_preflight_reports_a_missing_build_tool_and_blocks_a_planned_job_with_it(self):
        runtime = self.root / ".orchestrator"
        cfg = json.loads((runtime / "project.json").read_text()) if (runtime / "project.json").exists() else {}
        cfg.update({"build_command": "definitely-not-a-real-tool --build", "test_command": "python3 -m unittest"})
        (runtime / "project.json").write_text(json.dumps(cfg))
        (runtime / "config").mkdir(exist_ok=True)
        (runtime / "config" / "machines.json").write_text(json.dumps({"machines": [{"name": "local", "enabled": True, "execution_mode": "local", "models": ["m"]}]}))
        (runtime / "jobs" / "20260922-pre-1.json").write_text(json.dumps({"job_id": "20260922-pre-1", "status": "planned", "title": "P", "type": "feature-plan"}))
        _, data = self.request("GET", "/api/preflight?refresh=1")
        items = {i["id"]: i for i in data["items"]}
        self.assertEqual(items["build-tool"]["status"], "fail")
        self.assertIn("definitely-not-a-real-tool", items["build-tool"]["detail"])
        self.assertEqual(items["test-tool"]["status"], "ok")
        self.assertNotIn("xcode", items)  # not an Apple project
        _, detail = self.request("GET", "/api/jobs/20260922-pre-1")
        self.assertIn("build-tool", [b["id"] for b in detail["blockers"]])
        _, done = self.request("GET", "/api/jobs/20260922-bug-1")
        self.assertEqual(done["blockers"], [])  # finished jobs aren't blocked by the machine

    def test_job_id_validated(self):
        res, _ = self.request("GET", "/api/jobs/..%2Fproject")
        self.assertEqual(res.status, 400)
        res, _ = self.request("GET", "/api/jobs/missing")
        self.assertEqual(res.status, 404)

    def test_file_reads_are_confined_to_runtime_dir(self):
        res, data = self.request("GET", "/api/file?path=output/20260922-bug-1/brief.md")
        self.assertEqual((res.status, data["text"]), (200, "# Brief\n"))
        res, _ = self.request("GET", "/api/file?path=../../etc/passwd")
        self.assertEqual(res.status, 404)

    def test_configuration_documents_are_allowlisted_by_opaque_id(self):
        docs = self.root / "docs"
        docs.mkdir()
        guide = docs / "private-guide.md"
        guide.write_text("project-only marker\n")
        entry = next(item for item in ui.doc_entries(self.root) if item["name"] == guide.name)

        res, data = self.request("GET", f"/api/config/doc?id={entry['id']}")
        self.assertEqual((res.status, data), (200, {"name": guide.name, "text": "project-only marker\n"}))
        for invalid in ("missing", "..%2F0"):
            res, _ = self.request("GET", f"/api/config/doc?id={invalid}")
            self.assertEqual(res.status, 404)

    def test_devlogs_reports_unconfigured(self):
        _, data = self.request("GET", "/api/devlogs")
        self.assertEqual(data["sessions"]["configured"], False)
        self.assertEqual(data["pulls"], [])

    def test_switch_project_rejects_unknown_dirs(self):
        res, _ = self.request("POST", "/api/project", body={"root": "/tmp"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_project(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def spec_of(self, argv):
        return Path(argv[argv.index("--spec-file") + 1]).read_text()

    def test_new_job_writes_spec_file_and_passes_flags(self):
        argv = ui.build_new_job({"type": "feature", "summary": "Add rematch", "spec": "Details",
                                 "branch_mode": "new", "no_dispatch": True}, self.root)
        self.assertEqual(argv[4:9], ["script", "new_job.py", "feature", "--summary", "Add rematch"])
        spec = Path(argv[argv.index("--spec-file") + 1])
        self.assertTrue(spec.read_text().startswith("VISION: Add rematch\n\nADDITIONAL DETAILS:\nDetails"))
        self.assertIn(self.root / ".orchestrator" / "ui" / "specs", spec.parents)
        self.assertIn("--no-dispatch", argv)
        self.assertEqual(argv[argv.index("--branch-mode") + 1], "new")

    def test_each_job_type_is_composed_the_way_the_terminal_flow_asks_for_it(self):
        bug = self.spec_of(ui.build_new_job({"type": "bug", "summary": "Seat empty", "repro": "1. Join\n2. Background", "expected": "Seat kept"}, self.root))
        self.assertIn("REPRO STEPS:\n1. Join\n2. Background", bug)
        self.assertIn("EXPECTED BEHAVIOR:\nSeat kept", bug)
        self.assertNotIn("Anything left open", bug)  # a bug report has nothing left to decide
        design = self.spec_of(ui.build_new_job({"type": "design", "summary": "Profile screen", "vibe": "brutalist"}, self.root))
        self.assertIn("DESIGN VISION: Profile screen\nPREFERRED VIBE: brutalist", design)
        cov = self.spec_of(ui.build_new_job({"type": "coverage", "summary": "Lobby", "subsystems": "Seat service"}, self.root))
        self.assertIn("COVERAGE FOCUS: Lobby\n\nSUBSYSTEMS: Seat service", cov)

    def test_quick_is_just_the_instruction(self):
        argv = ui.build_new_job({"type": "quick", "summary": "Rename Foo to Bar\nand update docs " + "x" * 800}, self.root)
        self.assertEqual(argv[argv.index("--summary") + 1].splitlines()[0], "Rename Foo to Bar")
        self.assertNotIn("--spec-file", argv)

    def test_features_and_designs_are_told_to_choose_sensible_defaults_and_say_so(self):
        for kind in ("feature", "design"):
            self.assertIn("record each choice in `assumptions`", self.spec_of(ui.build_new_job({"type": kind, "summary": "Something"}, self.root)))
        self.assertNotIn("--spec-file", ui.build_new_job({"type": "quick", "summary": "Tweak"}, self.root))

    def test_the_old_you_decide_toggle_is_gone(self):
        argv = ui.build_new_job({"type": "bug", "summary": "x", "recommend": True}, self.root)
        self.assertNotIn("Decision latitude", self.spec_of(argv))
        self.assertNotIn("recommend", ui.ACTIONS["new_job"].fields)

    def test_attached_logs_are_quoted_for_the_planner_and_recorded_on_the_job(self):
        logs = self.root / ".orchestrator" / "logs" / "ui"
        logs.mkdir(parents=True)
        (logs / "20260922-101010-abcdef-test.log").write_text("starting\nFAILED: seat test\n")
        params = {"type": "bug", "summary": "Seat empty", "logs": [".orchestrator/logs/ui/20260922-101010-abcdef-test.log"]}
        spec = self.spec_of(ui.build_new_job(params, self.root))
        self.assertIn("### Log: test.log", spec)
        self.assertIn("FAILED: seat test", spec)
        path = self.root / ".orchestrator" / "jobs" / "20260922-bug-1.json"
        ui.UIHandler._record_attachments(path, params["_attachments"])
        self.assertEqual(json.loads(path.read_text())["last_manual_log_paths"], [".orchestrator/logs/ui/20260922-101010-abcdef-test.log"])

    def test_uploaded_references_are_listed_and_recorded_as_reference_artifacts(self):
        saved = ui.save_upload(self.root, "Login mock.PNG", b"\x89PNG\r\n")
        params = {"type": "design", "summary": "Login screen", "files": [saved["path"]]}
        spec = self.spec_of(ui.build_new_job(params, self.root))
        self.assertIn("Reference files", spec)
        self.assertIn(saved["path"], spec)
        path = self.root / ".orchestrator" / "jobs" / "20260922-bug-1.json"
        ui.UIHandler._record_attachments(path, params["_attachments"])
        ref = json.loads(path.read_text())["reference_artifacts"][0]
        self.assertEqual((ref["type"], ref["path"], ref["note"]), ("image_reference", saved["path"], "Login-mock.PNG"))

    def test_uploaded_text_logs_are_treated_as_logs_not_references(self):
        saved = ui.save_upload(self.root, "crash.log", b"EXC_BAD_ACCESS at seat.swift:42\n")
        params = {"type": "bug", "summary": "Crash", "files": [saved["path"]]}
        spec = self.spec_of(ui.build_new_job(params, self.root))
        self.assertIn("EXC_BAD_ACCESS", spec)
        self.assertEqual(params["_attachments"]["refs"], [])
        self.assertEqual(params["_attachments"]["logs"], [saved["path"]])

    def test_design_links_are_listed_and_recorded_and_junk_is_ignored(self):
        params = {"type": "design", "summary": "Login screen", "urls": ["https://www.figma.com/design/abc/Login", "https://example.com/mock", "javascript:alert(1)", "not a url", "ftp://x/y"]}
        spec = self.spec_of(ui.build_new_job(params, self.root))
        self.assertIn("https://www.figma.com/design/abc/Login", spec)
        self.assertNotIn("javascript:", spec)
        self.assertEqual([r["type"] for r in params["_attachments"]["refs"]], ["figma_url", "url_reference"])
        path = self.root / ".orchestrator" / "jobs" / "20260922-bug-1.json"
        ui.UIHandler._record_attachments(path, params["_attachments"])
        refs = json.loads(path.read_text())["reference_artifacts"]
        self.assertEqual((refs[0]["url"], refs[0]["type"], "path" in refs[0]), ("https://www.figma.com/design/abc/Login", "figma_url", False))

    def test_attachments_must_be_files_inside_the_runtime_folder(self):
        (self.root / "secret.txt").write_text("TOPSECRET")
        for rel in ("secret.txt", "../secret.txt", "/etc/hosts", ".orchestrator/nope.log"):
            with self.assertRaises(ui.UIError, msg=rel):
                ui.build_new_job({"type": "bug", "summary": "x", "logs": [rel]}, self.root)

    def test_new_job_for_a_feature_gives_the_planner_its_context(self):
        feature_store = ui.feature_store
        rt = self.root / ".orchestrator"
        feature_store.create(rt, "Auth", "Sign in", "Sources/Auth/")
        feature_store.create(rt, "Lobby", "Seats", "Sources/Lobby/", ["auth"])
        params = {"type": "feature", "summary": "Add rematch", "feature": "lobby"}
        argv = ui.build_new_job(params, self.root)
        text = Path(argv[argv.index("--spec-file") + 1]).read_text()
        self.assertIn('belongs to the feature "Lobby"', text)
        self.assertIn("Sources/Lobby/", text)
        self.assertIn("It builds on: Auth", text)
        self.assertIn("Auth: Sources/Auth/", text)  # other features' paths to stay out of
        self.assertEqual(params["_feature"], "lobby")

    def test_the_planner_is_told_which_use_case_a_feature_serves(self):
        ui.feature_store.create(self.root / ".orchestrator", "Rematch", "Ask again", serves="As a player I can ask for a rematch")
        argv = ui.build_new_job({"type": "feature", "summary": "Add rematch button", "feature": "rematch"}, self.root)
        text = Path(argv[argv.index("--spec-file") + 1]).read_text()
        self.assertIn("It serves this use case: As a player I can ask for a rematch", text)

    def test_new_job_rejects_an_unknown_feature(self):
        with self.assertRaises(ui.UIError):
            ui.build_new_job({"type": "feature", "summary": "x", "feature": "ghost"}, self.root)

    def test_new_job_for_a_feature_hands_new_job_its_feature(self):
        # new_job.py records it on the job as it's created (tests/test_e2e_workflow.py covers that side).
        ui.feature_store.create(self.root / ".orchestrator", "Lobby")
        argv = ui.build_new_job({"type": "bug", "summary": "Timer resets", "feature": "lobby"}, self.root)
        self.assertEqual(argv[argv.index("--feature") + 1], "lobby")
        self.assertNotIn("--feature", ui.build_new_job({"type": "bug", "summary": "Timer resets"}, self.root))

    def test_new_job_validates_input(self):
        with self.assertRaises(ui.UIError):
            ui.build_new_job({"type": "rm -rf", "summary": "x"}, self.root)
        with self.assertRaises(ui.UIError):
            ui.build_new_job({"type": "bug", "summary": ""}, self.root)

    def test_job_actions_resolve_job_file(self):
        argv = ui.ACTIONS["debug"].build({"job": "20260922-bug-1", "logs": "cloud:latest", "feedback": "still broken"}, self.root)
        self.assertTrue(argv[6].endswith(".orchestrator/jobs/20260922-bug-1.json"))
        self.assertEqual(argv[-4:], ["--logs", "cloud:latest", "--feedback", "still broken"])
        with self.assertRaises(ui.UIError):
            ui.ACTIONS["execute"].build({"job": "../../x"}, self.root)

    def test_logs_pull_session_validated(self):
        self.assertEqual(ui.build_logs_pull({}, self.root)[-2:], ["pull", "--latest"])
        self.assertEqual(ui.build_logs_pull({"session": "1a2b3c4d", "level": "warn"}, self.root)[-4:],
                         ["--session", "1a2b3c4d", "--level", "warn"])
        with self.assertRaises(ui.UIError):
            ui.build_logs_pull({"session": "x; rm -rf /"}, self.root)

    def test_every_action_uses_the_cli_entry_point_or_plain_git(self):
        import subprocess as sp
        sp.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"], cwd=self.root, check=True)
        params = {"job": "20260922-bug-1", "summary": "s", "feedback": "f", "answer": "a", "change": "c",
                  "name": "Suite", "branch": "main", "menu": "keys", "feature": "word-score"}
        (self.root / ".orchestrator" / "features.json").write_text(json.dumps({"features": [{"id": "word-score", "name": "Word score", "status": "planned", "paths": [], "depends_on": []}]}))
        for key, action in ui.ACTIONS.items():
            argv = action.build(dict(params, name="feature/x") if key == "git_new_branch" else params, self.root)
            if key == "stash_checkout":
                self.assertEqual(argv[:2], ["sh", "-c"], key)
            elif key.startswith("git_"):
                self.assertEqual(argv[0], "git", key)
            else:
                self.assertEqual(argv[:4], [sys.executable, "-P", "-m", "orchestrator"], key)

    def test_actions_run_the_package_that_serves_the_ui_not_the_projects_copy(self):
        import subprocess as sp
        server = ui.UIServer(("127.0.0.1", 0), self.root)
        try:
            env = server.child_env()
        finally:
            server.server_close()
        # A look-alike package in the project folder must not shadow the real one.
        fake = self.root / "orchestrator"
        fake.mkdir(exist_ok=True)
        (fake / "__init__.py").write_text("FAKE = True\n")
        out = sp.run([sys.executable, "-P", "-c", "import orchestrator; print(orchestrator.__file__)"],
                     cwd=self.root, env=env, capture_output=True, text=True).stdout.strip()
        self.assertEqual(Path(out).parent, Path(ui.__file__).resolve().parents[1])

    def test_config_menu_is_allowlisted(self):
        self.assertEqual(ui.build_config_menu({"menu": "fleet"}, self.root)[-2:], ["config_menu.py", "fleet"])
        for bad in ("", "fleet; rm -rf /", "../x"):
            with self.assertRaises(ui.UIError):
                ui.build_config_menu({"menu": bad}, self.root)

    def test_config_settings_roundtrip_and_validation(self):
        import subprocess as sp
        sp.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"], cwd=self.root, check=True)
        ui.config_update(self.root, "email", {"op": "add", "email": "a@b.co"})
        ui.config_update(self.root, "email", {"op": "add", "email": "a@b.co"})
        ui.config_update(self.root, "email", {
            "op": "provider", "provider": "gmail", "smtp_email": "sender@example.com",
            "smtp_password": "mail-secret",
        })
        ui.config_update(self.root, "email", {"op": "provider", "provider": "gmail", "smtp_password": ""})
        ui.config_update(self.root, "keys", {"id": "anthropic_api_key", "value": "sk-secret"})
        ui.config_update(self.root, "base-branch", {"branch": "main"})
        state = ui.config_state(self.root)
        self.assertEqual(state["email"]["recipients"], ["a@b.co"])
        self.assertTrue(state["email"]["smtp_password_set"])
        self.assertEqual(ui.read_settings(self.root)["smtp_password"], "mail-secret")
        self.assertEqual(state["base_branch"], "main")
        self.assertNotIn("sk-secret", json.dumps(state))  # secrets never leave the server
        self.assertNotIn("mail-secret", json.dumps(state))
        self.assertTrue([k for k in state["keys"] if k["id"] == "anthropic_api_key"][0]["saved"])
        archive = ui.jobs_dir(self.root) / "archive"
        archive.mkdir(parents=True)
        archived_file = archive / "job-ready.json"
        archived_file.write_text(json.dumps({"job_id": "job-ready", "title": "Ready", "status": "completed"}))
        ui.config_update(self.root, "archived-restore", {"id": "job-ready"})
        self.assertFalse(archived_file.exists())
        self.assertTrue((ui.jobs_dir(self.root) / "job-ready.json").is_file())
        for part, body in (("email", {"op": "add", "email": "nope"}), ("keys", {"id": "evil", "value": "x"}),
                           ("email", {"op": "provider", "provider": "carrier-pigeon"}),
                           ("base-branch", {"branch": "origin/evil"}), ("archived-restore", {"id": "../x"}), ("bogus", {})):
            with self.assertRaises(ui.UIError):
                ui.config_update(self.root, part, body)

    def test_stash_checkout_keeps_branch_out_of_the_shell_string(self):
        import subprocess as sp
        sp.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"], cwd=self.root, check=True)
        argv = ui.build_stash_checkout({"branch": "main"}, self.root)
        self.assertEqual(argv[-1], "main")
        self.assertNotIn("main", argv[2])
        with self.assertRaises(ui.UIError):
            ui.build_stash_checkout({"branch": "main; rm -rf /"}, self.root)

    def test_git_actions_validate_branches(self):
        import subprocess as sp
        sp.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"], cwd=self.root, check=True)
        self.assertEqual(ui.build_git_checkout({"branch": "main"}, self.root), ["git", "checkout", "main"])
        with self.assertRaises(ui.UIError):
            ui.build_git_checkout({"branch": "origin/evil"}, self.root)
        with self.assertRaises(ui.UIError):
            ui.build_git_new_branch({"name": "bad..name"}, self.root)
        with self.assertRaises(ui.UIError):
            ui.build_git_new_branch({"name": "-rf"}, self.root)
        self.assertEqual(ui.build_git_push({}, self.root), ["git", "push", "-u", "origin", "main"])

    def test_revise_and_test_names_are_validated(self):
        argv = ui.build_revise({"job": "20260922-bug-1", "change": "Split helper", "where": "Lobby"}, self.root)
        self.assertEqual(argv[6:8], ["revise", str((self.root / ".orchestrator/jobs/20260922-bug-1.json"))])
        self.assertEqual(argv[-6:], ["--change", "Split helper", "--where", "Lobby", "--done-when", ""])
        with self.assertRaises(ui.UIError):
            ui.ACTIONS["test_suite"].build({"name": "x; rm -rf /"}, self.root)


class WorktreeBranchTests(unittest.TestCase):
    """A branch checked out in another worktree can't be switched to here; say where it is instead of git's fatal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.root, self.other = base / "repo", base / "repo-other"
        self.root.mkdir()
        run = lambda *a, cwd=self.root: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)  # noqa: E731
        run("init", "-q", "-b", "main")
        run("-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "start")
        run("branch", "cloud-logs")
        run("branch", "feature")
        run("worktree", "add", "-q", str(self.other), "cloud-logs")

    def tearDown(self):
        self.tmp.cleanup()

    def test_branches_in_other_folders_are_found(self):
        self.assertEqual(ui.branches_elsewhere(self.root), {"cloud-logs": str(self.other)})
        self.assertEqual(ui.branches_elsewhere(self.other), {"main": str(self.root)})

    def test_switching_to_one_explains_instead_of_failing(self):
        with self.assertRaises(ui.UIError) as refused:
            ui.build_git_checkout({"branch": "cloud-logs"}, self.root)
        self.assertIn(str(self.other), str(refused.exception))
        self.assertIn("one place at a time", str(refused.exception))
        self.assertEqual(ui.build_git_checkout({"branch": "feature"}, self.root), ["git", "checkout", "feature"])

    def test_the_page_labels_them_and_explains(self):
        js = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        switch = js[js.index("async function switchBranch"):][:900]
        self.assertLess(switch.index("state.project?.elsewhere?.[branch]"), switch.index("runAction"))  # explained, never run
        self.assertIn('" (in another folder)"', js)
        self.assertEqual(js.count("branchOption("), 3)  # defined once, used by the top bar and the Git page


class JobStateTests(unittest.TestCase):
    def state(self, **job):
        return ui.job_state(job)

    def test_each_status_has_one_next_action(self):
        cases = {
            "planned": ("needs_you", "attention", "schedule"),
            "scheduled": ("working", "working", "execute"),
            "executing": ("working", "working", None),
            "completed": ("done", "done", None),
        }
        alive = {"worker_pid": os.getpid(), "worker_host": socket.gethostname()}  # "executing" means building only while a worker runs
        for status, (group, tone, action) in cases.items():
            st = self.state(status=status, **(alive if status == "executing" else {}))
            self.assertEqual((st["group"], st["tone"], st["next"] and st["next"]["action"]), (group, tone, action), status)

    def test_review_needed_offers_merge_with_a_pr_and_mark_complete_without(self):
        self.assertEqual(self.state(status="review-needed", pr_number=141)["next"]["action"], "merge")
        self.assertEqual(self.state(status="review-needed")["next"]["action"], "complete")
        self.assertTrue(ui.ACTIONS["complete"].confirm)

    def test_a_debugging_job_waiting_for_you_says_it_needs_a_fix(self):
        waiting = self.state(status="debugging")
        self.assertEqual((waiting["label"], waiting["tone"], waiting["next"]["action"]), ("Needs a fix", "attention", "debug"))

    def test_debugging_is_red_only_when_tests_fail(self):
        failing = self.state(status="debugging", test_status="tests-failed")
        self.assertEqual((failing["tone"], failing["label"], failing["next"]["action"]), ("failed", "Tests failing", "debug"))
        self.assertEqual(self.state(status="debugging")["tone"], "attention")

    def test_approvals(self):
        self.assertEqual(self.state(status="designing")["next"]["action"], "approve")
        self.assertEqual(self.state(status="planned", type="feature-plan")["next"]["label"], "Approve plan")
        self.assertEqual(self.state(status="planned", type="feature-plan", approved=True)["next"]["action"], "schedule")
        review = self.state(status="human-needed", verification={"status": "concerns", "comments": "Missing tests"})
        self.assertEqual((review["next"]["action"], review["label"]), ("approve", "Review suggestions"))
        self.assertIn("Missing tests", review["reason"])

    def test_question_needs_an_answer(self):
        st = self.state(status="human-needed", human_clarification_question="Which lobby size?")
        self.assertEqual((st["group"], st["next"]["action"]), ("needs_you", "answer"))

    def test_kind_labels_are_plain_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "j.json"
            path.write_text("{}")
            self.assertEqual(ui.job_summary(path, {"type": "bug-fix"})["kind"], "Bug fix")
            self.assertEqual(ui.job_summary(path, {"type": "test-audit"})["kind"], "Tests")


class LinkedLogTests(unittest.TestCase):
    def test_directories_expand_to_log_files_and_cloud_refs_are_described(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_project(root)
            pull = root / ".orchestrator" / "output" / "cloud_logs" / "20260921-1a2b"
            pull.mkdir(parents=True)
            (pull / "cloud.log").write_text("x")
            entry = ui.resolve_linked_log(root, ".orchestrator/output/cloud_logs/20260921-1a2b")
            self.assertEqual(entry["files"], ["output/cloud_logs/20260921-1a2b/cloud.log"])
            self.assertEqual(ui.resolve_linked_log(root, "cloud:latest")["files"], [])
            self.assertIn("Newest device launch", ui.resolve_linked_log(root, "cloud:latest")["label"])
            self.assertEqual(ui.resolve_linked_log(root, "/etc")["files"], [])


class JobDetailExtrasTests(unittest.TestCase):
    def test_docs_changes_and_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_project(root)
            (root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "Demo", "git_remote": "git@github.com:acme/app.git"}))
            job_file = root / ".orchestrator" / "jobs" / "20260922-bug-1.json"
            job = json.loads(job_file.read_text())
            job.update({"pr_number": 141, "issue_number": 12, "ai_modified_files": ["App/Lobby.swift"], "builder_hypothesis": "Stale presence"})
            job_file.write_text(json.dumps(job))
            (root / ".orchestrator" / "output" / "20260922-bug-1" / "builder_summary.md").write_text("Did the thing")
            detail = ui.job_detail(root, "20260922-bug-1")
        self.assertEqual([d["title"] for d in detail["docs"]], ["Brief", "Builder summary"])
        self.assertEqual(detail["changes"]["files"], ["App/Lobby.swift"])
        self.assertEqual(detail["changes"]["hypothesis"], "Stale presence")
        self.assertEqual(detail["links"], [
            {"label": "Issue #12", "url": "https://github.com/acme/app/issues/12"},
            {"label": "Pull request #141", "url": "https://github.com/acme/app/pull/141"},
        ])

    def test_repo_web_url_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_project(root)
            for remote, expected in [("git@github.com:acme/app.git", "https://github.com/acme/app"),
                                     ("https://github.com/acme/app", "https://github.com/acme/app"),
                                     ("https://token@github.com/acme/app.git", "https://github.com/acme/app"),
                                     ("not a url", None)]:
                (root / ".orchestrator" / "project.json").write_text(json.dumps({"git_remote": remote}))
                self.assertEqual(ui.repo_web_url(root), expected, remote)

    def test_git_state(self):
        import subprocess as sp
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_project(root)
            sp.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "first"], cwd=root, check=True)
            (root / "tracked.txt").write_text("a")
            sp.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add"], cwd=root, check=True)
            (root / "tracked.txt").write_text("b")  # " M tracked.txt": leading space on the first line
            (root / "new.txt").write_text("x")
            g = ui.git_state(root)
        self.assertIn({"status": "M", "path": "tracked.txt"}, g["changes"])
        self.assertEqual((g["branch"], g["upstream"], g["branches"]), ("main", None, ["main"]))
        self.assertIn("add", g["last_commit"])
        self.assertIn({"status": "??", "path": "new.txt"}, g["changes"])


class PtySessionTests(unittest.TestCase):
    def collect(self, session, until, timeout=10):
        offset, out = 0, b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            offset, data, finished = session.read(offset, timeout=0.5)
            out += data
            if until(out) or finished:
                return out, finished
        return out, False

    def test_runs_in_a_real_tty_and_takes_input(self):
        script = "import os; print('tty', os.isatty(0)); print('got', input('name? '))"
        with tempfile.TemporaryDirectory() as tmp:
            session = ui.PtySession("t1", "test", "Test", [sys.executable, "-c", script], Path(tmp), dict(os.environ))
            out, _ = self.collect(session, lambda o: b"name?" in o)
            self.assertIn(b"tty True", out)
            session.write(b"alice\r")
            out, finished = self.collect(session, lambda o: False)
            self.assertTrue(finished)
            self.assertIn(b"got alice", out)
            self.assertEqual(session.exit_code, 0)

    def test_has_controlling_terminal_for_getpass(self):
        script = "import os; fd = os.open('/dev/tty', os.O_RDWR); os.write(fd, b'ctty ok\\n')"
        with tempfile.TemporaryDirectory() as tmp:
            session = ui.PtySession("t2", "test", "Test", [sys.executable, "-c", script], Path(tmp), dict(os.environ))
            out, finished = self.collect(session, lambda o: False)
            self.assertTrue(finished)
            self.assertIn(b"ctty ok", out)

    def test_stop_terminates_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = ui.PtySession("t3", "test", "Test", [sys.executable, "-c", "import time; time.sleep(60)"],
                                    Path(tmp), dict(os.environ))
            session.stop()
            _, finished = self.collect(session, lambda o: False)
            self.assertTrue(finished)
            self.assertNotEqual(session.exit_code, 0)

    def test_late_reader_catches_up_from_offset_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = ui.PtySession("t4", "test", "Test", [sys.executable, "-c", "print('early')"], Path(tmp), dict(os.environ))
            self.collect(session, lambda o: False)
            _, data, finished = session.read(0, timeout=0)
            self.assertIn(b"early", data)
            self.assertTrue(finished)


class RunApiTests(ServerTestCase):
    def test_start_stream_and_input(self):
        echo = ui.Action("Echo", lambda p, r: [sys.executable, "-c", "print('ready'); print('you said', input())"])
        with patch.dict(ui.ACTIONS, {"echo": echo}):
            res, data = self.request("POST", "/api/runs", body={"action": "echo"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 201)
        sid = data["run"]["id"]
        session = self.server.sessions.get(sid)
        deadline = time.time() + 10
        while b"ready" not in session.read(0, 0.2)[1] and time.time() < deadline:
            pass
        res, _ = self.request("POST", f"/api/runs/{sid}/input", body={"data": "hi\r"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("GET", f"/api/runs/{sid}/stream?offset=0", headers={"Authorization": "Bearer test-token"})
        stream = conn.getresponse()
        self.assertEqual(stream.getheader("Content-Type"), "text/event-stream")
        text = stream.read().decode()
        conn.close()
        chunks = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and '"data"' in line]
        output = b"".join(base64.b64decode(c["data"]) for c in chunks)
        self.assertIn(b"you said hi", output)
        self.assertIn("event: end", text)
        transcript = list((self.root / ".orchestrator" / "logs" / "ui").glob(f"{sid}-echo.log"))
        self.assertEqual(len(transcript), 1)

    def test_output_endpoint_follows_a_run_with_plain_requests(self):
        # Tunnels that hold back event streams (Cloudflare's free ones) still carry ordinary requests.
        slow = ui.Action("Slow", lambda p, r: [sys.executable, "-u", "-c",
                                               "import time; print('first'); time.sleep(1.5); print('second')"])
        with patch.dict(ui.ACTIONS, {"slow": slow}):
            _, data = self.request("POST", "/api/runs", body={"action": "slow"}, headers=UI_HEADERS)
        sid, offset, output, started, finished, exit_code = data["run"]["id"], 0, b"", time.time(), False, None
        arrivals = []
        while not finished and time.time() - started < 20:
            res, out = self.request("GET", f"/api/runs/{sid}/output?offset={offset}&wait=5")
            self.assertEqual(res.status, 200)
            offset, finished, exit_code = out["offset"], out["finished"], out["exit_code"]
            chunk = base64.b64decode(out["data"])
            if chunk:
                arrivals.append((time.time() - started, chunk))
            output += chunk
        self.assertTrue(finished)
        self.assertEqual(exit_code, 0)
        self.assertIn(b"first", output)
        self.assertIn(b"second", output)
        # "first" arrived on its own, before "second" was printed: the wait ends when there is something new
        self.assertIn(b"first", arrivals[0][1])
        self.assertNotIn(b"second", arrivals[0][1])
        self.assertLess(arrivals[0][0], 1.2)

    def test_output_endpoint_rules(self):
        res, _ = self.request("GET", "/api/runs/nope/output?offset=0", auth=False)
        self.assertEqual(res.status, 401)
        quick = ui.Action("Quick", lambda p, r: [sys.executable, "-c", "print('x')"])
        with patch.dict(ui.ACTIONS, {"quick": quick}):
            _, data = self.request("POST", "/api/runs", body={"action": "quick"}, headers=UI_HEADERS)
        sid = data["run"]["id"]
        res, out = self.request("GET", f"/api/runs/{sid}/output?offset=abc")
        self.assertEqual(res.status, 400)
        res, out = self.request("GET", f"/api/runs/{sid}/output?offset=0&wait=9999")  # capped, and returns as soon as it finishes
        self.assertEqual(res.status, 200)
        self.assertTrue(out["finished"] or out["data"])

    def test_run_page_uses_polling_through_a_tunnel_and_the_stream_locally(self):
        js = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn("backend ? followByPolling(", js)
        self.assertIn("followByStream(", js)
        polling = js[js.index("function followByPolling"):][:1400]
        self.assertIn("runs/${id}/output?offset=${offset}&wait=", polling)
        self.assertNotIn("EventSource", polling)

    def test_run_starts_at_requested_terminal_size(self):
        probe = ui.Action("Size", lambda p, r: [sys.executable, "-c",
                                                "import os; s = os.get_terminal_size(); print('size', s.columns, s.lines)"])
        with patch.dict(ui.ACTIONS, {"size": probe}):
            _, data = self.request("POST", "/api/runs", body={"action": "size", "cols": 48, "rows": 20}, headers=UI_HEADERS)
        session = self.server.sessions.get(data["run"]["id"])
        out, deadline = b"", time.time() + 10
        while b"size" not in out and time.time() < deadline:
            out = session.read(0, 0.2)[1]
        self.assertIn(b"size 48 20", out)

    def wait_finished(self, sid, timeout=10):
        session = self.server.sessions.get(sid)
        deadline = time.time() + timeout
        while session.running and time.time() < deadline:
            time.sleep(0.05)
        return session

    def test_job_runs_are_titled_and_linked_to_their_job(self):
        noop = ui.Action("Run fix", lambda p, r: [sys.executable, "-c", "pass"], fields=["job"])
        with patch.dict(ui.ACTIONS, {"noop": noop}):
            _, data = self.request("POST", "/api/runs", body={"action": "noop", "params": {"job": "20260922-bug-1"}}, headers=UI_HEADERS)
        self.assertEqual(data["run"]["title"], "Run fix · Lobby seat stays empty")
        self.assertEqual(data["run"]["job"], "20260922-bug-1")
        _, detail = self.request("GET", "/api/jobs/20260922-bug-1")
        self.assertEqual([r["id"] for r in detail["runs"]], [data["run"]["id"]])

    def test_new_job_run_reports_the_job_it_created(self):
        jobs = self.root / ".orchestrator" / "jobs"
        script = f"import json,time; time.sleep(0.3); open({str(jobs / 'new-1.json')!r},'w').write(json.dumps({{'title':'x'}}))"
        fake = ui.Action("New job", lambda p, r: [sys.executable, "-c", script])
        with patch.dict(ui.ACTIONS, {"new_job": fake}):
            _, data = self.request("POST", "/api/runs", body={"action": "new_job"}, headers=UI_HEADERS)
        self.wait_finished(data["run"]["id"])
        time.sleep(1.2)
        _, runs = self.request("GET", "/api/runs")
        self.assertEqual(runs["runs"][0]["result_job"], "new-1")

    def test_coverage_job_run_is_titled_coverage_expanding(self):
        fake = ui.Action("New job", lambda p, r: [sys.executable, "-c", "import time; time.sleep(0.1)"])
        with patch.dict(ui.ACTIONS, {"new_job": fake}):
            _, data = self.request("POST", "/api/runs", body={"action": "new_job", "params": {"type": "coverage", "summary": "Improve test coverage"}}, headers=UI_HEADERS)
        self.assertEqual(data["run"]["title"], "Coverage expanding")
        self.wait_finished(data["run"]["id"])

    def test_jobs_list_marks_jobs_with_a_running_run(self):
        sleeper = ui.Action("Run fix", lambda p, r: [sys.executable, "-c", "import time; time.sleep(30)"], fields=["job"])
        with patch.dict(ui.ACTIONS, {"sleep": sleeper}):
            self.request("POST", "/api/runs", body={"action": "sleep", "params": {"job": "20260922-bug-1"}}, headers=UI_HEADERS)
        _, data = self.request("GET", "/api/jobs")
        self.assertTrue(data["jobs"][0]["active_run"])

    def test_unknown_action_rejected(self):
        res, data = self.request("POST", "/api/runs", body={"action": "shell", "params": {"cmd": "ls"}}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)
        self.assertIn("Unknown action", data["error"])

    def test_auth_endpoint_valid_token_sets_cookie(self):
        res, data = self.request("POST", "/api/auth", body={"token": "test-token"}, headers=UI_HEADERS, auth=False)
        self.assertEqual(res.status, 200)
        self.assertTrue(data["ok"])
        cookie = res.getheader("Set-Cookie")
        self.assertIn("orchestrator_ui=test-token", cookie)
        self.assertIn("HttpOnly", cookie)

    def test_auth_endpoint_invalid_token_rejected(self):
        res, data = self.request("POST", "/api/auth", body={"token": "wrong"}, headers=UI_HEADERS, auth=False)
        self.assertEqual(res.status, 401)
        self.assertIn("Invalid access token", data["error"])

    def test_auth_endpoint_valid_id_token(self):
        with patch("orchestrator.web.server.verify_firebase_id_token", return_value={"email": "tester@example.com"}), \
             patch("orchestrator.web.server.allowed_auth_sources", return_value={"tester@example.com": "git"}):
            res, data = self.request("POST", "/api/auth", body={"id_token": "valid-id-token"}, headers=UI_HEADERS, auth=False)
            self.assertEqual(res.status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(data["email"], "tester@example.com")
            cookie = res.getheader("Set-Cookie")
            self.assertIn(f"orchestrator_ui={data['token']}", cookie)
            self.assertNotIn("test-token", cookie)

    def test_auth_endpoint_unauthorized_email(self):
        with patch("orchestrator.web.server.verify_firebase_id_token", return_value={"email": "intruder@example.com"}), \
             patch("orchestrator.web.server.allowed_auth_sources", return_value={"owner@example.com": "git"}):
            res, data = self.request("POST", "/api/auth", body={"id_token": "some-id-token"}, headers=UI_HEADERS, auth=False)
            self.assertEqual(res.status, 403)
            self.assertIn("not authorized", data["error"])

    def test_auth_refuses_accounts_without_a_verified_email(self):
        # e.g. a GitHub account whose email is missing, or one the provider hasn't verified
        for info, expected in (({"email": ""}, "didn't share an email"),
                               ({"email": "tester@example.com", "emailVerified": False}, "isn't verified")):
            with patch("orchestrator.web.server.verify_firebase_id_token", return_value=info), \
                 patch("orchestrator.web.server.allowed_auth_sources", return_value={"tester@example.com": "git"}):
                res, data = self.request("POST", "/api/auth", body={"id_token": "t"}, headers=UI_HEADERS, auth=False)
                self.assertEqual(res.status, 403, info)
                self.assertIn(expected, data["error"])

    def test_auth_is_closed_when_no_emails_are_allowed(self):
        with patch("orchestrator.web.server.verify_firebase_id_token", return_value={"email": "anyone@example.com"}), \
             patch("orchestrator.web.server.allowed_auth_sources", return_value={}):
            res, data = self.request("POST", "/api/auth", body={"id_token": "t"}, headers=UI_HEADERS, auth=False)
            self.assertEqual(res.status, 403)
            self.assertIn("sign-in is closed", data["error"])
        # the access token path is unaffected
        res, _ = self.request("POST", "/api/auth", body={"token": "test-token"}, headers=UI_HEADERS, auth=False)
        self.assertEqual(res.status, 200)

    def test_allowed_emails_can_be_managed_and_report_their_source(self):
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_EMAILS": "env@example.com"}):
            ui.config_update(self.root, "allowed-email", {"op": "add", "email": "New@Example.com"})
            sources = ui.allowed_auth_sources(self.root)
            self.assertEqual(sources["env@example.com"], "environment")
            self.assertEqual(sources["new@example.com"], "settings")
            with self.assertRaises(ui.UIError):  # can't remove what this page didn't add
                ui.config_update(self.root, "allowed-email", {"op": "remove", "email": "env@example.com"})
            with self.assertRaises(ui.UIError):
                ui.config_update(self.root, "allowed-email", {"op": "add", "email": "not-an-email"})
            ui.config_update(self.root, "allowed-email", {"op": "remove", "email": "new@example.com"})
            self.assertNotIn("new@example.com", ui.allowed_auth_sources(self.root))

    def test_auth_logout_clears_cookie(self):
        res, data = self.request("POST", "/api/auth/logout", body={}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        cookie = res.getheader("Set-Cookie")
        self.assertIn("Max-Age=0", cookie)

    def test_projects_endpoint_lists_all_projects_and_active(self):
        res, data = self.request("GET", "/api/projects")
        self.assertEqual(res.status, 200)
        self.assertEqual(data["active"], str(ui.safe_resolve(self.root)))
        active_proj = next(p for p in data["projects"] if p["root"] == str(ui.safe_resolve(self.root)))
        self.assertTrue(active_proj["active"])
        self.assertIn("source_type", active_proj)
        self.assertEqual(active_proj["source_type"], "local")

    def test_detect_project_source_github_and_local(self):
        s_type, s_label, repo = ui.detect_project_source(self.root)
        self.assertEqual(s_type, "local")
        self.assertEqual(s_label, "Local")

        s_type, s_label, repo = ui.detect_project_source(self.root, {"github_repo": "my-org/my-project"})
        self.assertEqual(s_type, "github")
        self.assertEqual(s_label, "GitHub Tracked")
        self.assertEqual(repo, "my-org/my-project")

    def test_projects_add_and_forget(self):
        other_dir = self.root.parent / "other_project"
        make_project(other_dir)
        res, data = self.request("POST", "/api/projects/add", body={"root": str(other_dir), "name": "Other"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertTrue(data["ok"])

        res, data = self.request("GET", "/api/projects")
        self.assertTrue(any(p["root"] == str(ui.safe_resolve(other_dir)) for p in data["projects"]))

        res, data = self.request("DELETE", "/api/projects", body={"root": str(other_dir)}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertFalse(any(p["root"] == str(ui.safe_resolve(other_dir)) for p in data["projects"]))

    def test_projects_scan_finds_projects(self):
        other_dir = self.root.parent / "scanned_project"
        make_project(other_dir)
        res, data = self.request("POST", "/api/projects/scan", body={"paths": [str(self.root.parent)]}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        discovered = data["discovered"]
        self.assertTrue(any(p["root"] == str(ui.safe_resolve(other_dir)) for p in discovered))

    def test_scan_local_languages(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "App.swift").write_text("A" * 3000)
            (tmp_path / "script.py").write_text("B" * 1000)
            langs = ui.scan_local_languages(tmp_path)
            self.assertEqual(len(langs), 2)
            self.assertEqual(langs[0]["name"], "Swift")
            self.assertEqual(langs[0]["percent"], 75.0)
            self.assertEqual(langs[0]["color"], "#F05138")
            self.assertEqual(langs[1]["name"], "Python")
            self.assertEqual(langs[1]["percent"], 25.0)
            self.assertEqual(langs[1]["color"], "#3572A5")

    def test_get_project_languages_github_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_output = json.dumps({"Swift": 8000, "Python": 2000})
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=["gh"], returncode=0, stdout=fake_output, stderr=""
                )
                langs = ui.get_project_languages(tmp_path, github_repo="test-owner/test-repo")
                self.assertEqual(len(langs), 2)
                self.assertEqual(langs[0]["name"], "Swift")
                self.assertEqual(langs[0]["percent"], 80.0)
                self.assertEqual(langs[1]["name"], "Python")
                self.assertEqual(langs[1]["percent"], 20.0)

    def test_state_and_projects_include_languages(self):
        res, data = self.request("GET", "/api/state")
        self.assertEqual(res.status, 200)
        self.assertIn("languages", data["project"])
        self.assertIsInstance(data["project"]["languages"], list)

        res, data = self.request("GET", "/api/projects")
        self.assertEqual(res.status, 200)
        self.assertTrue(len(data["projects"]) > 0)
        for p in data["projects"]:
            self.assertIn("languages", p)
            self.assertIsInstance(p["languages"], list)

    def test_job_export_zip_endpoint(self):
        res, data = self.request("GET", "/api/jobs/20260922-bug-1/export-zip")
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Content-Type"), "application/zip")
        self.assertIn("20260922-bug-1-export.zip", res.getheader("Content-Disposition", ""))
        zip_bytes = io.BytesIO(data)
        with zipfile.ZipFile(zip_bytes, "r") as zf:
            names = zf.namelist()
            self.assertIn("20260922-bug-1/job.json", names)

    def test_visual_checks_endpoint(self):
        res, data = self.request("GET", "/api/visual-checks")
        self.assertEqual(res.status, 200)
        self.assertIn("checks", data)
        self.assertIsInstance(data["checks"], list)

    def test_config_models_update(self):
        body = {
            "models": {
                "architect": "claude-3-7-sonnet",
                "planner": "gpt-4o",
                "builder": "claude-3-5-sonnet",
                "reviewer": "claude-3-7-sonnet"
            }
        }
        res, data = self.request("POST", "/api/config/models", body=body, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertTrue(data["ok"])
        state = ui.config_state(self.root)
        self.assertEqual(state["models"]["architect"], "claude-3-7-sonnet")

    def test_config_role_prompts_update(self):
        body = {
            "id": "architect",
            "content": "Custom architect system instructions"
        }
        res, data = self.request("POST", "/api/config/role-prompts", body=body, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertTrue(data["ok"])
        state = ui.config_state(self.root)
        architect_prompt = next(p for p in state["role_prompts"] if p["id"] == "architect")
        self.assertTrue(architect_prompt["customized"])
        self.assertIn("Custom architect system instructions", architect_prompt["content"])

    def test_config_fleet_actions(self):
        res, data = self.request("POST", "/api/config/fleet", body={
            "op": "add", "name": "build-mac-1", "ssh_target": "user@mac1", "repo_path": "/Users/user/repo", "mode": "remote"
        }, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertTrue(data["ok"])
        state = ui.config_state(self.root)
        mac = next(m for m in state["machines"] if m["name"] == "build-mac-1")
        self.assertEqual(mac["ssh_target"], "user@mac1")
        self.assertTrue(mac["enabled"])

        res, data = self.request("POST", "/api/config/fleet", body={"op": "toggle", "name": "build-mac-1"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        state = ui.config_state(self.root)
        mac = next(m for m in state["machines"] if m["name"] == "build-mac-1")
        self.assertFalse(mac["enabled"])

        res, data = self.request("POST", "/api/config/fleet", body={"op": "remove", "name": "build-mac-1"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        state = ui.config_state(self.root)
        self.assertFalse(any(m["name"] == "build-mac-1" for m in state["machines"]))

    def test_config_firebase_update(self):
        body = {
            "firebase_app_id": "1:123456:ios:abcdef",
            "firebase_tester_groups": "internal-testers"
        }
        res, data = self.request("POST", "/api/config/firebase", body=body, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertTrue(data["ok"])
        state = ui.config_state(self.root)
        self.assertEqual(state["firebase"]["app_id"], "1:123456:ios:abcdef")
        self.assertEqual(state["firebase"]["tester_groups"], "internal-testers")



class JobChatEndpointTests(ServerTestCase):
    JOB = "20260922-bug-1"

    def completed(self, stdout, returncode=0, stderr=""):
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)

    def test_chat_stores_the_exchange_on_the_job(self):
        with patch.object(ui.subprocess, "run", return_value=self.completed("noise\n<<<ORCHESTRATOR-REPLY>>>\nIt was needed.\n")):
            res, data = self.request("POST", f"/api/jobs/{self.JOB}/chat", body={"message": "Why?"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertEqual([m["text"] for m in data["conversation"]], ["Why?", "It was needed."])
        res, detail = self.request("GET", f"/api/jobs/{self.JOB}")
        self.assertEqual(len(detail["job"]["conversation"]), 2)

    def test_model_failure_is_reported_and_nothing_is_saved(self):
        with patch.object(ui.subprocess, "run", return_value=self.completed("", 1, "No model is available.\n")):
            res, data = self.request("POST", f"/api/jobs/{self.JOB}/chat", body={"message": "Why?"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 502)
        res, detail = self.request("GET", f"/api/jobs/{self.JOB}")
        self.assertNotIn("conversation", detail["job"])

    def test_empty_message_is_rejected(self):
        res, _ = self.request("POST", f"/api/jobs/{self.JOB}/chat", body={"message": " "}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)

    def test_chat_requires_the_ui_header(self):
        res, _ = self.request("POST", f"/api/jobs/{self.JOB}/chat", body={"message": "hi"}, headers={"Content-Type": "application/json"})
        self.assertIn(res.status, (400, 403))


class InboxEndpointTests(ServerTestCase):
    def test_lists_what_needs_you_and_counts_it_in_state(self):
        res, data = self.request("GET", "/api/inbox")
        self.assertEqual(res.status, 200)
        self.assertEqual([i["job_id"] for i in data["here"]], ["20260922-bug-1"])
        self.assertEqual(data["here"][0]["next"]["action"], "debug")
        self.assertEqual(data["count"], 1)
        _, state = self.request("GET", "/api/state")
        self.assertEqual(state["inbox_count"], 1)

    def test_other_projects_show_up_after_the_active_one(self):
        other = Path(self.tmp.name) / "other"
        make_project(other)
        ui.remember_project(other, "Other")
        _, data = self.request("GET", "/api/inbox")
        self.assertEqual(data["count"], 1)  # only this project's count
        self.assertEqual([(i["project"]["name"], i["job_id"]) for i in data["elsewhere"]], [("Other", "20260922-bug-1")])

    def test_a_job_in_the_middle_of_a_run_is_not_waiting_on_you(self):
        with patch.object(ui.SessionManager, "running_job_ids", return_value={"20260922-bug-1"}):
            _, data = self.request("GET", "/api/inbox")
        self.assertEqual(data["here"], [])


class WebhookNotificationTests(ServerTestCase):
    URL = "https://hooks.slack.com/services/T000/B000/secret"

    def post_hook(self, body):
        return self.request("POST", "/api/config/webhook", body=body, headers=UI_HEADERS)

    def test_save_never_echoes_the_url_and_clear_removes_it(self):
        res, _ = self.post_hook({"op": "set", "url": self.URL})
        self.assertEqual(res.status, 200)
        _, config = self.request("GET", "/api/config")
        self.assertEqual(config["webhook"], {"set": True, "host": "hooks.slack.com"})
        self.assertNotIn("secret", json.dumps(config))
        self.post_hook({"op": "clear"})
        _, config = self.request("GET", "/api/config")
        self.assertEqual(config["webhook"], {"set": False, "host": ""})

    def test_rejects_non_https_and_test_without_a_webhook(self):
        res, _ = self.post_hook({"op": "set", "url": "http://hooks.example/x"})
        self.assertEqual(res.status, 400)
        res, _ = self.post_hook({"op": "test"})
        self.assertEqual(res.status, 400)

    def test_test_message_is_posted_and_failures_are_reported(self):
        self.post_hook({"op": "set", "url": self.URL})
        with patch.object(ui.notifier, "post_webhook") as post:
            res, _ = self.post_hook({"op": "test"})
        self.assertEqual(res.status, 200)
        self.assertEqual(post.call_args[0][0], self.URL)
        self.assertIn("Test notification", post.call_args[0][1]["text"])
        with patch.object(ui.notifier, "post_webhook", side_effect=ui.notifier.NotifyError("The webhook answered 404")):
            res, data = self.post_hook({"op": "test"})
        self.assertEqual(res.status, 502)
        self.assertIn("404", data["error"])

    def test_watcher_announces_only_new_items_and_posts_them(self):
        ui.write_settings(self.root, {"notification_webhook": self.URL})
        tracker = ui.notifier.Tracker()
        self.assertEqual(self.server.notify_once(tracker), [])  # seeds with the job already waiting
        (self.root / ".orchestrator" / "jobs" / "20260923-bug-2.json").write_text(json.dumps(
            {"title": "Crash on launch", "type": "bug", "status": "failed"}))
        with patch.object(ui.notifier, "post_webhook") as post:
            events = self.server.notify_once(tracker)
            again = self.server.notify_once(tracker)
        self.assertEqual([e["path"] for e in events], ["#/jobs/20260923-bug-2"])
        self.assertEqual(again, [])
        self.assertEqual(post.call_count, 1)
        self.assertIn("Crash on launch", post.call_args[0][1]["text"])

    def test_watcher_tracks_but_sends_nothing_without_a_webhook(self):
        tracker = ui.notifier.Tracker()
        self.server.notify_once(tracker)
        (self.root / ".orchestrator" / "jobs" / "20260923-bug-2.json").write_text(json.dumps({"title": "X", "type": "bug", "status": "failed"}))
        with patch.object(ui.notifier, "post_webhook") as post:
            events = self.server.notify_once(tracker)
        self.assertEqual(len(events), 1)
        post.assert_not_called()

    def test_background_watcher_delivers_and_stops_with_the_server(self):
        ui.write_settings(self.root, {"notification_webhook": self.URL})
        with patch.object(ui.notifier, "post_webhook") as post:
            thread = self.server.start_notifier(interval=0.05)
            (self.root / ".orchestrator" / "jobs" / "20260923-bug-2.json").write_text(json.dumps({"title": "Boom", "type": "bug", "status": "failed"}))
            deadline = time.time() + 5
            while not post.called and time.time() < deadline:
                time.sleep(0.05)
        self.assertTrue(post.called)
        self.server._stopping.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def test_state_says_whether_the_webhook_is_on_without_revealing_it(self):
        _, state = self.request("GET", "/api/state")
        self.assertEqual(state["alerts"], {"webhook": False})
        ui.write_settings(self.root, {"notification_webhook": self.URL})
        _, state = self.request("GET", "/api/state")
        self.assertEqual(state["alerts"], {"webhook": True})
        self.assertNotIn("secret", json.dumps(state))

    def test_state_carries_the_inbox_items_the_browser_notifies_from(self):
        _, state = self.request("GET", "/api/state")
        self.assertEqual([(i["id"].split(":")[0], i["hash"]) for i in state["inbox"]], [("job", "#/jobs/20260922-bug-1")])


class TestCaseViewTests(ServerTestCase):
    JOB = "20260922-bug-1"

    def setUp(self):
        super().setUp()
        ui._TEST_SCAN_CACHE.clear()
        tests_dir = self.root / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_seat.py").write_text("def test_keeps_seat():\n    pass\n\n# TC-1-03\ndef test_other():\n    pass\n")
        path = self.root / ".orchestrator" / "jobs" / f"{self.JOB}.json"
        job = json.loads(path.read_text())
        job["plan"] = {"test_cases": [
            {"id": "TC-1-01", "area": "Lobby", "title": "Seat kept", "type": "unit", "expected": "Seat stays", "tests": ["test_keeps_seat"], "steps": ["a"]},
            {"id": "TC-1-02", "area": "Lobby", "title": "Rejoin flow", "type": "integration", "expected": "Rejoins", "tests": ["test_not_written_yet"]},
            {"id": "TC-1-03", "area": "Lobby", "title": "Marked by id", "type": "unit", "expected": "ok"},
            {"id": "TC-1-04", "area": "Lobby", "title": "No test", "type": "ui", "expected": "ok"},
            {"id": "TC-1-05", "area": "Device", "title": "Check on phone", "type": "manual", "expected": "ok"},
        ]}
        path.write_text(json.dumps(job))

    def test_job_detail_reports_each_cases_coverage(self):
        _, data = self.request("GET", f"/api/jobs/{self.JOB}")
        view = data["test_cases"]
        self.assertEqual({c["id"]: c["status"] for c in view["cases"]},
                         {"TC-1-01": "covered", "TC-1-02": "planned", "TC-1-03": "covered", "TC-1-04": "unassigned", "TC-1-05": "manual"})
        summary = view["summary"]
        self.assertEqual((summary["covered"], summary["planned"], summary["unassigned"], summary["manual"], summary["automated"]), (2, 1, 1, 1, 4))
        self.assertEqual(summary["covered_pct"], 50.0)
        # Cases saved with the older unit/integration/ui/manual names read as the eight standard categories.
        self.assertEqual({t: n for t, n in summary["by_type"].items() if n},
                         {"functionality": 2, "integration": 1, "user-interface": 1, "user-acceptance": 1})
        self.assertEqual(len(summary["by_type"]), 8)
        covered = next(c for c in view["cases"] if c["id"] == "TC-1-01")
        self.assertTrue(covered["found_in"][0].endswith("test_seat.py"))

    def test_job_without_cases_has_an_empty_view(self):
        path = self.root / ".orchestrator" / "jobs" / "20260923-feature-2.json"
        path.write_text(json.dumps({"title": "Old job", "type": "feature", "status": "planned"}))
        _, data = self.request("GET", "/api/jobs/20260923-feature-2")
        self.assertEqual(data["test_cases"]["cases"], [])
        self.assertIsNone(data["test_cases"]["summary"]["covered_pct"])

    def test_library_endpoint_reads_the_repos_case_files(self):
        lib = self.root / "docs" / "test-cases" / "lobby"
        lib.mkdir(parents=True)
        (lib / "TC-9-01.json").write_text(json.dumps({"id": "TC-9-01", "area": "Lobby", "title": "From library", "type": "unit", "expected": "ok", "tests": ["test_keeps_seat"]}))
        _, data = self.request("GET", "/api/test-cases")
        self.assertEqual([(c["id"], c["status"]) for c in data["cases"]], [("TC-9-01", "covered")])

    def test_scan_is_reused_between_requests(self):
        with patch.object(ui.test_case_lib, "scan_tests", wraps=ui.test_case_lib.scan_tests) as scan:
            self.request("GET", f"/api/jobs/{self.JOB}")
            self.request("GET", "/api/test-cases")
        self.assertEqual(scan.call_count, 1)

    def test_library_post_create_edit_and_delete(self):
        # Create
        res, data = self.request("POST", "/api/test-cases", body={
            "op": "create",
            "area": "Auth",
            "title": "Validate JWT token",
            "type": "unit",
            "priority": "high",
            "expected": "Returns active user payload",
            "tests": ["test_jwt_validation"],
        }, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertTrue(any(c["id"] == "TC-0-01" and c["title"] == "Validate JWT token" for c in data["cases"]))
        auth_file = self.root / "docs" / "test-cases" / "auth" / "TC-0-01.json"
        self.assertTrue(auth_file.exists())
        saved = json.loads(auth_file.read_text())
        self.assertEqual(saved["title"], "Validate JWT token")
        self.assertEqual(saved["priority"], "high")

        # Edit
        res, data = self.request("POST", "/api/test-cases", body={
            "op": "edit",
            "id": "TC-0-01",
            "area": "Security",
            "title": "Validate JWT and expiry",
            "expected": "Returns active user or error",
        }, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertFalse(auth_file.exists())
        sec_file = self.root / "docs" / "test-cases" / "security" / "TC-0-01.json"
        self.assertTrue(sec_file.exists())
        saved_sec = json.loads(sec_file.read_text())
        self.assertEqual(saved_sec["title"], "Validate JWT and expiry")
        self.assertEqual(saved_sec["area"], "Security")

        # Delete
        res, data = self.request("POST", "/api/test-cases", body={"op": "delete", "id": "TC-0-01"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertFalse(sec_file.exists())
        self.assertFalse(any(c["id"] == "TC-0-01" for c in data["cases"]))

    def test_library_post_validation_errors(self):
        res, data = self.request("POST", "/api/test-cases", body={"op": "create", "title": ""}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)
        self.assertIn("title and an expected result", data["error"])

        res, data = self.request("POST", "/api/test-cases", body={"op": "delete", "id": ""}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)
        self.assertIn("Which test case?", data["error"])


class DeliveryEndpointTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        ui._GH_CACHE.clear()

    def test_reports_receipts_ready_jobs_and_pipeline(self):
        delivered = self.root / ".orchestrator" / "output" / "delivery"
        delivered.mkdir(parents=True)
        (delivered / "20260922-bug-0.json").write_text(json.dumps({"status": "delivered", "job_id": "20260922-bug-0", "version": "2.1", "build": "40", "groups": "internal", "branch": "ai/issue-0"}))
        job = self.root / ".orchestrator" / "jobs" / "20260922-bug-1.json"
        data = json.loads(job.read_text())
        data["status"] = "review-needed"
        job.write_text(json.dumps(data))
        rows = [{"name": "CI", "status": "completed", "conclusion": "success", "url": "https://x/1", "headBranch": "main"}]
        with patch.object(ui.subprocess, "run", wraps=ui.subprocess.run) as run:
            real = ui.subprocess.run

            def fake(argv, *a, **k):
                if argv[0] == "gh":
                    return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows), stderr="")
                return real(argv, *a, **k)

            run.side_effect = fake
            res, out = self.request("GET", "/api/delivery")
        self.assertEqual(res.status, 200)
        self.assertEqual((out["testers"]["latest"]["version"], out["testers"]["latest"]["build"]), ("2.1", "40"))
        self.assertEqual([j["id"] for j in out["ready"]], ["20260922-bug-1"])
        self.assertEqual(out["pipeline"]["runs"][0]["tone"], "done")
        self.assertIsNone(out["live"])  # the fixture isn't a git repo

    def test_without_gh_the_pipeline_is_marked_unavailable(self):
        real = ui.subprocess.run

        def fake(argv, *a, **k):
            if argv[0] == "gh":
                raise FileNotFoundError("gh")
            return real(argv, *a, **k)

        with patch.object(ui.subprocess, "run", side_effect=fake):
            res, out = self.request("GET", "/api/delivery")
        self.assertEqual(res.status, 200)
        self.assertEqual(out["pipeline"], {"available": False, "runs": []})

    def test_gh_results_are_cached_between_requests(self):
        calls = []
        real = ui.subprocess.run

        def fake(argv, *a, **k):
            if argv[0] == "gh":
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
            return real(argv, *a, **k)

        with patch.object(ui.subprocess, "run", side_effect=fake):
            self.request("GET", "/api/delivery")
            self.request("GET", "/api/delivery")
        self.assertEqual(len(calls), 1)

    def test_invite_link_is_saved_validated_and_shown_for_sharing(self):
        res, _ = self.request("POST", "/api/config/firebase", body={"firebase_invite_url": "http://insecure.example/i/1"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)
        link = "https://appdistribution.firebase.dev/i/abc123"
        res, _ = self.request("POST", "/api/config/firebase", body={"firebase_app_id": "1:2:ios:a", "firebase_invite_url": link}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        _, out = self.request("GET", "/api/delivery")
        self.assertEqual(out["testers"]["invite_url"], link)
        _, config = self.request("GET", "/api/config")
        self.assertEqual(config["firebase"]["invite_url"], link)
        self.request("POST", "/api/config/firebase", body={"firebase_invite_url": ""}, headers=UI_HEADERS)
        _, out = self.request("GET", "/api/delivery")
        self.assertEqual(out["testers"]["invite_url"], "")

    def test_secrets_are_not_in_the_payload(self):
        ui.write_settings(self.root, {"firebase_app_id": "1:23:ios:abc", "firebase_service_account_path": "/secret/key.json", "firebase_tester_groups": "qa, friends"})
        _, out = self.request("GET", "/api/delivery")
        self.assertEqual(out["testers"]["groups"], ["qa", "friends"])
        self.assertNotIn("/secret/key.json", json.dumps(out))


class MeasureEndpointTests(ServerTestCase):
    def post(self, path, body):
        return self.request("POST", path, body=body, headers=UI_HEADERS)

    def setUp(self):
        super().setUp()
        self.post("/api/features", {"name": "Lobby"})

    def kpi(self, **extra):
        return self.post("/api/features/lobby/kpis", {"op": "add", "name": "Seat claims", "event": "seat_claimed", "target": 60, **extra})

    def test_provider_setup_never_echoes_the_key_and_blank_keeps_it(self):
        res, _ = self.post("/api/config/analytics", {"op": "set", "provider": "mixpanel", "key": "SECRET-TOKEN", "region": "eu"})
        self.assertEqual(res.status, 200)
        _, data = self.request("GET", "/api/analytics")
        self.assertEqual((data["provider"], data["provider_name"], data["key_set"], data["region"]), ("mixpanel", "Mixpanel", True, "eu"))
        self.assertNotIn("SECRET-TOKEN", json.dumps(data))
        self.post("/api/config/analytics", {"op": "set", "provider": "mixpanel", "key": "", "region": "us"})
        self.assertEqual(ui.read_settings(self.root)["analytics_key"], "SECRET-TOKEN")
        self.post("/api/config/analytics", {"op": "clear"})
        _, data = self.request("GET", "/api/analytics")
        self.assertEqual((data["provider"], data["key_set"]), ("", False))

    def test_set_requires_a_known_provider_and_a_key(self):
        self.assertEqual(self.post("/api/config/analytics", {"op": "set", "provider": "nope", "key": "k"})[0].status, 400)
        self.assertEqual(self.post("/api/config/analytics", {"op": "set", "provider": "posthog"})[0].status, 400)
        self.assertEqual(self.post("/api/config/analytics", {"op": "test"})[0].status, 400)

    def test_connection_test_uses_the_saved_key_and_regional_host(self):
        self.post("/api/config/analytics", {"op": "set", "provider": "amplitude", "key": "K1", "region": "eu"})
        with patch.object(ui.analytics, "send_test") as send:
            res, _ = self.post("/api/config/analytics", {"op": "test"})
        self.assertEqual(res.status, 200)
        self.assertEqual(send.call_args[0], ("amplitude", "K1", "https://api.eu.amplitude.com"))
        with patch.object(ui.analytics, "send_test", side_effect=ui.analytics.AnalyticsError("Amplitude rejected the key (401)")):
            res, data = self.post("/api/config/analytics", {"op": "test"})
        self.assertEqual(res.status, 502)
        self.assertIn("401", data["error"])

    def test_kpi_lifecycle_through_the_api(self):
        _, data = self.kpi()
        kpi = data["features"][0]["kpis"][0]
        self.assertEqual((kpi["event"], kpi["status"]["state"]), ("seat_claimed", "no-data"))
        _, data = self.post("/api/features/lobby/kpis", {"op": "measure", "kpi": kpi["id"], "value": 72, "decision": "keep", "note": "week 1"})
        self.assertEqual(data["features"][0]["kpis"][0]["status"]["state"], "on-track")
        _, data = self.post("/api/features/lobby/kpis", {"op": "delete", "kpi": kpi["id"]})
        self.assertEqual(data["features"][0]["kpis"], [])

    def test_kpi_validation_errors(self):
        self.assertEqual(self.kpi(event="Bad Event")[0].status, 400)
        self.assertEqual(self.post("/api/features/lobby/kpis", {"op": "measure", "kpi": "ghost", "value": 1})[0].status, 404)
        self.assertEqual(self.post("/api/features/nope/kpis", {"op": "add", "name": "x", "event": "ok_event"})[0].status, 404)

    def test_tracking_plan_is_written_into_the_repo(self):
        self.kpi()
        res, data = self.post("/api/analytics/plan", {})
        self.assertEqual((res.status, data["path"]), (200, "docs/analytics/tracking-plan.md"))
        self.assertIn("`seat_claimed`", (self.root / data["path"]).read_text())

    def test_new_job_for_a_feature_with_kpis_must_emit_their_events(self):
        self.kpi()
        self.post("/api/config/analytics", {"op": "set", "provider": "posthog", "key": "k"})
        argv = ui.build_new_job({"type": "feature", "summary": "Add rematch", "feature": "lobby"}, self.root)
        text = Path(argv[argv.index("--spec-file") + 1]).read_text()
        self.assertIn("`seat_claimed`", text)
        self.assertIn("PostHog integration", text)


class HealthEndpointTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        ui._GH_CACHE.clear()
        ui._TEST_SCAN_CACHE.clear()

    def get_health(self):
        real = ui.subprocess.run

        def fake(argv, *a, **k):
            if argv[0] == "gh":
                raise FileNotFoundError("gh")
            return real(argv, *a, **k)

        with patch.object(ui.subprocess, "run", side_effect=fake):
            return self.request("GET", "/api/health")

    def items(self, data):
        return {i["id"]: i for i in data["items"]}

    def test_a_bare_project_is_told_what_is_missing_in_priority_order(self):
        res, data = self.get_health()
        self.assertEqual(res.status, 200)
        items = self.items(data)
        self.assertEqual(data["next"], "prd")
        self.assertEqual((items["prd"]["status"], items["instructions"]["status"], items["repo"]["status"], items["delivery"]["status"]),
                         ("todo", "todo", "todo", "todo"))
        self.assertEqual(data["stage"], "Building")  # the fixture has one job

    def test_prd_platforms_features_and_tests_are_read_from_the_project(self):
        (self.root / "docs" / "product").mkdir(parents=True)
        text = prd_mod.replace_section(prd_mod.template("X"), "pitch", "A thing.\n\nBuilt for: iOS app, Backend / API")
        text = prd_mod.replace_section(prd_mod.replace_section(text, "who", "Friends"), "features", "- One")
        (self.root / "docs" / "product" / "prd.md").write_text(text)
        (self.root / "AGENTS.md").write_text("# rules")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
        ui.feature_store.create(self.root / ".orchestrator", "Lobby")
        _, data = self.get_health()
        items = self.items(data)
        self.assertEqual((items["prd"]["status"], items["instructions"]["status"], items["tests"]["status"]), ("ok", "ok", "ok"))
        self.assertEqual(items["platforms"]["detail"], "iOS app, Backend / API")
        self.assertEqual(items["features"]["status"], "warn")  # the fixture's open job isn't in the feature

    def test_features_with_work_but_no_kpis_are_called_out(self):
        rt = self.root / ".orchestrator"
        ui.feature_store.create(rt, "Lobby")
        path = rt / "jobs" / "20260922-bug-1.json"
        job = json.loads(path.read_text())
        job["feature"] = "lobby"
        path.write_text(json.dumps(job))
        _, data = self.get_health()
        self.assertEqual(self.items(data)["kpis"]["status"], "todo")
        self.assertIn("1 feature", self.items(data)["kpis"]["detail"])

    def test_undecided_platforms_from_a_new_project_prd_are_flagged(self):
        (self.root / "docs" / "product").mkdir(parents=True)
        (self.root / "docs" / "product" / "prd.md").write_text(ui.new_project.render_prd(
            {"name": "X", "pitch": "p", "audience": "a", "problem": "q", "features": "one", "platform": ui.new_project.RECOMMEND}))
        _, data = self.get_health()
        self.assertEqual(self.items(data)["platforms"]["status"], "warn")


class NavigationDrawerTests(unittest.TestCase):
    def run_drawer(self, scenario):
        script = """
const assert = require('node:assert/strict');
class Element {
  constructor(name) {
    this.name = name; this.listeners = {}; this.attrs = {}; this.inert = false;
    this.hidden = false; this.disabled = false; this.style = {overflow: 'auto'};
    const classes = new Set();
    this.classList = {contains: c => classes.has(c), add: c => classes.add(c),
      remove: c => classes.delete(c), toggle: (c, on) => on ? classes.add(c) : classes.delete(c)};
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  emit(type, event = {}) { for (const fn of this.listeners[type] || []) fn(event); }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute(k) { delete this.attrs[k]; }
  focus() { document.activeElement = this; }
  getClientRects() { return this.hidden ? [] : [{}]; }
  closest(selector) {
    if (selector.includes('[data-action]') && this.attrs['data-action']) return this;
    if (selector.includes('#lock-btn') && this.name === 'lock') return this;
    if (selector.includes('#search-btn') && this.name === 'search') return this;
    return null;
  }
}
const app = new Element('app'), sidebar = new Element('sidebar');
const toggle = new Element('toggle'), close = new Element('close'), backdrop = new Element('backdrop');
const main = new Element('main'), plus = new Element('plus'), header = new Element('header');
const link = new Element('link'), actionBtn = new Element('action');
actionBtn.attrs['data-action'] = 'true';
const lockBtn = new Element('lock'), searchBtn = new Element('search');
const elements = {'.app': app, '#sidebar': sidebar, '#nav-toggle': toggle,
  '#nav-close': close, '#sidebar-backdrop': backdrop, '#lock-btn': lockBtn, '#search-btn': searchBtn};
const document = new Element('document');
document.body = new Element('body');
document.querySelector = selector => {
  if (selector === 'dialog[open]') return document.dialogOpen || null;
  return elements[selector] || null;
};
document.querySelectorAll = () => [main, plus, header];
sidebar.items = [close, link];
sidebar.querySelectorAll = () => sidebar.items;
sidebar.contains = el => sidebar.items.includes(el);
const media = new Element('media'); media.matches = true;
class MutationObserver {
  constructor(cb) { this.cb = cb; }
  observe() {}
  trigger() { this.cb(); }
}
const window = new Element('window'); window.matchMedia = () => media;
window.MutationObserver = MutationObserver;
require(process.argv[1]);
const controller = globalThis.NavigationDrawer.mount({document, window});
function key(key, shiftKey = false) {
  const event = {key, shiftKey, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
  document.emit('keydown', event);
  return event;
}
""" + scenario
        result = subprocess.run(
            ["node", "-e", script, str(PACKAGE_ROOT / "orchestrator/web/static/navigation.js")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_drawer_blocks_background_and_restores_focus_and_scroll_on_dismissal(self):
        self.run_drawer("""
assert.equal(sidebar.inert, true);
toggle.emit('click');
assert.equal(sidebar.inert, false);
assert.equal(sidebar.attrs['aria-modal'], 'true');
assert.equal(toggle.attrs['aria-expanded'], 'true');
assert.equal(backdrop.hidden, false);
assert.equal(main.inert, true);
assert.equal(plus.inert, true);
assert.equal(document.body.style.overflow, 'hidden');
assert.equal(document.activeElement, close);
assert.equal(key('Escape').defaultPrevented, true);
assert.equal(sidebar.inert, true);
assert.equal(backdrop.hidden, true);
assert.equal(main.inert, false);
assert.equal(plus.inert, false);
assert.equal(document.body.style.overflow, 'auto');
assert.equal(document.activeElement, toggle);
toggle.emit('click'); backdrop.emit('click');
assert.equal(toggle.attrs['aria-expanded'], 'false');
""")

    def test_drawer_keeps_keyboard_focus_inside_and_closes_after_navigation(self):
        self.run_drawer("""
toggle.emit('click');
close.focus(); assert.equal(key('Tab', true).defaultPrevented, true);
assert.equal(document.activeElement, link);
assert.equal(key('Tab').defaultPrevented, true);
assert.equal(document.activeElement, close);
window.emit('hashchange');
assert.equal(sidebar.inert, true);
assert.equal(main.inert, false);
toggle.emit('click');
const target = {closest: selector => selector.includes('a[href]') ? link : null};
sidebar.emit('click', {target});
assert.equal(backdrop.hidden, true);
""")

    def test_resizing_to_desktop_clears_modal_state_and_locked_sessions_cannot_open(self):
        self.run_drawer("""
toggle.emit('click');
media.matches = false; media.emit('change');
assert.equal(sidebar.inert, false);
assert.equal(main.inert, false);
assert.equal(backdrop.hidden, true);
assert.equal(sidebar.attrs['aria-modal'], undefined);
assert.equal(document.body.style.overflow, 'auto');
toggle.emit('click'); assert.equal(backdrop.hidden, true);
media.matches = true; media.emit('change');
assert.equal(sidebar.inert, true);
app.classList.add('session-locked');
toggle.emit('click'); assert.equal(backdrop.hidden, true);
""")

    def test_selecting_the_current_configuration_page_dismisses_the_drawer(self):
        self.run_drawer("""
toggle.emit('click');
// Selecting the current route emits no hashchange; the click still dismisses navigation.
const target = {closest: selector => selector.includes('[data-config-route]') ? link : null};
sidebar.emit('click', {target});
assert.equal(backdrop.hidden, true);
assert.equal(main.inert, false);
""")

    def test_selecting_project_or_action_buttons_dismisses_the_drawer(self):
        self.run_drawer("""
sidebar.items = [close, link, actionBtn, lockBtn, searchBtn];
toggle.emit('click');
assert.equal(backdrop.hidden, false);
sidebar.emit('click', {target: actionBtn});
assert.equal(backdrop.hidden, true);

toggle.emit('click');
sidebar.emit('click', {target: lockBtn});
assert.equal(backdrop.hidden, true);

toggle.emit('click');
sidebar.emit('click', {target: searchBtn});
assert.equal(backdrop.hidden, true);

toggle.emit('click');
sidebar.emit('change', {target: {id: 'project-select'}});
assert.equal(backdrop.hidden, true);
""")

    def test_open_dialog_prevents_escape_dismissal(self):
        self.run_drawer("""
toggle.emit('click');
assert.equal(backdrop.hidden, false);
document.dialogOpen = new Element('dialog');
assert.equal(key('Escape').defaultPrevented, false);
assert.equal(backdrop.hidden, false);
document.dialogOpen = null;
assert.equal(key('Escape').defaultPrevented, true);
assert.equal(backdrop.hidden, true);
""")


class ConnectionLogFrontendTests(unittest.TestCase):
    def run_node_test(self, scenario):
        script = """
const fs = require('fs');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[1], 'utf8');
const match = code.match(/const ConnectionLog = \\(\\(\\) => \\{[\\s\\S]*?\\n\\}\\)\\(\\);/);
assert(match, 'ConnectionLog not found in app.js');

const storage = {};
const localStorage = {
  getItem: k => storage[k] || null,
  setItem: (k, v) => { storage[k] = String(v); },
  removeItem: k => { delete storage[k]; }
};
const navigator = { onLine: true, userAgent: 'test', connection: { effectiveType: '4g' } };
const document = { visibilityState: 'visible', hidden: false, addEventListener: () => {} };
const window = { addEventListener: () => {} };

let apiCalls = [];
let apiStatusToThrow = null;
async function api(path, opts) {
  apiCalls.push({ path, opts });
  if (apiStatusToThrow) {
    const err = new Error('HTTP error');
    err.status = apiStatusToThrow;
    throw err;
  }
  return { ok: true };
}

eval(match[0].replace('const ConnectionLog =', 'globalThis.ConnectionLog ='));
""" + scenario
        result = subprocess.run(
            ["node", "-e", script, str(PACKAGE_ROOT / "orchestrator/web/static/app.js")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_connection_log_adds_records_and_caps_at_max(self):
        self.run_node_test("""
ConnectionLog.add('test_event', { detail_key: 'val' });
let stored = JSON.parse(storage['orchestrator_connection_log']);
assert.equal(stored.length, 1);
assert.equal(stored[0].kind, 'test_event');
assert.equal(stored[0].online, true);
assert.equal(stored[0].visible, 'visible');
assert.equal(stored[0].connection, '4g');
assert.equal(stored[0].detail_key, 'val');

for (let i = 0; i < 250; i++) {
  ConnectionLog.add(`event_${i}`);
}
stored = JSON.parse(storage['orchestrator_connection_log']);
assert.equal(stored.length, 200);
assert.equal(stored[0].kind, 'event_50');
assert.equal(stored[199].kind, 'event_249');
""")

    def test_connection_log_flush_and_handles_404(self):
        self.run_node_test("""
(async () => {
  for (let i = 0; i < 150; i++) ConnectionLog.add(`event_${i}`);
  await ConnectionLog.flush();
  assert.equal(apiCalls.length, 1);
  assert.equal(apiCalls[0].path, 'client-log');
  assert.equal(apiCalls[0].opts.body.events.length, 100);
  let stored = JSON.parse(storage['orchestrator_connection_log']);
  assert.equal(stored.length, 50);

  apiStatusToThrow = 404;
  await ConnectionLog.flush();
  stored = JSON.parse(storage['orchestrator_connection_log']);
  assert.equal(stored.length, 0);
})();
""")

    def test_last_login_method_persistence(self):
        script = """
const fs = require('fs');
const assert = require('node:assert/strict');
const appJs = fs.readFileSync(process.argv[1], 'utf8');

const storage = {};
globalThis.localStorage = {
  getItem: (k) => storage[k] || null,
  setItem: (k, v) => { storage[k] = String(v); },
  removeItem: (k) => { delete storage[k]; },
};
globalThis.document = { cookie: '' };
globalThis.window = { firebase: { auth: () => ({ currentUser: null }) } };

const fnMatch = appJs.match(/(const LAST_LOGIN_KEY = [\\s\\S]*?function setLastLoginMethod[\\s\\S]*?\\n\\})/);
assert.ok(fnMatch, 'helpers found');
eval(fnMatch[0]);

assert.equal(getLastLoginMethod(), '');

setLastLoginMethod('google');
assert.equal(storage['orchestrator_last_login_method'], 'google');
assert.ok(document.cookie.includes('orchestrator_last_login_method=google'));
assert.equal(getLastLoginMethod(), 'google');

delete storage['orchestrator_last_login_method'];
assert.equal(getLastLoginMethod(), 'google');

document.cookie = '';
assert.equal(getLastLoginMethod(), '');
window.firebase.auth = () => ({ currentUser: { providerData: [{ providerId: 'github.com' }] } });
assert.equal(getLastLoginMethod(), 'github');

setLastLoginMethod('token');
assert.equal(getLastLoginMethod(), 'token');
assert.equal(storage['orchestrator_last_login_method'], 'token');
"""
        result = subprocess.run(
            ["node", "-e", script, str(PACKAGE_ROOT / "orchestrator/web/static/app.js")],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)



class AccessibilityStaticTests(unittest.TestCase):
    STATIC = PACKAGE_ROOT / "orchestrator" / "web" / "static"

    @classmethod
    def setUpClass(cls):
        cls.html = (cls.STATIC / "index.html").read_text()
        cls.css = (cls.STATIC / "style.css").read_text()
        cls.js = (cls.STATIC / "app.js").read_text()

    def test_no_duplicate_ids_in_the_page_shell(self):
        import re
        ids = re.findall(r'\bid="([^"]+)"', self.html)
        self.assertEqual([i for i in set(ids) if ids.count(i) > 1], [])

    def test_page_has_language_skip_link_and_a_focusable_heading(self):
        self.assertIn('<html lang="en">', self.html)
        self.assertIn('id="skip-link"', self.html)
        self.assertIn('<h1 id="page-title" tabindex="-1">', self.html)

    def test_the_whole_view_is_not_a_live_region(self):
        self.assertNotIn('id="view" class="view" aria-live', self.html)

    def test_phone_drawer_reuses_the_full_sidebar(self):
        from html.parser import HTMLParser

        class SidebarParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.sidebar = False
                self.links = []
                self.controls = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "aside" and attrs.get("id") == "sidebar":
                    self.sidebar = True
                if self.sidebar:
                    if tag == "a":
                        self.links.append(attrs.get("href"))
                    if attrs.get("id"):
                        self.controls.append(attrs["id"])

            def handle_endtag(self, tag):
                if tag == "aside":
                    self.sidebar = False

        sidebar = SidebarParser()
        sidebar.feed(self.html)
        for href in ("#/", "#/activity", "#/product", "#/tests", "#/delivery", "#/git", "#/devlogs", "#/checkup", "#/ux-review",
                     "#/measure", "#/docs", "#/readiness", "#/help"):
            self.assertIn(href, sidebar.links)
        for href in ("#/connections", "#/projects"):  # set once: under Settings (Configuration), not the sidebar
            self.assertNotIn(href, sidebar.links)
        for control in ("project-select", "search-btn", "configuration-menu-trigger", "notify-btn", "lock-btn"):
            self.assertIn(control, sidebar.controls)

    def test_copy_stays_plain_and_consistent(self):
        """Words casual users understand, one case style, real plurals (docs/web-ui-github-audit.md #21)."""
        import re
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        app = (static / "app.js").read_text()
        config = (static / "configuration.js").read_text()
        for jargon in ("Next execution", "Pending execution", "Decomposed", "Parallel subtask", "AI Worker active",
                       "Your action needed", "Phase: ", "Tracked Projects", "Open Dashboard", "PRD · ", "Something went wrong"):
            self.assertNotIn(jargon, app, jargon)
        counted = r"\b(?:job|test|suite|file|feature|build|item|case|KPI|machine|model|run|task|change|project)\(s\)"
        self.assertEqual(re.findall(counted, re.sub(r"//[^\n]*", "", app)), [], "write the plural, not (s)")
        self.assertNotIn("confirm(", app, "use formDialog with a specific verb, not OK/Cancel")
        labels = re.findall(r'label: "([^"]+)"', config)
        title_case = [l for l in labels if re.search(r" [A-Z][a-z]", l) and not re.search(r"AI|Firebase|Xcode|Slack|Orchestrator|API", l)]
        self.assertEqual(title_case, [], "Configuration labels use sentence case")

    def test_design_tokens_hold(self):
        """The design system's tokens are the only source of sizes, layers and colours (docs/web-ui-github-audit.md #20)."""
        import re
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        css = (static / "style.css").read_text()
        scripts = "".join((static / name).read_text() for name in ("app.js", "configuration.js"))
        defined = set(re.findall(r"(--[a-z0-9-]+):", css))
        used = set(re.findall(r"var\((--[a-z0-9-]+)", css + scripts))
        self.assertEqual(used - defined, set(), "every var(--x) must be defined")
        self.assertEqual(re.findall(r"z-index: ?\d", css), [], "use the --z-* layers")
        self.assertEqual(re.findall(r"min-height: ?(?:28|32|36|38|40|44|48)px", css), [], "use the --control-* heights")
        self.assertLessEqual(len(set(re.findall(r"box-shadow:[^;]+", css))), 10, "use --shadow, --shadow-2, --shadow-3")
        # A ratchet: inline styles in app.js may only go down. Lower this number when you remove one.
        self.assertLessEqual((static / "app.js").read_text().count('style="'), 62)

    def test_brand_mark_uses_app_icon(self):
        self.assertIn('.brand-mark {', self.css)
        self.assertIn('url("icon-192.png")', self.css)
        block = self.css[self.css.index('.brand-mark {'):self.css.index('.project-switch {')]
        self.assertNotIn('background: var(--accent);', block)

    def test_badges_use_ink_tokens_not_hardcoded_white(self):
        for selector in (".badge {", ".pill-badge.needs-badge {", ".pill-badge.active-badge {"):
            block = self.css[self.css.index(selector):][:260].split("}")[0]
            self.assertNotIn("#fff", block, selector)

    def test_every_form_field_the_ui_builds_for_chat_has_a_name(self):
        self.assertIn('aria-label="Your question about this job"', self.js)

    def test_connection_loss_is_a_sticky_message_over_the_page_that_clears_itself(self):
        self.assertNotIn("conn-banner", self.html + self.js)  # no separate banner: it is one of the overlay messages
        self.assertIn("pollFailures >= 2", self.js)
        self.assertIn("Can't reach Orchestrator. Check that it's running and that you're online.", self.js)
        refresh = self.js[self.js.index("async function refreshState()"):][:3600]
        self.assertIn('{ id: "connection", sticky: true }', refresh)  # stays until it is over
        self.assertIn('clearMessage("connection")', refresh)  # and goes when the server answers again
        self.assertIn('"Back online."', refresh)

    def test_every_message_goes_through_one_overlay_at_the_top_of_the_page(self):
        self.assertIn('<script src="messages.js">', self.html)
        self.assertLess(self.html.index("messages.js"), self.html.index("app.js"))
        self.assertIn('id="messages" class="messages" popover="manual" role="region" aria-label="Messages"', self.html)
        self.assertNotIn('id="toast"', self.html)
        self.assertNotIn(".toast", self.css)
        toast = self.js[self.js.index("function toast("):][:400]
        self.assertIn("notify(level", toast)
        self.assertIn("Errors.explain(message)", toast)  # errors still say what to do about them
        css = self.css[self.css.index(".messages {"):]
        for part in ("position: fixed", "top: calc(var(--space-4) + env(safe-area-inset-top))", "z-index: var(--z-toast)", "pointer-events: none", "prefers-reduced-motion"):
            self.assertIn(part, css, part)
        for kind in ("success", "info", "warning", "error"):
            self.assertIn(f".msg-{kind} {{ --kind:", css)  # one colour per kind, from the design tokens
        self.assertNotIn("#fff", css[:css.index("prefers-reduced-motion")])  # ink comes from tokens

    def test_validation_prompts_are_warnings_not_errors(self):
        for text in ("Wait for the upload to finish.", "Create a feature first.", "Pick at least one platform"):
            line = next(l for l in self.js.splitlines() if text in l and "toast(" in l)
            self.assertIn('"warning")', line, text)

    def test_messages_in_the_app_are_short_and_plain(self):
        import re
        for text in re.findall(r'toast\("([^"]+)"', self.js):
            self.assertLessEqual(len(text), 110, text)  # a message is a sentence or two, not a paragraph
            self.assertNotRegex(text, r"\b(undefined|null|NaN|Exception|Traceback)\b", text)

    def test_setup_button_only_floats_on_the_pages_about_getting_started(self):
        self.assertIn('["home", "checkup"].includes(current.page)', self.js)

    def test_home_warns_when_jobs_cannot_run(self):
        self.assertIn("No machine set up: jobs can't run yet", self.js)
        self.assertIn("No model selected: jobs can't run yet", self.js)

    def test_help_covers_every_page_it_names_and_the_undo_window(self):
        start = self.js.index("pages.help = async")
        help_page = self.js[start:self.js.index("pages.devlogs = async")]
        for route in ("#/tests", "#/delivery", "#/measure", "#/checkup", "#/readiness", "#/projects", "#/activity", "#/config"):
            self.assertIn(f'"{route}"', help_page, route)
        self.assertIn('href="#/docs"', help_page)
        self.assertIn("10 seconds", help_page)
        for page in ("features", "tests", "delivery", "measure", "checkup"):
            self.assertIn(f"pages.{page} = async", self.js)

    def test_message_colours_use_ink_tokens(self):
        block = self.css[self.css.index(".msg-icon {"):][:260]
        self.assertIn("color: var(--panel)", block)  # the icon's ink follows the theme
        self.assertNotIn("#fff", block)

    def test_command_palette_is_an_accessible_dialog_with_keyboard_shortcuts(self):
        self.assertIn('id="palette" role="dialog" aria-modal="true"', self.html)
        self.assertIn('role="combobox"', self.html)
        self.assertIn('role="listbox"', self.html)
        self.assertIn('<script src="palette.js">', self.html)
        self.assertIn('e.key === "k"', self.js)
        self.assertIn('e.key === "/"', self.js)
        self.assertIn('aria-activedescendant', self.js)

    def test_sidebar_tools_are_grouped_by_purpose(self):
        for label in ("Build &amp; ship", "Review", "Settings"):
            self.assertIn(f'<span class="label">{label}</span>', self.html)

    def test_configuration_uses_plain_names_and_keeps_the_old_term_in_the_description(self):
        config = (self.STATIC / "configuration.js").read_text()
        for label, old in (("Machines", "fleet"), ("Tester builds (Firebase)", "firebase app distribution")):
            line = next(l for l in config.splitlines() if f'label: "{label}"' in l)
            self.assertIn(old, line.lower(), label)
        app = (self.STATIC / "app.js").read_text()
        checks = app[app.index("const READINESS_CHECKS"):app.index("pages.readiness = async")]
        for label, old in (("Tools and logins", "prerequisite audit"), ("Orchestrator health check", "self-tests")):
            self.assertIn(old, checks[checks.index(label):].split("]")[0].lower(), label)
        for jargon in ('label: "Machine Fleet"', 'label: "Prerequisite Audit"', 'label: "Self-Tests"'):
            self.assertNotIn(jargon, config)

    def test_saving_a_webhook_or_analytics_key_tests_it_straight_away(self):
        self.assertIn('body: {op: "test"}', self.js)
        self.assertIn("Saved, but the test message failed", self.js)
        self.assertIn("Saved, but the test event failed", self.js)

    def test_a_kpi_that_is_behind_offers_a_prefilled_improvement_job(self):
        fn = self.js[self.js.index("function improveKpiSummary"):self.js.index("pages.measure = async")]
        for part in ("k.event", "k.target", "last?.decision", "how we will know it worked"):
            self.assertIn(part, fn)
        self.assertIn('st.state === "behind" ? `<a class="btn small primary"', self.js)  # only when behind: one obvious next step per KPI

    def test_toasts_explain_errors_and_alert_setup_is_reachable(self):
        self.assertIn("Errors.explain(message)", self.js)
        self.assertIn('<script src="errors.js">', self.html)
        self.assertIn("Notify me when done", self.html + self.js)  # browser alerts
        config = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "configuration.js").read_text()
        self.assertIn("Slack &amp; chat alerts", config)  # the webhook card
        self.assertIn("ConfigurationPages.chatCard(hook)", self.js)  # shown on Connections, for owners

    def test_there_is_no_separate_inbox_page_and_a_job_waiting_on_you_is_just_a_job_in_the_list(self):
        self.assertNotIn('data-route="inbox"', self.html)
        self.assertNotIn("pages.inbox", self.js)
        self.assertNotIn('"#/inbox"', self.js)
        self.assertIn('id="inbox-badge"', self.html.split('data-route="home"')[1].split("</a>")[0])  # the count lives on Home
        home = self.js[self.js.index("pages.home = async"):self.js.index("function jobHeaderActions")]
        self.assertNotIn("waitingSectionHtml", self.js)  # no second list above the jobs
        self.assertEqual(home.count('class="job-table"'), 1)
        self.assertIn('needs_you: ["Needs you"', home)  # a filter, not a separate section
        self.assertIn('all: ["All"', home)
        row = self.js[self.js.index("function jobTableRow"):self.js.index("function elapsed")]
        self.assertIn('<a class="job-table-row"', row)  # the whole row is one link, with the hover hint
        self.assertIn("row-hover-hint", row)
        self.assertNotIn("<button", row)  # no button on the row: the next step is on the job page
        self.assertIn('{ key: "status", dir: "asc" }', self.js)  # needs-you jobs sort first by default
        self.assertNotIn("pages.jobs", self.js)  # one jobs list, on Home
        self.assertNotIn("moreActionsMenu", self.js)  # nothing hidden in a Home menu that a page already has

    def test_home_job_table_shows_type_column_and_mobile_layout_keeps_date_sole_column(self):
        row = self.js[self.js.index("function jobTableRow"):self.js.index("function elapsed")]
        self.assertIn('class="col-type"', row)
        self.assertIn('class="job-mobile-info"', row)
        self.assertIn('class="job-type-tag"', row)
        self.assertIn('class="col-status"', row)
        self.assertIn('class="col-date"', row)

        home = self.js[self.js.index("pages.home = async"):self.js.index("function jobHeaderActions")]
        self.assertIn('class="col-type sort-head"', home)
        self.assertIn('data-sort="type"', home)

        sort_fn = self.js[self.js.index("function sortJobs"):self.js.index("const sortArrow")]
        self.assertIn('key === "type"', sort_fn)

        css = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "style.css").read_text()
        self.assertIn(".job-table-header .col-type", css)
        self.assertIn(".job-table-row .col-type", css)
        self.assertIn(".job-table-row .job-mobile-info", css)

    def test_projects_page_start_and_add_project_buttons_share_row_equally(self):
        projects = self.js[self.js.index('pages.projects = async'):self.js.index('pages.run = async')]
        self.assertIn('class="projects-actions"', projects)
        self.assertIn('href="#/new-project"', projects)
        self.assertIn('id="add-project-btn"', projects)

        css = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "style.css").read_text()
        self.assertIn(".projects-actions", css)
        self.assertIn("grid-template-columns: 1fr 1fr", css)

    def test_signing_in_shows_a_loading_screen_and_background_refreshes_leave_it_alone(self):
        js = self.js
        self.assertIn("function showSigningIn", js)
        self.assertIn(".signing-in", (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "style.css").read_text())
        refresh = js[js.index("async function refreshState()"):][:200]
        self.assertIn("if (signingIn) return;", refresh)  # the 5 s poll must not redraw the gate mid sign-in
        self.assertIn("if (signingIn) return;", js[js.index("async function route()"):][:200])
        sso = js[js.index("const wireSso"):js.index("$(\"#redirect-fallback\")")]  # the provider buttons
        fallback = js[js.index("$(\"#redirect-fallback\")"):js.index("wireSso(\"#google-signin-btn\"")]
        self.assertLess(sso.index("signingIn = true"), sso.index("signInWithPopup"))  # set before the provider window opens
        self.assertLess(sso.index("showSigningIn("), sso.index("afterProviderSignIn("))  # loading screen before the token exchange
        self.assertEqual(sso.count("endSigningIn()"), 1)  # on failure here; on success in unlockWith, so it can never spin forever
        unlock = js[js.index("async function unlockWith"):][:400]
        self.assertIn("endSigningIn()", unlock)
        after = js[js.index("async function afterProviderSignIn"):js.index("async function unlockWith")]
        self.assertIn("api(\"auth\"", after)
        self.assertIn("unlockWith(", after)
        # A blocked pop-up is explained, not turned into a full-page redirect: that flow dies with "missing initial state" in
        # browsers that partition storage between the site and Firebase's auth domain. It is offered, opt-in, instead.
        self.assertNotIn("signInWithRedirect", sso)
        self.assertIn("Allow pop-ups for this site", sso)
        self.assertIn('id="redirect-fallback"', js)
        self.assertIn("signInWithRedirect", fallback)  # only when asked for, from the fallback link
        self.assertIn("initial state", js[js.index("Redirect sign-in notice"):][:600])  # and that error, if it reaches the app, is explained
        self.assertIn("pendingRedirect", js)  # coming back from a redirect shows loading, not the options
        self.assertIn("showSigningIn(\"Unlocking…\")", js)  # the token form too

    def test_what_the_home_menu_held_lives_on_the_page_it_belongs_to(self):
        self.assertIn('Run all tests', self.js)  # Tests
        self.assertIn('Expand coverage', self.js)
        self.assertNotIn('["Build", act("build")', self.js)
        readiness = self.js[self.js.index("const READINESS_CHECKS"):self.js.index("pages.help = async")]
        self.assertIn('Will this computer build it?', readiness)  # Readiness: probes
        for action in ("check", "check_config", "worker_check", "test"):  # and every environment check, in one place
            self.assertIn(f'["{action}", ', readiness)
        self.assertIn('href="#/readiness"', self.js[self.js.index("pages.checkup = async"):])  # Check-up points there
        self.assertIn('act("distribute")', self.js)  # Delivery
        self.assertIn('act("logs_pull")', self.js)  # Device logs
        self.assertIn("Open full console", self.html + self.js)

    def test_test_menu_actions_and_button_widths(self):
        tests_src = self.js[self.js.index("pages.tests = async"):self.js.index("pages.git = async")]
        self.assertIn("Run all tests", tests_src)
        self.assertIn("Expand coverage", tests_src)
        self.assertNotIn("moreMenu", tests_src)
        self.assertNotIn('act("build")', tests_src)
        self.assertNotIn("Refresh list", tests_src)
        self.assertIn('body[data-page="tests"] .topbar-actions > .test-run-all-btn { flex: 2 1 0;', self.css)
        self.assertIn('body[data-page="tests"] .topbar-actions > .test-expand-coverage-btn {', self.css)
        self.assertIn('flex: 1 1 0;', self.css)
        self.assertIn("cov.total_lines", tests_src)
        self.assertIn("cov.total_lines.toLocaleString()", tests_src)
        self.assertIn("Coverage target (%)", tests_src)
        self.assertIn(".input-with-suffix", self.css)
        self.assertIn(".input-suffix", self.css)

    def test_run_screen_stop_button_in_same_row_as_title(self):
        # Stop button on in-progress screens is placed on the right side, same row as the title
        self.assertIn('.topbar:has(> .topbar-actions > [data-stop])', self.css)
        self.assertIn('body[data-page="run"] .topbar', self.css)
        self.assertIn('"title actions"', self.css)
        self.assertIn('.topbar:has(> .topbar-actions > [data-stop]) .topbar-actions', self.css)
        self.assertIn('grid-area: actions;', self.css)

    def test_stop_job_confirmation_modal_spinner_and_toast(self):
        # Tapping stop has a confirmation modal using formDialog
        self.assertIn("async function stopJob(", self.js)
        self.assertIn('formDialog(title, body, okLabel, { danger: true, compact: true })', self.js)
        self.assertIn('"Stop this job?"', self.js)
        self.assertIn('"Stop job"', self.js)
        # Shows loading spinner overlay over job card while in progress
        self.assertIn('live-run-overlay', self.js)
        self.assertIn('role="status"', self.js)
        self.assertIn('aria-busy="true"', self.js)
        self.assertIn('Stopping…', self.js)
        # Toast confirms once stopped
        self.assertIn('toast(hasJob ? "Job stopped" : "Run stopped")', self.js)
        # CSS rules for card overlay and spinner
        self.assertIn('.live-run { position: relative;', self.css)
        self.assertIn('.live-run.stopping { pointer-events: none; }', self.css)
        self.assertIn('.live-run-overlay {', self.css)
        self.assertIn('.live-run-overlay .spinner {', self.css)


    def test_back_button_chevron_beside_title_without_text(self):
        # Back button is a chevron icon only, placed on the same row to the left of the title
        self.assertIn('<div class="topbar-title-row">', self.html)
        self.assertIn('<a class="back-link" id="back-link" href="#/" aria-label="Back" title="Back" hidden><svg class="icon" aria-hidden="true"><use href="#i-chevron"/></svg></a>', self.html)
        self.assertNotIn('>Back<', self.html)
        self.assertIn('.topbar-title-row {', self.css)
        self.assertIn('.back-link .icon {', self.css)
        self.assertIn('width: 22px;', self.css)
        self.assertIn('height: 22px;', self.css)
        self.assertIn('stroke-width: 2.5;', self.css)
        self.assertIn('transform: rotate(180deg);', self.css)

    def test_product_topbar_buttons_color_background_and_centered(self):
        # Product topbar buttons (Draft, Import) use color background and are centered; file link is larger
        self.assertNotIn('id="prd-view"', self.js)
        self.assertNotIn('>View .md<', self.js)
        self.assertIn('<button class="btn primary" id="prd-draft">Draft with AI</button>', self.js)
        self.assertIn('<button class="btn primary" id="prd-import">Import PRD</button>', self.js)
        self.assertIn('prd-file-link', self.js)
        self.assertIn('.prd-file-link', self.css)
        self.assertIn('body[data-page="product"] .topbar {', self.css)
        self.assertIn('body[data-page="product"] .topbar-actions {', self.css)
        self.assertIn('body[data-page="product"] .topbar-actions .btn {', self.css)
        self.assertIn('background: var(--accent);', self.css)
        self.assertIn('color: var(--accent-ink);', self.css)

    def test_visual_check_done_container_thumbnails_and_lightbox(self):
        # Visual check results are shown as thumbnails in the done container and can be opened
        self.assertIn('.visual-check-banner', self.css)
        self.assertIn('.visual-check-grid', self.css)
        self.assertIn('.visual-check-thumb-card', self.css)
        self.assertIn('dialog.dialog-image-viewer', self.css)
        self.assertIn('getVisualCheckForRun(', self.js)
        self.assertIn('openImageModal(', self.js)
        self.assertIn('wireImagePreviews(', self.js)
        self.assertIn('visual-check-banner', self.js)
        self.assertIn('data-preview-img', self.js)

    def test_new_job_asks_what_each_kind_needs_and_has_no_you_decide_toggle(self):
        form = self.js[self.js.index("pages.new = async"):self.js.index("const FEATURE_STATUS")]
        for needle in ("How do I make it happen?", "What should happen instead?", "Look and feel", "Upload logs or screenshots",
                       "What should be tested?", "What should change?", "recent-logs", "uploadFile(", "Pick a Figma design"):
            self.assertIn(needle, form, needle)
        self.assertNotIn("You decide the details", self.js)
        self.assertNotIn("recommend", form.lower().replace("recommended", ""))

    def test_product_documents_are_reachable_from_home_help_palette_and_the_new_project_flow(self):
        self.assertIn("productStripHtml(product", self.js)
        home = self.js[self.js.index("pages.home = async"):self.js.index("function jobHeaderActions")]
        self.assertLess(home.index("productStripHtml(product"), home.index('class="job-table"'))  # toward the top of the main screen
        self.assertIn('"#/product"', self.js)
        self.assertIn('<script src="markdown.js">', self.html)
        self.assertIn('href="#/product">Open the product requirements', self.js)
        self.assertIn("How do I get better plans?", self.js)
        self.assertNotIn("#/product/", self.js)  # one document, no sub-pages

    def test_a_blank_screen_gets_a_loading_message_after_a_moment(self):
        self.assertIn("Loading…", self.js[self.js.index("async function route()"):][:2500])

    def test_the_first_feature_or_design_is_nudged_toward_defining_the_product(self):
        form = self.js[self.js.index("pages.new = async"):self.js.index("const FEATURE_STATUS")]
        self.assertIn('st.type === "feature" || st.type === "design"', form)
        self.assertIn("Define the product first", form)
        self.assertIn("Skip for now", form)
        self.assertIn("sections.some((x) => x.filled)", form)  # nothing at all is written yet (every section is optional, so any one counts)

    def test_the_product_page_is_one_document_with_five_sections_you_can_edit_import_and_restore(self):
        page = self.js[self.js.index("pages.product = async"):self.js.index("async function hydrateAuthImages")]
        for part in ('data-prd-edit', 'api("product/settings"', 'api("product/revert"', 'product/history/', '"product/import"', '"product/design"', 'product/reference',
                     'api("product/draft"', "Update this automatically when jobs finish", "Import PRD"):
            self.assertIn(part, page, part)
        self.assertNotIn("Help me with this", self.js)  # removed
        self.assertNotIn("product/refine", self.js)
        self.assertNotIn("Import a PRD", self.js)  # the button just says Import PRD
        self.assertNotIn("product/scaffold", self.js)  # no six documents to create
        self.assertNotIn("last-review", self.js)  # and no separate review job
        self.assertNotIn("Everything here is optional", page)
        self.assertIn('title: "Product requirements"', page)
        self.assertIn("Press Enter to add another feature", page)
        self.assertIn("prd-features-edit", page)
        self.assertIn("compact: true", page)
        # Look and feel takes uploads, a pasted link, and an item picked from a connected app (Figma).
        self.assertIn("linkPickerHtml({ prefer: [\"figma\"]", page)

    def test_prd_markdown_rendered_flat_on_docs_page(self):
        docs = self.js[self.js.index("pages.docs = async"):self.js.index("pages.product = async")]
        prd_section = docs[docs.index('kind === "product"'):docs.index('kind === "job"')]
        self.assertIn('Markdown.render(p.text)', prd_section)
        self.assertNotIn('<pre class="file">${esc(p.text)}</pre>', prd_section)
        self.assertIn('actions: ""', prd_section)
        self.assertIn('class="card-h"', prd_section)
        self.assertIn('class="card-actions"', prd_section)
        self.assertIn('id="prd-download">Download</button>', prd_section)
        self.assertIn('id="prd-copy-path"', prd_section)

    def test_connected_apps_subsection_of_import_lists_apps_and_links_to_connections(self):
        page = self.js[self.js.index("pages.product = async"):self.js.index("async function hydrateAuthImages")]
        self.assertIn('reference-imports', page)
        self.assertIn('connected-apps-list', page)
        self.assertIn('providerIcon(a.id)', page)
        self.assertIn('href="#/connections"', page)
        self.assertIn('Connect more', page)
        self.assertIn('${connectedApps.length ? `<button type="button" class="btn small primary" id="prd-add-app">Choose reference</button>` : ""}', page)
        self.assertNotIn('disabled title="Connect an app first"', page)
        self.assertIn('.brand-icon', self.css)
        picker = self.js[self.js.index("async function linkPickerHtml"):self.js.index("async function attachToJob")]
        self.assertIn('Connected apps:', picker)
        self.assertIn('href="#/connections"', picker)

    def test_hosted_self_healing_and_reconnection(self):
        # Hosted app auto-reconnects and relocates without locking user into sign-in gate
        self.assertIn("authResolved = true", self.js)
        self.assertIn("relocateMachine().catch", self.js)
        self.assertIn("Account.pickMachine(machines, remembered)", self.js)
        self.assertIn("https://${stored}", self.js)

    def test_last_used_login_method_highlight_and_tag(self):
        # Last used login method is tracked and visually tagged in the sign-in gate
        self.assertIn("getLastLoginMethod()", self.js)
        self.assertIn("setLastLoginMethod(", self.js)
        self.assertIn("orchestrator_last_login_method", self.js)
        self.assertIn(".last-used-tag", self.css)
        self.assertIn(".sso-btn.last-used", self.css)
        self.assertIn("Last used", self.js)

    def test_signin_modal_sso_icons_alignment_and_assets(self):
        # SSO button icons align in a fixed column, use official 24x24 assets, and backgrounds match
        css = (self.STATIC / "style.css").read_text()
        self.assertIn(".sso-btn .icon {", css)
        self.assertIn("position: absolute;", css)
        self.assertIn("left: var(--space-4);", css)
        self.assertIn("top: 50%;", css)
        self.assertIn("transform: translateY(-50%);", css)
        self.assertIn(".sso-btn.apple-btn,", css)
        self.assertIn(".sso-btn.github-btn {", css)
        self.assertNotIn(".sso-btn.apple-btn {\n  background: #000;", css)
        index_html = (self.STATIC / "index.html").read_text()
        self.assertIn('<symbol id="i-google" viewBox="0 0 48 48">', index_html)
        self.assertIn('<symbol id="i-apple" viewBox="17 14 22 22">', index_html)
        self.assertIn('<symbol id="i-github" viewBox="0 0 24 24">', index_html)
        self.assertIn(".signin-wrap {", css)
        self.assertIn("align-items: flex-start;", css)
        self.assertNotIn("min-height: calc(100vh - 120px);", css)
        self.assertIn(".app.session-locked .topbar {", css)

    def test_signin_modal_omits_orchestrator_icon_and_title(self):
        # Sign-in modal card begins directly with heading without redundant app brand mark or title
        gate = self.js[self.js.index("function showSignInGate"):self.js.index("function showSignInGate") + 2500]
        self.assertNotIn("signin-brand", gate)
        self.assertNotIn("brand-mark", gate)
        signing_in = self.js[self.js.index("function showSigningIn"):self.js.index("function cpApi")]
        self.assertNotIn("signin-brand", signing_in)
        self.assertNotIn("brand-mark", signing_in)

    def test_home_new_job_button_is_sticky(self):
        # The + new job button stays in place when scrolling
        self.assertIn(".home-new-job-btn {", self.css)
        self.assertIn("position: fixed;", self.css)
        self.assertIn(".topbar-actions:has(.home-new-job-btn)", self.css)

    def test_slow_model_work_is_started_and_polled_not_held_open(self):
        page = self.js[self.js.index("pages.product = async"):self.js.index("async function hydrateAuthImages")]
        self.assertEqual(page.count("waitForTask("), 2)  # draft and import
        fn = self.js[self.js.index("async function waitForTask"):self.js.index("function productStripHtml")]
        self.assertIn("product/task/", fn)
        self.assertIn("e.status === 404 || ++misses >= 4", fn)  # a dropped connection is retried, a vanished task is not
        self.assertIn("7 * 60 * 1000", fn)  # and it never spins for ever

    def test_working_on_it_is_a_prominent_spinner_with_a_timer(self):
        page = self.js[self.js.index("pages.product = async"):self.js.index("async function hydrateAuthImages")]
        busy = page[page.index("const busy ="):][:900]
        for part in ('class="prd-busy"', 'class="spinner"', 'role="status"', "data-elapsed", "Math.floor"):
            self.assertIn(part, busy, part)
        css = self.css[self.css.index(".prd-busy {"):][:400]
        self.assertIn("var(--accent)", css)  # in the accent colour, on its soft tint
        self.assertIn("var(--accent-soft)", css)

    def test_draft_with_ai_button_and_cancellation(self):
        # Button label is "Draft with AI" across all PRD entry points
        self.assertIn('>Draft with AI</a>', self.js)
        self.assertIn('id="prd-draft">Draft with AI</button>', self.js)
        self.assertIn('id="prd-start-draft">Draft with AI</button>', self.js)
        self.assertNotIn('>Draft PRD<', self.js)
        # Busy indicator says it can take a few minutes and provides a cancel/stop option
        page = self.js[self.js.index("pages.product = async"):self.js.index("async function hydrateAuthImages")]
        self.assertIn("This can take a few minutes.", page)
        self.assertIn('id="prd-busy-cancel"', page)
        self.assertIn('method: "DELETE"', page)
        self.assertIn('toast("Draft cancelled", "cancel")', page)
        self.assertIn(".msg-cancel { --kind: var(--warn); }", self.css)
        # Proposed view shows subsections with revert and edit buttons, auto-saving without top approve/discard
        self.assertNotIn('id="prd-approve-all"', page)
        self.assertNotIn('id="prd-discard"', page)
        self.assertNotIn('data-approve-sec=', page)
        self.assertIn('data-revert-sec=', page)
        self.assertIn('data-edit-sec=', page)
        self.assertIn('id="prd-proposal-done"', page)
        self.assertIn('proposal-sections', page)
        self.assertIn('proposal-summary', page)
        proposal_fn = page[page.index("const proposalView"):page.index("const busy")]
        self.assertNotIn('<pre class="diff"', proposal_fn)
        self.assertNotIn('Read the whole proposed document', proposal_fn)

    def test_import_takes_a_dropped_file_and_waits_for_submit(self):
        page = self.js[self.js.index("const importPanel"):self.js.index("An existing project: read what is there")]
        for part in ('id="prd-drop"', '"dragover"', '"drop"', "e.dataTransfer?.files?.[0]", "if (f) setFile(f)", "e.preventDefault()", 'zone.addEventListener("keydown"'):
            self.assertIn(part, page, part)
        self.assertIn('id="prd-import-go" disabled>Submit</button>', page)
        self.assertIn("goBtn.disabled = !picked && !textEl.value.trim()", page)
        read = self.js[self.js.index("const readPrd"):self.js.index("const importPanel")]
        self.assertIn("NJ_UPLOAD_LIMIT", read)  # a dropped file is checked like a chosen one
        self.assertIn("md|markdown|txt|docx|pdf", read)
        self.assertIn(".prd-drop.over", self.css)
        self.assertIn("pointer: coarse", self.css)  # phones have nothing to drag, so they are not told to
        self.assertIn("browse files", page)
        self.assertNotIn("click to choose one", page)

    def test_home_shows_what_the_product_is_without_the_section_buttons(self):
        card = self.js[self.js.index("function productStripHtml"):][:1500]
        self.assertNotIn("doc-chip", card)
        self.assertNotIn("p.sections.map", card)
        self.assertIn("Import PRD", card)
        self.assertNotIn("doc-chip", self.css)  # and the styles went with them

    def test_an_automatic_update_is_announced_over_the_page_with_see_what_changed_undo_and_dismiss(self):
        fn = self.js[self.js.index("function showPrdUpdate"):self.js.index("// Slow model work runs on the server")]
        for part in ('id: `prd:${note.id}`', "sticky: true", "See what changed", '"Undo"', "api(\"product/dismiss\"", "undoPrdUpdate(note.id)", "onDismiss: seen"):
            self.assertIn(part, fn, part)
        self.assertIn("if (!first) Notifications.show", fn)  # a browser alert only for an update that happens while you are here
        self.assertNotIn("prdNoticeHtml", self.js)  # no separate banners on Home and the Product page any more
        refresh = self.js[self.js.index("async function refreshState()"):][:3000]
        self.assertIn("showPrdUpdate(newState.product_notice)", refresh)
        home = self.js[self.js.index("function productStripHtml"):][:1500]
        self.assertNotIn("notice", home)

    def test_design_images_load_with_the_token_not_a_bare_img_src(self):
        self.assertIn("data-auth-src", self.js)
        self.assertIn("img-src 'self' data: blob:", (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "server.py").read_text())
        fn = self.js[self.js.index("async function hydrateAuthImages"):][:900]
        self.assertIn("Authorization", fn)
        self.assertIn("createObjectURL", fn)

    def test_visible_keyboard_focus_for_all_controls(self):
        self.assertIn(":focus-visible { outline: 2px solid var(--accent)", self.css)

    def test_job_hero_and_containers_use_design_tokens(self):
        self.assertIn(".job-hero-title { margin: 0; font-size: var(--text-lg); font-weight: 650;", self.css)
        self.assertIn(".job-hero-main { display: flex; flex-direction: column; align-items: flex-start; gap: var(--space-1);", self.css)
        self.assertIn(".job-hero.tone-attention { border-color: var(--warn); background: var(--warn-soft); }", self.css)
        self.assertIn(".banner { display: flex; align-items: center; gap: var(--space-3) var(--space-4); padding: var(--space-3) var(--space-4); border-radius: var(--radius); border: 1px solid var(--border);", self.css)
        self.assertIn(".notice { padding: var(--space-3) var(--space-4); border-radius: var(--radius); border: 1px solid var(--warn);", self.css)

    def test_setup_checklist_js_handles_git_init_and_github_create(self):
        self.assertIn('api("setup/git-init"', self.js)
        self.assertIn('api("setup/github-create"', self.js)
        self.assertIn("Create GitHub repository", self.js)

    def test_console_stopped_message_in_js(self):
        self.assertIn("Console connection was stopped.", self.js)
        fn = self.js[self.js.index("function nextStep(r)"):self.js.index("pages.run =")]
        self.assertIn('r.action === "console"', fn)


class ScopeEndpointTests(ServerTestCase):
    JOB = "20260922-bug-1"

    def sh(self, *args):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, env=env)

    def setUp(self):
        super().setUp()
        self.sh("init", "-q", "-b", "main")
        (self.root / "src").mkdir()
        (self.root / "src" / "seat.py").write_text("x = 1\n")
        (self.root / "src" / "other.py").write_text("y = 1\n")
        self.sh("add", "src")
        self.sh("commit", "-qm", "base")
        self.sh("checkout", "-qb", "ai/issue-1")
        (self.root / "src" / "seat.py").write_text("x = 2\n" * 5)
        (self.root / "src" / "other.py").write_text("y = 2\n")
        (self.root / "src" / "helpers.py").write_text("def h():\n    pass\n")
        (self.root / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}\n')
        self.sh("add", "src", "package.json")
        self.sh("commit", "-qm", "work")
        self.sh("checkout", "-q", "main")
        path = self.root / ".orchestrator" / "jobs" / f"{self.JOB}.json"
        job = json.loads(path.read_text())
        job["plan"] = {"likely_files": ["src/seat.py"], "tasks": [{"title": "t", "likely_files": []}]}
        path.write_text(json.dumps(job))

    def scope(self):
        _, data = self.request("GET", f"/api/jobs/{self.JOB}")
        return data["scope"]

    def test_reports_what_went_beyond_the_plan(self):
        scope = self.scope()
        by_kind = {f["kind"]: f for f in scope["findings"]}
        self.assertEqual(by_kind["manifest"]["files"], ["package.json"])
        self.assertEqual(by_kind["new_files"]["files"], ["src/helpers.py"])
        self.assertEqual(by_kind["outside_plan"]["files"], ["src/other.py"])
        self.assertEqual((scope["files"], scope["in_scope"], scope["flagged"]), (4, 1, 3))
        self.assertIn("src/helpers.py", scope["trim"]["spec"])

    def test_accepting_files_clears_them_and_reset_brings_them_back(self):
        res, data = self.request("POST", f"/api/jobs/{self.JOB}/scope", body={"op": "accept", "paths": ["src/other.py", "src/helpers.py"]}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertEqual({f["kind"] for f in data["scope"]["findings"]}, {"manifest"})
        self.assertEqual(self.scope()["accepted"], 2)
        _, data = self.request("POST", f"/api/jobs/{self.JOB}/scope", body={"op": "reset"}, headers=UI_HEADERS)
        self.assertEqual(len(data["scope"]["findings"]), 3)

    def test_adopt_files_adds_them_to_plan_likely_files(self):
        res, data = self.request("POST", f"/api/jobs/{self.JOB}/scope", body={"op": "adopt", "paths": ["src/other.py", "src/helpers.py"]}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        job_data = json.loads((self.root / ".orchestrator" / "jobs" / f"{self.JOB}.json").read_text())
        self.assertIn("src/other.py", job_data["plan"]["likely_files"])
        self.assertIn("src/helpers.py", job_data["plan"]["likely_files"])

    def test_a_files_diff_is_available_inline(self):
        res, data = self.request("GET", f"/api/jobs/{self.JOB}/diff?path=src/seat.py")
        self.assertEqual((res.status, data["source"], data["binary"], data["truncated"]), (200, "branch", False, False))
        self.assertIn("-x = 1", data["diff"])
        self.assertIn("+x = 2", data["diff"])

    def test_orchestrators_own_files_are_not_listed_as_the_jobs_changes(self):
        self.sh("checkout", "-q", "ai/issue-1")
        (self.root / ".orchestrator" / "extra.json").write_text("{}")
        self.sh("add", "-f", ".orchestrator/extra.json")
        self.sh("commit", "-qm", "state")
        self.sh("checkout", "-q", "main")
        _, data = self.request("GET", f"/api/jobs/{self.JOB}")
        self.assertNotIn(".orchestrator/extra.json", data["changes"]["files"])
        self.assertIn("src/seat.py", data["changes"]["files"])

    def test_only_files_in_the_jobs_changes_can_be_read(self):
        (self.root / "secret.txt").write_text("TOPSECRET")
        for path in ("secret.txt", "../secret.txt", "/etc/hosts", "", "src/untouched.py"):
            res, data = self.request("GET", f"/api/jobs/{self.JOB}/diff?path={path}")
            self.assertEqual(res.status, 404, path)
            self.assertNotIn("TOPSECRET", json.dumps(data))

    def test_binary_and_oversized_diffs_are_handled(self):
        self.sh("checkout", "-q", "ai/issue-1")
        (self.root / "logo.bin").write_bytes(bytes(range(256)) * 4)
        (self.root / "big.txt").write_text("line\n" * 40000)
        self.sh("add", "logo.bin", "big.txt")
        self.sh("commit", "-qm", "more")
        self.sh("checkout", "-q", "main")
        _, binary = self.request("GET", f"/api/jobs/{self.JOB}/diff?path=logo.bin")
        self.assertEqual((binary["binary"], binary["diff"]), (True, ""))
        _, big = self.request("GET", f"/api/jobs/{self.JOB}/diff?path=big.txt")
        self.assertTrue(big["truncated"])
        self.assertLessEqual(len(big["diff"]), ui.MAX_DIFF_CHARS)

    def test_accept_needs_files(self):
        res, _ = self.request("POST", f"/api/jobs/{self.JOB}/scope", body={"op": "accept", "paths": []}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)

    def test_feature_paths_add_an_outside_feature_finding(self):
        ui.feature_store.create(self.root / ".orchestrator", "Seats", paths="src/seat.py")
        path = self.root / ".orchestrator" / "jobs" / f"{self.JOB}.json"
        job = json.loads(path.read_text())
        job["feature"] = "seats"
        path.write_text(json.dumps(job))
        kinds = [f["kind"] for f in self.scope()["findings"]]
        self.assertIn("outside_feature", kinds)

    def test_no_branch_means_no_scope_check(self):
        path = self.root / ".orchestrator" / "jobs" / "20260923-feature-2.json"
        path.write_text(json.dumps({"title": "Local only", "type": "feature", "status": "planned"}))
        _, data = self.request("GET", "/api/jobs/20260923-feature-2")
        self.assertIsNone(data["scope"])

    def test_requires_the_ui_header(self):
        res, _ = self.request("POST", f"/api/jobs/{self.JOB}/scope", body={"op": "reset"}, headers={"Content-Type": "application/json"})
        self.assertIn(res.status, (400, 403))


class UndoEndpointTests(ServerTestCase):
    JOB = "20260922-bug-1"

    def post(self, path, body):
        return self.request("POST", path, body=body, headers=UI_HEADERS)

    def test_deleting_a_feature_returns_what_is_needed_to_undo_it(self):
        self.post("/api/features", {"name": "Lobby", "paths": "src/"})
        self.post(f"/api/jobs/{self.JOB}/feature", {"feature": "lobby"})
        res, data = self.request("DELETE", "/api/features/lobby", headers=UI_HEADERS)
        self.assertEqual((res.status, data["features"]), (200, []))
        self.assertEqual((data["undo"]["feature"]["id"], data["undo"]["jobs"]), ("lobby", [self.JOB]))
        res, back = self.post("/api/features/restore", {"feature": data["undo"]["feature"], "jobs": data["undo"]["jobs"]})
        self.assertEqual(res.status, 200)
        self.assertEqual((back["features"][0]["id"], back["features"][0]["job_ids"]), ("lobby", [self.JOB]))
        _, jobs = self.request("GET", "/api/jobs")
        self.assertEqual(jobs["jobs"][0]["feature"], "lobby")

    def test_restore_does_not_steal_jobs_that_were_reassigned_meanwhile(self):
        self.post("/api/features", {"name": "Lobby"})
        self.post("/api/features", {"name": "Chat"})
        self.post(f"/api/jobs/{self.JOB}/feature", {"feature": "lobby"})
        _, data = self.request("DELETE", "/api/features/lobby", headers=UI_HEADERS)
        self.post(f"/api/jobs/{self.JOB}/feature", {"feature": "chat"})
        self.post("/api/features/restore", {"feature": data["undo"]["feature"], "jobs": data["undo"]["jobs"]})
        _, jobs = self.request("GET", "/api/jobs")
        self.assertEqual(jobs["jobs"][0]["feature"], "chat")

    def test_restore_validates(self):
        res, _ = self.post("/api/features/restore", {"feature": {"id": "../x", "name": "x"}})
        self.assertEqual(res.status, 400)
        res, _ = self.request("POST", "/api/features/restore", body={"feature": {"id": "ok", "name": "x"}}, headers={"Content-Type": "application/json"})
        self.assertIn(res.status, (400, 403))

    def test_deleting_a_kpi_can_be_undone(self):
        self.post("/api/features", {"name": "Lobby"})
        self.post("/api/features/lobby/kpis", {"op": "add", "name": "Claims", "event": "seat_claimed", "target": 5})
        self.post("/api/features/lobby/kpis", {"op": "measure", "kpi": "claims", "value": 3})
        res, data = self.post("/api/features/lobby/kpis", {"op": "delete", "kpi": "claims"})
        self.assertEqual((res.status, data["features"][0]["kpis"]), (200, []))
        res, back = self.post("/api/features/lobby/kpis", {"op": "restore", "kpi": data["undo"]["kpi"]})
        self.assertEqual(res.status, 200)
        kpi = back["features"][0]["kpis"][0]
        self.assertEqual((kpi["id"], len(kpi["measurements"])), ("claims", 1))

    def test_undoing_mark_complete_puts_the_job_back_where_it_was(self):
        jobs = self.root / ".orchestrator" / "jobs"
        job = json.loads((jobs / f"{self.JOB}.json").read_text())
        job.update({"status": "completed", "restore_status": "review-needed", "completed_at": "2026-01-01"})
        (jobs / "archive").mkdir()
        (jobs / "archive" / f"{self.JOB}.json").write_text(json.dumps(job))
        (jobs / f"{self.JOB}.json").unlink()
        res, _ = self.post("/api/config/archived-restore", {"id": self.JOB})
        self.assertEqual(res.status, 200)
        restored = json.loads((jobs / f"{self.JOB}.json").read_text())
        self.assertEqual(restored["status"], "review-needed")
        self.assertNotIn("restore_status", restored)
        self.assertNotIn("completed_at", restored)

    def test_archiving_jobs_hides_them_and_restore_brings_them_back_unchanged(self):
        jobs = self.root / ".orchestrator" / "jobs"
        before = json.loads((jobs / f"{self.JOB}.json").read_text())
        res, data = self.post("/api/jobs/archive", {"ids": [self.JOB]})
        self.assertEqual((res.status, data["archived"]), (200, [self.JOB]))
        self.assertFalse((jobs / f"{self.JOB}.json").exists())
        _, listed = self.request("GET", "/api/jobs")
        self.assertNotIn(self.JOB, [j["id"] for j in listed["jobs"]])
        self.post("/api/config/archived-restore", {"id": self.JOB})
        self.assertEqual(json.loads((jobs / f"{self.JOB}.json").read_text())["status"], before.get("status"))

    def test_archive_validates_and_skips_running_jobs(self):
        for body in ({}, {"ids": []}, {"ids": "x"}, {"ids": ["../x"]}):
            res, _ = self.post("/api/jobs/archive", body)
            self.assertEqual(res.status, 400, body)
        with patch.object(self.server.sessions, "running_job_ids", return_value=[self.JOB]):
            res, data = self.post("/api/jobs/archive", {"ids": [self.JOB]})
        self.assertEqual((res.status, data["archived"]), (200, []))


class RunHistoryTests(ServerTestCase):
    """Activity lists earlier runs from their saved logs, so the list survives a restart."""

    def test_earlier_runs_are_read_from_saved_logs_newest_first(self):
        logs = self.root / ".orchestrator" / "logs" / "ui"
        logs.mkdir(parents=True)
        (logs / "20260101-090000-abc123-test.log").write_text("ok")
        (logs / "20260102-090000-def456-new_job.log").write_text("ok")
        (logs / "notes.txt").write_text("not a run")
        res, data = self.request("GET", "/api/runs/history")
        self.assertEqual(res.status, 200)
        self.assertEqual([r["id"] for r in data["runs"]], ["20260102-090000-def456", "20260101-090000-abc123"])
        self.assertEqual(data["runs"][0]["title"], ui.ACTIONS["new_job"].title)
        self.assertEqual(data["runs"][1]["log"], "logs/ui/20260101-090000-abc123-test.log")


class UxReviewEndpointTests(ServerTestCase):
    """The UX and design review: passes, their screenshots, the screens setting and a job's check."""
    JOB = "20260922-bug-1"
    PNG = b"\x89PNG\r\n\x1a\nfake"

    def post(self, path, body):
        return self.request("POST", path, body=body, headers=UI_HEADERS)

    def test_passes_are_listed_newest_first_with_their_screens(self):
        out = self.root / ".orchestrator" / "output" / "ux-pass"
        for stamp, findings in (("20261001-090000", []), ("20261002-090000", [{"area": "ux", "severity": 3, "problem": "Two primaries"}])):
            (out / stamp / "screens").mkdir(parents=True)
            (out / stamp / "result.json").write_text(json.dumps({"at": "2026-10-02T09:00:00", "summary": "S", "findings": findings, "checklist": []}))
        (out / "20261002-090000" / "screens" / "home-390-light.png").write_bytes(self.PNG)
        (out / "notes").mkdir()
        res, data = self.request("GET", "/api/ux-pass")
        self.assertEqual(res.status, 200)
        self.assertEqual([p["id"] for p in data["passes"]], ["20261002-090000", "20261001-090000"])
        self.assertEqual(data["passes"][0]["counts"]["major"], 1)
        _, detail = self.request("GET", "/api/ux-pass/20261002-090000")
        self.assertIn("Two primaries", detail["fix_text"])
        res, body = self.request("GET", "/api/ux-screens/pass/20261002-090000/home-390-light.png")
        self.assertEqual((res.status, body), (200, self.PNG))
        for bad in ("/api/ux-screens/pass/20261002-090000/..%2Fresult.json", "/api/ux-screens/pass/../x.png",
                    "/api/ux-screens/other/20261002-090000/home-390-light.png", "/api/ux-pass/..%2Fjobs"):
            self.assertEqual(self.request("GET", bad)[0].status, 404, bad)

    def test_screens_to_review_are_saved_into_project_json_and_validated(self):
        res, data = self.post("/api/ui-review", {"url": "http://localhost:3000", "routes": "/\n/settings\n", "widths": "390, 1440",
                                                 "dark_mode": True, "review_changes": True})
        self.assertEqual(res.status, 200)
        self.assertEqual(data["settings"]["routes"], ["/", "/settings"])
        project = json.loads((self.root / ".orchestrator" / "project.json").read_text())
        self.assertEqual((project["project_name"], project["ui_review"]["widths"]), ("Demo", [390, 1440]))  # nothing else changed
        for body in ({"url": "localhost"}, {"widths": "wide"}, {"widths": "100"}):
            self.assertEqual(self.post("/api/ui-review", body)[0].status, 400, body)

    def test_a_jobs_check_comes_with_its_detail(self):
        jobs = self.root / ".orchestrator" / "jobs"
        job = json.loads((jobs / f"{self.JOB}.json").read_text())
        job["ux_review"] = {"at": "2026-10-04T10:00:00", "counts": {"findings": 1, "major": 1}, "files": ["web/app.js"], "screens": 1}
        (jobs / f"{self.JOB}.json").write_text(json.dumps(job))
        review = self.root / ".orchestrator" / "output" / self.JOB / "ux-review"
        (review / "screens").mkdir(parents=True)
        (review / "screens" / "home-390-light.png").write_bytes(self.PNG)
        (review / "result.json").write_text(json.dumps({"findings": [{"area": "ux", "severity": 3, "problem": "P"}], "checklist": [],
                                                        "screens": [{"file": "home-390-light.png", "route": "/", "width": 390, "dark": False}]}))
        _, detail = self.request("GET", f"/api/jobs/{self.JOB}")
        self.assertEqual((detail["ux_review"]["screens"], detail["ux_review"]["findings"][0]["problem"]), (1, "P"))
        self.assertEqual(detail["ux_review"]["screen_list"][0]["file"], "home-390-light.png")
        res, _ = self.request("GET", f"/api/ux-screens/job/{self.JOB}/home-390-light.png")
        self.assertEqual(res.status, 200)

    def test_both_reviews_are_runnable_actions(self):
        self.assertIn("ux_review_run.py", " ".join(ui.ACTIONS["ux_pass"].build({}, self.root)))
        argv = ui.ACTIONS["ux_review_job"].build({"job": self.JOB}, self.root)
        self.assertEqual(argv[-2:][0], "job")


class PageLoadBacklogTests(unittest.TestCase):
    def test_the_listen_backlog_takes_a_whole_page_load(self):
        # A first page load in Chrome reset connections for scripts (ERR_CONNECTION_RESET on messages.js, app.js…)
        # with socketserver's default backlog of 5, leaving the app blank in about 1 load in 4. A local socket burst
        # doesn't reproduce it, so this checks the setting itself.
        self.assertGreaterEqual(ui.UIServer.request_queue_size, 64)

class JobContextTests(ServerTestCase):
    """A job's attachments are grouped by what they are to the job (ticket, designs, what went wrong), not by source."""
    JOB = "20260922-bug-1"

    def post(self, path, body):
        return self.request("POST", path, body=body, headers=UI_HEADERS)

    def set_job(self, **fields):
        path = self.root / ".orchestrator" / "jobs" / f"{self.JOB}.json"
        job = json.loads(path.read_text())
        job.update(fields)
        path.write_text(json.dumps(job))

    def test_items_are_grouped_by_role_whatever_their_source(self):
        uploads = self.root / ".orchestrator" / "ui" / "uploads"
        uploads.mkdir(parents=True)
        (uploads / "20260101-000000-abcdef-mock.png").write_bytes(b"\x89PNG")
        self.set_job(type="feature-plan", external_links=[
            {"provider": "jira", "ref": "ABC-1", "title": "Lobby seats", "url": "https://x.atlassian.net/browse/ABC-1"},
            {"provider": "figma", "ref": "f1", "title": "Lobby screen"},
            {"provider": "sentry", "ref": "S-9", "title": "Crash on rejoin"}],
            reference_artifacts=[{"path": ".orchestrator/ui/uploads/20260101-000000-abcdef-mock.png", "type": "image_reference"},
                                 {"url": "https://www.figma.com/file/x", "type": "figma_url"}])
        _, data = self.request("GET", f"/api/jobs/{self.JOB}")
        ctx = data["context"]
        self.assertEqual([t["ref"] for t in ctx["ticket"]], ["ABC-1"])
        self.assertEqual([d["source"] for d in ctx["designs"]], ["Figma", "Upload", "Link"])  # one place for designs
        self.assertEqual(ctx["designs"][1]["image"], ".orchestrator/ui/uploads/20260101-000000-abcdef-mock.png")
        self.assertEqual([p["title"] for p in ctx["problem"]], ["Crash on rejoin"])
        self.assertFalse(ctx["problem_first"])

    def test_on_a_bug_job_a_screenshot_is_evidence_and_comes_first(self):
        self.set_job(type="bug", reference_artifacts=[{"path": ".orchestrator/ui/uploads/x-shot.png", "type": "image_reference"}])
        _, data = self.request("GET", f"/api/jobs/{self.JOB}")
        self.assertEqual((len(data["context"]["problem"]), len(data["context"]["designs"]), data["context"]["problem_first"]), (1, 0, True))

    def test_attaching_only_attaches_and_is_read_back_by_role(self):
        res, data = self.post(f"/api/jobs/{self.JOB}/attach", {"role": "problem", "text": "Fatal: seat index out of range", "note": "From TestFlight"})
        self.assertEqual(res.status, 200)
        self.assertEqual(data["context"]["problem"][0]["title"], "From TestFlight")
        res, data = self.post(f"/api/jobs/{self.JOB}/attach", {"role": "design", "url": "https://www.figma.com/file/abc"})
        self.assertEqual(data["context"]["designs"][0]["url"], "https://www.figma.com/file/abc")
        job = json.loads((self.root / ".orchestrator" / "jobs" / f"{self.JOB}.json").read_text())
        pasted = self.root / job["reference_artifacts"][0]["path"]
        self.assertEqual(pasted.read_text(), "Fatal: seat index out of range")  # the AI reads it on its next step
        self.assertEqual(self.server.sessions.list(), [])  # nothing ran
        for body in ({"role": "other", "url": "https://x"}, {"role": "design"}, {"role": "design", "url": "ftp://x"},
                     {"role": "design", "upload": "../../etc/passwd"}):
            self.assertEqual(self.post(f"/api/jobs/{self.JOB}/attach", body)[0].status, 400, body)

    def test_thumbnails_are_served_only_for_attached_images(self):
        uploads = self.root / ".orchestrator" / "ui" / "uploads"
        uploads.mkdir(parents=True)
        (uploads / "a.png").write_bytes(b"\x89PNG")
        (self.root / "secret.png").write_bytes(b"\x89PNG")
        self.assertEqual(self.request("GET", "/api/job-image?path=.orchestrator/ui/uploads/a.png")[0].status, 200)
        for bad in ("secret.png", ".orchestrator/project.json", ".orchestrator/ui/uploads/../../secret.png"):
            self.assertEqual(self.request("GET", f"/api/job-image?path={bad}")[0].status, 404, bad)


class BuildingMeansActiveTests(unittest.TestCase):
    """"Building" is said only while a worker is actually running; a stopped build is Paused, with Resume."""
    PLAN = {"tasks": [{"title": "a"}, {"title": "b"}, {"title": "c"}]}

    def state(self, **job):
        return ui.job_state({"status": "executing", "type": "feature-plan", "plan": self.PLAN, "completed_tasks": [0], **job})

    def test_a_stopped_build_is_paused_with_resume(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        st = self.state(worker_pid=dead.pid, worker_host=socket.gethostname())
        self.assertEqual((st["label"], st["group"], st["next"]["action"]), ("Paused", "needs_you", "resume"))
        self.assertIn("1 of 3 tasks done", st["reason"])

    def test_no_worker_recorded_is_not_building(self):
        self.assertEqual(self.state()["label"], "Paused")

    def test_a_running_worker_is_building(self):
        self.assertEqual(self.state(worker_pid=os.getpid(), worker_host=socket.gethostname())["label"], "Building")

    def test_a_worker_on_another_machine_is_trusted(self):
        self.assertEqual(self.state(worker_pid=1, worker_host="some-other-mac")["label"], "Building")


class KeepAliveBodyTests(ServerTestCase):
    """A request body no route reads must not leak into the next request on the same connection."""

    def two_requests(self, first_method, first_path, first_body, first_headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        auth = {"Authorization": "Bearer test-token"}
        conn.request(first_method, first_path, body=first_body, headers={**auth, **first_headers})
        first = conn.getresponse()
        first.read()
        conn.request("GET", "/api/state", headers=auth)
        second = conn.getresponse()
        second.read()
        conn.close()
        return first.status, second.status

    def test_delete_with_a_json_body_does_not_break_the_next_request(self):
        self.request("POST", "/api/features", body={"name": "Lobby"}, headers=UI_HEADERS)
        first, second = self.two_requests("DELETE", "/api/features/lobby", json.dumps({}).encode(), UI_HEADERS)
        self.assertEqual((first, second), (200, 200))

    def test_a_rejected_post_with_a_body_does_not_break_the_next_request(self):
        first, second = self.two_requests("POST", "/api/features", json.dumps({"name": "x"}).encode(), {"Content-Type": "application/json"})
        self.assertEqual((first, second), (403, 200))

    def test_a_route_that_ignores_its_body_does_not_break_the_next_request(self):
        first, second = self.two_requests("POST", "/api/analytics/plan", json.dumps({"ignored": "x" * 50}).encode(), UI_HEADERS)
        self.assertEqual((first, second), (200, 200))


class UploadAndRecentLogsEndpointTests(ServerTestCase):
    def upload(self, name, data, headers=None, query_name=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        path = f"/api/uploads?name={name}" if query_name else "/api/uploads"
        conn.request("POST", path, body=data, headers={"Authorization": "Bearer test-token", "X-Orchestrator-UI": "1", "Content-Type": "application/octet-stream", **(headers or {})})
        res = conn.getresponse()
        body = json.loads(res.read() or b"{}")
        conn.close()
        return res.status, body

    def test_a_file_is_saved_under_the_projects_uploads_folder(self):
        status, data = self.upload("mock.png", b"\x89PNG\r\nimage")
        self.assertEqual((status, data["kind"], data["name"], data["size"]), (200, "image", "mock.png", 11))
        self.assertTrue(data["path"].startswith(".orchestrator/ui/uploads/"))
        self.assertEqual((self.root / data["path"]).read_bytes(), b"\x89PNG\r\nimage")

    def test_names_are_made_safe_and_types_and_sizes_are_enforced(self):
        status, data = self.upload("..%2F..%2Fevil.log", b"x")
        self.assertEqual(status, 200)
        self.assertTrue(data["path"].startswith(".orchestrator/ui/uploads/") and ".." not in data["name"])
        self.assertEqual(self.upload("run.exe", b"x")[0], 400)
        self.assertEqual(self.upload("empty.png", b"")[0], 400)
        self.assertEqual(self.upload("", b"x", query_name=False)[0], 400)

    def test_oversized_uploads_are_refused_and_the_connection_survives(self):
        with patch.object(ui.new_job_form, "UPLOAD_LIMIT", 10):
            status, data = self.upload("big.png", b"x" * 50)
        self.assertEqual(status, 413)
        self.assertIn("MB", data["error"])
        self.assertEqual(self.request("GET", "/api/state")[0].status, 200)

    def test_uploads_need_the_ui_header(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/uploads?name=a.png", body=b"x", headers={"Authorization": "Bearer test-token", "Content-Type": "application/octet-stream"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_recent_logs_lists_device_pulls_and_run_logs_newest_first_and_skips_noise(self):
        rt = self.root / ".orchestrator"
        (rt / "logs" / "ui").mkdir(parents=True)
        (rt / "logs" / "ui" / "20260922-101010-abcdef-test.log").write_text("test output")
        (rt / "logs" / "ui" / "20260922-101011-abcdef-console.log").write_text("noise")
        (rt / "logs" / "ui" / "20260922-101012-abcdef-fix.log").write_text("")
        pull = rt / "output" / "cloud_logs" / "20260922-090000-s1"
        pull.mkdir(parents=True)
        (pull / "cloud.log").write_text("device lines")
        (pull / "meta.json").write_text(json.dumps({"session": "s1"}))
        os.utime(rt / "logs" / "ui" / "20260922-101010-abcdef-test.log", (1_000, 1_000))
        _, data = self.request("GET", "/api/recent-logs")
        self.assertEqual([(l["kind"], l["label"]) for l in data["logs"]], [("device", "Device launch s1"), ("run", "Test run")])
        self.assertEqual(data["logs"][1]["path"], ".orchestrator/logs/ui/20260922-101010-abcdef-test.log")


class MarkdownRenderTests(unittest.TestCase):
    def render(self, text):
        module = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "markdown.js"
        script = "const m = require(process.argv[1]); process.stdout.write(m.render(JSON.parse(process.argv[2])));"
        result = subprocess.run(["node", "--require", HTML_JS, "-e", script, str(module), json.dumps(text)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_common_constructs(self):
        out = self.render("# Title\n\nA **bold** and _soft_ and `code` line.\n\n- one\n- two\n\n1. first\n2. second\n\n> quoted\n\n---\n")
        for needle in ("<h2>Title</h2>", "<strong>bold</strong>", "<em>soft</em>", "<code>code</code>", "<ul><li>one</li><li>two</li></ul>",
                       "<ol><li>first</li><li>second</li></ol>", "<blockquote>quoted</blockquote>", "<hr>"):
            self.assertIn(needle, out)

    def test_check_boxes_nesting_and_wrapped_lines_in_lists(self):
        out = self.render("- [x] **Done task**\n  Wrapped detail\n  - Done when: it works\n- [ ] Open task\n\nplain")
        self.assertIn('<span class="task done" role="img" aria-label="done">☑</span> <strong>Done task</strong>', out)
        self.assertIn("<br>Wrapped detail", out)
        self.assertIn("<ul><li>Done when: it works</li></ul>", out)
        self.assertIn('<span class="task" role="img" aria-label="not done">☐</span> Open task', out)
        self.assertEqual(out.count("<ul>"), 2)  # one list with one nested list, and the paragraph after it is not swallowed
        self.assertIn("<p>plain</p>", out)

    def test_nested_content_stays_inert(self):
        out = self.render("- a\n  - <script>alert(1)</script> [x](javascript:alert(1))")
        self.assertNotIn("<script", out)
        self.assertNotIn("javascript:", out)

    def test_headings_shift_down_because_the_page_has_its_own_h1(self):
        out = self.render("# A\n## B\n### C")
        self.assertIn("<h2>A</h2>", out)
        self.assertIn("<h3>B</h3>", out)
        self.assertNotIn("<h1>", out)

    def test_tables_and_code_blocks(self):
        out = self.render("| KPI | Event |\n| --- | --- |\n| Claims | `seat_claimed` |\n\n```\nlet x = 1 < 2\n```")
        self.assertIn("<th>KPI</th>", out)
        self.assertIn("<td><code>seat_claimed</code></td>", out)
        self.assertIn("<pre><code>let x = 1 &lt; 2</code></pre>", out)

    def test_markup_and_scripts_in_a_document_are_inert(self):
        out = self.render("<script>alert(1)</script> <img src=x onerror=alert(1)>\n\n[click](javascript:alert(1)) [ok](https://example.com) [bad](data:text/html,x)\n\n**<b>x</b>**")
        self.assertNotIn("<script", out)
        self.assertNotIn("<img", out)
        self.assertNotIn("javascript:", out)
        self.assertNotIn("data:text", out)
        self.assertIn('<a href="https://example.com" target="_blank" rel="noopener noreferrer">ok</a>', out)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", out)

    def test_link_text_with_quotes_cannot_break_out_of_the_tag(self):
        out = self.render('[a" onclick="x](https://e.com/"onmouseover="y)')
        self.assertNotIn('" onclick', out)
        self.assertNotIn('onmouseover="y', out.replace("&quot;onmouseover=&quot;y", ""))

    def test_empty_input(self):
        self.assertEqual(self.render(""), "")


class PaletteLogicTests(unittest.TestCase):
    ENTRIES = [{"label": "Delivery", "hint": "What's live", "order": 1}, {"label": "Deliver to testers", "hint": "", "order": 0},
               {"label": "Seat assignment", "hint": "Ready for review", "order": 2}, {"label": "Inbox", "hint": "Delivery alerts", "order": 1},
               {"label": "Check-up", "hint": "", "order": 1}]

    def ranked(self, query, entries=None):
        module = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "palette.js"
        script = "const p = require(process.argv[1]); const [e, q] = JSON.parse(process.argv[2]); process.stdout.write(JSON.stringify(p.rank(e, q).map((x) => x.label)));"
        result = subprocess.run(["node", "--require", HTML_JS, "-e", script, str(module), json.dumps([entries or self.ENTRIES, query])], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_empty_query_lists_everything_actions_first(self):
        self.assertEqual(self.ranked("")[0], "Deliver to testers")
        self.assertEqual(len(self.ranked("")), 5)

    def test_prefix_beats_word_start_beats_contains_beats_hint(self):
        self.assertEqual(self.ranked("deliv"), ["Delivery", "Deliver to testers", "Inbox"])  # the shorter prefix match is the closer one; Inbox matches only through its hint
        self.assertEqual(self.ranked("testers"), ["Deliver to testers"])
        self.assertEqual(self.ranked("review"), ["Seat assignment"])

    def test_letters_in_order_still_find_it_and_no_match_is_empty(self):
        self.assertEqual(self.ranked("chk"), ["Check-up"])
        self.assertEqual(self.ranked("zzz"), [])

    def test_scattered_letters_must_start_at_a_word(self):
        entries = [{"label": "Turn on browser alerts", "hint": "", "order": 0}, {"label": "Seat assignment", "hint": "", "order": 2}]
        self.assertEqual(self.ranked("seat", entries), ["Seat assignment"])
        self.assertEqual(self.ranked("alrt", entries), ["Turn on browser alerts"])

    def test_matching_ignores_case_and_surrounding_space(self):
        self.assertEqual(self.ranked("  INBOX "), ["Inbox"])

    def test_results_are_capped(self):
        many = [{"label": f"Job {i}", "hint": "", "order": 2} for i in range(100)]
        self.assertEqual(len(self.ranked("job", many)), 40)


class ErrorHintTests(unittest.TestCase):
    def explain(self, message):
        module = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "errors.js"
        script = "const e = require(process.argv[1]); process.stdout.write(JSON.stringify(e.explain(JSON.parse(process.argv[2]))));"
        result = subprocess.run(["node", "--require", HTML_JS, "-e", script, str(module), json.dumps(message)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_known_failures_say_what_to_do_next(self):
        self.assertEqual(self.explain("Failed to fetch"), "Can't reach Orchestrator. Check that it's running and that you're online.")  # the browser's own wording is replaced, not repeated
        self.assertEqual(self.explain("Couldn't save: Load failed"), "Can't reach Orchestrator. Check that it's running and that you're online.")
        self.assertIn("Archived jobs", self.explain("Job not found"))
        self.assertIn("Measure page", self.explain("Amplitude rejected the key (401)"))
        self.assertIn("incoming webhook", self.explain("The webhook answered 404"))
        self.assertIn("gh auth login", self.explain("Not signed in to GitHub yet."))
        self.assertIn("Configuration > Models", self.explain("No model is available. Check Configuration > Models."))

    def test_the_original_message_is_kept_and_punctuated(self):
        out = self.explain("The webhook answered 404")
        self.assertTrue(out.startswith("The webhook answered 404. "))
        self.assertEqual(self.explain("Something odd happened"), "Something odd happened")

    def test_empty_messages_get_a_fallback(self):
        self.assertEqual(self.explain(""), "Something went wrong. Try again.")


class VisualCheckTests(ServerTestCase):
    JOB = "20260922-bug-1"

    def make_check(self, base, name, job=None, png=b"\x89PNG\r\n"):
        folder = self.root / ".orchestrator" / "output" / base / name if base else self.root / ".orchestrator" / "output" / name
        folder.mkdir(parents=True)
        (folder / "shot-1.png").write_bytes(png)
        (folder / "report.md").write_text("# report")
        if job:
            (folder / "meta.json").write_text(json.dumps({"job_id": job}))
        return folder

    def test_finds_the_folders_the_script_actually_writes_and_their_job(self):
        self.make_check("manual", "20260923-101500-visual-check", job=self.JOB)
        self.make_check(None, "visual-check-legacy")
        _, data = self.request("GET", "/api/visual-checks")
        by_id = {c["id"]: c for c in data["checks"]}
        self.assertEqual(by_id["20260923-101500-visual-check"]["job"], self.JOB)
        self.assertIsNone(by_id["visual-check-legacy"]["job"])
        self.assertEqual(by_id["20260923-101500-visual-check"]["screenshots"], ["shot-1.png"])

    def test_screenshots_are_served_from_either_location(self):
        self.make_check("manual", "20260923-101500-visual-check")
        self.make_check(None, "visual-check-legacy")
        for run in ("20260923-101500-visual-check", "visual-check-legacy"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", f"/api/visual-checks/{run}/screenshots/shot-1.png", headers={"Authorization": "Bearer test-token"})
            res = conn.getresponse()
            self.assertEqual((res.status, res.read()[:4], res.getheader("Content-Type")), (200, b"\x89PNG", "image/png"), run)
            conn.close()

    def test_urls_cannot_reach_other_files(self):
        (self.root / ".env").write_text("SECRET=1")
        (self.root / "notes.png").write_bytes(b"x")
        self.make_check("manual", "20260923-101500-visual-check")
        for run, name in (("..", ".env"), ("..", "notes.png"), ("20260923-101500-visual-check", "report.md"), (".hidden", "a.png"),
                          ("20260923-101500-visual-check", "..%2Fnotes.png"), ("20260923-101500-visual-check", "missing.png")):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", f"/api/visual-checks/{run}/screenshots/{name}", headers={"Authorization": "Bearer test-token"})
            res = conn.getresponse()
            body = res.read()
            self.assertEqual(res.status, 404, (run, name))
            self.assertNotIn(b"SECRET", body)
            conn.close()

    def test_a_visual_check_started_from_a_job_is_tagged_with_it(self):
        argv = ui.build_simulator_visual_check({"job": self.JOB}, self.root)
        self.assertEqual(argv[argv.index("--job") + 1], self.JOB)
        self.assertNotIn("--job", ui.build_simulator_visual_check({}, self.root))
        with self.assertRaises(ui.UIError):
            ui.build_simulator_visual_check({"job": "nope"}, self.root)

    def test_the_job_page_only_shows_its_own_checks(self):
        self.assertIn('.filter((c) => c.job === id)', (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text())


class PlanTaskEditEndpointTests(ServerTestCase):
    JOB = "20260922-bug-1"

    def setUp(self):
        super().setUp()
        path = self.root / ".orchestrator" / "jobs" / f"{self.JOB}.json"
        job = json.loads(path.read_text())
        job["plan"] = {"tasks": [{"title": "Repro"}, {"title": "Fix"}, {"title": "Verify"}]}
        job["completed_task_indices"] = [0]
        path.write_text(json.dumps(job))

    def post(self, body):
        return self.request("POST", f"/api/jobs/{self.JOB}/plan-tasks", body=body, headers=UI_HEADERS)

    def saved(self):
        return json.loads((self.root / ".orchestrator" / "jobs" / f"{self.JOB}.json").read_text())

    def test_edit_add_move_and_remove_are_saved_on_the_job(self):
        res, data = self.post({"op": "edit", "index": 1, "title": "Fix the seat bug", "likely_files": "src/seat.py"})
        self.assertEqual((res.status, data["tasks"][1]["title"]), (200, "Fix the seat bug"))
        self.post({"op": "add", "title": "Write notes"})
        self.post({"op": "move", "index": 2, "direction": "up"})
        self.assertEqual([t["title"] for t in self.saved()["plan"]["tasks"]], ["Repro", "Verify", "Fix the seat bug", "Write notes"])
        _, data = self.post({"op": "remove", "index": 3})
        self.assertEqual(len(data["tasks"]), 3)
        self.assertEqual(self.saved()["completed_task_indices"], [0])

    def test_finished_tasks_and_bad_input_are_rejected_without_changes(self):
        before = self.saved()
        for body in ({"op": "edit", "index": 0, "title": "x"}, {"op": "edit", "index": 1, "title": " "}, {"op": "remove", "index": 9},
                     {"op": "move", "index": 1, "direction": "up"}, {"op": "add"}, {"op": "explode"}):
            res, _ = self.post(body)
            self.assertEqual(res.status, 400, body)
        self.assertEqual(self.saved(), before)

    def test_a_running_job_cannot_be_edited(self):
        with patch.object(ui.SessionManager, "running_job_ids", return_value={self.JOB}):
            res, data = self.post({"op": "edit", "index": 1, "title": "x"})
        self.assertEqual(res.status, 409)
        self.assertIn("Pause it", data["error"])

    def test_requires_the_ui_header_and_a_real_job(self):
        res, _ = self.request("POST", f"/api/jobs/{self.JOB}/plan-tasks", body={"op": "add", "title": "x"}, headers={"Content-Type": "application/json"})
        self.assertIn(res.status, (400, 403))
        res, _ = self.request("POST", "/api/jobs/nope/plan-tasks", body={"op": "add", "title": "x"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 404)


class PlanTestCaseEditEndpointTests(ServerTestCase):
    JOB = "20260922-bug-1"

    def setUp(self):
        super().setUp()
        path = self.root / ".orchestrator" / "jobs" / f"{self.JOB}.json"
        job = json.loads(path.read_text())
        job["plan"] = {
            "tasks": [{"title": "Repro"}, {"title": "Fix"}],
            "test_cases": [
                {"id": "TC-1-01", "area": "Lobby", "title": "Seat kept", "type": "unit", "expected": "Seat stays", "tests": ["test_keeps_seat"]},
            ],
        }
        path.write_text(json.dumps(job))

    def post(self, body):
        return self.request("POST", f"/api/jobs/{self.JOB}/plan-test-cases", body=body, headers=UI_HEADERS)

    def saved_cases(self):
        job = json.loads((self.root / ".orchestrator" / "jobs" / f"{self.JOB}.json").read_text())
        return job.get("plan", {}).get("test_cases", [])

    def test_add_edit_and_remove_test_cases_on_job(self):
        # Add
        res, data = self.post({
            "op": "add",
            "title": "Rejoin timeout",
            "area": "Network",
            "type": "integration",
            "priority": "high",
            "expected": "Reconnects within 5s",
            "task": "2",
        })
        self.assertEqual(res.status, 200)
        cases = self.saved_cases()
        self.assertEqual(len(cases), 2)
        added = next(c for c in cases if c["id"] == "TC-1-02")
        self.assertEqual(added["title"], "Rejoin timeout")
        self.assertEqual(added["task"], 2)
        self.assertEqual(added["area"], "Network")

        # Edit
        res, data = self.post({
            "op": "edit",
            "id": "TC-1-02",
            "title": "Rejoin timeout with retry",
            "expected": "Reconnects with exponential backoff",
        })
        self.assertEqual(res.status, 200)
        cases = self.saved_cases()
        edited = next(c for c in cases if c["id"] == "TC-1-02")
        self.assertEqual(edited["title"], "Rejoin timeout with retry")
        self.assertEqual(edited["expected"], "Reconnects with exponential backoff")

        # Remove
        res, data = self.post({"op": "remove", "id": "TC-1-02"})
        self.assertEqual(res.status, 200)
        cases = self.saved_cases()
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["id"], "TC-1-01")

    def test_plan_test_cases_validation_and_concurrency(self):
        res, data = self.post({"op": "add", "title": ""})
        self.assertEqual(res.status, 400)

        res, data = self.post({"op": "edit", "id": "TC-99-99", "title": "Nope"})
        self.assertEqual(res.status, 400)

        res, data = self.post({"op": "remove", "id": "TC-99-99"})
        self.assertEqual(res.status, 400)

        with patch.object(ui.SessionManager, "running_job_ids", return_value={self.JOB}):
            res, data = self.post({"op": "add", "title": "Cannot edit", "expected": "Fails"})
        self.assertEqual(res.status, 409)
        self.assertIn("Pause it", data["error"])

    def test_requires_the_ui_header_and_real_job(self):
        res, _ = self.request("POST", f"/api/jobs/{self.JOB}/plan-test-cases", body={"op": "add", "title": "x", "expected": "y"}, headers={"Content-Type": "application/json"})
        self.assertIn(res.status, (400, 403))
        res, _ = self.request("POST", "/api/jobs/nope/plan-test-cases", body={"op": "add", "title": "x", "expected": "y"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 404)


class JobBriefTests(ServerTestCase):
    JOB = "20260922-bug-1"

    def test_get_and_post_job_brief(self):
        res, data = self.request("GET", f"/api/jobs/{self.JOB}/brief")
        self.assertEqual(res.status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["text"], "# Brief\n")
        self.assertEqual(data["path"], f".orchestrator/output/{self.JOB}/brief.md")
        self.assertEqual(data["runtime_path"], f"output/{self.JOB}/brief.md")

        new_text = "# Updated Bug Brief\n\nFix the seat presence bug."
        res, data = self.request("POST", f"/api/jobs/{self.JOB}/brief", body={"text": new_text}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["text"], new_text)

        brief_on_disk = (self.root / ".orchestrator" / "output" / self.JOB / "brief.md").read_text()
        self.assertEqual(brief_on_disk, new_text)

        res, data = self.request("GET", f"/api/jobs/{self.JOB}/brief")
        self.assertEqual(res.status, 200)
        self.assertEqual(data["text"], new_text)

        res, detail = self.request("GET", f"/api/jobs/{self.JOB}")
        self.assertEqual(res.status, 200)
        brief_doc = next(d for d in detail["docs"] if d["name"] == "brief.md")
        self.assertEqual(brief_doc["text"], new_text)
        self.assertEqual(brief_doc["path"], f".orchestrator/output/{self.JOB}/brief.md")
        self.assertEqual(brief_doc["runtime_path"], f"output/{self.JOB}/brief.md")

    def test_post_brief_validations(self):
        res, data = self.request("POST", f"/api/jobs/{self.JOB}/brief", body={"text": "   "}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)
        self.assertIn("Brief text cannot be empty", data["error"])

        with patch.object(ui.SessionManager, "running_job_ids", return_value={self.JOB}):
            res, data = self.request("POST", f"/api/jobs/{self.JOB}/brief", body={"text": "Hello"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 409)
        self.assertIn("Pause it before editing its brief", data["error"])

        res, _ = self.request("POST", "/api/jobs/missing-job-xyz/brief", body={"text": "Hello"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 404)


class ProductEndpointTests(ServerTestCase):
    """/api/product: the one product requirements document, its history, settings, designs, import and AI help."""
    PITCH = "A word game for two friends.\n\nBuilt for: iOS app"

    def post(self, path, body):
        return self.request("POST", path, body=body, headers=UI_HEADERS)

    def raw_post(self, path, data, ctype="application/octet-stream", headers={"X-Orchestrator-UI": "1"}):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, body=data, headers={"Authorization": "Bearer test-token", "Content-Type": ctype, **headers})
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        try:
            return res, json.loads(raw)
        except json.JSONDecodeError:
            return res, raw

    def raw_get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"Authorization": "Bearer test-token"})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data

    def finish(self, started):
        """A model-backed request answers at once with a task; wait for it the way the page does. Returns (final task, task id)."""
        res, data = started
        self.assertEqual(res.status, 202, data)
        for _ in range(100):
            _, task = self.request("GET", f"/api/product/task/{data['task']}")
            if task["status"] != "running":
                return task
            time.sleep(0.05)
        self.fail("the task never finished")

    def doc(self):
        return prd_mod.Prd(self.root, self.root / ".orchestrator")

    def test_a_new_project_shows_the_template_with_five_empty_sections_and_updates_on(self):
        _, data = self.request("GET", "/api/product")
        self.assertEqual((data["exists"], data["auto_update"], data["notice"], data["history"], data["path"]), (False, True, None, [], "docs/product/prd.md"))
        self.assertEqual([s["id"] for s in data["sections"]], ["pitch", "who", "features", "look", "not"])
        self.assertFalse(any(s["filled"] for s in data["sections"]))
        self.assertIn("## Pitch", data["text"])
        self.assertFalse((self.root / "docs" / "product" / "prd.md").exists())  # looking does not create files

    def test_editing_a_section_saves_it_and_records_a_version(self):
        res, data = self.post("/api/product", {"section": "pitch", "body": self.PITCH})
        self.assertEqual(res.status, 200)
        self.assertTrue(next(s for s in data["sections"] if s["id"] == "pitch")["filled"])
        self.assertIn("A word game", (self.root / "docs" / "product" / "prd.md").read_text())
        self.assertEqual([(h["source"], h["summary"]) for h in data["history"]], [("you", "Edited Pitch")])

    def test_the_whole_document_can_be_saved_and_bad_input_is_refused(self):
        res, data = self.post("/api/product", {"text": prd_mod.replace_section(prd_mod.template(), "pitch", self.PITCH)})
        self.assertEqual(res.status, 200)
        for bad in ({"section": "nope", "body": "x"}, {"text": "x" * (prd_mod.MAX_DOC_CHARS + 1)}, {"section": "pitch", "body": "x", "source": "evil"}):
            self.assertEqual(self.post("/api/product", bad)[0].status, 400, bad)

    def test_an_accepted_draft_or_import_is_recorded_as_such_and_other_sources_are_refused(self):
        text = prd_mod.replace_section(prd_mod.template(), "pitch", self.PITCH)
        _, d = self.post("/api/product", {"text": text, "source": "draft", "summary": "Drafted"})
        self.assertEqual(d["history"][0]["source"], "draft")
        _, d = self.post("/api/product", {"text": text + "\nmore", "source": "import"})
        self.assertEqual(d["history"][0]["source"], "import")
        self.assertEqual(self.post("/api/product", {"text": text, "source": "ai"})[0].status, 400)  # that was "Help me with this", now gone

    def test_saving_requires_the_ui_header(self):
        res, _ = self.request("POST", "/api/product", body={"section": "pitch", "body": "x"}, headers={"Content-Type": "application/json"})
        self.assertIn(res.status, (400, 403))
        self.assertFalse((self.root / "docs" / "product" / "prd.md").exists())

    def test_automatic_updates_can_be_switched_off_and_stay_off(self):
        _, data = self.post("/api/product/settings", {"auto_update": False})
        self.assertFalse(data["auto_update"])
        self.assertFalse(self.request("GET", "/api/product")[1]["auto_update"])
        self.assertTrue(self.post("/api/product/settings", {"auto_update": True})[1]["auto_update"])

    def test_history_shows_what_changed_and_any_version_can_be_restored(self):
        self.post("/api/product", {"section": "pitch", "body": "First idea"})
        self.post("/api/product", {"section": "pitch", "body": "Second idea"})
        _, data = self.request("GET", "/api/product")
        newest, oldest = data["history"][0], data["history"][1]
        _, v = self.request("GET", f"/api/product/history/{newest['id']}")
        self.assertIn("-First idea", v["diff"])
        self.assertIn("+Second idea", v["diff"])
        res, restored = self.post("/api/product/revert", {"id": oldest["id"]})
        self.assertEqual(res.status, 200)
        self.assertIn("First idea", (self.root / "docs" / "product" / "prd.md").read_text())
        self.assertEqual(restored["history"][0]["source"], "revert")
        self.assertEqual(len(restored["history"]), 3)  # nothing was lost
        self.assertEqual(self.post("/api/product/revert", {"id": "ghost"})[0].status, 400)
        self.assertEqual(self.request("GET", "/api/product/history/ghost")[0].status, 400)

    def test_an_automatic_update_is_announced_in_state_until_dismissed_and_can_be_undone(self):
        self.post("/api/product", {"section": "pitch", "body": self.PITCH})
        doc = self.doc()
        changed = prd_mod.replace_section(doc.read(), "features", "- Rematch after any game")
        job = {"job_id": "j1", "title": "Add rematch", "type": "feature-plan"}
        out = prd_mod.run_update(doc, job, lambda prompt: json.dumps({"changed": True, "summary": "Added rematch", "markdown": changed}))
        self.assertEqual(out["status"], "updated")
        _, state = self.request("GET", "/api/state")
        self.assertEqual((state["product_notice"]["summary"], state["product_notice"]["job_title"]), ("Added rematch", "Add rematch"))
        _, data = self.request("GET", "/api/product")
        self.assertEqual((data["notice"]["id"], data["history"][0]["source"]), (state["product_notice"]["id"], "auto"))
        self.post("/api/product/revert", {"id": data["history"][1]["id"]})  # Undo
        self.assertNotIn("Rematch", (self.root / "docs" / "product" / "prd.md").read_text())
        self.assertEqual(self.post("/api/product/dismiss", {})[1], {"ok": True})
        self.assertIsNone(self.request("GET", "/api/state")[1]["product_notice"])

    def test_links_must_be_web_links(self):
        self.post("/api/product", {"section": "pitch", "body": self.PITCH})
        res, data = self.post("/api/product/reference", {"label": "Figma: home", "url": "https://figma.com/file/abc"})
        self.assertEqual(res.status, 200)
        self.assertIn("[Figma: home](https://figma.com/file/abc)", next(s for s in data["sections"] if s["id"] == "look")["body"])
        for bad in ("javascript:alert(1)", "/etc/passwd", ""):
            self.assertEqual(self.post("/api/product/reference", {"url": bad})[0].status, 400, bad)

    def test_a_design_is_saved_in_the_repo_listed_under_look_and_feel_and_shown_safely(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        res, data = self.raw_post("/api/product/design?name=Home%20sketch.png", png)
        self.assertEqual(res.status, 200, data)
        saved = [p.name for p in (self.root / "docs" / "product" / "designs").iterdir()]
        self.assertEqual(len(saved), 1)
        self.assertIn(f"designs/{saved[0]}", next(s for s in data["sections"] if s["id"] == "look")["body"])
        res, body = self.raw_get(f"/api/product/design/{saved[0]}")
        self.assertEqual((res.status, res.getheader("Content-Type"), body), (200, "image/png", png))
        # Anything that is not a plain image is a download, never shown in the app's own origin.
        res, _ = self.raw_post("/api/product/design?name=page.html", b"<script>alert(1)</script>")
        html_name = next(p.name for p in (self.root / "docs" / "product" / "designs").iterdir() if p.suffix == ".html")
        res, body = self.raw_get(f"/api/product/design/{html_name}")
        self.assertEqual(res.getheader("Content-Type"), "application/octet-stream")
        self.assertIn("attachment", res.getheader("Content-Disposition"))
        for name in ("..%2F..%2Fproject.json", "nope.png", "a%2Fb.png"):
            self.assertEqual(self.raw_get(f"/api/product/design/{name}")[0].status, 404, name)

    def test_design_uploads_refuse_logs_empty_files_and_a_missing_ui_header(self):
        self.assertEqual(self.raw_post("/api/product/design?name=out.log", b"text")[0].status, 400)
        self.assertEqual(self.raw_post("/api/product/design?name=a.png", b"")[0].status, 400)
        self.assertEqual(self.raw_post("/api/product/design?name=a.png", b"x", headers={})[0].status, 403)

    def test_there_is_no_help_me_endpoint_any_more(self):
        res, _ = self.post("/api/product/refine", {"mode": "propose", "instruction": "x"})
        self.assertEqual(res.status, 404)

    def test_a_slow_model_call_returns_at_once_and_is_followed_by_polling(self):
        # A quick tunnel closes any request held open for ~100 s, so the answer must not depend on one long request.
        release = threading.Event()
        before = prd_mod.template()

        def slow(self_, root, prompt, model="", timeout=150):
            release.wait(5)
            return json.dumps({"summary": "done", "markdown": prd_mod.replace_section(before, "pitch", "From the model")})

        with patch.object(ui.UIHandler, "_model_call", slow):
            started = time.time()
            res, data = self.post("/api/product/import", {"text": "my old prd"})
            self.assertEqual(res.status, 202)
            self.assertLess(time.time() - started, 2)  # it did not wait for the model
            _, running = self.request("GET", f"/api/product/task/{data['task']}")
            self.assertEqual(running, {"status": "running"})
            release.set()
            task = self.finish((res, data))
        self.assertEqual(task["status"], "done")
        self.assertIn("+From the model", task["result"]["diff"])

    def test_a_reply_in_the_wrong_format_is_asked_for_again_once(self):
        calls = []

        def fake(self_, root, prompt, model="", timeout=150):
            calls.append(prompt)
            return "Sure! I wrote it to a file." if len(calls) == 1 else json.dumps({"summary": "ok", "markdown": prd_mod.replace_section(prd_mod.template(), "pitch", "Second try")})

        with patch.object(ui.UIHandler, "_model_call", fake):
            task = self.finish(self.post("/api/product/import", {"text": "my old prd"}))
        self.assertEqual(task["status"], "done")
        self.assertEqual(len(calls), 2)
        self.assertIn("FORMAT CORRECTION", calls[1])
        self.assertNotIn("FORMAT CORRECTION", calls[0])

    def test_a_model_that_keeps_getting_the_format_wrong_ends_in_a_clear_error_not_a_hang(self):
        with patch.object(ui.UIHandler, "_model_call", lambda self_, root, prompt, model="", timeout=150: "not json"):
            task = self.finish(self.post("/api/product/import", {"text": "my old prd"}))
        self.assertEqual(task["status"], "error")
        self.assertIn("expected form", task["error"])

    def test_a_model_that_cannot_run_reports_why(self):
        with patch.object(ui.UIHandler, "_model_call", side_effect=ui.UIError("No model is available.", 502)):
            task = self.finish(self.post("/api/product/import", {"text": "my old prd"}))
        self.assertEqual((task["status"], task["error"]), ("error", "No model is available."))

    def test_an_unknown_or_expired_task_says_so(self):
        res, data = self.request("GET", "/api/product/task/nope")
        self.assertEqual(res.status, 404)
        self.assertIn("Start it again", data["error"])

    def test_an_existing_project_can_be_read_and_drafted_without_saving_or_inventing_not_this(self):
        # The fixture project has files, so first make it a project with nothing to read.
        for child in list(self.root.iterdir()):
            if child.name != ".orchestrator" and child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        res, data = self.post("/api/product/draft", {})
        self.assertEqual(res.status, 400)
        self.assertIn("nothing in this project to read", data["error"])
        self.assertFalse(self.request("GET", "/api/product")[1]["can_draft"])
        (self.root / "README.md").write_text("# Word Duel\n\nA word game for two friends.")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "Add scoring"], cwd=self.root, check=True)
        self.assertTrue(self.request("GET", "/api/product")[1]["can_draft"])
        seen = []

        def fake(self_, root, prompt, model="", timeout=150):
            seen.append((prompt, timeout))
            return json.dumps({"summary": "Inferred from the README.", "markdown": prd_mod.replace_section(prd_mod.replace_section(
                prd_mod.template(), "pitch", "A word game for two friends."), "not", "- Not an IDE")})

        with patch.object(ui.UIHandler, "_model_call", fake):
            task = self.finish(self.post("/api/product/draft", {}))
        self.assertEqual(task["status"], "done", task)
        p = task["result"]
        self.assertIn("A word game for two friends.", seen[0][0])  # the README was read
        self.assertIn("Add scoring", seen[0][0])  # and the history
        self.assertGreaterEqual(seen[0][1], 240)  # reading a project takes a while
        self.assertIn("+A word game for two friends.", p["diff"])
        self.assertNotIn("Not an IDE", p["markdown"])  # guardrails are the person's
        self.assertIn("only you can say", p["summary"])
        self.assertFalse((self.root / "docs" / "product" / "prd.md").exists())  # nothing is saved until they accept

    def test_in_flight_draft_task_can_be_cancelled(self):
        (self.root / "README.md").write_text("# Word Duel\n\nA word game for two friends.")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"], cwd=self.root, check=True)

        def slow_call(*a, **k):
            time.sleep(5)
            return "{}"

        with patch.object(ui.UIHandler, "_model_call", slow_call):
            res, data = self.post("/api/product/draft", {})
            self.assertEqual(res.status, 202)
            task_id = data["task"]

            # Cancel via DELETE with UI_HEADERS
            res_del, data_del = self.request("DELETE", f"/api/product/task/{task_id}", headers=UI_HEADERS)
            self.assertEqual(res_del.status, 200)
            self.assertEqual(data_del.get("status"), "canceled")

            # Polling task status returns canceled
            _, task = self.request("GET", f"/api/product/task/{task_id}")
            self.assertEqual(task["status"], "canceled")

            # POST /cancel route also works
            res_post, data_post = self.post(f"/api/product/task/{task_id}/cancel", {})
            self.assertEqual(res_post.status, 200)
            self.assertEqual(data_post.get("status"), "canceled")

    def test_product_overview_returns_model_and_available_models(self):
        _, data = self.request("GET", "/api/product")
        self.assertEqual(data["model"], "claude-sonnet-4-6")
        self.assertTrue(isinstance(data["available_models"], list))
        self.assertTrue(any(m["id"] == "claude-sonnet-4-6" for m in data["available_models"]))

    def test_switching_product_model_persists_and_updates_subsequent_reads(self):
        res, data = self.post("/api/product/model", {"model": "gemini-3.1-pro-preview"})
        self.assertEqual(res.status, 200)
        self.assertEqual(data, {"ok": True, "model": "gemini-3.1-pro-preview"})
        _, get_data = self.request("GET", "/api/product")
        self.assertEqual(get_data["model"], "gemini-3.1-pro-preview")

        # Rejects empty or invalid model
        self.assertEqual(self.post("/api/product/model", {"model": ""})[0].status, 400)
        self.assertEqual(self.post("/api/product/model", {})[0].status, 400)

    def test_drafting_forwards_selected_model_to_model_call(self):
        (self.root / "README.md").write_text("# Word Duel\n\nA word game.")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"], cwd=self.root, check=True)
        seen_models = []

        def fake_call(self_, root, prompt, model="", timeout=150):
            seen_models.append(model)
            return json.dumps({"summary": "Drafted", "markdown": prd_mod.template()})

        with patch.object(ui.UIHandler, "_model_call", fake_call):
            # 1. Draft with explicit model in body
            self.finish(self.post("/api/product/draft", {"model": "gpt-4o"}))
            self.assertEqual(seen_models[-1], "gpt-4o")

            # 2. Draft without model uses the persisted model
            self.post("/api/product/model", {"model": "claude-opus-4-8"})
            self.finish(self.post("/api/product/draft", {}))
            self.assertEqual(seen_models[-1], "claude-opus-4-8")

    def test_model_calls_get_an_empty_scratch_folder_never_the_project(self):
        calls = []
        real = ui.subprocess.run

        def fake(argv, *a, **k):
            if "job_chat_run.py" in " ".join(argv):
                scratch = Path(argv[argv.index("--cwd") + 1])
                calls.append((scratch, list(scratch.iterdir()), scratch.is_relative_to(self.root)))
                return subprocess.CompletedProcess(argv, 0, stdout="<<<ORCHESTRATOR-REPLY>>>\nhello", stderr="")
            return real(argv, *a, **k)

        handler = ui.UIHandler.__new__(ui.UIHandler)
        handler.server = self.server
        with patch.object(ui.subprocess, "run", side_effect=fake):
            self.assertEqual(handler._model_call(self.root, "hi").strip(), "hello")
        scratch, contents, inside = calls[0]
        self.assertEqual((contents, inside), ([], False))  # empty, and not inside the project
        self.assertFalse(scratch.exists())  # and cleaned up afterwards

    def test_importing_pasted_text_or_a_file_proposes_the_five_sections_and_saves_nothing(self):
        proposal = json.dumps({"summary": "Kept everything", "markdown": prd_mod.replace_section(prd_mod.template(), "pitch", "From my old PRD")})
        seen = []

        def fake(self_, root, prompt, model="", timeout=150):
            seen.append((prompt, timeout))
            return proposal

        with patch.object(ui.UIHandler, "_model_call", fake):
            p = self.finish(self.post("/api/product/import", {"text": "OLD PRD: a word game"}))["result"]
            self.assertEqual(p["summary"], "Kept everything")
            self.assertIn("OLD PRD: a word game", seen[0][0])
            self.assertIn("+From my old PRD", p["diff"])
            p = self.finish(self.raw_post("/api/product/import?name=prd.md", b"# My PRD\n\nA word game from a file"))["result"]
            self.assertIn("A word game from a file", seen[1][0])
        self.assertGreaterEqual(seen[0][1], 240)  # reading a long document takes a while
        self.assertFalse((self.root / "docs" / "product" / "prd.md").exists())

    def test_import_refuses_unsupported_files_empty_input_and_a_missing_ui_header(self):
        self.assertEqual(self.raw_post("/api/product/import?name=deck.pptx", b"x")[0].status, 400)
        self.assertEqual(self.post("/api/product/import", {"text": "   "})[0].status, 400)
        self.assertEqual(self.raw_post("/api/product/import?name=a.md", b"x", headers={})[0].status, 403)

    def test_an_older_projects_documents_become_the_prd_the_first_time_it_is_opened(self):
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs" / "product-brief.md").write_text("# X\n\n## What it is\n\nA word game from the old brief.\n")
        _, data = self.request("GET", "/api/product")
        self.assertTrue(data["exists"])
        self.assertIn("A word game from the old brief.", next(s for s in data["sections"] if s["id"] == "pitch")["body"])
        self.assertEqual(data["history"][0]["source"], "migration")
        self.assertTrue((self.root / "docs" / "product-brief.md").exists())  # the old file stays

    def test_check_up_follows_what_is_written(self):
        def item():
            return next(i for i in self.request("GET", "/api/health")[1]["items"] if i["id"] == "prd")
        self.assertEqual((item()["status"], item()["route"]), ("todo", "#/product"))
        for section, body in (("pitch", self.PITCH), ("who", "Two friends"), ("features", "- Score a word")):
            self.post("/api/product", {"section": section, "body": body})
        self.assertEqual(item()["status"], "ok")
        self.post("/api/product/settings", {"auto_update": False})
        self.assertIn("Automatic updates are off", item()["detail"])


class DocsEndpointTests(ServerTestCase):
    """/api/docs: one place for the product requirements, every feature and job, and the project's own files."""

    def setUp(self):
        super().setUp()
        rt = self.root / ".orchestrator"
        (rt / "jobs" / "archive").mkdir(parents=True, exist_ok=True)
        ui.feature_store.create(rt, "Rematch", summary="Play again with the same person")
        live = {"job_id": "20261001-feature-7", "title": "Add rematch", "type": "feature-plan", "status": "review-needed", "feature": "rematch", "pr_number": 7, "issue_number": 7,
                "plan": {"summary": "Players can start another game", "tasks": [{"title": "Button", "acceptance_criteria": ["One tap"]}]}, "completed_task_indices": [0],
                "ai_modified_files": ["app/rematch.py"]}
        archived = {"job_id": "20260930-feature-5", "title": "Old finished job", "type": "feature-plan", "status": "completed", "feature": "rematch", "plan": {"summary": "Done long ago"}}
        (rt / "jobs" / "20261001-feature-7.json").write_text(json.dumps(live))
        (rt / "jobs" / "archive" / "20260930-feature-5.json").write_text(json.dumps(archived))
        (rt / "jobs" / "20261001-feature-7_task_1.json").write_text(json.dumps(live))  # the worker's scratch copy is not a job
        review = rt / "output" / "pr-7"
        review.mkdir(parents=True)
        (review / "review.md").write_text("Looks right to me. One nit about naming.")
        (self.root / "README.md").write_text("# Word Duel\n\nA game")
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs" / "guide.md").write_text("# How to play\n\nTake turns.")
        (self.root / "secrets.md").write_text("TOKEN")

    def test_the_index_lists_product_features_jobs_including_finished_ones_and_files(self):
        _, d = self.request("GET", "/api/docs")
        self.assertEqual((d["product"]["exists"], d["product"]["total"]), (False, 5))
        self.assertEqual([(f["id"], f["jobs"], f["jobs_done"]) for f in d["features"]], [("rematch", 2, 1)])
        ids = [j["id"] for j in d["jobs"]]
        self.assertIn("20261001-feature-7", ids)
        self.assertIn("20260930-feature-5", ids)  # finished jobs are in the archive but still documented
        self.assertFalse([i for i in ids if "_task_" in i])
        archived = next(j for j in d["jobs"] if j["id"] == "20260930-feature-5")
        self.assertEqual((archived["archived"], archived["feature_name"], archived["blurb"]), (True, "Rematch", "Done long ago"))
        paths = [f["path"] for f in d["files"]]
        self.assertIn("README.md", paths)
        self.assertIn("docs/guide.md", paths)
        self.assertNotIn("secrets.md", paths)

    def test_the_product_requirements_show_when_written(self):
        self.request("POST", "/api/product", body={"section": "pitch", "body": "A quick word game for two friends."}, headers=UI_HEADERS)
        _, d = self.request("GET", "/api/docs")
        self.assertEqual((d["product"]["exists"], d["product"]["written"]), (True, 1))
        self.assertIn("quick word game", d["product"]["pitch"])

    def test_a_job_page_is_composed_from_the_job_its_review_and_its_feature(self):
        res, d = self.request("GET", "/api/docs/job/20261001-feature-7")
        self.assertEqual((res.status, d["title"]), (200, "Add rematch"))
        for needle in ("**Feature:** Rematch", "Players can start another game", "- [x] **Button**", "Done when: One tap", "`app/rematch.py`", "Looks right to me", "**Pull request:** #7"):
            self.assertIn(needle, d["markdown"], needle)
        _, old = self.request("GET", "/api/docs/job/20260930-feature-5")
        self.assertIn("Done long ago", old["markdown"])  # an archived job works too

    def test_a_feature_page_lists_every_job_that_went_into_it(self):
        _, d = self.request("GET", "/api/docs/feature/rematch")
        self.assertEqual(d["title"], "Rematch")
        for needle in ("Play again with the same person", "## Jobs (1 of 2 done)", "**Add rematch**", "**Old finished job**", "Done long ago"):
            self.assertIn(needle, d["markdown"], needle)

    def test_project_files_open_only_when_they_are_part_of_the_documentation(self):
        _, d = self.request("GET", "/api/docs/file?path=docs%2Fguide.md")
        self.assertEqual((d["title"], "Take turns." in d["markdown"]), ("How to play", True))
        for bad in ("secrets.md", "..%2Foutside.md", "%2Fetc%2Fpasswd", ".orchestrator%2Fjobs%2Fx.json", ""):
            self.assertEqual(self.request("GET", f"/api/docs/file?path={bad}")[0].status, 404, bad)

    def test_unknown_jobs_features_and_routes_are_404(self):
        for path in ("/api/docs/job/nope", "/api/docs/feature/nope", "/api/docs/job/..%2F..%2Fproject", "/api/docs/whatever"):
            self.assertEqual(self.request("GET", path)[0].status, 404, path)

    def test_the_export_is_one_document_with_everything_in_it(self):
        self.request("POST", "/api/product", body={"section": "pitch", "body": "A quick word game."}, headers=UI_HEADERS)
        _, d = self.request("GET", "/api/docs/export")
        md = d["markdown"]
        self.assertTrue(d["name"].endswith("-documentation.md"))
        for needle in ("project documentation", "A quick word game.", "Rematch", "Add rematch", "Old finished job", "File: `README.md`", "Take turns.", "Looks right to me"):
            self.assertIn(needle, md, needle)
        self.assertNotIn("TOKEN", md)
        self.assertEqual([l for l in md.splitlines() if l.startswith("# ")], [md.splitlines()[0]])  # one top-level title

    def test_documentation_is_read_only_and_needs_a_session(self):
        self.assertEqual(self.request("GET", "/api/docs", auth=False)[0].status, 401)
        self.assertIn(self.request("POST", "/api/docs", body={}, headers=UI_HEADERS)[0].status, (404, 405))


class PipelineTests(ServerTestCase):
    def test_job_detail_does_not_invent_pipeline_models(self):
        res, data = self.request("GET", "/api/jobs/20260922-bug-1")
        self.assertEqual(data["pipeline"], {"planner": None, "builder": None, "reviewer": None})


class FeatureEndpointTests(ServerTestCase):
    JOB = "20260922-bug-1"

    def post(self, path, body):
        return self.request("POST", path, body=body, headers=UI_HEADERS)

    def test_create_assign_and_roll_up(self):
        res, data = self.post("/api/features", {"name": "Lobby", "paths": "Sources/Lobby/"})
        self.assertEqual(res.status, 200)
        self.assertEqual(data["features"][0]["id"], "lobby")
        self.assertEqual(data["unassigned"], [self.JOB])
        res, _ = self.post(f"/api/jobs/{self.JOB}/feature", {"feature": "lobby"})
        self.assertEqual(res.status, 200)
        res, data = self.request("GET", "/api/features")
        feature = data["features"][0]
        self.assertEqual((feature["status"], feature["jobs_total"], feature["job_ids"]), ("in-progress", 1, [self.JOB]))
        self.assertEqual(data["unassigned"], [])
        res, jobs = self.request("GET", "/api/jobs")
        self.assertEqual(jobs["jobs"][0]["feature"], "lobby")

    def test_assigning_to_an_unknown_feature_fails_and_unassign_works(self):
        res, _ = self.post(f"/api/jobs/{self.JOB}/feature", {"feature": "nope"})
        self.assertEqual(res.status, 404)
        self.post("/api/features", {"name": "Lobby"})
        self.post(f"/api/jobs/{self.JOB}/feature", {"feature": "lobby"})
        self.post(f"/api/jobs/{self.JOB}/feature", {"feature": ""})
        _, jobs = self.request("GET", "/api/jobs")
        self.assertIsNone(jobs["jobs"][0]["feature"])

    def test_complete_then_new_work_reopens(self):
        self.post("/api/features", {"name": "Lobby"})
        _, data = self.post("/api/features/lobby", {"status": "complete"})
        self.assertEqual(data["features"][0]["status"], "complete")
        self.post(f"/api/jobs/{self.JOB}/feature", {"feature": "lobby"})
        _, data = self.request("GET", "/api/features")
        self.assertEqual((data["features"][0]["status"], data["features"][0]["reopened"]), ("in-progress", True))

    def test_overlapping_feature_paths_are_reported(self):
        self.post("/api/features", {"name": "Lobby", "paths": "Sources/Lobby/"})
        _, data = self.post("/api/features", {"name": "Seats", "paths": "Sources/Lobby/Seat.swift"})
        self.assertEqual(data["overlaps"][0]["features"], ["lobby", "seats"])

    def test_delete_unassigns_its_jobs(self):
        self.post("/api/features", {"name": "Lobby"})
        self.post(f"/api/jobs/{self.JOB}/feature", {"feature": "lobby"})
        res, data = self.request("DELETE", "/api/features/lobby", headers=UI_HEADERS)
        self.assertEqual((res.status, data["features"]), (200, []))
        _, jobs = self.request("GET", "/api/jobs")
        self.assertIsNone(jobs["jobs"][0]["feature"])

    def test_dependencies_through_the_api(self):
        self.post("/api/features", {"name": "Auth"})
        _, data = self.post("/api/features", {"name": "Lobby", "depends_on": ["auth"]})
        lobby = next(f for f in data["features"] if f["id"] == "lobby")
        self.assertEqual((lobby["layer"], lobby["waiting_on"]), (1, ["auth"]))
        res, err = self.post("/api/features/auth", {"depends_on": ["lobby"]})
        self.assertEqual(res.status, 400)
        self.assertIn("circle", err["error"])

    def test_finished_jobs_still_count_toward_their_feature(self):
        archive = self.root / ".orchestrator" / "jobs" / "archive"
        archive.mkdir()
        (archive / "20260901-feature-9.json").write_text(json.dumps(
            {"title": "Shipped", "type": "feature", "status": "completed", "feature": "lobby"}))
        (archive / "20260901-feature-8.json").write_text(json.dumps(
            {"title": "Thrown away", "type": "feature", "status": "discarded", "feature": "lobby"}))
        self.post("/api/features", {"name": "Lobby"})
        self.post(f"/api/jobs/{self.JOB}/feature", {"feature": "lobby"})
        _, data = self.request("GET", "/api/features")
        lobby = data["features"][0]
        self.assertEqual((lobby["jobs_total"], lobby["jobs_done"]), (2, 1))
        self.assertEqual([j["id"] for j in data["archived_jobs"]], ["20260901-feature-9"])

    def test_validation_and_ui_header(self):
        res, _ = self.post("/api/features", {"name": " "})
        self.assertEqual(res.status, 400)
        res, _ = self.post("/api/features/missing", {"status": "complete"})
        self.assertEqual(res.status, 404)
        res, _ = self.request("POST", "/api/features", body={"name": "x"}, headers={"Content-Type": "application/json"})
        self.assertIn(res.status, (400, 403))


class NotificationLogicTests(unittest.TestCase):
    def events(self, prev, nxt):
        module = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "notifications.js"
        script = "const n = require(process.argv[1]); const [p, x] = JSON.parse(process.argv[2]); process.stdout.write(JSON.stringify(n.events(p, x)));"
        result = subprocess.run(["node", "--require", HTML_JS, "-e", script, str(module), json.dumps([prev, nxt])], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_first_poll_is_silent(self):
        self.assertEqual(self.events(None, [{"id": "r1", "running": False, "exit_code": 0}]), [])

    def test_finished_run_notifies_done_and_links_to_the_job(self):
        out = self.events([{"id": "r1", "running": True, "title": "Build", "job": "j1"}],
                          [{"id": "r1", "running": False, "exit_code": 0, "title": "Build", "job": "j1"}])
        self.assertEqual([(e["kind"], e["hash"]) for e in out], [("done", "#/jobs/j1")])

    def test_failed_run_notifies_a_problem(self):
        out = self.events([{"id": "r1", "running": True, "title": "Test"}],
                          [{"id": "r1", "running": False, "exit_code": 2, "title": "Test"}])
        self.assertEqual(out[0]["kind"], "problem")
        self.assertIn("exit 2", out[0]["body"])
        self.assertEqual(out[0]["hash"], "#/runs/r1")

    def test_waiting_runs_are_announced_through_the_inbox_not_the_run_list(self):
        running = {"id": "r1", "running": True, "waiting": False, "title": "Plan"}
        self.assertEqual(self.events([running], [{**running, "waiting": True}]), [])

    def inbox_events(self, prev, nxt):
        module = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "notifications.js"
        script = "const n = require(process.argv[1]); const [p, x] = JSON.parse(process.argv[2]); process.stdout.write(JSON.stringify(n.inboxEvents(p, x)));"
        result = subprocess.run(["node", "--require", HTML_JS, "-e", script, str(module), json.dumps([prev, nxt])], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_new_inbox_items_notify_once_and_first_poll_is_silent(self):
        a = {"id": "job:a", "title": "Login", "label": "Question for you", "reason": "Which provider?", "hash": "#/jobs/a"}
        b = {"id": "job:b", "title": "Chat", "label": "Tests failing", "reason": "Run a fix", "hash": "#/jobs/b"}
        self.assertEqual(self.inbox_events(None, [a]), [])
        out = self.inbox_events([a], [a, b])
        self.assertEqual([(e["title"], e["body"], e["hash"]) for e in out], [("Tests failing", "Chat: Run a fix", "#/jobs/b")])
        self.assertEqual(self.inbox_events([a, b], [a, b]), [])
        self.assertEqual(self.inbox_events([a, b], [a]), [])

    def test_unchanged_and_unknown_runs_are_silent(self):
        run = {"id": "r1", "running": True, "waiting": False}
        self.assertEqual(self.events([run], [run, {"id": "new", "running": False, "exit_code": 0}]), [])


class JobDetailPrinciplesTests(unittest.TestCase):
    """Job detail keeps one primary action and no terminal-style leftovers."""

    @classmethod
    def setUpClass(cls):
        cls.source = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        start = cls.source.index("pages.job = async")
        cls.job_page = cls.source[start:cls.source.index("const JOB_TYPE_INFO", start)]

    def test_no_hotkey_labels_or_terminal_hints(self):
        self.assertNotIn("data-key=", self.job_page)
        self.assertNotIn("Press '", self.job_page)
        self.assertNotIn("└─", self.job_page)

    def test_no_action_tile_grid(self):
        self.assertNotIn("action-tile", self.job_page)

    def test_context_is_grouped_by_role_with_one_way_to_attach(self):
        self.assertNotIn("Linked tickets, errors", self.source)  # no by-source junk drawer
        self.assertNotIn("Attached References", self.source)
        self.assertIn('group("Designs"', self.source)
        self.assertIn('group("What went wrong"', self.source)
        self.assertIn('class="ticket-chip"', self.job_page)  # the ticket is in the header
        self.assertNotIn('act("link_logs"', self.source)  # attaching never starts a fix run
        self.assertNotIn('act("attach_mockup"', self.source)
        attach = self.source[self.source.index("async function attachToJob"):self.source.index("// ---------------------------------------------------------------- start a new project")]
        self.assertIn('api(`jobs/${encodeURIComponent(jobId)}/attach`', attach)
        self.assertNotIn("runAction", attach)

    def test_a_plan_waiting_for_approval_is_shown_under_the_decision(self):
        self.assertIn('id="plan-section"', self.source)
        self.assertIn("${reviewingPlan ? planCardHtml : \"\"}", self.job_page)  # in the top section, by the hero
        self.assertIn('data-scroll-to="#plan-section">the plan below</button>', self.job_page)
        self.assertIn("!ctx.planShown &&", self.source)  # no second Revise plan in More while the card offers it
        self.assertIn("tasks.length && !reviewingPlan ? fold(\"Tasks\"", self.job_page)  # one task list, not two

    def test_each_secondary_action_appears_once(self):
        for action in ("revise", "select_models", "ask_ai", "link_logs", "attach_mockup", "discard_job"):
            self.assertLessEqual(self.source.count(f'act("{action}", j)') + self.job_page.count(f'data-action="{action}"'), 1, action)

    def test_debug_layout_toggles_removed(self):
        self.assertNotIn("orchestrator_home_version", self.source)
        self.assertNotIn("(debug)", self.source)

    def test_job_hero_proportions_and_standardized_button(self):
        self.assertIn('<h2 class="job-hero-title">', self.job_page)
        self.assertNotIn('job-hero-main">\n          <span class="pill', self.job_page)
        self.assertNotIn('btn primary big', self.job_page)
        self.assertIn('btn primary', self.job_page)

    def test_features_page_removed_from_nav_and_redirects_home(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        self.assertNotIn('data-route="features"', (static / "index.html").read_text())
        self.assertIn("pages.features = async", self.source)
        self.assertIn('location.hash = "#/"', self.source)
        self.assertEqual(self.source.count("data-job-feature="), 1)  # rendered once, in the job menu
        self.assertIn('closest("[data-job-feature]")', self.source)
        self.assertIn("drawFeatureLinks", self.source)
        self.assertIn("job.plan?.slice_warnings", self.job_page)
        self.assertIn('matchMedia("(max-width: 760px)")', self.job_page)
        for title in ("Tasks", "Activity & Runs", "Output files"):  # logs live under "What went wrong" now
            self.assertIn(f'fold("{title}"', self.job_page, title)
        self.assertIn("pages.help = async", self.source)
        self.assertIn('href="#/help"', (static / "index.html").read_text())
        self.assertIn('label: "Undo"', self.source)
        self.assertIn("data-scope-accept", self.job_page)
        self.assertIn("data-scope-adopt", self.job_page)
        self.assertIn("Adopt current files as plan scope", self.job_page)
        self.assertIn("scope-notice-clean", self.job_page)
        self.assertIn('data-scroll-to="#scope-section"', self.job_page)
        self.assertIn('data-route="checkup"', (static / "index.html").read_text())
        self.assertIn('query.get("summary")', self.source)
        self.assertIn('data-route="measure"', (static / "index.html").read_text())
        self.assertIn("testCaseRowsHtml(testCases.cases)", self.job_page)
        self.assertNotIn('view=map', self.source)

    def test_discard_is_a_confirmed_server_action(self):
        self.assertIn("discard", ui.ACTIONS)
        self.assertTrue(ui.ACTIONS["discard"].confirm)
        self.assertNotIn('runAction("console")', self.source[self.source.index("discard_job(params)"):][:120])

    def test_discard_job_option_removed_from_more_menu(self):
        start = self.source.index("function jobHeaderActions")
        end = self.source.index('document.addEventListener("click", async (e)', start)
        header_actions_fn = self.source[start:end]
        self.assertNotIn("Discard job and revert changes", header_actions_fn)
        self.assertNotIn('act("discard_job"', header_actions_fn)

    def test_delete_is_last_in_the_job_menu_after_archive_not_a_top_level_button(self):
        start = self.source.index("function jobHeaderActions")
        end = self.source.index('document.addEventListener("click", async (e)', start)
        header_actions_fn = self.source[start:end]
        self.assertIn('["Delete…", act("delete_job", j), "Remove the job, and optionally its changes", "danger"]', header_actions_fn)
        self.assertLess(header_actions_fn.index('["Archive"'), header_actions_fn.index('["Delete…"'))
        self.assertNotIn(">Delete</button>", header_actions_fn)
        self.assertIn('items.push("---", ...ending)', header_actions_fn)  # ending the job: last and apart
        self.assertIn("return moreMenu(items, { compact: true });", header_actions_fn)

    def test_job_menu_is_grouped_one_line_each_with_one_fix_item(self):
        fn = self.source[self.source.index("function jobHeaderActions"):self.source.index('document.addEventListener("click", async (e)', self.source.index("function jobHeaderActions"))]
        for title in ('["This job", [', '["Share", [', '["Open", [', '["Settings", [', 'items.push(["header", title]'):
            self.assertIn(title, fn)
        self.assertNotIn("Still broken?", fn)  # one way to ask for a fix
        self.assertIn('["Run a fix…", act("debug", j)', fn)

    def test_delete_job_dialog_flow_defined(self):
        start = self.source.index("async delete_job(params)")
        end = self.source.index("async splinter_job(params)", start)
        dialog_code = self.source[start:end]
        self.assertIn("Delete Job", dialog_code)
        self.assertIn("Revert changes", dialog_code)
        self.assertIn("Delete and keep changes", dialog_code)
        self.assertIn("Confirm Deletion", dialog_code)
        self.assertIn("api(`jobs/${encodeURIComponent(params.job)}/delete`", dialog_code)

    def test_test_cases_page_is_routed_and_in_nav_and_palette(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        html = (static / "index.html").read_text()
        css = (static / "style.css").read_text()
        self.assertNotIn('href="#/test-cases"', html)  # under Tests now, not its own sidebar entry
        self.assertIn('pages["test-cases"] = async', self.source)
        self.assertIn('parts[0] === "tests" && parts[1] === "cases") return { page: "test-cases"', self.source)
        self.assertIn('"test-cases": "tests/cases"', (static / "routes.js").read_text())  # old links still work
        self.assertIn('function testCaseForm(', self.source)
        self.assertIn('["Test cases", "#/tests/cases"', self.source)
        self.assertIn('data-job-tc-op="add"', self.job_page)
        self.assertIn('plan-test-cases', self.job_page)
        self.assertIn('.tc-actions { display: flex; gap: var(--space-2);', css)
        self.assertIn('href="#/tests/cases"', self.source)  # Linked from pages.tests

    def test_lifecycle_stepper_and_clarified_hero_in_job_page(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        css = (static / "style.css").read_text()
        self.assertIn('<nav class="job-stepper"', self.job_page)
        self.assertIn(".job-stepper {", css)
        self.assertIn(".job-step-dot {", css)
        self.assertIn(".job-hero-next-preview {", css)
        self.assertIn('job-hero-next-preview', self.job_page)
        # The stepper shows the stage and the hero's title and button say what's needed: no badge row repeating them.
        self.assertNotIn("job-hero-badge-row", self.job_page)

    def test_brief_section_positioned_near_top_and_editable(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        css = (static / "style.css").read_text()
        self.assertIn('id="brief-section"', self.job_page)
        self.assertLess(self.job_page.index('id="brief-section"'), self.job_page.index('<h2>Test cases'))
        self.assertIn('data-brief-edit', self.job_page)
        self.assertIn('brief-path-chip', self.job_page)
        self.assertIn('.brief-path-chip {', css)
        self.assertIn('brief-content', self.job_page)
        self.assertIn('.brief-content {', css)
        self.assertIn('>Raw</a>', self.job_page)
        self.assertIn('data-expandable="320"', self.job_page)  # long briefs fold behind Show more, not a scroll box

    def test_add_existing_project_modal_is_tabbed_with_mobile_exit_controls(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        html = (static / "index.html").read_text()
        css = (static / "style.css").read_text()
        app_js = (static / "app.js").read_text()

        # HTML dialog structure
        self.assertIn('<dialog id="dialog">', html)
        self.assertIn('class="dialog-header"', html)
        self.assertIn('id="dialog-close"', html)
        self.assertIn('aria-label="Close dialog"', html)

        # Tabbed structure in app.js
        self.assertIn('$("#dialog-title").textContent = "Add Existing Project"', app_js)
        self.assertIn('class="dialog-tabs"', app_js)
        self.assertIn('data-project-tab="discovered"', app_js)
        self.assertIn('data-project-tab="custom"', app_js)
        self.assertIn('id="panel-discovered"', app_js)
        self.assertIn('id="panel-custom"', app_js)

        # Clear exit mechanisms
        self.assertIn('cancel.hidden = false', app_js)
        self.assertIn('cancel.textContent = "Close"', app_js)
        self.assertIn('cancel.textContent = "Cancel"', app_js)
        self.assertIn('$("#dialog-close")?.addEventListener("click"', app_js)
        self.assertIn('globalDialog?.addEventListener("click"', app_js)

        # Responsive and tabbed CSS styles
        self.assertIn(".dialog-header {", css)
        self.assertIn(".dialog-close-btn {", css)
        self.assertIn(".dialog-tabs {", css)
        self.assertIn(".dialog-tab-btn {", css)
        self.assertIn(".dialog-tab-panel {", css)
        self.assertIn("max-height: calc(100dvh - 24px);", css)
        self.assertIn("flex-direction: row;", css)
        self.assertIn("width: min(var(--dialog-width), calc(100% - 2 * var(--space-4))", css)
        self.assertNotIn("width: calc(100vw - 2 * var(--space-3));", css)


class JobDeleteEndpointTests(ServerTestCase):
    JOB = "20260922-bug-1"

    def test_delete_job_with_keep_changes_archives_job(self):
        job_file = self.root / ".orchestrator" / "jobs" / f"{self.JOB}.json"
        archive_file = self.root / ".orchestrator" / "jobs" / "archive" / f"{self.JOB}.json"
        self.assertTrue(job_file.is_file())

        res, data = self.request("POST", f"/api/jobs/{self.JOB}/delete", body={"revert": False}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("deleted"), self.JOB)
        self.assertFalse(data.get("reverted"))
        self.assertFalse(job_file.exists())
        self.assertTrue(archive_file.is_file())
        saved = json.loads(archive_file.read_text())
        self.assertEqual(saved.get("status"), "discarded")

    def test_delete_job_with_revert_reverts_files_and_archives(self):
        job_file = self.root / ".orchestrator" / "jobs" / f"{self.JOB}.json"
        archive_file = self.root / ".orchestrator" / "jobs" / "archive" / f"{self.JOB}.json"
        untracked = self.root / "tmp_job_file.txt"
        untracked.write_text("hello")
        job = json.loads(job_file.read_text())
        job["ai_untracked_files"] = ["tmp_job_file.txt"]
        job_file.write_text(json.dumps(job))

        res, data = self.request("POST", f"/api/jobs/{self.JOB}/delete", body={"revert": True}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("reverted"))
        self.assertFalse(untracked.exists())
        self.assertFalse(job_file.exists())
        self.assertTrue(archive_file.is_file())

    def test_delete_action_registered(self):
        self.assertIn("delete_job", ui.ACTIONS)
        self.assertIn("keep_changes", ui.ACTIONS["delete_job"].fields)


class StableUrlAndTunnelTests(unittest.TestCase):
    def setUp(self):
        isolate_state(self)  # cloudflared's output is copied to the state folder's logs

    def test_silent_tunnel_discovery_has_a_real_deadline(self):
        import sys
        real_popen = subprocess.Popen
        child = real_popen([sys.executable, "-c", "import time; time.sleep(30)"], stdout=subprocess.PIPE, text=True)
        try:
            with patch("shutil.which", return_value="fixture"), patch("subprocess.Popen", return_value=child):
                before = time.monotonic()
                proc, url = ui.start_tunnel(8765, discovery_timeout=.05)
                self.assertLess(time.monotonic() - before, 1)
                self.assertIs(proc, child)
                self.assertIsNone(url)
        finally:
            child.terminate()
            child.wait(timeout=3)
            child.stdout.close()

    @patch("shutil.which", return_value="/usr/local/bin/tailscale")
    @patch("subprocess.check_output")
    def test_get_tailscale_info_success(self, mock_subp, mock_which):
        mock_subp.return_value = json.dumps({
            "Self": {
                "DNSName": "leemos-macbook-pro.tail50c55a.ts.net.",
                "TailscaleIPs": ["100.94.186.73", "fd7a:115c:a1e0::49"]
            }
        }).encode("utf-8")
        ip, dns = ui.get_tailscale_info()
        self.assertEqual(ip, "100.94.186.73")
        self.assertEqual(dns, "leemos-macbook-pro.tail50c55a.ts.net")

    @patch("shutil.which", return_value=None)
    def test_get_tailscale_info_not_found(self, mock_which):
        ip, dns = ui.get_tailscale_info()
        self.assertIsNone(ip)
        self.assertIsNone(dns)

    @patch("shutil.which", return_value="/usr/local/bin/tailscale")
    @patch("subprocess.check_output", side_effect=subprocess.CalledProcessError(1, "tailscale"))
    def test_get_tailscale_info_error(self, mock_subp, mock_which):
        ip, dns = ui.get_tailscale_info()
        self.assertIsNone(ip)
        self.assertIsNone(dns)

    @patch("shutil.which", return_value="/usr/local/bin/cloudflared")
    @patch("subprocess.Popen")
    def test_start_tunnel_named_token(self, mock_popen, mock_which):
        mock_proc = unittest.mock.MagicMock()
        mock_popen.return_value = mock_proc
        proc, url = ui.start_tunnel(8765, token="token-12345")
        self.assertEqual(proc, mock_proc)
        self.assertIsNone(url)
        mock_popen.assert_called_once_with(
            ["/usr/local/bin/cloudflared", "tunnel", "run", "--token", "token-12345"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    @patch("shutil.which", return_value="/usr/local/bin/cloudflared")
    @patch("subprocess.Popen")
    def test_start_tunnel_ephemeral(self, mock_popen, mock_which):
        mock_proc = unittest.mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout.readline.side_effect = [
            "2026-10-02 INF Starting tunnel...\n",
            "2026-10-02 INF Your quick Tunnel has been created! Visit it at https://abc-def.trycloudflare.com\n",
        ]
        mock_popen.return_value = mock_proc
        proc, url = ui.start_tunnel(8765)
        self.assertEqual(proc, mock_proc)
        self.assertEqual(url, "https://abc-def.trycloudflare.com")

    def test_public_url_host_allowed(self):
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name)
        root = base / "project"
        make_project(root)
        server = ui.UIServer(("127.0.0.1", 0), root, token="test-token")
        from urllib.parse import urlparse
        parsed = urlparse("https://my-domain.example.com:8765")
        server.allowed_hosts.add(parsed.netloc)
        server.allowed_hosts.add(parsed.hostname)
        self.assertIn("my-domain.example.com:8765", server.allowed_hosts)
        self.assertIn("my-domain.example.com", server.allowed_hosts)
        server.server_close()
        tmp.cleanup()


class JobChangesTests(unittest.TestCase):
    """A job's Changes card shows its own work: uncommitted edits count only while its branch is checked out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        run = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.root, check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        (self.root / "app.py").write_text("print(1)\n")
        run("add", "app.py")
        run("commit", "-q", "-m", "start")
        run("branch", "ai/job-1")
        (self.root / "app.py").write_text("print(2)\n")  # someone's own uncommitted edit, on main

    def tearDown(self):
        self.tmp.cleanup()

    def test_uncommitted_edits_on_another_branch_are_not_the_jobs(self):
        changes = ui.job_changes(self.root, {"branch": "ai/job-1", "base_branch": "main"})
        self.assertEqual((changes["files"], changes["local_files"], changes["local_summary"]), ([], [], ""))

    def test_uncommitted_edits_on_the_jobs_branch_are_its_own(self):
        subprocess.run(["git", "checkout", "-q", "ai/job-1"], cwd=self.root, check=True)
        changes = ui.job_changes(self.root, {"branch": "ai/job-1", "base_branch": "main"})
        self.assertEqual(changes["local_files"], ["app.py"])
        self.assertIn("app.py", changes["files"])


class SetupActionEndpointTests(ServerTestCase):
    def test_setup_git_init_endpoint(self):
        new_folder = Path(self.tmp.name) / "uninitialized_project"
        new_folder.mkdir()
        (new_folder / "file.txt").write_text("hello")
        self.server.set_root(new_folder)
        res, data = self.request("POST", "/api/setup/git-init", body={}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertTrue(data.get("ok"))
        self.assertTrue((new_folder / ".git").is_dir())

    def test_setup_github_create_endpoint(self):
        with patch("orchestrator.new_project.publish_to_github") as mock_pub:
            mock_pub.return_value = {"ok": True, "name": "Create the GitHub repository", "detail": "user/repo", "url": "https://github.com/user/repo"}
            res, data = self.request("POST", "/api/setup/github-create", body={"visibility": "private"}, headers=UI_HEADERS)
            self.assertEqual(res.status, 200)
            self.assertTrue(data.get("ok"))
            mock_pub.assert_called_once()

    def test_ptysession_records_stopped(self):
        sess = ui.PtySession("test", "console", "Interactive console", ["echo", "1"], self.root, {})
        self.assertFalse(sess.stopped)
        sess.stop()
        self.assertTrue(sess.stopped)
        self.assertTrue(sess.summary()["stopped"])


if __name__ == "__main__":
    unittest.main()


class RouteAliasTests(unittest.TestCase):
    STATIC = PACKAGE_ROOT / "orchestrator" / "web" / "static"

    def node(self, source: str):
        result = subprocess.run(["node", "-e", f"require(process.argv[1]);\n{source}", str(self.STATIC / "routes.js")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_an_old_address_goes_to_where_the_page_lives_now_keeping_its_query(self):
        out = self.node("""
const R = globalThis.RouteAliases, map = {"test-cases": "tests/cases", "config/chat": "connections?card=chat"};
process.stdout.write(JSON.stringify([
  R.redirect("#/test-cases?q=login&type=security", map), R.redirect("#/test-cases/", map),
  R.redirect("#/config/chat?card=x&y=1", map), R.redirect("#/tests", map), R.redirect("", map), R.redirect("#/nowhere"),
]));""")
        self.assertEqual(out, ["#/tests/cases?q=login&type=security", "#/tests/cases", "#/connections?card=chat&y=1", None, None, None])

    def test_aliases_load_before_the_app_and_route_applies_them_first(self):
        html = (self.STATIC / "index.html").read_text()
        self.assertLess(html.index('src="routes.js"'), html.index('src="app.js"'))
        app = (self.STATIC / "app.js").read_text()
        body = app[app.index("async function route() {"):]
        self.assertLess(body.index("RouteAliases.redirect(location.hash)"), body.index("resolveRoute()"))

    def test_every_settings_link_the_server_sends_opens_a_real_setting(self):
        import re as _re
        sources = (PACKAGE_ROOT / "orchestrator").rglob("*.py")
        routes = {m for p in sources for m in _re.findall(r'"#/config/([a-z0-9-]+)', p.read_text(encoding="utf-8"))}
        self.assertTrue(routes)
        out = subprocess.run(["node", "--require", HTML_JS, "-e",
                              f"require(process.argv[1]); process.stdout.write(JSON.stringify({json.dumps(sorted(routes))}"
                              ".filter((id) => !globalThis.ConfigurationPages.find(id))));",
                              str(self.STATIC / "configuration.js")], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [], "server links to settings that don't exist")


class MenuParityTests(unittest.TestCase):
    """The web Configuration menu and the terminal console's Configuration menu use the same groups and names."""
    STATIC = PACKAGE_ROOT / "orchestrator" / "web" / "static"
    # Settings both menus have: registry id -> the console letter that opens it.
    SHARED = {"base-branch": "G", "firebase": "D", "xcode-cloud": "X", "models": "M", "ai-instructions": "I",
              "fleet": "F", "updates": "U", "email": "E"}

    def registry(self):
        out = subprocess.run(["node", "--require", HTML_JS, "-e", "require(process.argv[1]); const P = globalThis.ConfigurationPages;"
                              "process.stdout.write(JSON.stringify(P.groups('owner')))", str(self.STATIC / "configuration.js")],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def console_menu(self):
        source = (PACKAGE_ROOT / "orchestrator" / "scripts" / "dev_console.py").read_text(encoding="utf-8")
        start = source.index("def handle_configuration_menu(")
        menu = source[start:source.index("get_choice_prompt(", start)]
        return re.sub(r"\\033\[[0-9;]*m", "", menu)

    def test_shared_settings_have_the_same_name_and_group_in_both_menus(self):
        menu = self.console_menu()
        headings = re.findall(r"--- (.+?) ---", menu)
        for group in self.registry():
            for entry in group["entries"]:
                letter = self.SHARED.get(entry["id"])
                if not letter:
                    continue
                self.assertIn(f"[{letter}] {entry['label']}", menu, entry["id"])
                # ...under the same heading: the last heading printed before the option
                before = menu[:menu.index(f"[{letter}] {entry['label']}")]
                self.assertEqual(re.findall(r"--- (.+?) ---", before)[-1], group["label"].replace("&amp;", "&"), entry["id"])
        shared_groups = {g["label"].replace("&amp;", "&") for g in self.registry() if any(e["id"] in self.SHARED for e in g["entries"])}
        self.assertTrue(shared_groups <= set(headings))

    def test_moved_settings_have_one_home_and_old_links_land_there(self):
        aliases = (self.STATIC / "routes.js").read_text()
        for old, new in (("setup-checklist", "readiness"), ("config/audit", "readiness"), ("config/self-tests", "readiness"),
                         ("config/setup-wizard", "readiness"), ("config/chat", "connections?card=chat"), ("config/api-keys", "config/ai"),
                         ("config/documentation", "docs"), ("config/archived-jobs", "?filter=archived"), ("test-cases", "tests/cases")):
            self.assertIn(f'"{old}": "{new}"', aliases, old)
        html = (self.STATIC / "index.html").read_text()
        self.assertIn('href="#/readiness" data-route="readiness" id="nav-readiness" hidden', html)  # shown until setup is done
        self.assertIn('navItem.hidden = !s || s.complete;', app := (self.STATIC / "app.js").read_text())
        app = (self.STATIC / "app.js").read_text()
        connections = app[app.index("pages.connections = async"):]
        self.assertIn('const hook = owner ? (await api("config").catch(() => null))?.webhook : null;', connections)  # owners only
        self.assertIn("data-owner-only", (self.STATIC / "configuration.js").read_text().split("function chatCard")[1][:200])


class SidebarLayoutTests(unittest.TestCase):
    STATIC = PACKAGE_ROOT / "orchestrator" / "web" / "static"

    def test_most_used_pages_lead_and_set_once_settings_sit_under_settings(self):
        html = (self.STATIC / "index.html").read_text()
        main = html[html.index('<nav class="nav" id="nav"'):html.index("</nav>", html.index('<nav class="nav" id="nav"'))]
        self.assertEqual(re.findall(r'data-route="([a-z-]+)"', main), ["readiness", "home", "activity", "product"])
        tabbar = html[html.index('<nav class="tabbar"'):]
        self.assertEqual(re.findall(r'data-route="([a-z-]+)"', tabbar[:tabbar.index("</nav>")]), ["home", "product", "new", "activity", "tests"])
        out = subprocess.run(["node", "--require", HTML_JS, "-e", "require(process.argv[1]);"
                              "process.stdout.write(JSON.stringify(globalThis.ConfigurationPages.groups('owner')[0]))",
                              str(self.STATIC / "configuration.js")], capture_output=True, text=True)
        first = json.loads(out.stdout)
        self.assertEqual((first["label"], [e["route"] for e in first["entries"]]), ("Set up", ["#/readiness", "#/connections", "#/projects"]))


class ConnectionsAndModalLayoutConsistencyTests(unittest.TestCase):
    STATIC = PACKAGE_ROOT / "orchestrator" / "web" / "static"

    def test_container_padding_design_standard_enforces_consistency(self):
        css = (self.STATIC / "style.css").read_text()
        # Canonical container padding tokens declared in :root
        self.assertIn("--container-pad-x: var(--space-5);", css)
        self.assertIn("--container-pad-y: var(--space-4);", css)
        # Mobile responsive override for container padding tokens
        self.assertIn("--container-pad-x: var(--space-4);", css)
        self.assertIn("--container-pad-y: var(--space-3);", css)
        # .card-b and card components use the container padding tokens
        self.assertIn(".card-b { padding: var(--container-pad-y) var(--container-pad-x); }", css)
        self.assertIn("padding: var(--space-3) var(--container-pad-x);", css)
        # Home page job table and hero use container-pad tokens
        self.assertIn(".job-hero {\n  display: flex;\n  align-items: center;\n  justify-content: space-between;\n  gap: var(--space-3) var(--space-5);\n  flex-wrap: wrap;\n  padding: var(--container-pad-y) var(--container-pad-x);", css)
        # Connections cards explicitly enforce container padding consistency
        self.assertIn(".conn-card .card-b,", css)
        self.assertIn('body[data-page="connections"] .card-b,', css)

    def test_modal_on_mobile_is_contained_and_does_not_bleed_off_edges(self):
        css = (self.STATIC / "style.css").read_text()
        # Form inputs selector includes input[type=date] with min-width: 0 and box-sizing: border-box
        self.assertIn("input[type=date]", css)
        self.assertIn("min-width: 0;", css)
        # Mobile dialog does not use narrow 12px margins that bleed off screen
        self.assertNotIn("width: calc(100vw - 2 * var(--space-3));", css)
        # Dialog is bounded by safe viewport margins and safe area insets
        self.assertIn("calc(100vw - 2 * var(--space-5)", css)
        self.assertIn("overflow-x: hidden;", css)
        # Dialog body is constrained with box-sizing and overflow containment
        self.assertIn("#dialog-body {\n  overflow-y: auto;\n  overflow-x: hidden;\n  min-height: 0;\n  min-width: 0;\n  max-width: 100%;", css)
