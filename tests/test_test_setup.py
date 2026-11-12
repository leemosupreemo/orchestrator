from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import cli, test_setup  # noqa: E402
from orchestrator.stack_detection import detect_project_stack  # noqa: E402
from test_web_ui import UI_HEADERS, ServerTestCase  # noqa: E402

XUNIT = '<Project Sdk="Microsoft.NET.Sdk"><ItemGroup><PackageReference Include="xunit" Version="2" /></ItemGroup></Project>'
PLAIN = '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup/></Project>'


def write(root: Path, rel: str, text: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def unity_with_extras(root: Path) -> None:
    write(root, "Client/ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.0.30f1\n")
    write(root, "Client/Assets/Scripts/A.cs", "class A {}")
    write(root, "Client/Packages/manifest.json", '{"dependencies":{}}')
    write(root, "tests/Proto.Tests/Proto.Tests.csproj", XUNIT)
    write(root, "tools/test-server.py", 'import subprocess\nsubprocess.run(["docker", "compose", "up"])\n')
    write(root, "tools/check_assets.py", "print('ok')\n")
    write(root, "tools/test-live.py", 'HOST = "127.0.0.1"\n')
    write(root, "server/docker-compose.yml", "services:\n  db:\n    container_name: game-db\n    image: x\n")


class DetectionTests(unittest.TestCase):
    def test_finds_dotnet_test_projects_and_skips_plain_projects_nested_repos_and_unity_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unity_with_extras(root)
            write(root, "src/Lib/Lib.csproj", PLAIN)
            write(root, "vendor/Other/Tests/Other.Tests.csproj", XUNIT)
            write(root, "vendor/Other/.git", "gitdir: x")  # another repository cloned inside this one
            write(root, "Client/Assets/Fake/Fake.Tests.csproj", XUNIT)  # inside Unity's Assets
            self.assertEqual(test_setup.dotnet_test_projects(root), ["tests/Proto.Tests/Proto.Tests.csproj"])
            self.assertEqual(test_setup.dotnet_test_command(root), 'dotnet test "tests/Proto.Tests/Proto.Tests.csproj" --nologo')

    def test_stack_detection_uses_them_when_the_stack_has_no_tests_of_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unity_with_extras(root)
            stack = detect_project_stack(root)
            self.assertEqual(stack.framework, "unity")
            self.assertIn("Proto.Tests.csproj", stack.test_command)

    def test_script_candidates_are_manual_flag_servers_and_require_docker_when_they_use_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unity_with_extras(root)
            by_id = {c["id"]: c for c in test_setup.script_candidates(root, set())}
            self.assertEqual(set(by_id), {"test-server", "check-assets", "test-live"})
            self.assertEqual(by_id["test-server"]["requires"], ["docker"])
            self.assertFalse(by_id["check-assets"]["risky"])
            self.assertTrue(by_id["test-live"]["risky"])  # talks to 127.0.0.1
            self.assertTrue(all(c["tier"] == "manual" for c in by_id.values()))
            self.assertEqual(by_id["test-server"]["command"], "python3 tools/test-server.py")

    def test_nothing_already_configured_is_suggested_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unity_with_extras(root)
            found = test_setup.suggest(root, {"test_command": "dotnet test x && python3 tools/check_assets.py",
                                              "test_commands": [{"name": "test-server", "command": "python3 tools/test-server.py"}]})
            self.assertIsNone(found["test_command"])
            self.assertEqual([c["id"] for c in found["commands"]], ["test-live"])

    def test_notes_say_what_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "Client/ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.0.30f1\n")
            write(root, "Client/Assets/A.cs", "x")
            write(root, "Client/Packages/manifest.json", "{}")
            write(root, "server/docker-compose.yml", "services:\n  db:\n    image: x\n")
            notes = " ".join(test_setup.suggest(root, {})["notes"])
            self.assertIn("Docker compose found (server/docker-compose.yml)", notes)
            self.assertIn("no Unity Test Framework", notes)


class ApplyTests(unittest.TestCase):
    def test_all_takes_the_main_command_and_safe_commands_but_never_a_risky_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unity_with_extras(root)
            changes, problems = test_setup.apply(root, {"project_name": "G"}, ["all"])
            self.assertEqual(problems, [])
            self.assertIn("Proto.Tests.csproj", changes["test_command"])
            self.assertEqual({c["name"] for c in changes["test_commands"]}, {"test-server", "check-assets"})
            self.assertTrue(all(c["tier"] == "manual" for c in changes["test_commands"]))

    def test_a_risky_command_has_to_be_named_and_unknown_names_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unity_with_extras(root)
            changes, problems = test_setup.apply(root, {}, ["test-live"])
            self.assertEqual([c["name"] for c in changes["test_commands"]], ["test-live"])
            self.assertEqual(test_setup.apply(root, {}, ["nope"])[1], ["Nothing is suggested under the name nope."])

    def test_existing_commands_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unity_with_extras(root)
            existing = {"name": "mine", "command": "make"}
            changes, _ = test_setup.apply(root, {"test_commands": [existing]}, ["check-assets"])
            self.assertEqual([c["name"] for c in changes["test_commands"]], ["mine", "check-assets"])


class CliTests(unittest.TestCase):
    def run_cli(self, root: Path, argv: list[str]):
        from orchestrator.project_config import load_project_config
        out = io.StringIO()
        with patch("orchestrator.cli.load_project_config", return_value=load_project_config(root)), redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def project(self, tmp: str) -> Path:
        root = Path(tmp)
        unity_with_extras(root)
        write(root, ".orchestrator/project.json", json.dumps({"project_name": "G", "build_command": "echo b"}))
        return root

    def test_check_config_points_at_the_tests_it_found_and_accepting_fixes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(tmp)
            code, out = self.run_cli(root, ["check-config"])
            self.assertEqual(code, 1)
            self.assertIn("Found tests: dotnet test", out)
            self.assertIn("suggest-tests --accept test_command", out)
            code, out = self.run_cli(root, ["suggest-tests", "--accept", "test_command"])
            self.assertEqual(code, 0)
            saved = json.loads((root / ".orchestrator/project.json").read_text())
            self.assertIn("Proto.Tests.csproj", saved["test_command"])
            self.assertEqual(saved["project_name"], "G")  # nothing else changed
            self.assertNotIn("Found tests", self.run_cli(root, ["check-config"])[1])

    def test_listing_marks_risky_scripts_and_accept_validates_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.project(tmp)
            code, out = self.run_cli(root, ["suggest-tests"])
            self.assertEqual(code, 0)
            self.assertIn("test-server: python3 tools/test-server.py  needs: docker", out)
            self.assertIn("! it looks like it connects to a server", out.split("test-live", 1)[1])
            self.assertEqual(self.run_cli(root, ["suggest-tests", "--accept", "nope"])[0], 1)

    def test_a_project_with_nothing_to_suggest_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, ".orchestrator/project.json", json.dumps({"project_name": "G", "build_command": "b", "test_command": "t"}))
            self.assertIn("Nothing to suggest", self.run_cli(root, ["suggest-tests"])[1])


class EndpointTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        unity_with_extras(self.root)

    def post(self, path, body):
        return self.request("POST", path, body, headers=UI_HEADERS)

    def test_the_page_gets_suggestions_and_accepting_saves_only_those(self):
        _, found = self.request("GET", "/api/test-setup")
        self.assertIn("Proto.Tests.csproj", found["test_command"]["command"])
        self.assertEqual({c["id"] for c in found["commands"]}, {"test-server", "check-assets", "test-live"})
        res, data = self.post("/api/test-setup/apply", {"accept": ["test_command", "check-assets"]})
        self.assertEqual((res.status, data["changed"]), (200, ["test_command", "test_commands"]))
        saved = json.loads((self.root / ".orchestrator/project.json").read_text())
        self.assertEqual((saved["project_name"], [c["name"] for c in saved["test_commands"]]), ("Demo", ["check-assets"]))
        _, again = self.request("GET", "/api/test-setup")
        self.assertIsNone(again["test_command"])
        self.assertNotIn("check-assets", [c["id"] for c in again["commands"]])

    def test_unknown_names_are_refused_and_members_cannot_apply(self):
        self.assertEqual(self.post("/api/test-setup/apply", {"accept": ["nope"]})[0].status, 400)
        with patch.object(__import__("orchestrator.web.server", fromlist=["x"]).UIHandler, "_is_owner", return_value=False):
            self.assertEqual(self.post("/api/test-setup/apply", {"accept": ["all"]})[0].status, 403)


if __name__ == "__main__":
    unittest.main()
