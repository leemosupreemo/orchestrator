from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "orchestrator" / "scripts"))

from orchestrator import test_commands as tc  # noqa: E402
import run_build_and_tests as runner  # noqa: E402


def entry(name="unit", command="true", **extra):
    return {"name": name, "command": command, **extra}


class ParseTests(unittest.TestCase):
    def test_a_valid_entry_gets_defaults(self):
        commands, errors = tc.parse([entry()])
        self.assertEqual(errors, [])
        self.assertEqual(commands, [tc.ExtraTestCommand("unit", "true", ".", tc.DEFAULT_TIMEOUT, 0, "per-job", ())])

    def test_empty_values_mean_no_commands(self):
        for raw in (None, [], ()):
            self.assertEqual(tc.parse(raw), ([], []))

    def test_every_problem_is_reported_and_bad_entries_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            commands, errors = tc.parse([
                entry("ok"),
                entry("ok"),                                   # duplicate name
                {"command": "true"},                           # no name
                entry("empty", ""),                            # no command
                entry("bad name!"),
                entry("far", cwd="../outside"),
                entry("slow", timeout=0),
                entry("loop", retries=9),
                entry("tier", tier="sometimes"),
                entry("req", requires=["docker:yes", "path:", "gpu"]),
                entry("reqtype", requires=["docker", 5]),
                "not an object",
            ], root)
        self.assertEqual([c.name for c in commands], ["ok"])
        text = "\n".join(errors)
        for fragment in ("name is used twice", "needs a name", "needs a command", "name may only use", "outside the project",
                         "timeout must be", "retries must be", "tier must be one of", 'requirement "docker" takes no value',
                         'needs a value', 'unknown requirement "gpu"', "requires must be a list of strings", "must be an object"):
            self.assertIn(fragment, text)

    def test_not_a_list(self):
        self.assertEqual(tc.parse("dotnet test")[1], ["test_commands must be a list."])

    def test_backend_test_command_becomes_a_command_named_backend(self):
        config = SimpleNamespace(root=None, test_commands=[entry()], backend_test_command="pytest server")
        commands, errors = tc.from_config(config)
        self.assertEqual([c.name for c in commands], ["unit", "backend"])
        self.assertEqual(errors, [])
        clash = SimpleNamespace(root=None, test_commands=[entry("backend")], backend_test_command="pytest server")
        self.assertIn("rename the entry", tc.from_config(clash)[1][0])
        self.assertEqual(tc.from_config(SimpleNamespace(root=None, test_commands=[], backend_test_command=None)), ([], []))


class RequirementTests(unittest.TestCase):
    def test_each_requirement_says_what_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "have.txt").write_text("x")
            missing = tc.unmet_requirements(
                ["docker", "container:db", "command:dotnet", "path:have.txt", "path:.local/baseline", "env:TOKEN", "env:SET"], root,
                which=lambda name: None, docker=lambda: False, container=lambda name: False,
                environ={"SET": "1", "TOKEN": ""})
            self.assertEqual(len(missing), 5)  # have.txt exists and SET is set
            self.assertIn("Docker isn't running", missing[0])
            self.assertIn("db container isn't running", missing[1])
            self.assertIn("dotnet isn't installed", missing[2])
            self.assertIn(".local/baseline doesn't exist", missing[3])
            self.assertIn("TOKEN isn't set", missing[4])
            self.assertEqual(tc.unmet_requirements(
                ["docker", "container:db", "command:dotnet", "path:have.txt", "env:SET"], root,
                which=lambda name: "/usr/bin/" + name, docker=lambda: True, container=lambda name: name == "db",
                environ={"SET": "1"}), [])


class RunOneTests(unittest.TestCase):
    ROOT = Path("/project")

    def run_one(self, outcomes, **extra):
        """`outcomes`: (exit code, output, timed out) for each attempt."""
        calls = iter(outcomes)
        command = tc.ExtraTestCommand("unit", "run-tests", **extra)
        return tc.run_one(command, self.ROOT, run=lambda *_: next(calls), requirements=lambda r, p: [])

    def test_pass(self):
        result = self.run_one([(0, "ok", False)])
        self.assertEqual((result["status"], result["attempts"], result["flaky"]), ("passed", 1, False))

    def test_failure_keeps_the_last_output_and_exit_code(self):
        result = self.run_one([(1, "boom", False)])
        self.assertEqual((result["status"], result["exit_code"], result["output"]), ("failed", 1, "boom"))

    def test_a_pass_after_a_retry_is_flaky(self):
        result = self.run_one([(1, "first", False), (0, "second", False)], retries=2)
        self.assertEqual((result["status"], result["attempts"], result["flaky"], result["output"]), ("passed", 2, True, "second"))

    def test_retries_run_out(self):
        result = self.run_one([(1, "a", False), (1, "b", False)], retries=1)
        self.assertEqual((result["status"], result["attempts"], result["output"]), ("failed", 2, "b"))

    def test_timeout(self):
        result = self.run_one([(124, "partial", True)], timeout=5)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["reason"], "stopped after 5s")

    def test_unmet_requirements_block_without_running(self):
        def never(*_):
            raise AssertionError("must not run")
        command = tc.ExtraTestCommand("server", "run", requires=("docker",))
        result = tc.run_one(command, self.ROOT, run=never, requirements=lambda r, p: ["Docker isn't running"])
        self.assertEqual((result["status"], result["reason"], result["attempts"]), ("blocked", "Docker isn't running", 0))


class RunAllTests(unittest.TestCase):
    def test_runs_one_tier_writes_logs_and_a_summary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            commands = [tc.ExtraTestCommand("a", "echo a"), tc.ExtraTestCommand("b", "exit 3"),
                        tc.ExtraTestCommand("slow", "sleep 99", tier="manual")]
            results = tc.run_all(commands, Path(tmp), out)
            self.assertEqual([(r["name"], r["status"]) for r in results], [("a", "passed"), ("b", "failed")])
            self.assertEqual((out / "test_a.log").read_text().strip(), "a")
            saved = json.loads((out / "test_commands.json").read_text())
            self.assertEqual([s["name"] for s in saved], ["a", "b"])
            self.assertNotIn("output", saved[0])
            self.assertFalse(tc.passed(results))
            self.assertIn("b: FAILED (exit 3)", tc.summary(results))
            self.assertIn("=== b (failed): exit 3", tc.failure_report(results))

    def test_blocked_does_not_fail_the_run(self):
        results = [{"name": "s", "status": "blocked", "reason": "Docker isn't running", "seconds": 0, "flaky": False,
                    "output": "", "command": "x"}]
        self.assertTrue(tc.passed(results))
        self.assertIn("blocked: Docker isn't running", tc.summary(results))
        self.assertEqual(tc.failure_report(results), "")

    def test_runs_in_the_configured_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            results = tc.run_all([tc.ExtraTestCommand("where", "pwd", cwd="sub")], root, root / "out")
            self.assertEqual(Path(results[0]["output"].strip()).resolve(), (root / "sub").resolve())


class ExecuteTests(unittest.TestCase):
    def test_a_timeout_kills_the_command_and_its_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child-survived"
            # the child would write the marker after 3s if it were left running
            command = f"(sleep 3; touch {marker}) & echo started; sleep 30"
            began = time.monotonic()
            code, output, timed_out = tc.execute(command, Path(tmp), 1)
            self.assertTrue(timed_out)
            self.assertEqual(code, tc.TIMEOUT_EXIT_CODE)
            self.assertIn("started", output)
            self.assertLess(time.monotonic() - began, 10)
            time.sleep(3.5)
            self.assertFalse(marker.exists())

    def test_exit_code_and_combined_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output, timed_out = tc.execute("echo out; echo err >&2; exit 4", Path(tmp), 10)
            self.assertEqual((code, timed_out), (4, False))
            self.assertIn("out", output)
            self.assertIn("err", output)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "output"
        self.addCleanup(self.tmp.cleanup)

    def config(self, **fields):
        return SimpleNamespace(root=self.root, test_commands=[], backend_test_command=None, **fields)

    def run_pipeline(self, config, main=(True, True), job=None):
        with patch.object(runner, "ROOT", self.root), patch.object(runner, "OUTPUT_DIR", self.out), \
             patch.object(runner, "PROJECT_CONFIG", config), \
             patch.object(runner, "run_main_build_and_tests", return_value=main) as main_run, \
             patch.object(runner, "analyze_failure", return_value="analysis text") as analyze:
            result = runner.run_build_and_tests(job or {"job_id": "j"}, self.root / "summary.md")
        return result, main_run, analyze

    def test_without_extra_commands_nothing_changes(self):
        result, main_run, analyze = self.run_pipeline(self.config())
        self.assertEqual(result, (True, True))
        main_run.assert_called_once()
        analyze.assert_not_called()
        self.assertFalse((self.out / "j" / "test_commands.json").exists())

    def test_extra_commands_run_after_the_main_tests_pass(self):
        config = self.config()
        config.test_commands.append(entry("extra", "echo hello"))
        result, _, _ = self.run_pipeline(config)
        self.assertEqual(result, (True, True))
        self.assertEqual((self.out / "j" / "test_extra.log").read_text().strip(), "hello")

    def test_a_failing_extra_command_fails_the_tests_and_feeds_test_log(self):
        config = self.config()
        config.test_commands.append(entry("extra", "echo bad news; exit 1"))
        result, _, analyze = self.run_pipeline(config)
        self.assertEqual(result, (True, False))
        self.assertIn("bad news", (self.out / "j" / "test.log").read_text())
        self.assertIn("=== extra (failed)", analyze.call_args.args[1])
        self.assertTrue(list((self.out / "j").glob("test_analysis_*.md")))

    def test_extra_commands_are_skipped_when_the_main_run_fails(self):
        config = self.config()
        config.test_commands.append(entry("extra", "touch ran"))
        for main in ((False, False), (True, False)):
            result, _, _ = self.run_pipeline(config, main=main)
            self.assertEqual(result, main)
        self.assertFalse((self.root / "ran").exists())

    def test_extra_commands_are_skipped_when_the_job_says_no_tests_apply(self):
        config = self.config()
        config.test_commands.append(entry("extra", "touch ran"))
        result, _, _ = self.run_pipeline(config, job={"job_id": "j", "test_command_override": "N/A"})
        self.assertEqual(result, (True, True))
        self.assertFalse((self.root / "ran").exists())

    def test_manual_commands_do_not_run_with_a_job_and_a_blocked_one_does_not_fail_it(self):
        config = self.config()
        config.test_commands += [entry("manual", "touch ran", tier="manual"),
                                 entry("server", "touch ran", requires=["path:.local/missing"])]
        result, _, _ = self.run_pipeline(config)
        self.assertEqual(result, (True, True))
        self.assertFalse((self.root / "ran").exists())
        saved = json.loads((self.out / "j" / "test_commands.json").read_text())
        self.assertEqual([(s["name"], s["status"]) for s in saved], [("server", "blocked")])

    def test_backend_test_command_runs_as_backend(self):
        config = self.config()
        config.backend_test_command = "echo backend ok"
        result, _, _ = self.run_pipeline(config)
        self.assertEqual(result, (True, True))
        self.assertEqual((self.out / "j" / "test_backend.log").read_text().strip(), "backend ok")


if __name__ == "__main__":
    unittest.main()
