from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess
from pathlib import Path

from orchestrator.web.contract import Operation
from orchestrator.web.tools import run_tool, run_app_tests, run_refresh


class WebToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".orchestrator" / "config").mkdir(parents=True)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "guide.md").write_text("# Guide\n")
        (self.root / ".orchestrator" / "config" / "machines.json").write_text(
            json.dumps({"machines": [{"name": "local", "enabled": True, "execution_mode": "local"}]}))

    def tearDown(self):
        self.temp.cleanup()

    def test_base_branch_change_writes_local_settings(self):
        run_tool(self.root, Operation.TOOL_BASE_BRANCH, {"branch": "develop"})
        settings = json.loads((self.root / ".orchestrator" / "config" / "settings.json").read_text())
        self.assertEqual(settings["global_base_branch"], "develop")

    def test_email_settings_can_add_recipient(self):
        result = run_tool(self.root, Operation.TOOL_EMAIL, {"action": "add", "email": "me@example.com"})
        self.assertEqual(result["notification_emails"], ["me@example.com"])

    def test_document_reader_cannot_escape_project_docs(self):
        self.assertEqual(run_tool(self.root, Operation.TOOL_DOCS, {"name": "guide.md"})["content"], "# Guide\n")
        with self.assertRaises(ValueError):
            run_tool(self.root, Operation.TOOL_DOCS, {"name": "../../secret"})

    def test_api_key_menu_returns_status_without_values(self):
        (self.root / ".orchestrator" / "config" / "settings.json").write_text(
            json.dumps({"openai_api_key": "TOP_SECRET"}))
        result = run_tool(self.root, Operation.TOOL_KEYS, {})
        self.assertTrue(result["configured"]["openai"])
        self.assertNotIn("TOP_SECRET", json.dumps(result))
        with self.assertRaises(ValueError):
            run_tool(self.root, Operation.TOOL_KEYS, {"openai_api_key": "TOP_SECRET"})

    def test_fleet_enablement_updates_local_machine_config(self):
        run_tool(self.root, Operation.TOOL_FLEET, {"action": "disable", "name": "local"})
        machines = json.loads((self.root / ".orchestrator" / "config" / "machines.json").read_text())
        self.assertFalse(machines["machines"][0]["enabled"])

    def test_app_tests_lists_xcode_plans_instead_of_python_tests(self):
        (self.root / ".orchestrator" / "project.json").write_text(json.dumps({"test_target": "AppTests"}))
        plans = self.root / "AppTests" / "TestPlans"
        plans.mkdir(parents=True)
        (plans / "Unit.xctestplan").write_text('{"testTargets": []}')
        self.assertEqual(run_app_tests(self.root, {"action": "list"})["plans"], ["Unit.xctestplan"])

    def test_refresh_archives_closed_github_issue_and_reports_fleet(self):
        jobs = self.root / ".orchestrator" / "jobs"
        jobs.mkdir(parents=True)
        (jobs / "job-1.json").write_text(json.dumps({"job_id": "job-1", "issue_number": 12, "status": "planned"}))
        with patch("orchestrator.web.tools.subprocess.run", return_value=CompletedProcess([], 0, '[{"number":12}]', '')):
            result = run_refresh(self.root)
        self.assertEqual(result["archived_jobs"], ["job-1"])
        self.assertTrue(result["machine_availability"]["local"])
        self.assertTrue((jobs / "archive" / "job-1.json").exists())


if __name__ == "__main__":
    unittest.main()
