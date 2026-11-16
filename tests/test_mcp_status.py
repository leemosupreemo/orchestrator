from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator import mcp_status

APP = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js"

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class McpStatusTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        which = patch.object(mcp_status.shutil, "which", side_effect=lambda b: f"/bin/{b}" if b in ("claude", "codex", "gemini", "opencode") else None)
        which.start()
        self.addCleanup(which.stop)

    def write(self, rel, data):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))

    def grid(self):
        s = mcp_status.status(home=self.home)
        return [t["id"] for t in s["tools"]], {r["id"]: r["installed"] for r in s["servers"]}

    def test_only_tools_on_this_computer_get_a_column(self):
        tools, _ = self.grid()
        self.assertEqual(tools, ["claude", "codex", "gemini", "opencode"])  # agy isn't installed here

    def test_servers_are_found_by_name_or_launch_command_whatever_they_are_called(self):
        self.write(".claude.json", {"mcpServers": {"ssh-manager": {"command": "npx", "args": ["-y", "mcp-ssh-manager"]}}})
        self.write(".codex/config.toml", '[mcp_servers.context7-mcp]\ncommand = "npx"\nargs = ["-y", "@upstash/context7-mcp"]\n'
                                        '[mcp_servers.builder]\ncommand = "npx"\nargs = ["-y", "xcodebuildmcp"]\n')
        self.write(".gemini/settings.json", {"mcpServers": {"XcodeBuildMCP": {"command": "npx", "args": ["-y", "xcodebuildmcp"]}}})
        _, have = self.grid()
        self.assertTrue(have["mcp-ssh-manager"]["claude"])
        self.assertTrue(have["context7"]["codex"])
        self.assertTrue(have["xcodebuild"]["codex"])  # named "builder", found by its launch command
        self.assertTrue(have["xcodebuild"]["gemini"])
        self.assertFalse(have["xcodediagnostics"]["codex"])  # same package, different server: needs --diagnostics
        self.assertFalse(have["github"]["claude"])

    def test_install_writes_gemini_and_opencode_settings_and_keeps_the_rest(self):
        self.write(".gemini/settings.json", {"theme": "dark", "mcpServers": {"other": {"command": "x"}}})
        mcp_status.install("swiftlens", "gemini", home=self.home)
        mcp_status.install("swiftlens", "opencode", home=self.home)
        gemini = json.loads((self.home / ".gemini/settings.json").read_text())
        self.assertEqual(gemini["theme"], "dark")
        self.assertEqual(gemini["mcpServers"]["swiftlens"], {"command": "npx", "args": ["-y", "swiftlens-mcp"]})
        self.assertIn("other", gemini["mcpServers"])
        opencode = json.loads((self.home / ".config/opencode/opencode.json").read_text())
        self.assertEqual(opencode["mcp"]["swiftlens"], {"type": "local", "command": ["npx", "-y", "swiftlens-mcp"], "enabled": True})
        _, have = self.grid()
        self.assertTrue(have["swiftlens"]["gemini"] and have["swiftlens"]["opencode"])

    def test_install_uses_the_tools_own_command_for_claude_and_codex(self):
        calls = []
        with patch.object(mcp_status.subprocess, "run", side_effect=lambda argv, **k: calls.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr="")):
            mcp_status.install("github", "claude", home=self.home)
            mcp_status.install("github", "codex", home=self.home)
        self.assertEqual(calls[0], ["claude", "mcp", "add", "--scope", "user", "github", "--", "npx", "-y", "@modelcontextprotocol/server-github"])
        self.assertEqual(calls[1], ["codex", "mcp", "add", "github", "--", "npx", "-y", "@modelcontextprotocol/server-github"])
        with patch.object(mcp_status.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="boom\nalready exists")):
            with self.assertRaisesRegex(ValueError, "already exists"):
                mcp_status.install("github", "claude", home=self.home)

    def test_rea_installs_as_its_mcp_server_and_unknown_names_are_refused(self):
        mcp_status.install("rea", "gemini", home=self.home)
        self.assertEqual(json.loads((self.home / ".gemini/settings.json").read_text())["mcpServers"]["rea"],
                         {"command": "npx", "args": ["-y", "rea-agents", "--mcp"]})  # `rea --mcp` serves REA over MCP
        self.write(".claude.json", {"mcpServers": {"rea": {"command": "rea", "args": ["mcp"]}}})  # what REA's own setup may write
        _, have = self.grid()
        self.assertTrue(have["rea"]["gemini"] and have["rea"]["claude"])
        with self.assertRaises(ValueError):
            mcp_status.install("nope", "claude", home=self.home)


class ConnectionsPageTests(unittest.TestCase):
    def setUp(self):
        self.app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()

    def test_mcp_grid_shows_ticks_and_install_buttons(self):
        self.assertIn('data-mcp-install="${esc(srv.id)}" data-mcp-tool="${esc(t.id)}"', self.app)
        self.assertIn('api("mcp/install", { method: "POST", body: { server: install.dataset.mcpInstall, tool: install.dataset.mcpTool } })', self.app)
        server = (PACKAGE_ROOT / "orchestrator" / "web" / "server.py").read_text()
        self.assertIn('parts == ["mcp", "status"]', server)
        self.assertIn('self._require_owner("add MCP servers to AI tools")', server)

    def test_alerts_and_git_cards_show_a_status(self):
        self.assertIn('<strong class="conn-name">${providerIcon("slack")}Slack, email, this device and your Mac</strong>${alertsPill}', self.app)
        self.assertIn("<strong class=\"conn-name\">Repository &amp; branches</strong>${gitPill}", self.app)

    def test_connect_jira_opens_the_jira_form(self):
        self.assertIn('href="#/connections?connect=jira">Connect Jira</a>', self.app)

    def test_reverse_engineering_checks_rea_is_installed_and_offers_to_install_it(self):
        self.assertIn('<div id="prd-rea-status" aria-live="polite">', self.app)
        self.assertIn('api("mcp/install", { method: "POST", body: { server: "rea", tool: b.dataset.reaInstall } })', self.app)
        self.assertIn('useIn: { href: "#/product?reverse=1", label: "Use on Product" }', self.app)  # the grid row links to it

    def test_trello_explains_the_api_key_instead_of_a_dead_button(self):
        self.assertNotIn("quickTrello.disabled", self.app)
        self.assertIn("Trello signs you in through an app, so it needs an API key first", self.app)


if __name__ == "__main__":
    unittest.main()


class UpdateRequirementsWithAiTests(unittest.TestCase):
    """Once some requirements are written, "Draft with AI" becomes "Update with AI" and asks how to update them."""

    CURRENT = "# Product\n\n## Pitch\n\nA notes app for nurses.\n\n## Who it's for\n\n\n## Core features\n\n- Quick notes\n"

    def test_the_prompt_follows_the_chosen_update(self):
        from orchestrator import prd
        missing = prd.draft_prompt("digest", self.CURRENT, "missing", "")
        self.assertIn("Fill in ONLY the sections that are empty or TBD", missing)
        refresh = prd.draft_prompt("digest", self.CURRENT, "refresh", "we dropped Android")
        self.assertIn("Bring it up to date with the project", refresh)
        self.assertIn("we dropped Android", refresh)
        self.assertIn("has no product requirements yet", prd.draft_prompt("digest", "", "", ""))

    def test_filling_what_is_missing_keeps_filled_sections_exactly(self):
        from orchestrator import prd
        reply = json.dumps({"summary": "- ok", "markdown": "# Product\n\n## Pitch\n\nREWRITTEN\n\n## Who it's for\n\nNurses on shift.\n\n## Core features\n\n- Quick notes\n- Sync\n"})
        kept = prd.split(prd.parse_draft(reply, self.CURRENT, keep_filled=True)["markdown"])
        self.assertEqual(kept["pitch"].strip(), "A notes app for nurses.")  # filled: untouched
        self.assertEqual(kept["who"].strip(), "Nurses on shift.")  # was empty: filled in
        refreshed = prd.split(prd.parse_draft(reply, self.CURRENT)["markdown"])
        self.assertEqual(refreshed["pitch"].strip(), "REWRITTEN")

    def test_the_button_and_dialog(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn('${prdFilled() ? "Update with AI" : "Draft with AI"}', app)
        self.assertIn('<input type="radio" name="mode" value="missing"', app)
        self.assertIn('...(mode ? { mode, notes } : {})', app)
        server = (PACKAGE_ROOT / "orchestrator" / "web" / "server.py").read_text()
        self.assertIn('keep_filled=mode == "missing"', server)


class YoloAllOnTheTitleRowTests(unittest.TestCase):
    def test_the_switch_sits_on_the_jobs_title_row(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        header = app[app.index('<div class="card-h card-h-wrap home-jobs-h">'):][:700]
        self.assertLess(header.index("<h2>Jobs"), header.index('class="yolo-switch home-yolo"'))
        self.assertLess(header.index('class="yolo-switch home-yolo"'), header.index('home-jobs-filters'))
        css = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()
        self.assertIn(".home-jobs-h > .home-yolo { order: 1; margin-left: auto;", css)  # phones: beside the title, right-aligned


class MessagesAndStoppingTests(unittest.TestCase):
    def test_messages_slide_in_and_out(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        js = (static / "messages.js").read_text()
        self.assertIn('if (fresh) el.classList.add("msg-new"); // after fill, which resets the classes', js)
        self.assertIn('el.classList.add("msg-out");', js)
        css = (static / "style.css").read_text()
        self.assertIn("@keyframes msg-out", css)
        self.assertIn("@media (prefers-reduced-motion: reduce) { .msg-new, .msg-out { animation: none; } }", css)

    def test_stop_only_says_stopped_once_it_has(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn('if (stopped) toast(hasJob ? "Job stopped" : "Run stopped");', app)
        self.assertIn("It's still stopping. The page updates when it has.", app)

    def test_live_run_cards_say_details_not_open_log(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertNotIn(">Open log<", app)
        self.assertIn('href="#/runs/${encodeURIComponent(r.id)}">Details</a>', app)


class DialogCloseSizeTests(unittest.TestCase):
    def test_the_dialog_close_button_is_15_percent_larger(self):
        css = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()
        rule = css[css.index(".dialog-close-btn {"):][:700]
        self.assertIn("scale: 1.38;", rule)

    def test_close_buttons_enlarged_by_20_percent(self):
        css = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()
        for selector in (".nav-close", ".email-panel-close", ".msg-close", ".feedback-close-btn", ".preview-close"):
            idx = css.rindex(f"{selector} {{")
            block = css[idx:css.index("}", idx) + 1]
            self.assertIn("scale: 1.2;", block, f"{selector} should have scale: 1.2;")


class YoloAllStaysOnTests(unittest.TestCase):
    """YOLO all on a job that's already building: the worker picks the switch up and doesn't save over it."""

    def setUp(self):
        scripts = str(Path(__file__).resolve().parents[1] / "orchestrator" / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import worker_run
        self.worker = worker_run

    def test_the_worker_keeps_a_switch_made_in_the_app_while_it_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs" / "job-1.json"
            path.parent.mkdir()
            job = {"id": "job-1", "status": "executing", "yolo": False, "is_yolo": False}
            path.write_text(json.dumps(job))
            path.write_text(json.dumps({**job, "yolo": True, "is_yolo": True}))  # YOLO all, from the app
            job["status"] = "review-needed"
            self.worker.write_json(path, job)  # the worker saves its older copy
            saved = json.loads(path.read_text())
            self.assertTrue(saved["yolo"] and saved["is_yolo"])
            self.assertEqual(saved["status"], "review-needed")
            self.assertTrue(job["is_yolo"])  # and it now builds on without stopping
            self.assertTrue(self.worker.refresh_yolo({"is_yolo": False}, path))

    def test_the_dialog_says_what_happens_now_and_the_switch_shows_on(self):
        app = APP.read_text(encoding="utf-8")
        fn = app[app.index("async function yoloAll("):app.index("pages.home = async")]
        for text in ("Turn on YOLO for every job in progress?", "<p>Right away:</p>", "start${starting.length === 1 ? \"s\" : \"\"} building now",
                     "already building", "input.checked = on;"):
            self.assertIn(text, fn)


class EasierBugListsTests(unittest.TestCase):
    def test_a_file_of_bugs_goes_straight_to_separating_them(self):
        app = APP.read_text(encoding="utf-8")
        self.assertNotIn("Have more than one bug? Paste them all here", app)
        self.assertIn("Add a list of bugs…", app)
        self.assertIn("Or paste several above. Each becomes its own job.", app)
        self.assertIn("if (looksLikeSeveralBugs(box.value)) form().requestSubmit();", app)

    def test_plan_only_with_yolo_says_how_they_combine(self):
        app = APP.read_text(encoding="utf-8")
        self.assertIn("With both on, planning runs without stopping, then the job waits for you.", app)


class DialogCloseAlignmentTests(unittest.TestCase):
    def test_the_close_button_stays_beside_the_titles_first_line(self):
        css = (APP.parent / "style.css").read_text(encoding="utf-8")
        header = css[css.index(".dialog-header {"):css.index("dialog h2 {")]
        self.assertIn("align-items: flex-start;", header)
        title = css[css.index("dialog h2 {"):css.index(".dialog-close-btn {")]
        self.assertIn("min-height: var(--control-md);", title)
        close = css[css.index(".dialog-close-btn {"):css.index(".dialog-close-btn:focus:not(:focus-visible)")]
        self.assertIn("flex-shrink: 0;", close)


class RequirementsSectionColoursTests(unittest.TestCase):
    def test_filled_sections_are_blue_and_ones_to_write_are_yellow(self):
        app = APP.read_text(encoding="utf-8")
        self.assertIn('prd-sec ${s.filled ? "is-filled" : "is-empty"}', app)
        self.assertIn('${s.filled ? "Filled in" : "To fill in"}', app)
        css = (APP.parent / "style.css").read_text(encoding="utf-8")
        self.assertIn(".prd-sec.is-filled > .card-h { background: var(--accent-soft); border-left: 4px solid var(--accent); }", css)
        self.assertIn(".prd-sec.is-empty > .card-h { background: var(--warn-soft); border-left: 4px solid var(--warn); }", css)


class SessionComputerTests(unittest.TestCase):
    def test_the_sidebar_names_this_sessions_primary_computer_and_its_helpers(self):
        from orchestrator.web import server
        with patch.object(server.account, "load_machine", lambda: {"name": "Studio", "machine_id": "m1"}):
            c = server.session_computer([{"name": "local", "execution_mode": "local"}, {"name": "mini", "execution_mode": "ssh"}])
        self.assertEqual({k: c[k] for k in ("name", "id", "workers", "primary")}, {"name": "Studio", "id": "m1", "workers": ["mini"], "primary": True})
        index = (APP.parent / "index.html").read_text(encoding="utf-8")
        self.assertIn('<button type="button" class="session-computer" id="session-computer" hidden></button>', index)
        app = APP.read_text(encoding="utf-8")
        self.assertIn('<span class="pill session-primary">Primary</span>', app)
        self.assertIn("renderSessionComputer($(\"#session-computer\"));", app)


class SmallLayoutFixesTests(unittest.TestCase):
    def test_the_analytics_provider_row_has_the_cards_padding(self):
        app = APP.read_text(encoding="utf-8")
        self.assertIn('<div class="card-b row gap-12 wrap align-center analytics-provider-row">', app)
        css = (APP.parent / "style.css").read_text(encoding="utf-8")
        self.assertIn(".analytics-summary .analytics-provider-row { padding: 0 var(--space-4) var(--space-4); }", css)

    def test_a_link_into_part_of_product_has_a_back(self):
        app = APP.read_text(encoding="utf-8")
        self.assertIn('if (r.page === "product" && DEEP_LINK_KEYS.some((k) => r.query?.has(k))) return navStack.length > 1', app)


class ConnectByHandIsAdvancedTests(unittest.TestCase):
    def test_typing_a_token_by_hand_is_tucked_under_advanced(self):
        app = APP.read_text(encoding="utf-8")
        self.assertNotIn("Option B:", app[app.index("const figmaOptionA"):app.index("const dialogPromise")])
        self.assertIn('const hasLogin = p.id === "figma" || p.id === "trello" || Boolean(sentryOptionA);', app)  # Sentry, once sign-in is set up
        self.assertIn('<details class="advanced conn-manual" id="conn-manual"><summary>${p.id === "sentry" ? "Advanced: use a token instead (self-hosted Sentry)" : `Advanced: enter', app)
        self.assertIn("okBtn.hidden = !manual.open", app)
        self.assertIn('name="trello_login_key"', app)  # Trello's log-in needs only its API key, kept outside Advanced
        self.assertIn("<strong>1. Create an API token</strong>", app)  # Jira: one flow, its fields stay visible


class MenusAndTapOutsideTests(unittest.TestCase):
    def test_menus_open_by_their_button_and_everything_closes_on_a_tap_outside(self):
        app = APP.read_text(encoding="utf-8")
        self.assertIn("function placeMenu(d) {", app)
        self.assertIn('document.addEventListener("toggle", (e) => { if (e.target.matches?.("details.more") && e.target.open) placeMenu(e.target); }, true);', app)
        self.assertIn('document.querySelectorAll("details.more[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });', app)
        css = (APP.parent / "style.css").read_text(encoding="utf-8")
        self.assertNotIn("bottom: calc(76px + env(safe-area-inset-bottom)); z-index: var(--z-menu); }", css)  # no bottom sheet on phones
        messages = (APP.parent / "messages.js").read_text(encoding="utf-8")
        self.assertIn('document.addEventListener("pointerdown", outside, true);', messages)
        feedback = (APP.parent / "feedback.js").read_text(encoding="utf-8")
        self.assertIn("A tap outside it (on the dimmed page) closes it", feedback)


class PrimaryOnThisComputerTests(unittest.TestCase):
    def setUp(self):
        from orchestrator.web import server
        self.server = server
        server._primary_cache.clear()

    def test_a_computer_without_an_account_or_without_a_connection_carries_on_as_primary(self):
        s = self.server
        with tempfile.TemporaryDirectory() as tmp, patch.object(s.account, "load_machine", lambda: None):
            self.assertEqual(s.primary_status(Path(tmp)), {"you": True, "enabled": False})
        def down(*a, **k):
            raise s.account.AccountError("Couldn't reach Orchestrator's servers")
        with tempfile.TemporaryDirectory() as tmp, patch.object(s.account, "load_machine", lambda: {"machine_id": "m_1", "machine_secret": "x"}), \
                patch.object(s.account, "call", down):
            status = s.primary_status(Path(tmp))
            self.assertTrue(status["you"])
            self.assertIn("unknown", status)

    def test_the_currently_selected_machine_can_always_start_work(self):
        s = self.server
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".orchestrator" / "jobs").mkdir(parents=True)
            (root / ".orchestrator" / "jobs" / "job-1.json").write_text(json.dumps({"id": "job-1", "title": "T", "status": "planned"}))
            handler = SimpleNamespace(server=SimpleNamespace(sessions=SimpleNamespace(running_job_ids=lambda: set()), _publishing_jobs=set()),
                                      _require_owner=lambda what: None)
            handler.server.child_env = lambda terminal=False: {}
            handler.server._repository_lock = __import__("threading").Lock()
            handler.server.sessions.start = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("started"))
            with self.assertRaisesRegex(RuntimeError, "started"):
                s.UIHandler._launch_run(handler, "execute", {"job": "job-1"}, root)

    def test_the_sidebar_has_no_not_primary_designation(self):
        app = APP.read_text(encoding="utf-8")
        self.assertNotIn("Not primary", app)
        self.assertNotIn("session-not-primary", app)
        self.assertNotIn('api("computer/primary"', app)

    def test_recommend_fix_button_shows_with_error(self):
        app = APP.read_text(encoding="utf-8")
        self.assertIn('data-attempt-fix="${esc(err)}" data-attempt-job="${esc(targetJob)}">Attempt fix</button>', app)
        self.assertIn('data-attempt-fix="${esc(job.last_error)}" data-attempt-job="${esc(job.job_id)}">Attempt fix</button>', app)
        self.assertIn('next?.action !== "answer"', app)

    def test_swipe_left_on_job_reveals_archive_button(self):
        app = APP.read_text(encoding="utf-8")
        css = (APP.parent / "style.css").read_text(encoding="utf-8")
        self.assertIn('class="job-swipe-wrap"', app)
        self.assertIn('class="job-swipe-archive" data-archive-jobs=', app)
        self.assertIn('.job-swipe-wrap.is-open .job-table-row', css)

    def test_oauth_expired_error_guides_user_to_ai_tools(self):
        app = APP.read_text(encoding="utf-8")
        self.assertIn('fullText.includes("oauth session expired")', app)
        self.assertIn('title: "AI sign-in expired"', app)
        self.assertIn('href="#/config/ai">Open AI tools</a>', app)

    def test_diverged_branch_error_shows_sync_guidance(self):
        app = APP.read_text(encoding="utf-8")
        srv = (APP.parents[3] / "orchestrator" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn('fullText.includes("local and remote commits have diverged")', app)
        self.assertIn('title: "Branch out of sync with remote"', app)
        self.assertIn('data-recover="git-sync-branch"', app)
        self.assertIn('elif action == "git-sync-branch":', srv)
        self.assertIn('guidance.title.includes("sync")', app)

    def test_git_sync_branch_recovery_behavior(self):
        from types import SimpleNamespace
        from unittest import mock
        from orchestrator.web import server

        def make_handler():
            h = server.UIHandler.__new__(server.UIHandler)
            h.server = SimpleNamespace(child_env=lambda: {})
            h.sent = []
            h.audited = []
            h._json = lambda payload, status=200: h.sent.append((status, payload))
            h._audit = lambda event, **kw: h.audited.append((event, kw))
            return h

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git" / "rebase-merge").mkdir(parents=True)
            calls = []

            def fake_run(cmd, **kw):
                calls.append(cmd)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            h = make_handler()
            with mock.patch.object(server.shutil, "which", return_value="/usr/bin/git"), \
                 mock.patch.object(server.subprocess, "run", side_effect=fake_run):
                h._git_sync_branch(root, {"branch": "codex/two-faction-foundation"})

            self.assertEqual(h.sent, [(200, {
                "ok": True,
                "branch": "codex/two-faction-foundation",
                "message": "Synced codex/two-faction-foundation to match origin/codex/two-faction-foundation.",
            })])
            self.assertEqual(h.audited, [("git_sync_branch", {"branch": "codex/two-faction-foundation"})])
            self.assertEqual(calls, [
                ["git", "-C", str(root), "rebase", "--abort"],
                ["git", "-C", str(root), "status", "--porcelain"],
                ["git", "-C", str(root), "fetch", "origin", "--", "codex/two-faction-foundation"],
                ["git", "-C", str(root), "checkout", "codex/two-faction-foundation"],
                ["git", "-C", str(root), "reset", "--hard", "origin/codex/two-faction-foundation"],
            ])

            # Refuses option-like branch names
            with mock.patch.object(server.shutil, "which", return_value="/usr/bin/git"):
                with self.assertRaises(server.UIError):
                    h._git_sync_branch(root, {"branch": "--delete"})

            # Refuses when uncommitted local changes exist
            with mock.patch.object(server.shutil, "which", return_value="/usr/bin/git"), \
                 mock.patch.object(server.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=" M dirty.swift\n", stderr="")):
                with self.assertRaises(server.UIError) as ctx:
                    h._git_sync_branch(root, {"branch": "main"})
                self.assertEqual(ctx.exception.status, server.HTTPStatus.CONFLICT)

            # Falls back to current branch when branch is omitted
            h2 = make_handler()
            calls2 = []

            def fake_run_fallback(cmd, **kw):
                calls2.append(cmd)
                if cmd[-1] == "--show-current":
                    return SimpleNamespace(returncode=0, stdout="feature/x\n", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(server.shutil, "which", return_value="/usr/bin/git"), \
                 mock.patch.object(server.subprocess, "run", side_effect=fake_run_fallback):
                h2._git_sync_branch(root, {})
            self.assertEqual(h2.sent[0][1]["branch"], "feature/x")

    def test_git_index_lock_offers_modal_confirmed_recovery(self):
        app = APP.read_text(encoding="utf-8")
        srv = (APP.parents[3] / "orchestrator" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn('data-recover="git-unlock-index"', app)
        self.assertIn('data-recover-confirm="Remove stale .git/index.lock?"', app)
        self.assertIn('formDialog(btn.dataset.recoverConfirm', app)
        self.assertIn('elif action == "git-unlock-index":', srv)
        self.assertIn('["lsof", str(lock)]', srv)
        self.assertIn('["git", "-C", str(root), "status", "--porcelain"]', srv)
        self.assertIn('"Update Orchestrator"', app)
        self.assertIn('cpApi("machines/update"', app)

    def test_untracked_overwrite_offers_scoped_git_stash_recovery(self):
        from orchestrator.web import server

        app = APP.read_text(encoding="utf-8")
        self.assertIn('untracked working tree files would be overwritten', app)
        self.assertIn('data-recover="git-stash"', app)
        self.assertIn('data-recover-body=', app)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir(parents=True, exist_ok=True)
            (root / "docs" / "a.md").write_text("a")
            (root / "docs" / "b.md").write_text("b")
            calls = []

            class FakeHandler:
                def __init__(self):
                    self.server = SimpleNamespace(child_env=lambda: {})
                    self.audits = []
                    self.sent = []

                def _audit(self, action, detail=None):
                    self.audits.append((action, detail))

                def _json(self, data):
                    self.sent.append(data)

            FakeHandler._git_stash = server.UIHandler._git_stash

            def fake_run(cmd, **kw):
                calls.append(cmd)
                return SimpleNamespace(returncode=0, stdout="Saved working directory and index state", stderr="")

            h = FakeHandler()
            with patch.object(server.shutil, "which", return_value="/usr/bin/git"), \
                 patch.object(server.subprocess, "run", side_effect=fake_run):
                h._git_stash(root, {"paths": ["docs/a.md", "docs/b.md"]})

            self.assertEqual(calls[0][:4], ["git", "stash", "push", "-u"])
            self.assertEqual(calls[0][-3:], ["--", "docs/a.md", "docs/b.md"])
            self.assertEqual(h.sent[0]["paths"], ["docs/a.md", "docs/b.md"])

            with patch.object(server.shutil, "which", return_value="/usr/bin/git"):
                with self.assertRaises(server.UIError):
                    h._git_stash(root, {"paths": ["../escape.txt"]})
                with self.assertRaises(server.UIError):
                    h._git_stash(root, {"paths": ["--all"]})


class CrossMachineJobsTests(unittest.TestCase):
    def test_normalize_repo_and_import_remote_job(self):
        import tempfile
        from orchestrator import account
        from orchestrator.web import server

        self.assertEqual(account._normalize_repo("git@github.com:leemosupreemo/daoc-mobile.git"), "leemosupreemo/daoc-mobile")
        self.assertEqual(account._normalize_repo("https://github.com/leemosupreemo/daoc-mobile.git"), "leemosupreemo/daoc-mobile")
        self.assertEqual(account._normalize_repo("leemosupreemo/DAoC-Mobile"), "leemosupreemo/daoc-mobile")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".orchestrator").mkdir()
            res = server.import_remote_job(root, {
                "source_machine_name": "Studio Mac",
                "job": {
                    "job_id": "20261114-013319-quick-16",
                    "title": "Make vegetation more realistic",
                    "type": "quick",
                    "status": "human-needed",
                    "branch": "codex/two-faction-foundation",
                    "summary": "Improve trees and grass",
                },
            })
            self.assertTrue(res["ok"])
            self.assertEqual(res["id"], "20261114-013319-quick-16")
            saved = json.loads((root / ".orchestrator" / "jobs" / "20261114-013319-quick-16.json").read_text())
            self.assertEqual(saved["imported_from_machine"], "Studio Mac")
            self.assertEqual(saved["branch"], "codex/two-faction-foundation")

    def test_import_carries_the_full_plan_and_every_state_field(self):
        from unittest import mock
        from orchestrator.web import server

        tasks = [{"title": f"Task {i}", "description": f"Do step {i}"} for i in range(15)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(server.subprocess, "run"):
                server.import_remote_job(root, {"job": {
                    "job_id": "job-15", "status": "review-needed", "code_host": "git", "approved": True,
                    "yolo": True, "branch": "ai/job-15", "human_clarification_question": "Which one?",
                    "test_status": "passed", "tasks_count": 15, "tasks": tasks,
                    "completed_task_indices": [0, 1, 2, 99, "x"],
                }})
            saved = json.loads((root / ".orchestrator" / "jobs" / "job-15.json").read_text())
            self.assertEqual(len(saved["plan"]["tasks"]), 15)
            self.assertEqual(saved["plan"]["tasks"][12]["description"], "Do step 12")
            self.assertEqual(saved["completed_task_indices"], [0, 1, 2])  # out-of-range and non-integer entries dropped
            self.assertEqual((saved["code_host"], saved["approved"], saved["test_status"]), ("git", True, "passed"))
            self.assertEqual(saved["human_clarification_question"], "Which one?")
            self.assertTrue(saved["yolo"] and saved["is_yolo"])  # the worker reads is_yolo

    def test_import_refuses_a_partial_plan(self):
        from orchestrator.web import server

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(server.UIError):
                server.import_remote_job(Path(tmp), {"job": {
                    "job_id": "job-cut", "status": "planned", "tasks_count": 15, "tasks": [{"title": "only one"}],
                }})
            self.assertFalse((Path(tmp) / ".orchestrator" / "jobs" / "job-cut.json").exists())
            # a finished job travels as counts only, and that is fine
            server.import_remote_job(Path(tmp), {"job": {"job_id": "job-done", "status": "completed", "tasks_count": 15, "tasks": []}})
            self.assertTrue((Path(tmp) / ".orchestrator" / "jobs" / "job-done.json").exists())

    def test_import_refuses_a_job_archived_here(self):
        from orchestrator.web import server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / ".orchestrator" / "jobs" / "archive"
            archive.mkdir(parents=True)
            original = {"job_id": "job-a", "status": "archived", "plan": {"tasks": [{"title": "kept"}]}}
            (archive / "job-a.json").write_text(json.dumps(original))
            with self.assertRaises(server.UIError):
                server.import_remote_job(root, {"job": {"job_id": "job-a", "status": "planned"}})
            self.assertFalse((root / ".orchestrator" / "jobs" / "job-a.json").exists())
            self.assertEqual(json.loads((archive / "job-a.json").read_text()), original)

    def test_import_never_hands_git_an_option_as_the_branch(self):
        from unittest import mock
        from orchestrator.web import server

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(server.subprocess, "run") as run:
            res = server.import_remote_job(Path(tmp), {"job": {"job_id": "job-x", "branch": "--prune-tags"}})
            run.assert_not_called()
            self.assertFalse(res["fetched_branch"])
            server.import_remote_job(Path(tmp), {"job": {"job_id": "job-y", "branch": "ai/job-y"}})
            self.assertEqual(run.call_args.args[0], ["git", "fetch", "origin", "--", "ai/job-y"])

    def test_the_origin_lookup_runs_git_once_until_the_repo_config_changes(self):
        import subprocess
        from unittest import mock
        from orchestrator import account

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://gitlab.com/Team-B/API.git"], cwd=root, check=True)
            real_run = subprocess.run
            with mock.patch.object(account.subprocess, "run", side_effect=real_run) as run:
                self.assertEqual(account.project_repo(root), "team-b/api")
                self.assertEqual(account.project_repo(root), "team-b/api")
                self.assertEqual(run.call_count, 1)  # the second call came from the cache
                self.assertEqual(account.project_repo(root, "Owner/Repo"), "owner/repo")  # a known GitHub repo never asks git
                self.assertEqual(run.call_count, 1)
                # a changed config is asked again, and the cache cannot grow without bound
                account._origin_repo_cache.update({("elsewhere", i): "x/y" for i in range(256)})
                with (root / ".git" / "config").open("a") as fh:
                    fh.write("[core]\n\tbare = false\n")
                self.assertEqual(account.project_repo(root), "team-b/api")
                self.assertEqual(run.call_count, 2)
                self.assertLess(len(account._origin_repo_cache), 256)

    def test_snapshot_sends_progress_state_fields_and_the_whole_plan(self):
        import subprocess
        from orchestrator import account

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://gitlab.com/Team-B/API.git"], cwd=root, check=True)
            jobs = root / ".orchestrator" / "jobs"
            jobs.mkdir(parents=True)
            tasks = [{"title": f"T{i}", "description": f"d{i}"} for i in range(15)]
            (jobs / "j1.json").write_text(json.dumps({
                "job_id": "j1", "status": "review-needed", "code_host": "git", "approved": True,
                "completed_task_indices": [0, 1, 2, 3, 4, 5, 6, 7], "plan": {"summary": "s", "tasks": tasks}}))
            (jobs / "j2.json").write_text(json.dumps({"job_id": "j2", "status": "completed", "plan": {"tasks": tasks}}))
            snap = account.build_snapshot(root=root)
            by_id = {j["job_id"]: j for j in snap["recent_jobs"]}
            j1 = by_id["j1"]
            self.assertEqual((j1["tasks_count"], j1["tasks_done"], len(j1["tasks"])), (15, 8, 15))
            self.assertEqual(j1["tasks"][14]["description"], "d14")
            self.assertEqual((j1["code_host"], j1["approved"]), ("git", True))
            self.assertEqual(j1["repo"], "team-b/api")  # a non-GitHub origin still gives a comparable repo
            self.assertEqual((by_id["j2"]["tasks_count"], by_id["j2"]["tasks"]), (15, []))  # finished: counts only

    def test_ui_merges_remote_jobs_and_provides_handoff_page(self):
        app = APP.read_text(encoding="utf-8")
        for bit in ('pages["remote-job"] = async', 'api("jobs/import-remote"', '#/remote-job/', 'on ${esc(j.remote_machine.name)}',
                    'if (parts[0] === "remote-job" && parts[1] && parts[2]) return { page: "remote-job", args: [parts[1], parts[2]], nav: "home", query };'):
            self.assertIn(bit, app)

    def test_ui_remote_rows_follow_the_review_fixes(self):
        app = APP.read_text(encoding="utf-8")
        # "Open on <computer>": repairs a moved connection, signs in properly, only then navigates, and on failure puts the
        # browser back on the computer it came from (signing in drops the token and switches the backend)
        opener = app[app.index("// Signing in moves the browser to that computer and drops the token"):]
        opener = opener[:opener.index('btn.textContent = "Open remote"')]
        order = [opener.index(bit) for bit in ("res = await signInToMachine(machine)", "res = await repairMachineConnection(machine, error)",
                                               "await unlockWith(res.token", "location.hash", "before.forEach(")]
        self.assertEqual(order, sorted(order))
        self.assertIn('"orchestrator_token"', opener.split("const before")[0])  # the token is among what is restored
        # archived jobs count as present, the control plane can't stall Home, matching is by repo, yolo is local only
        for bit in ("jobs.concat(archivedJobs)", 'cpApi("machines", { signal: AbortSignal.timeout(3000) })',
                    "Date.now() - homeRemoteSnaps.at > 30000", "p.repo || p.github_repo || p.git_remote",
                    'const same = (myRepo && rRepo) ? myRepo === rRepo : (myName && String(rj.project || "").toLowerCase() === myName);',
                    "!j.remote_machine && HOME_FILTERS.working[1](j)", "rj.tasks_done", 'st === "failed"', 'st === "scheduled"'):
            self.assertIn(bit, app)
        self.assertNotIn('t.status === "completed"', app[app.index("const remoteJobs"):app.index("const mergedJobs")])

    def test_remote_job_page_wording_switches_project_and_handles_404(self):
        import subprocess
        script = """
const fs = require('fs');
const assert = require('assert');
const code = fs.readFileSync(process.argv[1], 'utf8');
const start = code.indexOf('pages["remote-job"] = async');
const end = code.indexOf('pages.job = async');
const pages = {};
const listeners = {};
const buttons = {};
const makeBtn = (id) => ({
  id, disabled: false, textContent: '',
  addEventListener: (ev, fn) => { listeners[id + ':' + ev] = fn; }
});
const $ = (sel) => {
  if (!sel.startsWith('#')) return null;
  const id = sel.slice(1);
  if (!buttons[id]) buttons[id] = makeBtn(id);
  return buttons[id];
};
const esc = (s) => String(s ?? '');
const toasts = [];
const toast = (msg, err) => toasts.push({ msg, err: !!err });
let currentMachine = { id: 'm1', name: 'Pro', reachable: false };
const remoteJob = { job_id: 'job-7', title: 'Make vegetation more realistic', status: 'human-needed', branch: 'codex/two-faction', project: 'DAoC-Mobile', repo: 'me/daoc-mobile' };
const updateCalls = [];
let cpUpdateError = null;
const cpApi = async (path, opts) => {
  if (path === 'machines/update') {
    if (cpUpdateError) throw new Error(cpUpdateError);
    updateCalls.push(opts.body);
    return { ok: true };
  }
  return path === 'machines' ? { machines: [currentMachine] } : { snapshot: { recent_jobs: [remoteJob] } };
};
const Account = { active: () => true, MACHINE_KEY: 'mkey' };
const ACCOUNT_BACKEND_KEY = 'abk';
const localStorage = { getItem: (k) => k === 'mkey' ? 'local-m' : null, setItem: () => {}, removeItem: () => {} };
const location = { hash: '' };
const state = {
  runner: { version: '0.4.2' },
  project: { computer: { name: 'Studio' }, root: '/Users/me/other', recent: [{ name: 'other', root: '/Users/me/other', repo: 'me/other' }, { name: 'DAoC-Mobile', root: '/Users/me/DAoC-Mobile', repo: 'me/daoc-mobile' }] }
};
const switched = [];
const cleanup = [];
const dialogs = [];
const localActions = [];
const formDialog = async (title, body, okLabel) => { dialogs.push({ title, body, okLabel }); return true; };
const runAction = async (name, params) => { localActions.push({ name, params }); };
const signInToMachine = async () => ({ token: 'tok-1' });
const unlockWith = async () => {};
const switchProject = async (root, then) => { switched.push({ root, then }); };
let importShouldSucceed = false;
const api = async (path, opts) => {
  if (path === 'jobs/import-remote' && importShouldSucceed) return { ok: true, id: 'job-7', fetched_branch: true };
  throw new Error('Not found');
};
const sessionStore = {};
const sessionStorage = {
  getItem: (k) => (k in sessionStore ? sessionStore[k] : null),
  setItem: (k, v) => { sessionStore[k] = String(v); },
  removeItem: (k) => { delete sessionStore[k]; }
};
const refreshState = async () => {};

const resumeStart = code.indexOf('const UPDATE_RESUME_KEY =');
const resumeEnd = code.indexOf('function wireAutoRecovery');
eval(code.slice(resumeStart, resumeEnd));
const promptStart = code.indexOf('async function promptOrchestratorUpdate');
const promptEnd = code.indexOf('function wireApiKeyForm');
eval(code.slice(promptStart, promptEnd));
eval(code.slice(start, end));

(async () => {
  // 1. Offline remote machine: concise wording, no Back button, update modal on 404
  const off = await pages['remote-job'](['m1', 'job-7']);
  assert(off.sub.includes('remote machine (Pro, offline)'), off.sub);
  assert(off.html.includes('Remote job on Pro'), off.html);
  assert(off.html.includes('Continue here'), off.html);
  assert(!off.html.includes('Open remote'), off.html);
  assert(!off.html.includes('href="#/">Back</a>'), off.html);
  assert(!off.html.includes('Lives on'), off.html);
  off.after();
  await listeners['continue-remote-job-here:click']({ currentTarget: buttons['continue-remote-job-here'] });
  assert.equal(dialogs.length, 1);
  assert.equal(dialogs[0].title, 'Update Orchestrator');
  assert(dialogs[0].body.includes('from <code>0.4.2</code> to the latest version'), dialogs[0].body);
  assert.deepEqual(updateCalls, [{ machine_id: 'local-m' }]);
  assert.equal(buttons['continue-remote-job-here'].textContent, 'Continue here');

  // 1c. CLI-installed machine falls back to runAction("update_local") and saves pending resume, then tryResumeAfterUpdate imports job
  cpUpdateError = "Mac wasn't set up with the Orchestrator app; update it there with `orchestrator update`.";
  await listeners['continue-remote-job-here:click']({ currentTarget: buttons['continue-remote-job-here'] });
  assert.deepEqual(localActions, [{ name: 'update_local', params: {} }]);
  assert(readPendingUpdateResume()?.type === 'import-remote', 'pending resume saved');
  importShouldSucceed = true;
  const resumed = await tryResumeAfterUpdate({ quiet: true });
  assert.equal(resumed, true);
  assert.equal(location.hash, '#/jobs/job-7');
  assert.equal(readPendingUpdateResume(), null);
  importShouldSucceed = false;
  cpUpdateError = null;

  // 1d. runStatus for update_local shows Restart needed and suppresses pip upgrade noise
  const stoppingRuns = new Set();
  const nonAiActions = new Set(['update_local']);
  const runDuration = () => '1s';
  const runStatusStart = code.indexOf('function runStatus(r, vc = null)');
  const runStatusEnd = code.indexOf('function wireKeychainUnlock');
  eval(code.slice(runStatusStart, runStatusEnd));
  savePendingUpdateResume({ type: 'import-remote', job: remoteJob });
  const rsHtml = runStatus({ action: 'update_local', running: false, exit_code: 0, progress: { notice: 'To update, run: pip install --upgrade pip' } });
  assert(rsHtml.includes('Restarting UI'), rsHtml);
  assert(rsHtml.includes('Orchestrator updated — restarting UI to continue your job…'), rsHtml);
  assert(!rsHtml.includes('Completed with notes'), rsHtml);
  assert(!rsHtml.includes('pip install --upgrade pip'), rsHtml);
  clearPendingUpdateResume();

  // 1b. Online-but-connecting remote machine surfaces connection detail in sub
  currentMachine = { id: 'm1', name: 'Pro', online: true, reachable: false, connection: { state: 'checking', detail: 'Testing Tailscale Funnel' } };
  const connecting = await pages['remote-job'](['m1', 'job-7']);
  assert(connecting.sub.includes('Testing Tailscale Funnel'), connecting.sub);

  // 2. Reachable remote machine: Open remote auto-switches to matching project on remote machine
  currentMachine = { id: 'm1', name: 'Pro', reachable: true };
  const on = await pages['remote-job'](['m1', 'job-7']);
  assert(on.html.includes('Open remote'), on.html);
  on.after();
  await listeners['open-remote-machine-job:click']({ currentTarget: buttons['open-remote-machine-job'] });
  assert.deepEqual(switched, [{ root: '/Users/me/DAoC-Mobile', then: '#/jobs/job-7' }]);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        res = subprocess.run(["node", "-e", script, str(APP)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

    def test_trigger_ui_restart_prefers_launchd_else_shutdown(self):
        from unittest.mock import MagicMock, patch
        from orchestrator.web import server as srv

        fake_server = MagicMock()
        fake_server._restart_requested = False

        # When launchctl reports the agent isn't loaded (or non-darwin), fall back to server.shutdown() + _restart_requested.
        with patch.object(srv.subprocess, "run", return_value=MagicMock(returncode=1)), \
             patch.object(srv.threading, "Thread", side_effect=lambda target, **kw: MagicMock(start=target)):
            srv._trigger_ui_restart(fake_server)
        self.assertTrue(fake_server._restart_requested)
        fake_server.shutdown.assert_called_once()

        # When launchd agent is loaded on macOS, kickstart it without calling shutdown() directly.
        fake_server2 = MagicMock()
        fake_server2._restart_requested = False
        with patch.object(srv.sys, "platform", "darwin"), \
             patch.object(srv.subprocess, "run", return_value=MagicMock(returncode=0)), \
             patch.object(srv.subprocess, "Popen") as popen:
            srv._trigger_ui_restart(fake_server2)
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0][:3], ["launchctl", "kickstart", "-k"])
        self.assertFalse(fake_server2._restart_requested)


class ProjectDocumentationSectionTests(unittest.TestCase):
    def test_review_sidebar_and_page_cover_project_docs_and_ai_gap_fill(self):
        repo = APP.parents[3]
        index_html = (repo / "orchestrator" / "web" / "static" / "index.html").read_text(encoding="utf-8")
        review_block = index_html[index_html.index('<span class="label">Review</span>'):index_html.index('<span class="label">Settings</span>')]
        self.assertIn('<a href="#/project-docs" data-route="project-docs"><span>Documentation</span></a>', review_block)

        app = APP.read_text(encoding="utf-8")
        for snippet in (
            'pages["project-docs"] = async',
            'api("docs")',
            'api("docs/export")',
            'id="proj-docs-fill"',
            'Fill gaps with AI',
            'runAction("new_job", fillParams)',
            '["Documentation", "#/project-docs"',
            'title: "Document"',
            'title: "Finalize"',
            'const phasesInfo = [planStep, buildStep, verifyStep, docStep, reviewStep];',
            'phasesInfo.map((p) => `${esc(p.title)}: ${esc(p.subtitle)}`).join(" · ")',
        ):
            self.assertIn(snippet, app)

    def test_job_prompts_include_documentation_updates_and_review(self):
        prompts = APP.parents[3] / "orchestrator" / "prompts"
        for name in ("builder_feature_task.md", "builder_bug.md", "builder_infra.md", "reviewer.md"):
            text = (prompts / name).read_text(encoding="utf-8").lower()
            self.assertIn("documentation", text, f"{name} should instruct about documentation")

    def test_larger_jobs_ingest_project_documentation_as_context(self):
        from orchestrator import project_docs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# My App\n\nSetup instructions here.", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "architecture.md").write_text("# Architecture\n\nCore modules.", encoding="utf-8")
            block = project_docs.context_block(root)
            self.assertIn("## Project documentation", block)
            self.assertIn("### README.md", block)
            self.assertIn("Setup instructions here.", block)
            self.assertIn("### docs/architecture.md", block)
            self.assertIn("Core modules.", block)

        new_job_src = (APP.parents[3] / "orchestrator" / "scripts" / "new_job.py").read_text(encoding="utf-8")
        self.assertIn("project_docs.context_block(ROOT)", new_job_src)

    def test_git_auth_recovery_ui_and_handler(self):
        app = APP.read_text(encoding="utf-8")
        for snippet in (
            "isGitAuthError",
            'recoveryAction: "git-auth"',
            'title: "GitHub credentials needed"',
            'id="inline-git-auth-form"',
            'data-gh-signin="1"',
            "wireGitAuthForm(container);",
            'api("recovery/git-auth"',
        ):
            self.assertIn(snippet, app)

        server_src = (APP.parents[3] / "orchestrator" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn('elif action == "git-auth":', server_src)
        self.assertIn("def _git_auth(", server_src)

        from orchestrator.web import server as web_server
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / "repo"
            root.mkdir()
            sent = []
            handler = web_server.UIHandler.__new__(web_server.UIHandler)
            handler._audit = lambda *a, **kw: None
            handler._json = lambda data, **kw: sent.append(data)
            handler._error = lambda code, msg: sent.append({"error": msg, "code": code})
            with patch.object(web_server.sys, "platform", "linux"), \
                 patch.object(web_server.Path, "home", return_value=home), \
                 patch.object(web_server.subprocess, "run", return_value=unittest.mock.MagicMock(returncode=0, stdout="store", stderr="")):
                handler._git_auth(root, {"username": "octo", "token": "ghp_secret123"})
            self.assertTrue(sent and sent[0].get("ok"))
            cred_file = home / ".git-credentials"
            self.assertTrue(cred_file.exists())
            self.assertIn("https://octo:ghp_secret123@github.com", cred_file.read_text(encoding="utf-8"))
            self.assertEqual(cred_file.stat().st_mode & 0o777, 0o600)

    def test_oauth_expired_guidance_offers_on_the_spot_signin(self):
        app = APP.read_text(encoding="utf-8")
        for snippet in (
            'fullText.includes("oauth access token has expired")',
            'fullText.includes("re-authenticate to continue")',
            'act("ai_connect", { provider: oauthProvider, name: oauthLabel })',
            'Sign in to ${oauthLabel}',
            'ToolConnect.providerFromText(fullText)',  # one list of tools for the card and the dialog
            'ai_connect(params) {\n    ToolConnect.open(params.provider',  # the card opens the sign-in modal, not a new run
            'detail: "Your login session expired. Sign in again, then rerun."',
        ):
            self.assertIn(snippet, app)

    def test_snapshot_screen_clears_mobile_title_and_shows_try_live_progress(self):
        app = APP.read_text(encoding="utf-8")
        start = app.index("async function showSnapshotScreen(")
        block = app[start:start + 3200]
        self.assertIn('if (mobileTitle) mobileTitle.textContent = "Orchestrator";', block)
        self.assertIn('openBtn.textContent = "Connecting…";', block)
        self.assertIn('openBtn.textContent = "Try live connection";', block)

    def test_machine_failover_goes_down_list_unless_cancelled(self):
        app = APP.read_text(encoding="utf-8")
        acct = (APP.parent / "account.js").read_text(encoding="utf-8")
        self.assertIn("function orderedMachines(", acct)
        self.assertIn("Account.orderedMachines(machines, remembered)", app)
        self.assertIn("data-cancel-connect", app)
        self.assertIn("if (cancelled) return;", app)
        self.assertIn("if (failover.length)", app)

    def test_run_tabs_inside_stream_header_subcontainer(self):
        app = APP.read_text(encoding="utf-8")
        css = (APP.parent / "style.css").read_text(encoding="utf-8")
        self.assertIn('<div class="run-stream">', app)
        self.assertIn('<div class="run-stream-head">', app)
        self.assertIn('.run-stream {', css)
        self.assertIn('.run-stream-head {', css)

    def test_run_action_shows_busy_state_and_navigates_immediately(self):
        app = APP.read_text(encoding="utf-8")
        start = app.index("async function runAction(")
        block = app[start:start + 1300]
        self.assertIn('button.setAttribute("aria-busy", "true")', block)
        self.assertIn("Starting…", block)
        self.assertIn("state.runs.unshift(run)", block)
        self.assertLess(block.index("location.hash = `#/runs/${run.id}`;"), block.index("refreshState()"))

    def test_outdated_runner_auto_updates_with_notice(self):
        app = APP.read_text(encoding="utf-8")
        start = app.index("function checkRunnerVersion(")
        block = app[start:start + 1800]
        self.assertIn('action: "update_local"', block)
        self.assertIn("Updating Orchestrator automatically…", block)
        self.assertIn("Updating Orchestrator in the background…", block)
        self.assertIn("once active jobs finish", block)

    def test_timeline_animates_new_steps_and_line_extension(self):
        app = APP.read_text(encoding="utf-8")
        css = (APP.parent / "style.css").read_text(encoding="utf-8")
        self.assertIn("seenSteps", app)
        self.assertIn("is-entering", app)
        self.assertIn("is-extending", app)
        self.assertIn("@keyframes run-line-extend", css)
        self.assertIn("@keyframes run-step-pop", css)

    def test_ambient_shooting_lines_field_is_present(self):
        app = APP.read_text(encoding="utf-8")
        css = (APP.parent / "style.css").read_text(encoding="utf-8")
        self.assertIn("initAmbientLightField", app)
        self.assertIn(".ambient-light-field", css)
        self.assertIn("@keyframes ambientTrailMoveX", css)
        self.assertIn("@keyframes ambientTrailMoveY", css)

    def test_job_hero_keeps_problem_succinct_with_single_primary_action(self):
        app = APP.read_text(encoding="utf-8")
        self.assertIn("Details (${problem.lines.length})", app)
        self.assertIn('const nextBtnClass = signInAction ? "btn small" : "btn small primary";', app)

    def test_analytics_page_includes_performance_metrics_subsection(self):
        app = APP.read_text(encoding="utf-8")
        srv = (APP.parents[3] / "orchestrator" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn('id="analytics-performance"', app)
        self.assertIn("<h2>Performance metrics</h2>", app)
        self.assertIn("API latency (p50 / p95)", app)
        self.assertIn("def performance_metrics(", srv)
        self.assertIn('"performance": performance_metrics(root)', srv)

    def test_datadog_new_relic_sentry_in_performance_and_connections(self):
        from orchestrator import integrations
        for pid in ("sentry", "datadog", "new_relic"):
            self.assertIn(pid, integrations.PROVIDERS)
        app = APP.read_text(encoding="utf-8")
        self.assertIn('providers: ["sentry", "datadog", "new_relic"]', app)
        self.assertIn('#/connections#connections-plugins-diagnostics', app)

    def test_mixpanel_card_explains_project_token_vs_mcp_oauth(self):
        app = APP.read_text(encoding="utf-8")
        self.assertIn("Token auth (no OAuth)", app)
        self.assertIn("mcp.mixpanel.com/mcp", app)
        self.assertNotIn('<details class="fold model-tree mt-12" ${count > 0 ? "open" : ""}>', app)

    def test_analytics_success_measures_edits_in_modal_and_updates_prd(self):
        app = APP.read_text(encoding="utf-8")
        self.assertIn('id="edit-success-measures"', app)
        self.assertIn('call("product", { section: "success", body: v.body || "", source: "you" }', app)

    def test_unfilled_fields_use_concise_field_placeholder_style(self):
        app = APP.read_text(encoding="utf-8")
        css = (APP.parent / "style.css").read_text(encoding="utf-8")
        self.assertIn(".field-placeholder", css)
        self.assertIn('class="field-placeholder">Add project-wide outcomes and targets…', app)
        self.assertIn('class="field-placeholder">No KPIs yet', app)
        self.assertIn('class="field-placeholder">${esc(s.hint)}', app)
