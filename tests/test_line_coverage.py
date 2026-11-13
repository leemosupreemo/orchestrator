from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import line_coverage as lc  # noqa: E402
from test_test_groups import write  # noqa: E402


def cobertura(root: Path, files: dict[str, list[tuple[int, int]]], relative: bool = False) -> str:
    classes = ""
    for name, lines in files.items():
        filename = name if relative else str(root / name)
        body = "".join(f'<line number="{n}" hits="{h}" branch="False"/>' for n, h in lines)
        classes += f'<class name="C" filename="{filename}"><lines>{body}</lines></class>'
    sources = f"<sources><source>{root}/</source></sources>" if relative else ""
    return f'<?xml version="1.0"?><coverage>{sources}<packages><package name="P"><classes>{classes}</classes></package></packages></coverage>'


class ParserTests(unittest.TestCase):
    def test_cobertura_gives_covered_and_coverable_lines_per_project_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            xml = cobertura(root, {"lib/A.cs": [(1, 3), (2, 0), (3, 1)], "lib/B.cs": [(1, 0)]})
            got = lc.parse_cobertura(xml, root)
            self.assertEqual(got["lib/A.cs"], ({1, 3}, {1, 2, 3}))
            self.assertEqual(got["lib/B.cs"], (set(), {1}))

    def test_relative_filenames_resolve_against_the_report_sources_and_outside_files_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(set(lc.parse_cobertura(cobertura(root, {"lib/A.cs": [(1, 1)]}, relative=True), root)), {"lib/A.cs"})
            outside = cobertura(Path("/somewhere/else"), {"X.cs": [(1, 1)]})
            self.assertEqual(lc.parse_cobertura(outside, root), {})
            self.assertEqual(lc.parse_cobertura("<!DOCTYPE x><coverage/>", root), {})
            self.assertEqual(lc.parse_cobertura("not xml", root), {})

    def test_coverage_json_is_read_per_file_and_files_outside_the_project_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = {"files": {"tools/a.py": {"executed_lines": [1, 2], "missing_lines": [3]},
                              str(root / "tools/b.py"): {"executed_lines": [], "missing_lines": [1, 2]},
                              "/usr/lib/python3/x.py": {"executed_lines": [1], "missing_lines": []}}}
            got = lc.parse_coverage_json(json.dumps(data), root)
            self.assertEqual(got, {"tools/a.py": ({1, 2}, {1, 2, 3}), "tools/b.py": (set(), {1, 2})})
            self.assertEqual(lc.parse_coverage_json("nope", root), {})

    def test_a_line_any_group_executed_counts_as_covered(self):
        total = {"a.py": ({1}, {1, 2, 3})}
        lc.merge(total, {"a.py": ({2}, {1, 2, 3}), "b.py": (set(), {1})})
        self.assertEqual(total, {"a.py": ({1, 2}, {1, 2, 3}), "b.py": (set(), {1})})


class MeasureTests(unittest.TestCase):
    def dotnet_group(self):
        return {"id": "xunit-t", "framework": "xUnit", "folder": "tests/T", "project": "tests/T/T.csproj", "runner": "dotnet", "toggle": True}

    def fake_dotnet(self, root, files=None, code=0, output=""):
        calls = []

        def run(command, cwd, timeout):
            calls.append(command)
            match = re.search(r"--results-directory (\S+)", command)
            if match and files is not None:
                out = Path(match.group(1).strip("'"))
                (out / "guid").mkdir(parents=True, exist_ok=True)
                (out / "guid" / "coverage.cobertura.xml").write_text(cobertura(root, files), encoding="utf-8")
            return code, output, False

        run.calls = calls
        return run

    def test_dotnet_runs_the_project_with_coverlet_and_reads_its_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self.fake_dotnet(root, {"lib/A.cs": [(1, 1), (2, 0)]})
            result = lc.measure_group(self.dotnet_group(), root, run=run, which=lambda n: "/usr/bin/dotnet")
            self.assertEqual((result["status"], result["files"]["lib/A.cs"]), ("ok", ({1}, {1, 2})))
            self.assertIn('--collect:"XPlat Code Coverage"', run.calls[0])
            self.assertIn('dotnet test "tests/T/T.csproj"', run.calls[0])

    def test_a_missing_collector_or_a_build_failure_says_what_to_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = lc.measure_group(self.dotnet_group(), root, run=self.fake_dotnet(root, files=None), which=lambda n: "/x")
            self.assertEqual(result["status"], "needs-setup")
            self.assertIn("dotnet add package coverlet.collector", result["reason"])
            failed = lc.measure_group(self.dotnet_group(), root, run=self.fake_dotnet(root, None, 1, "x.csproj : error MSB1: bad [x.csproj]"), which=lambda n: "/x")
            self.assertEqual((failed["status"], failed["reason"]), ("needs-setup", "bad"))
            self.assertEqual(lc.measure_group(self.dotnet_group(), root, which=lambda n: None)["reason"], "dotnet isn't installed or isn't on PATH")

    def test_other_frameworks_are_unsupported(self):
        self.assertEqual(lc.measure_group({"runner": None, "toggle": True}, Path("."))["status"], "unsupported")

    def test_a_real_python_group_is_measured_by_coverage_py_when_it_is_installed(self):
        import importlib.util
        if importlib.util.find_spec("coverage") is None:
            self.skipTest("coverage.py isn't installed here")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "lib/calc.py", "def add(a, b):\n    return a + b\n\ndef never():\n    return 1\n")
            write(root, "tests/test_calc.py", "import unittest\nfrom lib.calc import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(1, 2), 3)\n")
            group = {"id": "g", "framework": "unittest", "folder": "tests", "runner": "python", "toggle": True, "files": ["test_calc.py"], "isolate": False}
            result = lc.measure_group(group, root)
            self.assertEqual(result["status"], "ok", result)
            covered, every = result["files"]["lib/calc.py"]
            self.assertEqual((covered, every), ({1, 2, 4}, {1, 2, 4, 5}))  # both defs and add()'s body ran; never()'s body (line 5) did not
            self.assertTrue(result["tests_passed"])


class RollUpTests(unittest.TestCase):
    analysis = {"platforms": [{"name": "Shared", "paths": ["Game.Protocol"], "files": 4}, {"name": "Client", "paths": ["Unity"], "files": 9}]}

    def test_each_platform_gets_lines_covered_over_the_files_the_tests_loaded_and_tests_are_not_counted(self):
        measurement = {"at": 100.0, "files": {"Game.Protocol/A.cs": [9, 10], "Game.Protocol/B.cs": [1, 10], "Game.Protocol/Tests/T.cs": [5, 5], "elsewhere/x.cs": [1, 1]}}
        out = lc.apply(self.analysis, measurement)
        shared, client = out["platforms"]
        self.assertEqual(shared["measured"], {"covered": 10, "lines": 20, "files": 2, "percent": 50,
                                              "gaps": [{"path": "Game.Protocol/B.cs", "uncovered": 9, "lines": 10}, {"path": "Game.Protocol/A.cs", "uncovered": 1, "lines": 10}]})
        self.assertIsNone(client["measured"])
        self.assertEqual(out["measured_at"], 100.0)

    def test_nothing_measured_leaves_the_analysis_alone(self):
        self.assertIsNone(lc.apply(self.analysis, {})["measured_at"])

    def test_the_cache_round_trips_and_a_missing_one_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            lc.write_cache(Path(tmp), {"at": 1.0, "files": {"a.py": [1, 2]}})
            self.assertEqual(lc.read_cache(Path(tmp))["files"], {"a.py": [1, 2]})
        self.assertEqual(lc.read_cache(Path("/nonexistent")), {})


class MeasureAllTests(unittest.TestCase):
    def test_blocked_and_unmeasurable_groups_are_skipped_and_the_rest_combine(self):
        groups = [{"id": "a", "framework": "xUnit", "folder": "a", "runner": "dotnet", "project": "a.csproj", "toggle": True},
                  {"id": "b", "framework": "xUnit", "folder": "b", "runner": "dotnet", "project": "b.csproj", "toggle": True},
                  {"id": "c", "framework": "Go testing", "toggle": False}]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def run(command, cwd, timeout):
                match = re.search(r"--results-directory (\S+)", command)
                out = Path(match.group(1).strip("'"))
                out.mkdir(parents=True, exist_ok=True)
                (out / "coverage.cobertura.xml").write_text(cobertura(root, {"lib/A.cs": [(1, 1), (2, 1)]}), encoding="utf-8")
                return 0, "", False

            logged = []
            result = lc.measure(groups, root, blocked={"b"}, run=run, which=lambda n: "/x", log=logged.append)
            self.assertEqual(result["files"], {"lib/A.cs": [2, 2]})
            self.assertEqual(result["groups"]["b"]["status"], "skipped")
            self.assertNotIn("c", result["groups"])
            self.assertTrue(any("Skipping" in line for line in logged))


class OneCoverageButtonTests(unittest.TestCase):
    def test_the_overall_tool_is_chosen_in_the_order_the_measurement_tries_them(self):
        from orchestrator import coverage as tools
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(tools.chosen_tool(root, "dotnet test"))
            self.assertEqual(tools.chosen_tool(root, "python3 -m pytest"), "python")
            write(root, "pyproject.toml", "")
            self.assertEqual(tools.chosen_tool(root, ""), "python")
            write(root, "package.json", "{}")
            self.assertEqual(tools.chosen_tool(root, ""), "javascript")
            write(root, "Package.swift", "")
            self.assertEqual(tools.chosen_tool(root, ""), "swift")
            write(root, "Cargo.toml", "")
            self.assertEqual(tools.chosen_tool(root, ""), "rust")
            write(root, "go.mod", "")
            self.assertEqual(tools.chosen_tool(root, ""), "go")

    def test_the_coverage_action_measures_by_platform_and_does_not_run_the_same_python_tests_twice(self):
        import importlib.util
        import os
        import subprocess
        if importlib.util.find_spec("coverage") is None:
            self.skipTest("coverage.py isn't installed here")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "lib/calc.py", "def add(a, b):\n    return a + b\n\ndef never():\n    return 1\n")
            write(root, "tests/test_calc.py", "import unittest\nfrom lib.calc import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(1, 2), 3)\n")
            write(root, "pyproject.toml", "[project]\nname = 'x'\n")
            write(root, ".orchestrator/project.json", json.dumps({"project_name": "P", "build_command": "b", "test_command": "python3 -m unittest discover -s tests"}))
            env = {**os.environ, "ORCHESTRATOR_PROJECT_ROOT": str(root), "PYTHONPATH": str(PACKAGE_ROOT)}
            done = subprocess.run([sys.executable, "-m", "orchestrator", "script", "job_actions.py", "coverage"], cwd=root, env=env,
                                  capture_output=True, text=True, timeout=240)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("Saved line coverage for", done.stdout)
            self.assertIn("comes from the per-platform run above", done.stdout)  # the overall run was skipped, not repeated
            cache = json.loads((root / ".orchestrator/output/line-coverage.json").read_text())
            self.assertEqual(cache["files"]["lib/calc.py"], [3, 4])  # both defs and add()'s body ran; never()'s body didn't


if __name__ == "__main__":
    unittest.main()
