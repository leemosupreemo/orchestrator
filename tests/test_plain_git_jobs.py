"""Jobs without GitHub (code_host "git"): local references, ai/job- branches, pushing for review, reviewing a branch's
own diff and merging on this computer. Uses real temporary repositories, never GitHub."""
from __future__ import annotations

import dataclasses
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"
for path in (PACKAGE_ROOT, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import common  # noqa: E402
import new_job  # noqa: E402
import open_or_update_pr  # noqa: E402
import review_ready  # noqa: E402
import worker_run  # noqa: E402
from orchestrator.web import server as ui  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


class Repo:
    """A project repository with a main branch and a job branch that changes one file."""

    def __init__(self, folder: str):
        self.root = Path(folder) / "project"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "Test")
        (self.root / "app.txt").write_text("one\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "start")

    def job_branch(self, name="ai/job-abc123-fix-it", content="two\n"):
        git(self.root, "checkout", "-q", "-b", name)
        (self.root / "app.txt").write_text(content)
        git(self.root, "commit", "-qam", "job work")
        git(self.root, "checkout", "-q", "main")
        return name


def checkout_state() -> tuple[str, str, str]:
    """The real checkout these tests run from: branch, commit and uncommitted changes. Tests must never touch it."""
    return (git(PACKAGE_ROOT, "rev-parse", "--abbrev-ref", "HEAD"), git(PACKAGE_ROOT, "rev-parse", "HEAD"),
            git(PACKAGE_ROOT, "status", "--porcelain", "--untracked-files=no"))


class PlainGitJobTests(unittest.TestCase):
    def setUp(self):
        self.real_checkout = checkout_state()
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-git-")
        self.repo = Repo(self.tmp.name)
        # Each script imported ROOT by value, so point every one at the test repository.
        self.root_patches = [patch.object(module, "ROOT", self.repo.root) for module in (common, open_or_update_pr, review_ready)]
        for root_patch in self.root_patches:
            root_patch.start()

    def tearDown(self):
        for root_patch in self.root_patches:
            root_patch.stop()
        self.tmp.cleanup()
        self.assertEqual(checkout_state(), self.real_checkout, "a test changed the real checkout")

    def config(self, **values):
        return dataclasses.replace(common.PROJECT_CONFIG, root=self.repo.root, base_branch="main", **values)

    def test_a_plain_git_job_gets_a_local_reference_and_no_issue(self):
        with patch.object(new_job, "PROJECT_CONFIG", self.config(code_host="git")), \
                patch.object(new_job, "create_issue", side_effect=AssertionError("no GitHub issue in plain git")):
            number, ref = new_job.file_job("Fix it", "body", ["job:bug"])
        self.assertIsNone(number)
        self.assertRegex(ref, r"^[0-9a-f]{6}$")
        with patch.object(new_job, "PROJECT_CONFIG", self.config(code_host="github")), \
                patch.object(new_job, "create_issue", return_value=42):
            self.assertEqual(new_job.file_job("Fix it", "body", []), (42, "42"))

    def test_branch_is_named_from_the_issue_or_the_local_reference(self):
        # Only the pure naming helper: prepare_git_branch itself runs `git checkout -f` against the real project.
        job = {"job_id": "20261003-bug-abc123", "job_ref": "abc123", "title": "Fix the login"}
        local = worker_run.job_branch_name(job, None)
        github = worker_run.job_branch_name(job, 17)
        self.assertEqual(worker_run.job_branch_name({"job_id": "20261003-bug-ff00aa", "title": "Fix the login"}, None)[:14], "ai/job-ff00aa-")
        self.assertTrue(local.startswith("ai/job-abc123-"), local)
        self.assertTrue(github.startswith("ai/issue-17-"), github)
        self.assertTrue(common.is_ai_branch(local) and common.is_ai_branch(github))
        self.assertFalse(common.is_ai_branch("feature/mine"))

    def test_issue_helpers_do_nothing_without_an_issue(self):
        with patch.object(common, "run", side_effect=AssertionError("gh must not run")):
            common.gh_comment(None, "hello")
            common.update_issue_status(None, "status:executing")

    def test_branch_is_pushed_with_a_merge_request_link(self):
        remote = Path(self.tmp.name) / "remote.git"
        git(Path(self.tmp.name), "init", "-q", "--bare", str(remote))
        git(self.repo.root, "remote", "add", "origin", str(remote))
        branch = self.repo.job_branch()
        job = {"branch": branch, "base_branch": "main"}
        with patch.object(open_or_update_pr, "PROJECT_CONFIG", self.config(git_remote=str(remote))):
            self.assertEqual(open_or_update_pr.push_branch_for_review(job), "")  # a local path has no web page
        self.assertIn(branch, git(remote, "branch"))
        with patch.object(open_or_update_pr, "PROJECT_CONFIG", self.config(git_remote="git@gitlab.com:me/app.git")), \
                patch.object(open_or_update_pr, "run"):
            link = open_or_update_pr.push_branch_for_review(job)
        self.assertEqual(link, "https://gitlab.com/me/app/-/merge_requests/new?merge_request%5Bsource_branch%5D=ai%2Fjob-abc123-fix-it"
                               "&merge_request%5Btarget_branch%5D=main")

    def test_no_remote_means_nothing_to_push(self):
        with patch.object(open_or_update_pr, "PROJECT_CONFIG", self.config(git_remote=None)), \
                patch.object(open_or_update_pr, "run", side_effect=AssertionError("nothing to push to")):
            self.assertEqual(open_or_update_pr.push_branch_for_review({"branch": "ai/job-x", "base_branch": "main"}), "")

    def test_review_reads_the_branch_diff_from_this_repository(self):
        branch = self.repo.job_branch()
        metadata, diff = review_ready.load_branch_diff(branch, "main")
        self.assertIn("app.txt", metadata)
        self.assertIn("job work", metadata)
        self.assertIn("+two", diff)

    def test_local_merge_merges_and_never_pushes(self):
        branch = self.repo.job_branch()
        with patch.object(common, "run", side_effect=AssertionError("unused")):
            ok, message = common.merge_branch_locally(branch, "main", "Merge job")
        self.assertTrue(ok, message)
        self.assertEqual((self.repo.root / "app.txt").read_text(), "two\n")
        self.assertEqual(git(self.repo.root, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertIn("git push origin main", message)

    def test_conflicting_merge_changes_nothing(self):
        branch = self.repo.job_branch(content="theirs\n")
        (self.repo.root / "app.txt").write_text("ours\n")
        git(self.repo.root, "commit", "-qam", "main moved on")
        before = git(self.repo.root, "rev-parse", "HEAD")
        ok, message = common.merge_branch_locally(branch, "main", "Merge job")
        self.assertFalse(ok)
        self.assertIn("conflicts", message)
        self.assertEqual(git(self.repo.root, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.repo.root, "status", "--porcelain"), "")

    def test_uncommitted_changes_block_a_merge(self):
        branch = self.repo.job_branch()
        (self.repo.root / "app.txt").write_text("edited\n")
        ok, message = common.merge_branch_locally(branch, "main", "Merge job")
        self.assertFalse(ok)
        self.assertIn("uncommitted", message)
        self.assertEqual((self.repo.root / "app.txt").read_text(), "edited\n")

    def test_job_page_links_the_merge_request(self):
        url = "https://gitlab.com/me/app/-/merge_requests/new?merge_request%5Bsource_branch%5D=ai%2Fjob-x"
        links = ui.github_links(self.repo.root, {"code_host": "git", "branch": "ai/job-x", "merge_request_url": url})
        self.assertEqual(links, [{"label": "merge request", "url": url, "where": "On GitLab"}])
        self.assertEqual(ui.github_links(self.repo.root, {"merge_request_url": "javascript:alert(1)"}), [])

    def test_web_ui_offers_merge_for_a_plain_git_job(self):
        state = ui.job_state({"status": "review-needed", "code_host": "git", "branch": "ai/job-x", "base_branch": "main",
                              "merge_request_url": "https://gitlab.com/me/app/-/merge_requests/new"})
        self.assertEqual(state["next"]["action"], "merge")
        self.assertIn("ai/job-x", state["reason"])
        github_without_pr = ui.job_state({"status": "review-needed", "branch": "ai/issue-1-x"})
        self.assertEqual(github_without_pr["next"]["action"], "complete")


if __name__ == "__main__":
    unittest.main()
