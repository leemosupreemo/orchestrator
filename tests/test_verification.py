import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator import test_commands
from orchestrator.verification import command_digest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"))
import run_build_and_tests as runner


def command(name, **fields):
    return {"name": name, "command": f"echo {name}", **fields}


def config(entries, mode="observe"):
    return SimpleNamespace(test_commands=entries, verification_mode=mode, test_command="echo main")


class VerificationTests(unittest.TestCase):
    def test_digest_changes_when_the_effective_command_changes(self):
        raw = command("first", parallel_verify=True)
        first = test_commands.parse([raw])[0][0]
        same = test_commands.parse([{**raw, "timeout": test_commands.DEFAULT_TIMEOUT}])[0][0]
        changed = test_commands.parse([{**raw, "requires": ["command:dotnet"]}])[0][0]
        self.assertEqual(command_digest(first), command_digest(same))
        self.assertNotEqual(command_digest(first), command_digest(changed))

    def test_off_does_not_record_measurements(self):
        entries = [command(name, parallel_verify=True) for name in ("first", "second")]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.multiple(runner, ROOT=root, OUTPUT_DIR=root, PROJECT_CONFIG=config(entries, "off")):
                self.assertTrue(runner.run_extra_test_commands({"job_id": "job"}))
            self.assertFalse((root / "job/verification").exists())

    def test_editing_a_command_preserves_observation_opt_in(self):
        entries, errors = test_commands.with_entry(
            [command("first", parallel_verify=True)], command("renamed"), "first", Path("."),
        )
        self.assertEqual(errors, [])
        self.assertTrue(entries[0]["parallel_verify"])

    def test_configuration_requires_two_adjacent_per_job_checks(self):
        first = command("first", parallel_verify=True)
        second = command("second", parallel_verify=True)
        checks, errors = test_commands.from_config(config([first, second]))
        self.assertEqual(errors, [])
        self.assertTrue(all(check.parallel_verify for check in checks))
        for entries in ([first], [first, command("middle"), second],
                        [first, {**second, "tier": "nightly"}],
                        [first, {**second, "parallel_verify": "yes"}],
                        [first, first], [first, {**second, "command": "echo main"}]):
            with self.subTest(entries=entries):
                self.assertTrue(test_commands.from_config(config(entries))[1])

    def test_observe_preserves_order_and_blocked_semantics(self):
        entries = [command("before"), command("first", parallel_verify=True, requires=["command:no-such-verification-tool"]),
                   command("second", parallel_verify=True), command("after")]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.multiple(runner, ROOT=root, OUTPUT_DIR=root, PROJECT_CONFIG=config(entries)):
                for _ in range(2):
                    self.assertTrue(runner.run_extra_test_commands({"job_id": "job"}))
            results = json.loads((root / "job/test_commands.json").read_text())
            self.assertEqual([result["name"] for result in results], ["before", "first", "second", "after"])
            records = [json.loads(path.read_text()) for path in (root / "job/verification").glob("*.json")]
            self.assertEqual(len(records), 2)
            self.assertEqual(len({record["invocation_id"] for record in records}), 2)
            for record in records:
                self.assertEqual([check["status"] for check in record["checks"]], ["blocked", "passed"])
                self.assertIn("no-such-verification-tool", record["checks"][0]["reason"])
                self.assertGreaterEqual(record["elapsed_seconds"], 0)

    def test_main_failure_and_skip_never_enter_verification(self):
        for verdict, override in (((False, True), ""), ((True, False), ""), ((True, True), "none")):
            with patch.object(runner, "run_main_build_and_tests", return_value=verdict), \
                 patch.object(runner, "run_extra_test_commands") as extra:
                self.assertEqual(runner.run_build_and_tests({"test_command_override": override}, Path("summary")), verdict)
                extra.assert_not_called()

    def test_assist_fails_when_required_check_is_blocked_without_a_helper(self):
        entries = [command("first", parallel_verify=True, requires=["command:no-such-verification-tool"]),
                   command("second", parallel_verify=True)]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = config(entries, "assist")
            settings.runtime_dir = root
            with patch.multiple(runner, ROOT=root, OUTPUT_DIR=root, PROJECT_CONFIG=settings), \
                 patch.object(runner, "analyze_failure", return_value="analysis"):
                self.assertFalse(runner.run_extra_test_commands({"job_id": "job", "allowed_machines": ["local"]}))
            self.assertIn("no-such-verification-tool", (root / "job/test.log").read_text())

    def test_assist_only_requires_opted_in_checks(self):
        entries = [command("first", parallel_verify=True), command("second", parallel_verify=True),
                   command("ordinary", requires=["command:no-such-verification-tool"])]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings = config(entries, "assist")
            settings.runtime_dir = root
            with patch.multiple(runner, ROOT=root, OUTPUT_DIR=root, PROJECT_CONFIG=settings):
                self.assertTrue(runner.run_extra_test_commands({"job_id": "job", "allowed_machines": ["local"]}))

    def test_assist_requires_its_pair_even_if_builder_skips_main_tests(self):
        with patch.object(runner, "PROJECT_CONFIG", config([], "assist")), \
             patch.object(runner, "run_main_build_and_tests", return_value=(True, True)), \
             patch.object(runner, "run_extra_test_commands", return_value=False) as extra:
            self.assertEqual(runner.run_build_and_tests({"test_command_override": "none"}, Path("summary")), (True, False))
            extra.assert_called_once()
