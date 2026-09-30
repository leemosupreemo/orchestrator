from __future__ import annotations

import argparse
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

    def test_remove_and_prune_project_commands(self) -> None:
        from orchestrator.project_config import load_recent_projects, remember_project

        with tempfile.TemporaryDirectory(prefix="orchestrator-proj1-") as dir1, \
             tempfile.TemporaryDirectory(prefix="orchestrator-proj2-") as dir2:
            p1 = Path(dir1)
            p2 = Path(dir2)
            (p1 / "App1.xcodeproj").mkdir()
            (p2 / "App2.xcodeproj").mkdir()

            remember_project(p1, "App1")
            remember_project(p2, "App2")

            data = load_recent_projects()
            self.assertEqual(len(data["projects"]), 2)

            # Test remove command
            remove_out = io.StringIO()
            with redirect_stdout(remove_out):
                self.assertEqual(cli.main(["remove", "App1"]), 0)
            self.assertIn("Removed 'App1'", remove_out.getvalue())

            data = load_recent_projects()
            self.assertEqual(len(data["projects"]), 1)
            self.assertEqual(data["projects"][0]["name"], "App2")

        # Now dir2 is deleted (exited context manager)
        prune_out = io.StringIO()
        with redirect_stdout(prune_out):
            self.assertEqual(cli.main(["prune"]), 0)
        self.assertIn("No recent projects", prune_out.getvalue())

        data = load_recent_projects()
        self.assertEqual(len(data["projects"]), 0)

    def test_load_recent_projects_auto_prunes_missing_dirs(self) -> None:
        from orchestrator.project_config import load_recent_projects, save_recent_projects

        save_recent_projects({
            "version": 1,
            "active": "GhostApp",
            "projects": [
                {"name": "GhostApp", "root": "/non/existent/path/ghost"},
            ]
        })

        data = load_recent_projects(prune_missing=True)
        self.assertEqual(data["projects"], [])
        self.assertIsNone(data["active"])

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

    @patch("orchestrator.scripts.dev_console.get_github_auth_info", return_value=(False, [], None))
    @patch("orchestrator.scripts.discover_machines.get_local_ssh_hosts", return_value=[])
    @patch("orchestrator.cli.prompt_text", side_effect=["", ""])
    @patch("orchestrator.cli.prompt_yes_no", return_value=False)
    @patch("orchestrator.cli.run_script", return_value=0)
    def test_wizard_requires_github_before_applying(self, _run, _yes_no, _text, _hosts, _gh) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-wizard-") as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()
            output = io.StringIO()
            with redirect_stdout(output):
                result = cli.main(["wizard", "--root", str(root), "--project-name", "SampleApp", "--models", "codex"])
            self.assertEqual(result, 1)
            self.assertIn("GitHub is required", output.getvalue())
            self.assertFalse((root / ".orchestrator" / "project.json").exists())

    @patch("orchestrator.scripts.dev_console.get_github_auth_info", return_value=(True, [{"user": "devuser", "host": "github.com", "active": True}], "devuser"))
    @patch("orchestrator.scripts.discover_machines.get_local_ssh_hosts", return_value=[])
    @patch("orchestrator.cli.prompt_text")
    @patch("orchestrator.cli.prompt_yes_no")
    @patch("orchestrator.cli.prompt_radio")
    @patch("orchestrator.cli.run_script", return_value=0)
    @patch("orchestrator.scripts.setup_distribution.installed_provisioning_profile_names", return_value={"Profile A", "Profile B"})
    @patch("orchestrator.cli.prompt_password")
    def test_wizard_interactive_profile_selection(self, mock_prompt_password, mock_installed_profiles, mock_run_script, mock_prompt_radio, mock_prompt_yes_no, mock_prompt_text, mock_get_local_ssh_hosts, _mock_gh) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-wizard-") as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()

            # Configure delivery, skip ASC/keychain, then skip Xcode settings check.
            mock_prompt_yes_no.side_effect = [True, False, False, False]
            mock_prompt_text.side_effect = [
                "", "4",  # Accept detected project; select delivery
                "SampleApp/GoogleService-Info.plist", "ABC123DEFG", "ad-hoc",
                "",  # Apply
            ]
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

    @patch("orchestrator.scripts.dev_console.get_github_auth_info", return_value=(True, [{"user": "devuser", "host": "github.com", "active": True}], "devuser"))
    @patch("orchestrator.cli.run_script", return_value=0)
    @patch("orchestrator.cli.prompt_text", side_effect=lambda label, default="", **kwargs: default)
    @patch("orchestrator.cli.prompt_yes_no", return_value=False)
    def test_wizard_confirms_existing_settings_without_changes(self, mock_prompt_yes_no, mock_prompt_text, mock_run_script, _mock_gh) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-wizard-confirm-") as temp_dir:
            root = Path(temp_dir)
            (root / "ExistingApp.xcodeproj").mkdir()

            # Pre-populate project configuration
            cli.init_project(argparse.Namespace(
                root=str(root),
                project=None,
                project_name="ExistingApp",
                scheme="CustomScheme",
                test_target="CustomTests",
                base_branch="develop",
                force=True,
                with_starter_docs=True,
                with_helper_script=True,
            ))

            runtime = root / ".orchestrator"
            project_p = runtime / "project.json"
            p_data = json.loads(project_p.read_text(encoding="utf-8"))
            p_data["firebase_distribution"] = True
            p_data["development_team"] = "TEAM12345"
            p_data["delivery_method"] = "ad-hoc"
            p_data["firebase_plist_path"] = "ExistingApp/GoogleService-Info.plist"
            project_p.write_text(json.dumps(p_data, indent=2) + "\n", encoding="utf-8")

            # Run wizard with existing config
            # Incomplete Firebase fixture: isolate persistence from distribution validation.
            with patch("orchestrator.cli.validate_config_command", return_value=0):
                result = cli.main([
                    "wizard", "--root", str(root), "--models", "codex,claude",
                ])

            self.assertEqual(result, 0)
            # Verify project config was preserved exactly as initialized
            final_p_data = json.loads(project_p.read_text(encoding="utf-8"))
            self.assertEqual(final_p_data["project_name"], "ExistingApp")
            self.assertEqual(final_p_data["scheme"], "CustomScheme")
            self.assertEqual(final_p_data["test_target"], "CustomTests")
            self.assertEqual(final_p_data["base_branch"], "develop")
            self.assertEqual(final_p_data["development_team"], "TEAM12345")
            self.assertEqual(final_p_data["firebase_distribution"], True)

    @patch("orchestrator.scripts.dev_console.get_github_auth_info", return_value=(True, [{"user": "devuser", "host": "github.com", "active": True}], "devuser"))
    @patch("orchestrator.cli.run_script", return_value=0)
    @patch("orchestrator.cli.prompt_text")
    @patch("orchestrator.cli.prompt_yes_no")
    def test_wizard_modifies_existing_settings_when_user_requests_change(self, mock_prompt_yes_no, mock_prompt_text, mock_run_script, _mock_gh) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-wizard-modify-") as temp_dir:
            root = Path(temp_dir)
            (root / "OldApp.xcodeproj").mkdir()

            cli.init_project(argparse.Namespace(
                root=str(root),
                project=None,
                project_name="OldApp",
                scheme="OldScheme",
                test_target="OldTests",
                base_branch="main",
                force=True,
                with_starter_docs=True,
                with_helper_script=True,
            ))

            mock_prompt_yes_no.side_effect = [False]  # Skip Xcode settings check
            mock_prompt_text.side_effect = [
                "1", "NewApp", "2", "NewScheme", "3", "NewTests", "4", "release",
                "",  # Finish editing fields
                "",  # Skip optional tools
                "",  # Apply
            ]

            result = cli.main([
                "wizard",
                "--root",
                str(root),
                "--models",
                "codex",
            ])

            self.assertEqual(result, 0)
            final_p = json.loads((root / ".orchestrator" / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(final_p["project_name"], "NewApp")
            self.assertEqual(final_p["scheme"], "NewScheme")
            self.assertEqual(final_p["test_target"], "NewTests")
            self.assertEqual(final_p["base_branch"], "release")

    @patch("orchestrator.cli.run_script", return_value=0)
    def test_distribute_command_dispatches_to_smoke_test_delivery(self, mock_run_script) -> None:
        self.assertEqual(cli.main(["distribute", "--notes", "Quick test build"]), 0)
        mock_run_script.assert_called_with("smoke_test_delivery.py", [])
        self.assertEqual(os.environ.get("DISTRIBUTION_RELEASE_NOTES"), "Quick test build")

    def test_audit_project_setup_detects_all_pillars_and_gaps(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-audit-") as temp_dir:
            root = Path(temp_dir)
            # Empty directory: should detect gaps for project.json, grounding docs, git, machines.json
            audit = cli.audit_project_setup(root)
            self.assertIn("Project & Xcode", audit["categories"])
            self.assertIn("Grounding & Documentation", audit["categories"])
            self.assertIn("AI Providers & Models", audit["categories"])
            self.assertIn("GitHub Source Control", audit["categories"])
            self.assertIn("Worker Fleet", audit["categories"])
            self.assertIn("Distribution & Signing", audit["categories"])

            self.assertGreater(audit["gap_count"], 0)
            self.assertTrue(any("project.json" in gap.lower() for gap in audit["gaps"]))

            # Verify audit report prints without error
            output = io.StringIO()
            with redirect_stdout(output):
                cli.print_setup_audit_report(audit)
            self.assertIn("Setup Status & Gaps Audit", output.getvalue())

    def test_audit_project_setup_complete_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-audit-complete-") as temp_dir:
            root = Path(temp_dir)
            (root / ".git").mkdir()
            (root / "TestApp.xcodeproj").mkdir()

            # Initialize project with starter docs
            cli.init_project(argparse.Namespace(
                root=str(root),
                project=None,
                project_name="TestApp",
                scheme="TestApp",
                test_target="TestAppTests",
                base_branch="main",
                force=True,
                with_starter_docs=True,
                with_helper_script=True
            ))

            audit = cli.audit_project_setup(root)
            # Verify project & grounding items are now passing
            proj_items = {item["name"]: item["status"] for item in audit["categories"]["Project & Xcode"]}
            doc_items = {item["name"]: item["status"] for item in audit["categories"]["Grounding & Documentation"]}

            self.assertEqual(proj_items["Git Repository"], "ok")
            self.assertEqual(proj_items["Project Config"], "ok")
            self.assertEqual(doc_items["AGENTS.md"], "ok")
            self.assertEqual(doc_items["Build Commands"], "ok")
            self.assertEqual(doc_items["Architecture"], "ok")
            self.assertEqual(doc_items["Coding Standards"], "ok")
            self.assertEqual(doc_items["AI Workflow"], "ok")


if __name__ == "__main__":
    unittest.main()

