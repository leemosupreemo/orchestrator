"""From a computer that has never run Orchestrator to its first jobs finished, through the web app's own API, offline.

The journey a new person takes: open the app (the server starts with no project), point it at code they already have, accept
the detected settings, then start jobs from the New job form and finish them. Only OpenCode is set up, and setup happens
before it's connected, as the setup page asks. The other AI tools are installed but signed out (stubs that record any call),
so the test fails if a job picks a tool that isn't ready, or reaches a real model. About 15 s.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.e2e.harness import PYTHON, FreshInstall  # noqa: E402

# What setup.js sends from "Set up your project": every field, empty ones as null, and no models.
SETUP_FIELDS = ("project_name", "base_branch", "build_command", "test_command", "xcode_project", "xcode_workspace", "scheme", "test_target")
FEATURE = "Add a long-word bonus: a word with 7 or more letters scores 10 extra points"
QUICK = "Document the bonus in the README"


def new_job_form(job_type: str, summary: str, **changes) -> dict:
    """The New job form's payload with its defaults (current branch, run straight away, any AI)."""
    return {"type": job_type, "summary": summary, "branch_mode": "current", "no_dispatch": False, "yolo": False, "free": False,
            "feature": "", "links": [], "files": [], **({"spec": "Words of seven letters or more score 10 extra points."} if job_type == "feature" else {}),
            **changes}


class FreshInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = app = FreshInstall().start()
        try:
            cls.before = app.api("state")[1]
            cls.inspected = app.api("setup/inspect", {"root": str(app.project.root)})
            inferred = cls.inspected[1].get("inferred") or {}
            cls.applied = app.api("setup/apply", {"root": str(app.project.root), "values": {k: inferred.get(k) or None for k in SETUP_FIELDS}})
            cls.after = app.api("state")[1]
            cls.checklist = app.api("setup")[1]
            cls.providers = app.api("ai-providers")[1]
            cls.machines_at_setup = app.project.root / ".orchestrator" / "config" / "machines.json"
            cls.machines_at_setup = json.loads(cls.machines_at_setup.read_text())["machines"]
            app.connect_opencode()  # "Next, connect an AI provider"
            # Free models only, as OpenCode's own sign-in text tells a new person to choose.
            cls.first = app.run("new_job", new_job_form("feature", FEATURE, free=True))
            cls.first_job = next(iter(app.jobs()), {})
            cls.merge = app.run("merge", {"job": cls.first_job.get("id", "")}) if cls.first_job else None
            cls.second = app.run("new_job", new_job_form("quick", QUICK, branch_mode="new"))
            cls.second_job = next((j for j in app.jobs() if j["id"] == cls.second.get("result_job")), {})
        except Exception:
            cls.app.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()

    def explain(self, *runs) -> str:
        runs = runs or (self.first, self.merge, self.second)
        server = self.app.server_log.read_text()[-1500:] if self.app.server_log.exists() else ""
        return "\n".join(f"--- {r['action']} (exit {r['exit_code']})\n{r['output'][-2500:]}" for r in runs if r) + f"\n--- server log\n{server}"

    def git(self, *args):
        return self.app.project.git(*args)

    # ---- setup
    def test_a_fresh_install_starts_with_no_project_and_asks_for_one(self):
        self.assertTrue(self.before["needs_project"])
        self.assertIsNone(self.before["project"])
        self.assertEqual(self.before["projects"], [])

    def test_existing_code_is_inspected_and_its_build_and_test_commands_found(self):
        status, found = self.inspected
        self.assertEqual(status, 200, found)
        self.assertFalse(found["configured"])
        self.assertTrue(found["inferred"]["build_command"])
        self.assertIn("unittest", found["inferred"]["test_command"])

    def test_the_test_sends_what_the_setup_page_sends(self):
        page = (Path(__file__).resolve().parents[2] / "orchestrator" / "web" / "static" / "setup.js").read_text()
        self.assertEqual(re.findall(r'\["(\w+)", "[^"]+"\]', page.split("const fields = [", 1)[1].split("];", 1)[0]), list(SETUP_FIELDS))
        self.assertNotIn("models", page.split('api("setup/apply"', 1)[0].rsplit("const settings", 1)[1])

    def test_accepting_the_detected_settings_sets_the_project_up_before_any_ai_is_connected(self):
        self.assertEqual(self.applied, (200, {"ok": True, "root": str(self.app.project.root), "errors": []}))
        self.assertFalse(self.after.get("needs_project"))
        self.assertTrue(self.after["project"]["configured"])
        done = {item["id"]: item["done"] for item in self.checklist["items"]}
        self.assertTrue(done["project"] and done["git"], self.checklist)
        self.assertFalse(done["llm"], "no AI is connected yet")
        result = subprocess.run([PYTHON, "-P", "-m", "orchestrator", "check-config"], cwd=self.app.project.root, env=self.app.env(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installed_tools_nobody_signed_in_to_are_not_ready(self):
        installed = {p["id"] for p in self.providers["providers"] if p["installed"]}
        self.assertTrue({"claude", "codex", "ollama"} <= installed, installed)
        self.assertEqual([p["id"] for p in self.providers["providers"] if p["ready"]], [])

    def test_the_computer_starts_with_no_models_and_follows_the_ai_connected_later(self):
        self.assertEqual(len(self.machines_at_setup), 1)
        self.assertTrue(self.machines_at_setup[0]["models_follow_ready"])
        self.assertEqual(self.machines_at_setup[0]["models"], [], "signed-out tools must not be switched on")
        now = json.loads((self.app.project.root / ".orchestrator" / "config" / "machines.json").read_text())["machines"][0]["models"]
        self.assertTrue(now and all(m.startswith("opencode/") for m in now), now)

    # ---- the first job
    def test_the_first_job_runs_from_the_form_to_ready_to_merge(self):
        self.assertEqual(self.first["exit_code"], 0, self.explain(self.first))
        self.assertEqual(self.first_job["status"], "review-needed", self.explain(self.first))
        self.assertEqual(self.first_job["state"]["next"]["action"], "merge")

    def test_every_role_ran_on_the_ready_tool_and_nothing_else_was_tried(self):
        calls = self.app.project.llm_calls()
        self.assertTrue({"planner", "verifier", "builder", "reviewer"} <= {c["role"] for c in calls}, [c["role"] for c in calls])
        self.assertTrue(all(c["model"].startswith("opencode/") for c in calls), {c["model"] for c in calls})
        self.assertEqual(self.app.real_cli_calls(), [], "a job ran an AI tool that isn't signed in")

    def test_merge_and_complete_lands_the_change_and_the_projects_tests_pass(self):
        self.assertEqual(self.merge["exit_code"], 0, self.explain(self.merge))
        self.assertIn("LONG_WORD_BONUS", (self.app.project.root / "wordgame" / "scoring.py").read_text())
        result = self.app.project.run_tests()
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        self.assertNotIn(self.first_job["id"], [j["id"] for j in self.app.jobs()], "the merged job should leave the list")

    # ---- the second job
    def test_a_quick_job_on_its_own_branch_finishes_too(self):
        self.assertEqual(self.second["exit_code"], 0, self.explain(self.second))
        self.assertEqual(self.second_job.get("status"), "review-needed", self.explain(self.second))
        branch = self.second_job["branch"]
        self.assertNotEqual(branch, "main")
        self.assertIn("README.md", self.git("diff", "--name-only", f"main...{branch}").splitlines())
        self.assertIn("seven or more letters", self.git("show", f"{branch}:README.md"))

    def test_nothing_reaches_github_or_a_real_model(self):
        self.assertEqual(self.app.project.gh()["unsupported"], [])
        self.assertEqual(self.app.real_cli_calls(), [])


if __name__ == "__main__":
    unittest.main()
