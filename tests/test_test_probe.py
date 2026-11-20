from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import test_groups as tg, test_probe as tp  # noqa: E402
from test_test_groups import project, write  # noqa: E402
from test_web_ui import UI_HEADERS, ServerTestCase, ui  # noqa: E402

DOTNET_LIST = """\
Test run for /p/bin/Tests.dll (.NETCoreApp,Version=v10.0)
The following Tests are available:
    Ns.LoginTests.LogsIn
    Ns.CharacterTests.Combo(classId: 43, raceId: 11)
    Ns.CharacterTests.Combo(classId: 44, raceId: 12)
"""
DOTNET_FAIL = ("  Determining projects to restore...\n/p/Tests.csproj(9,5): error : Pass -p:PreparedServerSource=/absolute/path/from/"
               "tools/build-profile-server.py prepare\n")
PYTEST_LIST = "tests/api/test_b.py::test_y\ntests/api/test_b.py::TestK::test_z\n\n2 tests collected in 0.01s\n"


def fake(output: str, code: int = 0, timed_out: bool = False):
    calls = []

    def run(command, cwd, timeout):
        calls.append((command, cwd, timeout))
        return code, output, timed_out

    run.calls = calls
    return run


def group(**kw):
    base = {"id": "g", "framework": "xUnit", "folder": "tests/T", "project": "tests/T/T.csproj", "runner": "dotnet",
            "command": "x", "toggle": True, "tests": 1, "files": []}
    base.update(kw)
    return base


class ParserTests(unittest.TestCase):
    def test_dotnet_list_counts_every_expanded_test(self):
        self.assertEqual(len(tp.parse_dotnet(DOTNET_LIST)), 3)
        self.assertIsNone(tp.parse_dotnet("build failed"))

    def test_pytest_collection(self):
        self.assertEqual(tp.parse_pytest(PYTEST_LIST), ["tests/api/test_b.py::test_y", "tests/api/test_b.py::TestK::test_z"])
        self.assertIsNone(tp.parse_pytest("no tests ran"))

    def test_the_reason_is_what_the_tool_said_without_the_path_in_front(self):
        self.assertEqual(tp._first_error(DOTNET_FAIL), "Pass -p:PreparedServerSource=/absolute/path/from/tools/build-profile-server.py prepare")
        self.assertEqual(tp._first_error("E   ModuleNotFoundError: No module named 'x'"), "ModuleNotFoundError: No module named 'x'")
        self.assertEqual(tp._first_error("/p/Tests.csproj : error MSB4019: The imported project was not found. [/p/Tests.csproj]"),
                         "The imported project was not found.")


class ProbeTests(unittest.TestCase):
    clock = staticmethod(lambda: 0.0)

    def test_dotnet_ok_counts_the_runners_list_and_builds_but_runs_no_tests(self):
        run = fake(DOTNET_LIST)
        result = tp.probe(group(), Path("/p"), run=run, which=lambda n: "/usr/bin/dotnet", clock=self.clock)
        self.assertEqual((result["status"], result["tests"], result["how"]), ("ok", 3, "dotnet test --list-tests"))
        self.assertIn("--list-tests", run.calls[0][0])

    def test_something_missing_is_needs_setup_with_the_tools_own_reason(self):
        result = tp.probe(group(), Path("/p"), run=fake(DOTNET_FAIL, code=1), which=lambda n: "/x", clock=self.clock)
        self.assertEqual(result["status"], "needs-setup")
        self.assertIn("PreparedServerSource", result["reason"])
        self.assertEqual(tp.probe(group(), Path("/p"), run=fake(""), which=lambda n: None, clock=self.clock)["reason"],
                         "dotnet isn't installed or isn't on PATH")

    def test_a_timeout_is_an_error_not_a_verdict(self):
        result = tp.probe(group(), Path("/p"), run=fake("", code=124, timed_out=True), which=lambda n: "/x", clock=self.clock)
        self.assertEqual(result["status"], "error")

    def test_pytest_and_its_missing_module(self):
        pyt = group(runner="python", framework="pytest", folder="tests/api", files=["test_b.py"], isolate=False)
        self.assertEqual(tp.probe(pyt, Path("/p"), run=fake(PYTEST_LIST), clock=self.clock)["tests"], 2)
        missing = tp.probe(pyt, Path("/p"), run=fake("/usr/bin/python3: No module named pytest", code=1), clock=self.clock)
        self.assertEqual((missing["status"], missing["reason"]), ("needs-setup", "pytest isn't installed (pip install pytest)"))

    def test_unittest_names_the_modules_only_when_the_group_is_isolated(self):
        plain = group(runner="python", framework="unittest", folder="tests/t", files=["test_a.py"], isolate=False)
        isolated = {**plain, "isolate": True}
        out = json.dumps({"names": ["test_a.T.test_x"], "errors": []})
        run = fake(out)
        self.assertEqual(tp.probe(plain, Path("/p"), run=run, clock=self.clock)["tests"], 1)
        self.assertNotIn("test_a", run.calls[0][0].split("tests/t", 1)[1])
        run = fake(out)
        tp.probe(isolated, Path("/p"), run=run, clock=self.clock)
        self.assertTrue(run.calls[0][0].rstrip().endswith("tests/t test_a"))

    def test_files_that_cannot_be_imported_are_reported(self):
        bad = group(runner="python", framework="unittest", folder="t", files=[], isolate=False)
        out = json.dumps({"names": ["a.T.test_x"], "errors": ["ModuleNotFoundError: No module named 'tools'"]})
        result = tp.probe(bad, Path("/p"), run=fake(out), clock=self.clock)
        self.assertEqual(result["status"], "needs-setup")
        self.assertIn("1 test file(s) can't be imported", result["reason"])

    def test_other_frameworks_are_unsupported_and_untouched(self):
        run = fake("")
        result = tp.probe(group(runner=None, framework="Go testing"), Path("/p"), run=run, clock=self.clock)
        self.assertEqual((result["status"], run.calls), ("unsupported", []))

    def test_a_real_unittest_folder_is_listed_by_the_real_loader(self):
        """No fakes: the snippet runs under this Python against a temp folder, including one file that can't be imported."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "tests/test_a.py", "import unittest\nclass T(unittest.TestCase):\n    def test_one(self): pass\n    def test_two(self): pass\n")
            write(root, "tests/test_bad.py", "import not_a_real_module_xyz\n")
            g = group(runner="python", framework="unittest", folder="tests", files=["test_a.py", "test_bad.py"], isolate=False)
            real = tp.probe(g, root, run=lambda c, d, t: (lambda p: (p.returncode, p.stdout + p.stderr, False))(
                subprocess.run(c, cwd=d, shell=True, capture_output=True, text=True)))
            self.assertEqual((real["status"], real["tests"]), ("needs-setup", 2))
            self.assertIn("not_a_real_module_xyz", real["reason"])


class ProbeCommandTests(unittest.TestCase):
    def test_the_check_command_lists_a_real_project_and_saves_what_it_found(self):
        """No fakes: `orchestrator script job_actions.py probe` on a temp project with one good and one unimportable group."""
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "tests/ok/test_ok.py", "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n    def test_b(self): pass\n")
            write(root, "tests/bad/test_bad.py", "import not_a_real_module_xyz\n")
            write(root, ".orchestrator/project.json", json.dumps({"project_name": "P", "build_command": "b", "test_command": "t"}))
            env = {**os.environ, "ORCHESTRATOR_PROJECT_ROOT": str(root), "PYTHONPATH": str(PACKAGE_ROOT)}
            done = subprocess.run([sys.executable, "-m", "orchestrator", "script", "job_actions.py", "probe"], cwd=root, env=env,
                                  capture_output=True, text=True, timeout=120)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("2 tests (counted by unittest)", done.stdout)
            self.assertIn("Needs setup", done.stdout)
            cache = json.loads((root / ".orchestrator/output/test-probes.json").read_text())
            by = {k.split("-")[-1]: v for k, v in cache.items()}
            self.assertEqual((by["ok"]["status"], by["ok"]["tests"]), ("ok", 2))
            self.assertEqual(by["bad"]["status"], "needs-setup")
            one = subprocess.run([sys.executable, "-m", "orchestrator", "script", "job_actions.py", "probe", "--group", "unittest-tests-ok"],
                                 cwd=root, env=env, capture_output=True, text=True, timeout=120)
            self.assertNotIn("bad", one.stdout)


class CacheTests(unittest.TestCase):
    def test_results_are_cached_merged_and_overlaid_with_the_exact_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            tp.write_cache(runtime, {"a": {"status": "ok", "tests": 389, "names": ["x"]}})
            tp.write_cache(runtime, {"b": {"status": "needs-setup", "tests": None, "reason": "why"}})
            cache = tp.read_cache(runtime)
            self.assertEqual(set(cache), {"a", "b"})
            groups = tp.overlay([{"id": "a", "tests": 179}, {"id": "b", "tests": 10}, {"id": "c", "tests": 5}], cache)
            self.assertEqual([(g["tests"], g["exact"], g["estimated"]) for g in groups], [(389, True, 179), (10, False, 10), (5, False, 5)])
            self.assertIsNone(groups[2]["probe"])
        self.assertEqual(tp.read_cache(Path("/nonexistent")), {})


class IsolationTests(unittest.TestCase):
    def test_a_folder_that_contains_other_groups_names_only_its_own_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "tests/test_top.py", "import unittest\n")
            write(root, "tests/sub/test_sub.py", "import unittest\n")
            suites = [{"path": root / "tests/test_top.py", "rel_path": Path("tests/test_top.py"), "name": "Top", "test_count": 2, "test_funcs": ["a", "b"], "language": "python"},
                      {"path": root / "tests/sub/test_sub.py", "rel_path": Path("tests/sub/test_sub.py"), "name": "Sub", "test_count": 1, "test_funcs": ["c"], "language": "python"}]
            groups = {g["folder"]: g for g in tg.build(root, suites, {})[0]}
            self.assertTrue(groups["tests"]["isolate"])
            self.assertFalse(groups["tests/sub"]["isolate"])
            self.assertTrue(groups["tests"]["command"].endswith("tests test_top"))
            self.assertEqual(groups["tests/sub"]["command"], "python3 -m unittest discover -s tests/sub")

    def test_pytest_names_files_when_isolated_and_a_name_that_cannot_be_imported_falls_back(self):
        self.assertEqual(tg.python_command("pytest", "tests", ["test_a.py"], True), "python3 -m pytest tests/test_a.py")
        self.assertEqual(tg.python_command("pytest", "tests", ["test_a.py"], False), "python3 -m pytest tests")
        self.assertEqual(tg.python_command("unittest", "tests", ["test-a.py"], True), "python3 -m unittest discover -s tests")


class BlockedGroupsCannotRunEveryJobTests(ServerTestCase):
    def post(self, body):
        return self.request("POST", "/api/test-groups", body, headers=UI_HEADERS)

    def setUp(self):
        super().setUp()
        self.groups = [group(id="ok-g", command='dotnet test "a.csproj" --nologo', project="a.csproj", folder="a"),
                       group(id="bad-g", command='dotnet test "b.csproj" --nologo', project="b.csproj", folder="b")]
        (self.root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "Demo", "test_command": "make"}))
        tp.write_cache(self.root / ".orchestrator", {"bad-g": {"status": "needs-setup", "reason": "needs PreparedServerSource", "tests": None},
                                                    "ok-g": {"status": "ok", "tests": 3}})
        patcher = patch.object(ui.UIHandler, "_tests", return_value={"groups": self.groups})
        patcher.start()
        self.addCleanup(patcher.stop)

    def saved(self):
        return json.loads((self.root / ".orchestrator" / "project.json").read_text())

    def test_a_group_that_cannot_run_here_is_refused_for_every_job_but_can_still_be_by_hand(self):
        res, data = self.post({"group": "bad-g", "mode": "per-job"})
        self.assertEqual(res.status, 400)
        self.assertIn("needs PreparedServerSource", data["error"])
        self.assertIn("Check", data["error"])
        self.assertEqual(self.post({"group": "bad-g", "mode": "manual"})[0].status, 200)
        self.assertEqual(self.post({"group": "ok-g", "mode": "per-job"})[0].status, 200)

    def test_all_every_job_leaves_out_the_blocked_one_and_says_so(self):
        res, data = self.post({"group": "all", "mode": "per-job"})
        self.assertEqual(res.status, 200)
        self.assertEqual(data["skipped"], ["xUnit b"])
        names = [e["command"] for e in self.saved().get("test_commands", [])] + [self.saved()["test_command"]]
        self.assertFalse(any("b.csproj" in c for c in names))
        self.assertTrue(any("a.csproj" in c for c in names))


class ActionTests(ServerTestCase):
    def test_check_runs_the_probe_for_one_group_or_all_and_a_name_cannot_become_an_option(self):
        one = ui.ACTIONS["test_probe"].build({"group": "xunit-t"}, self.root)
        self.assertEqual(one[-4:], ["job_actions.py", "probe", "--group", "xunit-t"])
        self.assertEqual(ui.ACTIONS["test_probe"].build({"group": "all"}, self.root)[-2:], ["job_actions.py", "probe"])
        with self.assertRaises(ui.UIError):
            ui.ACTIONS["test_probe"].build({"group": "x; rm -rf /"}, self.root)


if __name__ == "__main__":
    unittest.main()
