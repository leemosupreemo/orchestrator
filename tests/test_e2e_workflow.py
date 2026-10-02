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

    PLAN = json.dumps({
        "title": "Context Test", "summary": "Summary", "assumptions": [], "constraints": [], "risks": [],
        "tasks": [{"title": "Task 1", "description": "Desc", "acceptance_criteria": ["AC"], "likely_files": [], "tests": [], "complexity": "low"}],
        "test_cases": [{"title": "AC holds", "type": "unit", "expected": "AC is satisfied", "covers": ["AC"], "tests": ["test_ac"]}]})

    PRD_TEXT = None

    def plan_with_docs(self, prd_text, kind="feature"):
        from orchestrator import prd
        if prd_text:
            prd.Prd(self.root, self.root / ".orchestrator").write(prd_text, record=False)
        prompts = []

        def fake_llm(model, prompt, *a, **k):
            prompts.append(prompt)
            return (self.PLAN, "gemini-3.1-pro-preview", "sid")  # a real model id, so the verification step runs as it does for real jobs

        from orchestrator.scripts import new_job
        with patch("orchestrator.scripts.new_job.run_llm", side_effect=fake_llm), patch("orchestrator.scripts.new_job.create_issue", return_value=130):
            with patch("orchestrator.scripts.new_job.ROOT", self.root), patch("orchestrator.scripts.new_job.make_job_paths", side_effect=self.make_job_paths):
                with patch("sys.stdin", io.StringIO("Add rematch\n\n")):
                    new_job.main([kind, "--branch-mode", "manual", "--no-dispatch", "--summary", "Add rematch", "--allowed-models", "gemini,codex", "--allowed-machines", "local"])
        return prompts

    @staticmethod
    def prd_with_non_goals():
        from orchestrator import prd
        text = prd.replace_section(prd.template("Word Duel"), "pitch", "A word game for two friends.")
        text = prd.replace_section(text, "features", "- As a player I can start a match\n- As a player I can take a turn")
        return prd.replace_section(text, "not", "- No chat\n- No accounts")

    def test_the_planner_reads_the_product_requirements_first(self):
        prompts = self.plan_with_docs(self.prd_with_non_goals())
        self.assertTrue(prompts)
        self.assertIn("## Product context (source of truth", prompts[0])
        self.assertIn("No chat", prompts[0])
        self.assertLess(prompts[0].index("Product context"), prompts[0].index("Raw input:"))

    def test_the_plan_verifier_checks_the_plan_against_the_requirements(self):
        prompts = self.plan_with_docs(self.prd_with_non_goals())
        verifier = [p for p in prompts if "### GENERATED PLAN ###" in p]
        self.assertTrue(verifier, "the verifier did not run")
        self.assertIn("Reject or flag a plan that builds something under", verifier[0])
        self.assertIn("No chat", verifier[0])

    def plan_json(self, tasks):
        return json.dumps({"title": "Slice Test", "summary": "Summary", "assumptions": [], "constraints": [], "risks": [], "tasks": tasks,
                           "test_cases": [{"title": "AC holds", "type": "unit", "expected": "AC is satisfied", "covers": ["AC"], "tests": ["test_ac"]}]})

    LAYERED = [{"title": "Database schema for matches", "description": "Entities and migrations", "acceptance_criteria": ["AC"], "likely_files": [], "tests": [], "complexity": "low"},
               {"title": "Matches API endpoint", "description": "", "acceptance_criteria": ["AC"], "likely_files": [], "tests": [], "complexity": "low"},
               {"title": "Match screen UI", "description": "", "acceptance_criteria": ["AC"], "likely_files": [], "tests": [], "complexity": "low"}]
    SLICED = [{"title": "Player can start a match from the home screen", "description": "Simplest version end to end", "acceptance_criteria": ["AC"], "likely_files": [], "tests": [], "complexity": "low"},
              {"title": "Player can take a turn", "description": "", "acceptance_criteria": ["AC"], "likely_files": [], "tests": [], "complexity": "low"}]

    def run_planner(self, replies, kind="feature"):
        prompts, queue = [], list(replies)

        def fake_llm(model, prompt, *a, **k):
            prompts.append(prompt)
            return (queue.pop(0) if len(queue) > 1 else queue[0], "mock-model", "sid")

        from orchestrator.scripts import new_job
        with patch("orchestrator.scripts.new_job.run_llm", side_effect=fake_llm), patch("orchestrator.scripts.new_job.create_issue", return_value=131):
            with patch("orchestrator.scripts.new_job.ROOT", self.root), patch("orchestrator.scripts.new_job.make_job_paths", side_effect=self.make_job_paths):
                with patch("sys.stdin", io.StringIO("Add rematch\n\n")):
                    new_job.main([kind, "--branch-mode", "manual", "--no-dispatch", "--summary", "Add rematch", "--allowed-models", "gemini,codex", "--allowed-machines", "local"])
        job_files = list((self.root / ".orchestrator" / "jobs").glob("*.json"))
        return prompts, json.loads(job_files[0].read_text())

    def test_a_layered_feature_plan_is_sent_back_once_and_the_fixed_plan_is_kept(self):
        prompts, job = self.run_planner([self.plan_json(self.LAYERED), self.plan_json(self.SLICED)])
        self.assertEqual(len(prompts), 2)
        self.assertIn("PLAN SHAPE (re-plan requested)", prompts[1])
        self.assertEqual(job["plan"]["tasks"][0]["title"], "Player can start a match from the home screen")
        self.assertNotIn("slice_warnings", job["plan"])

    def test_a_plan_that_stays_layered_is_kept_with_the_warning_visible(self):
        prompts, job = self.run_planner([self.plan_json(self.LAYERED)])
        self.assertEqual(len(prompts), 2)  # one re-plan, no loop
        self.assertTrue(job["plan"]["slice_warnings"])
        self.assertEqual(job["status"], "planned")

    def test_a_replan_keeps_the_jobs_own_models_and_machines(self):
        # Answering a question or accepting the architect's suggestions re-plans with --update. It used to fall back to the
        # paid defaults, so a free-only job quietly started calling paid models.
        from orchestrator.scripts import new_job
        free = "opencode/nemotron-3-ultra-free"
        seen = []

        def fake_llm(model, prompt, *a, **k):
            seen.append((model, k.get("allowed_models")))
            return (self.plan_json(self.SLICED), free, "sid")

        jobs_dir = self.root / ".orchestrator" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        path = jobs_dir / "20260101-000000-feature-9.json"
        path.write_text(json.dumps({"job_id": "20260101-000000-feature-9", "type": "feature-plan", "issue_number": 9, "title": "Add rematch", "status": "human-needed",
                                    "planner": free, "builder": free, "reviewer": free, "allowed_models": [free], "allowed_machines": ["local"], "branch_mode": "manual", "branch": "main"}))
        with patch("orchestrator.scripts.new_job.run_llm", side_effect=fake_llm), patch("orchestrator.scripts.new_job.ROOT", self.root), \
                patch("orchestrator.scripts.new_job.gh_text", return_value=""), patch("orchestrator.scripts.new_job.make_job_paths", side_effect=self.make_job_paths):
            with patch("sys.stdin", io.StringIO("")):
                new_job.main(["feature", "--no-dispatch", "--update", str(path), "--feedback", "answer", "--skip-verify"])
        job = json.loads(path.read_text())
        self.assertEqual((job["planner"], job["builder"], job["reviewer"]), (free, free, free))
        self.assertEqual(job["allowed_models"], [free])
        self.assertEqual(job["allowed_machines"], ["local"])
        self.assertEqual(seen, [(free, [free])])  # one call (verification skipped), and only the allowed model

    def test_a_sliced_plan_costs_no_extra_model_call(self):
        prompts, job = self.run_planner([self.plan_json(self.SLICED)])
        self.assertEqual(len(prompts), 1)
        self.assertNotIn("slice_warnings", job["plan"])

    def test_without_a_written_prd_nothing_is_added(self):
        prompts = self.plan_with_docs("")
        self.assertNotIn("## Product context (source of truth", prompts[0])

    def test_the_feature_planner_is_told_to_build_vertical_slices(self):
        text = (Path(__file__).resolve().parents[1] / "orchestrator" / "prompts" / "planner_feature.md").read_text()
        self.assertIn("Vertical slices, not layers", text)
        self.assertIn("after the FIRST task something real runs end to end", text)
        self.assertIn("Serve the product", text)

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
                "tasks": [{"title": "Task 1", "description": "Desc", "acceptance_criteria": ["AC"], "likely_files": [], "tests": [], "complexity": "low"}],
                "test_cases": [{"title": "AC holds", "type": "unit", "expected": "AC is satisfied", "covers": ["AC"], "tests": ["test_ac"]}]
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
            "manual (no git actions)" # Branch choice
        ]
        mock_confirm.return_value = False # No Stitch, No Spec, No Advanced, No YOLO
        dev_console.handle_new_job(session_allowed_models=["gpt-5.5"], session_allowed_machines=["local"])
        
        # Verify run_script was called with mapped branch mode
        args_passed = mock_run_script.call_args[0][1]
        self.assertIn("--branch-mode", args_passed)
        idx = args_passed.index("--branch-mode")
        self.assertEqual(args_passed[idx+1], "manual")

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


