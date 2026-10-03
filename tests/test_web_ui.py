from __future__ import annotations

import base64
import http.client
import shutil
import io
import json
import os
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
    def sign_in(self, email="tester@example.com"):
        with patch("orchestrator.web.server.verify_firebase_id_token", return_value={"email": email}), \
             patch("orchestrator.web.server.allowed_auth_emails", return_value={email}):
            res, data = self.request("POST", "/api/auth", body={"id_token": "id"}, headers=UI_HEADERS, auth=False)
        self.assertEqual(res.status, 200, data)
        return data["token"]

    def as_user(self, token, method, path, body=None, allowed=("tester@example.com",)):
        headers = {"Authorization": f"Bearer {token}", **(UI_HEADERS if body is not None else {})}
        with patch("orchestrator.web.server.allowed_auth_emails", return_value=set(allowed)):
            self.server.forget_allowed_emails()
            return self.request(method, path, body=body, headers=headers, auth=False)

    def test_sign_in_gets_its_own_token_never_the_servers(self):
        token = self.sign_in()
        self.assertNotEqual(token, "test-token")
        res, data = self.as_user(token, "GET", "/api/state")
        self.assertEqual(res.status, 200)
        self.assertEqual(data["token"], token)  # state echoes the caller's credential, not the access token
        self.assertEqual(data["you"], {"kind": "sign_in", "email": "tester@example.com"})
        self.assertNotIn("test-token", json.dumps(data))

    def test_owner_state_reports_the_owner(self):
        _, data = self.request("GET", "/api/state")
        self.assertEqual(data["you"]["kind"], "owner")

    def test_state_reports_the_runner_version(self):
        _, data = self.request("GET", "/api/state")
        self.assertEqual(data["runner"]["api_version"], ui.account.API_VERSION)
        self.assertTrue(data["runner"]["version"])

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
            ["node", "-e", script, str(picker)],
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
            ["node", "-e", script, str(picker)],
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
            ["node", "-e", script, str(picker)],
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
            ["node", "-e", script, str(picker)],
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
        start = app_js.index('matches("#project-select, .project-select-inline")')
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


class TunnelKeeperTests(unittest.TestCase):
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
    def run_account_script(self, source: str, preload: str = ""):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        result = subprocess.run(
            ["node", "-e", f"{preload}\nrequire(process.argv[1]);\nrequire(process.argv[2]);\n{source}",
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
  empty: A.renderMachines([], {email: "a@x.com", message: "Run `orchestrator connect`"}),
  pair: A.renderPair({name: "<Mac>", os: "macOS", version: "1"}, "ABCD2345", {email: "a@x.com"}),
}));""")
        self.assertEqual(result["list"].count('data-account-action="open"'), 1)  # only the reachable one opens
        self.assertEqual(result["list"].count('data-account-action="remove"'), 3)
        self.assertIn("&lt;Studio&gt;", result["list"])
        self.assertNotIn("<Studio>", result["list"])
        self.assertIn("not reachable from the web", result["list"])
        self.assertIn("<code>orchestrator ui --tunnel</code>", result["list"])
        self.assertIn("Add your computer", result["empty"])
        self.assertIn("pipx install", result["empty"])
        self.assertIn('data-account-form="code"', result["empty"])
        self.assertIn("<code>orchestrator connect</code>", result["empty"])  # the message's `code` is shown as code
        self.assertIn("ABCD-2345", result["pair"])
        self.assertIn("&lt;Mac&gt;", result["pair"])
        self.assertIn('data-account-action="claim"', result["pair"])

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
            ["node", "-e", f"require(process.argv[1]);\n{source}", str(helper)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_registry_exposes_phase_one_native_routes(self):
        entries = self.run_configuration_script("""
const entries = globalThis.ConfigurationPages.groups().flatMap((group) => group.entries);
process.stdout.write(JSON.stringify(Object.fromEntries(entries.map((entry) => [entry.id, entry]))));
""")
        self.assertEqual({key: entries[key]["route"] for key in (
            "api-keys", "base-branch", "projects", "archived-jobs", "email", "documentation"
        )}, {
            "api-keys": "#/config/api-keys",
            "base-branch": "#/config/base-branch",
            "projects": "#/projects",
            "archived-jobs": "#/config/archived-jobs",
            "email": "#/config/email",
            "documentation": "#/config/documentation",
        })
        self.assertTrue(all(entries[key]["enabled"] for key in (
            "api-keys", "base-branch", "projects", "archived-jobs", "email", "documentation"
        )))

    def test_registry_exposes_all_configuration_entries_enabled(self):
        result = self.run_configuration_script("""
const entries = globalThis.ConfigurationPages.groups().flatMap((group) => group.entries);
const ids = ['models', 'ai-instructions', 'fleet', 'firebase', 'xcode-cloud',
  'setup-wizard', 'audit', 'self-tests', 'updates'];
const active = Object.fromEntries(ids.map((id) => [id, entries.find((entry) => entry.id === id)]));
process.stdout.write(JSON.stringify({active, menu: globalThis.ConfigurationPages.renderMenu()}));
""")
        for entry_id, entry in result["active"].items():
            self.assertTrue(entry["enabled"])
            self.assertEqual(entry["route"], f"#/config/{entry_id}")
            self.assertIsNone(entry["status"])
        self.assertEqual(result["menu"].count(" disabled"), 0)
        self.assertNotIn('data-config-route="null"', result["menu"])
        self.assertNotIn("Coming next", result["menu"])

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

    def test_resolver_accepts_only_enabled_native_configuration_pages(self):
        result = self.run_configuration_script("""
const pages = globalThis.ConfigurationPages;
process.stdout.write(JSON.stringify({
  enabled: pages.resolve('api-keys'),
  models: pages.resolve('models'),
  external: pages.resolve('projects'),
  unknown: pages.resolve('unknown')
}));
""")
        self.assertEqual(result["enabled"]["route"], "#/config/api-keys")
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
        for group in ("Models &amp; instructions", "Projects &amp; machines",
                      "Delivery &amp; notifications", "Help &amp; health"):
            self.assertIn(group, result["html"])
        for route in ("#/config/api-keys", "#/config/base-branch", "#/projects",
                      "#/config/archived-jobs", "#/config/email", "#/config/documentation",
                      "#/config/models", "#/config/fleet", "#/config/firebase"):
            self.assertIn(route, result["html"])
        self.assertEqual(result["html"].count(" disabled"), 0)
        self.assertNotIn("Coming next", result["html"])
        self.assertNotIn('data-action="config_menu"', result["html"])

    def test_api_keys_page_shows_status_without_rendering_secrets(self):
        result = self.run_configuration_script("""
const page = globalThis.ConfigurationPages.render('api-keys', {
  keys: [
    {id: 'anthropic_api_key', label: 'Anthropic', saved: true, env: true, value: 'sk-do-not-render'},
    {id: 'openai_api_key', label: 'OpenAI', saved: false, env: true},
    {id: 'ollama_api_key', label: 'Ollama', saved: false, env: false}
  ],
  ollama_host: 'http://ollama.example'
});
process.stdout.write(JSON.stringify(page));
""")
        self.assertEqual(result["title"], "API Keys")
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
        self.assertEqual(result["title"], "Base Branch")
        self.assertIn('<option value="feature/one" selected>', result["html"])
        self.assertIn('&quot;&gt;&lt;script&gt;bad()&lt;/script&gt;', result["html"])
        self.assertNotIn('<script>bad()</script>', result["html"])
        self.assertNotIn("develop", result["html"])

    def test_archived_jobs_page_marks_corrupt_entries_and_empty_state(self):
        result = self.run_configuration_script("""
const populated = globalThis.ConfigurationPages.render('archived-jobs', {
  archived: [
    {id: 'job-1', job_id: 'JOB-1', title: 'Ready <now>', status: 'completed', corrupt: false},
    {id: 'broken', job_id: 'BROKEN', title: 'Unreadable', status: 'unknown', corrupt: true}
  ]
});
const empty = globalThis.ConfigurationPages.render('archived-jobs', {archived: []});
process.stdout.write(JSON.stringify({populated, empty}));
""")
        html = result["populated"]["html"]
        self.assertEqual(result["populated"]["title"], "Archived Jobs")
        for text in ("JOB-1", "Ready &lt;now&gt;", "completed", "BROKEN", "Corrupt"):
            self.assertIn(text, html)
        self.assertIn('data-config-action="archive-restore"', html)
        self.assertIn('data-id="job-1"', html)
        self.assertRegex(html, r'BROKEN[\s\S]*?<button[^>]*disabled')
        self.assertIn("No archived jobs", result["empty"]["html"])

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
        self.assertEqual(result["gmail"]["title"], "Email Notifications")
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

    def test_documentation_page_groups_allowlisted_opaque_documents(self):
        result = self.run_configuration_script("""
const grouped = globalThis.ConfigurationPages.render('documentation', {docs: [
  {id: '0', name: 'Getting <Started>.md', section: 'Orchestrator docs', path: '/must/not/render'},
  {id: '7', name: 'Project Guide.md', section: 'Project docs'}
]});
const oneGroup = globalThis.ConfigurationPages.render('documentation', {docs: [
  {id: '2', name: 'Only.md', section: 'Orchestrator docs'}
]});
const pages = [
  globalThis.ConfigurationPages.render(undefined, {}),
  globalThis.ConfigurationPages.render('api-keys', {keys: []}),
  globalThis.ConfigurationPages.render('base-branch', {branches: []}),
  globalThis.ConfigurationPages.render('archived-jobs', {archived: []}),
  globalThis.ConfigurationPages.render('email', {email: {recipients: []}}),
  grouped
];
process.stdout.write(JSON.stringify({grouped, oneGroup, allHtml: pages.map((page) => page.html).join('')}));
""")
        grouped = result["grouped"]["html"]
        self.assertEqual(result["grouped"]["title"], "Documentation")
        self.assertIn("Orchestrator docs", grouped)
        self.assertIn("Project docs", grouped)
        self.assertIn("Getting &lt;Started&gt;.md", grouped)
        self.assertIn('data-id="0"', grouped)
        self.assertIn('data-id="7"', grouped)
        self.assertNotIn("/must/not/render", grouped)
        self.assertNotIn("Project docs", result["oneGroup"]["html"])
        for terminal_handoff in ('data-action="config_menu"', "data-scroll-to", "Open Full CLI Menu"):
            self.assertNotIn(terminal_handoff, result["allHtml"])


class ReadApiTests(ServerTestCase):
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

    def test_created_job_is_put_under_its_feature(self):
        rt = self.root / ".orchestrator"
        ui.feature_store.create(rt, "Lobby")
        path = rt / "jobs" / "20260922-bug-1.json"
        ui.UIHandler._record_feature(path, self.root, "lobby")
        self.assertEqual(json.loads(path.read_text())["feature"], "lobby")
        self.assertEqual(ui.feature_store.load(rt)[0]["status"], "in-progress")

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
        for status, (group, tone, action) in cases.items():
            st = self.state(status=status)
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
             patch("orchestrator.web.server.allowed_auth_emails", return_value={"tester@example.com"}):
            res, data = self.request("POST", "/api/auth", body={"id_token": "valid-id-token"}, headers=UI_HEADERS, auth=False)
            self.assertEqual(res.status, 200)
            self.assertTrue(data["ok"])
            self.assertEqual(data["email"], "tester@example.com")
            cookie = res.getheader("Set-Cookie")
            self.assertIn(f"orchestrator_ui={data['token']}", cookie)
            self.assertNotIn("test-token", cookie)

    def test_auth_endpoint_unauthorized_email(self):
        with patch("orchestrator.web.server.verify_firebase_id_token", return_value={"email": "intruder@example.com"}), \
             patch("orchestrator.web.server.allowed_auth_emails", return_value={"owner@example.com"}):
            res, data = self.request("POST", "/api/auth", body={"id_token": "some-id-token"}, headers=UI_HEADERS, auth=False)
            self.assertEqual(res.status, 403)
            self.assertIn("not authorized", data["error"])

    def test_auth_refuses_accounts_without_a_verified_email(self):
        # e.g. a GitHub account whose email is missing, or one the provider hasn't verified
        for info, expected in (({"email": ""}, "didn't share an email"),
                               ({"email": "tester@example.com", "emailVerified": False}, "isn't verified")):
            with patch("orchestrator.web.server.verify_firebase_id_token", return_value=info), \
                 patch("orchestrator.web.server.allowed_auth_emails", return_value={"tester@example.com"}):
                res, data = self.request("POST", "/api/auth", body={"id_token": "t"}, headers=UI_HEADERS, auth=False)
                self.assertEqual(res.status, 403, info)
                self.assertIn(expected, data["error"])

    def test_auth_is_closed_when_no_emails_are_allowed(self):
        with patch("orchestrator.web.server.verify_firebase_id_token", return_value={"email": "anyone@example.com"}), \
             patch("orchestrator.web.server.allowed_auth_emails", return_value=set()):
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
        self.assertEqual(summary["by_type"], {"unit": 2, "integration": 1, "ui": 1, "manual": 1})
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

    def test_phone_layout_keeps_every_page_reachable_through_more(self):
        for href in ("#/tests", "#/delivery", "#/measure", "#/checkup", "#/git", "#/connections", "#/config", "#/devlogs"):
            self.assertIn(f'"{href}"', self.js[self.js.index("const MORE_LINKS"):][:400], href)
        self.assertIn('id="nav-more"', self.html)

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
        for part in ("position: fixed", "top: calc(var(--space-4) + env(safe-area-inset-top))", "z-index: 1000", "pointer-events: none", "prefers-reduced-motion"):
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
        for route in ("#/features", "#/tests", "#/delivery", "#/measure", "#/checkup", "#/projects", "#/activity", "#/config", "#/config/documentation"):
            self.assertIn(f'"{route}"' if route != "#/config/documentation" else route, help_page, route)
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
        for label in ("Build &amp; ship", "Learn &amp; improve", "Set up"):
            self.assertIn(f'<span class="label">{label}</span>', self.html)

    def test_configuration_uses_plain_names_and_keeps_the_old_term_in_the_description(self):
        config = (self.STATIC / "configuration.js").read_text()
        for label, old in (("Machines", "fleet"), ("Tool check", "prerequisite audit"), ("Orchestrator health check", "self-tests"), ("Tester builds (Firebase)", "firebase app distribution")):
            line = next(l for l in config.splitlines() if f'label: "{label}"' in l)
            self.assertIn(old, line.lower(), label)
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
        self.assertIn('label: "Slack & chat alerts"', config)  # the webhook, in the Configuration menu

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
        self.assertIn('["Build", act("build")', self.js)  # Tests
        self.assertIn('Will this computer build it?', self.js)  # Check-up: probes
        self.assertIn('Setup tools', self.js)  # Check-up: the setup checks and wizard
        for action in ("check", "check_config", "worker_check", "wizard"):
            self.assertIn(f'["{action}", ', self.js)
        self.assertIn('act("distribute")', self.js)  # Delivery
        self.assertIn('act("logs_pull")', self.js)  # Device logs
        self.assertIn("Open full console", self.html + self.js)

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
                     'api("product/draft"', "Update this automatically when jobs finish", "Import PRD", "Nothing is saved until you accept"):
            self.assertIn(part, page, part)
        self.assertNotIn("Help me with this", self.js)  # removed
        self.assertNotIn("product/refine", self.js)
        self.assertNotIn("Import a PRD", self.js)  # the button just says Import PRD
        self.assertNotIn("product/scaffold", self.js)  # no six documents to create
        self.assertNotIn("last-review", self.js)  # and no separate review job
        self.assertNotIn(">Optional<", page)  # every section is optional, so none is singled out
        self.assertIn("Everything here is optional", page)
        # Look and feel takes uploads, a pasted link, and an item picked from a connected app (Figma).
        self.assertIn("linkPickerHtml({ prefer: [\"figma\"]", page)

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

    def test_import_takes_a_dropped_file_and_starts_reading_at_once(self):
        page = self.js[self.js.index("const importPanel"):self.js.index("An existing project: read what is there")]
        for part in ('id="prd-drop"', '"dragover"', '"drop"', "e.dataTransfer?.files?.[0]", "readPrd({ file: f })", "e.preventDefault()", 'zone.addEventListener("keydown"'):
            self.assertIn(part, page, part)
        read = self.js[self.js.index("const readPrd"):self.js.index("const importPanel")]
        self.assertIn("NJ_UPLOAD_LIMIT", read)  # a dropped file is checked like a chosen one
        self.assertIn("md|markdown|txt|docx|pdf", read)
        self.assertIn(".prd-drop.over", self.css)
        self.assertIn("pointer: coarse", self.css)  # phones have nothing to drag, so they are not told to

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
        result = subprocess.run(["node", "-e", script, str(module), json.dumps(text)], capture_output=True, text=True)
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
        result = subprocess.run(["node", "-e", script, str(module), json.dumps([entries or self.ENTRIES, query])], capture_output=True, text=True)
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
        result = subprocess.run(["node", "-e", script, str(module), json.dumps(message)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_known_failures_say_what_to_do_next(self):
        self.assertEqual(self.explain("Failed to fetch"), "Can't reach Orchestrator. Check that it's running and that you're online.")  # the browser's own wording is replaced, not repeated
        self.assertEqual(self.explain("Couldn't save: Load failed"), "Can't reach Orchestrator. Check that it's running and that you're online.")
        self.assertIn("Archived Jobs", self.explain("Job not found"))
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
        result = subprocess.run(["node", "-e", script, str(module), json.dumps([prev, nxt])], capture_output=True, text=True)
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
        result = subprocess.run(["node", "-e", script, str(module), json.dumps([prev, nxt])], capture_output=True, text=True)
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

    def test_each_secondary_action_appears_once(self):
        for action in ("revise", "select_models", "ask_ai", "link_logs", "attach_mockup", "discard_job"):
            self.assertLessEqual(self.source.count(f'act("{action}", j)') + self.job_page.count(f'data-action="{action}"'), 1, action)

    def test_debug_layout_toggles_removed(self):
        self.assertNotIn("orchestrator_home_version", self.source)
        self.assertNotIn("(debug)", self.source)

    def test_features_page_is_routed_and_in_the_nav(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        self.assertIn('data-route="features"', (static / "index.html").read_text())
        self.assertIn("pages.features = async", self.source)
        self.assertEqual(self.source.count("data-job-feature="), 1)  # rendered once, in the job menu
        self.assertIn('closest("[data-job-feature]")', self.source)
        self.assertIn("drawFeatureLinks", self.source)
        self.assertIn("job.plan?.slice_warnings", self.job_page)
        self.assertIn('matchMedia("(max-width: 760px)")', self.job_page)
        for title in ("Tasks Checklist", "Activity & Runs", "Logs", "Output files"):
            self.assertIn(f'fold("{title}"', self.job_page, title)
        self.assertIn("pages.help = async", self.source)
        self.assertIn('href="#/help"', (static / "index.html").read_text())
        self.assertIn('label: "Undo"', self.source)
        self.assertIn("data-scope-accept", self.job_page)
        self.assertIn('data-scroll-to="#scope-section"', self.job_page)
        self.assertIn('data-route="checkup"', (static / "index.html").read_text())
        self.assertIn('query.get("summary")', self.source)
        self.assertIn('data-route="measure"', (static / "index.html").read_text())
        self.assertIn("testCaseRowsHtml(testCases.cases)", self.job_page)
        self.assertIn('view=map', self.source)

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

    def test_delete_button_added_to_job_detail_top_right(self):
        start = self.source.index("function jobHeaderActions")
        end = self.source.index('document.addEventListener("click", async (e)', start)
        header_actions_fn = self.source[start:end]
        self.assertIn("btn danger", header_actions_fn)
        self.assertIn('act("delete_job", j)', header_actions_fn)
        self.assertIn(">Delete</button>", header_actions_fn)

    def test_delete_job_dialog_flow_defined(self):
        start = self.source.index("async delete_job(params)")
        end = self.source.index("async splinter_job(params)", start)
        dialog_code = self.source[start:end]
        self.assertIn("Delete Job", dialog_code)
        self.assertIn("Revert changes", dialog_code)
        self.assertIn("Delete and keep changes", dialog_code)
        self.assertIn("Confirm Deletion", dialog_code)
        self.assertIn("api(`jobs/${encodeURIComponent(params.job)}/delete`", dialog_code)


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


if __name__ == "__main__":
    unittest.main()
