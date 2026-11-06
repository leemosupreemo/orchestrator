from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure paths
TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SCRIPTS_DIR = REPO_ROOT / "orchestrator" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import notify
import export_job
import trace_braces

class UtilitiesTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.test_dir.name)

    def tearDown(self):
        self.test_dir.cleanup()

    @patch("notify.subprocess.run")
    @patch("notify.send_email_notification")
    def test_notify_desktop_and_email(self, mock_email, mock_subproc):
        """Verify notify triggers osascript and email notification."""
        mock_email.return_value = True
        mock_subproc.return_value = MagicMock(returncode=0)

        res = notify.notify("Test Title", "Test Message", job_id="job-123", summary="Done")
        self.assertTrue(res)
        mock_subproc.assert_called_once()
        self.assertIn("osascript", mock_subproc.call_args[0][0])
        mock_email.assert_called_once_with("Test Title", "Test Message", "job-123", "Done")

    def test_notify_disabled_env(self):
        """Verify notification skips when ORCHESTRATOR_DISABLE_NOTIFICATIONS=1."""
        with patch.dict(os.environ, {"ORCHESTRATOR_DISABLE_NOTIFICATIONS": "1"}):
            res = notify.notify("Test", "Message")
            self.assertFalse(res)

    def test_trace_braces_balanced(self):
        """Verify trace_braces handles valid Swift code without error output."""
        swift_file = self.temp_root / "Valid.swift"
        swift_file.write_text("""// Comment {
func test() {
    let str = "{ Hello }"
    /* Block { */
    if true {
        return
    }
}
""")
        captured_output = io.StringIO()
        with patch("sys.stdout", captured_output):
            trace_braces.trace_braces(str(swift_file))
        self.assertEqual(captured_output.getvalue().strip(), "")

    def test_trace_braces_unclosed(self):
        """Verify trace_braces detects unclosed opening braces."""
        swift_file = self.temp_root / "Unclosed.swift"
        swift_file.write_text("""func test() {
    if true {
}
""")
        captured_output = io.StringIO()
        with patch("sys.stdout", captured_output):
            trace_braces.trace_braces(str(swift_file))
        self.assertIn("Unclosed braces", captured_output.getvalue())

    def test_export_job_zip(self):
        """Verify export_job creates a valid zip containing job files and manifest."""
        job_file = self.temp_root / "job-1.json"
        job_data = {
            "job_id": "job-1",
            "title": "Test Job",
            "interactive_investigations": [
                {
                    "tool": "Codex CLI",
                    "timestamp": "2026-09-06T14:30:00-05:00",
                    "duration": "1m 10s",
                    "notes": "Verified root cause",
                }
            ]
        }
        job_file.write_text(json.dumps(job_data))
        zip_out = self.temp_root / "export.zip"

        export_job.export_job(str(job_file), str(zip_out))
        self.assertTrue(zip_out.exists())

        import zipfile
        with zipfile.ZipFile(zip_out, "r") as z:
            manifest_text = z.read("job-1/manifest.txt").decode("utf-8")
            self.assertIn("Verified root cause", manifest_text)
            self.assertIn("Codex CLI", manifest_text)

    def test_export_job_destination_icloud(self):
        """Verify export_job exports to iCloud Drive path when destination='icloud'."""
        mock_icloud_dir = self.temp_root / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
        mock_icloud_dir.mkdir(parents=True, exist_ok=True)

        job_file = self.temp_root / "job-2.json"
        job_file.write_text(json.dumps({"job_id": "job-2", "title": "iCloud Test Job"}))

        with patch("pathlib.Path.home", return_value=self.temp_root):
            out_path = export_job.export_job(str(job_file), destination="icloud")
            self.assertIsNotNone(out_path)
            self.assertTrue(out_path.exists())
            self.assertIn("com~apple~CloudDocs", str(out_path))
            self.assertIn("Orchestrator", str(out_path))

    def test_export_job_destination_gdrive(self):
        """Verify export_job exports to Google Drive path when destination='gdrive'."""
        mock_gdrive_dir = self.temp_root / "Library" / "CloudStorage" / "GoogleDrive-test@gmail.com" / "My Drive"
        mock_gdrive_dir.mkdir(parents=True, exist_ok=True)

        job_file = self.temp_root / "job-3.json"
        job_file.write_text(json.dumps({"job_id": "job-3", "title": "GDrive Test Job"}))

        with patch("pathlib.Path.home", return_value=self.temp_root):
            out_path = export_job.export_job(str(job_file), destination="gdrive")
            self.assertIsNotNone(out_path)
            self.assertTrue(out_path.exists())
            self.assertIn("GoogleDrive-test@gmail.com", str(out_path))
            self.assertIn("Orchestrator", str(out_path))

    @patch("export_job.prompt_radio")
    @patch("export_job.sys.stdin.isatty", return_value=True)
    def test_export_job_interactive_prompt_selection(self, mock_isatty, mock_prompt_radio):
        """Verify interactive prompt_radio allows picking destination."""
        mock_downloads_dir = self.temp_root / "Downloads"
        mock_downloads_dir.mkdir(parents=True, exist_ok=True)

        job_file = self.temp_root / "job-4.json"
        job_file.write_text(json.dumps({"job_id": "job-4", "title": "Interactive Test Job"}))

        mock_prompt_radio.return_value = "📥 macOS Downloads (~/Downloads/)"

        with patch("pathlib.Path.home", return_value=self.temp_root):
            out_path = export_job.export_job(str(job_file))
            self.assertIsNotNone(out_path)
            self.assertTrue(out_path.exists())
            self.assertIn("Downloads", str(out_path))

    def test_make_brief_includes_investigation_history(self):
        """Verify make_brief injects interactive investigation history into briefs."""
        import run_builder
        job = {
            "type": "bug-fix",
            "issue_number": 101,
            "title": "Fix crash on launch",
            "plan": {
                "summary": "Fix AppDelegate launch crash",
                "repro_steps": ["Launch app", "Observe crash"],
                "expected_behavior": "App launches smoothly",
                "acceptance_criteria": ["No crash on startup"],
                "constraints": ["Keep iOS 17 compatibility"],
                "likely_files": ["Sources/AppDelegate.swift"],
            },
            "interactive_investigations": [
                {
                    "tool": "Codex CLI",
                    "timestamp": "2026-09-06T14:00:00-05:00",
                    "duration": "2m 15s",
                    "notes": "Traced null pointer in launch configuration",
                    "new_commits": ["abc1234 Add safe unwrapping to AppDelegate"],
                }
            ]
        }
        brief = run_builder.make_brief(job)
        self.assertIn("# Bug brief", brief)
        self.assertIn("### 🔍 Interactive Investigation Findings & CLI Notes", brief)
        self.assertIn("Codex CLI | 2m 15s | 2026-09-06 14:00", brief)
        self.assertIn("Traced null pointer in launch configuration", brief)
        self.assertIn("`abc1234 Add safe unwrapping to AppDelegate`", brief)

    def test_strip_xcode_test_plan(self):
        import manual_run
        cmd = "xcodebuild test -scheme App -testPlan SmokeTests -destination 'platform=iOS Simulator,id=123'"
        stripped = manual_run.strip_xcode_test_plan(cmd)
        self.assertNotIn("-testPlan", stripped)
        self.assertNotIn("SmokeTests", stripped)
        self.assertIn("-scheme App", stripped)

    def test_manual_run_headers_and_summaries(self):
        import manual_run
        captured_output = io.StringIO()
        with patch("sys.stdout", captured_output):
            manual_run.print_execution_header("xcodebuild test -scheme App -destination 'platform=iOS Simulator,id=SIM1'", Path("orchestrator_output/manual/latest/test.log"), mode="test")
        out = captured_output.getvalue()
        self.assertIn("TEST EXECUTION PIPELINE", out)
        self.assertIn("SIM1", out)
        self.assertIn("test.log", out)

        # Test success summary
        test_out_success = "Executed 10 tests, with 0 failures in 1.2s"
        captured_output = io.StringIO()
        with patch("sys.stdout", captured_output):
            manual_run.print_test_results_summary(test_out_success, Path("orchestrator_output/manual/latest/test.log"), 4.5, 0)
        out = captured_output.getvalue()
        self.assertIn("TEST RUN SUCCEEDED", out)
        self.assertIn("4.5s", out)
        self.assertIn("10", out)

        # Test failure summary with failing tests
        test_out_fail = "Executed 10 tests, with 2 failures\nTest Case '-[AppTests.AuthTests testLogin]' failed\nTest Case '-[AppTests.AuthTests testLogout]' failed"
        captured_output = io.StringIO()
        with patch("sys.stdout", captured_output):
            manual_run.print_test_results_summary(test_out_fail, Path("orchestrator_output/manual/latest/test.log"), 3.2, 1)
        out = captured_output.getvalue()
        self.assertIn("TEST RUN FAILED", out)
        self.assertIn("AuthTests.testLogin", out)
        self.assertIn("AuthTests.testLogout", out)

        # Test build summary
        captured_output = io.StringIO()
        with patch("sys.stdout", captured_output):
            manual_run.print_build_results_summary("", Path("orchestrator_output/manual/latest/build.log"), 8.0, 0)
        out = captured_output.getvalue()
        self.assertIn("BUILD SUCCEEDED", out)


if __name__ == "__main__":
    unittest.main()
