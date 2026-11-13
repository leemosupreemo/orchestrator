"""Repository diagnostics and repairs use temporary Git repositories and simulated GitHub responses."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import repository_health as health


class RepositoryHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.runtime = self.root / ".orchestrator"
        self.runtime.mkdir()
        self.git("init", "-qb", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.root / "app.txt").write_text("original\n")
        (self.runtime / "project.json").write_text('{"project_name":"Test"}\n')
        (self.root / ".gitignore").write_text(".orchestrator/state/\n.orchestrator/jobs/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial")
        self.labels = {"job:bug": {"name": "job:bug", "color": "abcdef"}, "custom": {"name": "custom"}}
        self.permissions = {"push": True, "pull": True}

    def git(self, *args, root=None):
        return subprocess.run(["git", *args], cwd=root or self.root, check=True, capture_output=True, text=True).stdout.strip()

    def fake_api(self, root, endpoint, *, method="GET", values=None, paginate=False):
        if endpoint.endswith("/labels"):
            if method == "POST":
                self.assertNotIn(values["name"], self.labels)
                self.labels[values["name"]] = values.copy()
                return values
            return list(self.labels.values())
        return {"permissions": self.permissions, "has_issues": True, "archived": False}

    def test_diagnostics_distinguish_read_access_from_unverified_write_and_merge(self):
        remote = Path(self.tmp.name) / "remote.git"
        self.git("init", "-q", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        # Public GitHub metadata is simulated; Git read access is exercised against a real remote.
        with patch.object(health, "gh_api", side_effect=self.fake_api):
            result = health.check_connection(self.root, self.runtime, "https://github.com/team/app.git", "github")
        checks = {c["id"]: c for c in result["checks"]}
        self.assertEqual(checks["read"]["status"], "ok")
        self.assertEqual(checks["publish"]["status"], "unknown")
        self.assertEqual(checks["issues"]["status"], "unknown")
        self.assertEqual(checks["merge"]["status"], "per_job")
        self.assertTrue(result["checked_at"])
        self.assertIn("status:planned", [l["name"] for l in result["labels"] if not l["present"]])
        self.assertIsNone(health.saved_check(self.runtime, "https://github.com/other/app.git", "github"))

    def test_label_repair_creates_only_missing_managed_labels(self):
        with patch.object(health, "gh_api", side_effect=self.fake_api):
            result = health.repair_labels(self.root, self.runtime, "git@github.com:team/app.git", "github")
        self.assertEqual(self.labels["job:bug"]["color"], "abcdef")
        self.assertIn("custom", self.labels)
        self.assertEqual(len(result["created"]), len(health.LABELS) - 1)
        self.assertTrue(all(l["present"] for l in result["labels"]))

    def test_github_failures_are_reported_without_claiming_connection_is_healthy(self):
        self.git("remote", "add", "origin", str(Path(self.tmp.name) / "missing.git"))
        with patch.object(health, "gh_api", side_effect=health.RepositoryError("Sign in to GitHub")):
            result = health.check_connection(self.root, self.runtime, "https://github.com/team/app.git", "github")
        checks = {c["id"]: c for c in result["checks"]}
        self.assertEqual(checks["read"]["status"], "blocked")
        self.assertEqual(checks["issues"]["status"], "blocked")
        self.assertEqual(result["labels"], [])

    def test_read_only_github_permissions_are_not_reported_as_publish_access(self):
        self.permissions["push"] = False
        with patch.object(health, "gh_api", side_effect=self.fake_api):
            result = health.check_connection(self.root, self.runtime, "https://github.com/team/app.git", "github")
        checks = {c["id"]: c for c in result["checks"]}
        self.assertEqual(checks["publish"]["status"], "blocked")

    def test_label_repair_rejects_other_hosts_and_plain_git_mode(self):
        for origin, mode in [("git@gitlab.com:team/app.git", "git"), ("git@github.com:team/app.git", "git")]:
            with self.subTest(origin=origin, mode=mode), self.assertRaises(health.RepositoryError):
                health.repair_labels(self.root, self.runtime, origin, mode)

    def test_configuration_status_compares_with_last_fetched_base(self):
        remote = Path(self.tmp.name) / "remote.git"
        self.git("init", "-q", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-qu", "origin", "main")
        self.assertEqual(health.configuration_status(self.root, self.runtime, "main")["status"], "matches")
        (self.runtime / "project.json").write_text('{"project_name":"Changed"}')
        self.assertEqual(health.configuration_status(self.root, self.runtime, "main")["status"], "different")
        self.git("remote", "remove", "origin")
        self.assertEqual(health.configuration_status(self.root, self.runtime, "main")["status"], "unknown")

    def make_publish_job(self, **fields):
        remote = Path(self.tmp.name) / "remote.git"
        self.git("init", "-q", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("branch", "ai/issue-7-fix")
        jobs = self.runtime / "jobs"
        jobs.mkdir()
        path = jobs / "job-7.json"
        path.write_text(json.dumps({"job_id": "job-7", "title": "Fix login", "branch": "ai/issue-7-fix",
                                    "status": "review-needed", "code_host": "git", **fields}))
        return path, remote

    def test_republish_pushes_only_the_jobs_branch_without_switching_or_rebuilding(self):
        path, remote = self.make_publish_job()
        before = self.git("rev-parse", "HEAD")
        result = health.republish(self.root, self.runtime, "local-remote", "git", path, "main")
        self.assertEqual(self.git("rev-parse", "refs/heads/ai/issue-7-fix", root=remote), before)
        self.assertEqual(self.git("branch", "--show-current"), "main")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        saved = json.loads(path.read_text())
        self.assertEqual(saved["published_commit"], before)
        self.assertTrue(saved["last_published_at"])
        self.assertIn("published", result["detail"].lower())

    def test_republish_preserves_job_changes_made_during_network_request(self):
        path, _ = self.make_publish_job()
        original = health.run
        def command(root, argv, **kwargs):
            result = original(root, argv, **kwargs)
            if argv[:2] == ["git", "push"]:
                saved = json.loads(path.read_text())
                saved["title"] = "Updated during publishing"
                path.write_text(json.dumps(saved))
            return result
        with patch.object(health, "run", side_effect=command):
            health.republish(self.root, self.runtime, "local-remote", "git", path, "main")
        self.assertEqual(json.loads(path.read_text())["title"], "Updated during publishing")

    def test_republish_reuses_existing_pull_request(self):
        path, _ = self.make_publish_job(code_host="github", issue_number=7)
        original = health.run
        def command(root, argv, **kwargs):
            if argv[:3] == ["gh", "pr", "list"]:
                return json.dumps([{"number": 9, "url": "https://github.com/team/app/pull/9"}])
            if argv[:3] == ["gh", "pr", "create"]:
                self.fail("An existing PR must not be duplicated")
            return original(root, argv, **kwargs)
        with patch.object(health, "run", side_effect=command):
            health.republish(self.root, self.runtime, "git@github.com:team/app.git", "github", path, "main")
        self.assertEqual(json.loads(path.read_text())["pr_number"], 9)

    def test_failed_publish_is_recorded_and_can_be_retried_without_losing_other_errors(self):
        path, remote = self.make_publish_job(last_error="An unrelated review concern")
        self.git("remote", "set-url", "origin", str(Path(self.tmp.name) / "missing.git"))
        with self.assertRaises(health.RepositoryError):
            health.republish(self.root, self.runtime, "local-remote", "git", path, "main")
        self.assertTrue(json.loads(path.read_text())["publish_error"])
        self.git("remote", "set-url", "origin", str(remote))
        health.republish(self.root, self.runtime, "local-remote", "git", path, "main")
        saved = json.loads(path.read_text())
        self.assertFalse(saved["publish_error"])
        self.assertEqual(saved["last_error"], "An unrelated review concern")

    def test_publishing_summary_flags_missing_pr_only_after_review_is_ready(self):
        path, _ = self.make_publish_job(code_host="github", issue_number=7)
        result = health.publishing_jobs(self.root, self.runtime, "git@github.com:team/app.git", "github")
        self.assertEqual(result["problems"][0]["id"], "job-7")
        saved = json.loads(path.read_text())
        saved["status"] = "executing"
        path.write_text(json.dumps(saved))
        self.assertEqual(health.publishing_jobs(self.root, self.runtime, "git@github.com:team/app.git", "github")["problems"], [])

    def test_current_base_branch_jobs_do_not_offer_retry_with_custom_base(self):
        path, _ = self.make_publish_job(branch="main", code_host="github", base_branch="develop")
        result = health.publishing_jobs(self.root, self.runtime, "git@github.com:team/app.git", "github", "develop")
        self.assertEqual(result["problems"], [])
        with self.assertRaises(health.RepositoryError):
            health.republish(self.root, self.runtime, "git@github.com:team/app.git", "github", path, "develop")

    def test_republish_rejects_base_branches_and_unfinished_jobs(self):
        path, _ = self.make_publish_job(branch="main")
        with self.assertRaises(health.RepositoryError):
            health.republish(self.root, self.runtime, "local-remote", "git", path, "main")
        saved = json.loads(path.read_text())
        saved.update(branch="ai/issue-7-fix", status="executing")
        path.write_text(json.dumps(saved))
        with self.assertRaises(health.RepositoryError):
            health.republish(self.root, self.runtime, "local-remote", "git", path, "main")
