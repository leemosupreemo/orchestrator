from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "orchestrator" / "scripts"))

import open_or_update_pr as pr_mgr  # noqa: E402


from types import SimpleNamespace

class PushBranchForReviewTests(unittest.TestCase):
    def test_returns_empty_when_no_branch_or_no_remote(self):
        with patch.object(pr_mgr, "PROJECT_CONFIG", SimpleNamespace(git_remote=None, base_branch="main")):
            self.assertEqual(pr_mgr.push_branch_for_review({"branch": "feat/login"}), "")
        with patch.object(pr_mgr, "PROJECT_CONFIG", SimpleNamespace(git_remote="https://github.com/org/repo", base_branch="main")):
            self.assertEqual(pr_mgr.push_branch_for_review({}), "")

    def test_successful_push_returns_review_url(self):
        mock_cfg = SimpleNamespace(git_remote="https://github.com/org/repo", base_branch="main")
        with patch.object(pr_mgr, "PROJECT_CONFIG", mock_cfg), \
             patch.object(pr_mgr, "run") as mock_run:
            url = pr_mgr.push_branch_for_review({"branch": "feat/login"})
            self.assertIn("github.com/org/repo/compare/main...feat%2Flogin", url)
            mock_run.assert_called_once()

    def test_failed_push_returns_empty_string(self):
        mock_cfg = SimpleNamespace(git_remote="https://github.com/org/repo", base_branch="main")
        with patch.object(pr_mgr, "PROJECT_CONFIG", mock_cfg), \
             patch.object(pr_mgr, "run", side_effect=RuntimeError("SSH error")):
            self.assertEqual(pr_mgr.push_branch_for_review({"branch": "feat/login"}), "")


class OpenOrUpdatePRTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output_dir = self.root / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_job(self, job_dict: dict) -> Path:
        job_file = self.root / f"{job_dict['job_id']}.json"
        job_file.write_text(json.dumps(job_dict))
        return job_file

    def test_returns_none_when_job_has_no_branch(self):
        job_file = self._write_job({"job_id": "job-1"})
        self.assertEqual(pr_mgr.open_or_update_pr(job_file), (None, None))

    def test_does_not_open_pr_from_base_branch(self):
        job_file = self._write_job({"job_id": "job-main", "branch": "main"})
        with patch.object(pr_mgr, "run"), \
             patch.object(pr_mgr, "gh_json", return_value=[]), \
             patch.object(pr_mgr, "OUTPUT_DIR", self.output_dir):
            self.assertEqual(pr_mgr.open_or_update_pr(job_file), (None, None))

    def test_creates_new_draft_pr_when_none_exists(self):
        job = {
            "job_id": "job-create",
            "branch": "feature/dark-mode",
            "issue_number": 42,
            "title": "Add dark mode toggle",
            "debug_history": [
                {
                    "iteration": 1,
                    "hypothesis": "CSS variable mismatch",
                    "action": "Update tokens",
                    "result": {"build_ok": True, "tests_ok": True, "notes": "Resolved color tokens"},
                }
            ],
        }
        job_file = self._write_job(job)

        created_args = []

        def fake_gh_text(*args):
            created_args.append(args)
            return "https://github.com/org/repo/pull/101\n"

        with patch.object(pr_mgr, "run"), \
             patch.object(pr_mgr, "gh_json", return_value=[]), \
             patch.object(pr_mgr, "gh_text", side_effect=fake_gh_text), \
             patch.object(pr_mgr, "OUTPUT_DIR", self.output_dir):
            pr_num, pr_url = pr_mgr.open_or_update_pr(job_file)
            self.assertEqual(pr_num, 101)
            self.assertEqual(pr_url, "https://github.com/org/repo/pull/101")
            self.assertTrue(any("pr" in c and "create" in c for c in created_args))
            create_call = next(c for c in created_args if c[0] == "pr" and c[1] == "create")
            self.assertIn("--draft", create_call)
            self.assertIn("AI draft for issue #42: Add dark mode toggle", create_call)

    def test_updates_existing_matching_pr(self):
        job = {
            "job_id": "job-update",
            "branch": "feature/analytics",
            "issue_number": 99,
            "title": "Fix analytics events",
        }
        job_file = self._write_job(job)

        existing_prs = [
            {"number": 45, "title": "Work on issue #99", "body": "Initial description"},
        ]

        edited_calls = []

        def fake_gh_text(*args):
            edited_calls.append(args)
            return ""

        def fake_gh_json(*args):
            if args[1] == "list":
                return existing_prs
            if args[1] == "view":
                return {"url": "https://github.com/org/repo/pull/45"}
            return {}

        with patch.object(pr_mgr, "run"), \
             patch.object(pr_mgr, "gh_json", side_effect=fake_gh_json), \
             patch.object(pr_mgr, "gh_text", side_effect=fake_gh_text), \
             patch.object(pr_mgr, "OUTPUT_DIR", self.output_dir):
            pr_num, pr_url = pr_mgr.open_or_update_pr(job_file)
            self.assertEqual(pr_num, 45)
            self.assertEqual(pr_url, "https://github.com/org/repo/pull/45")
            self.assertTrue(any(c[0] == "pr" and c[1] == "edit" and c[2] == "45" for c in edited_calls))


if __name__ == "__main__":
    unittest.main()
