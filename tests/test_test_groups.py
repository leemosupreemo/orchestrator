from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import test_groups as tg  # noqa: E402
from test_web_ui import UI_HEADERS, ServerTestCase, ui  # noqa: E402

XUNIT = '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="xunit" Version="2"/></ItemGroup></Project>'
NUNIT = '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="NUnit" Version="4"/></ItemGroup></Project>'


def write(root: Path, rel: str, text: str = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def suite(root: Path, rel: str, language: str, funcs: list[str], **extra) -> dict:
    return {"path": root / rel, "rel_path": Path(rel), "name": Path(rel).stem, "test_count": len(funcs),
            "test_funcs": funcs, "language": language, **extra}


def project(tmp: str):
    root = Path(tmp)
    write(root, "tests/Proto.Tests/Proto.Tests.csproj", XUNIT)
    write(root, "tests/Proto.Tests/A.cs", "[Fact] void One(){}")
    write(root, "tests/Server.Tests/Server.Tests.csproj", NUNIT)
    write(root, "tests/tools/test_a.py", "import unittest\nclass T(unittest.TestCase):\n  def test_x(self): pass\n")
    write(root, "tests/api/test_b.py", "import pytest\ndef test_y(): pass\n")
    write(root, "app/main.go_test.txt")
    suites = [
        suite(root, "tests/Proto.Tests/A.cs", "csharp", ["One", "Two"], framework="dotnet"),
        suite(root, "tests/Server.Tests/B.cs", "csharp", ["Three"], framework="dotnet"),
        suite(root, "tests/tools/test_a.py", "python", ["test_x"]),
        suite(root, "tests/api/test_b.py", "python", ["test_y"]),
        suite(root, "Assets/Tests/U.cs", "csharp", ["Edit"], framework="unity"),
        suite(root, "lib/x_test.go", "go", ["TestX"]),
    ]
    write(root, "tests/Server.Tests/B.cs", "x")
    return root, suites


class BuildTests(unittest.TestCase):
    def test_groups_by_framework_and_folder_with_plain_explanations_and_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, suites = project(tmp)
            groups, extras = tg.build(root, suites, {})
            by = {g["framework"] + ":" + g["folder"]: g for g in groups}
            self.assertEqual(set(by), {"xUnit:tests/Proto.Tests", "NUnit:tests/Server.Tests", "unittest:tests/tools",
                                       "pytest:tests/api", "Unity Test Framework:", "Go testing:"})
            proto = by["xUnit:tests/Proto.Tests"]
            self.assertEqual((proto["tests"], proto["suites"], proto["toggle"]), (2, 1, True))
            self.assertEqual(proto["command"], 'dotnet test "tests/Proto.Tests/Proto.Tests.csproj" --nologo')
            self.assertIn("unit tests", proto["info"])
            self.assertEqual(by["unittest:tests/tools"]["command"], "python3 -m unittest discover -s tests/tools")
            self.assertEqual(by["pytest:tests/api"]["command"], "python3 -m pytest tests/api")
            self.assertFalse(by["Unity Test Framework:"]["toggle"])
            self.assertFalse(by["Go testing:"]["toggle"])
            self.assertEqual(extras["tests/Proto.Tests/A.cs"]["funcs"], ["One", "Two"])
            self.assertEqual(extras["tests/Proto.Tests/A.cs"]["group"], proto["id"])

    def test_suites_in_another_repository_cloned_inside_are_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, suites = project(tmp)
            write(root, "vendor/Other/.git", "gitdir: x")
            write(root, "vendor/Other/Tests/O.cs", "x")
            suites.append(suite(root, "vendor/Other/Tests/O.cs", "csharp", ["O"], framework="dotnet"))
            kept = tg.outside_nested_repos(root, suites)
            self.assertNotIn("vendor/Other/Tests/O.cs", [Path(s["rel_path"]).as_posix() for s in kept])
            self.assertEqual(len(kept), len(suites) - 1)


class StateTests(unittest.TestCase):
    def groups(self, tmp, config):
        root, suites = project(tmp)
        return {g["framework"] + ":" + g["folder"]: g for g in tg.build(root, suites, config)[0]}

    def test_mode_comes_from_the_main_command_or_an_extra_entry_or_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"test_command": "dotnet test tests/Proto.Tests -c Release --nologo && python3 tools/mutations.py",
                      "test_commands": [{"name": "t", "command": "python3 -m unittest discover -s tests/tools", "tier": "manual"},
                                        {"name": "n", "command": 'dotnet test "tests/Server.Tests/Server.Tests.csproj" --nologo', "tier": "nightly"}]}
            g = self.groups(tmp, config)
            self.assertEqual((g["xUnit:tests/Proto.Tests"]["mode"], g["xUnit:tests/Proto.Tests"]["where"]), ("per-job", "main"))
            self.assertEqual((g["unittest:tests/tools"]["mode"], g["unittest:tests/tools"]["extra"]), ("manual", "t"))
            self.assertEqual(g["NUnit:tests/Server.Tests"]["mode"], "nightly")
            self.assertEqual(g["pytest:tests/api"]["mode"], "off")
            self.assertIsNone(g["Go testing:"]["mode"])

    def test_a_folder_is_not_matched_by_a_longer_path_that_starts_with_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = self.groups(tmp, {"test_command": "python3 -m unittest discover -s tests/tools2"})
            self.assertEqual(g["unittest:tests/tools"]["mode"], "off")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root, self.suites = project(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def group(self, key, config):
        return {f"{g['framework']}:{g['folder']}": g for g in tg.build(self.root, self.suites, config)[0]}[key]

    def test_making_an_unset_group_run_on_every_job_adds_an_entry_unless_there_is_no_main_command(self):
        config = {"test_command": "make test"}
        changes = tg.plan(config, self.group("pytest:tests/api", config), "per-job")
        self.assertEqual(changes, {"test_commands": [{"name": "pytest-tests-api", "command": "python3 -m pytest tests/api"}]})
        empty = {"test_command": tg.NO_AUTOMATIC_TESTS}
        self.assertEqual(tg.plan(empty, self.group("pytest:tests/api", empty), "per-job"), {"test_command": "python3 -m pytest tests/api"})

    def test_making_a_main_command_group_manual_moves_only_its_part_and_keeps_its_flags(self):
        config = {"test_command": "dotnet test tests/Proto.Tests -c Release --nologo && python3 tools/mutations.py"}
        changes = tg.plan(config, self.group("xUnit:tests/Proto.Tests", config), "manual")
        self.assertEqual(changes["test_command"], "python3 tools/mutations.py")
        self.assertEqual(changes["test_commands"], [{"name": "xunit-tests-proto-tests", "tier": "manual",
                                                     "command": "dotnet test tests/Proto.Tests -c Release --nologo"}])

    def test_the_last_automatic_group_leaves_a_clear_no_tests_command_and_it_is_replaced_when_one_comes_back(self):
        config = {"test_command": 'dotnet test "tests/Proto.Tests/Proto.Tests.csproj" --nologo'}
        group = self.group("xUnit:tests/Proto.Tests", config)
        changes = tg.plan(config, group, "manual")
        self.assertEqual(changes["test_command"], tg.NO_AUTOMATIC_TESTS)
        after = {**config, **changes}
        back = tg.plan(after, self.group("xUnit:tests/Proto.Tests", after), "per-job")
        self.assertEqual(back, {"test_command": 'dotnet test "tests/Proto.Tests/Proto.Tests.csproj" --nologo', "test_commands": None})

    def test_flipping_an_existing_entry_changes_only_its_tier_and_noops_change_nothing(self):
        config = {"test_command": "make", "test_commands": [{"name": "t", "command": "python3 -m unittest discover -s tests/tools", "tier": "manual", "timeout": 60}]}
        group = self.group("unittest:tests/tools", config)
        self.assertEqual(tg.plan(config, group, "per-job")["test_commands"], [{"name": "t", "command": "python3 -m unittest discover -s tests/tools", "timeout": 60}])
        self.assertEqual(tg.plan(config, group, "manual"), {})
        main = {"test_command": 'dotnet test "tests/Proto.Tests/Proto.Tests.csproj" --nologo'}
        self.assertEqual(tg.plan(main, self.group("xUnit:tests/Proto.Tests", main), "per-job"), {})
        self.assertEqual(tg.plan(main, self.group("Go testing:", main), "manual"), {})  # not movable

    def test_entry_names_stay_unique(self):
        config = {"test_command": "make", "test_commands": [{"name": "pytest-tests-api", "command": "other"}]}
        entry = tg.plan(config, self.group("pytest:tests/api", config), "manual")["test_commands"][-1]
        self.assertEqual(entry["name"], "pytest-tests-api-2")

    def test_all_moves_every_movable_group_and_result_names_are_valid(self):
        from orchestrator import test_commands
        config = {"test_command": "dotnet test tests/Proto.Tests --nologo"}
        groups = tg.build(self.root, self.suites, config)[0]
        changes = tg.plan_all(config, groups, "manual")
        self.assertEqual(changes["test_command"], tg.NO_AUTOMATIC_TESTS)
        self.assertEqual(len(changes["test_commands"]), 4)  # xUnit, NUnit, unittest, pytest
        self.assertEqual(test_commands.parse(changes["test_commands"], self.root)[1], [])
        after = {**config, **changes}
        per_job = tg.plan_all(after, tg.build(self.root, self.suites, after)[0], "per-job")
        self.assertNotEqual(per_job["test_command"], tg.NO_AUTOMATIC_TESTS)  # the first one takes the empty main command
        self.assertEqual(len(per_job["test_commands"]), 3)  # the other three stay as entries
        self.assertTrue(all(e.get("tier", "per-job") == "per-job" for e in per_job["test_commands"]))


class EndpointTests(ServerTestCase):
    def post(self, body):
        return self.request("POST", "/api/test-groups", body, headers=UI_HEADERS)

    def setUp(self):
        super().setUp()
        self.groups = [{"id": "xunit-t", "framework": "xUnit", "folder": "tests/T", "project": "tests/T/T.csproj", "runner": "dotnet",
                        "command": 'dotnet test "tests/T/T.csproj" --nologo', "toggle": True},
                       {"id": "go-testing", "framework": "Go testing", "toggle": False, "command": None}]
        (self.root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "Demo", "test_command": "make test"}))
        patcher = patch.object(ui.UIHandler, "_tests", return_value={"groups": self.groups})
        patcher.start()
        self.addCleanup(patcher.stop)

    def saved(self):
        return json.loads((self.root / ".orchestrator" / "project.json").read_text())

    def test_a_group_is_moved_and_the_rest_of_the_config_is_kept(self):
        res, data = self.post({"group": "xunit-t", "mode": "manual"})
        self.assertEqual((res.status, data["changed"]), (200, ["test_commands"]))
        saved = self.saved()
        self.assertEqual((saved["project_name"], saved["test_command"]), ("Demo", "make test"))
        self.assertEqual(saved["test_commands"], [{"name": "xunit-t", "command": 'dotnet test "tests/T/T.csproj" --nologo', "tier": "manual"}])
        self.assertEqual(self.post({"group": "xunit-t", "mode": "per-job"})[1]["changed"], ["test_commands"])
        self.assertEqual(self.saved()["test_commands"], [{"name": "xunit-t", "command": 'dotnet test "tests/T/T.csproj" --nologo'}])
        self.assertEqual(self.post({"group": "all", "mode": "per-job"})[1]["changed"], [])  # already

    def test_bad_requests_and_members_are_refused(self):
        self.assertEqual(self.post({"group": "xunit-t", "mode": "sometimes"})[0].status, 400)
        self.assertEqual(self.post({"group": "nope", "mode": "manual"})[0].status, 400)
        res, data = self.post({"group": "go-testing", "mode": "manual"})
        self.assertEqual(res.status, 400)
        self.assertIn("main test command", data["error"])
        with patch.object(ui.UIHandler, "_is_owner", return_value=False):
            self.assertEqual(self.post({"group": "all", "mode": "manual"})[0].status, 403)
        self.assertNotIn("test_commands", self.saved())


if __name__ == "__main__":
    unittest.main()
