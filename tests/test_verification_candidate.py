import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import verification_candidate as candidate


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


class CandidateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.remote = self.root / "remote.git"
        self.owner = self.root / "owner"
        self.helper = self.root / "helper"
        git(self.root, "init", "--bare", str(self.remote))
        git(self.root, "clone", str(self.remote), str(self.owner))
        git(self.owner, "config", "user.name", "Test")
        git(self.owner, "config", "user.email", "test@example.com")
        for path in ("changed", "deleted", "unrelated"):
            (self.owner / path).write_text(f"base {path}")
        git(self.owner, "add", ".")
        git(self.owner, "commit", "-m", "Base")
        git(self.owner, "push", "origin", "HEAD")
        git(self.root, "clone", str(self.remote), str(self.helper))
        self.base = git(self.owner, "rev-parse", "HEAD")

    def test_publish_exact_job_tree_and_keep_owner_index(self):
        (self.owner / "changed").write_text("changed by job")
        (self.owner / "literal[1]").write_text("added by job")
        (self.owner / "deleted").unlink()
        (self.owner / "unrelated").write_text("staged by user")
        git(self.owner, "add", "unrelated")
        index = git(self.owner, "write-tree")
        snapshot = candidate.create(self.owner, self.base, ["changed", "literal[1]", "deleted"])
        self.assertEqual(git(self.owner, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(self.owner, "write-tree"), index)

        with candidate.publish(self.owner, "origin", snapshot) as ref:
            worktree = self.root / "attempt"
            with candidate.checkout(self.helper, "origin", ref, snapshot, worktree):
                self.assertEqual(git(worktree, "rev-parse", "HEAD^{tree}"), snapshot.tree)
                self.assertEqual((worktree / "changed").read_text(), "changed by job")
                self.assertEqual((worktree / "literal[1]").read_text(), "added by job")
                self.assertEqual((worktree / "unrelated").read_text(), "base unrelated")
                self.assertFalse((worktree / "deleted").exists())
            self.assertFalse(worktree.exists())
        self.assertEqual(git(self.owner, "ls-remote", "origin", ref), "")

    def test_moved_ref_cannot_be_used_as_the_candidate(self):
        (self.owner / "changed").write_text("job change")
        snapshot = candidate.create(self.owner, self.base, ["changed"])
        other_ref = "refs/orchestrator/verification/" + "1" * 32
        git(self.owner, "push", "origin", f"{self.base}:{other_ref}")
        worktree = self.root / "attempt"
        with self.assertRaisesRegex(candidate.CandidateError, "revision does not match"):
            with candidate.checkout(self.helper, "origin", other_ref, snapshot, worktree):
                self.fail("A different commit was accepted")
        self.assertFalse(worktree.exists())

    def test_reuses_clean_commit_and_rejects_unsafe_paths(self):
        self.assertEqual(candidate.create(self.owner, self.base, []).commit, self.base)
        for path in ("../outside", ".", "/absolute", "missing"):
            with self.subTest(path=path), self.assertRaises(candidate.CandidateError):
                candidate.create(self.owner, self.base, [path])

    def test_missing_ref_leaves_no_temporary_checkout_or_local_ref(self):
        snapshot = candidate.create(self.owner, self.base, [])
        missing_ref = candidate.REF_PREFIX + "2" * 32
        worktree = self.root / "attempt"
        with self.assertRaises(candidate.CandidateError):
            with candidate.checkout(self.helper, "origin", missing_ref, snapshot, worktree):
                self.fail("A missing ref was accepted")
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.helper, "for-each-ref", candidate.REF_PREFIX), "")

    def test_unmaterialized_lfs_input_rejects_checkout_and_cleans_up(self):
        (self.owner / ".gitattributes").write_text("asset filter=lfs\n")
        (self.owner / "asset").write_text("version https://git-lfs.github.com/spec/v1\noid sha256:" + "0" * 64 + "\nsize 10\n")
        snapshot = candidate.create(self.owner, self.base, [".gitattributes", "asset"])
        worktree = self.root / "attempt"
        with candidate.publish(self.owner, "origin", snapshot) as ref:
            with self.assertRaises(candidate.CandidateError):
                with candidate.checkout(self.helper, "origin", ref, snapshot, worktree):
                    self.fail("An unmaterialized asset was accepted")
        self.assertFalse(worktree.exists())
        self.assertEqual(git(self.helper, "for-each-ref", candidate.REF_PREFIX), "")
