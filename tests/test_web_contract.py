from __future__ import annotations

import unittest

from orchestrator.web.contract import Operation, validate_command
from orchestrator.web.menu import configuration_menu, main_menu, available_actions


class WebContractTests(unittest.TestCase):
    def test_main_menu_covers_console_navigation(self):
        self.assertEqual(
            [item.label for item in main_menu()],
            ["New Job", "Run Unit Tests", "Configuration & Tools", "Refresh & Sync"],
        )

    def test_configuration_menu_covers_console_items(self):
        self.assertEqual(
            [item.label for item in configuration_menu()],
            [
                "LLM Models", "Manage LLM API Keys", "Manage Machine Fleet",
                "Run Prerequisite Audit", "Select Base Branch", "Change Target Project",
                "Manage Archived Jobs", "AI Instruction Settings",
                "Email Notification Settings", "Documentation & Architecture Guides",
                "Firebase App Distro", "Orchestrator Self-Tests", "Update Orchestrator",
            ],
        )

    def test_planned_feature_plan_approves_subtasks(self):
        actions = {item.operation for item in available_actions({"status": "planned", "type": "feature-plan"})}
        self.assertIn(Operation.JOB_APPROVE_SUBTASKS, actions)
        self.assertNotIn(Operation.JOB_SCHEDULE, actions)
        self.assertIn(Operation.JOB_REVISE, actions)

    def test_planned_bug_schedules(self):
        actions = {item.operation for item in available_actions({"status": "planned", "type": "bug"})}
        self.assertIn(Operation.JOB_SCHEDULE, actions)
        self.assertNotIn(Operation.JOB_APPROVE_SUBTASKS, actions)

    def test_review_needed_has_merge_delivery_and_iteration(self):
        actions = {item.operation for item in available_actions({"status": "review-needed", "type": "bug-fix"})}
        self.assertTrue({Operation.JOB_MERGE, Operation.JOB_DELIVER, Operation.JOB_ITERATE, Operation.JOB_REOPEN_BUG} <= actions)

    def test_executing_has_resume_and_reset(self):
        actions = {item.operation for item in available_actions({"status": "executing", "type": "feature"})}
        self.assertIn(Operation.JOB_RESUME, actions)
        self.assertIn(Operation.JOB_RERUN, actions)
        self.assertNotIn(Operation.JOB_MERGE, actions)

    def test_common_context_actions_are_visible(self):
        actions = {item.operation for item in available_actions({"status": "planned", "type": "feature"})}
        self.assertTrue({Operation.JOB_LINK_LOGS, Operation.JOB_ATTACH_REFERENCE, Operation.JOB_ASK_AI,
                         Operation.JOB_MODELS, Operation.JOB_EXPORT, Operation.JOB_BRIEF,
                         Operation.JOB_GITHUB, Operation.JOB_CLOSE_ISSUE, Operation.JOB_DISCARD} <= actions)

    def test_command_rejects_unknown_operation_and_shell_arguments(self):
        good = {"project_id": "thirteen", "operation": "job.schedule", "job_id": "job-1",
                "inputs": {}, "expected_revision": 3}
        self.assertEqual(validate_command(good).operation, Operation.JOB_SCHEDULE)
        with self.assertRaises(ValueError):
            validate_command({**good, "operation": "shell.exec"})
        with self.assertRaises(ValueError):
            validate_command({**good, "args": ["rm", "-rf", "/"]})
        with self.assertRaises(ValueError):
            validate_command({**good, "inputs": {"command": "rm -rf /"}})

    def test_mutating_job_command_requires_revision(self):
        with self.assertRaises(ValueError):
            validate_command({"project_id": "thirteen", "operation": "job.schedule", "job_id": "job-1", "inputs": {}})
        with self.assertRaises(ValueError):
            validate_command({"project_id": "../other", "operation": "job.schedule", "job_id": "job-1",
                              "expected_revision": 1, "inputs": {}})


if __name__ == "__main__":
    unittest.main()
