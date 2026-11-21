"""A job's acceptance criteria reach every step that judges its work, and a product job that only touched tests is flagged.

The vegetation job's plan said "Increase foliage density 2x" (asked for in feedback), but its fix loop was given only the original
brief and drifted to making tests pass, its reviewer was given only the brief, and the resume committed a single test file."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator/scripts"))
import worker_run as worker  # noqa: E402
from orchestrator import acceptance  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}
# The shape of the real job: the plan lists both, the spec says who asked for each.
VEGETATION_JOB = {
    "type": "quick-fix", "title": "Quick: The vegetatation needs to look more realistic",
    "plan": {"acceptance_criteria": ["Implementation matches provided instructions.", "Increase foliage density 2x"]},
    "spec": {
        "acceptance_criteria": [
            {"text": "Implementation matches provided instructions.", "provenance": "inferred", "source": "agent_inference"},
            {"text": "Increase foliage density 2x", "provenance": "explicit", "source": "user_experience_feedback"}],
        "functional_requirements": [
            {"text": "Quick: The vegetatation needs to look more realistic", "provenance": "explicit", "source": "user_prompt"},
            {"text": "Increase foliage density 2x", "provenance": "explicit", "source": "user_experience_feedback"}]},
}


class CriteriaTests(unittest.TestCase):
    def test_the_persons_feedback_is_found_once_and_the_boilerplate_is_dropped(self):
        self.assertEqual(acceptance.criteria(VEGETATION_JOB), [{"text": "Increase foliage density 2x", "person": True}])

    def test_the_persons_own_criteria_come_first_and_plain_plan_items_count(self):
        job = {"plan": {"acceptance_criteria": ["Shows a spinner", "Retries twice"]},
               "spec": {"acceptance_criteria": [{"text": "Retries twice", "provenance": "explicit", "source": "user_prompt"}]}}
        self.assertEqual([(c["text"], c["person"]) for c in acceptance.criteria(job)], [("Retries twice", True), ("Shows a spinner", False)])

    def test_nothing_listed_means_no_block_and_odd_shapes_are_survived(self):
        self.assertEqual(acceptance.block({}), "")
        self.assertEqual(acceptance.block({"plan": {"acceptance_criteria": None}, "spec": "x"}), "")
        self.assertEqual(len(acceptance.criteria({"plan": {"acceptance_criteria": [f"c{i}" for i in range(40)]}})), acceptance.MAX_CRITERIA)

    def test_the_block_says_tests_alone_do_not_meet_it(self):
        text = acceptance.block(VEGETATION_JOB)
        self.assertIn("[asked for by the person] Increase foliage density 2x", text)
        self.assertIn("passing tests does not satisfy them", text)


class DiffWarningTests(unittest.TestCase):
    def job(self, files, **extra):
        return {**VEGETATION_JOB, "ai_modified_files": files, **extra}

    def test_a_product_job_that_changed_only_tests_is_flagged_with_what_it_was_asked(self):
        warnings = acceptance.diff_warnings(self.job(["tests/Daoc.Protocol.Tests/ClientConfigTests.cs"]))
        self.assertEqual(len(warnings), 1)
        self.assertIn("ClientConfigTests.cs", warnings[0])
        self.assertIn("Increase foliage density 2x", warnings[0])

    def test_changing_product_code_too_is_fine_and_tool_state_is_not_counted(self):
        self.assertEqual(acceptance.diff_warnings(self.job(["tests/a_test.py", "DaocUnity/Assets/Scripts/World/GrassSystem.cs"])), [])
        self.assertEqual(len(acceptance.diff_warnings(self.job([".claude/helpers/x.cjs", "tests/test_a.py"]))), 1)  # only the test counts
        self.assertEqual(acceptance.diff_warnings(self.job([".claude/helpers/x.cjs"])), [])  # nothing real changed: another rule's job

    def test_test_jobs_and_empty_jobs_are_not_flagged(self):
        self.assertEqual(acceptance.diff_warnings(self.job(["tests/test_a.py"], type="coverage")), [])
        self.assertEqual(acceptance.diff_warnings(self.job(["tests/test_a.py"], output_scope="tests-only")), [])
        self.assertEqual(acceptance.diff_warnings(self.job([])), [])
        self.assertEqual(acceptance.diff_warnings({}), [])


class ReviewerAndDebuggerSeeThemTests(unittest.TestCase):
    def test_the_reviewer_is_given_the_criteria_and_the_warning(self):
        import review_ready as rr
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "brief.md").write_text("The vegetation needs to look more realistic")
            job = {**VEGETATION_JOB, "ai_modified_files": ["tests/Daoc.Protocol.Tests/ClientConfigTests.cs"]}
            (root / "job.json").write_text(json.dumps(job))
            prompts = []

            def fake(model, prompt, **kwargs):
                prompts.append(prompt)
                return ("ok", "m", "sid")

            argv = ["review_ready.py", "7", "--brief-file", str(root / "brief.md"), "--job-file", str(root / "job.json"), "--reviewer", "x"]
            with patch.object(sys, "argv", argv), patch.object(rr, "ROOT", root), patch.object(rr, "OUTPUT_DIR", root / "out"), \
                    patch.object(rr, "gh_text", return_value="{}"), patch.object(rr, "load_pr_diff", return_value="diff"), patch.object(rr, "run_llm", side_effect=fake):
                with open(os.devnull, "w") as sink, __import__("contextlib").redirect_stdout(sink):
                    rr.main()
        self.assertIn("[asked for by the person] Increase foliage density 2x", prompts[0])
        self.assertIn("Every file this job changed is a test file", prompts[0])
        self.assertIn("## Acceptance criteria", prompts[0])  # the reviewer's output format asks for each one

    def test_the_debugger_prompt_carries_the_block_on_top_of_the_brief(self):
        source = (ROOT / "orchestrator/scripts/debug_job.py").read_text()
        self.assertIn("brief += \"\\n\" + acceptance.block(job)", source)
        self.assertLess(source.index("brief += reference_context(job)"), source.index("acceptance.block(job)"))
        self.assertIn("{{brief}}", (ROOT / "orchestrator/prompts/debug_agent.md").read_text())


class CommitOnlyTheJobsFilesTests(unittest.TestCase):
    """A bare `git commit` takes everything staged; the job's commit must take only the job's files."""

    def repo(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        git = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True, text=True).stdout
        git("init", "-q", "-b", "main")
        (root / "a.py").write_text("one\n")
        (root / ".claude").mkdir()
        (root / ".claude" / "helper.cjs").write_text("one\n")
        git("add", ".")
        git("commit", "-q", "-m", "start")
        (root / "a.py").write_text("changed by the job\n")
        (root / ".claude" / "helper.cjs").write_text("changed by a tool\n")
        git("add", ".claude/helper.cjs")  # staged by something else before the job commits
        return tmp, root, git

    def test_the_commit_takes_the_jobs_files_and_leaves_other_staged_work_staged(self):
        tmp, root, git = self.repo()
        with tmp, patch.dict(os.environ, GIT_ENV), patch.object(worker, "ROOT", root):
            worker.commit_job_files(["a.py"], "feat: job")
            self.assertEqual(git("show", "--name-only", "--format=", "HEAD").split(), ["a.py"])
            self.assertEqual(git("diff", "--cached", "--name-only").split(), [".claude/helper.cjs"])

    def test_a_task_checkpoint_does_the_same(self):
        tmp, root, git = self.repo()
        job = {}
        with tmp, patch.dict(os.environ, GIT_ENV), patch.object(worker, "ROOT", root), patch.object(worker, "backup_push"):
            worker.checkpoint_task(job, 0, 1, "t", ["a.py"])
            self.assertEqual(git("show", "--name-only", "--format=", "HEAD").split(), ["a.py"])
            self.assertEqual(git("diff", "--cached", "--name-only").split(), [".claude/helper.cjs"])
            self.assertIn("0", job["task_commits"])

    def test_the_command_names_every_file_after_a_pathspec_separator(self):
        self.assertEqual(worker.commit_paths_command(["a b.py", "c.py"], "it's done"),
                         "git commit -m 'it'\"'\"'s done' -- 'a b.py' c.py")


class ReviewOutcomeGateTests(unittest.TestCase):
    def test_parse_review_outcome_distinguishes_approve_from_blockers_and_unmet_criteria(self):
        import review_ready

        clean = review_ready.parse_review_outcome(
            "## Blockers\nNone\n## Acceptance criteria\n- Criterion 1: **Met** (foo.py:10)\n## Verdict\nApprove. Looks good."
        )
        self.assertFalse(clean["needs_changes"])
        self.assertEqual(clean["verdict"], "approve")
        self.assertFalse(clean["has_blockers"])
        self.assertEqual(clean["unmet_criteria"], [])

        blocked = review_ready.parse_review_outcome(
            "## Blockers\n- Missing validation on empty input\n"
            "## Acceptance criteria\n- Criterion 1: **Not met** — validation missing\n"
            "## Verdict\nRequest changes. Fix the missing validation."
        )
        self.assertTrue(blocked["needs_changes"])
        self.assertEqual(blocked["verdict"], "request_changes")
        self.assertTrue(blocked["has_blockers"])
        self.assertEqual(len(blocked["unmet_criteria"]), 1)
        self.assertIn("Missing validation", blocked["feedback"])
        self.assertIn("Not met", blocked["feedback"])


if __name__ == "__main__":
    unittest.main()
