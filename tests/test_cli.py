from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from orchestrator import cli  # noqa: E402


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = tempfile.TemporaryDirectory(prefix="orchestrator-user-state-")
        self.old_state_dir = os.environ.get("ORCHESTRATOR_USER_STATE_DIR")
        os.environ["ORCHESTRATOR_USER_STATE_DIR"] = self.state_dir.name

    def tearDown(self) -> None:
        if self.old_state_dir is None:
            os.environ.pop("ORCHESTRATOR_USER_STATE_DIR", None)
        else:
            os.environ["ORCHESTRATOR_USER_STATE_DIR"] = self.old_state_dir
        self.state_dir.cleanup()

    def test_init_project_scaffolds_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-init-") as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()

            result = cli.main([
                "init",
                "--root",
                str(root),
                "--project-name",
                "SampleApp",
                "--base-branch",
                "develop",
            ])

            self.assertEqual(result, 0)
            runtime = root / ".orchestrator"
            project = json.loads((runtime / "project.json").read_text(encoding="utf-8"))
            machines = json.loads((runtime / "config" / "machines.json").read_text(encoding="utf-8"))
            settings = json.loads((runtime / "config" / "settings.json").read_text(encoding="utf-8"))
            gitignore = (runtime / ".gitignore").read_text(encoding="utf-8")

            self.assertEqual(project["project_name"], "SampleApp")
            self.assertEqual(project["base_branch"], "develop")
            self.assertEqual(project["xcode_project"], "SampleApp.xcodeproj")
            self.assertEqual(project["scheme"], "SampleApp")
            self.assertEqual(project["test_target"], "SampleAppTests")
            self.assertEqual(
                Path(machines["machines"][0]["repo_path"]).resolve(),
                root.resolve(),
            )
            self.assertEqual(settings["notification_emails"], [])
            self.assertIn("jobs/", gitignore)
            self.assertIn("logs/", gitignore)
            self.assertIn("output/", gitignore)
            self.assertIn("state/", gitignore)

    def test_init_project_accepts_project_alias_and_remembers_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-init-") as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()

            result = cli.main([
                "init",
                "--project",
                str(root),
                "--project-name",
                "SampleApp",
            ])

            self.assertEqual(result, 0)
            recent = json.loads((Path(self.state_dir.name) / "projects.json").read_text(encoding="utf-8"))
            self.assertEqual(recent["active"], "SampleApp")
            self.assertEqual(recent["projects"][0]["root"], str(root.resolve()))

    def test_projects_and_use_commands_manage_recent_project_selection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-use-") as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()
            self.assertEqual(cli.main(["init", "--project", str(root), "--project-name", "SampleApp"]), 0)

            list_output = io.StringIO()
            with redirect_stdout(list_output):
                self.assertEqual(cli.main(["projects"]), 0)
            self.assertIn("* Project: SampleApp", list_output.getvalue())

            use_output = io.StringIO()
            with redirect_stdout(use_output):
                self.assertEqual(cli.main(["use", "SampleApp"]), 0)
            self.assertIn("Project: SampleApp", use_output.getvalue())
            self.assertIn(str(root.resolve()), use_output.getvalue())

    def test_init_project_can_generate_starter_docs_and_helper_script(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-init-") as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()

            result = cli.main([
                "init",
                "--root",
                str(root),
                "--project-name",
                "SampleApp",
                "--with-starter-docs",
                "--with-helper-script",
            ])

            self.assertEqual(result, 0)
            build_docs = (root / "docs" / "build-test-commands.md").read_text(encoding="utf-8")
            workflow_docs = (root / "docs" / "ai-workflow.md").read_text(encoding="utf-8")
            agents_docs = (root / "AGENTS.md").read_text(encoding="utf-8")
            helper = root / "scripts" / "orchestrator"

            self.assertIn("xcodebuild build -project SampleApp.xcodeproj -scheme SampleApp", build_docs)
            self.assertIn("xcodebuild test -project SampleApp.xcodeproj -scheme SampleApp", build_docs)
            self.assertIn("orchestrator console", workflow_docs)
            self.assertIn("Run `orchestrator check-config`", agents_docs)
            self.assertTrue(helper.exists())
            self.assertTrue(helper.stat().st_mode & 0o111)

    @patch("orchestrator.cli.subprocess.run")
    def test_init_project_uses_xcodebuild_json_for_scheme_and_targets(self, mock_run) -> None:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "project": {
                    "schemes": ["AppScheme"],
                    "targets": ["App", "AppUnitTests"],
                }
            }),
            stderr="",
        )
        with tempfile.TemporaryDirectory(prefix="orchestrator-init-") as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()

            result = cli.main(["init", "--root", str(root)])

            self.assertEqual(result, 0)
            project = json.loads((root / ".orchestrator" / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(project["scheme"], "AppScheme")
            self.assertEqual(project["test_target"], "AppUnitTests")
            self.assertEqual(project["detected_schemes"], ["AppScheme"])
            self.assertEqual(project["detected_targets"], ["App", "AppUnitTests"])

    def test_wizard_configures_first_run_assets_non_interactively(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-wizard-") as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()

            result = cli.main([
                "wizard",
                "--root",
                str(root),
                "--project-name",
                "SampleApp",
                "--models",
                "codex,gemini",
                "--copy-prompt-overrides",
                "--ssh-machine",
                "mac2=remote-host:/Users/me/SampleApp",
                "--firebase",
                "--distribution-script-path",
                "scripts/distribute_ios.sh",
                "--firebase-plist-path",
                "SampleApp/GoogleService-Info.plist",
                "--team-id",
                "ABC123DEFG",
                "--method",
                "ad-hoc",
                "--non-interactive",
            ])

            self.assertEqual(result, 0)
            runtime = root / ".orchestrator"
            project = json.loads((runtime / "project.json").read_text(encoding="utf-8"))
            machines = json.loads((runtime / "config" / "machines.json").read_text(encoding="utf-8"))

            self.assertTrue((root / "AGENTS.md").exists())
            self.assertTrue((root / "docs" / "build-test-commands.md").exists())
            self.assertTrue((root / "scripts" / "orchestrator").exists())
            self.assertTrue((runtime / "prompts" / "builder_bug.md").exists())
            self.assertEqual(project["delivery_provider"], "firebase")
            self.assertTrue(project["firebase_distribution"])
            self.assertEqual(project["distribution_script_path"], "scripts/distribute_ios.sh")
            self.assertEqual(project["firebase_plist_path"], "SampleApp/GoogleService-Info.plist")

            machine_index = {machine["name"]: machine for machine in machines["machines"]}
            self.assertEqual(machine_index["local"]["models"], ["codex", "gemini"])
            self.assertEqual(machine_index["mac2"]["execution_mode"], "ssh")
            self.assertEqual(machine_index["mac2"]["ssh_target"], "remote-host")
            self.assertEqual(machine_index["mac2"]["repo_path"], "/Users/me/SampleApp")
            self.assertEqual(machine_index["mac2"]["models"], ["codex", "gemini"])

    def test_wizard_requires_model_in_non_interactive_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-wizard-") as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()

            result = cli.main([
                "wizard",
                "--root",
                str(root),
                "--project-name",
                "SampleApp",
                "--non-interactive",
            ])

            self.assertEqual(result, 1)
            self.assertFalse((root / ".orchestrator").exists())

    def test_wizard_requires_complete_firebase_args_before_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-wizard-") as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()

            result = cli.main([
                "wizard",
                "--root",
                str(root),
                "--project-name",
                "SampleApp",
                "--models",
                "codex",
                "--firebase",
                "--distribution-script-path",
                "scripts/distribute_ios.sh",
                "--non-interactive",
            ])

            self.assertEqual(result, 1)
            self.assertFalse((root / ".orchestrator").exists())

    @patch("orchestrator.cli.validate_config_command", return_value=0)
    @patch("orchestrator.cli.run_script", return_value=0)
    def test_wizard_verify_runs_checks_and_worker_install(self, mock_run_script, _validate) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-wizard-") as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()

            result = cli.main([
                "wizard",
                "--root",
                str(root),
                "--project-name",
                "SampleApp",
                "--models",
                "codex",
                "--ssh-machine",
                "mac2=remote-host:/Users/me/SampleApp",
                "--verify",
                "--install-workers",
                "--non-interactive",
            ])

            self.assertEqual(result, 0)
            self.assertIn(("check_setup.py", []), [call.args for call in mock_run_script.call_args_list])
            self.assertIn(
                ("worker_tools.py", ["install", "--machine", "mac2"]),
                [call.args for call in mock_run_script.call_args_list],
            )
            self.assertIn(
                ("worker_tools.py", ["check", "--machine", "mac2"]),
                [call.args for call in mock_run_script.call_args_list],
            )

    @patch("orchestrator.cli.validate_machine_config", return_value=[])
    @patch("orchestrator.cli.validate_project_config", return_value=[])
    def test_check_config_returns_success_for_valid_config(self, _project, _machines) -> None:
        self.assertEqual(cli.main(["check-config"]), 0)

    @patch("orchestrator.cli.validate_machine_config", return_value=["machines error"])
    @patch("orchestrator.cli.validate_project_config", return_value=["project error"])
    def test_check_config_returns_failure_for_errors(self, _project, _machines) -> None:
        self.assertEqual(cli.main(["check-config"]), 1)

    @patch("orchestrator.cli.validate_machine_config", return_value=[])
    @patch("orchestrator.cli.validate_project_config", return_value=[])
    def test_check_config_reports_distribution_errors(self, _project, _machines) -> None:
        config = MagicMock()
        config.config_dir = Path("/tmp/orchestrator-config")
        config.firebase_distribution = True
        config.validate_distribution_config.return_value = ["distribution script is outdated"]

        output = io.StringIO()
        with patch("orchestrator.cli.load_project_config", return_value=config), redirect_stdout(output):
            self.assertEqual(cli.main(["check-config"]), 1)

        self.assertIn("distribution script is outdated", output.getvalue())

    @patch("orchestrator.cli.run_script", return_value=0)
    def test_worker_commands_dispatch_to_worker_tools(self, mock_run_script) -> None:
        self.assertEqual(cli.main(["worker-check", "--machine", "mac2"]), 0)
        mock_run_script.assert_called_with("worker_tools.py", ["check", "--machine", "mac2"])

        self.assertEqual(cli.main(["worker-install"]), 0)
        mock_run_script.assert_called_with("worker_tools.py", ["install"])

    @patch("orchestrator.scripts.discover_machines.get_local_ssh_hosts", return_value=[])
    @patch("orchestrator.cli.prompt_text")
    @patch("orchestrator.cli.prompt_yes_no")
    @patch("orchestrator.cli.prompt_radio")
    @patch("orchestrator.cli.run_script", return_value=0)
    @patch("orchestrator.scripts.setup_distribution.installed_provisioning_profile_names", return_value={"Profile A", "Profile B"})
    @patch("orchestrator.cli.prompt_password")
    def test_wizard_interactive_profile_selection(self, mock_prompt_password, mock_installed_profiles, mock_run_script, mock_prompt_radio, mock_prompt_yes_no, mock_prompt_text, mock_get_local_ssh_hosts) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-wizard-") as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()

            mock_prompt_yes_no.side_effect = [False, False, True, False, False, False, False]
            mock_prompt_text.side_effect = ["SampleApp/GoogleService-Info.plist", "ABC123DEFG", "ad-hoc"]
            mock_prompt_radio.return_value = "Profile B"

            result = cli.main([
                "wizard",
                "--root",
                str(root),
                "--project-name",
                "SampleApp",
                "--models",
                "codex",
            ])

            self.assertEqual(result, 0)
            mock_prompt_radio.assert_called_once()
            # Ensure the selected profile was passed to setup_distribution.py
            calls = [call.args for call in mock_run_script.call_args_list]
            setup_args = next(args for script, args in calls if script == "setup_distribution.py")
            self.assertIn("--provisioning-profile", setup_args)
            self.assertIn("Profile B", setup_args)

    @patch("orchestrator.web.local_server.start_local_ui_server")
    def test_ui_and_web_commands_dispatch(self, mock_start_server) -> None:
        self.assertEqual(cli.main(["ui", "--port", "5050", "--no-open"]), 0)
        mock_start_server.assert_called_with(host="0.0.0.0", port=5050, open_browser=False, project_root=None)

        self.assertEqual(cli.main(["web", "--port", "6060"]), 0)
        mock_start_server.assert_called_with(host="0.0.0.0", port=6060, open_browser=True, project_root=None)


if __name__ == "__main__":
    unittest.main()

