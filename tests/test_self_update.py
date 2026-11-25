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

    def test_self_update_tick_skips_restart_if_a_run_started_during_apply(self):
        from unittest.mock import MagicMock, patch
        from orchestrator.web import server as web_server

        fake = MagicMock()
        # Idle before apply(), then a run starts while apply() is downloading.
        fake.sessions.list.side_effect = [[], [{"id": "r1", "running": True}]]
        with patch.object(web_server.self_update, "load", return_value={"auto_minor": True}), \
             patch.object(web_server.self_update, "due", return_value=True), \
             patch.object(web_server.self_update, "check", return_value={"update": "minor", "behind": 1}), \
             patch.object(web_server.self_update, "should_install", return_value=True), \
             patch.object(web_server.self_update, "apply", return_value={"ok": True, "restart": True, "message": "Updated"}), \
             patch.object(web_server, "_trigger_ui_restart") as restart:
            web_server.UIServer.self_update_tick(fake)
        restart.assert_not_called()

    def test_needs_restart_detects_new_commit_on_disk_and_waits_for_clean_orchestrator_tree(self):
        boot = self_update.current_commit(self.mac)
        self.assertTrue(boot)
        self.assertFalse(self_update.needs_restart(boot, self.mac))
        self.commit("Fix cache cleanup")
        git(self.mac, "pull", "-q", "--ff-only", "origin", "web-ui")
        self.assertTrue(self_update.needs_restart(boot, self.mac))
        # Mid-edit tracked changes under orchestrator/ defer restart until committed/cleaned.
        (self.mac / "orchestrator" / "release.json").write_text("{}\n")
        self.assertFalse(self_update.needs_restart(boot, self.mac))
        git(self.mac, "checkout", "--", "orchestrator/release.json")
        # Untracked scratch files under orchestrator/ do not block restart.
        (self.mac / "orchestrator" / "scratch.tmp").write_text("temp\n")
        self.assertTrue(self_update.needs_restart(boot, self.mac))
        (self.mac / "orchestrator" / "scratch.tmp").unlink()
        # Non-git installs or empty boot_commit never claim a git HEAD restart.
        self.assertFalse(self_update.needs_restart("", self.mac))
        with patch.object(self_update, "install_kind", return_value="pip"):
            self.assertEqual(self_update.current_commit(self.mac), "")
            self.assertFalse(self_update.needs_restart(boot, self.mac))
        with patch.object(self_update, "install_kind", return_value="packaged"):
            self.assertFalse(self_update.needs_restart(boot, self.mac))

    def test_self_update_tick_restarts_when_boot_commit_differs_from_disk_and_idle(self):
        from unittest.mock import MagicMock, patch
        from orchestrator.web import server as web_server

        fake = MagicMock()
        fake._boot_commit = "a" * 40
        fake.sessions.list.return_value = []
        with patch.object(web_server.self_update, "load", return_value={"auto_minor": True, "last_check": {"update": "none"}}), \
             patch.object(web_server.self_update, "due", return_value=False), \
             patch.object(web_server.self_update, "should_install", return_value=False), \
             patch.object(web_server.self_update, "needs_restart", return_value=True), \
             patch.object(web_server, "_trigger_ui_restart") as restart:
            web_server.UIServer.self_update_tick(fake)
        restart.assert_called_once_with(fake)

        # Respects auto_minor=False.
        with patch.object(web_server.self_update, "load", return_value={"auto_minor": False, "last_check": {"update": "none"}}), \
             patch.object(web_server.self_update, "due", return_value=False), \
             patch.object(web_server.self_update, "should_install", return_value=False), \
             patch.object(web_server.self_update, "needs_restart", return_value=True), \
             patch.object(web_server, "_trigger_ui_restart") as restart:
            web_server.UIServer.self_update_tick(fake)
        restart.assert_not_called()

        # Does not interrupt an active run.
        fake.sessions.list.return_value = [{"id": "r1", "running": True}]
        with patch.object(web_server.self_update, "load", return_value={"auto_minor": True, "last_check": {"update": "none"}}), \
             patch.object(web_server.self_update, "due", return_value=False), \
             patch.object(web_server.self_update, "should_install", return_value=False), \
             patch.object(web_server.self_update, "needs_restart", return_value=True), \
             patch.object(web_server, "_trigger_ui_restart") as restart:
            web_server.UIServer.self_update_tick(fake)
        restart.assert_not_called()

    def test_deferred_restart_completes_on_next_idle_tick_after_apply(self):
        from unittest.mock import MagicMock, patch
        from orchestrator.web import server as web_server

        boot = self_update.current_commit(self.mac)
        self.commit("Fix Unity cache daemon check")
        fake = MagicMock()
        fake._boot_commit = boot
        # Tick 1: idle before apply(), run starts during apply() -> restart deferred.
        fake.sessions.list.side_effect = [[], [{"id": "r1", "running": True}]]
        with patch.object(self_update, "install_root", return_value=self.mac), \
             patch.object(web_server, "_trigger_ui_restart") as restart:
            web_server.UIServer.self_update_tick(fake, force=True)
            restart.assert_not_called()
            # Tick 2: run finished, HEAD == origin/web-ui (update == "none"), but HEAD != boot -> restarts now.
            fake.sessions.list.side_effect = None
            fake.sessions.list.return_value = []
            web_server.UIServer.self_update_tick(fake, force=False)
            restart.assert_called_once_with(fake)

    def test_shared_ui_server_keeps_boot_commit_in_shared_attributes(self):
        server_src = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn("self._boot_commit = self_update.current_commit()", server_src)
        self.assertIn('"_boot_commit"', server_src)


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
        self.assertNotIn("What's new", out["breaking"])
        self.assertNotIn("Recent updates", out["breaking"])
        self.assertNotIn("An update is ready", out["minor"])
        self.assertIn("Outdated (2 behind)", out["minor"])
        self.assertIn('<button type="button" class="btn small" data-why="Already on the latest version." disabled>Up to date</button>', out["current"])
        self.assertNotIn('<div class="col-date">Action</div>', out["current"])
        self.assertNotIn(">Update now</button>", out["current"])
        self.assertNotIn(">Update now</button>", out["blocked"])
        self.assertIn('data-update-setting="auto_minor" checked', out["minor"])
        self.assertIn('data-update-setting="auto_breaking">', out["minor"])
        self.assertIn('data-update-op="install-all"', out["multi"])
        self.assertNotIn('data-update-op="check-all"', out["multi"])
        self.assertNotIn('data-update-op="check"', out["multi"])
        self.assertIn('class="job-table"', out["multi"])
        self.assertIn('<span class="pill idle">Local</span>', out["multi"])
        self.assertIn('MacBook Air', out["multi"])
        self.assertIn('Outdated (3 behind)', out["multi"])
        self.assertIn('data-update-machine="b" data-update-machine-op="install"', out["multi"])
        self.assertNotIn('data-failover-setting', out["multi"])
        cfg = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "configuration.js").read_text(encoding="utf-8")
        self.assertIn('data-failover-setting', cfg)
        self.assertIn('Offline', out["multi"])
        self.assertNotIn('data-update-sync-all', out["multi"])
        self.assertNotIn('data-update-sync-all', out["current"])
