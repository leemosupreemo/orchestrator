"""The one place worktrees are made (worktrees.py), used by the integration check, both previews and jobs at once.
Git runs only in throwaway repositories."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import worktrees

ROOT = Path(__file__).resolve().parents[1]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        (self.repo / "a.txt").write_text("a")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "first")
        git(self.repo, "branch", "feature")

    def test_a_scratch_checkout_is_removed_even_when_the_work_fails(self):
        with self.assertRaises(ValueError):
            with worktrees.scratch(self.repo, "main") as work:
                seen = work
                self.assertTrue((work / "a.txt").exists())
                raise ValueError("the build failed")
        self.assertFalse(seen.exists())
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)

    def test_a_bad_ref_says_so(self):
        with self.assertRaises(worktrees.WorktreeError):
            with worktrees.scratch(self.repo, "no-such-branch"):
                pass

    def test_a_kept_checkout_is_reused_and_a_branch_checked_out_elsewhere_still_works(self):
        kept = self.tmp / "kept"
        self.assertEqual(worktrees.ensure(self.repo, kept, "main"), kept)  # main is checked out in the repo: detached, so fine
        (kept / "note.txt").write_text("work")
        self.assertEqual(worktrees.ensure(self.repo, kept, "main"), kept)
        self.assertTrue((kept / "note.txt").exists())
        self.assertFalse(worktrees.remove(self.repo, kept))  # uncommitted work: kept
        self.assertTrue(worktrees.remove(self.repo, kept, force=True))
        self.assertFalse(kept.exists())

    def test_where_a_branch_is_checked_out(self):
        self.assertEqual(worktrees.checked_out_in(self.repo, "main").resolve(), self.repo.resolve())
        self.assertIsNone(worktrees.checked_out_in(self.repo, "feature"))

    def test_nothing_else_makes_its_own_worktrees(self):
        # verification_candidate.py is the one exception: it checks the exact commit and tree it was sent, and refuses
        # checkouts that need submodules or Git LFS files, around its own `worktree add`. The shared helper does neither.
        allowed = {"worktrees.py", "verification_candidate.py"}
        for path in (ROOT / "orchestrator").rglob("*.py"):
            if path.name not in allowed and "__pycache__" not in path.parts:
                self.assertNotIn('"worktree", "add"', path.read_text(encoding="utf-8"), path)


if __name__ == "__main__":
    unittest.main()
