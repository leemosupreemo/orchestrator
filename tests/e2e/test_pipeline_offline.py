"""A real project, end to end, offline.

Runs the real scripts (planner, verifier, builder, test runner, reviewer, scheduler, git, product documents) against a throwaway
project, with a scripted `opencode` CLI standing in for the free model and a fake `gh`. It runs in the normal suite: no network,
no credentials, about a minute. `test_live_free_models.py` is the same journey against real free models.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.e2e.harness import DummyProject  # noqa: E402

FREE = "opencode/nemotron-3-ultra-free"
SUMMARY = "Add a long-word bonus: a word with 7 or more letters scores 10 extra points"


def project_calls(project: DummyProject, role: str):
    return [c for c in project.llm_calls() if c["role"] == role]


def plan_job(project: DummyProject, kind: str = "feature", summary: str = SUMMARY):
    return project.orchestrator("script", "new_job.py", kind, "--summary", summary, "--branch-mode", "new", "--no-dispatch", "--planner", FREE,
                                "--builder", FREE, "--reviewer", FREE, "--allowed-models", FREE, "--allowed-machines", "local")


def action(project: DummyProject, name: str, *args: str):
    return project.orchestrator("script", "job_actions.py", name, str(project.jobs()[-1]), *args)


class OfflinePipelineTests(unittest.TestCase):
    """One project, one run through every stage; the tests below look at different parts of what happened."""

    @classmethod
    def setUpClass(cls):
        cls.project = DummyProject([FREE]).create().fake_models("prose_first,concerns_once")
        cls.steps = {}
        p = cls.project
        cls.steps["plan"] = plan_job(p)
        cls.after_plan = p.job()
        cls.steps["accept"] = action(p, "approve")  # the architect raised concerns once; accept its suggestions
        cls.after_accept = p.job()
        cls.steps["approve"] = action(p, "approve")  # approve the plan: schedules and runs build, tests and review
        cls.after_run = p.job()

    @classmethod
    def tearDownClass(cls):
        cls.project.cleanup()

    def calls(self, role):
        return [c for c in self.project.llm_calls() if c["role"] == role]

    def explain(self):
        return "\n".join(f"$ {k} (exit {v.returncode})\n{re.sub(chr(27) + r'\[[0-9;?]*[a-zA-Z]', '', v.stdout)[-1500:]}\n{v.stderr[-800:]}" for k, v in self.steps.items())

    # ---- planning
    def test_planning_works_even_when_the_model_first_answers_in_prose(self):
        self.assertEqual(self.steps["plan"].returncode, 0, self.explain())
        self.assertGreaterEqual(len(self.calls("planner")), 2, "the orchestrator did not ask the model to restate its answer as JSON")
        self.assertIn("FORMAT CORRECTION", self.calls("planner")[1]["prompt"])
        self.assertTrue(self.after_plan["plan"]["tasks"])

    def test_the_plan_comes_with_test_cases_and_a_first_slice_that_runs_end_to_end(self):
        plan = self.after_plan["plan"]
        self.assertTrue(plan["test_cases"])
        self.assertFalse(plan.get("test_case_problems"))
        self.assertFalse(plan.get("slice_warnings"))
        self.assertTrue(plan["tasks"][0]["acceptance_criteria"])

    def test_the_architect_is_asked_once_then_its_suggestions_are_accepted_without_asking_again(self):
        self.assertEqual(self.after_plan["status"], "human-needed")
        self.assertEqual(self.after_plan["verification"]["status"], "concerns")
        self.assertEqual(len(self.calls("verifier")), 1, "accepting the suggestions must not re-run the verifier")
        self.assertEqual(self.after_accept["status"], "planned", self.explain())
        self.assertEqual(self.after_accept["clarification_history"] if "clarification_history" in self.after_accept else [], [])

    # ---- the models
    def test_only_the_allowed_free_model_is_ever_called_and_the_job_keeps_that_through_a_replan(self):
        self.assertTrue(self.project.llm_calls())
        self.assertEqual({c["model"] for c in self.project.llm_calls()}, {FREE})
        for stage in (self.after_plan, self.after_accept, self.after_run):
            self.assertEqual(stage["allowed_models"], [FREE])
            self.assertEqual((stage["planner"], stage["builder"], stage["reviewer"]), (FREE, FREE, FREE))

    # ---- the product documents reach every role
    def test_every_role_is_handed_the_product_documents_in_its_own_form(self):
        for role in ("planner", "verifier", "builder", "reviewer"):
            calls = self.calls(role)
            self.assertTrue(calls, f"{role} never ran\n{self.explain()}")
            self.assertIn("Product context", calls[0]["prompt"], role)
            self.assertIn("No network play", calls[0]["prompt"], f"{role} was not told the non-goals")
        self.assertLess(len(self.calls("builder")[0]["prompt"].split("Product context")[1].split("Brief:")[0]), 6000)

    # ---- build, test, review
    def test_the_job_is_built_tested_and_reviewed_onto_a_branch(self):
        self.assertEqual(self.steps["approve"].returncode, 0, self.explain())
        job = self.after_run
        self.assertTrue(job["branch"].startswith("ai/issue-"), job["branch"])
        self.assertEqual(job["status"], "review-needed", self.explain())
        self.assertEqual(job["actual_builder_used"], FREE)
        self.assertNotEqual(self.project.git("rev-parse", "main"), self.project.git("rev-parse", job["branch"]), "nothing was committed")
        self.assertEqual(self.project.git("log", "--oneline", "main").count("\n"), 0, "main was changed")

    def test_the_change_is_real_and_the_projects_own_tests_pass_on_the_branch(self):
        self.project.git("checkout", "-q", self.after_run["branch"])
        try:
            res = self.project.run_tests()
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            self.assertIn("Ran 6 tests", res.stderr)  # the three existing tests plus the three written first
        finally:
            self.project.git("checkout", "-q", "main")

    def test_the_diff_is_only_what_the_task_called_for(self):
        changed = set(self.project.git("diff", "--name-only", f"main...{self.after_run['branch']}").splitlines())
        self.assertEqual({c for c in changed if not c.startswith(".orchestrator/")}, {"wordgame/scoring.py", "tests/test_scoring.py"})

    def test_a_draft_pull_request_is_opened_and_the_review_is_recorded(self):
        prs = self.project.gh()["prs"]
        self.assertEqual(len(prs), 1, self.explain())
        pr = next(iter(prs.values()))
        self.assertTrue(pr["isDraft"])
        self.assertEqual(pr["headRefName"], self.after_run["branch"])
        self.assertEqual(self.project.gh()["unsupported"], [], "the orchestrator used a gh command the fake does not know")
        self.assertTrue(self.calls("reviewer"))

    def test_the_reviewer_is_told_what_the_plan_promised(self):
        prompt = self.calls("reviewer")[0]["prompt"]
        self.assertIn("score('abcdefg') includes 10 extra points", prompt)


class LivingPrdTests(unittest.TestCase):
    """When a job finishes the worker asks whether it changes the product requirements, and keeps them true."""

    def run_job(self, behaviour: str, auto_update: bool = True, with_prd: bool = True):
        from orchestrator import prd
        project = DummyProject([FREE]).create(with_product_docs=with_prd).fake_models(behaviour)
        self.addCleanup(project.cleanup)
        doc = prd.Prd(project.root, project.root / ".orchestrator")
        if not auto_update:
            doc.set_auto_update(False)
        self.before = doc.read()
        self.assertEqual(plan_job(project).returncode, 0)
        self.output = action(project, "approve").stdout
        return project, doc

    def prd_calls(self, project):
        return [c for c in project.llm_calls() if c["role"] == "prd"]

    def test_a_job_that_changes_the_product_updates_the_document_keeps_the_words_and_says_so(self):
        project, doc = self.run_job("prd_update")
        job = project.job()
        self.assertEqual(job["status"], "review-needed", self.output[-1500:])
        self.assertTrue(job["prd_checked"])
        text = doc.read()
        self.assertIn("Players can ask for a rematch after any game", text)
        self.assertIn("A tiny word-scoring library", text)  # the person's pitch is untouched
        self.assertIn("No network play: this is a library only", text)  # and so is Not this
        self.assertEqual([h["source"] for h in doc.history()], ["auto", "earlier"])
        self.assertEqual(doc.history()[0]["job"], job["job_id"])
        self.assertEqual(doc.notice()["summary"], "Added the rematch feature this job built")
        self.assertIn("Updated the product requirements", self.output)
        doc.revert(doc.history()[1]["id"])  # and the person can take it back
        self.assertEqual(doc.read(), self.before)

    def test_the_model_is_given_the_document_and_what_the_job_decided_and_only_the_allowed_model_is_used(self):
        project, doc = self.run_job("prd_update")
        calls = self.prd_calls(project)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], FREE)
        self.assertIn("A tiny word-scoring library", calls[0]["prompt"])
        self.assertIn("Long-word bonus", calls[0]["prompt"])  # what the job did
        self.assertEqual({c["model"] for c in project.llm_calls()}, {FREE})

    def test_no_change_needed_means_no_new_version_and_no_announcement(self):
        project, doc = self.run_job("")
        self.assertEqual(len(self.prd_calls(project)), 1)
        self.assertEqual(doc.read(), self.before)
        self.assertEqual(doc.history(), [])
        self.assertIsNone(doc.notice())
        self.assertTrue(project.job()["prd_checked"])
        self.assertIn("No change needed", self.output)

    def test_a_project_with_no_prd_still_runs_adds_no_context_and_never_makes_one_up(self):
        project, doc = self.run_job("prd_update", with_prd=False)
        job = project.job()
        self.assertEqual(job["status"], "review-needed", self.output[-1500:])
        self.assertFalse(doc.exists())
        self.assertEqual(self.prd_calls(project), [])  # nothing written yet, so nothing to keep true
        self.assertFalse(job.get("prd_checked"))  # and it will be checked once there is something to check
        for role in ("planner", "builder", "reviewer"):
            self.assertNotIn("## Product context (source of truth", project_calls(project, role)[0]["prompt"], role)

    def test_an_agentic_model_asked_about_the_document_cannot_write_it_directly(self):
        # It writes the file itself and then says "no change": with no scratch folder the document would be silently replaced, with no version.
        project, doc = self.run_job("writes_files")
        self.assertEqual(doc.read(), self.before)
        self.assertEqual(doc.history(), [])
        self.assertEqual(project.git("status", "--porcelain"), "")

    def test_switched_off_the_model_is_never_asked_and_nothing_changes(self):
        project, doc = self.run_job("prd_update", auto_update=False)
        self.assertEqual(self.prd_calls(project), [])
        self.assertEqual(doc.read(), self.before)
        self.assertEqual(project.job()["status"], "review-needed")

    def test_an_edit_that_guts_the_document_is_refused_and_the_job_still_finishes(self):
        project, doc = self.run_job("prd_gut")
        self.assertEqual(doc.read(), self.before)
        self.assertIsNone(doc.notice())
        self.assertEqual(doc.history(), [])
        self.assertEqual(project.job()["status"], "review-needed")
        self.assertIn("emptied Pitch", self.output)


class ReadOnlyModelCallTests(unittest.TestCase):
    """The web app's "just answer me" model calls (chat, Help me, Import, Draft) must not be able to change the project."""

    def test_an_agentic_model_asked_for_an_answer_writes_into_the_scratch_folder_not_the_project(self):
        import tempfile
        project = DummyProject([FREE]).create(with_product_docs=False).fake_models("writes_files")
        self.addCleanup(project.cleanup)
        with tempfile.TemporaryDirectory() as scratch:
            res = project.orchestrator("script", "job_chat_run.py", "--model", FREE, "--cwd", scratch, "--timeout", "60", input="Say hello")
            self.assertEqual(res.returncode, 0, res.stderr[-600:])
            self.assertIn("<<<ORCHESTRATOR-REPLY>>>", res.stdout)
            self.assertTrue((Path(scratch) / "docs" / "product" / "prd.md").exists(), "the model did not run in the folder it was given")
        self.assertFalse((project.root / "docs" / "product" / "prd.md").exists(), "the model changed the project")
        self.assertEqual(project.git("status", "--porcelain"), "")


class TaskCheckpointTests(unittest.TestCase):
    """Each finished task is its own commit, so the latest one can be undone."""

    @classmethod
    def setUpClass(cls):
        cls.project = DummyProject([FREE]).create().fake_models("two_tasks")
        p = cls.project
        plan_job(p)
        action(p, "approve")
        cls.output = action(p, "approve").stdout
        cls.job = p.job()

    @classmethod
    def tearDownClass(cls):
        cls.project.cleanup()

    def test_each_task_is_committed_by_itself_and_recorded(self):
        self.assertEqual(self.job["completed_task_indices"], [0, 1], self.output[-1500:])
        self.assertEqual(sorted(self.job["task_commits"]), ["0", "1"])
        subjects = self.project.git("log", "--format=%s", f"main..{self.job['branch']}").splitlines()
        self.assertTrue(any("task 1/2" in s for s in subjects), subjects)
        self.assertTrue(any("task 2/2" in s for s in subjects), subjects)
        self.assertEqual(self.project.git("show", "--name-only", "--format=", self.job["task_commits"]["1"]).strip(), "README.md")

    def test_the_jobs_changed_files_still_cover_every_task(self):
        files = set(self.job["ai_modified_files"]) | set(self.job["ai_untracked_files"])
        self.assertTrue({"wordgame/scoring.py", "tests/test_scoring.py", "README.md"} <= files, files)

    def test_the_latest_task_can_be_undone_and_the_earlier_one_stays(self):
        from orchestrator import task_revert
        self.project.git("checkout", "-q", self.job["branch"])
        try:
            job = json.loads(json.dumps(self.job))
            task_revert.revert_latest(self.project.root, job, 1)
            self.assertNotIn("score a 10 point bonus", (self.project.root / "README.md").read_text())
            self.assertIn("LONG_WORD_BONUS", (self.project.root / "wordgame" / "scoring.py").read_text())
            self.assertEqual(job["completed_task_indices"], [0])
            self.assertEqual(self.project.run_tests().returncode, 0)
        finally:
            self.project.git("reset", "-q", "--hard", self.job["task_commits"]["1"])
            self.project.git("checkout", "-q", "main")


class CombinedCheckTests(unittest.TestCase):
    """Two jobs of one feature that each pass alone but break the project's tests together."""

    @classmethod
    def setUpClass(cls):
        cls.project = p = DummyProject([FREE]).create()
        base = p.git("rev-parse", "main")
        scoring = (p.root / "wordgame" / "scoring.py").read_text()
        # Job 1 changes what 'tea' scores (and its own test agrees); job 2 adds a test that relies on the old value.
        p.git("checkout", "-q", "-b", "ai/one", base)
        (p.root / "wordgame" / "scoring.py").write_text(scoring.replace('"aeioulnrst"', '"aeioulnrs"').replace('"dg"', '"dgt"'))
        tests = (p.root / "tests" / "test_scoring.py").read_text()
        (p.root / "tests" / "test_scoring.py").write_text(tests.replace('score("tea"), 3)', 'score("tea"), 4)').replace('score("t-e!"), 2)', 'score("t-e!"), 3)'))
        p.git("commit", "-q", "-am", "job one")
        p.git("checkout", "-q", "-b", "ai/two", base)
        (p.root / "tests" / "test_extra.py").write_text("import unittest\nfrom wordgame.scoring import score\n\nclass Extra(unittest.TestCase):\n    def test_tea_is_three(self):\n        self.assertEqual(score('tea'), 3)\n")
        p.git("add", "-A")
        p.git("commit", "-q", "-m", "job two")
        p.git("checkout", "-q", "main")
        rt = p.root / ".orchestrator"
        (rt / "jobs").mkdir(exist_ok=True)
        (rt / "features.json").write_text(json.dumps({"features": [{"id": "word-score", "name": "Word score", "status": "in-progress", "paths": [], "depends_on": []}]}))
        for n, b in ((1, "ai/one"), (2, "ai/two")):
            (rt / "jobs" / f"20261001-000000-feature-{n}.json").write_text(json.dumps({"job_id": f"20261001-000000-feature-{n}", "status": "review-needed", "title": f"Job {n}", "type": "feature-plan", "branch": b, "feature": "word-score"}))
        cls.verify = p.orchestrator("script", "verify_integration.py", "word-score")

    @classmethod
    def tearDownClass(cls):
        cls.project.cleanup()

    def test_each_branch_passes_alone(self):
        for b in ("ai/one", "ai/two"):
            self.project.git("checkout", "-q", b)
            try:
                self.assertEqual(self.project.run_tests().returncode, 0, b)
            finally:
                self.project.git("checkout", "-q", "main")

    def test_together_they_fail_and_the_result_says_so(self):
        self.assertEqual(self.verify.returncode, 1, self.verify.stdout[-800:])
        result = json.loads((self.project.root / ".orchestrator" / "integration" / "word-score.json").read_text())
        self.assertEqual((result["status"], sorted(result["merged"])), ("test-failed", ["ai/one", "ai/two"]))
        self.assertIn("test_tea_is_three", result["test"]["tail"])

    def test_the_checkout_is_untouched(self):
        self.assertEqual(self.project.git("branch", "--show-current"), "main")
        self.assertEqual(self.project.git("status", "--porcelain"), "")
        self.assertEqual(self.project.git("worktree", "list").count("\n"), 0)


class BuilderTestCommandTests(unittest.TestCase):
    """The worker checks which tests the builder ran before it counts a task as done."""

    def run_with(self, behaviour: str):
        project = DummyProject([FREE]).create().fake_models(behaviour)
        self.addCleanup(project.cleanup)
        self.assertEqual(plan_job(project).returncode, 0)
        self.output = action(project, "approve").stdout
        return project, project.job()

    def test_running_the_whole_suite_is_accepted(self):
        # The builder prompt asks for "the complete repository test command"; that covers every planned test.
        project, job = self.run_with("full_suite")
        self.assertEqual(job["status"], "review-needed", job.get("last_error"))
        self.assertEqual(job["completed_task_indices"], [0])

    def test_running_only_an_unrelated_test_is_still_caught(self):
        project, job = self.run_with("narrow_tests")
        self.assertIn("the test command was downgraded", self.output)
        self.assertEqual(job.get("completed_task_indices"), [], "the task was counted as done without its tests being run")
        self.assertFalse(project.gh()["prs"], "a pull request was opened for work that was not verified")


if __name__ == "__main__":
    unittest.main()
