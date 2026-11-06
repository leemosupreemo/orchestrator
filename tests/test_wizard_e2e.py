from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import cli
from orchestrator.config_validation import validate_project_config
from orchestrator.project_config import load_project_config
from orchestrator.scripts import dev_console
from orchestrator.scripts.generate_test_index import generate_test_index


class WizardEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = tempfile.TemporaryDirectory(prefix="orchestrator-test-state-")
        self.old_state_dir = os.environ.get("ORCHESTRATOR_USER_STATE_DIR")
        self.old_project_root = os.environ.get("ORCHESTRATOR_PROJECT_ROOT")
        os.environ["ORCHESTRATOR_USER_STATE_DIR"] = self.state_dir.name

    def tearDown(self) -> None:
        if self.old_state_dir is None:
            os.environ.pop("ORCHESTRATOR_USER_STATE_DIR", None)
        else:
            os.environ["ORCHESTRATOR_USER_STATE_DIR"] = self.old_state_dir
        if self.old_project_root is None:
            os.environ.pop("ORCHESTRATOR_PROJECT_ROOT", None)
        else:
            os.environ["ORCHESTRATOR_PROJECT_ROOT"] = self.old_project_root
        self.state_dir.cleanup()

    @patch("orchestrator.scripts.discover_machines.get_local_ssh_hosts", return_value=[])
    @patch("orchestrator.scripts.dev_console.get_github_auth_info", return_value=(True, [{"user": "octocat", "host": "github.com", "active": True}], "octocat"))
    @patch("orchestrator.cli.test_ssh_connection", return_value=True)
    @patch("orchestrator.cli.run_script", return_value=0)
    def test_end_to_end_wizard_python_stack(self, mock_run_script, mock_ssh_test, _mock_gh, _mock_hosts) -> None:
        """
        Comprehensive test of an interactive wizard setup for a Python/FastAPI service:
        - Detects Python stack
        - Walks through audit, project config, AI models, GitHub auth, SSH fleet, starter docs
        - Confirms Xcode/Firebase prompts are bypassed
        - Validates resulting project.json, machines.json, starter docs, and test discovery
        """
        with tempfile.TemporaryDirectory(prefix="python-service-") as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "pyproject.toml").write_text(
                "[project]\nname = \"auth-service\"\nversion = \"0.1.0\"\n"
                "dependencies = [\"fastapi\", \"pytest\"]\n"
            )
            (root / "src").mkdir()
            (root / "src" / "main.py").write_text("def run():\n    print('Running auth-service')\n")
            (root / "tests").mkdir()
            (root / "tests" / "test_auth.py").write_text(
                "import unittest\n\n"
                "class AuthTest(unittest.TestCase):\n"
                "    def test_login(self):\n"
                "        self.assertTrue(True)\n"
                "    def test_token_refresh(self):\n"
                "        self.assertTrue(True)\n"
            )

            prompt_text_responses = [
                "1", "auth-service",
                "3", "python3 -m compileall -q src",
                "4", "pytest",
                "",  # Accept project settings
                "1,2",  # Configure GitHub and workers
                "",  # keep active GitHub account
                "ci-worker",
                "deploy@192.168.1.50",
                "/opt/repos/auth-service",
                "",  # Apply
            ]

            prompt_yes_no_responses = [
                True,   # add SSH worker manually
                True,   # install worker packages
            ]

            stdout_buf = io.StringIO()
            with patch("orchestrator.cli.prompt_text", side_effect=prompt_text_responses) as mock_ptext, \
                 patch("orchestrator.cli.prompt_yes_no", side_effect=prompt_yes_no_responses) as mock_pyn, \
                 redirect_stdout(stdout_buf):

                ret = cli.main([
                    "wizard",
                    "--root", str(root),
                    "--models", "claude,codex",
                    "--verify",
                ])

            out = stdout_buf.getvalue()

            # 1. Verify exit code
            self.assertEqual(ret, 0)

            # 2. Verify wizard stage output
            self.assertIn("Orchestrator Wizard", out)
            self.assertIn("[4/5] Review and apply", out)
            self.assertIn("[1/5] Project", out)
            self.assertIn("Detected stack: Python (pytest)", out)
            self.assertIn("Authenticated as octocat", out)
            # Ensure Xcode-specific signing was NOT prompted
            self.assertNotIn("iOS Signing Configuration", out)
            self.assertNotIn("Xcode Scheme", out)

            # 3. Verify .orchestrator/project.json
            project_file = root / ".orchestrator" / "project.json"
            self.assertTrue(project_file.exists())
            p_data = json.loads(project_file.read_text(encoding="utf-8"))
            self.assertEqual(p_data["project_name"], "auth-service")
            self.assertEqual(p_data["build_command"], "python3 -m compileall -q src")
            self.assertEqual(p_data["test_command"], "pytest")
            self.assertEqual(p_data["base_branch"], "main")
            self.assertIsNone(p_data["scheme"])
            self.assertEqual(p_data["test_target"], "")
            self.assertFalse(p_data["firebase_distribution"])

            # 4. Verify machines.json (local + remote worker)
            machines_file = root / ".orchestrator" / "config" / "machines.json"
            self.assertTrue(machines_file.exists())
            m_data = json.loads(machines_file.read_text(encoding="utf-8"))
            machines_by_name = {m["name"]: m for m in m_data["machines"]}

            # Local machine shouldn't require Xcode/Simulator
            local_m = machines_by_name["local"]
            self.assertFalse(local_m["supports_xcode"])
            self.assertFalse(local_m["supports_simulator"])
            self.assertEqual(local_m["models"], ["claude", "codex"])

            # SSH remote machine
            self.assertIn("ci-worker", machines_by_name)
            remote_m = machines_by_name["ci-worker"]
            self.assertEqual(remote_m["ssh_target"], "deploy@192.168.1.50")
            self.assertEqual(remote_m["repo_path"], "/opt/repos/auth-service")

            # 5. Verify generated starter docs are contextual to Python
            agents_md = (root / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("auth-service", agents_md)

            build_test_md = (root / "docs" / "build-test-commands.md").read_text(encoding="utf-8")
            self.assertIn("python3 -m compileall -q src", build_test_md)
            self.assertIn("pytest", build_test_md)

            arch_md = (root / "docs" / "architecture.md").read_text(encoding="utf-8")
            self.assertIn("Platform:** Python", arch_md)
            self.assertIn("Domain / Models", arch_md)

            standards_md = (root / "docs" / "coding-standards.md").read_text(encoding="utf-8")
            self.assertIn("Python Standards", standards_md)
            self.assertIn("PEP 8", standards_md)

            # 6. Verify test discovery finds the test suite
            suites = dev_console.discover_test_suites(root)
            self.assertEqual(len(suites), 1)
            self.assertEqual(suites[0]["language"], "python")
            self.assertEqual(suites[0]["name"], "AuthTest")
            self.assertEqual(suites[0]["test_count"], 2)

            # 7. Verify test index output for Builder/Planner agent prompt
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(root)
                cfg = load_project_config()
                test_idx = generate_test_index(root, project_config=cfg)
                self.assertIn("pytest tests/test_auth.py", test_idx)
                errors = validate_project_config(cfg)
                self.assertEqual(errors, [])
            finally:
                os.environ.pop("ORCHESTRATOR_PROJECT_ROOT", None)
                os.chdir(old_cwd)

    @patch("orchestrator.scripts.discover_machines.get_local_ssh_hosts", return_value=[])
    @patch("orchestrator.scripts.dev_console.get_github_auth_info", return_value=(True, [{"user": "rustacean", "host": "github.com", "active": True}], "rustacean"))
    @patch("orchestrator.cli.test_ssh_connection", return_value=True)
    @patch("orchestrator.cli.run_script", return_value=0)
    def test_end_to_end_wizard_rust_stack(self, mock_run_script, mock_ssh_test, _mock_gh, _mock_hosts) -> None:
        """
        Comprehensive test of an interactive wizard setup for a Rust crate:
        - Detects Rust (Cargo)
        - Accepts cargo defaults for build & test commands
        - Validates generated docs and config integrity
        """
        with tempfile.TemporaryDirectory(prefix="rust-crate-") as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "Cargo.toml").write_text("[package]\nname = \"tokenizer\"\nversion = \"0.2.0\"\n")
            (root / "src").mkdir()
            (root / "src" / "lib.rs").write_text("pub fn tokenize(s: &str) -> Vec<&str> { s.split_whitespace().collect() }\n")
            (root / "tests").mkdir()
            (root / "tests" / "test_tokens.rs").write_text(
                "#[test]\nfn test_words() {\n    assert_eq!(tokenizer::tokenize(\"hello world\"), vec![\"hello\", \"world\"]);\n}\n"
            )

            prompt_text_responses = [
                "1", "tokenizer",
                "",  # Accept remaining detected settings
                "",  # Skip optional tools
                "",  # Apply
            ]

            prompt_yes_no_responses = [
                # No optional sections selected.
            ]

            stdout_buf = io.StringIO()
            with patch("orchestrator.cli.prompt_text", side_effect=prompt_text_responses), \
                 patch("orchestrator.cli.prompt_yes_no", side_effect=prompt_yes_no_responses), \
                 redirect_stdout(stdout_buf):

                ret = cli.main([
                    "wizard",
                    "--root", str(root),
                    "--models", "claude",
                    "--verify",
                ])

            out = stdout_buf.getvalue()
            self.assertEqual(ret, 0)
            self.assertIn("[1/5] Project", out)
            self.assertIn("Detected stack: Rust (Cargo)", out)

            project_file = root / ".orchestrator" / "project.json"
            p_data = json.loads(project_file.read_text(encoding="utf-8"))
            self.assertEqual(p_data["project_name"], "tokenizer")
            self.assertEqual(p_data["build_command"], "cargo build")
            self.assertEqual(p_data["test_command"], "cargo test")

            standards_md = (root / "docs" / "coding-standards.md").read_text(encoding="utf-8")
            self.assertIn("Rust Standards", standards_md)
            self.assertIn("cargo fmt", standards_md)

            # Test discovery for Rust
            suites = dev_console.discover_test_suites(root)
            self.assertEqual(len(suites), 1)
            self.assertEqual(suites[0]["language"], "rust")
            self.assertEqual(suites[0]["name"], "test_tokens")

            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(root)
                cfg = load_project_config()
                test_idx = generate_test_index(root, project_config=cfg)
                self.assertIn("cargo test --test test_tokens", test_idx)
                errors = validate_project_config(cfg)
                self.assertEqual(errors, [])
            finally:
                os.environ.pop("ORCHESTRATOR_PROJECT_ROOT", None)
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
