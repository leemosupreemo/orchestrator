from __future__ import annotations

import json
import plistlib
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import deliver_build  # noqa: E402
import simulator_visual_check  # noqa: E402


class VisualDeliveryConfigTests(unittest.TestCase):
    def test_distribution_script_options_reflect_project_script_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "ship.sh"
            script.write_text(
                "case $1 in\n  --project) ;;\n  --groups) ;;\n  --release-notes) ;;\nesac\n",
                encoding="utf-8",
            )

            self.assertEqual(
                deliver_build.distribution_script_options(script),
                {"--project", "--groups", "--release-notes"},
            )

    def test_delivery_receipt_reads_exported_app_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ipa_path = Path(temp_dir) / "Thirteen.ipa"
            with zipfile.ZipFile(ipa_path, "w") as archive:
                archive.writestr(
                    "Payload/Thirteen.app/Info.plist",
                    plistlib.dumps({"CFBundleShortVersionString": "1.1", "CFBundleVersion": "187"}),
                )

            receipt = deliver_build.build_delivery_receipt(
                f"Distribution complete\nIPA: {ipa_path}\n",
                testers=None,
                groups="internal-testers",
            )

            self.assertEqual(receipt["version"], "1.1")
            self.assertEqual(receipt["build"], "187")
            self.assertEqual(receipt["ipa_path"], str(ipa_path))
            self.assertEqual(receipt["groups"], "internal-testers")
            self.assertEqual(len(receipt["sha256"]), 64)

    def test_visual_check_command_replaces_destination(self) -> None:
        command = "xcodebuild build -project App.xcodeproj -destination old-destination"

        updated = simulator_visual_check.command_with_destination(command, "platform=iOS Simulator,id=NEW")

        self.assertEqual(updated.count("-destination"), 1)
        self.assertIn("'platform=iOS Simulator,id=NEW'", updated)
        self.assertNotIn("old-destination", updated)

    @patch("deliver_build.send_final_notification")
    @patch("deliver_build.ensure_keychain_unlocked", return_value=(True, "Keychain is unlocked"))
    @patch("deliver_build.subprocess.Popen")
    @patch("deliver_build.subprocess.check_output")
    def test_deliver_build_uses_configured_distribution_script(self, mock_check_output, mock_popen, _unlock, _notify) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()
            (root / "tools").mkdir()
            script = root / "tools" / "ship.sh"
            script.write_text(
                "case $1 in --project|--release-notes|--groups) ;; esac\n",
                encoding="utf-8",
            )
            ipa_path = root / "SampleApp.ipa"
            with zipfile.ZipFile(ipa_path, "w") as archive:
                archive.writestr(
                    "Payload/SampleApp.app/Info.plist",
                    plistlib.dumps({"CFBundleShortVersionString": "1.0", "CFBundleVersion": "2"}),
                )
            job_path = root / "job.json"
            job_path.write_text(
                json.dumps({
                    "job_id": "job-1",
                    "title": "Ship build",
                    "branch": "feature/build",
                    "issue_number": 1,
                }),
                encoding="utf-8",
            )

            mock_check_output.side_effect = [b"feature/build\n", b"20260902231500\n", b"2026-06-06 00:00:00\n"]
            process = MagicMock()
            process.stdout = [f"IPA: {ipa_path}\n"]
            process.wait.return_value = 0
            mock_popen.return_value = process
            
            from orchestrator.project_config import ProjectConfig
            config = MagicMock(spec=ProjectConfig)
            config.distribution_script_path = "tools/ship.sh"
            config.xcode_project = "SampleApp.xcodeproj"
            config.validate_distribution_config.return_value = []
            config.scheme = None
            config.xcode_workspace = None
            config.provisioning_profile_specifier = None
            config.development_team = None
            config.delivery_method = None
            config.asc_key_id = None
            config.asc_issuer_id = None
            config.asc_key_path = None
            config.firebase_plist_path = None
            config.firebase_testers = None
            config.firebase_groups = None
            config.runtime_dir = root / ".orchestrator"

            with (
                patch.object(deliver_build, "ROOT", root),
                patch.object(deliver_build, "PROJECT_CONFIG", config),
                self.assertRaises(SystemExit) as exit_context,
            ):
                deliver_build.deliver_build(job_path)

            self.assertEqual(exit_context.exception.code, 0)
            cmd = mock_popen.call_args.args[0]
            self.assertEqual(cmd[0:2], ["/bin/bash", str(script)])
            self.assertIn("--project", cmd)
            self.assertIn(str(root / "SampleApp.xcodeproj"), cmd)
            self.assertNotIn("--build-number", cmd)
            self.assertEqual(cmd[cmd.index("--groups") + 1], "internal-testers")

    @patch("deliver_build.send_final_notification")
    @patch("deliver_build.ensure_keychain_unlocked", return_value=(True, "Keychain is unlocked"))
    @patch("deliver_build.subprocess.Popen")
    @patch("deliver_build.subprocess.check_output")
    def test_successful_script_without_reported_ipa_fails_verification(
        self, mock_check_output, mock_popen, _unlock, mock_notify
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()
            (root / "scripts").mkdir()
            script = root / "scripts" / "distribute_ios.sh"
            script.write_text("case $1 in --groups) ;; esac\n", encoding="utf-8")
            job_path = root / "job.json"
            job_path.write_text(
                json.dumps({"job_id": "job-1", "title": "Ship build", "branch": "main"}),
                encoding="utf-8",
            )
            mock_check_output.side_effect = [b"main\n", b"2026-09-11 17:00:00\n"]
            process = MagicMock()
            process.stdout = ["Firebase command returned successfully\n"]
            process.wait.return_value = 0
            mock_popen.return_value = process

            config = MagicMock()
            config.validate_distribution_config.return_value = []
            config.distribution_script_path = "scripts/distribute_ios.sh"
            config.xcode_project = "SampleApp.xcodeproj"
            config.xcode_workspace = None
            config.scheme = "SampleApp"
            config.runtime_dir = root / ".orchestrator"
            config.firebase_testers = None
            config.firebase_groups = "internal-testers"
            config.provisioning_profile_specifier = None
            config.development_team = None
            config.delivery_method = None
            config.asc_key_id = None
            config.asc_issuer_id = None
            config.asc_key_path = None
            config.firebase_plist_path = None

            with (
                patch.object(deliver_build, "ROOT", root),
                patch.object(deliver_build, "PROJECT_CONFIG", config),
                self.assertRaises(SystemExit) as exit_context,
            ):
                deliver_build.deliver_build(job_path)

            self.assertEqual(exit_context.exception.code, 1)
            mock_notify.assert_called_once_with(unittest.mock.ANY, "Ship build", "main", False)

    @patch("deliver_build.ensure_keychain_unlocked")
    def test_deliver_build_missing_branch_exits_nonzero_without_unlocking_keychain(self, mock_unlock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job_path = root / "job.json"
            job_path.write_text(json.dumps({"job_id": "job-1", "title": "Ship build"}), encoding="utf-8")

            with (
                patch.object(deliver_build, "ROOT", root),
                self.assertRaises(SystemExit) as exit_context,
            ):
                deliver_build.deliver_build(job_path)

            self.assertEqual(exit_context.exception.code, 1)
            mock_unlock.assert_not_called()

    @patch("deliver_build.send_final_notification")
    @patch("deliver_build.ensure_keychain_unlocked", return_value=(True, "Keychain is unlocked"))
    @patch("deliver_build.subprocess.Popen")
    @patch("deliver_build.subprocess.run")
    @patch("deliver_build.subprocess.check_output", return_value=b"main\n")
    def test_deliver_build_refuses_branch_mismatch_without_checkout(
        self, _check_output, mock_run, mock_popen, _unlock, _notify
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()
            (root / "scripts").mkdir()
            (root / "scripts" / "distribute_ios.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            job_path = root / "job.json"
            job_path.write_text(
                json.dumps({"job_id": "job-1", "title": "Ship build", "branch": "feature/build"}),
                encoding="utf-8",
            )

            config = MagicMock()
            config.validate_distribution_config.return_value = []

            with (
                patch.object(deliver_build, "ROOT", root),
                patch.object(deliver_build, "PROJECT_CONFIG", config),
                self.assertRaises(SystemExit) as exit_context,
            ):
                deliver_build.deliver_build(job_path)

            self.assertEqual(exit_context.exception.code, 1)
            mock_run.assert_not_called()
            mock_popen.assert_not_called()

    @patch("simulator_visual_check.plistlib.load")
    @patch("simulator_visual_check.open", create=True)
    @patch("pathlib.Path.exists")
    @patch("simulator_visual_check.get_best_simulator_destination")
    @patch("simulator_visual_check.simulator_udid")
    @patch("simulator_visual_check.stream_command")
    @patch("simulator_visual_check.run_simctl")
    @patch("simulator_visual_check.write_text")
    @patch("simulator_visual_check.time.sleep")
    @patch("simulator_visual_check.cleanup_logs")
    def test_visual_check_auto_resolves_bundle_id(
        self, mock_cleanup_logs, mock_sleep, mock_write_text, mock_run_simctl, mock_stream_cmd,
        mock_udid, mock_best_dest, mock_path_exists, mock_open_file, mock_plist_load
    ) -> None:
        # Mock sys.argv / argparse
        test_args = SimpleNamespace(
            no_build=False,
            bundle_id="",
            app_path="/tmp/test.app",
            wait=3.0,
            screenshots=1,
            interval=1.0,
            job=""
        )

        mock_path_exists.return_value = True
        mock_plist_load.return_value = {"CFBundleIdentifier": "com.test.resolved-bundle-id"}
        mock_best_dest.return_value = "id=test-udid"
        mock_udid.return_value = "test-udid"
        mock_stream_cmd.return_value = True
        mock_run_simctl.return_value = True

        with (
            patch("argparse.ArgumentParser.parse_args", return_value=test_args),
            patch("simulator_visual_check.extract_commands", return_value=("xcodebuild build", "xcodebuild test")),
            patch("pathlib.Path.mkdir"),
            patch("pathlib.Path.unlink"),
            patch("pathlib.Path.is_symlink", return_value=False),
            patch("pathlib.Path.symlink_to"),
        ):
            try:
                simulator_visual_check.main()
            except SystemExit as e:
                self.assertEqual(e.code, 0)

            # Verify simctl launch step was called with the resolved bundle ID
            launch_call = [call for call in mock_run_simctl.call_args_list if call[0][0][0] == "launch"]
            self.assertTrue(len(launch_call) > 0)
            self.assertEqual(launch_call[0][0][0][2], "com.test.resolved-bundle-id")

    @patch("deliver_build.send_final_notification")
    @patch("deliver_build.ensure_keychain_unlocked", return_value=(True, "Keychain is unlocked"))
    @patch("deliver_build.subprocess.Popen")
    @patch("deliver_build.subprocess.check_output")
    @patch("deliver_build.format_markdown_for_terminal")
    def test_deliver_build_diagnoses_signing_team_mismatch(
        self, mock_format_markdown, mock_check_output, mock_popen, _unlock, _notify
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()
            (root / "scripts").mkdir()
            script = root / "scripts" / "distribute_ios.sh"
            script.write_text(
                "case $1 in --project|--release-notes|--groups) ;; esac\n",
                encoding="utf-8",
            )
            job_path = root / "job.json"
            job_path.write_text(
                json.dumps({
                    "job_id": "job-1",
                    "title": "Ship build",
                    "branch": "feature/build",
                    "issue_number": 1,
                }),
                encoding="utf-8",
            )

            mock_check_output.side_effect = [b"feature/build\n", b"20260902231500\n", b"2026-06-06 00:00:00\n"]
            process = MagicMock()
            process.stdout = [
                "/Users/leemosupreemo/Documents/Themis/ThemisPlayground.xcodeproj: "
                "error: No Account for Team \"REC974HW8X\". Add a new account...\n",
                "/Users/leemosupreemo/Documents/Themis/ThemisPlayground.xcodeproj: "
                "error: No profiles for 'nmeskin.Themis' were found: ...\n"
            ]
            process.wait.return_value = 65  # non-zero to trigger error handling
            mock_popen.return_value = process

            mock_format_markdown.side_effect = lambda x: x

            from orchestrator.project_config import ProjectConfig
            config = MagicMock(spec=ProjectConfig)
            config.distribution_script_path = "scripts/distribute_ios.sh"
            config.xcode_project = "SampleApp.xcodeproj"
            config.validate_distribution_config.return_value = []
            config.scheme = None
            config.xcode_workspace = None
            config.provisioning_profile_specifier = None
            config.development_team = "3J93523B6Q"  # Mismatched team
            config.delivery_method = "development"
            config.asc_key_id = "MUD2T6SH8G"
            config.asc_issuer_id = "6b2330c7-0203-4e58-9924-ba5c1f42e1a8"
            config.asc_key_path = "/Users/leemosupreemo/.private_keys/AuthKey_MUD2T6SH8G.p8"
            config.firebase_testers = None
            config.firebase_groups = None
            config.runtime_dir = root / ".orchestrator"

            with (
                patch("builtins.print") as mock_print,
                patch.object(deliver_build, "ROOT", root),
                patch.object(deliver_build, "PROJECT_CONFIG", config),
                self.assertRaises(SystemExit) as exit_context,
            ):
                deliver_build.deliver_build(job_path)

            self.assertEqual(exit_context.exception.code, 65)
            
            # Verify the printed diagnosis message includes details of mismatch
            called_args = [call.args[0] for call in mock_print.call_args_list if call.args]
            printed_output = "\n".join(called_args)
            self.assertIn("Team ID Mismatch Detected!", printed_output)
            self.assertIn("3J93523B6Q", printed_output)
            self.assertIn("REC974HW8X", printed_output)
            self.assertIn("nmeskin.Themis", printed_output)

    @patch("deliver_build.send_final_notification")
    @patch("deliver_build.ensure_keychain_unlocked", return_value=(True, "Keychain is unlocked"))
    @patch("deliver_build.subprocess.Popen")
    @patch("deliver_build.subprocess.check_output")
    @patch("deliver_build.format_markdown_for_terminal")
    def test_deliver_build_diagnoses_firebase_distribution_404(
        self, mock_format_markdown, mock_check_output, mock_popen, _unlock, _notify
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()
            (root / "scripts").mkdir()
            script = root / "scripts" / "distribute_ios.sh"
            script.write_text(
                "case $1 in --project|--release-notes|--testers|--groups) ;; esac\n",
                encoding="utf-8",
            )
            job_path = root / "job.json"
            job_path.write_text(
                json.dumps({
                    "job_id": "job-1",
                    "title": "Ship build",
                    "branch": "feature/build",
                    "issue_number": 1,
                    "testers": "invalid-tester@gmail.com",
                    "groups": "invalid-testers"
                }),
                encoding="utf-8",
            )

            mock_check_output.side_effect = [b"feature/build\n", b"20260902231500\n", b"2026-06-06 00:00:00\n"]
            process = MagicMock()
            process.stdout = [
                "Error: failed to distribute to testers/groups: Request to ... had HTTP Error: 404, Requested entity was not found.\n"
            ]
            process.wait.return_value = 1  # generic non-zero exit code
            mock_popen.return_value = process

            mock_format_markdown.side_effect = lambda x: x

            from orchestrator.project_config import ProjectConfig
            config = MagicMock(spec=ProjectConfig)
            config.distribution_script_path = "scripts/distribute_ios.sh"
            config.xcode_project = "SampleApp.xcodeproj"
            config.validate_distribution_config.return_value = []
            config.scheme = None
            config.xcode_workspace = None
            config.provisioning_profile_specifier = None
            config.development_team = "3J93523B6Q"
            config.delivery_method = "development"
            config.asc_key_id = "MUD2T6SH8G"
            config.asc_issuer_id = "6b2330c7-0203-4e58-9924-ba5c1f42e1a8"
            config.asc_key_path = "/Users/leemosupreemo/.private_keys/AuthKey_MUD2T6SH8G.p8"
            config.firebase_testers = None
            config.firebase_groups = None
            config.runtime_dir = root / ".orchestrator"

            with (
                patch("builtins.print") as mock_print,
                patch.object(deliver_build, "ROOT", root),
                patch.object(deliver_build, "PROJECT_CONFIG", config),
                self.assertRaises(SystemExit) as exit_context,
            ):
                deliver_build.deliver_build(job_path)

            self.assertEqual(exit_context.exception.code, 1)
            
            # Verify the printed diagnosis message includes details of the Firebase 404 error
            called_args = [call.args[0] for call in mock_print.call_args_list if call.args]
            printed_output = "\n".join(called_args)
            self.assertIn("Firebase distribution failed (HTTP 404)", printed_output)
            self.assertIn("invalid-tester@gmail.com", printed_output)
            self.assertIn("invalid-testers", printed_output)
            self.assertIn("firebase_testers", printed_output)


if __name__ == "__main__":
    unittest.main()
