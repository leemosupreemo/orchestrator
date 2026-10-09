from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator import coverage as cov


class FakeRun:
    """Answers each command by its first words and writes the report the real tool would."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, cwd):
        self.calls.append(argv)
        for prefix, (code, stdout, write) in self.answers.items():
            if " ".join(argv).startswith(prefix):
                if write:
                    write(argv)
                return subprocess.CompletedProcess(argv, code, stdout, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def arg_value(argv, flag):
    for a in argv:
        if a.startswith(flag):
            return a.split("=", 1)[1]
    return argv[argv.index(flag.rstrip("=")) + 1]


class CoverageTests(unittest.TestCase):
    def project(self, *files, package=None):
        root = Path(tempfile.mkdtemp())
        for f in files:
            (root / f).write_text("")
        if package is not None:
            (root / "package.json").write_text(json.dumps(package))
        return root

    def test_python_uses_coverage_py_around_the_projects_own_test_command(self):
        root = self.project("pyproject.toml")
        py = sys.executable
        run = FakeRun({f"{py} -m coverage json": (0, "", lambda a: Path(a[-1]).write_text(json.dumps({"totals": {"percent_covered": 81.234}})))})
        m = cov.measure(root, "python3 -m unittest discover tests", run)
        self.assertEqual((m.pct, m.tool, m.metric), (81.2, "coverage.py", "statements"))
        self.assertIn([py, "-m", "coverage", "run", "-m", "unittest", "discover", "tests"], run.calls)
        pytest = FakeRun({f"{py} -m coverage json": (0, "", lambda a: Path(a[-1]).write_text(json.dumps({"totals": {"percent_covered": 50}})))})
        cov.measure(root, "pytest -q", pytest)
        self.assertIn([py, "-m", "coverage", "run", "-m", "pytest", "-q"], pytest.calls)

    def test_python_auto_installs_coverage_py_when_not_installed(self):
        root = self.project("pyproject.toml")
        py = sys.executable
        installed = [False]
        calls = []

        def custom_run(argv, cwd):
            calls.append(argv)
            cmd = " ".join(argv)
            if cmd == f"{py} -m coverage --version":
                return subprocess.CompletedProcess(argv, 0 if installed[0] else 1, "", "")
            if cmd == f"{py} -m pip install coverage":
                installed[0] = True
                return subprocess.CompletedProcess(argv, 0, "Successfully installed coverage", "")
            if cmd.startswith(f"{py} -m coverage json"):
                Path(argv[-1]).write_text(json.dumps({"totals": {"percent_covered": 92.5}}))
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        m = cov.measure(root, "pytest", custom_run)
        self.assertEqual((m.pct, m.tool), (92.5, "coverage.py"))
        self.assertTrue(installed[0])
        self.assertIn([py, "-m", "pip", "install", "coverage"], calls)

    def test_a_missing_tool_says_how_to_get_it_instead_of_guessing(self):
        py = sys.executable
        with self.assertRaisesRegex(cov.CoverageUnavailable, "pip install coverage"):
            cov.measure(self.project("pyproject.toml"), "pytest", FakeRun({f"{py} -m coverage --version": (1, "", None)}))
        with self.assertRaisesRegex(cov.CoverageUnavailable, "cargo install cargo-llvm-cov"):
            cov.measure(self.project("Cargo.toml"), "cargo test", FakeRun({"cargo llvm-cov --version": (1, "", None)}))
        with self.assertRaisesRegex(cov.CoverageUnavailable, "@vitest/coverage-v8"):
            cov.measure(self.project(package={"devDependencies": {"vitest": "1"}}), "npm test", FakeRun({}))
        with self.assertRaisesRegex(cov.CoverageUnavailable, "Jest and Vitest"):
            cov.measure(self.project(package={"devDependencies": {"mocha": "1"}}), "npm test", FakeRun({}))
        with self.assertRaisesRegex(cov.CoverageUnavailable, "JaCoCo"):
            cov.measure(self.project("build.gradle.kts"), "./gradlew test", FakeRun({}))

    def test_failing_tests_record_no_number(self):
        with self.assertRaisesRegex(cov.CoverageUnavailable, "tests failed"):
            cov.measure(self.project("go.mod"), "go test ./...", FakeRun({"go test": (1, "", None)}))

    def test_go_rust_swift_and_jest_read_their_tools_totals(self):
        go = FakeRun({"go tool cover": (0, "a.go:1:\tF\t100.0%\ntotal:\t\t\t(statements)\t67.5%\n", None)})
        self.assertEqual(cov.measure(self.project("go.mod"), "go test ./...", go).pct, 67.5)

        llvm = {"data": [{"totals": {"lines": {"percent": 72.06}}}]}
        rust = FakeRun({"cargo llvm-cov --json": (0, "", lambda a: Path(a[-1]).write_text(json.dumps(llvm)))})
        self.assertEqual((cov.measure(self.project("Cargo.toml"), "cargo test", rust).pct), 72.1)

        root = self.project("Package.swift")
        report = root / "codecov.json"
        report.write_text(json.dumps(llvm))
        swift = FakeRun({"swift test --show-codecov-path": (0, f"{report}\n", None)})
        self.assertEqual(cov.measure(root, "swift test", swift).tool, "swift test (llvm-cov)")

        summary = {"total": {"lines": {"pct": 90}}}
        jest = FakeRun({"npx jest": (0, "", lambda a: (Path(arg_value(a, "--coverageDirectory=")) / "coverage-summary.json").write_text(json.dumps(summary)))})
        m = cov.measure(self.project(package={"devDependencies": {"jest": "29"}}), "npm test", jest)
        self.assertEqual((m.pct, m.tool, m.metric), (90.0, "Jest", "lines"))

    def test_total_lines_and_covered_lines_extracted_when_present(self):
        py = sys.executable
        root = self.project("pyproject.toml")
        run = FakeRun({f"{py} -m coverage json": (0, "", lambda a: Path(a[-1]).write_text(json.dumps({"totals": {"percent_covered": 85.0, "num_statements": 1200, "covered_lines": 1020}})))})
        m = cov.measure(root, "pytest -q", run)
        self.assertEqual(m.total_lines, 1200)
        self.assertEqual(m.covered_lines, 1020)

        llvm = {"data": [{"totals": {"lines": {"percent": 72.06, "count": 2500, "covered": 1800}}}]}
        rust = FakeRun({"cargo llvm-cov --json": (0, "", lambda a: Path(a[-1]).write_text(json.dumps(llvm)))})
        m_rust = cov.measure(self.project("Cargo.toml"), "cargo test", rust)
        self.assertEqual(m_rust.total_lines, 2500)
        self.assertEqual(m_rust.covered_lines, 1800)

    def test_count_source_lines_helper(self):
        root = self.project()
        (root / "a.swift").write_text("let a = 1\nlet b = 2\n")
        (root / "b.py").write_text("print('hello')\n")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "x.js").write_text("ignore = 1\n")
        self.assertEqual(cov.count_source_lines(root), 3)


if __name__ == "__main__":
    unittest.main()
