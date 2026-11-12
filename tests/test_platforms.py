from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import platforms as pf  # noqa: E402
from test_test_groups import write  # noqa: E402


def lines(n: int, prefix: str = "x") -> str:
    return "\n".join(f"{prefix} {i}" for i in range(n)) + "\n"


def game(root: Path) -> None:
    """A Unity client, a shared protocol library with its tests, a cloned server repo, and some tooling."""
    write(root, "GameUnity/ProjectSettings/ProjectVersion.txt", "m_EditorVersion: 6000.0.30f1\n")
    write(root, "GameUnity/Assets/Scripts/GameClient.cs", "class GameClient {\n" + lines(300) + "}\n")
    write(root, "GameUnity/Assets/Scripts/TouchHud.cs", "class TouchHud {\n" + lines(100) + "}\n")
    write(root, "GameUnity/Assets/Plugins/Vendored.cs", "class VendoredThing {\n" + lines(900) + "}\n")  # third party: not counted
    write(root, "Game.Protocol/PacketReader.cs", "class PacketReader {\n" + lines(50) + "}\n")
    write(root, "Game.Protocol/Account.cs", "class AccountRecord {\n" + lines(30) + "}\n")
    write(root, "tests/Game.Protocol.Tests/Game.Protocol.Tests.csproj",
          '<Project><ItemGroup><ProjectReference Include="..\\..\\Game.Protocol\\Game.Protocol.csproj" /></ItemGroup></Project>')
    write(root, "Game.Protocol/Game.Protocol.csproj", "<Project/>")
    write(root, "tests/Game.Protocol.Tests/ReaderTests.cs", "class ReaderTests { void Reads() { var r = new PacketReader(); } }\n")
    write(root, "GameServerRepo/.git", "gitdir: x")
    write(root, "GameServerRepo/GameServer/Player.cs", "class PlayerObject {\n" + lines(400) + "}\n")
    write(root, "GameServerRepo/GameServer/Zone.cs", "class Zone {\n" + lines(20) + "}\n")
    write(root, "GameServerRepo/Tests/PlayerTests.cs", "class PlayerTests { void T() { var p = new PlayerObject(); } }\n")
    write(root, "tools/importer.py", "class ZoneImporter:\n" + lines(60) + "\n")
    write(root, "tools/orphan.py", "def go():\n" + lines(80) + "\n")
    write(root, "tests/tools/test_importer.py", "from tools.importer import ZoneImporter\n")


def suite(root: Path, rel: str, count: int) -> dict:
    return {"path": root / rel, "rel_path": Path(rel), "name": Path(rel).stem, "test_count": count, "language": "csharp"}


class DetectTests(unittest.TestCase):
    def test_platforms_come_from_the_folders_with_a_separate_repo_labelled_by_its_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game(root)
            found = {p["name"]: p for p in pf.detect(root, {})}
            self.assertEqual(found["Client (Unity)"]["paths"], ["GameUnity"])
            self.assertEqual(found["Shared code"]["paths"], ["Game.Protocol"])
            self.assertEqual(found["Tooling"]["kind"], "tooling")
            server = found["GameServerRepo (server, separate repository)"]
            self.assertEqual(server["kind"], "server")
            self.assertNotIn("tests", [p["paths"][0] for p in found.values()])  # tests are not a platform

    def test_xcode_apps_and_server_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "MyApp.xcodeproj/project.pbxproj", "x")
            write(root, "MyApp/A.swift", "struct A {}")
            write(root, "functions/index.js", "x")
            write(root, "MyAppTests/ATests.swift", "x")
            names = {p["name"]: p["kind"] for p in pf.detect(root, {"project_name": "MyApp"})}
            self.assertEqual(names, {"Client (iOS app)": "client", "Server": "server"})

    def test_project_json_names_the_platforms_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"platforms": [{"name": "Game client", "kind": "client", "paths": ["GameUnity/"]},
                                    {"name": "Odd", "kind": "nonsense", "paths": ["x"]}, {"nope": 1}]}
            got = pf.detect(Path(tmp), config)
            self.assertEqual([(p["name"], p["kind"], p["paths"], p["detected"]) for p in got],
                             [("Game client", "client", ["GameUnity"], False), ("Odd", "other", ["x"], False)])


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        game(self.root)
        suites = [suite(self.root, "tests/Game.Protocol.Tests/ReaderTests.cs", 12), suite(self.root, "GameServerRepo/Tests/PlayerTests.cs", 5),
                  suite(self.root, "tests/tools/test_importer.py", 4)]
        self.result = pf.analyze(self.root, {}, suites)
        self.by = {p["name"].split(" (")[0]: p for p in self.result["platforms"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_shared_code_counts_the_files_a_test_mentions_and_its_tests_via_the_test_projects_reference(self):
        shared = self.by["Shared code"]
        self.assertEqual((shared["files"], shared["tested_files"], shared["tests"]), (2, 1, 12))
        self.assertEqual(shared["percent"], round(100 * 51 / 82))  # PacketReader's lines out of both files' lines
        self.assertEqual([g["path"] for g in shared["gaps"]], ["Game.Protocol/Account.cs"])

    def test_the_client_has_no_tests_and_its_biggest_gaps_come_first_without_third_party_code(self):
        client = self.by["Client"]
        self.assertEqual((client["files"], client["tested_files"], client["tests"], client["percent"]), (2, 0, 0, 0))
        self.assertEqual([g["path"] for g in client["gaps"]], ["GameUnity/Assets/Scripts/GameClient.cs", "GameUnity/Assets/Scripts/TouchHud.cs"])
        self.assertEqual(client["lines"], 302 + 102)  # the vendored plugin's 900 lines aren't counted

    def test_a_test_inside_another_repository_belongs_to_that_platform_and_short_or_shared_names_are_ignored(self):
        server = next(p for p in self.result["platforms"] if p["kind"] == "server")
        self.assertEqual((server["tests"], server["tested_files"], server["files"]), (5, 1, 2))
        self.assertEqual([g["path"] for g in server["gaps"]], ["GameServerRepo/GameServer/Zone.cs"])  # Zone is a short generic name

    def test_python_tooling_is_matched_by_imports_and_credited_by_votes(self):
        tooling = self.by["Tooling"]
        self.assertEqual((tooling["tested_files"], tooling["tests"]), (1, 4))
        self.assertEqual(tooling["gaps"][0]["path"], "tools/orphan.py")

    def test_platforms_are_ordered_client_server_shared_tooling(self):
        self.assertEqual([p["kind"] for p in self.result["platforms"]], ["client", "server", "shared", "tooling"])

    def test_the_note_says_this_is_not_line_coverage_and_there_is_nothing_for_an_empty_project(self):
        self.assertIn("not line coverage", self.result["note"])
        with tempfile.TemporaryDirectory() as empty:
            self.assertEqual(pf.analyze(Path(empty), {})["platforms"], [])

    def test_config_files_are_not_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "functions/index.js", "export function handler() {}\n" + lines(20))
            write(root, "functions/eslint.config.js", lines(30))
            write(root, "functions/.eslintrc.js", lines(30))
            result = pf.analyze(root, {})
            self.assertEqual(result["platforms"][0]["files"], 1)


if __name__ == "__main__":
    unittest.main()
