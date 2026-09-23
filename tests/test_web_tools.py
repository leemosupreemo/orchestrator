from __future__ import annotations

import json
import tempfile
import unittest
import types
import sys
from unittest.mock import patch
from subprocess import CompletedProcess, TimeoutExpired
from pathlib import Path

from orchestrator.web.contract import Operation
from orchestrator.web import tools
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
        cleared = run_tool(self.root, Operation.TOOL_KEYS, {"action": "clear"})
        self.assertFalse(cleared["configured"]["openai"])

    def test_fleet_enablement_updates_local_machine_config(self):
        run_tool(self.root, Operation.TOOL_FLEET, {"action": "disable", "name": "local"})
        machines = json.loads((self.root / ".orchestrator" / "config" / "machines.json").read_text())
        self.assertFalse(machines["machines"][0]["enabled"])

    def test_fleet_can_select_rename_and_remove_remote_machine(self):
        path = self.root / ".orchestrator" / "config" / "machines.json"
        data = json.loads(path.read_text())
        data["machines"].append({"name": "remote", "execution_mode": "ssh", "ssh_target": "host", "repo_path": "/repo"})
        path.write_text(json.dumps(data))
        run_tool(self.root, Operation.TOOL_FLEET, {"action": "select", "machine_ids": ["remote"]})
        self.assertEqual(run_tool(self.root, Operation.TOOL_FLEET, {})["selected"], ["remote"])
        run_tool(self.root, Operation.TOOL_FLEET, {"action": "rename", "name": "remote", "new_name": "build"})
        self.assertEqual(run_tool(self.root, Operation.TOOL_FLEET, {})["selected"], ["build"])
        run_tool(self.root, Operation.TOOL_FLEET, {"action": "remove", "name": "build"})
        self.assertEqual([m["name"] for m in run_tool(self.root, Operation.TOOL_FLEET, {})["machines"]], ["local"])

    def test_fleet_purge_runs_in_selected_project_context(self):
        with patch("orchestrator.web.tools.subprocess.run", return_value=CompletedProcess([], 0, '{"purged": 1}', "")) as run:
            result = run_tool(self.root, Operation.TOOL_FLEET, {"action": "purge", "machine_ids": ["local"]})
        self.assertEqual(result["purged"], 1)
        self.assertEqual(run.call_args.kwargs["env"]["ORCHESTRATOR_PROJECT_ROOT"], str(self.root))

    def test_instructions_manage_cli_files_and_required_links(self):
        status = run_tool(self.root, Operation.TOOL_INSTRUCTIONS, {})
        self.assertIn("AGENTS.md", status["files"])
        run_tool(self.root, Operation.TOOL_INSTRUCTIONS, {"action": "create_missing"})
        path = self.root / "AGENTS.md"
        self.assertIn("docs/architecture.md", path.read_text())
        path.write_text("# Custom\n")
        run_tool(self.root, Operation.TOOL_INSTRUCTIONS, {"action": "repair"})
        self.assertIn("docs/ai-workflow.md", path.read_text())

    def test_copy_role_prompts_preserves_project_settings(self):
        settings = self.root / ".orchestrator" / "config" / "settings.json"
        settings.write_text('{"gemini_api_key":"keep"}')
        result = run_tool(self.root, Operation.TOOL_INSTRUCTIONS, {"action": "copy_role_prompts"})
        self.assertIn("agent_lead.md", result["role_prompts"])
        self.assertEqual(json.loads(settings.read_text())["gemini_api_key"], "keep")

    def test_email_import_and_provider_use_cli_settings(self):
        result = run_tool(self.root, Operation.TOOL_EMAIL, {"action": "import", "content": 'name,email\nA,a@example.com\nB,"b@example.com"'})
        self.assertEqual(result["notification_emails"], ["a@example.com", "b@example.com"])
        result = run_tool(self.root, Operation.TOOL_EMAIL, {"action": "provider", "provider": "resend", "sender_email": "sender@example.com"})
        self.assertEqual(result["sender_email"], "sender@example.com")
        self.assertIn("sender_ready", result)
        self.assertNotIn("password", json.dumps(result))

    def test_docs_include_cli_help_and_project_root(self):
        (self.root / "README.md").write_text("# Root\n")
        listing = run_tool(self.root, Operation.TOOL_DOCS, {})["documents"]
        self.assertIn("README.md", listing)
        self.assertEqual(run_tool(self.root, Operation.TOOL_DOCS, {"name": "README.md"})["content"], "# Root\n")

    def test_key_status_reports_cli_readiness_without_credentials(self):
        result = run_tool(self.root, Operation.TOOL_KEYS, {})
        self.assertIn("codex", result["cli_available"])
        self.assertIn("gemini", result["sign_in_commands"])

    def test_email_test_uses_notification_script(self):
        run_tool(self.root, Operation.TOOL_EMAIL, {"action": "add", "email": "me@example.com"})
        with patch("orchestrator.web.tools.subprocess.run", return_value=CompletedProcess([], 0, "sent", "")) as run:
            result = run_tool(self.root, Operation.TOOL_EMAIL, {"action": "test"})
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("notify.py", str(run.call_args))

    def test_audit_includes_fleet_probes(self):
        probe = types.SimpleNamespace(probe_machine=lambda machine: {"machine": machine["name"], "reachable": True})
        with patch("orchestrator.web.tools.subprocess.run", return_value=CompletedProcess([], 0, "ready", "")), patch.dict(sys.modules, {"probe_machine": probe}):
            result = run_tool(self.root, Operation.TOOL_AUDIT, {})
        self.assertEqual(result["fleet"], [{"machine": "local", "reachable": True}])

    def test_audit_interprets_missing_and_optional_checks(self):
        report = ("  \033[92m✅\033[0m Homebrew                       INSTALLED\n"
                  "  \033[91m❌\033[0m Node.js                        MISSING\n"
                  "  ❌ Manual API Keys                NONE\n"
                  "  ❌ Signing Credentials            ASC API KEYS (INSECURE LOCATION)\n"
                  "  ❌ claude CLI                     MISSING\n"
                  "  ✅ codex Auth                    LOGGED IN\n"
                  "  ✅ READY: At least one AI provider is configured.\n"
                  "  ○ Playwright MCP                 recommended, not required\n")
        probe = types.SimpleNamespace(probe_machine=lambda machine: {"machine": machine["name"], "reachable": True, "repo_path_ok": True, "binaries": {"codex": True, "node": False}})
        with patch("orchestrator.web.tools.subprocess.run", return_value=CompletedProcess([], 0, report, "")), patch.dict(sys.modules, {"probe_machine": probe}):
            result = run_tool(self.root, Operation.TOOL_AUDIT, {})
        by_name = {item["name"]: item for item in result["checklist"]}
        self.assertEqual(by_name["Homebrew"]["status"], "ready")
        self.assertEqual(by_name["Node.js"]["status"], "needs_attention")
        self.assertIn("Install", by_name["Node.js"]["next_step"])
        self.assertEqual(by_name["Manual API Keys"]["status"], "optional")
        self.assertIn(".private_keys", by_name["Signing Credentials"]["next_step"])
        self.assertEqual(by_name["claude CLI"]["status"], "optional")
        self.assertEqual(by_name["Playwright MCP"]["status"], "optional")

    def test_update_status_does_not_run_pull(self):
        with patch("orchestrator.web.tools.subprocess.run") as run:
            result = run_tool(self.root, Operation.TOOL_UPDATE, {"action": "status"})
        self.assertIn("package_location", result)
        self.assertFalse(any("pull" in str(call) for call in run.call_args_list))

    def test_distribution_smoke_test_is_noninteractive(self):
        with patch("orchestrator.web.tools.subprocess.run", return_value=CompletedProcess([], 0, "ok", "")) as run:
            run_tool(self.root, Operation.TOOL_DISTRIBUTION, {"action": "smoke_test"})
        self.assertIn("--non-interactive", run.call_args.args[0])

    def test_long_running_command_reports_output_before_completion(self):
        updates = []
        command = [sys.executable, "-u", "-c", "import time; print('first'); time.sleep(.3); print('second')"]
        self.assertTrue(hasattr(tools, "_run_command"))
        result = tools._run_command(command, cwd=self.root, env={}, timeout=5, progress=updates.append)
        self.assertEqual(result.returncode, 0)
        self.assertIn("first", result.stdout)
        self.assertIn("second", result.stdout)
        self.assertTrue(any("first" in update and "second" not in update for update in updates))
        self.assertIn("second", updates[-1])

    def test_package_self_tests_do_not_inherit_selected_project_root(self):
        captured = {}
        def fake_run(command, *, cwd, env, timeout, progress):
            captured.update(cwd=cwd, env=env)
            return CompletedProcess(command, 0, "OK", "")
        with patch.dict(tools._run_local.__globals__, {"_run_command": fake_run}):
            tools._run_local(self.root, Operation.TOOL_SELF_TESTS, {"suite": "all"})
        self.assertEqual(captured["env"]["ORCHESTRATOR_PROJECT_ROOT"], str(captured["cwd"]))
        self.assertNotEqual(captured["cwd"], self.root)

    def test_streamed_command_times_out_and_keeps_partial_log(self):
        updates = []
        command = [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(5)"]
        with self.assertRaises(TimeoutExpired) as ended:
            tools._run_command(command, cwd=self.root, env={}, timeout=1, progress=updates.append)
        self.assertIn("started", ended.exception.output)
        self.assertIn("started", updates[-1])

    def test_distribution_status_reports_login_and_project_without_account(self):
        (self.root / ".firebaserc").write_text('{"projects":{"default":"example-project"}}')
        with patch("orchestrator.web.tools.shutil.which", return_value="/usr/bin/firebase"), patch(
            "orchestrator.web.tools.subprocess.run", return_value=CompletedProcess([], 0, "Logged in as someone@example.com", "")
        ):
            result = run_tool(self.root, Operation.TOOL_DISTRIBUTION, {"action": "status"})
        self.assertTrue(result["firebase_logged_in"])
        self.assertEqual(result["project_id"], "example-project")
        self.assertNotIn("someone@example.com", json.dumps(result))

    def test_noninteractive_delivery_preflight_never_opens_wizard(self):
        from orchestrator.scripts import smoke_test_delivery
        config = types.SimpleNamespace(validate_distribution_config=lambda: ["signing missing"], runtime_dir=self.root / ".orchestrator")
        with patch.object(smoke_test_delivery, "PROJECT_CONFIG", config), patch.object(smoke_test_delivery, "prompt_confirm", side_effect=AssertionError("interactive prompt")):
            with self.assertRaises(SystemExit) as ended:
                smoke_test_delivery.run_smoke_delivery(non_interactive=True)
        self.assertEqual(ended.exception.code, 1)

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
