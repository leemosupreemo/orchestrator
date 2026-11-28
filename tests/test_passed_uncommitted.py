"""Work that was done and passed its tests, where only the commit failed (a git lock), says so and is committed on Resume,
instead of reading "Partially complete · Next: debug failing tests" and building again."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

from orchestrator.web import server as ui

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator" / "scripts"))


class PassedUncommittedTests(unittest.TestCase):
    def test_the_job_says_its_tests_passed_and_resume_commits_it(self):
        job = {"status": "human-needed", "last_error": "Command 'git add a.cs' returned non-zero exit status 128.",
               "passed_uncommitted": {"files": ["a.cs", "b.cs"]}, "ai_modified_files": ["a.cs", "b.cs"] * 5,
               "debug_history": [{"result": {"build_ok": True, "tests_ok": False}}]}
        state = ui.job_state(job)
        self.assertEqual((state["label"], state["next"]["action"], state["next"]["label"]),
                         ("Tests passed, not committed", "resume", "Commit the work"))
        self.assertIn("2 files changed", state["reason"])
        self.assertIn("nothing is built again", state["reason"])

    def test_the_worker_records_the_pass_before_committing_and_resume_skips_the_builder(self):
        source = (ROOT / "orchestrator" / "scripts" / "worker_run.py").read_text(encoding="utf-8")
        marked = source.index('job["passed_uncommitted"] = {"files"')
        self.assertLess(marked, source.index("commit_job_files(ai_modified + ai_untracked, commit_msg)"))
        self.assertIn('committing_only = bool(resume and isinstance(job.get("passed_uncommitted"), dict))', source)
        self.assertIn("        if committing_only:\n            pass\n        elif job.get(\"type\") == \"feature-plan\"", source)
        self.assertIn('if job.pop("passed_uncommitted", None) is not None:', source)


    def test_a_job_ready_to_merge_offers_no_fix_for_an_old_error(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        # ready to merge, the status box asks for changes; "Run fix" only comes after it, for a job with no merge waiting
        self.assertIn(': ["merge", "complete"].includes(nextAction) ? { mode: "iterate"', app)
        self.assertLess(app.index('{ mode: "iterate"'), app.index('{ mode: "debug"'))
        source = (ROOT / "orchestrator" / "scripts" / "worker_run.py").read_text(encoding="utf-8")
        self.assertIn('        # SUCCESS POINT\n        job.pop("last_error", None)', source)


class CommitOnlyResumeTests(unittest.TestCase):
    """Resume on "Tests passed, not committed" commits the saved files, and only those, without running the builder.
    Runs in a throwaway repository with a local bare remote, never this checkout."""

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()

    def test_resume_commits_the_saved_files_without_building_again(self):
        import worker_run as worker
        import common
        tmp = Path(tempfile.mkdtemp())
        remote, repo = tmp / "remote.git", tmp / "repo"
        self.git(tmp, "init", "-q", "--bare", str(remote))
        repo.mkdir()
        self.git(repo, "init", "-q", "-b", "job/grass")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "user.name", "Test")
        self.git(repo, "remote", "add", "origin", str(remote))
        for name in ("Grass.cs", "helper.cjs"):
            (repo / name).write_text("old")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-q", "-m", "first")
        (repo / "Grass.cs").write_text("new grass")         # the job's work, tested and passing
        (repo / "helper.cjs").write_text("a tool's update")  # changed meanwhile by something else: not the job's
        (repo / "test_grass.py").write_text("ok")            # the test the job wrote
        jobs = tmp / "jobs"
        jobs.mkdir()
        job_path = jobs / "j1.json"
        job_path.write_text(json.dumps({
            "job_id": "j1", "title": "Grass", "type": "quick", "status": "human-needed", "branch": "job/grass",
            "base_branch": "main", "branch_mode": "new", "last_error": "Command 'git add' returned non-zero exit status 128.",
            "passed_uncommitted": {"files": ["Grass.cs", "test_grass.py"]}}))

        class Stop(BaseException):  # past the commit: stop before the pull request step
            pass

        def phase(name, *a, **k):
            if name == "pull_request":
                raise Stop()

        config = copy.copy(worker.PROJECT_CONFIG)  # the real config, pointed at the throwaway repository
        for key, value in (("base_branch", "main"), ("git_remote", str(remote)), ("firebase_distribution", False), ("delivery_platform", "ios")):
            object.__setattr__(config, key, value)
        with mock.patch.object(worker, "ROOT", repo), mock.patch.object(common, "ROOT", repo), \
             mock.patch.object(worker, "PROJECT_CONFIG", config), mock.patch.object(worker, "OUTPUT_DIR", tmp / "out"), \
             mock.patch.object(worker, "prepare_git_branch", side_effect=AssertionError("a checkout would throw the work away")), \
             mock.patch.object(worker, "run_builder", side_effect=AssertionError("the builder ran again")), \
             mock.patch.object(worker, "update_issue_status"), mock.patch.object(worker, "gh_comment"), \
             mock.patch.object(worker, "send_notifications"), mock.patch.object(worker, "refresh_yolo", return_value=False), \
             mock.patch.object(worker, "StatusBar"), mock.patch.object(worker, "print_phase", side_effect=phase), \
             mock.patch("orchestrator.analytics.track_job_run"):
            with self.assertRaises(Stop):
                worker.execute_job(job_path, resume=True)

        committed = self.git(repo, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(sorted(committed), ["Grass.cs", "test_grass.py"])
        self.assertIn("helper.cjs", self.git(repo, "status", "--porcelain"))  # left alone
        job = json.loads(job_path.read_text())
        self.assertNotIn("passed_uncommitted", job)
        self.assertEqual(self.git(remote, "rev-parse", "job/grass"), self.git(repo, "rev-parse", "HEAD"))  # and backed up

    def test_on_another_branch_nothing_is_touched_and_it_says_how_to_fix_it(self):
        import worker_run as worker
        with mock.patch.object(worker, "current_git_branch", return_value="main"):
            with self.assertRaises(RuntimeError) as err:
                worker.commit_pending_branch({"branch": "job/grass", "base_branch": "main"})
        self.assertIn("Switch back to 'job/grass'", str(err.exception))
        with mock.patch.object(worker, "current_git_branch", return_value="job/grass"):
            self.assertEqual(worker.commit_pending_branch({"branch": "job/grass", "base_branch": "main"}), ("job/grass", "main"))

if __name__ == "__main__":
    unittest.main()
