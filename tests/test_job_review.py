from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import job_review


class FinalReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / ".orchestrator"
        self.runtime.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.test")
        self.git("config", "user.name", "Test")
        (self.root / "app.txt").write_text("version one")
        self.git("add", "app.txt")
        self.git("commit", "-m", "Initial")
        self.commit = self.git("rev-parse", "HEAD")
        self.job = {"job_id": "job-1", "branch": "main", "status": "review-needed", "type": "feature",
                    "plan": {"tasks": [{"title": "Implement"}]}, "completed_task_indices": [0]}

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, stderr=subprocess.DEVNULL, text=True).strip()

    def config(self, **values):
        (self.runtime / "project.json").write_text(json.dumps(values))

    def receipt(self, **changes):
        folder = self.runtime / "output" / "delivery"
        folder.mkdir(parents=True, exist_ok=True)
        data = {"job_id": "job-1", "branch": "main", "status": "delivered", "commit": self.commit,
                "version": "1.2", "build": "3", "console_url": "https://console.firebase.google.com/test"}
        data.update(changes)
        (folder / "job-1.json").write_text(json.dumps(data))

    def test_unavailable_delivery_permits_explicit_manual_approval_only(self):
        state = job_review.review_state(self.root, self.job)
        self.assertTrue(state["can_approve"])
        self.assertEqual(state["delivery"]["status"], "unavailable")
        with self.assertRaisesRegex(ValueError, "review"):
            job_review.approve(self.root, self.job, reviewed=False)
        approval = job_review.approve(self.root, self.job, reviewed=True, expected_revision=state["revision"], reviewer="me@example.test")
        self.assertEqual(approval["reviewed_by"], "me@example.test")
        self.assertTrue(approval["reviewed_at"])
        self.assertEqual(approval["revision"], state["revision"])
        self.assertEqual(approval["commit"], self.commit)
        self.assertEqual(approval["delivery"]["status"], "unavailable")

    def test_configured_delivery_requires_a_successful_current_job_receipt(self):
        self.config(firebase_app_id="1:test:ios:app")  # manual delivery is configured even with automatic delivery off
        self.assertFalse(job_review.review_state(self.root, self.job)["can_approve"])
        for bad in ({"status": "failed"}, {"status": "delivering"}, {"job_id": "another-job"},
                    {"branch": "other"}, {"commit": "0" * 40}, {"commit": None}):
            with self.subTest(receipt=bad):
                self.receipt(**bad)
                self.assertFalse(job_review.review_state(self.root, self.job)["can_approve"])
                with self.assertRaises(ValueError):
                    job_review.approve(self.root, self.job, reviewed=True)
        self.receipt()
        state = job_review.review_state(self.root, self.job)
        self.assertTrue(state["can_approve"])
        self.assertEqual(state["delivery"]["status"], "delivered")
        self.assertEqual(state["delivery"]["version"], "1.2")
        self.assertEqual(state["delivery"]["url"], "https://console.firebase.google.com/test")

    def test_new_commits_invalidate_delivery_and_pending_review(self):
        self.config(firebase_distribution=True)
        self.receipt()
        revision = job_review.review_state(self.root, self.job)["revision"]
        (self.root / "app.txt").write_text("version two")
        self.git("add", "app.txt")
        self.git("commit", "-m", "Change")
        self.assertEqual(job_review.review_state(self.root, self.job)["delivery"]["status"], "stale")
        with self.assertRaises(ValueError):
            job_review.approve(self.root, self.job, reviewed=True, expected_revision=revision)

    def test_yolo_and_incomplete_work_never_supply_approval(self):
        for changes in ({"status": "executing"}, {"status": "planned"}, {"completed_task_indices": []}):
            with self.subTest(changes=changes):
                job = {**self.job, "yolo": True, "is_yolo": True, **changes}
                self.assertFalse(job_review.review_state(self.root, job)["can_approve"])
                with self.assertRaises(ValueError):
                    job_review.approve(self.root, job, reviewed=True)

    def test_tests_only_work_does_not_require_an_app_upload(self):
        self.config(firebase_distribution=True)
        state = job_review.review_state(self.root, {**self.job, "output_scope": "tests-only"})
        self.assertTrue(state["can_approve"])
        self.assertEqual(state["delivery"]["status"], "unavailable")

    def test_current_published_preview_can_be_reviewed_but_not_expired_or_other_branch(self):
        folder = self.runtime / "output" / "previews"
        folder.mkdir(parents=True)
        self.config(delivery_platform="web")
        (self.root / "firebase.json").write_text('{"hosting": {}}')
        for changes, allowed in (({}, True), ({"branch": "elsewhere"}, False), ({"expires": "2000-01-01T00:00:00Z"}, False)):
            preview = {"branch": "main", "commit": self.commit[:7], "url": "https://example.test/preview", **changes}
            (folder / "main.json").write_text(json.dumps(preview))
            self.assertEqual(job_review.review_state(self.root, self.job)["can_approve"], allowed)

    def test_delivery_links_must_be_safe_web_links(self):
        self.config(firebase_distribution=True)
        self.receipt(console_url="javascript:alert(1)")
        self.assertIsNone(job_review.review_state(self.root, self.job)["delivery"]["url"])

    def test_material_review_state_changes_invalidate_the_dialog_even_on_the_same_commit(self):
        self.config(firebase_distribution=True)
        self.receipt()
        revision = job_review.review_state(self.root, self.job)["revision"]
        self.receipt(build="4")
        with self.assertRaises(ValueError):
            job_review.approve(self.root, self.job, reviewed=True, expected_revision=revision)
        job = {**self.job, "output_scope": "tests-only", "ai_untracked_files": ["tests/new_test.py"]}
        (self.root / "tests").mkdir()
        test_file = self.root / "tests" / "new_test.py"
        test_file.write_text("assert True")
        revision = job_review.review_state(self.root, job)["revision"]
        test_file.write_text("assert False")
        with self.assertRaises(ValueError):
            job_review.approve(self.root, job, reviewed=True, expected_revision=revision)

    def test_dirty_code_and_expired_web_receipts_cannot_satisfy_delivery(self):
        self.config(firebase_distribution=True)
        self.receipt()
        (self.root / "app.txt").write_text("not committed or delivered")
        self.assertFalse(job_review.review_state(self.root, self.job)["can_approve"])
        self.git("checkout", "--", "app.txt")
        self.receipt(platform="web", built_at="2000-01-01 00:00:00", preview_url="https://example.test/old")
        self.assertFalse(job_review.review_state(self.root, self.job)["can_approve"])

    def test_an_in_progress_retry_cannot_use_an_old_preview_and_ios_manual_setup_is_required(self):
        self.config(delivery_provider="firebase", distribution_script_path="tools/ship.sh", firebase_plist_path="App.plist")
        self.assertTrue(job_review.review_state(self.root, self.job)["delivery"]["required"])
        folder = self.runtime / "output" / "previews"
        folder.mkdir(parents=True)
        (folder / "main.json").write_text(json.dumps({"branch": "main", "commit": self.commit,
                                                      "url": "https://example.test/current"}))
        self.receipt(status="delivering")
        self.assertFalse(job_review.review_state(self.root, self.job)["can_approve"])

    def test_web_final_actions_require_acknowledgment_and_current_revision(self):
        from orchestrator.web import server as ui
        jobs = self.runtime / "jobs"
        jobs.mkdir()
        (jobs / "job-1.json").write_text(json.dumps(self.job))
        for action in ("merge", "complete"):
            with self.subTest(action=action):
                for bad in ({"job": "job-1"}, {"job": "job-1", "reviewed": "true"},
                            {"job": "job-1", "reviewed": True, "review_revision": "old"}):
                    with self.assertRaises(ui.UIError):
                        ui.ACTIONS[action].build(bad, self.root)
                argv = ui.ACTIONS[action].build({"job": "job-1", "reviewed": True,
                                                "review_revision": job_review.review_state(self.root, self.job)["revision"]}, self.root)
                self.assertIn("--reviewed", argv)
                self.assertIn(job_review.review_state(self.root, self.job)["revision"], argv)

    def test_cli_cannot_complete_without_review_and_saves_the_review_when_approved(self):
        from unittest.mock import patch
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"))
        import job_actions
        jobs = self.runtime / "jobs"
        jobs.mkdir()
        path = jobs / "job-1.json"
        path.write_text(json.dumps(self.job))
        with patch("dev_console.archive_job") as archive:
            self.assertEqual(job_actions.main(["complete", str(path)]), 1)
            archive.assert_not_called()
            self.assertEqual(job_actions.main(["complete", str(path), "--reviewed"]), 0)
            approval = archive.call_args[0][0]["manual_review"]
            self.assertEqual(approval["commit"], self.commit)
            self.assertEqual(approval["reviewed_by"], "CLI")

    def test_delivery_receipt_records_the_commit_and_a_failed_retry_replaces_old_success(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"))
        import deliver_build
        job_file = self.root / "job.json"
        job_file.write_text(json.dumps(self.job))
        self.config(firebase_distribution=True)

        def delivered(_path):
            self.receipt(commit=None)
            raise SystemExit(0)

        with patch.object(deliver_build, "ROOT", self.root), \
             patch.object(deliver_build, "PROJECT_CONFIG", SimpleNamespace(runtime_dir=self.runtime)), \
             patch.object(deliver_build, "_deliver_build", side_effect=delivered):
            with self.assertRaises(SystemExit) as stopped:
                deliver_build.deliver_build(job_file)
            self.assertEqual(stopped.exception.code, 0)
        state = job_review.review_state(self.root, self.job)
        self.assertEqual(state["delivery"]["status"], "delivered")

        with patch.object(deliver_build, "ROOT", self.root), \
             patch.object(deliver_build, "PROJECT_CONFIG", SimpleNamespace(runtime_dir=self.runtime)), \
             patch.object(deliver_build, "_deliver_build", side_effect=SystemExit(1)):
            with self.assertRaises(SystemExit):
                deliver_build.deliver_build(job_file)
        state = job_review.review_state(self.root, self.job)
        self.assertEqual(state["delivery"]["status"], "failed")
        self.assertFalse(state["can_approve"])
