from __future__ import annotations

import subprocess
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_config_validation import make_config

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"))
import run_build_and_tests as runner
import check_setup
import common
import manual_run


class GenericExecutionTests(unittest.TestCase):
    def test_real_python_project_build_and_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_example.py").write_text(
                "import unittest\nclass Example(unittest.TestCase):\n"
                "    def test_answer(self):\n        self.assertEqual(6 * 7, 42)\n")
            python = shlex.quote(sys.executable)
            config = make_config(root, xcode_project=None, scheme=None, test_target="",
                                 build_command=f"{python} -m compileall -q test_example.py",
                                 test_command=f"{python} -m unittest discover -v")
            with patch.object(common, "PROJECT_CONFIG", config), \
                 patch.object(common, "DOCS_DIR", root / "docs"), \
                 patch.object(common, "ROOT", root), patch.object(runner, "ROOT", root), \
                 patch.object(runner, "OUTPUT_DIR", root / "output"), \
                 patch.object(runner, "get_best_simulator_destination") as simulator:
                self.assertEqual(runner.run_build_and_tests({"job_id": "python"}, root / "summary.md"),
                                 (True, True))
                self.assertIn("Ran 1 test", (root / "output/python/test.log").read_text())
                simulator.assert_not_called()

    def test_manual_custom_run_does_not_require_build_configuration(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manual_run, "ROOT", Path(tmp)), \
             patch.object(manual_run, "OUTPUT_DIR", Path(tmp) / "output"), \
             patch.object(manual_run, "extract_commands") as extract, \
             patch.object(manual_run, "stream_command", return_value=True) as stream, \
             patch("sys.argv", ["manual_run.py", "run", "--run-cmd", "cargo fmt --check"]):
            manual_run.main()
            extract.assert_not_called()
            self.assertEqual(stream.call_args.args[0], "cargo fmt --check")

    def test_manual_selected_suite_uses_exact_non_xcode_test_command(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(manual_run, "ROOT", Path(tmp)), \
             patch.object(manual_run, "OUTPUT_DIR", Path(tmp) / "output"), \
             patch.object(manual_run, "extract_commands", return_value=("cargo build", "cargo test")), \
             patch.object(manual_run, "stream_command", return_value=True) as stream, \
             patch("sys.argv", ["manual_run.py", "test", "--test-command",
                                "cargo test --test parser"]):
            manual_run.main()

            self.assertEqual(stream.call_args.args[0], "cargo test --test parser")

    def test_test_tool_format_check_rejects_prose_and_malformed_quotes(self):
        for override in ["Run cargo test", "cargo test 'unterminated", "npm test", ""]:
            with self.subTest(override=override):
                self.assertFalse(runner.uses_configured_test_tool(override, "cargo test"))

    def test_overrides_use_configured_toolchain(self):
        for base, override in [("cargo test", "cargo test parser"),
                               ("cargo test", "cargo test none_case"),
                               ("npm test", "npm test -- --runInBand"),
                               ("go test ./...", "go test ./parser"),
                               ("uv run pytest", "uv run pytest tests/test_parser.py")]:
            with self.subTest(override=override), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(runner, "OUTPUT_DIR", Path(tmp)), \
                 patch.object(runner, "extract_commands", return_value=("custom-build", base)), \
                 patch.object(runner, "run_shell", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                 patch.object(runner, "get_best_simulator_destination") as simulator:
                result = runner.run_build_and_tests(
                    {"job_id": "generic", "test_command_override": override,
                     "plan": {"likely_files": ["scripts/parser.py"]}}, Path(tmp) / "summary.md")
                self.assertEqual(result, (True, True))
                self.assertEqual([call.args[0] for call in run.call_args_list], ["custom-build", override])
                simulator.assert_not_called()

    def test_generic_failure_does_not_capture_simulator_logs(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(runner, "OUTPUT_DIR", Path(tmp)), \
             patch.object(runner, "extract_commands", return_value=("cargo build", "cargo test")), \
             patch.object(runner, "run_shell", side_effect=[subprocess.CompletedProcess([], 0),
                 subprocess.CompletedProcess([], 1, "test failed", "")]), \
             patch.object(runner, "analyze_failure", return_value="Test failure"), \
             patch.object(runner, "capture_system_logs") as capture, \
             patch.object(runner, "extract_xcresult_summary", return_value="") as xcresult:
            self.assertEqual(runner.run_build_and_tests({"job_id": "generic"}, Path(tmp) / "summary.md"),
                             (True, False))
            capture.assert_not_called()
            xcresult.assert_not_called()

    def test_generic_setup_does_not_probe_xcode(self):
        import io
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp), xcode_project=None, scheme=None,
                                 test_target="", build_command="cargo build", test_command="cargo test")
            with patch.object(check_setup, "PROJECT_CONFIG", config), \
                 patch("shutil.which", return_value=None) as which, \
                 patch("pathlib.Path.exists", return_value=False), \
                 patch("sys.stdout", new_callable=io.StringIO):
                check_setup.check()
            self.assertNotIn("xcodebuild", [call.args[0] for call in which.call_args_list])
