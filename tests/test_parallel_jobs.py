"""Jobs at once (parallel_jobs.py): when a job would wait for the project folder, it builds in its own worktree instead, unless
the project allows one job at a time, the job works on the current branch, or it changes the same files as a running job.
Git runs only in throwaway repositories, and worktrees go to a temporary user folder."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import parallel_jobs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator" / "scripts"))

HOLDER = {"job": "a", "title": "Grass"}
RUNNING = ("a", {"title": "Grass", "status": "executing", "plan": {"likely_files": ["World/Grass.cs"]}})


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class DecideTests(unittest.TestCase):
    def decide(self, job, project=None, holder=HOLDER, jobs=(RUNNING,)):
        return parallel_jobs.decide(Path("/p"), "b", job, list(jobs), holder, {"parallel_jobs": 2} if project is None else project)

    def test_a_job_that_would_wait_builds_alongside_in_its_own_checkout(self):
        self.assertEqual(self.decide({"branch_mode": "new", "plan": {"likely_files": ["UI/Menu.cs"]}}), (True, ""))

    def test_one_at_a_time_or_a_free_folder_works_in_the_project_folder(self):
        self.assertEqual(self.decide({"branch_mode": "new"}, project={}), (False, ""))
        self.assertEqual(self.decide({"branch_mode": "new"}, holder=None), (False, ""))

    def test_the_guards_say_why_it_waits(self):
        self.assertIn("current branch", self.decide({"branch_mode": "current"})[1])
        self.assertIn("same files as “Grass”", self.decide({"branch_mode": "new", "plan": {"likely_files": ["World/Grass.cs"]}})[1])
        self.assertIn("uncommitted in the project folder", self.decide({"branch_mode": "new", "passed_uncommitted": {"files": ["x"]}})[1])
        busy = [RUNNING, ("c", {"worktree": "/w/c", "worker_pid": os.getpid(), "title": "Other"})]
        self.assertIn("2 jobs are already running", self.decide({"branch_mode": "new"}, jobs=busy)[1])

    def test_a_job_with_a_checkout_always_carries_on_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.decide({"branch_mode": "current", "worktree": tmp}, project={}), (True, ""))

    def test_the_limit_is_kept_between_one_and_four(self):
        self.assertEqual([parallel_jobs.limit(p) for p in ({}, {"parallel_jobs": 9}, {"parallel_jobs": "x"}, {"parallel_jobs": 3})], [1, 4, 1, 3])


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.state = mock.patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(tmp / "state")})
        self.state.start()
        self.addCleanup(self.state.stop)
        self.main = tmp / "project"
        self.main.mkdir()
        git(self.main, "init", "-q", "-b", "main")
        git(self.main, "config", "user.email", "test@example.invalid")
        git(self.main, "config", "user.name", "Test")
        (self.main / "a.txt").write_text("a")
        git(self.main, "add", ".")
        git(self.main, "commit", "-q", "-m", "first")

    def test_the_checkout_lives_outside_the_project_and_is_reused(self):
        path = parallel_jobs.create(self.main, "job-b")
        self.assertFalse(str(path).startswith(str(self.main)))  # test discovery, Xcode and Unity never see it
        self.assertTrue((path / "a.txt").exists())
        self.assertEqual(parallel_jobs.create(self.main, "job-b"), path)
        self.assertEqual(git(self.main, "branch", "--show-current"), "main")  # the project folder is untouched

    def test_a_new_branch_starts_from_the_base_without_checking_the_base_out(self):
        import worker_run
        path = parallel_jobs.create(self.main, "job-b")
        config = mock.Mock(git_remote="", base_branch="main")
        with mock.patch.object(worker_run, "ROOT", path), mock.patch.object(worker_run, "PROJECT_CONFIG", config):
            job = {}
            self.assertEqual(worker_run.prepare_worktree_branch(job, "ai/job-b", "main"), ("ai/job-b", "main"))
        self.assertEqual(git(path, "branch", "--show-current"), "ai/job-b")
        self.assertEqual(git(self.main, "branch", "--show-current"), "main")

    def test_finishing_removes_a_clean_checkout_but_never_uncommitted_work(self):
        path = parallel_jobs.create(self.main, "job-b")
        (path / "a.txt").write_text("work in progress")
        self.assertFalse(parallel_jobs.remove(self.main, {"worktree": str(path)}))
        self.assertTrue(path.exists())
        self.assertTrue(parallel_jobs.remove(self.main, {"worktree": str(path)}, force=True))  # discarding: thrown away
        self.assertFalse(path.exists())

    def test_the_worker_runs_in_the_checkout_with_the_projects_records(self):
        env = parallel_jobs.worker_env(self.main, Path("/w/b"), self.main / ".orchestrator")
        self.assertEqual((env["ORCHESTRATOR_PROJECT_ROOT"], env["ORCHESTRATOR_RUNTIME_DIR"], env[parallel_jobs.ENV_MAIN]),
                         ("/w/b", str(self.main / ".orchestrator"), str(self.main.resolve())))


class SettingTests(unittest.TestCase):
    def test_the_setting_is_saved_and_checked(self):
        from orchestrator.web import server as ui
        root = Path(tempfile.mkdtemp())
        (root / ".orchestrator").mkdir()
        (root / ".orchestrator" / "project.json").write_text("{}")
        self.assertEqual(ui.config_update(root, "parallel-jobs", {"jobs": 3}), {"saved": True, "parallel_jobs": 3})
        self.assertEqual(json.loads((root / ".orchestrator" / "project.json").read_text())["parallel_jobs"], 3)
        for bad in (0, 5, "x"):
            with self.assertRaises(ui.UIError):
                ui.config_update(root, "parallel-jobs", {"jobs": bad})

    def test_the_git_page_and_job_page_show_it(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('mutate(event.submitter, "config/parallel-jobs"', app)
        self.assertIn("Builds alongside other jobs in its own checkout", app)
        actions = (ROOT / "orchestrator" / "scripts" / "job_actions.py").read_text(encoding="utf-8")
        self.assertIn("drop_worktree(job, force=True)  # discarding", actions)


if __name__ == "__main__":
    unittest.main()
