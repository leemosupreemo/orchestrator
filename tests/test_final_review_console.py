"""Console finalization must require human review and preserve active jobs on failure."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "orchestrator/scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import dev_console
from orchestrator import job_review


class ConsoleFinalReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / ".orchestrator/jobs/job-1.json"
        self.path.parent.mkdir(parents=True)
        self.archive = self.path.parent / "archive"
        self.archive.mkdir()
        self.job = {"job_id": "job-1", "title": "A job", "status": "review-needed", "issue_number": 7}
        self.path.write_text(json.dumps(self.job))
        self.original = self.path.read_bytes()
        self.job["_path"] = self.path
        self.enterContext(patch.object(dev_console, "ARCHIVE_DIR", self.archive))
        self.enterContext(patch.object(dev_console, "ROOT", self.root))
        self.enterContext(patch.object(dev_console, "_back_to_menu"))
        self.enterContext(redirect_stdout(StringIO()))

    def signoff(self):
        self.job["manual_review"] = job_review.approve(self.root, self.job, reviewed=True, reviewer="A person")

    def test_closed_issue_cleanup_leaves_unreviewed_job_active(self):
        with patch.object(dev_console, "list_jobs", return_value=[self.job]), \
             patch.object(dev_console.subprocess, "check_output", return_value=b'[{"number":7}]'):
            dev_console.handle_cleanup_closed(silent=True, confirm=False)
        self.assertTrue(self.path.exists(), "Closing an issue must leave its unreviewed job active")
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse((self.archive / self.path.name).exists())

    def test_completed_archive_refuses_missing_signoff(self):
        with self.assertRaises(ValueError):
            dev_console.archive_job(self.job)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_failed_source_removal_rolls_back_archive_without_rewriting_active_job(self):
        self.signoff()
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == self.path:
                raise OSError("Active job cannot be removed")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink), self.assertRaises(OSError):
            dev_console.archive_job(self.job)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse((self.archive / self.path.name).exists())
        self.assertEqual(list(self.archive.iterdir()), [])

    def test_failed_atomic_destination_write_leaves_no_approval_on_disk(self):
        self.signoff()
        with patch.object(dev_console.os, "replace", side_effect=OSError("Disk full")), self.assertRaises(OSError):
            dev_console.archive_job(self.job)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(list(self.archive.iterdir()), [])

    def test_successful_archive_preserves_manual_review_and_never_serializes_path(self):
        self.signoff()
        approval = dict(self.job["manual_review"])
        destination = dev_console.archive_job(self.job)
        saved = json.loads(destination.read_text())
        self.assertFalse(self.path.exists())
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["manual_review"], approval)
        self.assertNotIn("_path", saved)
        self.assertEqual(self.job["status"], "review-needed")

    def test_noninteractive_merge_paths_reject_missing_signoff_before_commands(self):
        for handler in (dev_console.handle_merge_cleanup, dev_console.handle_local_merge):
            with self.subTest(handler=handler.__name__), patch("common.interactive", return_value=False), \
                 patch.object(dev_console.subprocess, "check_output") as read, \
                 patch.object(dev_console.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run, \
                 patch.object(dev_console, "merge_branch_locally", return_value=(False, "Test merge refused")) as merge:
                job = {**self.job, "pr_number": 7, "branch": "ai/job-1"}
                self.assertIs(handler(job), False)
                read.assert_not_called()
                run.assert_not_called()
                merge.assert_not_called()
                self.assertEqual(self.path.read_bytes(), self.original)

    def test_interactive_signoff_requires_explicit_confirmation_and_stays_in_memory(self):
        self.assertTrue(hasattr(dev_console, "review_job_completion"), "Console review guard is missing")
        with patch("common.interactive", return_value=True), patch.object(dev_console, "prompt_confirm", return_value=True) as confirm:
            approval = dev_console.review_job_completion(self.job)
        self.assertEqual(approval["reviewed_by"], "Console")
        self.assertEqual(confirm.call_args.kwargs["default"], False)
        self.assertEqual(self.path.read_bytes(), self.original)
        with patch("common.interactive", return_value=True), patch.object(dev_console, "prompt_confirm", return_value=False):
            with self.assertRaises(ValueError):
                dev_console.review_job_completion(self.job)

    def test_saved_signoff_revalidated_against_current_revision(self):
        self.assertTrue(hasattr(dev_console, "review_job_completion"), "Console review guard is missing")
        self.signoff()
        self.job["manual_review"]["revision"] = "old-revision"
        with self.assertRaises(ValueError):
            dev_console.review_job_completion(self.job)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_close_issue_leaves_local_job_for_manual_review(self):
        self.assertTrue(hasattr(dev_console, "handle_close_issue"), "Close issue must preserve the active job")
        with patch.object(dev_console, "prompt_confirm", return_value=True), \
             patch.object(dev_console.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(dev_console.handle_close_issue(self.job))
        self.assertEqual(run.call_args.args[0], ["gh", "issue", "close", "7"])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse((self.archive / self.path.name).exists())
