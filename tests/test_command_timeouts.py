"""The main build and test commands stop when they hang, instead of blocking a job forever."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(PACKAGE_ROOT / "orchestrator" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import run_build_and_tests as runner  # noqa: E402
from orchestrator.config_validation import validate_project_config  # noqa: E402
from test_config_validation import make_config  # noqa: E402


class RunShellTimeoutTests(unittest.TestCase):
    def test_a_command_that_hangs_is_stopped_with_everything_it_started(self):
        marker = f"orchestrator-timeout-test-{os.getpid()}"
        started = time.time()
        result = common.run_shell(f"echo begin; sleep 61 & exec -a {marker} sleep 62", check=False, timeout=2)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.returncode, common.TIMEOUT_EXIT_CODE)
        self.assertLess(time.time() - started, 15)
        self.assertIn("begin", result.stdout)
        self.assertIn("Stopped after 2 seconds", result.stderr)
        left: list[str] = []
        for _ in range(30):  # a stopped process is gone once the system has reaped it (a zombie isn't running)
            left = []
            for line in subprocess.run(["ps", "-eo", "pid,stat,command"], capture_output=True, text=True).stdout.splitlines()[1:]:
                pid, stat, command = line.split(None, 2)
                if not stat.startswith("Z") and (re.search(r"\bsleep 6[12]\b", command) or marker in command) and not command.startswith("ps "):
                    left.append(line)
            if not left:
                break
            time.sleep(0.2)
        self.assertEqual(left, [], "the command's background processes were left running")

    def test_a_command_that_finishes_in_time_is_unchanged(self):
        for capture in (True, False):
            result = common.run_shell("echo fine; exit 3", check=False, capture=capture, timeout=30)
            self.assertFalse(result.timed_out)
            self.assertEqual(result.returncode, 3)
        self.assertEqual(common.run_shell("echo fine", check=False, timeout=30).stdout.strip(), "fine")

    def test_without_a_timeout_nothing_changes(self):
        result = common.run_shell("echo plain", check=False)
        self.assertFalse(hasattr(result, "timed_out"))
        self.assertEqual(result.stdout.strip(), "plain")

    def test_the_note_uses_seconds_or_minutes(self):
        self.assertIn("30 seconds", common.timeout_note(30))
        self.assertIn("90 minutes", common.timeout_note(5400))
        self.assertIn("1.5 minutes", common.timeout_note(90))


class LimitTests(unittest.TestCase):
    def limit(self, kind, **config):
        with patch.object(runner, "PROJECT_CONFIG", SimpleNamespace(**config)):
            return runner.command_timeout(kind)

    def test_defaults_are_long_enough_for_big_builds_and_overrides_win(self):
        self.assertEqual(self.limit("build"), runner.DEFAULT_BUILD_TIMEOUT)
        self.assertEqual(self.limit("test"), runner.DEFAULT_TEST_TIMEOUT)
        self.assertEqual(self.limit("build", build_timeout=7200), 7200)
        self.assertEqual(self.limit("test", test_timeout=120), 120)

    def test_zero_means_no_limit_and_nonsense_falls_back_to_the_default(self):
        self.assertIsNone(self.limit("test", test_timeout=0))
        for bad in (-5, "soon", True, None, 1.5):
            self.assertEqual(self.limit("test", test_timeout=bad), runner.DEFAULT_TEST_TIMEOUT, bad)
        with patch.object(runner, "PROJECT_CONFIG", MagicMock()):  # tests that mock the whole config
            self.assertEqual(runner.command_timeout("build"), runner.DEFAULT_BUILD_TIMEOUT)

    def test_bad_values_are_reported_by_check_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok = make_config(Path(tmp), build_command="make", test_command="make test", xcode_project=None, scheme=None, test_target="")
            self.assertEqual(validate_project_config(ok), [])
            for bad in (-1, "soon", True, 1.5):
                config = make_config(Path(tmp), build_command="make", test_command="make test", xcode_project=None, scheme=None,
                                     test_target="", test_timeout=bad)
                self.assertTrue(any("test_timeout must be a whole number of seconds" in e for e in validate_project_config(config)), bad)


class PipelineTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "output"
        self.summary = self.root / "summary.md"
        for target, value in (("get_best_simulator_destination", "id=X"),):
            patcher = patch.object(runner, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_pipeline(self, build, test, **patches):
        job = {"job_id": "j"}
        with patch.object(runner, "ROOT", self.root), patch.object(runner, "OUTPUT_DIR", self.out), \
             patch.object(runner, "extract_commands", return_value=(build, test)), \
             patch.object(runner, "command_timeout", side_effect=lambda kind: 1), \
             patch.object(runner, "analyze_failure", side_effect=AssertionError("a hang is not sent to the AI")):
            return runner.run_build_and_tests(job, self.summary)

    def test_a_hung_build_is_stopped_once_and_explained_without_running_it_again(self):
        started = time.time()
        result = self.run_pipeline(f"echo building; sleep 30", "true")
        self.assertEqual(result, (False, False))
        self.assertLess(time.time() - started, 12)  # one 1-second wait, not two, and not 30
        log = (self.out / "j" / "build.log").read_text()
        self.assertIn("didn't finish", log)
        self.assertIn("build_timeout", log)
        self.assertTrue(list((self.out / "j").glob("build_analysis_*.md")))

    def test_hung_tests_are_stopped_and_the_build_still_counts_as_passed(self):
        result = self.run_pipeline("true", "echo starting; sleep 30")
        self.assertEqual(result, (True, False))
        log = (self.out / "j" / "test.log").read_text()
        self.assertIn("The tests didn't finish", log)
        self.assertIn("Stopped after 1 seconds", log)
        self.assertIn("test_timeout", log)
        self.assertIn("starting", log)  # what the tests printed before they hung
        self.assertTrue(list((self.out / "j").glob("test_analysis_*.md")))

    def test_commands_that_finish_are_not_affected(self):
        self.assertEqual(self.run_pipeline("true", "echo 1 passed"), (True, True))


if __name__ == "__main__":
    unittest.main()
