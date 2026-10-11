"""Release tagging against disposable repositories and an offline bare remote."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import releases


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.git("commit", "--allow-empty", "-qm", "First")
        self.commit = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def test_suggests_first_version_and_base_commit(self):
        options = releases.options(self.root, "main")
        self.assertEqual(options["suggested_tag"], "v0.1.0")
        self.assertEqual(options["commit"], self.commit)
        self.assertFalse(options["can_push"])

    def test_tags_base_without_switching_branch_or_including_dirty_files(self):
        self.git("checkout", "-qb", "feature")
        self.git("commit", "--allow-empty", "-qm", "Unreleased feature")
        (self.root / "uncommitted.txt").write_text("not released")
        result = releases.create(self.root, "main", "v0.1.0", self.commit, push=False)
        self.assertTrue(result["created"])
        self.assertEqual(self.git("rev-parse", "v0.1.0"), self.commit)
        self.assertEqual(self.git("branch", "--show-current"), "feature")
        self.assertTrue((self.root / "uncommitted.txt").exists())

    def test_publishes_only_requested_tag(self):
        remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        self.git("remote", "add", "origin", str(remote))
        self.git("tag", "-a", "private-tag", "-m", "Private release")
        self.git("config", "push.followTags", "true")
        result = releases.create(self.root, "main", "v0.1.0", self.commit, push=True)
        self.assertTrue(result["pushed"])
        tags = subprocess.check_output(["git", "--git-dir", str(remote), "tag"], text=True).strip()
        self.assertEqual(tags, "v0.1.0")

    def test_changed_base_and_invalid_tag_create_nothing(self):
        self.git("commit", "--allow-empty", "-qm", "New base")
        with self.assertRaises(releases.ReleaseError):
            releases.create(self.root, "main", "v0.1.0", self.commit, push=False)
        for tag in ("--force", "bad tag", "../bad", "v1; echo nope"):
            with self.subTest(tag=tag), self.assertRaises(releases.ReleaseError):
                releases.create(self.root, "main", tag, self.git("rev-parse", "main"), push=False)
        self.assertEqual(self.git("tag"), "")

    def test_existing_conflicting_tag_is_never_replaced(self):
        self.git("tag", "v0.1.0")
        self.git("commit", "--allow-empty", "-qm", "New base")
        with self.assertRaises(releases.ReleaseError):
            releases.create(self.root, "main", "v0.1.0", self.git("rev-parse", "main"), push=False)
        self.assertEqual(self.git("rev-parse", "v0.1.0"), self.commit)

    def test_failed_push_preserves_tag_and_can_retry(self):
        self.git("remote", "add", "origin", str(Path(self.tmp.name) / "missing.git"))
        result = releases.create(self.root, "main", "v0.1.0", self.commit, push=True)
        self.assertFalse(result["pushed"])
        self.assertTrue(result["warning"])
        self.assertEqual(self.git("rev-parse", "v0.1.0"), self.commit)
        retry = releases.create(self.root, "main", "v0.1.0", self.commit, push=False)
        self.assertFalse(retry["created"])

    def test_suggestion_skips_existing_versions(self):
        self.git("tag", "v0.1.0")
        self.git("tag", "v0.1.1")
        self.assertEqual(releases.options(self.root, "main")["suggested_tag"], "v0.1.2")
