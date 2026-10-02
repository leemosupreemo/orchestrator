from __future__ import annotations

import base64
import http.client
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
        res, _ = self.request("OPTIONS", "/api/state", auth=False, headers={"Origin": "https://example.com"})
        self.assertEqual(res.status, 204)
        self.assertEqual(res.getheader("Access-Control-Allow-Origin"), "https://example.com")
        self.assertIn("Authorization", res.getheader("Access-Control-Allow-Headers", ""))

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

    def test_new_job_writes_spec_file_and_passes_flags(self):
        argv = ui.build_new_job({"type": "feature", "summary": "Add rematch", "spec": "Details",
                                 "branch_mode": "new", "no_dispatch": True}, self.root)
        self.assertEqual(argv[4:9], ["script", "new_job.py", "feature", "--summary", "Add rematch"])
        spec = Path(argv[argv.index("--spec-file") + 1])
        self.assertEqual(spec.read_text(), "Details\n")
        self.assertIn(self.root / ".orchestrator" / "ui" / "specs", spec.parents)
        self.assertIn("--no-dispatch", argv)
        self.assertEqual(argv[argv.index("--branch-mode") + 1], "new")

    def test_new_job_recommend_adds_decision_latitude_to_the_spec(self):
        argv = ui.build_new_job({"type": "feature", "summary": "Add rematch", "recommend": True}, self.root)
        text = Path(argv[argv.index("--spec-file") + 1]).read_text()
        self.assertTrue(text.startswith("Add rematch"))
        self.assertIn("assumptions", text)
        plain = ui.build_new_job({"type": "feature", "summary": "Add rematch"}, self.root)
        self.assertNotIn("--spec-file", plain)

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
                  "name": "Suite", "branch": "main", "menu": "keys"}
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
            self.assertIn("orchestrator_ui=test-token", cookie)

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
        self.assertEqual(data["next"], "brief")
        self.assertEqual((items["brief"]["status"], items["instructions"]["status"], items["repo"]["status"], items["delivery"]["status"]),
                         ("todo", "todo", "todo", "todo"))
        self.assertEqual(data["stage"], "Building")  # the fixture has one job

    def test_brief_platforms_features_and_tests_are_read_from_the_project(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "product-brief.md").write_text("# X\n\n## Platforms\n\n- iOS app\n- Backend / API\n")
        (self.root / "AGENTS.md").write_text("# rules")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
        ui.feature_store.create(self.root / ".orchestrator", "Lobby")
        _, data = self.get_health()
        items = self.items(data)
        self.assertEqual((items["brief"]["status"], items["instructions"]["status"], items["tests"]["status"]), ("ok", "ok", "ok"))
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

    def test_undecided_platforms_from_a_new_project_brief_are_flagged(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "product-brief.md").write_text(ui.new_project.render_brief(
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

    def test_connection_loss_is_shown_not_silent(self):
        self.assertIn('id="conn-banner"', self.html)
        self.assertIn("pollFailures >= 2", self.js)
        self.assertIn('$("#conn-banner").hidden = true', self.js)

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

    def test_toast_colours_use_ink_tokens(self):
        block = self.css[self.css.index(".toast.bad {"):][:80]
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

    def test_toasts_explain_errors_and_the_inbox_shows_alert_status(self):
        self.assertIn("Errors.explain(message)", self.js)
        self.assertIn('<script src="errors.js">', self.html)
        self.assertIn("Alerts: browser", self.js)

    def test_there_is_no_separate_inbox_page_home_leads_with_what_is_waiting(self):
        self.assertNotIn('data-route="inbox"', self.html)
        self.assertNotIn("pages.inbox", self.js)
        self.assertNotIn('"#/inbox"', self.js)
        self.assertIn('id="inbox-badge"', self.html.split('data-route="home"')[1].split("</a>")[0])  # the count lives on Home
        home = self.js[self.js.index("pages.home = async"):self.js.index("const ORIGINAL_JOB_FILTERS") if "const ORIGINAL_JOB_FILTERS" in self.js else self.js.index("const JOB_FILTERS")]
        self.assertIn('api("inbox")', home)
        self.assertIn("waitingSectionHtml(waiting)", home)
        self.assertLess(home.index("waitingSectionHtml(waiting)"), home.index('class="job-table"'))
        self.assertIn('rest: ["Everything else"', home)  # jobs already shown above aren't listed twice by default

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
        self.assertIn("Check that it's still running", self.explain("Failed to fetch"))
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


if __name__ == "__main__":
    unittest.main()
