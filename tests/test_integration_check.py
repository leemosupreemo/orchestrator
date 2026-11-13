from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import integration_check as ic

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True, env={**ENV, "HOME": str(root)}).stdout.strip()


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "config.txt").write_text("name=base\n")
        (self.root / "a.txt").write_text("a\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")

    def branch(self, name, files):
        git(self.root, "checkout", "-q", "-b", name, "main")
        for f, text in files.items():
            (self.root / f).write_text(text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", name)
        git(self.root, "checkout", "-q", "main")

    def verify(self, branches, build=None, test=None):
        return ic.verify(self.root, "main", branches, build, test)

    def test_two_branches_that_merge_and_pass_together(self):
        self.branch("j1", {"one.txt": "1\n"})
        self.branch("j2", {"two.txt": "2\n"})
        r = self.verify(["j1", "j2"], test="test -f one.txt && test -f two.txt")
        self.assertEqual((r["status"], r["merged"], r["conflicts"]), ("pass", ["j1", "j2"], []))
        self.assertTrue(r["test"]["ok"])

    def test_a_conflict_names_the_branch_and_files_and_skips_the_build(self):
        self.branch("j1", {"config.txt": "name=one\n"})
        self.branch("j2", {"config.txt": "name=two\n"})
        r = self.verify(["j1", "j2"], build="exit 9")
        self.assertEqual(r["status"], "conflict")
        self.assertEqual(r["conflicts"], [{"branch": "j2", "files": ["config.txt"]}])
        self.assertFalse(r["build"]["ran"])

    def test_two_changes_that_each_pass_alone_but_fail_together_are_caught(self):
        # j1 renames the function's argument use; j2 adds a caller of the old form: no textual conflict, broken result.
        self.branch("j1", {"lib.txt": "api=v2\n"})
        self.branch("j2", {"user.txt": "needs=v1\n"})
        check = "grep -q 'api=v2' lib.txt && ! grep -q 'needs=v1' user.txt"
        self.assertEqual(self.verify(["j1"], test=check)["status"], "nothing")  # alone: nothing to combine
        r = self.verify(["j1", "j2"], test=check)
        self.assertEqual(r["status"], "test-failed")
        self.assertEqual(r["merged"], ["j1", "j2"])

    def test_the_users_checkout_is_never_touched_and_the_scratch_one_is_removed(self):
        self.branch("j1", {"one.txt": "1\n"})
        self.branch("j2", {"two.txt": "2\n"})
        (self.root / "wip.txt").write_text("my uncommitted work\n")
        self.verify(["j1", "j2"], test="true")
        self.assertEqual(git(self.root, "branch", "--show-current"), "main")
        self.assertEqual((self.root / "wip.txt").read_text(), "my uncommitted work\n")
        self.assertFalse((self.root / "one.txt").exists())
        self.assertEqual(git(self.root, "worktree", "list").count("\n"), 0)

    def test_missing_branches_are_reported_and_fewer_than_two_means_nothing_to_check(self):
        self.branch("j1", {"one.txt": "1\n"})
        r = self.verify(["j1", "gone"])
        self.assertEqual((r["status"], r["missing"]), ("nothing", ["gone"]))

    def test_a_hung_command_times_out_instead_of_hanging(self):
        self.branch("j1", {"one.txt": "1\n"})
        self.branch("j2", {"two.txt": "2\n"})
        r = ic.verify(self.root, "main", ["j1", "j2"], None, "sleep 5", timeout=1)
        self.assertEqual(r["status"], "test-failed")
        self.assertTrue(r["test"]["timed_out"])

    def test_staleness_follows_the_base_and_the_branch_heads(self):
        self.branch("j1", {"one.txt": "1\n"})
        self.branch("j2", {"two.txt": "2\n"})
        r = self.verify(["j1", "j2"])
        base = git(self.root, "rev-parse", "main")
        self.assertFalse(ic.is_stale(r, base, ic.branch_heads(self.root, ["j1", "j2"])))
        git(self.root, "checkout", "-q", "j1")
        (self.root / "more.txt").write_text("x\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "more")
        git(self.root, "checkout", "-q", "main")
        self.assertTrue(ic.is_stale(r, base, ic.branch_heads(self.root, ["j1", "j2"])))
        self.assertFalse(ic.is_stale(None, base, {}))

    def test_results_round_trip(self):
        rt = self.root / ".orchestrator"
        ic.save_result(rt, "f1", {"status": "pass"})
        self.assertEqual(ic.load_result(rt, "f1"), {"status": "pass"})
        self.assertIsNone(ic.load_result(rt, "nope"))


if __name__ == "__main__":
    unittest.main()
