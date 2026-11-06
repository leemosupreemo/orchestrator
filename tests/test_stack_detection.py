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


if __name__ == "__main__":
    unittest.main()
