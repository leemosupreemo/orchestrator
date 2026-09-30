from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"

# Add current dir to sys.path so we can import orchestrator and common
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

class E2EWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        # Create a temporary project root
        self.test_dir = tempfile.TemporaryDirectory(prefix="orchestrator-e2e-")
        self.root = Path(self.test_dir.name)
        self.state_dir = tempfile.TemporaryDirectory(prefix="orchestrator-user-state-")
        self.old_env = os.environ.copy()
        os.environ["ORCHESTRATOR_USER_STATE_DIR"] = self.state_dir.name
        
        # Set environment variables BEFORE any orchestrator modules are imported or used
        os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(self.root)
        os.environ["ORCHESTRATOR_RUNTIME_DIR"] = ".orchestrator"
        os.environ["ORCHESTRATOR_USER_STATE_DIR"] = self.state_dir.name

        # Mock a git repo
        subprocess.run(["git", "init"], cwd=str(self.root), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.root))
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.root))
        (self.root / "README.md").write_text("# Test Project")
        subprocess.run(["git", "add", "README.md"], cwd=str(self.root))
        self.old_modules = dict(sys.modules)
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith("orchestrator") or mod_name in ["common", "new_job", "probe_machine", "llm", "model_router", "model_registry"]:
                del sys.modules[mod_name]

        # Initialize orchestrator in this project
        from orchestrator import cli
        cli.main(["init", "--root", str(self.root), "--project-name", "TestProject", "--base-branch", "master"])
        
        # Mock some required files for new_job.py
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs" / "build-test-commands.md").write_text("## iOS app build\n```bash\necho build\n```\n## iOS app tests\n```bash\necho test\n```")

    def tearDown(self) -> None:
        sys.modules.clear()
        sys.modules.update(self.old_modules)
        os.environ.clear()
        os.environ.update(self.old_env)
        self.state_dir.cleanup()
        self.test_dir.cleanup()

    def make_job_paths(self, job_id):
        from orchestrator.scripts import common

        jobs_dir = self.root / ".orchestrator" / "jobs"
        output_dir = self.root / ".orchestrator" / "output" / job_id
        jobs_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        return common.JobPaths(
            job_file=jobs_dir / f"{job_id}.json",
            brief_file=output_dir / "brief.md",
            output_dir=output_dir
        )

    @patch("orchestrator.scripts.new_job.run_llm")
    @patch("orchestrator.scripts.new_job.create_issue")
    def test_new_job_branch_mode_mapping(self, mock_create_issue, mock_llm):
        """
        Tests that branch mode choice in dev_console.py is correctly mapped 
        when passed to new_job.py.
        """
        mock_create_issue.return_value = 123
        mock_llm.return_value = (json.dumps({
            "title": "E2E Test Job",
            "summary": "Summary",
            "assumptions": [],
            "constraints": [],
            "risks": [],
            "tasks": [{"title": "Task 1", "description": "Desc", "acceptance_criteria": ["AC"], "likely_files": [], "tests": [], "complexity": "low"}]
        }), "mock-model", "mock-session-id")

        args = ["feature", "--branch-mode", "manual", "--no-dispatch", "--yolo"]
        input_data = "E2E Test Job\nAC1\n\nConstraints\n\n"
        
        from orchestrator.scripts import new_job

        with patch("orchestrator.scripts.new_job.ROOT", self.root):
          with patch("orchestrator.scripts.new_job.make_job_paths", side_effect=self.make_job_paths):
            with patch("sys.stdin", io.StringIO(input_data)):
                new_job.main(args)
        
        # Check the created job file
        jobs_dir = self.root / ".orchestrator" / "jobs"
        job_files = list(jobs_dir.glob("*.json"))
        self.assertEqual(len(job_files), 1)
        job_data = json.loads(job_files[0].read_text())
        self.assertEqual(job_data["branch_mode"], "manual")
        self.assertIsNone(job_data["branch"])
        mock_create_issue.assert_called_once()

    @patch("orchestrator.scripts.new_job.run_llm")
    @patch("orchestrator.scripts.new_job.create_issue")
    def test_role_based_job_persists_team_and_gives_planner_shared_context(self, mock_create_issue, mock_llm):
        mock_create_issue.return_value = 131
        mock_llm.return_value = (json.dumps({
            "title": "Persist Profiles",
            "summary": "Persist user profiles",
            "assumptions": ["Profiles already exist in memory"],
            "constraints": ["No data loss"],
            "risks": ["Migration compatibility"],
            "likely_files": ["database/profiles.sql"],
            "tasks": [{"title": "Add migration", "description": "Persist profiles", "acceptance_criteria": ["Existing profiles survive upgrade"], "likely_files": ["database/profiles.sql"], "tests": [], "complexity": "medium"}],
        }), "mock-model", "mock-session-id")

        from orchestrator.scripts import new_job
        args = [
            "feature", "--summary", "Persist user profiles in a database migration",
            "--development-approach", "role-based", "--team-roles", "database_specialist",
            "--branch-mode", "manual", "--no-dispatch", "--yolo",
        ]
        with patch("orchestrator.scripts.new_job.ROOT", self.root):
            with patch("orchestrator.scripts.new_job.make_job_paths", side_effect=self.make_job_paths):
                new_job.main(args)

        job_file = next((self.root / ".orchestrator" / "jobs").glob("*.json"))
        job = json.loads(job_file.read_text())
        self.assertEqual(job["development_approach"], "role-based")
        self.assertEqual(job["team"]["mode"], "custom")
        self.assertIn("database_specialist", [role["id"] for role in job["team"]["roles"]])
        self.assertEqual(job["team"]["intent_brief"]["user_outcome"], "Persist user profiles")
        self.assertEqual(job["team"]["intent_brief"]["acceptance_criteria"], ["Existing profiles survive upgrade"])
        self.assertEqual(len(job["work_packages"]), 1)
        self.assertEqual(job["work_packages"][0]["role"], "implementation_engineer")
        self.assertEqual(job["work_packages"][0]["requested_mode"], "agentic")
        planner_prompt = mock_llm.call_args.args[1]
        self.assertIn("ROLE-BASED TEAM CONTEXT", planner_prompt)
        self.assertIn("Database Specialist", planner_prompt)

    @patch("orchestrator.scripts.new_job.run_llm")
    @patch("orchestrator.scripts.new_job.gh_text")
    def test_replanning_role_based_job_preserves_user_customized_team(self, mock_gh_text, mock_llm):
        from orchestrator.scripts import new_job
        from orchestrator.scripts.team_roles import assemble_team

        mock_llm.return_value = (json.dumps({
            "title": "Account Settings",
            "summary": "Adjust account settings",
            "assumptions": [],
            "constraints": [],
            "risks": [],
            "acceptance_criteria": ["Settings save"],
            "likely_files": [],
            "tasks": [{"title": "Adjust settings", "description": "Update behavior", "acceptance_criteria": ["Settings save"], "likely_files": [], "tests": [], "complexity": "low"}],
        }), "mock-model", "mock-session-id")
        job_id = "existing-role-job"
        paths = self.make_job_paths(job_id)
        custom_team = assemble_team("", "feature", requested_roles=["accessibility_specialist"])
        custom_team["intent_brief"] = {
            "user_request": "Let users manage an account accessibly",
            "user_outcome": "Accessible account management",
            "acceptance_criteria": ["VoiceOver announces every control"],
            "constraints": ["Preserve VoiceOver behavior"],
            "assumptions": ["Account settings already exist"],
            "risks": ["Focus order regression"],
            "non_goals": ["Redesign authentication"],
        }
        paths.job_file.write_text(json.dumps({
            "job_id": job_id,
            "type": "feature-plan",
            "issue_number": 44,
            "title": "Account Settings",
            "development_approach": "role-based",
            "team": custom_team,
            "plan": {},
        }))

        args = [
            "feature", "--update", str(paths.job_file), "--summary", "Adjust account settings",
            "--branch-mode", "manual", "--no-dispatch", "--yolo",
        ]
        with patch("orchestrator.scripts.new_job.ROOT", self.root):
            with patch("orchestrator.scripts.new_job.make_job_paths", side_effect=self.make_job_paths):
                new_job.main(args)

        updated = json.loads(paths.job_file.read_text())
        self.assertEqual(updated["team"]["mode"], "custom")
        self.assertIn("accessibility_specialist", [role["id"] for role in updated["team"]["roles"]])
        self.assertEqual(updated["team"]["intent_brief"]["user_request"], "Let users manage an account accessibly")
        self.assertIn("Preserve VoiceOver behavior", updated["team"]["intent_brief"]["constraints"])
        planner_prompt = mock_llm.call_args.args[1]
        for value in (
            "Accessible account management",
            "VoiceOver announces every control",
            "Preserve VoiceOver behavior",
            "Account settings already exist",
            "Focus order regression",
            "Redesign authentication",
        ):
            self.assertIn(value, planner_prompt)

    @patch("orchestrator.scripts.new_job.flush_stdin")
    @patch("orchestrator.scripts.new_job.run_llm")
    @patch("orchestrator.scripts.new_job.create_issue")
    def test_stdin_flushing_is_called(self, mock_create_issue, mock_llm, mock_flush):
        """
        Tests that flush_stdin is called before multiline prompts.
        """
        mock_create_issue.return_value = 124
        mock_llm.return_value = (json.dumps({
            "title": "Flush Test",
            "summary": "Summary",
            "assumptions": [],
            "constraints": [],
            "risks": [],
            "tasks": [{"title": "Task 1", "description": "Desc", "acceptance_criteria": ["AC"], "likely_files": [], "tests": [], "complexity": "low"}]
        }), "mock-model", "mock-session-id")

        args = ["feature", "--branch-mode", "new", "--no-dispatch", "--yolo"]
        input_data = "Flush Test Job\nAC1\n\n\n"
        
        from orchestrator.scripts import new_job
        with patch("orchestrator.scripts.new_job.ROOT", self.root):
            with patch("orchestrator.scripts.new_job.make_job_paths", side_effect=self.make_job_paths):
                with patch("sys.stdin", io.StringIO(input_data)):
                    new_job.main(args)
        
        # Verify flush_stdin was called
        self.assertTrue(mock_flush.called)
        mock_create_issue.assert_called_once()

    @patch("orchestrator.scripts.new_job.run_llm")
    @patch("orchestrator.scripts.new_job.create_issue")
    def test_malformed_verifier_output_is_ignored(self, mock_create_issue, mock_llm):
        mock_create_issue.return_value = 125
        mock_llm.side_effect = [
            (json.dumps({
                "title": "Verifier Fallback",
                "summary": "Summary",
                "assumptions": [],
                "constraints": [],
                "risks": [],
                "tasks": [{"title": "Task 1", "description": "Desc", "acceptance_criteria": ["AC"], "likely_files": [], "tests": [], "complexity": "low"}]
            }), "gemini-3.1-pro-preview", "sid-1"),
            (json.dumps({"comments": "Missing status"}), "gemini-3.1-pro-preview", "sid-2"),
        ]

        args = [
            "feature",
            "--branch-mode", "manual",
            "--no-dispatch",
            "--allowed-models", "gemini,codex",
            "--allowed-machines", "local",
        ]
        input_data = "Verifier fallback\n\n"

        from orchestrator.scripts import new_job
        with patch("orchestrator.scripts.new_job.ROOT", self.root):
            with patch("orchestrator.scripts.new_job.make_job_paths", side_effect=self.make_job_paths):
                with patch("sys.stdin", io.StringIO(input_data)):
                    new_job.main(args)

        jobs_dir = self.root / ".orchestrator" / "jobs"
        job_files = list(jobs_dir.glob("*.json"))
        self.assertEqual(len(job_files), 1)
        job_data = json.loads(job_files[0].read_text())
        self.assertIsNone(job_data["verification"])
        self.assertEqual(job_data["status"], "planned")
        mock_create_issue.assert_called_once()

    @patch("orchestrator.scripts.dev_console.prompt_radio")
    @patch("orchestrator.scripts.dev_console.prompt_confirm")
    @patch("orchestrator.scripts.dev_console.run_script")
    def test_dev_console_mapping_logic(self, mock_run_script, mock_confirm, mock_radio):
        """
        Tests the mapping logic inside dev_console.handle_new_job.
        """
        from orchestrator.scripts import dev_console
        
        # Mock UI interactions
        mock_radio.side_effect = [
            "feature", # Job type
            "Role-based team — Recommended",
            "manual (no git actions)" # Branch choice
        ]
        mock_confirm.return_value = False # No Stitch, No Spec, No Advanced, No YOLO
        dev_console.handle_new_job(session_allowed_models=["gpt-5.5"], session_allowed_machines=["local"])
        
        # Verify run_script was called with mapped branch mode
        args_passed = mock_run_script.call_args[0][1]
        self.assertIn("--branch-mode", args_passed)
        idx = args_passed.index("--branch-mode")
        self.assertEqual(args_passed[idx+1], "manual")
        self.assertEqual(
            args_passed[args_passed.index("--development-approach") + 1],
            "role-based",
        )

    @patch("orchestrator.scripts.new_job.gh_text")
    def test_create_issue_retry_on_missing_labels(self, mock_gh_text):
        from orchestrator.scripts import new_job
        import subprocess
        
        # First call fails because 'source:manual' is not found
        # Second call succeeds
        mock_gh_text.side_effect = [
            subprocess.CalledProcessError(
                returncode=1,
                cmd=["gh", "issue", "create"],
                output="",
                stderr="could not add label: 'source:manual' not found"
            ),
            "https://github.com/org/repo/issues/123"
        ]
        
        issue_number = new_job.create_issue("Test issue", "Body text", ["job:bug", "source:manual"])
        
        self.assertEqual(issue_number, 123)
        self.assertEqual(mock_gh_text.call_count, 2)
        # Check second call didn't have '--label source:manual'
        last_call_args = mock_gh_text.call_args_list[1][0]
        self.assertNotIn("source:manual", last_call_args)

    @patch("orchestrator.scripts.new_job.run_llm")
    @patch("orchestrator.scripts.new_job.create_issue")
    def test_new_job_coverage_with_summary(self, mock_create_issue, mock_llm):
        """Verify new_job supports --summary argument for coverage job creation."""
        mock_create_issue.return_value = 130
        mock_llm.return_value = (json.dumps({
            "title": "Coverage: AuthViewModel",
            "summary": "Expand Unit Test Coverage: AuthViewModel",
            "assumptions": [],
            "constraints": [],
            "risks": [],
            "tasks": [{"title": "Write AuthViewModel tests", "description": "Desc", "acceptance_criteria": ["100% coverage"], "likely_files": [], "tests": [], "complexity": "low"}]
        }), "mock-model", "mock-session-id")

        from orchestrator.scripts import new_job
        args = ["coverage", "--summary", "Expand Unit Test Coverage: AuthViewModel", "--branch-mode", "manual", "--no-dispatch"]
        with patch("orchestrator.scripts.new_job.ROOT", self.root):
            with patch("orchestrator.scripts.new_job.make_job_paths", side_effect=self.make_job_paths):
                new_job.main(args)

        # Check job file creation
        jobs_dir = self.root / ".orchestrator" / "jobs"
        job_files = list(jobs_dir.glob("*coverage*.json"))
        self.assertEqual(len(job_files), 1)
        job_data = json.loads(job_files[0].read_text())
        self.assertEqual(job_data["type"], "test-audit")
        mock_create_issue.assert_called_once()


if __name__ == "__main__":
    unittest.main()
