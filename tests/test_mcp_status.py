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

    def test_servers_with_their_own_setup_and_unknown_names_are_refused(self):
        with self.assertRaisesRegex(ValueError, "setup step"):
            mcp_status.install("rea", "claude", home=self.home)
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

    def test_trello_explains_the_api_key_instead_of_a_dead_button(self):
        self.assertNotIn("quickTrello.disabled", self.app)
        self.assertIn("Trello signs you in through an app, so it needs an API key first", self.app)


if __name__ == "__main__":
    unittest.main()
