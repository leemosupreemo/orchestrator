"""A builder that couldn't write anything must stop the job, and a test command for tests that don't exist must not be run as if it counted."""
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

from orchestrator import builder_check  # noqa: E402
import run_build_and_tests as runner  # noqa: E402
import run_builder  # noqa: E402

# The summary a real builder (Sonnet) returned in the DAoC-Mobile "Make vegetation more realistic" job.
REAL_BLOCKED = ("All five writes were blocked by the session's permission mode — no file on disk was changed. The implementation is fully "
                "specified above. The test file and four production edits must be applied once write permissions are granted.")


class WritesBlockedTests(unittest.TestCase):
    def test_the_real_report_is_recognised(self):
        self.assertTrue(builder_check.writes_blocked(REAL_BLOCKED))

    def test_other_ways_of_saying_it(self):
        for text in ("The edits were denied, so I could not apply them.", "I was unable to write files in this session.",
                     "Nothing was written to disk.", "No files were modified.", "Writes are blocked by the permission settings.",
                     "Every write was refused", "The tool was blocked by the session's permission mode"):
            with self.subTest(text=text):
                self.assertTrue(builder_check.writes_blocked(text))

    def test_a_normal_report_is_not_flagged(self):
        for text in ("Added SlopeAttenuation to ZoneFoliagePlacement and updated TreeWind.shader. Tests pass.",
                     '{"summary": "Edited 4 files and wrote tests/test_x.py", "test_command": "pytest tests/test_x.py"}', "", None):
            with self.subTest(text=text):
                self.assertFalse(builder_check.writes_blocked(text))


class TreeFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()  # a throwaway repository: never the live checkout
        self.root = Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        (self.root / "a.txt").write_text("one")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True, capture_output=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_moves_when_a_file_is_added_or_a_modified_one_is_edited_again(self):
        start = builder_check.tree_fingerprint(self.root)
        self.assertEqual(start, builder_check.tree_fingerprint(self.root))
        (self.root / "new.py").write_text("x")
        added = builder_check.tree_fingerprint(self.root)
        self.assertTrue(builder_check.changed(start, added))
        (self.root / "a.txt").write_text("two")
        modified = builder_check.tree_fingerprint(self.root)
        (self.root / "a.txt").write_text("three!")  # still " M a.txt": only the size says it changed again
        self.assertTrue(builder_check.changed(modified, builder_check.tree_fingerprint(self.root)))

    def test_the_orchestrators_own_files_do_not_count(self):
        start = builder_check.tree_fingerprint(self.root)
        (self.root / ".orchestrator").mkdir()
        (self.root / ".orchestrator" / "job.json").write_text("{}")
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "x").write_text("{}")
        self.assertFalse(builder_check.changed(start, builder_check.tree_fingerprint(self.root)))

    def test_not_a_repository_is_unknown_not_changed(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertIsNone(builder_check.tree_fingerprint(Path(plain)))
        self.assertFalse(builder_check.changed(None, "abc"))


class MissingTargetsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "tests" / "build_integrity").mkdir(parents=True)
        (self.root / "tests" / "build_integrity" / "test_water.py").write_text("")
        (self.root / "tests" / "test_top.py").write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_real_command_is_flagged(self):
        cmd = "python3 -m unittest discover -s tests/build_integrity -p test_vegetation_realism.py"
        self.assertEqual(builder_check.missing_test_targets(cmd, self.root), ["test_vegetation_realism.py (in tests/build_integrity)"])

    def test_existing_tests_are_fine(self):
        for cmd in ("python3 -m unittest discover -s tests/build_integrity -p test_water.py",
                    "python3 -m unittest discover -s tests -p 'test_*.py'", "python3 -m unittest discover -s tests/build_integrity -p test_water.py",
                    "python3 -m pytest tests/test_top.py", "pytest tests/test_top.py::Case::test_one -q", "python3 -m pytest tests/build_integrity",
                    "python3 -m unittest discover -s tests"):
            with self.subTest(cmd=cmd):
                self.assertEqual(builder_check.missing_test_targets(cmd, self.root), [])

    def test_a_missing_file_argument_is_flagged(self):
        self.assertEqual(builder_check.missing_test_targets("pytest tests/test_gone.py tests/test_top.py", self.root), ["tests/test_gone.py"])
        self.assertEqual(builder_check.missing_test_targets("python3 tests/gone.py", self.root), ["tests/gone.py"])

    def test_what_cannot_be_checked_reports_nothing(self):
        for cmd in ("cd sub && pytest gone.py", "pytest $TARGET/gone.py", 'pytest "unclosed', "", "xcodebuild test -only-testing:A/B"):
            with self.subTest(cmd=cmd):
                self.assertEqual(builder_check.missing_test_targets(cmd, self.root), [])


class NoTestsRanTests(unittest.TestCase):
    def test_each_runner_saying_so(self):
        for out in ("----\nRan 0 tests in 0.000s\n\nNO TESTS RAN", "no tests ran in 0.01s", "collected 0 items", "No tests found, exiting with code 1"):
            with self.subTest(out=out):
                self.assertTrue(builder_check.no_tests_ran(out))

    def test_a_run_with_tests_in_it_is_not(self):
        for out in ("Ran 3 tests in 0.1s\n\nOK", "Ran 0 tests in 0s\nRan 12 tests in 1s\nOK", "5 passed in 1s", "2 failed, 1 passed", ""):
            with self.subTest(out=out):
                self.assertFalse(builder_check.no_tests_ran(out))


class RunnerTests(unittest.TestCase):
    """run_build_and_tests with the shell stubbed out: which command is actually run, and what a run with no tests says."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.out = self.root / "output"
        for p in (patch.object(runner, "ROOT", self.root), patch.object(runner, "OUTPUT_DIR", self.out),
                  patch.object(runner, "get_best_simulator_destination", return_value="id=SIM"),
                  patch.object(runner, "extract_commands", return_value=("echo build", "python3 -m unittest discover -s tests"))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.ran: list[str] = []

    def shell(self, outputs):
        """A run_shell that answers each test command from `outputs` ({substring: (returncode, stdout)}) and passes the build."""
        def fake(cmd, cwd=None, check=False, capture=False, timeout=None):
            self.ran.append(cmd)
            for needle, (code, out) in outputs.items():
                if needle in cmd:
                    return subprocess.CompletedProcess(args=[cmd], returncode=code, stdout=out, stderr="")
            return subprocess.CompletedProcess(args=[cmd], returncode=0, stdout="", stderr="")
        return patch.object(runner, "run_shell", side_effect=fake)

    def test_an_override_naming_a_missing_file_is_not_run(self):
        job = {"job_id": "j1", "test_command_override": "python3 -m unittest discover -s tests/build_integrity -p test_vegetation_realism.py"}
        with self.shell({"discover -s tests": (0, "Ran 4 tests in 0.1s\n\nOK")}):
            build_ok, test_ok = runner.run_build_and_tests(job, self.root / "s.md")
        self.assertEqual((build_ok, test_ok), (True, True))
        self.assertFalse(any("test_vegetation_realism" in c for c in self.ran))
        self.assertIn("python3 -m unittest discover -s tests", self.ran)

    def test_an_override_that_runs_no_tests_falls_back_to_the_projects_own(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_there.py").write_text("")
        job = {"job_id": "j2", "test_command_override": "python3 -m unittest discover -s tests -p test_there.py"}
        outputs = {"-p test_there.py": (5, "Ran 0 tests in 0.000s\n\nNO TESTS RAN"), "discover -s tests": (0, "Ran 7 tests in 1s\n\nOK")}
        with self.shell(outputs):
            _build_ok, test_ok = runner.run_build_and_tests(job, self.root / "s.md")
        self.assertTrue(test_ok)
        self.assertEqual(self.ran[-1], "python3 -m unittest discover -s tests")  # the project's own, after the override found nothing

    def test_no_tests_at_all_is_reported_plainly_without_asking_a_model(self):
        job = {"job_id": "j3"}
        with self.shell({"discover": (5, "Ran 0 tests in 0.000s\n\nNO TESTS RAN")}), \
             patch.object(runner, "analyze_failure", side_effect=AssertionError("a model must not be asked to guess from an empty run")):
            build_ok, test_ok = runner.run_build_and_tests(job, self.root / "s.md")
        self.assertEqual((build_ok, test_ok), (True, False))
        analysis = next((self.out / "j3").glob("test_analysis_*.md")).read_text()
        self.assertIn("No tests ran", analysis)
        self.assertIn("python3 -m unittest discover -s tests", analysis)

    def test_a_real_failure_is_still_analysed(self):
        job = {"job_id": "j4"}
        with self.shell({"discover": (1, "Ran 3 tests in 0.1s\n\nFAILED (failures=1)")}), \
             patch.object(runner, "analyze_failure", return_value="the fix") as analyse:
            _build_ok, test_ok = runner.run_build_and_tests(job, self.root / "s.md")
        self.assertFalse(test_ok)
        analyse.assert_called_once()


class BuilderTests(unittest.TestCase):
    """run_builder with the model stubbed out: a builder that reports blocked writes stops the job, and resuming never reuses that report."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        (self.root / "a.txt").write_text("one")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True, capture_output=True)
        self.out = self.root / "out"
        self.job_path = self.root / "job.json"
        self.job_path.write_text(json.dumps({"job_id": "j", "type": "quick-fix", "title": "T", "plan": {"likely_files": []}}))
        self.llm_calls = 0
        self.llm_answer = REAL_BLOCKED
        self.build = MagicMock(return_value=(True, True))
        stubs = [patch.object(run_builder, "ROOT", self.root), patch.object(run_builder, "OUTPUT_DIR", self.out),
                 patch.object(run_builder, "StatusBar", MagicMock()), patch.object(run_builder, "print_phase"),
                 patch.object(run_builder, "make_brief", return_value="brief"), patch.object(run_builder, "run_build_and_tests", self.build),
                 patch.object(run_builder, "run_llm", side_effect=self.fake_llm), patch.object(run_builder, "resolve_builder_model", return_value="m"),
                 patch.object(run_builder.reuse_brief, "candidates", return_value=[]),
                 patch("generate_test_index.generate_test_index", return_value="")]
        for p in stubs:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def fake_llm(self, *args, **kwargs):
        self.llm_calls += 1
        if self.llm_answer is not REAL_BLOCKED:
            (self.root / "new_file.py").write_text("x")  # a builder that did write something
        return self.llm_answer, "model-x", "sess"

    def test_blocked_writes_stop_the_job_before_build_and_tests(self):
        with self.assertRaisesRegex(RuntimeError, "could not change any files"):
            run_builder.run_builder(self.job_path)
        self.build.assert_not_called()

    def test_a_report_of_blocked_writes_is_ignored_when_files_did_change(self):
        self.llm_answer = '{"summary": "Edits applied. The writes were blocked once, then retried and succeeded.", "test_command": "n/a"}'
        result = run_builder.run_builder(self.job_path)
        self.assertEqual(result[:2], (True, True))
        self.build.assert_called_once()

    def test_resuming_does_not_reuse_a_summary_from_a_run_that_wrote_nothing(self):
        summary = self.out / "j" / "builder_summary.md"
        summary.parent.mkdir(parents=True)
        summary.write_text(REAL_BLOCKED)
        self.llm_answer = '{"summary": "done", "test_command": "n/a"}'
        run_builder.run_builder(self.job_path, resume=True)
        self.assertEqual(self.llm_calls, 1)  # the builder ran again
        self.assertNotIn("blocked", summary.read_text())

    def test_resuming_still_reuses_a_good_summary(self):
        summary = self.out / "j" / "builder_summary.md"
        summary.parent.mkdir(parents=True)
        summary.write_text('{"summary": "all done", "test_command": "n/a"}')
        run_builder.run_builder(self.job_path, resume=True)
        self.assertEqual(self.llm_calls, 0)

    def test_unplanned_quick_job_gets_scoping_guardrail(self):
        captured: list[str] = []

        def capture_llm(model, prompt, *args, **kwargs):
            captured.append(prompt)
            return ('{"summary": "done", "test_command": "n/a"}', "model-x", "sess")

        with patch.object(run_builder, "run_llm", side_effect=capture_llm):
            run_builder.run_builder(self.job_path)
        self.assertEqual(len(captured), 1)
        self.assertIn("QUICK TASK SCOPING GUARDRAIL", captured[0])
        self.assertIn("wired into real runtime callers", captured[0])


class SourceOnlyAndIgnoredPathTests(unittest.TestCase):
    def test_is_ignored_tool_path_filters_agent_state_only(self):
        for p in (".claude/helpers/hook-handler.cjs", "./.orchestrator/jobs/1.json", ".worktrees/a/b.py"):
            self.assertTrue(builder_check.is_ignored_tool_path(p), p)
        for p in ("DaocUnity/Assets/Shaders/TreeWind.shader", "tests/test_x.py", "src/claude_client.py"):
            self.assertFalse(builder_check.is_ignored_tool_path(p), p)

    def test_source_only_test_files_flags_grep_style_and_allows_real_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests" / "build_integrity"
            tests_dir.mkdir(parents=True)
            grep_test = tests_dir / "test_vegetation_realism.py"
            grep_test.write_text(
                "import os, unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_has_prop(self):\n"
                "        with open('TreeWind.shader') as f:\n"
                "            self.assertIn('_GustFrequency', f.read())\n"
            )
            real_test = tests_dir / "test_real.py"
            real_test.write_text(
                "import unittest\n"
                "from myapp import wind\n"
                "class T(unittest.TestCase):\n"
                "    def test_gust(self):\n"
                "        self.assertGreater(wind.gust(1.0), 0)\n"
            )
            subproc_test = tests_dir / "test_cli.py"
            subproc_test.write_text(
                "import subprocess, unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_cli(self):\n"
                "        with open('README.md') as f:\n"
                "            self.assertTrue(f.read())\n"
                "        subprocess.run(['true'], check=True)\n"
            )
            self.assertEqual(
                builder_check.source_only_test_files("python3 -m unittest discover -s tests/build_integrity -p test_vegetation_realism.py", root),
                ["tests/build_integrity/test_vegetation_realism.py"],
            )
            self.assertEqual(builder_check.source_only_test_files("pytest tests/build_integrity/test_real.py", root), [])
            self.assertEqual(builder_check.source_only_test_files("pytest tests/build_integrity/test_cli.py", root), [])

    def test_source_only_override_falls_back_to_projects_own_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests" / "build_integrity"
            tests_dir.mkdir(parents=True)
            (tests_dir / "test_vegetation_realism.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_s(self):\n"
                "        with open('a.cs') as f:\n"
                "            self.assertIn('Slope', f.read())\n"
            )
            ran: list[str] = []

            def fake_shell(cmd, cwd=None, check=False, capture=False, timeout=None):
                ran.append(cmd)
                return subprocess.CompletedProcess(args=[cmd], returncode=0, stdout="Ran 4 tests\nOK", stderr="")

            job = {"job_id": "j-src", "test_command_override": "python3 -m unittest discover -s tests/build_integrity -p test_vegetation_realism.py"}
            with patch.object(runner, "ROOT", root), patch.object(runner, "OUTPUT_DIR", root / "out"), \
                 patch.object(runner, "get_best_simulator_destination", return_value="id=SIM"), \
                 patch.object(runner, "extract_commands", return_value=("echo build", "dotnet test")), \
                 patch.object(runner, "run_shell", side_effect=fake_shell):
                self.assertEqual(runner.run_build_and_tests(job, root / "s.md"), (True, True))
            self.assertIn("dotnet test", ran)
            self.assertFalse(any("test_vegetation_realism" in c for c in ran))


if __name__ == "__main__":
    unittest.main()
