from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "orchestrator" / "scripts"))

import common  # noqa: E402
import run_build_and_tests as runner  # noqa: E402


class RunBuildAndTestsHelperTests(unittest.TestCase):
    def test_uses_configured_test_tool_matches_executable(self):
        self.assertTrue(runner.uses_configured_test_tool("pytest tests/test_one.py", "pytest -v"))
        self.assertTrue(runner.uses_configured_test_tool("python3 -m unittest -v", "python3 -m unittest"))
        self.assertFalse(runner.uses_configured_test_tool("cargo test", "pytest -v"))
        # Malformed quoting should be safely rejected
        self.assertFalse(runner.uses_configured_test_tool('pytest "unclosed', "pytest"))

    def test_resolve_command_replaces_pwd_and_injects_simulator(self):
        with patch.object(runner, "ROOT", Path("/mock/project")), \
             patch.object(runner, "get_best_simulator_destination", return_value="id=123-ABC"):
            resolved = runner.resolve_command("cd $(pwd) && xcodebuild test")
            self.assertIn("/mock/project", resolved)
            self.assertIn("-destination id=123-ABC", resolved)

        # Non-xcodebuild commands leave $(pwd) resolved without simulator injection
        with patch.object(runner, "ROOT", Path("/mock/project")):
            resolved = runner.resolve_command("cd $(pwd) && pytest")
            self.assertEqual(resolved, "cd /mock/project && pytest")
            self.assertEqual(runner.resolve_command(""), "")

    def test_extract_xcresult_summary_no_match(self):
        output = "Build failed with normal compiler errors\nNo xcresult here"
        self.assertEqual(runner.extract_xcresult_summary(output), "")

    def test_extract_xcresult_summary_success(self):
        output = (
            "Test session results, code coverage, and logs:\n"
            "\t/private/tmp/DerivedData/Logs/Test/Run.xcresult\n"
        )
        fake_json = json.dumps({"actions": [{"status": "failed", "runDestination": {"targetArchitecture": "arm64"}}]})
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["xcrun", "xcresulttool"], returncode=0, stdout=fake_json, stderr=""
            )
            summary = runner.extract_xcresult_summary(output)
            self.assertIn("--- XCRESULT DATA ---", summary)
            self.assertIn("Run.xcresult", summary)
            self.assertIn("arm64", summary)
            mock_run.assert_called_once_with(
                ["xcrun", "xcresulttool", "get", "--path", "/private/tmp/DerivedData/Logs/Test/Run.xcresult", "--format", "json"],
                capture_output=True,
                text=True,
            )

    def test_extract_xcresult_summary_tool_failure(self):
        output = "Test session results, code coverage, and logs:\t/tmp/fail.xcresult"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["xcrun", "xcresulttool"], returncode=1, stdout="", stderr="xcresulttool: internal error"
            )
            summary = runner.extract_xcresult_summary(output)
            self.assertIn("Failed to run xcresulttool", summary)

    def test_capture_system_logs_success_when_booted_sim_present(self):
        simctl_data = json.dumps({
            "devices": {
                "com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
                    {"name": "iPhone 16", "udid": "SIM-UDID-001", "state": "Booted"}
                ]
            }
        })
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "system.log"
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = [
                    subprocess.CompletedProcess(args=["simctl", "list"], returncode=0, stdout=simctl_data, stderr=""),
                    subprocess.CompletedProcess(args=["simctl", "spawn"], returncode=0, stdout="Syslog content from sim", stderr=""),
                ]
                success = runner.capture_system_logs(out_file)
                self.assertTrue(success)
                self.assertTrue(out_file.exists())
                self.assertEqual(out_file.read_text(), "Syslog content from sim")

    def test_capture_system_logs_returns_false_when_no_booted_sims(self):
        simctl_data = json.dumps({"devices": {"runtime": []}})
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "system.log"
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    args=["simctl", "list"], returncode=0, stdout=simctl_data, stderr=""
                )
                self.assertFalse(runner.capture_system_logs(out_file))
                self.assertFalse(out_file.exists())


class RunBuildAndTestsExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.output_dir = self.root / "output"
        self.summary_file = self.root / "summary.md"
        self.sim_patcher = patch.object(runner, "get_best_simulator_destination", return_value="id=TEST-SIM")
        self.sim_patcher.start()

    def tearDown(self):
        self.sim_patcher.stop()
        self.tmp.cleanup()

    def test_skips_tests_when_override_is_na_or_skipped(self):
        for override in ["n/a", "none", "no tests", "skipped"]:
            job = {"job_id": "skip-job", "test_command_override": override}
            with patch.object(runner, "ROOT", self.root), \
                 patch.object(runner, "OUTPUT_DIR", self.output_dir), \
                 patch.object(runner, "extract_commands", return_value=("echo build", "echo test")), \
                 patch.object(runner, "run_shell", return_value=subprocess.CompletedProcess(args=[], returncode=0)):
                build_ok, tests_ok = runner.run_build_and_tests(job, self.summary_file)
                self.assertTrue(build_ok)
                self.assertTrue(tests_ok)

    def test_test_command_override_custom_runner(self):
        job = {"job_id": "custom-test-job", "test_command_override": "python3 -m pytest tests/unit"}
        executed_cmds = []

        def fake_run_shell(cmd, cwd=None, check=False, capture=False):
            executed_cmds.append(cmd)
            return subprocess.CompletedProcess(args=[cmd], returncode=0, stdout="1 passed", stderr="")

        with patch.object(runner, "ROOT", self.root), \
             patch.object(runner, "OUTPUT_DIR", self.output_dir), \
             patch.object(runner, "extract_commands", return_value=("echo build", "xcodebuild test")), \
             patch.object(runner, "run_shell", side_effect=fake_run_shell):
            build_ok, tests_ok = runner.run_build_and_tests(job, self.summary_file)
            self.assertTrue(build_ok)
            self.assertTrue(tests_ok)
            self.assertIn("python3 -m pytest tests/unit", executed_cmds)

    def test_build_failure_analyzes_error_and_returns_false(self):
        job = {"job_id": "fail-build-job"}
        with patch.object(runner, "ROOT", self.root), \
             patch.object(runner, "OUTPUT_DIR", self.output_dir), \
             patch.object(runner, "extract_commands", return_value=("make fail_build", "make test")), \
             patch.object(runner, "run_shell", return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="Syntax error", stderr="")), \
             patch.object(runner, "analyze_failure", return_value="Fix syntax error on line 12"):
            build_ok, tests_ok = runner.run_build_and_tests(job, self.summary_file)
            self.assertFalse(build_ok)
            self.assertFalse(tests_ok)
            self.assertTrue((self.output_dir / "fail-build-job" / "build.log").exists())

    def test_override_with_only_testing_appends_to_xcodebuild(self):
        job = {"job_id": "only-test-job", "test_command_override": "-only-testing:MyAppTests/AuthTests"}
        executed = []

        def fake_run(cmd, cwd=None, check=False, capture=False):
            executed.append(cmd)
            return subprocess.CompletedProcess(args=[cmd], returncode=0, stdout="Passed", stderr="")

        with patch.object(runner, "ROOT", self.root), \
             patch.object(runner, "OUTPUT_DIR", self.output_dir), \
             patch.object(runner, "extract_commands", return_value=("echo build", "xcodebuild test")), \
             patch.object(runner, "get_best_simulator_destination", return_value="id=SIM1"), \
             patch.object(runner, "run_shell", side_effect=fake_run):
            build_ok, tests_ok = runner.run_build_and_tests(job, self.summary_file)
            self.assertTrue(build_ok)
            self.assertTrue(tests_ok)
            self.assertTrue(any("-only-testing:MyAppTests/AuthTests" in cmd for cmd in executed))

    def test_override_malformed_text_falls_back_to_raw(self):
        job = {"job_id": "malformed-override-job", "test_command_override": "Some random markdown response"}
        executed = []

        def fake_run(cmd, cwd=None, check=False, capture=False):
            executed.append(cmd)
            return subprocess.CompletedProcess(args=[cmd], returncode=0, stdout="Passed", stderr="")

        with patch.object(runner, "ROOT", self.root), \
             patch.object(runner, "OUTPUT_DIR", self.output_dir), \
             patch.object(runner, "extract_commands", return_value=("echo build", "pytest -v")), \
             patch.object(runner, "run_shell", side_effect=fake_run):
            build_ok, tests_ok = runner.run_build_and_tests(job, self.summary_file)
            self.assertTrue(build_ok)
            self.assertTrue(tests_ok)
            self.assertIn("pytest -v", executed)

    def test_build_destination_rejected_retries_with_fallback(self):
        job = {"job_id": "dest-fallback-job"}
        err_msg = "Unable to find a device matching the provided destination specifier"
        calls = []

        def fake_run(cmd, cwd=None, check=False, capture=False):
            calls.append((cmd, capture))
            if "DEST_A" in cmd:
                return subprocess.CompletedProcess(args=[cmd], returncode=70, stdout=err_msg, stderr="")
            return subprocess.CompletedProcess(args=[cmd], returncode=0, stdout="Build Succeeded", stderr="")

        with patch.object(runner, "ROOT", self.root), \
             patch.object(runner, "OUTPUT_DIR", self.output_dir), \
             patch.object(runner, "extract_commands", return_value=("xcodebuild -destination 'id=DEST_A' build", None)), \
             patch.object(runner, "get_fallback_simulator_destinations", return_value=["id=DEST_A", "id=DEST_B"]), \
             patch.object(runner, "run_shell", side_effect=fake_run):
            build_ok, tests_ok = runner.run_build_and_tests(job, self.summary_file)
            self.assertTrue(build_ok)
            self.assertTrue(tests_ok)

    def test_build_environment_issue_raises_validation_error(self):
        job = {"job_id": "env-fail-job"}
        output = "xcodebuild: error: Unable to boot device in current state"
        with patch.object(runner, "ROOT", self.root), \
             patch.object(runner, "OUTPUT_DIR", self.output_dir), \
             patch.object(runner, "extract_commands", return_value=("xcodebuild build", None)), \
             patch.object(runner, "run_shell", return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout=output, stderr="")), \
             patch.object(runner, "detect_simulator_environment_issue", return_value="Simulator runtime hung"):
            with self.assertRaises(runner.EnvironmentValidationError):
                runner.run_build_and_tests(job, self.summary_file)
            self.assertTrue((self.output_dir / "env-fail-job" / "environment_failure.md").exists())

    def test_disk_full_triggers_purge_and_retry(self):
        job = {"job_id": "disk-full-job"}
        attempts = [0]

        def fake_run(cmd, cwd=None, check=False, capture=False):
            attempts[0] += 1
            if attempts[0] == 1:
                return subprocess.CompletedProcess(args=[cmd], returncode=1, stdout="No space left on device", stderr="")
            return subprocess.CompletedProcess(args=[cmd], returncode=0, stdout="Build OK", stderr="")

        with patch.object(runner, "ROOT", self.root), \
             patch.object(runner, "OUTPUT_DIR", self.output_dir), \
             patch.object(runner, "extract_commands", return_value=("make build", None)), \
             patch.object(runner, "run_shell", side_effect=fake_run), \
             patch.object(runner, "is_disk_full_error", return_value=True), \
             patch.object(runner, "purge_zombie_processes") as mock_purge:
            build_ok, tests_ok = runner.run_build_and_tests(job, self.summary_file)
            self.assertTrue(build_ok)
            self.assertTrue(tests_ok)
            mock_purge.assert_called_once()

    def test_xcodebuild_test_failure_extracts_xcresult_and_captures_logs(self):
        job = {"job_id": "test-failure-diag-job"}
        stdout_fail = (
            "Test suite AllTests started\n"
            "Test case -[AuthTests testLogin] failed (0.12s)\n"
            "Test session results, code coverage, and logs:\n"
            "\t/tmp/run.xcresult\n"
        )
        with patch.object(runner, "ROOT", self.root), \
             patch.object(runner, "OUTPUT_DIR", self.output_dir), \
             patch.object(runner, "extract_commands", return_value=("echo build", "xcodebuild test")), \
             patch.object(runner, "run_shell", side_effect=[
                 subprocess.CompletedProcess(args=[], returncode=0, stdout="Build OK", stderr=""),
                 subprocess.CompletedProcess(args=[], returncode=1, stdout=stdout_fail, stderr="Assertion failed"),
             ]), \
             patch.object(runner, "analyze_failure", return_value="Test failed on assert"), \
             patch.object(runner, "extract_xcresult_summary", return_value="Summary of xcresult"), \
             patch.object(runner, "capture_system_logs") as mock_capture:
            build_ok, tests_ok = runner.run_build_and_tests(job, self.summary_file)
            self.assertTrue(build_ok)
            self.assertFalse(tests_ok)
            mock_capture.assert_called_once()
            job_out = self.output_dir / "test-failure-diag-job"
            self.assertTrue((job_out / "test.log").exists())


if __name__ == "__main__":
    unittest.main()

