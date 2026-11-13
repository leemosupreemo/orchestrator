"""A real project, end to end, using real models, restricted to free ones.

Opt in with ORCHESTRATOR_E2E_LIVE=1 (it makes real model calls and takes several minutes). It builds a throwaway
project, then drives the same scripts the web UI and terminal drive: plan a feature, answer a question or accept the
architect's suggestions if asked, approve, build, test, review. `gh` is faked, so nothing reaches GitHub.

    ORCHESTRATOR_E2E_LIVE=1 python3 -m unittest tests.e2e.test_live_free_models -v

Free models are not deterministic, so this checks what must hold whatever they write: the job gets through every stage,
only the allowed models were called, the project's own tests still pass, the work is on a branch with a pull request,
and the product documents reached the roles.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.e2e.harness import DummyProject  # noqa: E402

FREE_MODEL = os.environ.get("ORCHESTRATOR_E2E_MODEL", "opencode/nemotron-3-ultra-free")
LIVE = os.environ.get("ORCHESTRATOR_E2E_LIVE") == "1"
SUMMARY = ("Add a long-word bonus to score(): a word with 7 or more letters (a-z only, ignore punctuation and case) "
           "scores 10 extra points on top of its letter points. Existing behaviour must not change.")
ANSWER = "Count letters a-z only; ignore punctuation. Bonus is a flat 10 points. Keep the change to wordgame/scoring.py and its tests."
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


@unittest.skipUnless(LIVE, "set ORCHESTRATOR_E2E_LIVE=1 to run against real (free) models")
class LiveFreeModelProject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = DummyProject([FREE_MODEL]).create()
        cls.log: list[str] = []
        cls.run_pipeline()

    @classmethod
    def tearDownClass(cls):
        if os.environ.get("ORCHESTRATOR_E2E_KEEP"):
            if cls.project._tmp:
                cls.project._tmp._finalizer.detach()  # otherwise the folder is deleted when Python exits
            print(f"\nkept: {cls.project.base}")
        else:
            cls.project.cleanup()

    @classmethod
    def step(cls, *args, timeout=1800):
        res = cls.project.orchestrator("script", *args, timeout=timeout)
        cls.log.append(f"$ {' '.join(args)}  (exit {res.returncode})\n{ANSI.sub('', res.stdout)}\n{ANSI.sub('', res.stderr)}")
        return res

    @classmethod
    def run_pipeline(cls):
        p = cls.project
        cls.step("new_job.py", "feature", "--summary", SUMMARY, "--branch-mode", "new", "--no-dispatch", "--planner", FREE_MODEL, "--builder", FREE_MODEL,
                 "--reviewer", FREE_MODEL, "--allowed-models", FREE_MODEL, "--allowed-machines", "local")
        for _ in range(4):  # a free verifier tends to raise concerns; a person would answer or accept, and so do we
            jobs = p.jobs()
            if not jobs:
                return
            job = p.job()
            if job.get("status") == "human-needed" and job.get("human_clarification_question"):
                if job.get("last_error") or (job.get("plan") or {}).get("tasks") is None:
                    return
                if job.get("verification", {}).get("status") in {"concerns", "rejected"}:
                    cls.step("job_actions.py", "approve", str(jobs[-1]))  # accept the architect's suggestions
                else:
                    cls.step("job_actions.py", "answer", str(jobs[-1]), "--answer", ANSWER)
                continue
            break
        job = p.job()
        if job.get("status") == "planned":
            cls.step("job_actions.py", "approve", str(p.jobs()[-1]))

    # -------------------------------------------------------------- what must hold

    def diagnostics(self) -> str:
        return "\n\n".join(self.log)[-6000:]

    def test_planning_produced_a_checked_plan(self):
        job = self.project.job()
        self.assertTrue(job, self.diagnostics())
        plan = job.get("plan") or {}
        self.assertTrue(plan.get("tasks"), self.diagnostics())
        self.assertTrue(plan.get("test_cases"), "a feature plan must come with test cases")
        self.assertFalse(plan.get("test_case_problems"), plan.get("test_case_problems"))
        self.assertEqual(self.project.gh()["unsupported"], [], "the orchestrator used a gh command the fake does not know")
        self.assertIn(str(job.get("issue_number")), self.project.gh()["issues"])

    def test_only_the_allowed_models_were_ever_used(self):
        job = self.project.job()
        self.assertEqual(job.get("allowed_models"), [FREE_MODEL], "a re-plan must not widen the models a job may use")
        for key in ("planner", "builder", "reviewer"):
            self.assertEqual(job.get(key), FREE_MODEL, key)
        used = {job.get(k) for k in ("actual_planner_used", "actual_builder_used", "actual_reviewer_used") if job.get(k)}
        self.assertTrue(used <= {FREE_MODEL}, f"models used: {used}")
        text = "\n".join(self.log)
        called = set(re.findall(r"Running LLM \(([^,)]+)", text))
        self.assertTrue(called <= {FREE_MODEL}, f"models called: {called}")
        self.assertTrue(called, "no model call was seen in the output")

    def test_the_job_got_through_build_and_review_onto_a_branch_with_a_pull_request(self):
        job = self.project.job()
        self.assertIn(job.get("status"), {"review-needed", "completed"}, f"status {job.get('status')}\n{self.diagnostics()}")
        self.assertTrue(job.get("branch", "").startswith("ai/"), job.get("branch"))
        self.assertTrue(self.project.gh()["prs"], "no pull request was opened")
        self.assertNotEqual(self.project.git("rev-parse", "main"), self.project.git("rev-parse", job["branch"]), "nothing was committed to the branch")

    def test_the_projects_own_tests_pass_on_the_branch_and_main_is_untouched(self):
        job = self.project.job()
        self.project.git("checkout", "-q", job["branch"])
        res = self.project.run_tests()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        scoring = (self.project.root / "wordgame" / "scoring.py").read_text()
        self.assertNotEqual(scoring, __import__("tests.e2e.harness", fromlist=["SCORING"]).SCORING, "the feature was not implemented")
        from importlib import import_module  # the behaviour itself, in a fresh interpreter so no cache is shared
        import subprocess
        code = "from wordgame.scoring import score; import sys; sys.exit(0 if (score('abcdefg') - score('abcdef') >= 10 and score('tea') == 3) else 1)"
        out = subprocess.run([sys.executable, "-c", code], cwd=self.project.root, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, "the bonus is not applied as described\n" + scoring)
        self.project.git("checkout", "-q", "main")
        self.assertIn("LETTER_POINTS", (self.project.root / "wordgame" / "scoring.py").read_text())
        self.assertEqual(self.project.git("log", "--oneline", "main").count("\n"), 0, "main moved")

    def test_the_diff_stays_inside_what_was_asked(self):
        job = self.project.job()
        changed = set(self.project.git("diff", "--name-only", f"main...{job['branch']}").splitlines())
        changed = {c for c in changed if not c.startswith(".orchestrator/") and "__pycache__" not in c}
        self.assertTrue(changed, "no files changed")
        self.assertTrue(all(c.startswith(("wordgame/", "tests/")) for c in changed), f"touched files outside the feature: {changed}")


if __name__ == "__main__":
    unittest.main()
