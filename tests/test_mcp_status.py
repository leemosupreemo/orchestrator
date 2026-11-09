from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator import mcp_status

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
        self.assertIn("<strong class=\"conn-name\">Slack, email, this device and your Mac</strong>${alertsPill}", self.app)
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
        self.assertIn("scale: 1.15;", rule)
