from __future__ import annotations

import json
import os
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import importlib
import dev_console  # noqa: E402
importlib.reload(dev_console)
from model_registry import ModelCapability, ModelMetadata, ModelTier

class DevConsoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_root = dev_console.ROOT
        self.temp_root = Path("/tmp/orchestrator-test-root")
        self.temp_root.mkdir(parents=True, exist_ok=True)
        dev_console.ROOT = self.temp_root
        dev_console.CONFIG_DIR = self.temp_root / ".orchestrator" / "config"
        dev_console.CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        dev_console.ROOT = self.old_root
        import shutil
        if self.temp_root.exists():
            shutil.rmtree(self.temp_root)

    @patch("dev_console.read_json")
    @patch("dev_console.write_json")
    @patch("dev_console.get_key")
    @patch("dev_console.input")
    @patch("dev_console.prompt_input")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("pathlib.Path.exists")
    def test_handle_email_settings_adds_recipient(self, mock_exists, _mock_status, _mock_clear, mock_prompt_input, mock_input, mock_get_key, mock_write, mock_read):
        # Initial settings
        mock_exists.return_value = True
        mock_read.return_value = {
            "notification_emails": ["old@example.com"],
            "notification_provider": "gmail"
        }
        
        # Actions: 'a' (add), then 'new@example.com', then 'b' (back)
        mock_get_key.side_effect = ["a", "b"]
        mock_prompt_input.return_value = "new@example.com"
        mock_input.return_value = ""
        
        dev_console.handle_email_settings([], [])
        
        # Verify write_json was called with updated emails
        mock_write.assert_called()
        final_settings = mock_write.call_args[0][1]
        self.assertIn("new@example.com", final_settings["notification_emails"])
        self.assertIn("old@example.com", final_settings["notification_emails"])

    @patch("dev_console.load_machines")
    @patch("dev_console.check_machine_availability")
    @patch("dev_console.prompt_checkbox")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    def test_handle_fleet_management_syncs_and_updates_session(self, _mock_status, _mock_clear, mock_checkbox, mock_avail, mock_load):
        mock_load.return_value = [
            {"name": "mac1", "enabled": True},
            {"name": "mac2", "enabled": True}
        ]
        mock_avail.return_value = True
        mock_checkbox.return_value = ["mac2"] # User selects only mac2
        
        new_machines = dev_console.handle_fleet_management(["mac1"])
        
        self.assertEqual(new_machines, ["mac2"])
        mock_avail.assert_called() # Should have pinged the fleet

    @patch("dev_console.input")
    @patch("dev_console.prompt_input")
    @patch("dev_console.prompt_confirm")
    @patch("dev_console.write_json")
    @patch("dev_console.load_machines")
    @patch("discover_machines.discover_machine_candidates")
    def test_discover_and_add_machines_saves_suitable_candidate(
        self,
        mock_discover,
        mock_load,
        mock_write,
        mock_confirm,
        mock_prompt_input,
        _mock_input,
    ):
        mock_load.return_value = [{"name": "macair", "ssh_target": "macair.local"}]
        mock_discover.return_value = {
            "suitable": [
                {
                    "status": "suitable",
                    "host": "Leemos-MacBook-Pro.local",
                    "hostname": "Leemos-MacBook-Pro.local",
                    "python": "Python 3.9.6",
                    "xcode": "Xcode 26.6",
                }
            ],
            "needs_keys": [],
            "unsuitable": [],
        }
        mock_confirm.return_value = True
        mock_prompt_input.return_value = "/Users/leemo/Projects/App"

        machines, selected = dev_console.discover_and_add_machines(["macair"])

        self.assertIn("Leemos-MacBook-Pro", selected)
        self.assertEqual(machines[-1]["name"], "Leemos-MacBook-Pro")
        self.assertEqual(machines[-1]["ssh_target"], "Leemos-MacBook-Pro.local")
        self.assertEqual(machines[-1]["repo_path"], "/Users/leemo/Projects/App")
        saved_payload = mock_write.call_args.args[1]
        self.assertEqual(saved_payload["machines"][-1]["name"], "Leemos-MacBook-Pro")

    @patch("dev_console.input")
    @patch("dev_console.prompt_confirm")
    @patch("dev_console.write_json")
    @patch("dev_console.load_machines")
    def test_remove_machine_from_fleet_updates_config_and_session(
        self,
        mock_load,
        mock_write,
        mock_confirm,
        _mock_input,
    ):
        mock_load.return_value = [
            {"name": "macair", "ssh_target": "macair.local"},
            {"name": "macpro", "ssh_target": "macpro.local"},
        ]
        mock_confirm.return_value = True

        machines, selected, removed = dev_console.remove_machine_from_fleet("macpro", ["macair", "macpro"])

        self.assertTrue(removed)
        self.assertEqual([m["name"] for m in machines], ["macair"])
        self.assertEqual(selected, ["macair"])
        saved_payload = mock_write.call_args.args[1]
        self.assertEqual([m["name"] for m in saved_payload["machines"]], ["macair"])

    @patch("dev_console.input")
    @patch("dev_console.write_json")
    @patch("dev_console.load_machines")
    def test_remove_machine_from_fleet_keeps_last_machine(self, mock_load, mock_write, _mock_input):
        mock_load.return_value = [{"name": "macair", "ssh_target": "macair.local"}]

        machines, selected, removed = dev_console.remove_machine_from_fleet("macair", ["macair"])

        self.assertFalse(removed)
        self.assertEqual([m["name"] for m in machines], ["macair"])
        self.assertEqual(selected, ["macair"])
        mock_write.assert_not_called()

    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.PROJECT_CONFIG")
    def test_handle_instruction_files_creates_missing(self, mock_config, _mock_status, _mock_clear, mock_get_key):
        mock_config.runtime_dir = Path(".orchestrator")
        
        # Mock get_key: 'a' (create all), then 'b' (back)
        mock_get_key.side_effect = ["a", "b"]
        
        with patch("dev_console.input", return_value=""):
            # We want to verify that files are created in our temp_root
            dev_console.handle_instruction_files()
            
            # Check if one of the expected files was created
            gemini_md = self.temp_root / "GEMINI.md"
            self.assertTrue(gemini_md.exists())
            self.assertIn("Source of truth", gemini_md.read_text())

    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.handle_role_prompts")
    def test_handle_instruction_files_custom_prompt_action_uses_session_state(self, mock_role_prompts, _mock_status, _mock_clear, mock_get_key):
        mock_get_key.side_effect = ["p", "b"]

        dev_console.handle_instruction_files(["mac1"], ["codex"])

        mock_role_prompts.assert_called_once_with(["mac1"], ["codex"])

    @patch("dev_console.get_key")
    @patch("dev_console.input")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.PROJECT_CONFIG")
    @patch("dev_console.subprocess.run")
    @patch("dev_console.prompt_confirm")
    def test_handle_role_prompts_manages_templates(self, mock_confirm, mock_run, mock_config, _mock_status, _mock_clear, mock_input, mock_get_key):
        runtime_dir = self.temp_root / ".orchestrator"
        mock_config.runtime_dir = runtime_dir
        prompts_dir = runtime_dir / "prompts"
        
        # Scenario 1: No prompts, user chooses '1' to copy templates
        mock_get_key.side_effect = ["1", "b"]
        mock_input.return_value = ""
        dev_console.handle_role_prompts([], [])
        mock_run.assert_called() # Should run 'wizard --copy-prompt-overrides'
        
        # Scenario 2: Prompts exist, user chooses '2' to delete them
        prompts_dir.mkdir(parents=True, exist_ok=True)
        (prompts_dir / "builder_bug.md").write_text("test")
        mock_get_key.side_effect = ["2", "b"]
        mock_input.return_value = ""
        mock_confirm.return_value = True
        dev_console.handle_role_prompts([], [])
        self.assertFalse(prompts_dir.exists())

    @patch("dev_console.get_key")
    @patch("dev_console.input")
    @patch("dev_console.prompt_input")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.run_script")
    @patch("dev_console.save_job")
    @patch("dev_console.refresh_job")
    def test_handle_job_selection_answers_clarification(self, mock_refresh, mock_save, mock_run_script, _mock_status, _mock_clear, mock_prompt_input, mock_input, mock_get_key):
        job = {
            "job_id": "test-job",
            "status": "human-needed",
            "human_clarification_question": "What is the color?",
            "type": "feature-plan",
            "_path": "test.json"
        }
        mock_refresh.return_value = job
        
        # Actions: 'a' (answer), then 'Blue', then 'b' (back)
        mock_get_key.side_effect = ["a", "b"]
        mock_prompt_input.return_value = "Blue"
        mock_input.return_value = ""
        
        dev_console.handle_job_selection(job, [], [])
        
        # Verify old question was cleared and re-plan was triggered
        mock_save.assert_called()
        saved_job = mock_save.call_args[0][0]
        self.assertIsNone(saved_job["human_clarification_question"])
        
        mock_run_script.assert_called_with(
            "new_job.py",
            ["feature", "--no-dispatch", "--update", "test.json", "--feedback", "### USER CLARIFICATION ###\nBlue"],
            sub_menu=True
        )

    @patch("dev_console.view_job_brief_summary")
    @patch("dev_console.get_key")
    @patch("dev_console.input")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.refresh_job")
    def test_handle_job_selection_view_brief_summary_action(self, mock_refresh, mock_status, mock_clear, mock_input, mock_get_key, mock_view):
        job = {
            "job_id": "test-job",
            "status": "review-needed",
            "type": "feature-plan",
            "issue_number": 123,
            "_path": "test.json",
        }
        status_bar = MagicMock()
        mock_status.return_value.__enter__.return_value = status_bar
        mock_refresh.return_value = job
        mock_get_key.side_effect = ["v", "b"]
        mock_input.return_value = ""

        dev_console.handle_job_selection(job, [], [])

        mock_view.assert_called_once_with(job)
        status_bar.clear_footer.assert_called()
        status_bar.reset_scroll_region.assert_called_with(force=True)
        mock_clear.assert_called()

    @patch("dev_console.print_header")
    @patch("dev_console.print")
    def test_view_job_brief_summary_shows_fallback_when_files_missing(self, mock_print, mock_header):
        job = {
            "job_id": "missing-output-job",
            "plan": {
                "summary": "Ship the feature.",
                "tasks": [{"name": "Build UI", "description": "Create the main screen."}],
            },
        }

        dev_console.view_job_brief_summary(job)

        mock_header.assert_any_call("Brief / Summary")
        mock_header.assert_any_call("Plan Summary")
        printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertIn("No brief.md or builder_summary.md file has been generated", printed)
        self.assertIn("Ship the feature.", printed)
        self.assertIn("Build UI", printed)

    def test_format_risk_mitigation_pair(self):
        """Verify format_risk_mitigation_pair crafts appropriate strategies for various risk types."""
        r1, m1 = dev_console.format_risk_mitigation_pair(
            "A superficial UI binding fix could make the tab appear active while showing no risks.",
            "Trace full path from processing output to model to ViewModel."
        )
        self.assertEqual(r1, "A superficial UI binding fix could make the tab appear active while showing no risks.")
        self.assertEqual(m1, "Trace full path from processing output to model to ViewModel.")

        # Inferred strategy for async race conditions
        r2, m2 = dev_console.format_risk_mitigation_pair("Risk generation may be asynchronous, creating a race condition.")
        self.assertIn("async", m2.lower())

        # Inferred strategy for schema decoding differences
        r3, m3 = dev_console.format_risk_mitigation_pair("New documents may use a different schema or decoding structure.")
        self.assertIn("schema", m3.lower())

    @patch("dev_console.print_header")
    def test_view_job_brief_summary_renders_risks_with_mitigations(self, mock_header):
        job = {
            "job_id": "risk-mitigation-job",
            "verification": {
                "risks_identified": [
                    "A superficial UI binding fix could make the tab appear active while showing no risks.",
                    "Risk generation may be asynchronous, creating a race condition."
                ],
                "suggested_additions": [
                    "Trace the full data flow from processor to persisted model to ViewModel.",
                    "Check async report generation state transitions with loading/error states."
                ]
            }
        }
        with patch("builtins.print") as mock_print:
            dev_console.view_job_brief_summary(job)
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("Identified Risks & Mitigation Strategies", [call.args[0] for call in mock_header.call_args_list if call.args])
            self.assertIn("Risk #1:", printed)
            self.assertIn("Mitigation / Test Strategy:", printed)
            self.assertIn("persisted model to ViewModel", printed)

    @patch("dev_console.refresh_job")
    @patch("dev_console.run_script")
    @patch("dev_console.prompt_input")
    def test_handle_tweak_revise_builds_guided_feedback(self, mock_prompt_input, mock_run_script, mock_refresh):
        job = {
            "job_id": "test-job",
            "status": "review-needed",
            "type": "feature-plan",
            "_path": "test.json",
        }
        mock_prompt_input.side_effect = [
            "Make the retry state explain the failure.",
            "Checkout error panel",
            "A failed request shows the error reason and retry button.",
        ]
        mock_refresh.return_value = job

        result = dev_console.handle_tweak_revise(job)

        expected_feedback = (
            "Requested change: Make the retry state explain the failure.\n"
            "Affected area: Checkout error panel\n"
            "Done when: A failed request shows the error reason and retry button."
        )
        mock_run_script.assert_called_once_with(
            "new_job.py",
            ["feature", "--no-dispatch", "--update", "test.json", "--feedback", expected_feedback],
            sub_menu=True,
        )
        first_prompt = mock_prompt_input.call_args_list[0]
        self.assertEqual(first_prompt.args[0], "Briefly describe what should change")
        self.assertTrue(first_prompt.kwargs["field_below"])
        self.assertIs(result, job)

    @patch("dev_console.run_script")
    @patch("dev_console.prompt_input", return_value="")
    def test_handle_tweak_revise_cancels_when_change_is_blank(self, _mock_prompt_input, mock_run_script):
        job = {
            "job_id": "test-job",
            "status": "review-needed",
            "type": "feature-plan",
            "_path": "test.json",
        }

        result = dev_console.handle_tweak_revise(job)

        self.assertIsNone(result)
        mock_run_script.assert_not_called()

    def test_refresh_job_preserves_current_job_when_file_is_invalid(self):
        job_path = self.temp_root / "invalid-job.json"
        job_path.write_text("null\n", encoding="utf-8")
        job = {"job_id": "test-job", "status": "review-needed", "_path": job_path}

        refreshed = dev_console.refresh_job(job)

        self.assertIs(refreshed, job)

    @patch("dev_console.handle_tweak_revise", return_value=None)
    @patch("dev_console.get_key", side_effect=["t", "b"])
    @patch("dev_console.input", return_value="")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    def test_handle_job_selection_keeps_job_when_tweak_is_cancelled(self, mock_status, _mock_clear, _mock_input, _mock_get_key, mock_tweak):
        job = {
            "job_id": "test-job",
            "status": "review-needed",
            "type": "feature-plan",
            "issue_number": 123,
            "_path": self.temp_root / "test-job.json",
        }

        dev_console.handle_job_selection(job, ["local"], ["gemini"])

        status_instance = mock_status.return_value.__enter__.return_value
        status_instance.clear_footer.assert_called()
        status_instance.reset_scroll_region.assert_called_with(force=True)
        mock_tweak.assert_called_once_with(job)

    @patch("dev_console.get_key", side_effect=["d", "b"])
    @patch("dev_console.input", return_value="")
    @patch("dev_console.prompt_input", side_effect=["", "0"])
    @patch("dev_console.auto_link_latest_logs")
    @patch("dev_console.run_script")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    def test_handle_job_selection_autofix_survives_invalid_refresh(self, _mock_status, _mock_clear, mock_run_script, mock_auto_logs, _mock_prompt_input, _mock_input, _mock_get_key):
        job_path = self.temp_root / "autofix-job.json"
        job_path.write_text("null\n", encoding="utf-8")
        job = {
            "job_id": "autofix-job",
            "status": "review-needed",
            "type": "feature-plan",
            "issue_number": 123,
            "_path": job_path,
            "test_summary": {
                "status": "failing",
                "created_count": 0,
                "planned_count": 0,
                "failed_count": 1,
                "passed_count": 0,
                "total_run": 1,
                "failing_tests": ["TestA"],
                "tests_ok": False,
            },
        }

        dev_console.handle_job_selection(job, ["local"], ["gemini"])

        mock_auto_logs.assert_called_once_with(job)
        mock_run_script.assert_called_once()
        self.assertEqual(mock_run_script.call_args.args[0], "debug_job.py")

    @patch("dev_console.prompt_input", return_value="Focus on the latest build failure.")
    @patch("dev_console.print_header")
    @patch("dev_console.print")
    def test_prompt_autofix_iteration_settings_collects_guidance(self, mock_print, mock_header, _mock_prompt_input):
        job = {"max_iterations": 8}

        feedback = dev_console.prompt_autofix_iteration_settings(job)

        self.assertEqual(feedback, "Focus on the latest build failure.")
        mock_header.assert_called_once_with("Auto-Fix / Iterate")
        printed = "\n".join(str(call.args[0]) for call in mock_print.call_args_list if call.args)
        self.assertIn("The AI will inspect the current job", printed)
        self.assertIn("Optional guidance examples", printed)
        self.assertIn("When to use", printed)
        first_prompt = _mock_prompt_input.call_args_list[0]
        self.assertEqual(first_prompt.args[0], "Guidance (Optional)")
        self.assertTrue(first_prompt.kwargs["field_below"])

    @patch("dev_console.get_key", side_effect=["d", "b"])
    @patch("dev_console.input", return_value="")
    @patch("dev_console.prompt_autofix_iteration_settings", return_value="Focus on retries.")
    @patch("dev_console.auto_link_latest_logs")
    @patch("dev_console.run_script")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    def test_handle_job_selection_autofix_passes_feedback(self, mock_status, mock_clear, mock_run_script, _mock_auto_logs, mock_settings, _mock_input, _mock_get_key):
        status_bar = MagicMock()
        mock_status.return_value.__enter__.return_value = status_bar
        job = {
            "job_id": "autofix-job",
            "status": "review-needed",
            "type": "feature-plan",
            "issue_number": 123,
            "_path": self.temp_root / "autofix-job.json",
            "test_summary": {
                "status": "failing",
                "created_count": 0,
                "planned_count": 0,
                "failed_count": 1,
                "passed_count": 0,
                "total_run": 1,
                "failing_tests": ["TestA"],
                "tests_ok": False,
            },
        }

        dev_console.handle_job_selection(job, ["local"], ["gemini"])

        mock_settings.assert_called_once_with(job)
        status_bar.clear_footer.assert_called()
        status_bar.reset_scroll_region.assert_called_with(force=True)
        mock_clear.assert_called()
        args = mock_run_script.call_args.args[1]
        self.assertIn("--feedback", args)
        self.assertIn("Focus on retries.", args)

    def test_review_needed_job_menu_actions_are_reachable(self):
        action_keys = ["m", "f", "t", "r", "l", "k", "q", "o", "y", "v", "g", "c", "x", "b"]

        for key in action_keys:
            with self.subTest(key=key):
                job = {
                    "job_id": "review-job",
                    "status": "review-needed",
                    "type": "feature-plan",
                    "issue_number": 123,
                    "pr_number": 456,
                    "branch": "feature/review-job",
                    "base_branch": "main",
                    "_path": self.temp_root / "review-job.json",
                }

                with ExitStack() as stack:
                    stack.enter_context(patch("dev_console.get_key", side_effect=[key, "b"]))
                    stack.enter_context(patch("dev_console.input", return_value=""))
                    stack.enter_context(patch("dev_console.clear_screen"))
                    stack.enter_context(patch("dev_console.StatusBar"))
                    stack.enter_context(patch("dev_console.refresh_job", return_value=job))
                    mock_save = stack.enter_context(patch("dev_console.save_job"))
                    mock_run_script = stack.enter_context(patch("dev_console.run_script"))
                    mock_merge = stack.enter_context(patch("dev_console.handle_merge_cleanup"))
                    mock_tweak = stack.enter_context(patch("dev_console.handle_tweak_revise", return_value=job))
                    mock_discard = stack.enter_context(patch("dev_console.handle_discard_job"))
                    mock_logs = stack.enter_context(patch("dev_console.prompt_for_logs", return_value=["build.log"]))
                    mock_reference = stack.enter_context(patch("dev_console.prompt_for_reference_artifact"))
                    mock_ask = stack.enter_context(patch("dev_console.handle_ask_ai"))
                    mock_view = stack.enter_context(patch("dev_console.view_job_brief_summary"))
                    mock_confirm = stack.enter_context(patch("dev_console.prompt_confirm", return_value=False))
                    stack.enter_context(patch("dev_console.prompt_input", side_effect=["", "0"]))
                    mock_auto_logs = stack.enter_context(patch("dev_console.auto_link_latest_logs"))
                    stack.enter_context(patch("dev_console.prompt_autofix_iteration_settings", return_value=None))
                    mock_loading = stack.enter_context(patch("dev_console.run_with_loading_screen", return_value=(["Gemini"], {"Gemini": "gemini"}, {}, [])))
                    mock_checkbox = stack.enter_context(patch("dev_console.prompt_checkbox", return_value=[]))
                    mock_radio = stack.enter_context(patch("dev_console.prompt_radio", return_value="Open Pull Request #456 in browser"))
                    mock_subprocess_run = stack.enter_context(patch("dev_console.subprocess.run"))
                    mock_project_config = stack.enter_context(patch("dev_console.PROJECT_CONFIG"))
                    mock_project_config.base_branch = "main"
                    mock_project_config.validate_distribution_config.return_value = []

                    dev_console.handle_job_selection(job, ["local"], ["gemini"])

                if key == "m":
                    mock_merge.assert_called_once_with(job)
                elif key == "f":
                    mock_run_script.assert_any_call(
                        "deliver_build.py",
                        [str(job["_path"])],
                        sub_menu=True,
                        session_machines=["local"],
                        session_models=["gemini"],
                    )
                elif key == "t":
                    mock_tweak.assert_called_once_with(job)
                elif key == "r":
                    mock_confirm.assert_called()
                elif key == "l":
                    self.assertEqual(mock_logs.call_args[0][0], job)
                    self.assertIsNotNone(mock_logs.call_args.kwargs.get("status_bar"))
                    mock_save.assert_called()
                    self.assertEqual(job["last_manual_log_paths"], ["build.log"])
                elif key == "k":
                    mock_reference.assert_called_once_with(job)
                elif key == "q":
                    mock_ask.assert_called_once_with(job, ["gemini"])
                elif key == "o":
                    mock_loading.assert_called()
                    mock_checkbox.assert_called()
                elif key == "y":
                    mock_run_script.assert_any_call("export_job.py", [str(job["_path"])], sub_menu=True)
                elif key == "v":
                    mock_view.assert_called_once_with(job)
                elif key == "g":
                    mock_subprocess_run.assert_any_call(["gh", "pr", "view", "456", "--web"], cwd=str(dev_console.ROOT), check=False)
                elif key == "c":
                    mock_confirm.assert_called()
                elif key == "x":
                    mock_discard.assert_called_once_with(job)

    @patch("dev_console.run_streaming_process")
    @patch("dev_console.print_divider")
    @patch("dev_console.input")
    @patch("dev_console.print")
    def test_run_script_extracts_failure_summary_from_logs(self, mock_print, mock_input, mock_print_divider, mock_run_streaming):
        # Setup run_streaming_process to fail with a non-zero exit code and output_log containing an error
        mock_run_streaming.return_value = (1, "Some generic logs\n❌ Bundle id is required. Set app_bundle_id in project.json or pass --bundle-id.\nMore logs")
        mock_input.return_value = ""

        # Run script
        dev_console.run_script("some_script.py", [])

        # Verify summary output was printed containing the bundle ID error
        printed_args = [call[0][0] for call in mock_print.call_args_list if call[0]]
        full_printed = "".join(printed_args)
        self.assertIn("Bundle id is required", full_printed)

    @patch("dev_console.run_streaming_process")
    @patch("dev_console.subprocess.run")
    def test_run_script_runs_smoke_delivery_without_streaming_footer(self, mock_subprocess_run, mock_run_streaming):
        mock_subprocess_run.return_value = MagicMock(returncode=0)

        rc = dev_console.run_script("smoke_test_delivery.py", [], sub_menu=True, prompt="")

        self.assertEqual(rc, 0)
        mock_subprocess_run.assert_called_once()
        mock_run_streaming.assert_not_called()

    @patch("dev_console.subprocess.check_output")
    @patch("dev_console.subprocess.run")
    def test_check_origin_update_status_detects_remote_updates(self, mock_run, mock_check_output):
        mock_check_output.side_effect = [
            "main\n",
            "0\t3\n",
        ]

        status = dev_console.check_origin_update_status(Path("/tmp/pkg"))

        self.assertEqual(status["state"], "behind")
        self.assertEqual(status["behind"], 3)
        self.assertEqual(status["remote_ref"], "origin/main")
        mock_run.assert_called_once()

    @patch("dev_console.subprocess.check_output")
    @patch("dev_console.subprocess.run")
    def test_check_origin_update_status_detects_current_branch(self, _mock_run, mock_check_output):
        mock_check_output.side_effect = [
            "main\n",
            "0\t0\n",
        ]

        status = dev_console.check_origin_update_status(Path("/tmp/pkg"))

        self.assertEqual(status["state"], "current")
        self.assertEqual(status["behind"], 0)

    def test_script_failure_summary_prefers_signing_diagnostic(self):
        output = """
❌ Distribution failed (exit 65).

\033[1;91mSigning setup needs attention\033[0m
Xcode could not find a certificate or provisioning profile for this app.

Detailed Manual Steps / Options to Fix This:
Option A: Headless Auto-Signing (Recommended)
...

❌ Smoke delivery failed during build/distribution.
"""

        summary = dev_console.script_failure_summary(output)

        self.assertIn("Signing setup needs attention", summary)
        self.assertIn("Option A: Headless Auto-Signing", summary)
        self.assertNotIn("Smoke delivery failed", summary)

    def test_self_test_static_commands_reference_existing_targets(self):
        for choice, (_header, script_name, args) in dev_console.SELF_TEST_STATIC_COMMANDS.items():
            with self.subTest(choice=choice):
                if script_name.startswith("-m unittest"):
                    for arg in args:
                        if arg.endswith(".py"):
                            self.assertTrue((PACKAGE_ROOT / arg).exists(), f"{choice} references missing test file: {arg}")
                    continue

                self.assertTrue(
                    (SCRIPTS_DIR / script_name).exists(),
                    f"{choice} references missing script: {script_name}",
                )

    @patch("dev_console.subprocess.run")
    @patch("dev_console.shutil.which")
    @patch("dev_console.get_all_models")
    def test_model_selection_marks_missing_ollama_model_not_enabled(self, mock_get_models, mock_which, mock_run):
        mock_get_models.return_value = [
            ModelMetadata(
                id="qwen-3.7-max",
                family="qwen",
                tier=ModelTier.HIGH,
                capabilities=[ModelCapability.CODING],
                required_clis=["ollama"],
            )
        ]
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name == "ollama" else None
        mock_run.return_value = MagicMock(returncode=0, stdout="NAME ID SIZE MODIFIED\n", stderr="")

        options, _value_map, details_map, _summary = dev_console.get_model_selection_data()

        qwen_label = next(option for option in options if option.startswith("qwen-3.7-max"))
        self.assertIn("[Not Downloaded]", qwen_label)
        details = "\n".join(details_map[qwen_label])
        self.assertIn("Access:", details)
        self.assertIn("Not downloaded", details)
        self.assertIn("Backend:", details)
        self.assertIn("ollama", details)
        self.assertIn("Ollama Model:", details)
        self.assertIn("missing", details)
        self.assertIn("ollama pull qwen-3.7-max", details)

    @patch("dev_console.subprocess.run")
    @patch("dev_console.shutil.which")
    @patch("dev_console.get_all_models")
    def test_model_selection_enables_installed_ollama_model(self, mock_get_models, mock_which, mock_run):
        mock_get_models.return_value = [
            ModelMetadata(
                id="qwen-3.7-max",
                family="qwen",
                tier=ModelTier.HIGH,
                capabilities=[ModelCapability.CODING],
                required_clis=["ollama"],
            )
        ]
        mock_which.side_effect = lambda name: f"/usr/bin/{name}" if name == "ollama" else None
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="NAME ID SIZE MODIFIED\nqwen-3.7-max:latest abc 7 GB now\n",
            stderr="",
        )

        options, _value_map, details_map, _summary = dev_console.get_model_selection_data()

        qwen_label = next(option for option in options if option.startswith("qwen-3.7-max"))
        self.assertNotIn("[Not Enabled]", qwen_label)
        details = "\n".join(details_map[qwen_label])
        self.assertIn("Access:", details)
        self.assertIn("Ready", details)
        self.assertIn("Ollama Model:", details)
        self.assertIn("installed", details)

    def test_model_selection_defaults_skip_unavailable_labels(self):
        value_map = {
            "gpt-5.3-codex": "gpt-5.3-codex",
            "qwen-3.7-max \033[1;91m[Not Enabled]\033[0m": "qwen-3.7-max",
        }

        defaults = dev_console.resolve_model_selection_defaults(["gpt-5.3-codex", "qwen-3.7-max"], value_map)

        self.assertEqual(defaults, ["gpt-5.3-codex"])

    def test_unique_doc_paths_dedupes_resolved_paths(self):
        first = self.temp_root / "docs" / "getting-started.md"
        duplicate = self.temp_root / "docs" / ".." / "docs" / "getting-started.md"
        second = self.temp_root / "docs" / "user-guide.md"
        first.parent.mkdir(parents=True, exist_ok=True)
        first.touch()
        second.touch()

        docs = dev_console.unique_doc_paths([first, duplicate, second])

        self.assertEqual(docs, [first, second])

    def test_discover_test_suites_large_modular_project_1000_tests(self):
        """Simulate discovering 1000+ tests across modular packages, Swift Testing, XCTest, and Quick/Nimble."""
        # Module 1: Packages/Core/Tests/CoreTests - 300 tests (Swift Testing with tags/traits)
        core_dir = self.temp_root / "Packages" / "Core" / "Tests" / "CoreTests"
        core_dir.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            test_methods = "\n".join([
                f"""    @Test("Core test case {j}", .tags(.critical))
    @MainActor
    func coreTest_{j}() async throws {{}}"""
                for j in range(30)
            ])
            (core_dir / f"CoreTests_{i}.swift").write_text(f"""import Testing
@Suite struct CoreTests_{i} {{
{test_methods}
}}
""")

        # Module 2: Modules/Auth/Tests - 300 tests (XCTest with custom base class)
        auth_dir = self.temp_root / "Modules" / "Auth" / "Tests"
        auth_dir.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            test_methods = "\n".join([
                f"""    func testAuthScenario_{j}() {{}}"""
                for j in range(30)
            ])
            (auth_dir / f"AuthTests_{i}.swift").write_text(f"""import XCTest
class AuthTests_{i}: BaseAuthTestCase {{
{test_methods}
}}
""")

        # Module 3: Features/Checkout/Tests - 200 tests (Quick/Nimble BDD)
        checkout_dir = self.temp_root / "Features" / "Checkout" / "Tests"
        checkout_dir.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            spec_cases = "\n".join([
                f"""            it("verifies checkout step {j}") {{}}"""
                for j in range(20)
            ])
            (checkout_dir / f"CheckoutSpec_{i}.swift").write_text(f"""import Quick
class CheckoutSpec_{i}: QuickSpec {{
    override class func spec() {{
        describe("Checkout") {{
{spec_cases}
        }}
    }}
}}
""")

        # Module 4: AppTests at root - 250 tests
        app_dir = self.temp_root / "AppTests"
        app_dir.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            test_methods = "\n".join([
                f"""    func testAppFeature_{j}() {{}}"""
                for j in range(25)
            ])
            (app_dir / f"AppFeatureTests_{i}.swift").write_text(f"""import XCTest
class AppFeatureTests_{i}: XCTestCase {{
{test_methods}
}}
""")

        # Total created = 300 + 300 + 200 + 250 = 1050 tests across 40 test files
        suites = dev_console.discover_test_suites(self.temp_root, "AppTests")
        total_tests = sum(s["test_count"] for s in suites)
        self.assertEqual(len(suites), 40)
        self.assertEqual(total_tests, 1050)

    def test_discover_app_source_files_identifies_untested_modules(self):
        """Verify categorization of Swift source files and identification of untested ViewModels and Services."""
        src_dir = self.temp_root / "Sources" / "App"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "AuthViewModel.swift").write_text("// Auth view model\nclass AuthViewModel: ObservableObject {}")
        (src_dir / "SyncManager.swift").write_text("// Sync manager\nclass SyncManager {}")
        (src_dir / "DocumentManager.swift").write_text("// Document manager\nclass DocumentManager {}")
        (src_dir / "DateHelper.swift").write_text("// Date helper\nstruct DateHelper {}")

        test_dir = self.temp_root / "Tests"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "DocumentManagerTests.swift").write_text("import XCTest\nclass DocumentManagerTests: XCTestCase { func testDoc() {} }")

        suites = dev_console.discover_test_suites(self.temp_root)
        app_files = dev_console.discover_app_source_files(self.temp_root, suites)

        self.assertEqual(app_files["total_source_files"], 4)
        self.assertEqual(len(app_files["view_models"]), 1)
        self.assertEqual(len(app_files["untested_view_models"]), 1)
        self.assertEqual(app_files["untested_view_models"][0]["stem"], "AuthViewModel")

        self.assertEqual(len(app_files["services"]), 2)
        # DocumentManager is tested by DocumentManagerTests, so untested_services should only contain SyncManager
        self.assertEqual(len(app_files["untested_services"]), 1)
        self.assertEqual(app_files["untested_services"][0]["stem"], "SyncManager")

    @patch("dev_console.run_llm")
    def test_analyze_coverage_gaps_with_llm(self, mock_run_llm):
        """Verify that analyze_coverage_gaps parses structured AI recommendations when LLM is available."""
        src_dir = self.temp_root / "App"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "AuthViewModel.swift").write_text("class AuthViewModel {}")

        mock_llm_json = """[
            {
                "subsystem": "AuthViewModel / Session Management",
                "priority": "HIGH",
                "rationale": "Core auth state machine and token refresh have 0 unit tests.",
                "target_files": ["AuthViewModel.swift"],
                "suggested_focus": "Login state transitions and token expiration"
            }
        ]"""
        mock_run_llm.return_value = (mock_llm_json, "gemini-3.1-pro-preview", "session-123")

        suites = []
        gaps = dev_console.analyze_coverage_gaps(self.temp_root, suites, ["gemini-3.1-pro-preview"])

        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["subsystem"], "AuthViewModel / Session Management")
        self.assertEqual(gaps[0]["priority"], "HIGH")
        self.assertEqual(gaps[0]["source"], "ai")

    @patch("dev_console.run_llm")
    def test_analyze_coverage_gaps_fallback_to_heuristic(self, mock_run_llm):
        """Verify that analyze_coverage_gaps falls back to heuristic gap analysis when LLM fails."""
        src_dir = self.temp_root / "App"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "PaymentViewModel.swift").write_text("class PaymentViewModel {}")
        (src_dir / "NetworkClient.swift").write_text("class NetworkClient {}")

        mock_run_llm.side_effect = RuntimeError("API key quota exceeded")

        suites = []
        gaps = dev_console.analyze_coverage_gaps(self.temp_root, suites, ["gemini-3.1-pro-preview"])

        self.assertTrue(len(gaps) >= 2)
        subsystems = [g["subsystem"] for g in gaps]
        self.assertIn("PaymentViewModel", subsystems)
        self.assertIn("NetworkClient", subsystems)
        self.assertEqual(gaps[0]["source"], "heuristic")

    @patch("dev_console.run_script")
    @patch("dev_console.prompt_radio")
    @patch("dev_console.analyze_coverage_gaps")
    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    def test_handle_manage_tests_expand_coverage_ai_selection(
        self, _mock_status, _mock_clear, mock_get_key, mock_analyze_gaps, mock_prompt_radio, mock_run_script
    ):
        """Verify selecting an AI-identified gap in manage tests launches new_job.py coverage with the summary."""
        mock_get_key.side_effect = ["e", "b"]
        mock_analyze_gaps.return_value = [
            {
                "subsystem": "AuthViewModel / Session Management",
                "priority": "HIGH",
                "rationale": "Core auth state machine has 0 tests.",
                "target_files": ["AuthViewModel.swift"],
                "suggested_focus": "Login transitions",
                "source": "ai"
            }
        ]
        # User selects the first option (the AI gap)
        mock_prompt_radio.return_value = "🎯 AuthViewModel / Session Management \033[1;91m[HIGH]\033[0m (Core auth state machine has 0 tests. - Targets: AuthViewModel.swift)"

        dev_console.handle_manage_tests(["local"], ["gemini"])

        mock_run_script.assert_called()
        call_args = mock_run_script.call_args[0]
        script_name = call_args[0]
        script_cmd_args = call_args[1]

        self.assertEqual(script_name, "new_job.py")
        self.assertEqual(script_cmd_args[0], "coverage")
        self.assertIn("--branch-mode", script_cmd_args)
        self.assertEqual(script_cmd_args[script_cmd_args.index("--branch-mode") + 1], "current")
        self.assertIn("--summary", script_cmd_args)
        summary_val = script_cmd_args[script_cmd_args.index("--summary") + 1]
        self.assertIn("AuthViewModel / Session Management", summary_val)

    @patch("dev_console.run_script")
    @patch("dev_console.prompt_confirm")
    @patch("dev_console.prompt_radio")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    def test_handle_new_job_branch_selection(
        self, _mock_status, _mock_clear, mock_prompt_radio, mock_prompt_confirm, mock_run_script
    ):
        """Verify that creating a job from the new job menu prompts for branch selection."""
        # 1. Job type prompt returns "Bug Fix", then branch selection returns "new"
        mock_prompt_radio.side_effect = ["🐞 Bug Fix (Identify + Fix)", "new (creates a new branch to work in)"]
        # 2. Confirm prompts for spec file, yolo, advanced options
        mock_prompt_confirm.side_effect = [False, False, False]

        dev_console.handle_new_job(["local"], ["gemini"])

        self.assertEqual(mock_prompt_radio.call_count, 2)
        self.assertEqual(mock_prompt_radio.call_args_list[0][0][0], "Select Job Type")
        job_options = mock_prompt_radio.call_args_list[0][0][1]
        self.assertNotIn("🧪 Test Coverage Audit (Maintenance)", job_options)

        mock_run_script.assert_called()
        call_args = mock_run_script.call_args[0]
        script_args = call_args[1]
        self.assertEqual(script_args[0], "bug")
        self.assertIn("--branch-mode", script_args)
        self.assertEqual(script_args[script_args.index("--branch-mode") + 1], "new")


    @patch("shutil.which")
    @patch("subprocess.check_output")
    def test_get_github_auth_info_single_account(self, mock_check_output, mock_which):
        mock_which.return_value = "/usr/local/bin/gh"
        mock_check_output.return_value = (
            b"github.com\n"
            b"  \xe2\x9c\x93 Logged in to github.com account devuser (/path/hosts.yml)\n"
            b"  - Active account: true\n"
            b"  - Git operations protocol: ssh\n"
            b"  - Token scopes: 'repo', 'read:org'\n"
        )
        has_gh, accounts, active_user = dev_console.get_github_auth_info()
        self.assertTrue(has_gh)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["user"], "devuser")
        self.assertEqual(accounts[0]["host"], "github.com")
        self.assertTrue(accounts[0]["active"])
        self.assertEqual(active_user, "devuser")

    @patch("shutil.which")
    @patch("subprocess.check_output")
    def test_get_github_auth_info_multiple_accounts(self, mock_check_output, mock_which):
        mock_which.return_value = "/usr/local/bin/gh"
        mock_check_output.return_value = (
            b"github.com\n"
            b"  \xe2\x9c\x93 Logged in to github.com account primary_user (/path/hosts.yml)\n"
            b"  - Active account: true\n"
            b"  \xe2\x9c\x93 Logged in to github.com account secondary_user (/path/hosts.yml)\n"
            b"  - Active account: false\n"
        )
        has_gh, accounts, active_user = dev_console.get_github_auth_info()
        self.assertTrue(has_gh)
        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0]["user"], "primary_user")
        self.assertTrue(accounts[0]["active"])
        self.assertEqual(accounts[1]["user"], "secondary_user")
        self.assertFalse(accounts[1]["active"])
        self.assertEqual(active_user, "primary_user")

    @patch("shutil.which")
    def test_get_github_auth_info_not_installed(self, mock_which):
        mock_which.return_value = None
        has_gh, accounts, active_user = dev_console.get_github_auth_info()
        self.assertFalse(has_gh)
        self.assertEqual(accounts, [])
        self.assertIsNone(active_user)

    @patch("subprocess.run")
    @patch("dev_console.input")
    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.get_github_auth_info")
    def test_handle_github_menu_view_status(self, mock_auth_info, _mock_status, _mock_clear, mock_get_key, _mock_input, mock_run):
        mock_auth_info.return_value = (True, [{"host": "github.com", "user": "devuser", "active": True}], "devuser")
        mock_get_key.side_effect = ["v", "b"]
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "github.com logged in"
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        dev_console.handle_github_menu(["local"], ["gemini"])

        called_cmds = [c[0][0] for c in mock_run.call_args_list]
        self.assertIn(["gh", "auth", "status"], called_cmds)

    @patch("subprocess.run")
    @patch("dev_console.input")
    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.get_github_auth_info")
    def test_handle_github_menu_add_account(self, mock_auth_info, _mock_status, _mock_clear, mock_get_key, _mock_input, mock_run):
        mock_auth_info.return_value = (True, [{"host": "github.com", "user": "devuser", "active": True}], "devuser")
        mock_get_key.side_effect = ["a", "1", "b"]
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        dev_console.handle_github_menu(["local"], ["gemini"])

        called_cmds = [c[0][0] for c in mock_run.call_args_list]
        self.assertTrue(any(cmd[:3] == ["gh", "auth", "login"] and "--web" in cmd for cmd in called_cmds))

    @patch("subprocess.run")
    @patch("dev_console.prompt_radio")
    @patch("dev_console.input")
    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.get_github_auth_info")
    def test_handle_github_menu_switch_account_multiple(self, mock_auth_info, _mock_status, _mock_clear, mock_get_key, _mock_input, mock_prompt_radio, mock_run):
        mock_auth_info.return_value = (
            True,
            [
                {"host": "github.com", "user": "user1", "active": True},
                {"host": "github.com", "user": "user2", "active": False}
            ],
            "user1"
        )
        mock_get_key.side_effect = ["w", "b"]
        mock_prompt_radio.return_value = "user2 (github.com)"
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_run.return_value = mock_proc

        dev_console.handle_github_menu(["local"], ["gemini"])

        called_cmds = [c[0][0] for c in mock_run.call_args_list]
        self.assertIn(["gh", "auth", "switch", "--hostname", "github.com", "--user", "user2"], called_cmds)

    @patch("subprocess.run")
    @patch("dev_console.prompt_confirm")
    @patch("dev_console.input")
    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.get_github_auth_info")
    def test_handle_github_menu_switch_account_single_prompts_add(self, mock_auth_info, _mock_status, _mock_clear, mock_get_key, _mock_input, mock_confirm, mock_run):
        mock_auth_info.return_value = (
            True,
            [{"host": "github.com", "user": "user1", "active": True}],
            "user1"
        )
        mock_get_key.side_effect = ["w", "b", "b"]
        mock_confirm.return_value = True

        dev_console.handle_github_menu(["local"], ["gemini"])

    @patch("dev_console.input", return_value="")
    @patch("dev_console.subprocess.Popen")
    @patch("dev_console.clear_screen")
    @patch("dev_console.ProgressIndicator")
    @patch("dev_console.StatusBar")
    @patch("dev_console.sys.stdout.isatty", return_value=True)
    def test_run_calculate_coverage_uses_thinking_loader(self, _mock_isatty, mock_status_bar, mock_progress_indicator, _mock_clear, mock_popen, _mock_input):
        mock_proc = MagicMock()
        mock_proc.stdout = ["Test Suite 'AuthTests' started\n", "Test Case '-[AuthTests testLogin]' started\n", "Passed\n"]
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        indicator_instance = MagicMock()
        mock_progress_indicator.return_value = indicator_instance

        status_bar_instance = MagicMock()
        status_bar_context = MagicMock()
        status_bar_context.__enter__.return_value = status_bar_instance
        mock_status_bar.return_value = status_bar_context

        pct = dev_console.run_calculate_coverage(["local"], ["gemini"])

        mock_progress_indicator.assert_called_once()
        self.assertIn("Thinking", mock_progress_indicator.call_args[1].get("label", ""))
        self.assertTrue(status_bar_instance.set_scroll_region.called)
        self.assertTrue(status_bar_instance.render.called)
        self.assertTrue(indicator_instance.clear.called)

    @patch("dev_console.run_script")
    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.PROJECT_CONFIG")
    def test_handle_quick_distribute_flow(self, mock_project_config, mock_status_bar, _mock_clear, mock_get_key, mock_run_script):
        mock_project_config.validate_distribution_config.return_value = []
        mock_project_config.scheme = "MyApp"
        mock_project_config.project_name = "MyApp"
        mock_project_config.delivery_method = "ad-hoc"
        mock_project_config.firebase_groups = "internal-testers"
        
        status_bar_instance = MagicMock()
        status_bar_context = MagicMock()
        status_bar_context.__enter__.return_value = status_bar_instance
        mock_status_bar.return_value = status_bar_context

        # Choose 'd' to distribute, then 'b' to back
        mock_get_key.side_effect = ["d", "b"]

        dev_console.handle_quick_distribute(["local"], ["gemini"])

        mock_run_script.assert_called_once_with(
            "smoke_test_delivery.py",
            [],
            sub_menu=True,
            session_machines=["local"],
            session_models=["gemini"]
        )

    @patch("dev_console.get_key", return_value="b")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.get_coverage_data")
    @patch("dev_console.discover_test_suites")
    @patch("builtins.print")
    def test_handle_manage_tests_menu_coverage_box_date_on_newline(self, mock_print, mock_suites, mock_cov, mock_status_bar, _mock_clear, _mock_get_key):
        mock_cov.return_value = {
            "overall_coverage_pct": 75.5,
            "timestamp": "2026-09-04T20:30:49.123456"
        }
        mock_suites.return_value = [
            {"name": "AuthTests", "test_count": 5, "rel_path": "AppTests/AuthTests.swift"}
        ]
        status_bar_instance = MagicMock()
        status_bar_context = MagicMock()
        status_bar_context.__enter__.return_value = status_bar_instance
        mock_status_bar.return_value = status_bar_context

        dev_console.handle_manage_tests(["local"], ["gemini"])

        printed_lines = [call[0][0] for call in mock_print.call_args_list if call[0]]
        # Verify coverage line has percentage but NOT the timestamp attached
        cov_lines = [l for l in printed_lines if "CODE COVERAGE:" in str(l)]
        self.assertTrue(len(cov_lines) > 0)
        self.assertIn("75.5%", cov_lines[0])
        self.assertNotIn("2026-09-04", cov_lines[0])

        # Verify timestamp is printed on its own Last Audited line
        audited_lines = [l for l in printed_lines if "Last Audited:" in str(l)]
        self.assertTrue(len(audited_lines) > 0)
        self.assertIn("2026-09-04 20:30:49", audited_lines[0])

        # Verify test suites printed with clean bullet format
        suite_lines = [l for l in printed_lines if "AuthTests" in str(l)]
        self.assertTrue(len(suite_lines) > 0)
        self.assertIn("• AuthTests", suite_lines[0])
        self.assertIn("5 test(s)", suite_lines[0])

    @patch("dev_console.get_key", return_value="b")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.discover_test_suites", return_value=[])
    @patch("builtins.print")
    def test_handle_test_frameworks_menu_table_layout(self, mock_print, _mock_suites, mock_status_bar, _mock_clear, _mock_get_key):
        status_bar_instance = MagicMock()
        status_bar_context = MagicMock()
        status_bar_context.__enter__.return_value = status_bar_instance
        mock_status_bar.return_value = status_bar_context

        dev_console.handle_test_frameworks_menu(["local"], ["gemini"])

        printed_lines = [call[0][0] for call in mock_print.call_args_list if call[0]]
        
        # Verify framework items are rendered in a proper box table
        table_header_lines = [l for l in printed_lines if "Test Framework / Plugin" in str(l)]
        self.assertTrue(len(table_header_lines) > 0)
        self.assertIn("Opt", table_header_lines[0])
        self.assertIn("Status", table_header_lines[0])

        swift_testing_lines = [l for l in printed_lines if "Swift Testing" in str(l)]
        self.assertTrue(len(swift_testing_lines) > 0)
        self.assertIn("Swift Testing (Native)", swift_testing_lines[0])
        self.assertIn("Available", swift_testing_lines[0])

        # Verify box borders are present
        box_borders = [l for l in printed_lines if "┌" in str(l) or "└" in str(l)]
        self.assertTrue(len(box_borders) >= 2)

    @patch("dev_console.prompt_checkbox")
    def test_prompt_for_logs_formatting_and_defaults(self, mock_prompt_checkbox):
        job_id = "20260905-130559-bug-79"
        job_dir = self.temp_root / ".orchestrator" / "output" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        log_file = job_dir / "test_2026-09-05_13-14-07.log"
        log_file.write_text("dummy logs")

        manual_dir = self.temp_root / ".orchestrator" / "output" / "manual" / "2026-09-04 20:29:33-visual-check"
        manual_dir.mkdir(parents=True, exist_ok=True)

        old_out = dev_console.OUTPUT_DIR
        dev_console.OUTPUT_DIR = self.temp_root / ".orchestrator" / "output"
        try:
            job = {
                "job_id": job_id,
                "last_manual_log_paths": [str(manual_dir.relative_to(self.temp_root))]
            }

            def fake_prompt_checkbox(label, options, defaults, **kwargs):
                self.assertEqual(options[0], "\033[1;96mPaste New Logs...\033[0m")
                self.assertEqual(options[1], "\033[1;96mCustom Path...\033[0m")
                self.assertEqual(options[2], "--- Recent Logs ---")
                self.assertTrue(len(defaults) > 0)
                self.assertTrue(any("2026-09-04" in d for d in defaults))
                self.assertIsNotNone(kwargs.get("status_bar"))
                self.assertTrue(kwargs["status_bar"].sub_menu)
                return defaults

            mock_prompt_checkbox.side_effect = fake_prompt_checkbox

            result = dev_console.prompt_for_logs(job)
            self.assertEqual(result, [str(manual_dir.relative_to(self.temp_root))])
        finally:
            dev_console.OUTPUT_DIR = old_out

    def test_format_job_row_removes_numbers_from_title_and_shows_modified_date(self):
        job = {
            "job_id": "20260905-130559-bug-79",
            "type": "bug",
            "status": "review-needed",
            "issue_number": 79,
            "title": "#79 Fix risk tab not populating",
            "updated_at": "2026-09-05T13:05:59",
        }
        row = dev_console.format_job_row(0, job, title_width=35)
        
        self.assertIn("Fix risk tab not populating", row)
        self.assertNotIn("#79", row)
        self.assertIn("09/05", row)

    def test_format_job_header_prominent_banner(self):
        job1 = {
            "job_id": "20260905-130559-bug-79",
            "type": "bug",
            "title": "Fix risk tab selection",
        }
        banner1 = dev_console.format_job_header(job1)
        self.assertIn("===== [BUG] FIX RISK TAB SELECTION =====", banner1)
        self.assertIn("====", banner1)

        job2 = {
            "job_id": "20260905-130559-report-80",
            "type": "report",
            "title": "[Report] Fix risk tab selection",
        }
        banner2 = dev_console.format_job_header(job2)
        self.assertIn("===== [REPORT] FIX RISK TAB SELECTION =====", banner2)

    @patch("dev_console.shutil.which", side_effect=lambda x: f"/usr/local/bin/{x}" if x == "opencode" else None)
    @patch("dev_console.prompt_input", side_effect=["1", "b"])
    @patch("dev_console.subprocess.run")
    @patch("dev_console.clear_screen")
    def test_handle_ask_ai_interactive_opencode_launch(self, _mock_clear, mock_subproc, _mock_prompt_input, _mock_which):
        job = {
            "job_id": "test-job-123",
            "issue_number": 123,
            "title": "Fix risk tab",
            "status": "review-needed",
            "branch": "feature/test",
            "base_branch": "main",
        }
        mock_subproc.return_value = MagicMock(return_value=0, stdout="", stderr="")
        dev_console.handle_ask_ai(job, ["gemini"])

        called_cmds = [call.args[0] for call in mock_subproc.call_args_list if call.args]
        opencode_calls = [cmd for cmd in called_cmds if isinstance(cmd, list) and cmd[0] == "opencode"]
        self.assertTrue(len(opencode_calls) > 0)
        self.assertEqual(opencode_calls[0][1], "--prompt")
        self.assertIn("Job #123: Fix risk tab", opencode_calls[0][2])

    @patch("dev_console.shutil.which", side_effect=lambda x: f"/usr/local/bin/{x}" if x in ["agy", "claude", "gemini", "codex"] else None)
    @patch("dev_console.prompt_input", side_effect=["1", "2", "3", "4", "b"])
    @patch("dev_console.subprocess.run")
    @patch("dev_console.clear_screen")
    def test_handle_ask_ai_all_cli_options(self, _mock_clear, mock_subproc, _mock_prompt_input, _mock_which):
        job = {
            "job_id": "test-job-789",
            "issue_number": 789,
            "title": "Fix memory leak",
            "status": "review-needed",
            "branch": "feature/fix-leak",
            "base_branch": "main",
        }
        mock_subproc.return_value = MagicMock(return_value=0, stdout="", stderr="")
        dev_console.handle_ask_ai(job, ["gemini"])

        called_cmds = [call.args[0] for call in mock_subproc.call_args_list if call.args]
        # Verify agy
        agy_calls = [cmd for cmd in called_cmds if isinstance(cmd, list) and cmd[0] == "agy"]
        self.assertTrue(len(agy_calls) > 0)
        self.assertEqual(agy_calls[0][1], "--prompt-interactive")
        self.assertIn("Job #789: Fix memory leak", agy_calls[0][2])

        # Verify claude
        claude_calls = [cmd for cmd in called_cmds if isinstance(cmd, list) and cmd[0] == "claude"]
        self.assertTrue(len(claude_calls) > 0)
        self.assertIn("Job #789: Fix memory leak", claude_calls[0][1])

        # Verify gemini
        gemini_calls = [cmd for cmd in called_cmds if isinstance(cmd, list) and cmd[0] == "gemini"]
        self.assertTrue(len(gemini_calls) > 0)
        self.assertEqual(gemini_calls[0][1], "-i")
        self.assertIn("Job #789: Fix memory leak", gemini_calls[0][2])

        # Verify codex
        codex_calls = [cmd for cmd in called_cmds if isinstance(cmd, list) and cmd[0] == "codex"]
        self.assertTrue(len(codex_calls) > 0)
        self.assertEqual(codex_calls[0][1], "--no-alt-screen")
        self.assertIn("Job #789: Fix memory leak", codex_calls[0][2])

    @patch("dev_console.shutil.which", side_effect=lambda x: f"/usr/local/bin/{x}" if x in ["opencode", "claude"] else None)
    @patch("dev_console.prompt_input", side_effect=["", "b"])  # Empty input = hit Enter for default
    @patch("dev_console.subprocess.run")
    @patch("dev_console.clear_screen")
    def test_handle_ask_ai_recommended_default_launch(self, _mock_clear, mock_subproc, _mock_prompt_input, _mock_which):
        # Job built by claude-opus-4-7 should recommend claude (option 2) and launch it on Enter
        job = {
            "job_id": "test-job-999",
            "issue_number": 999,
            "title": "Refactor router",
            "status": "review-needed",
            "builder": "claude-opus-4-7",
        }
        mock_subproc.return_value = MagicMock(return_value=0, stdout="", stderr="")
        dev_console.handle_ask_ai(job, ["gemini"])

        called_cmds = [call.args[0] for call in mock_subproc.call_args_list if call.args]
        claude_calls = [cmd for cmd in called_cmds if isinstance(cmd, list) and cmd[0] == "claude"]
        self.assertTrue(len(claude_calls) > 0)
        self.assertIn("Job #999: Refactor router", claude_calls[0][1])

    @patch("dev_console.prompt_input", side_effect=["g", "b"])
    @patch("dev_console.handle_tweak_revise")
    @patch("dev_console.clear_screen")
    def test_handle_ask_ai_guidance_option(self, _mock_clear, mock_tweak, _mock_prompt_input):
        job = {
            "job_id": "test-job-clue",
            "issue_number": 88,
            "title": "Fix token decoding",
            "status": "debugging",
        }
        revised = {**job, "status": "planned"}
        mock_tweak.return_value = revised
        result = dev_console.handle_ask_ai(job, ["gemini"])
        mock_tweak.assert_called_once_with(job)
        self.assertEqual(result["status"], "planned")

    def test_generate_chat_context_bundles_investigations_and_diff(self):
        job = {
            "job_id": "test-job-ctx-1",
            "issue_number": 55,
            "title": "Fix crash on launch",
            "status": "debugging",
            "branch": "feature/fix-crash",
            "base_branch": "main",
            "interactive_investigations": [
                {
                    "tool": "Codex CLI",
                    "timestamp": "2026-09-06T14:00:00-05:00",
                    "duration": "1m 15s",
                    "notes": "Found nil unwrap in AppDelegate",
                    "new_commits": ["abc1234 Guard against nil config"],
                }
            ],
        }
        bundle, ctx_path = dev_console.generate_chat_context(job)
        self.assertIn("# Context for Job #55: Fix crash on launch", bundle)
        self.assertIn("## Prior Investigation Findings & CLI Notes", bundle)
        self.assertIn("Codex CLI | 1m 15s | 2026-09-06 14:00", bundle)
        self.assertIn("Found nil unwrap in AppDelegate", bundle)
        self.assertIn("`abc1234 Guard against nil config`", bundle)
        self.assertTrue(ctx_path.exists())

    @patch("dev_console.time.sleep")
    @patch("dev_console.shutil.which", side_effect=lambda x: f"/usr/local/bin/{x}" if x == "codex" else None)
    @patch("dev_console.sys.stdin.isatty", return_value=True)
    @patch("dev_console.prompt_input", side_effect=["1", "b"])
    @patch("dev_console.subprocess.run")
    @patch("dev_console.clear_screen")
    @patch("dev_console.save_job")
    def test_handle_ask_ai_captures_commits_into_job_and_investigations_file(
        self, mock_save, _mock_clear, mock_subproc, _mock_prompt_input, _mock_isatty, _mock_which, mock_sleep
    ):
        job = {
            "job_id": "test-job-investigate-1",
            "issue_number": 88,
            "title": "Investigate sqlite lock contention",
            "status": "debugging",
            "branch": "feature/sqlite-fix",
            "base_branch": "main",
        }

        # Mock git rev-parse HEAD (initial), codex run, git log (new commit)
        def subproc_side_effect(cmd, *args, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if isinstance(cmd, list):
                if cmd[:2] == ["git", "rev-parse"]:
                    m.stdout = "head1111\n"
                elif cmd[:2] == ["git", "log"]:
                    m.stdout = "head2222 Add WAL mode pragma\n"
                elif cmd[0] == "codex":
                    m.stdout = ""
                else:
                    m.stdout = ""
            else:
                m.stdout = ""
            m.stderr = ""
            return m

        mock_subproc.side_effect = subproc_side_effect

        dev_console.handle_ask_ai(job, ["codex"])

        self.assertIn("interactive_investigations", job)
        self.assertEqual(len(job["interactive_investigations"]), 1)
        inv = job["interactive_investigations"][0]
        self.assertEqual(inv["tool"], "Codex CLI")
        self.assertEqual(inv["cli_key"], "codex")
        self.assertEqual(inv["new_commits"], ["head2222 Add WAL mode pragma"])

        mock_save.assert_called_with(job)
        mock_sleep.assert_called_with(2.0)

        inv_file = dev_console.OUTPUT_DIR / "test-job-investigate-1" / "investigations.md"
        self.assertTrue(inv_file.exists())
        inv_content = inv_file.read_text(encoding="utf-8")
        self.assertIn("Investigate sqlite lock contention", inv_content)
        self.assertIn("head2222 Add WAL mode pragma", inv_content)

    @patch("dev_console.print_header")
    def test_view_job_brief_summary_renders_investigations(self, _mock_header):
        job_id = "test-job-view-inv"
        job_dir = dev_console.OUTPUT_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        inv_file = job_dir / "investigations.md"
        inv_file.write_text("## Session 1: Codex CLI\n- Notes: Fixed bug", encoding="utf-8")

        job = {"job_id": job_id, "title": "Test Inv View"}
        with patch("builtins.print") as mock_print:
            dev_console.view_job_brief_summary(job)
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("Fixed bug", printed)

    @patch("dev_console.refresh_job")
    @patch("dev_console.get_job_test_summary")
    @patch("dev_console.StatusBar")
    @patch("dev_console.clear_screen")
    @patch("dev_console.input", return_value="")
    @patch("dev_console.get_key", side_effect=["b"])
    def test_handle_job_selection_renders_test_bullet_and_failing_action(
        self, _mock_key, _mock_input, _mock_clear, _mock_status, mock_get_test_summary, mock_refresh
    ):
        job = {
            "job_id": "test-job-fail-1",
            "status": "debugging",
            "debug_phase": "propose",
            "type": "bug",
            "issue_number": 79,
            "_path": "test.json",
        }
        mock_refresh.return_value = job
        mock_get_test_summary.return_value = {
            "status": "failing",
            "created_count": 3,
            "planned_count": 3,
            "failed_count": 1,
            "passed_count": 14,
            "total_run": 15,
            "failing_tests": ["ThemisTests.RiskViewModelTests.testTabSelection"],
            "tests_ok": False,
        }

        with patch("builtins.print") as mock_print:
            dev_console.handle_job_selection(job, [], [])
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("3 created", printed)
            self.assertIn("1 failing", printed)
            self.assertNotIn("passing)", printed)
            self.assertIn("ThemisTests.RiskViewModelTests.testTabSelection", printed)
            self.assertIn("FIX REQUIRED: Auto-Debug", printed)
            self.assertIn("to run Auto-Fix", printed)
            self.assertIn("Fix Failing Tests", printed)
            self.assertIn("(1 test failing)", printed)

    @patch("dev_console.refresh_job")
    @patch("dev_console.get_job_test_summary")
    @patch("dev_console.StatusBar")
    @patch("dev_console.clear_screen")
    @patch("dev_console.input", return_value="")
    @patch("dev_console.get_key", side_effect=["b"])
    def test_handle_job_selection_renders_all_passing_tests(
        self, _mock_key, _mock_input, _mock_clear, _mock_status, mock_get_test_summary, mock_refresh
    ):
        job = {
            "job_id": "test-job-pass-1",
            "status": "review-needed",
            "type": "feature-plan",
            "issue_number": 80,
            "_path": "test.json",
        }
        mock_refresh.return_value = job
        mock_get_test_summary.return_value = {
            "status": "passing",
            "created_count": 2,
            "planned_count": 2,
            "failed_count": 0,
            "passed_count": 25,
            "total_run": 25,
            "failing_tests": [],
            "tests_ok": True,
        }

        with patch("builtins.print") as mock_print:
            dev_console.handle_job_selection(job, [], [])
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("2 created", printed)
            self.assertIn("All Passing", printed)
            self.assertNotIn("suite tests", printed)

    @patch("dev_console.refresh_job")
    @patch("dev_console.get_job_test_summary")
    @patch("dev_console.StatusBar")
    @patch("dev_console.clear_screen")
    @patch("dev_console.input", return_value="")
    @patch("dev_console.get_key", side_effect=["b"])
    def test_handle_job_selection_renders_linked_logs_bulleted_list(
        self, _mock_key, _mock_input, _mock_clear, _mock_status, mock_get_test_summary, mock_refresh
    ):
        job = {
            "job_id": "test-job-logs-1",
            "status": "debugging",
            "last_manual_log_paths": ["logs/build.log", "logs/test.log"],
            "_path": "test.json",
        }
        mock_refresh.return_value = job
        mock_get_test_summary.return_value = {
            "status": "untested",
            "created_count": 0,
            "planned_count": 0,
            "failed_count": 0,
            "passed_count": 0,
            "total_run": 0,
            "failing_tests": [],
            "tests_ok": True,
        }

        with patch("builtins.print") as mock_print:
            dev_console.handle_job_selection(job, [], [])
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("Linked Logs:", printed)
            self.assertIn("2 attached", printed)
            self.assertIn("build.log", printed)
            self.assertIn("test.log", printed)
            self.assertIn("file://", printed)

    @patch("dev_console.refresh_job")
    @patch("dev_console.get_job_test_summary")
    @patch("dev_console.StatusBar")
    @patch("dev_console.clear_screen")
    @patch("dev_console.input", return_value="")
    @patch("dev_console.get_key", side_effect=["b"])
    def test_handle_job_selection_renders_ai_sessions_with_clean_subbullets(
        self, _mock_key, _mock_input, _mock_clear, _mock_status, mock_get_test_summary, mock_refresh
    ):
        job = {
            "job_id": "test-job-ai-1",
            "status": "debugging",
            "llm_sessions": [
                {"id": "sid-1", "model": "gemini-3.1-pro-preview"},
                {"id": "sid-2", "model": "gpt-5.5"},
                {"id": "sid-3", "model": "opencode/big-pickle"},
                {"id": "sid-4", "model": "claude-sonnet-4-6"},
            ],
            "interactive_investigations": [
                {
                    "tool": "Codex CLI",
                    "duration": "10s",
                    "new_commits": ["abc1234"],
                    "notes": "fixed tab selection",
                }
            ],
            "_path": "test.json",
        }
        mock_refresh.return_value = job
        mock_get_test_summary.return_value = {
            "status": "untested",
            "created_count": 0,
            "planned_count": 0,
            "failed_count": 0,
            "passed_count": 0,
            "total_run": 0,
            "failing_tests": [],
            "tests_ok": True,
        }

        with patch("builtins.print") as mock_print:
            dev_console.handle_job_selection(job, [], [])
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("AI Sessions:", printed)
            self.assertIn("4 recorded", printed)
            self.assertIn("View LLM Usage History", printed)
            self.assertIn("Models:", printed)
            self.assertIn("gemini-3.1-pro-preview", printed)
            self.assertIn("+2 more", printed)
            self.assertIn("Latest CLI:", printed)
            self.assertIn("Codex CLI", printed)
            self.assertIn("(10s, 1 commit)", printed)
            self.assertIn("fixed tab selection", printed)

    @patch("dev_console.subprocess.run")
    def test_run_interactive_cli_with_framed_footer_non_tty(self, mock_run):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_run.return_value = mock_res
        
        status_bar = MagicMock()
        ret = dev_console.run_interactive_cli_with_framed_footer(["echo", "hello"], status_bar=status_bar)
        self.assertEqual(ret, 0)
        mock_run.assert_called_once()


    def test_render_ai_changes_synopsis_parses_json_payload_into_clean_sections(self):
        json_payload = json.dumps({
            "hypothesis": "The risk tab failed because RiskDetailsView page was omitted.",
            "summary": "Root cause: omitted page. Fix: always render page. Verified: 21 tests pass.",
            "implementation_plan": "1. Trace data flow.\n2. Add DocumentRiskAnalysisState.\n3. Add unit tests.",
            "files_changed": [
                "ThemisPlayground/DocumentDetailView.swift",
                "ThemisPlayground/Models.swift"
            ],
            "test_command": "-only-testing:ThemisPlaygroundTests/DocumentLogicTests",
            "risks": "UI-level tab selection is covered only indirectly."
        })
        job = {
            "job_id": "test-job-synopsis",
            "builder_summary": json_payload,
            "ai_untracked_files": ["ThemisPlaygroundTests/NewTests.swift"],
        }

        with patch("builtins.print") as mock_print:
            dev_console.render_ai_changes_synopsis(job)
            printed = "\n".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("Hypothesis / Root Cause:", printed)
            self.assertIn("The risk tab failed because", printed)
            self.assertIn("Summary of Changes:", printed)
            self.assertIn("Root cause:", printed)
            self.assertIn("Fix:", printed)
            self.assertIn("Implementation Steps:", printed)
            self.assertIn("1.", printed)
            self.assertIn("Trace data flow.", printed)
            self.assertIn("2.", printed)
            self.assertIn("Add DocumentRiskAnalysisState.", printed)
            self.assertIn("Risks & Considerations:", printed)
            self.assertIn("UI-level tab selection", printed)
            self.assertIn("Modified Files:", printed)
            self.assertIn("DocumentDetailView.swift", printed)
            self.assertIn("Untracked (New) Files:", printed)
            self.assertIn("NewTests.swift", printed)
            self.assertIn("Verification Test Filter:", printed)
            self.assertIn("-only-testing:ThemisPlaygroundTests/DocumentLogicTests", printed)
            # Ensure raw JSON braces are not dumped
            self.assertNotIn('{\n      "hypothesis"', printed)

    def test_render_ai_changes_synopsis_handles_plain_text(self):
        job = {
            "job_id": "test-job-plain",
            "builder_summary": "Applied targeted fix to RiskViewModel.",
            "builder_hypothesis": "Risk tab state binding was disconnected.",
            "ai_modified_files": ["RiskViewModel.swift"],
        }

        with patch("builtins.print") as mock_print:
            dev_console.render_ai_changes_synopsis(job)
            printed = "\n".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("Synopsis / Summary of Changes:", printed)
            self.assertIn("Applied targeted fix to RiskViewModel.", printed)
            self.assertIn("Hypothesis / Root Cause:", printed)
            self.assertIn("Risk tab state binding was disconnected.", printed)
            self.assertIn("Modified Files:", printed)
            self.assertIn("RiskViewModel.swift", printed)

    @patch("os.get_terminal_size", return_value=(50, 24))
    def test_print_wrapped_bullet_hanging_indent_on_small_screens(self, mock_term):
        with patch("builtins.print") as mock_print:
            dev_console.print_wrapped_bullet(
                "  \033[90m└─ 🤖 Models:\033[0m ",
                "\033[97mgemini-3.1-pro-preview, gpt-5.5, opencode/big-pickle (+1 more)\033[0m"
            )
            printed_lines = [call.args[0] for call in mock_print.call_args_list]
            self.assertGreater(len(printed_lines), 1)
            # First line should start with the bullet prefix
            self.assertTrue(printed_lines[0].startswith("  \033[90m└─ 🤖 Models:\033[0m "))
            # All wrapped continuation lines must start with 15 spaces of hanging indent (matching prefix length)
            for line in printed_lines[1:]:
                self.assertTrue(line.startswith(" " * 15), f"Wrapped line should have 15 spaces indent: {repr(line)}")

    @patch("os.get_terminal_size", return_value=(40, 24))
    def test_print_wrapped_bullet_with_osc8_hyperlink_on_small_screens(self, mock_term):
        with patch("builtins.print") as mock_print:
            link = "\033]8;;file:///Users/leemosupreemo/distribution.log\033\\\033[92mdistribution_2026-09-05 13:05:59-bug-79.log\033[0m\033]8;;\033\\"
            dev_console.print_wrapped_bullet("  \033[90m└─ 📄\033[0m ", link)
            printed_lines = [call.args[0] for call in mock_print.call_args_list]
            self.assertGreater(len(printed_lines), 1)
            # Continuation lines must have 7 spaces of hanging indent
            for line in printed_lines[1:]:
                self.assertTrue(line.startswith(" " * 7), f"Wrapped line should have 7 spaces indent: {repr(line)}")

    @patch("os.get_terminal_size", return_value=(55, 24))
    @patch("dev_console.refresh_job")
    @patch("dev_console.get_job_test_summary")
    @patch("dev_console.StatusBar")
    @patch("dev_console.clear_screen")
    @patch("dev_console.input", return_value="")
    @patch("dev_console.get_key", side_effect=["b"])
    def test_handle_job_selection_sub_bullets_wrap_with_hanging_indent(
        self, _mock_key, _mock_input, _mock_clear, _mock_status, mock_get_test_summary, mock_refresh, mock_term
    ):
        job = {
            "job_id": "test-job-narrow",
            "status": "debugging",
            "last_manual_log_paths": ["logs/distribution_2026-09-05 13:05:59-bug-79.log"],
            "llm_sessions": [
                {"id": "sid-1", "model": "gemini-3.1-pro-preview"},
                {"id": "sid-2", "model": "gpt-5.5"},
                {"id": "sid-3", "model": "opencode/big-pickle"},
            ],
            "interactive_investigations": [
                {
                    "tool": "Codex CLI",
                    "duration": "10s",
                    "notes": "investigated and confirmed the issue across all pages",
                }
            ],
            "_path": "test.json",
        }
        mock_refresh.return_value = job
        mock_get_test_summary.return_value = {
            "status": "failing",
            "created_count": 0,
            "planned_count": 3,
            "failed_count": 1,
            "passed_count": 2,
            "total_run": 3,
            "failing_tests": ["ThemisPlaygroundTests/DocumentLogicTests/testDeepLinkNavigationWithInvalidState"],
            "tests_ok": False,
        }

        with patch("builtins.print") as mock_print:
            dev_console.handle_job_selection(job, [], [])
            printed_lines = [call.args[0] for call in mock_print.call_args_list if call.args]
            
            # Check failing test wrap
            failing_lines = [l for l in printed_lines if "DocumentLogicTests" in l or "testDeepLink" in l]
            self.assertTrue(len(failing_lines) >= 1)
            
            # Check models wrap
            models_lines = [l for l in printed_lines if "Models:" in l or "gemini-3.1-pro-preview" in l]
            self.assertTrue(len(models_lines) >= 1)

            # Check logs wrap
            logs_lines = [l for l in printed_lines if "distribution_2026-09-05" in l]
            self.assertTrue(len(logs_lines) >= 1)

    @patch("dev_console.refresh_job")
    @patch("dev_console.get_job_test_summary")
    @patch("dev_console.StatusBar")
    @patch("dev_console.clear_screen")
    @patch("dev_console.input", return_value="")
    @patch("dev_console.get_key", side_effect=["b"])
    def test_handle_job_selection_debugging_shows_attempt_budget_and_pending_state(
        self, _mock_key, _mock_input, _mock_clear, _mock_status, mock_get_test_summary, mock_refresh
    ):
        job = {
            "job_id": "test-job-debug-pending",
            "status": "debugging",
            "iteration": 1,
            "max_iterations": 8,
            "debug_history": [
                {
                    "iteration": 1,
                    "hypothesis": "User reported bug is still happening.",
                    "action": "Iterate fix based on user feedback.",
                    "result": "pending",
                }
            ],
            "_path": "test.json",
        }
        mock_refresh.return_value = job
        mock_get_test_summary.return_value = {
            "status": "untested",
            "created_count": 0,
            "planned_count": 0,
            "failed_count": 0,
            "passed_count": 0,
            "total_run": 0,
            "failing_tests": [],
            "tests_ok": True,
        }

        with patch("builtins.print") as mock_print:
            dev_console.handle_job_selection(job, [], [])
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("Auto-Debug Loop", printed)
            self.assertIn("Ready for Attempt #1", printed)
            self.assertNotIn("Budget: Up to", printed)
            self.assertNotIn("of 8", printed)
            self.assertIn("User reported bug is still happening.", printed)
            self.assertIn("to run Auto-Fix (starts Attempt #1)", printed)

    @patch("dev_console.refresh_job")
    @patch("dev_console.get_job_test_summary")
    @patch("dev_console.StatusBar")
    @patch("dev_console.clear_screen")
    @patch("dev_console.input", return_value="")
    @patch("dev_console.get_key", side_effect=["b"])
    def test_handle_job_selection_debugging_shows_prior_failure_reason_and_next_attempt(
        self, _mock_key, _mock_input, _mock_clear, _mock_status, mock_get_test_summary, mock_refresh
    ):
        job = {
            "job_id": "test-job-debug-failed",
            "status": "debugging",
            "iteration": 2,
            "max_iterations": 8,
            "debug_history": [
                {
                    "iteration": 1,
                    "hypothesis": "Risk tab was omitted from PageTabView.",
                    "action": "Added DocumentRiskAnalysisState in Models.swift",
                    "result": {"build_ok": True, "tests_ok": False},
                }
            ],
            "_path": "test.json",
        }
        mock_refresh.return_value = job
        mock_get_test_summary.return_value = {
            "status": "failing",
            "created_count": 3,
            "planned_count": 3,
            "failed_count": 1,
            "passed_count": 2,
            "total_run": 3,
            "failing_tests": ["DocumentLogicTests.testTabSelection"],
            "tests_ok": False,
        }

        with patch("builtins.print") as mock_print:
            dev_console.handle_job_selection(job, [], [])
            printed = " ".join(str(c) for c in mock_print.call_args_list)
            self.assertIn("Attempt #1 Failed", printed)
            self.assertIn("1 suite test failing", printed)
            self.assertIn("Ready for", printed)
            self.assertIn("Attempt #2", printed)
            self.assertNotIn("of 8", printed)
            self.assertIn("Added DocumentRiskAnalysisState", printed)
            self.assertIn("DocumentLogicTests.testTabSelection", printed)
            self.assertIn("to run Auto-Fix (starts Attempt #2)", printed)


if __name__ == "__main__":
    unittest.main()







