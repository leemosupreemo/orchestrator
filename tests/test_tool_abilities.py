"""What each AI tool can do, scanned from the tools themselves (tool_abilities.py), and the reviews that are a button on a job
(run_ability.py). Fake `claude` and `codex` scripts stand in for the real ones, and git runs only in throwaway repositories."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import tool_abilities

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator" / "scripts"))

INIT = {"type": "system", "subtype": "init", "slash_commands": ["code-review", "security-review", "ultrareview", "init",
        "engineering:debug", "simplify", "__remote-workflow"], "skills": ["simplify", "engineering:debug"]}
FAKE_CLAUDE = f"""#!/bin/sh
echo '{json.dumps(INIT)}'
echo '{{"type": "assistant"}}'
"""
FAKE_CODEX_HELP = """#!/bin/sh
cat <<'EOF'
Usage: codex [OPTIONS] <COMMAND>

Commands:
  exec              Run Codex non-interactively [aliases: e]
  review            Run a code review non-interactively
  login             Manage login
  help              Print this message
EOF
"""


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.bin = Path(tempfile.mkdtemp())
        for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX_HELP)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.bin)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = Path(tempfile.mkdtemp())

    def test_claude_reports_what_it_has_loaded_sorted_by_kind(self):
        found = {i["name"]: i["kind"] for i in tool_abilities.scan_tool("claude", self.root)}
        self.assertEqual(found["code-review"], "built-in")
        self.assertEqual(found["simplify"], "skill")
        self.assertEqual(found["engineering:debug"], "skill")
        self.assertNotIn("__remote-workflow", found)  # internal

    def test_other_tools_list_their_subcommands_and_custom_folders(self):
        (self.bin / ".codex" / "prompts").mkdir(parents=True)
        (self.bin / ".codex" / "prompts" / "ship-it.md").write_text("Ship it")
        found = {i["name"]: i["kind"] for i in tool_abilities.scan_tool("codex", self.root)}
        self.assertEqual((found["review"], found["ship-it"]), ("subcommand", "custom"))
        self.assertNotIn("help", found)
        self.assertEqual(tool_abilities.scan_tool("not-installed-tool", self.root), [])

    def test_the_scan_is_cached_and_says_when_it_is_old(self):
        runtime = self.root / ".orchestrator"
        data = tool_abilities.scan(self.root, runtime, ["claude", "codex"])
        self.assertEqual(tool_abilities.load(runtime), json.loads(json.dumps(data)))
        self.assertFalse(tool_abilities.is_stale(data))
        self.assertTrue(tool_abilities.is_stale(data, now=data["at"] + tool_abilities.RESCAN_AFTER + 1))

    def test_only_found_abilities_of_ready_tools_become_buttons(self):
        data = tool_abilities.scan(self.root, self.root / ".orchestrator", ["claude", "codex"])
        ids = [h["id"] for h in tool_abilities.highlights(data, {"claude", "codex"})]
        self.assertEqual(ids, ["claude-code-review", "claude-security-review", "claude-ultrareview", "codex-review"])
        self.assertEqual([h["id"] for h in tool_abilities.highlights(data, {"codex"})], ["codex-review"])  # Claude not signed in
        ultra = next(h for h in tool_abilities.highlights(data, {"claude"}) if h["id"] == "claude-ultrareview")
        self.assertEqual((ultra["mode"], ultra["billed"]), ("terminal", True))

    def test_each_review_reads_and_never_edits(self):
        claude = tool_abilities.command(tool_abilities.find("claude-code-review"), "ai/job-1", "main")
        self.assertEqual(claude[:3], ["claude", "-p", "/code-review ai/job-1"])
        self.assertNotIn("Edit", claude[-1])
        self.assertEqual(tool_abilities.command(tool_abilities.find("codex-review"), "ai/job-1", "main"), ["codex", "review", "--base", "main"])
        self.assertEqual(tool_abilities.command(tool_abilities.find("claude-ultrareview"), "b", "main", 17), ["claude", "/code-review ultra 17"])


class RunAbilityTests(unittest.TestCase):
    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()

    def test_a_review_runs_on_the_jobs_branch_and_is_saved_with_its_documents(self):
        import run_ability
        tmp = Path(tempfile.mkdtemp())
        repo = tmp / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", "main")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "user.name", "Test")
        (repo / "a.txt").write_text("a")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-q", "-m", "first")
        self.git(repo, "checkout", "-q", "-b", "ai/job-1")
        (repo / "b.txt").write_text("the job's change")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-q", "-m", "job")
        self.git(repo, "checkout", "-q", "main")
        fake = tmp / "bin"
        fake.mkdir()
        (fake / "codex").write_text('#!/bin/sh\necho "branch: $(git branch --show-current)"; ls\n')
        (fake / "codex").chmod(0o755)
        job = tmp / "j1.json"
        job.write_text(json.dumps({"job_id": "j1", "branch": "ai/job-1", "base_branch": "main"}))
        with mock.patch.object(run_ability, "ROOT", repo), mock.patch.object(run_ability, "OUTPUT_DIR", tmp / "out"), \
             mock.patch.object(sys, "argv", ["run_ability.py", str(job), "codex-review"]), \
             mock.patch.dict(os.environ, {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(run_ability.main(), 0)
        saved = (tmp / "out" / "j1" / "codex-review.md").read_text()
        self.assertIn("branch: orchestrator-review/j1", saved)  # a real branch at the job's commit, in a scratch checkout
        self.assertIn("b.txt", saved)
        self.assertEqual(self.git(repo, "branch", "--show-current"), "main")  # the project folder untouched
        self.assertEqual(self.git(repo, "branch", "--list", "orchestrator-review/*"), "")  # and nothing left behind


class WiringTests(unittest.TestCase):
    def test_server_actions_and_pages(self):
        from orchestrator.web import server as ui
        self.assertIn("ai_review_terminal", ui.OWNER_ONLY_ACTIONS)  # it bills the owner's Claude account
        self.assertIn("ai_review_terminal", ui.TERMINAL_ACTIONS)
        self.assertIn("ai_review", ui.SIDE_ACTIONS)
        with self.assertRaises(ui.UIError):  # the billed cloud review never runs as a plain review
            ui.build_ai_review({"job": "x", "ability": "claude-ultrareview"}, Path("/tmp"), terminal=False)
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('act(a.mode === "terminal" ? "ai_review_terminal" : "ai_review", { job: s.id, ability: a.id })', app)
        self.assertIn('if (owner && (data.stale || !data.at)) scan(); else draw(data);', app)  # rescans when old
        config = (ROOT / "orchestrator" / "web" / "static" / "configuration.js").read_text(encoding="utf-8")
        self.assertIn('id="ai-abilities"', config)


if __name__ == "__main__":
    unittest.main()
