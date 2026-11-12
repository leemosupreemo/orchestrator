from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator import cli
from orchestrator.stack_detection import detect_project_stack
from orchestrator.scripts import dev_console
from orchestrator.scripts.generate_test_index import generate_test_index
from tests.test_config_validation import make_config


class StackDetectionTests(unittest.TestCase):
    def test_shared_requirements_take_precedence_over_legacy_branch_copy(self):
        from orchestrator import prd
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / prd.PATH
            legacy.parent.mkdir(parents=True)
            legacy.write_text(prd.replace_section(prd.template(), "pitch", "An iOS app"))
            prd.Prd(root, root / ".orchestrator").write(prd.replace_section(prd.template(), "pitch", "A Python service"))
            self.assertEqual(detect_project_stack(root).language, "python")

    def test_detects_xcode_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "MyApp.xcodeproj").mkdir()
            stack = detect_project_stack(root)
            self.assertEqual(stack.language, "swift")
            self.assertEqual(stack.framework, "xcode")
            self.assertTrue(stack.uses_xcode)
            self.assertEqual(stack.scheme, "MyApp")

    def test_detects_spm_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Package.swift").write_text("// swift-tools-version: 6.0\nimport PackageDescription\n")
            stack = detect_project_stack(root)
            self.assertEqual(stack.language, "swift")
            self.assertEqual(stack.framework, "spm")
            self.assertFalse(stack.uses_xcode)
            self.assertEqual(stack.build_command, "swift build")
            self.assertEqual(stack.test_command, "swift test")

    def test_detects_rust_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Cargo.toml").write_text("[package]\nname = \"service\"\nversion = \"0.1.0\"\n")
            stack = detect_project_stack(root)
            self.assertEqual(stack.language, "rust")
            self.assertEqual(stack.framework, "cargo")
            self.assertFalse(stack.uses_xcode)
            self.assertEqual(stack.build_command, "cargo build")
            self.assertEqual(stack.test_command, "cargo test")

    def test_detects_python_project_pytest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname = \"my_pkg\"\ndependencies = [\"pytest\"]\n")
            (root / "src").mkdir()
            stack = detect_project_stack(root)
            self.assertEqual(stack.language, "python")
            self.assertEqual(stack.framework, "pytest")
            self.assertFalse(stack.uses_xcode)
            self.assertEqual(stack.build_command, "python3 -m compileall -q src")
            self.assertEqual(stack.test_command, "pytest")

    def test_detects_python_project_unittest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("requests==2.28.0\n")
            stack = detect_project_stack(root)
            self.assertEqual(stack.language, "python")
            self.assertEqual(stack.framework, "unittest")
            self.assertFalse(stack.uses_xcode)
            self.assertEqual(stack.build_command, "python3 -m compileall -q .")
            self.assertEqual(stack.test_command, "python3 -m unittest discover tests")

    def test_detects_node_typescript_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({
                "name": "web-app",
                "scripts": {
                    "build": "tsc",
                    "test": "vitest run"
                }
            }))
            (root / "tsconfig.json").write_text("{}")
            stack = detect_project_stack(root)
            self.assertEqual(stack.language, "typescript")
            self.assertEqual(stack.framework, "npm")
            self.assertFalse(stack.uses_xcode)
            self.assertEqual(stack.build_command, "npm run build")
            self.assertEqual(stack.test_command, "npm test")

    def test_detects_go_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "go.mod").write_text("module example.com/service\n\ngo 1.22\n")
            stack = detect_project_stack(root)
            self.assertEqual(stack.language, "go")
            self.assertEqual(stack.framework, "go test")
            self.assertFalse(stack.uses_xcode)
            self.assertEqual(stack.build_command, "go build ./...")
            self.assertEqual(stack.test_command, "go test ./...")

    def test_detects_unity_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ProjectSettings").mkdir()
            (root / "ProjectSettings" / "ProjectVersion.txt").write_text(
                "m_EditorVersion: 2022.3.10f1\nm_EditorVersionWithRevision: 2022.3.10f1 (ff3792e53c62)\n")
            (root / "Assets").mkdir()
            (root / "Package.swift").write_text("// a generated package must not win\n")
            stack = detect_project_stack(root)
            self.assertEqual((stack.language, stack.framework), ("csharp", "unity"))
            self.assertEqual(stack.display_name, "Unity 2022.3.10f1")
            self.assertFalse(stack.uses_xcode)
            editor = "/Applications/Unity/Hub/Editor/2022.3.10f1/Unity.app/Contents/MacOS/Unity"
            self.assertIn(f'"${{UNITY_PATH:-{editor}}}" -batchmode -nographics -projectPath . -quit', stack.build_command)
            self.assertIn("-runTests -testPlatform EditMode", stack.test_command)

    def test_a_folder_with_only_project_settings_is_not_unity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ProjectSettings").mkdir()
            (root / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.0.1f1\n")
            self.assertEqual(detect_project_stack(root).language, "generic")

    def _unity_in(self, folder: Path, manifest: str | None = None):
        (folder / "ProjectSettings").mkdir(parents=True)
        (folder / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.6f1\n")
        (folder / "Assets").mkdir()
        if manifest is not None:
            (folder / "Packages").mkdir()
            (folder / "Packages" / "manifest.json").write_text(manifest)

    def test_detects_a_unity_client_one_folder_down_over_a_tooling_package_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"dependencies": {"puppeteer": "^25"}}')
            self._unity_in(root / "GameClient", '{"dependencies": {"com.unity.test-framework": "1.4.5"}}')
            stack = detect_project_stack(root)
            self.assertEqual((stack.language, stack.framework, stack.project_dir), ("csharp", "unity", "GameClient"))
            self.assertEqual(stack.display_name, "Unity 6000.6.6f1")
            self.assertEqual(stack.manifest_file, "GameClient/ProjectSettings/ProjectVersion.txt")
            self.assertIn("-projectPath GameClient -quit", stack.build_command)
            self.assertIn("Unity/Hub/Editor/6000.6.6f1/", stack.build_command)
            self.assertIn("-projectPath GameClient -runTests -testPlatform EditMode -testResults GameClient/Temp/unity-editmode-results.xml", stack.test_command)

    def test_a_root_native_stack_still_beats_a_nested_unity_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Cargo.toml").write_text("[package]\nname = \"server\"\n")
            self._unity_in(root / "Client")
            self.assertEqual(detect_project_stack(root).framework, "cargo")

    def test_a_unity_project_without_the_test_framework_gets_no_default_test_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._unity_in(root / "Client", '{"dependencies": {"com.unity.modules.physics": "1.0.0"}}')
            stack = detect_project_stack(root)
            self.assertEqual(stack.framework, "unity")
            self.assertIsNone(stack.test_command)  # -runTests would run nothing
            self.assertIn("-quit", stack.build_command)
            # but a test assembly is enough
            (root / "Client" / "Assets" / "Tests.asmdef").write_text('{"references": ["UnityEngine.TestRunner"]}')
            self.assertIn("-runTests", detect_project_stack(root).test_command)

    def test_xunit_suites_run_with_dotnet_test_narrowed_to_their_classes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests" / "Proto.Tests"
            tests.mkdir(parents=True)
            (tests / "Proto.Tests.csproj").write_text("<Project/>")
            (tests / "DomainTests.cs").write_text(
                "// every class here is a fixture\n"
                "public class ConTests {\n  [Fact]\n  public void Con() {}\n"
                "  [Theory]\n  [InlineData(1)]\n  public void Many(int n) {}\n}\n"
                "public class ZoneTests {\n  [Fact(Skip = \"later\")]\n  public void Zone() {}\n}\n")
            unity_dir = root / "Client" / "Assets" / "Tests"
            unity_dir.mkdir(parents=True)
            (unity_dir / "ScoreTests.cs").write_text("public class ScoreTests {\n  [Test]\n  public void A() {}\n}\n")
            suites = {s["rel_path"].name: s for s in dev_console.discover_test_suites(root)}
            domain = suites["DomainTests.cs"]
            self.assertEqual((domain["framework"], domain["classes"], domain["test_count"]), ("dotnet", ["ConTests", "ZoneTests"], 3))
            self.assertEqual(suites["ScoreTests.cs"]["framework"], "unity")
            config = make_config(root, xcode_project=None, scheme=None, test_target="", build_command="make", test_command="make test")
            self.assertEqual(
                dev_console.test_command_for_suite(domain, config),
                "dotnet test tests/Proto.Tests --nologo --filter 'FullyQualifiedName~ConTests|FullyQualifiedName~ZoneTests'")
            with self.assertRaises(ValueError):  # a Unity suite still needs a Unity command
                dev_console.test_command_for_suite(suites["ScoreTests.cs"], config)

    def test_unity_suites_are_found_and_run_on_the_right_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ProjectSettings").mkdir()
            (root / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.10f1\n")
            edit = root / "Assets" / "Tests" / "Editor"
            play = root / "Assets" / "Tests" / "Runtime"
            cached = root / "Library" / "PackageCache" / "com.unity.x" / "Tests"
            for folder in (edit, play, cached):
                folder.mkdir(parents=True)
            (edit / "ScoreTests.cs").write_text(
                "public class ScoreTests {\n  [Test]\n  public void AddsPoints() {}\n  [TestCase(1)]\n  public void Caps(int n) {}\n}\n")
            (play / "SpawnTests.cs").write_text(
                "public class SpawnTests {\n  [UnityTest]\n  public IEnumerator Spawns() { yield return null; }\n}\n")
            (cached / "VendorTests.cs").write_text("public class VendorTests { [Test] public void Vendor() {} }\n")
            (root / "Assets" / "Score.cs").write_text("public class Score { }\n")
            suites = {s["name"]: s for s in dev_console.discover_test_suites(root)}
            self.assertEqual(set(suites), {"ScoreTests", "SpawnTests"})
            self.assertEqual((suites["ScoreTests"]["language"], suites["ScoreTests"]["test_count"]), ("csharp", 2))
            self.assertEqual(suites["SpawnTests"]["test_funcs"], ["Spawns"])

            stack = detect_project_stack(root)
            config = make_config(root, xcode_project=None, scheme=None, test_target="", build_command=stack.build_command,
                                 test_command=stack.test_command)
            self.assertIn("-testPlatform EditMode", dev_console.test_command_for_suite(suites["ScoreTests"], config))
            self.assertTrue(dev_console.test_command_for_suite(suites["ScoreTests"], config).endswith("-testFilter ScoreTests"))
            self.assertIn("-testPlatform PlayMode", dev_console.test_command_for_suite(suites["SpawnTests"], config))
            plain = make_config(root, xcode_project=None, scheme=None, test_target="", build_command="make", test_command="make test")
            with self.assertRaises(ValueError):
                dev_console.test_command_for_suite(suites["ScoreTests"], plain)

    def test_unity_test_cases_resolve_to_nunit_methods(self):
        from orchestrator.scripts import test_cases
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "Assets" / "Tests"
            tests.mkdir(parents=True)
            (tests / "ScoreTests.cs").write_text(
                "public class ScoreTests {\n  // TC-1-01\n  [Test]\n  public void AddsPoints() {}\n}\n")
            index = test_cases.scan_tests(root)
            self.assertIn("ScoreTests/AddsPoints", index["methods"])
            self.assertIn("TC-1-01", index["markers"])

    def test_generic_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stack = detect_project_stack(root)
            self.assertEqual(stack.language, "generic")
            self.assertFalse(stack.uses_xcode)


class ContextualInitTests(unittest.TestCase):
    def test_init_python_project_contextual(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname = \"my-service\"\n")
            (root / "src").mkdir()

            res = cli.main([
                "init",
                "--root", str(root),
                "--project-name", "my-service",
                "--with-starter-docs",
            ])
            self.assertEqual(res, 0)

            project_cfg = json.loads((root / ".orchestrator" / "project.json").read_text(encoding="utf-8"))
            self.assertIsNone(project_cfg.get("scheme"))
            self.assertEqual(project_cfg.get("test_target"), "")
            self.assertEqual(project_cfg.get("build_command"), "python3 -m compileall -q src")
            self.assertIn("unittest", project_cfg.get("test_command"))

            machines = json.loads((root / ".orchestrator" / "config" / "machines.json").read_text(encoding="utf-8"))
            self.assertFalse(machines["machines"][0]["supports_xcode"])
            self.assertFalse(machines["machines"][0]["supports_simulator"])

            build_docs = (root / "docs" / "build-test-commands.md").read_text(encoding="utf-8")
            self.assertIn("python3 -m compileall", build_docs)
            arch_docs = (root / "docs" / "architecture.md").read_text(encoding="utf-8")
            self.assertIn("Python", arch_docs)
            standards_docs = (root / "docs" / "coding-standards.md").read_text(encoding="utf-8")
            self.assertIn("Python Standards", standards_docs)

    def test_init_rust_project_contextual(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Cargo.toml").write_text("[package]\nname = \"fast-parser\"\nversion = \"0.1.0\"\n")

            res = cli.main([
                "init",
                "--root", str(root),
                "--project-name", "fast-parser",
                "--with-starter-docs",
            ])
            self.assertEqual(res, 0)

            project_cfg = json.loads((root / ".orchestrator" / "project.json").read_text(encoding="utf-8"))
            self.assertIsNone(project_cfg.get("scheme"))
            self.assertEqual(project_cfg.get("build_command"), "cargo build")
            self.assertEqual(project_cfg.get("test_command"), "cargo test")

            build_docs = (root / "docs" / "build-test-commands.md").read_text(encoding="utf-8")
            self.assertIn("cargo build", build_docs)
            self.assertIn("cargo test", build_docs)
            standards_docs = (root / "docs" / "coding-standards.md").read_text(encoding="utf-8")
            self.assertIn("Rust Standards", standards_docs)


class MultiLanguageTestDiscoveryTests(unittest.TestCase):
    def test_discovers_python_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_auth.py").write_text(
                "import unittest\nclass AuthTests(unittest.TestCase):\n"
                "    def test_login(self):\n        pass\n"
                "    def test_logout(self):\n        pass\n"
            )

            suites = dev_console.discover_test_suites(root)
            self.assertEqual(len(suites), 1)
            self.assertEqual(suites[0]["language"], "python")
            self.assertEqual(suites[0]["name"], "AuthTests")
            self.assertEqual(suites[0]["test_count"], 2)
            self.assertIn("test_login", suites[0]["test_funcs"])

    def test_discovers_rust_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "integration_test.rs").write_text(
                "#[test]\nfn test_parse() {}\n\n#[test]\nfn test_serialize() {}\n"
            )

            suites = dev_console.discover_test_suites(root)
            self.assertEqual(len(suites), 1)
            self.assertEqual(suites[0]["language"], "rust")
            self.assertEqual(suites[0]["name"], "integration_test")
            self.assertEqual(suites[0]["test_count"], 2)

    def test_discovers_go_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "handler_test.go").write_text(
                "package main\nimport \"testing\"\nfunc TestHandler(t *testing.T) {}\n"
            )

            suites = dev_console.discover_test_suites(root)
            self.assertEqual(len(suites), 1)
            self.assertEqual(suites[0]["language"], "go")
            self.assertEqual(suites[0]["test_count"], 1)

    def test_discovers_typescript_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "button.test.ts").write_text(
                "describe('Button', () => {\n  it('renders correctly', () => {});\n});\n"
            )

            suites = dev_console.discover_test_suites(root)
            self.assertEqual(len(suites), 1)
            self.assertEqual(suites[0]["language"], "typescript")
            self.assertEqual(suites[0]["name"], "Button")

    def test_generate_test_index_multi_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_api.py").write_text(
                "def test_get():\n    pass\n"
            )
            config = make_config(root, xcode_project=None, scheme=None, test_target="",
                                 build_command="python3 -m compileall .", test_command="pytest")
            index = generate_test_index(root, project_config=config)
            self.assertIn("pytest", index)
            self.assertIn("test_api.py", index)

    def test_generate_test_index_keeps_paths_relative_to_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_api.py").write_text("def test_get():\n    pass\n")
            config = make_config(root, xcode_project=None, scheme=None, test_target="",
                                 build_command="python3 -m compileall .", test_command="pytest")

            index = generate_test_index(tests_dir, project_config=config)

            self.assertIn("pytest tests/test_api.py", index)

    def test_generate_test_index_does_not_treat_rust_unit_tests_as_integration_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            (src / "lib.rs").write_text("#[cfg(test)]\nmod tests { #[test] fn parses() {} }\n")
            config = make_config(root, xcode_project=None, scheme=None, test_target="",
                                 build_command="cargo build", test_command="cargo test")

            index = generate_test_index(root, project_config=config)

            self.assertIn("cargo test", index)
            self.assertNotIn("--test lib", index)

    def test_suite_test_command_uses_language_specific_syntax(self):
        config = make_config(Path("/repo"), xcode_project=None, scheme=None, test_target="",
                             build_command="cargo build", test_command="cargo test")
        rust_unit = {"language": "rust", "rel_path": Path("src/lib.rs"), "file_stem": "lib"}
        rust_integration = {"language": "rust", "rel_path": Path("tests/parser.rs"), "file_stem": "parser"}

        self.assertEqual(dev_console.test_command_for_suite(rust_unit, config), "cargo test")
        self.assertEqual(dev_console.test_command_for_suite(rust_integration, config),
                         "cargo test --test parser")

        python_config = make_config(Path("/repo"), xcode_project=None, scheme=None, test_target="",
                                    build_command="python3 -m compileall .", test_command="pytest")
        python_suite = {"language": "python", "rel_path": Path("tests/test_api.py"),
                        "file_stem": "test_api"}
        self.assertEqual(dev_console.test_command_for_suite(python_suite, python_config),
                         "pytest tests/test_api.py")

        # unittest doesn't take a path: `discover tests tests/test_api.py` would run nothing
        unittest_config = make_config(Path("/repo"), xcode_project=None, scheme=None, test_target="",
                                      build_command="python3 -m compileall .", test_command="python3 -m unittest discover tests")
        self.assertEqual(dev_console.test_command_for_suite(python_suite, unittest_config),
                         "python3 -m unittest discover -s tests -p test_api.py")

    def test_kotlin_and_java_suites_are_found_and_run_with_the_build_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unit = root / "app" / "src" / "test" / "kotlin" / "com" / "x"
            unit.mkdir(parents=True)
            (unit / "LobbyTest.kt").write_text("class LobbyTest {\n  @Test\n  fun keepsSeat() {}\n  @Test fun `rejoins after a drop`() {}\n}\n")
            java = root / "lib" / "src" / "test" / "java"
            java.mkdir(parents=True)
            (java / "ParserTests.java").write_text("public class ParserTests {\n  @Test\n  public void parses() {}\n}\n")
            (root / "app" / "src" / "main").mkdir(parents=True)
            (root / "app" / "src" / "main" / "Lobby.kt").write_text("class Lobby { fun seat() {} }\n")
            suites = {s["name"]: s for s in dev_console.discover_test_suites(root)}
            self.assertEqual(set(suites), {"LobbyTest", "ParserTests"})
            self.assertEqual((suites["LobbyTest"]["language"], suites["LobbyTest"]["test_count"]), ("kotlin", 2))
            self.assertEqual(suites["ParserTests"]["language"], "java")

            (root / "gradlew").write_text("")
            config = make_config(root, xcode_project=None, scheme=None, test_target="", build_command="./gradlew build",
                                 test_command="./gradlew test")
            self.assertEqual(dev_console.test_command_for_suite(suites["LobbyTest"], config), "./gradlew test --tests LobbyTest")
            (root / "pom.xml").write_text("")
            self.assertEqual(dev_console.test_command_for_suite(suites["ParserTests"], config), "mvn -q test -Dtest=ParserTests")


if __name__ == "__main__":
    unittest.main()
