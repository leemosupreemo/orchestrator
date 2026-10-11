"""Exercise the Connections page renderer with analytics API responses."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from orchestrator.analytics import public_providers

STATIC = Path(__file__).resolve().parents[1] / "orchestrator/web/static"


@unittest.skipUnless(shutil.which("node"), "node is required for UI rendering")
class AnalyticsConnectionsUITests(unittest.TestCase):
    def render(self, data, role="owner", plugins=None):
        script = r'''
const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const input = JSON.parse(process.argv[2]);
const pages = {}, state = {you: {role: input.role}, project: {name: "Demo", branch: "main"}};
const esc = value => String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const providerIcon = () => "";
const pill = (tone, label) => `<span class="pill ${tone}">${esc(label)}</span>`;
require(require("path").join(require("path").dirname(process.argv[1]), "html.js"));
require(require("path").join(require("path").dirname(process.argv[1]), "configuration.js"));
const ConfigurationPages = globalThis.ConfigurationPages;
eval(source.slice(source.indexOf("function connectionStatus("), source.indexOf("const MOBILE_CONTROL_TOOLS")));
eval(source.slice(source.indexOf("const MOBILE_CONTROL_TOOLS"), source.indexOf("pages.connections = async")));
async function api(path) {
  if (path === "integrations") return {integrations: input.plugins || []};
  if (path === "config") return {};
  if (path === "analytics") {
    if (input.data === null) throw new Error("Unavailable");
    return input.data;
  }
  throw new Error(`Unexpected request: ${path}`);
}
const start = source.indexOf("const ANALYTICS_BLURBS");
if (start >= 0 && start < source.indexOf("pages.measure = async")) {
  eval(source.slice(start, source.indexOf("pages.measure = async")));
}
const pageStart = source.indexOf("pages.connections = async");
eval(source.slice(pageStart, source.indexOf("\n};", pageStart) + 3));
pages.connections(undefined, new URLSearchParams()).then(page => console.log(JSON.stringify(page.html)));
'''
        result = subprocess.run(
            ["node", "-e", script, str(STATIC / "app.js"), json.dumps({"data": data, "role": role, "plugins": plugins})],
            text=True, capture_output=True, check=True,
        )
        return json.loads(result.stdout)

    def data(self, provider="", key_set=False):
        return {
            "providers": public_providers(), "provider": provider,
            "provider_name": next((p["name"] for p in public_providers() if p["id"] == provider), ""),
            "key_set": key_set, "region": "eu", "dashboard_url": None,
            "provider_mcp": {p["id"]: [
                {"org": "OpenAI (Codex)", "installed": True, "detected_via": "~/.codex/config.toml"},
                {"org": "Anthropic (Claude Code)", "installed": False, "cmd": f"configure {p['id']}"},
            ] for p in public_providers()},
        }

    def test_connections_sections_follow_requested_order(self):
        html = self.render(self.data())
        sections = ["connections-plugins", "connections-alerts", "connections-git",
                    "connections-analytics", "connections-mobile", "connections-mcp"]
        positions = []
        for section in sections:
            self.assertIn(f'id="{section}"', html)
            positions.append(html.index(f'id="{section}"'))
        self.assertEqual(positions, sorted(positions))
        self.assertIn('href="#/git"', html)
        self.assertIn('href="#/config/email"', html)
        self.assertIn('data-rec-tool="shellfish"', html)
        self.assertIn('data-rec-mcp="context7"', html)

    def test_plugins_are_grouped_by_purpose_without_losing_unknown_providers(self):
        plugins = [{"id": pid, "name": pid, "blurb": "", "connected": False}
                   for pid in ("jira", "figma", "sentry", "trello", "future-provider")]
        html = self.render(self.data(), plugins=plugins)
        expected = {
            "design": ["figma"], "planning": ["jira", "trello"],
            "diagnostics": ["sentry"], "other": ["future-provider"],
        }
        for group, providers in expected.items():
            self.assertIn(f'id="connections-plugins-{group}"', html)
            body = html.split(f'id="connections-plugins-{group}"', 1)[1].split('<section class="connections-plugin-group"', 1)[0]
            for provider in providers:
                self.assertIn(f'data-conn="{provider}"', body)
                self.assertEqual(html.count(f'data-conn="{provider}"'), 1)
        analytics = html.split('id="connections-analytics"', 1)[1]
        self.assertIn('data-analytics-p="mixpanel"', analytics)

    def test_each_provider_has_its_own_status_tree_and_settings_link(self):
        html = self.render(self.data())
        for provider in public_providers():
            with self.subTest(provider=provider["id"]):
                self.assertIn(f'data-analytics-p="{provider["id"]}"', html)
                card = html.split(f'data-analytics-p="{provider["id"]}"', 1)[1].split("</section>", 1)[0]
                self.assertIn(f'{provider["name"]} MCP by LLM organization', card)
                self.assertIn("1 / 2 installed", card)
                self.assertIn("OpenAI (Codex)", card)
                self.assertIn("Anthropic (Claude Code)", card)
                self.assertIn(f'data-setup-copy="configure {provider["id"]}"', card)
                self.assertIn(f'href="#/measure?provider={provider["id"]}"', card)
                self.assertIn("Not connected", card)
                self.assertNotIn("Open project", card)

    def test_only_connected_provider_has_a_dashboard_link(self):
        for provider in public_providers():
            with self.subTest(provider=provider["id"]):
                html = self.render(self.data(provider["id"], True), role="member")
                self.assertEqual(html.count("Connected (EU)"), 1)
                self.assertIn(f'href="{provider["dashboards"]["eu"]}"', html)
                for other in public_providers():
                    if other["id"] != provider["id"]:
                        self.assertNotIn(f'href="{other["dashboards"]["eu"]}"', html)

    def test_missing_key_is_not_shown_as_connected(self):
        html = self.render(self.data("mixpanel"))
        self.assertIn("Key missing", html)
        self.assertNotIn('href="https://eu.mixpanel.com/project"', html)

    def test_analytics_failure_keeps_connections_usable(self):
        html = self.render(None)
        self.assertIn("Analytics connection status is unavailable", html)
        self.assertIn('href="#/measure"', html)
        self.assertNotIn("Not configured", html)
