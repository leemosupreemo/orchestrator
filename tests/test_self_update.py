"""Orchestrator keeping itself current: minor updates install by themselves, breaking ones wait unless allowed. Git runs
only in throwaway repositories with a local bare remote, never this checkout."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import self_update


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        patcher = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(base / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        kind = patch.object(self_update, "install_kind", return_value="git")
        kind.start()
        self.addCleanup(kind.stop)
        self.origin = base / "origin.git"
        git(base, "init", "-q", "--bare", "-b", "web-ui", str(self.origin))
        self.dev = base / "dev"
        git(base, "clone", "-q", str(self.origin), str(self.dev))
        git(self.dev, "checkout", "-q", "-b", "web-ui")
        for repo in (self.dev,):
            git(repo, "config", "user.email", "t@example.com")
            git(repo, "config", "user.name", "T")
        self.release(1)
        self.commit("start")
        self.mac = base / "mac"
        git(base, "clone", "-q", "-b", "web-ui", str(self.origin), str(self.mac))

    def release(self, level, summary=""):
        (self.dev / "orchestrator").mkdir(exist_ok=True)
        (self.dev / "orchestrator" / "release.json").write_text(json.dumps(
            {"breaking": level, "breaking_changes": [{"level": level, "summary": summary}] if summary else []}))

    def commit(self, message):
        (self.dev / "f.txt").write_text(message)
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", message)
        git(self.dev, "push", "-q", "origin", "web-ui")

    def test_a_minor_update_is_found_and_installs_by_itself(self):
        self.assertEqual(self_update.check(self.mac)["update"], "none")
        self.commit("Fix the job page")
        status = self_update.check(self.mac)
        self.assertEqual((status["update"], status["behind"], status["changes"]), ("minor", 1, ["Fix the job page"]))
        self.assertTrue(self_update.should_install(status, self_update.load()))
        result = self_update.apply(self.mac)
        self.assertTrue(result["ok"] and result["restart"])
        self.assertEqual((self.mac / "f.txt").read_text(), "Fix the job page")
        self.assertEqual(self_update.check(self.mac)["update"], "none")
        self.assertEqual(len(self_update.load()["history"]), 1)

    def test_a_breaking_update_waits_unless_allowed(self):
        self.release(2, "Jobs sync through your account")
        self.commit("New sync format")
        status = self_update.check(self.mac)
        self.assertEqual((status["update"], status["breaking_changes"]), ("breaking", ["Jobs sync through your account"]))
        self.assertFalse(self_update.should_install(status, self_update.load()))
        self_update.set_options(auto_breaking=True)
        self.assertTrue(self_update.should_install(status, self_update.load()))
        self_update.set_options(auto_minor=False)
        self.assertFalse(self_update.should_install({"update": "minor"}, self_update.load()))

    def test_edits_of_its_own_block_the_update_and_nothing_is_touched(self):
        self.commit("Something new")
        (self.mac / "f.txt").write_text("local edit")
        status = self_update.check(self.mac)
        self.assertIn("edits of its own", status["blocked"])
        self.assertFalse(self_update.should_install(status, self_update.load()))
        self.assertFalse(self_update.apply(self.mac)["ok"])
        self.assertEqual((self.mac / "f.txt").read_text(), "local edit")

    def test_an_unreachable_origin_is_reported_not_installed(self):
        git(self.mac, "remote", "set-url", "origin", "/nonexistent/repo.git")
        status = self_update.check(self.mac)
        self.assertEqual(status["update"], "unknown")
        self.assertFalse(self_update.should_install(status, self_update.load()))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(__import__("shutil").which("node"), "node is required")
class UpdatesPageTests(unittest.TestCase):
    def test_the_page_says_what_kind_of_update_waits_and_offers_the_settings(self):
        app = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        start = app.index("function updatesPageHtml(")
        fn = app[start:app.index("\n// ----", start)]
        script = f"""
const esc = (s) => String(s); const ago = () => "5m ago"; const plural = (n, w) => `${{n}} ${{w}}${{n === 1 ? "" : "s"}}`;
{fn}
const base = {{ version: "0.1.0", install: "git", settings: {{ auto_minor: true, auto_breaking: false }}, history: [] }};
console.log(JSON.stringify({{
  breaking: updatesPageHtml({{ ...base, last_check: {{ at: 1, update: "breaking", behind: 3, branch: "web-ui", installed: "abc", breaking_changes: ["New sync"], changes: ["a", "b"] }} }}),
  minor: updatesPageHtml({{ ...base, last_check: {{ at: 1, update: "minor", behind: 2 }} }}),
  multi: updatesPageHtml({{ ...base, last_check: {{ at: 1, update: "none", installed: "aaa111", latest: "aaa111" }} }}, {{
    pairedMachines: [{{ id: "a", name: "MacBook Pro", reachable: true }}, {{ id: "b", name: "MacBook Air", reachable: true, version: "0.1.0" }}, {{ id: "c", name: "Mini", reachable: false, last_seen: 1 }}],
    currentMachineId: "a", currentMachineName: "MacBook Pro",
    remoteUpdates: {{ b: {{ version: "0.1.0", last_check: {{ at: 1, update: "minor", behind: 3, installed: "old999", branch: "web-ui" }} }} }},
    otherCount: 2, syncAll: true,
  }}),
  current: updatesPageHtml({{ ...base, last_check: {{ at: 1, update: "none" }} }}),
  blocked: updatesPageHtml({{ ...base, last_check: {{ at: 1, update: "minor", blocked: "This copy of Orchestrator has edits of its own" }} }}),
}}));
"""
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertIn("A breaking update is ready.", out["breaking"])
        self.assertIn("<li>New sync</li>", out["breaking"])
        self.assertIn(">Update now</button>", out["breaking"])
        self.assertIn("What's new (2)", out["breaking"])
        self.assertIn("It installs by itself once no run is going.", out["minor"])
        self.assertIn("Up to date.", out["current"])
        self.assertNotIn(">Update now</button>", out["current"])
        self.assertNotIn(">Update now</button>", out["blocked"])
        self.assertIn('data-update-setting="auto_minor" checked', out["minor"])
        self.assertIn('data-update-setting="auto_breaking">', out["minor"])
        self.assertIn('data-update-op="install-all"', out["multi"])
        self.assertIn('data-update-op="check-all"', out["multi"])
        self.assertIn('<span class="pill idle">Local</span>', out["multi"])
        self.assertIn('MacBook Air', out["multi"])
        self.assertIn('Outdated (3 behind)', out["multi"])
        self.assertIn('data-update-machine="b" data-update-machine-op="install"', out["multi"])
        self.assertIn('Offline', out["multi"])
        self.assertNotIn('data-update-sync-all', out["multi"])
        self.assertNotIn('data-update-sync-all', out["current"])
