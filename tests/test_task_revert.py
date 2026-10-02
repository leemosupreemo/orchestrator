from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import task_revert


def git(root, *args):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(root)}
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True, env=env).stdout.strip()


class RevertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "a.txt").write_text("base\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        git(self.root, "checkout", "-q", "-b", "ai/job")
        self.commits = {}
        for n in (0, 1):
            (self.root / f"t{n}.txt").write_text(f"task {n}\n")
            git(self.root, "add", "-A")
            git(self.root, "commit", "-q", "-m", f"task {n}")
            self.commits[str(n)] = git(self.root, "rev-parse", "HEAD")
        self.job = {"branch": "ai/job", "status": "review-needed", "completed_task_indices": [0, 1], "task_commits": dict(self.commits)}

    def test_only_the_latest_task_is_offered(self):
        self.assertEqual(task_revert.latest_revertable(self.job), 1)
        self.assertIsNone(task_revert.latest_revertable({"completed_task_indices": [0]}))  # no commit recorded (an older job)

    def test_undoing_removes_that_tasks_files_and_keeps_the_earlier_one(self):
        task_revert.revert_latest(self.root, self.job, 1)
        self.assertFalse((self.root / "t1.txt").exists())
        self.assertTrue((self.root / "t0.txt").exists())
        self.assertEqual(self.job["completed_task_indices"], [0])
        self.assertNotIn("1", self.job["task_commits"])
        self.assertEqual(self.job["reverted_tasks"], [1])
        self.assertIn("Revert", git(self.root, "log", "-1", "--format=%s"))  # history is kept, not rewritten
        self.assertEqual(task_revert.latest_revertable(self.job), 0)  # and the one before it can now be undone

    def test_an_earlier_task_cannot_be_undone_out_of_order(self):
        with self.assertRaisesRegex(task_revert.RevertError, "most recently"):
            task_revert.revert_latest(self.root, self.job, 0)
        self.assertTrue((self.root / "t0.txt").exists())

    def test_refuses_on_the_wrong_branch_and_with_uncommitted_changes(self):
        git(self.root, "checkout", "-q", "main")
        with self.assertRaisesRegex(task_revert.RevertError, "Switch to ai/job"):
            task_revert.revert_latest(self.root, self.job, 1)
        git(self.root, "checkout", "-q", "ai/job")
        (self.root / "t0.txt").write_text("edited\n")
        with self.assertRaisesRegex(task_revert.RevertError, "uncommitted"):
            task_revert.revert_latest(self.root, self.job, 1)

    def test_a_conflict_changes_nothing(self):
        (self.root / "t1.txt").write_text("later edit\n")
        git(self.root, "commit", "-q", "-am", "later change to the same file")
        job = dict(self.job)
        before = git(self.root, "rev-parse", "HEAD")
        with self.assertRaisesRegex(task_revert.RevertError, "conflicts"):
            task_revert.revert_latest(self.root, job, 1)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        self.assertEqual(job["completed_task_indices"], [0, 1])

    def test_a_missing_commit_is_reported(self):
        self.job["task_commits"]["1"] = "0" * 40
        with self.assertRaisesRegex(task_revert.RevertError, "no longer"):
            task_revert.revert_latest(self.root, self.job, 1)


if __name__ == "__main__":
    unittest.main()
