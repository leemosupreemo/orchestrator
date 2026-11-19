"""Run a fix on a Tests or Design job rewrites its files with what failed; it does not start the production debug loop."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"))
import debug_job


class ScopedDebugTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "job.json"

    def write(self, job):
        self.path.write_text(json.dumps(job))

    def test_a_tests_job_goes_straight_to_the_rewrite_with_what_the_person_said(self):
        self.write({"job_id": "j", "type": "test-audit", "status": "human-needed", "output_scope": "tests-only"})
        with patch.object(debug_job, "run_implement") as implement, patch.object(debug_job, "run_propose") as propose:
            debug_job.run_debug_iteration(self.path, feedback="the CRLF case is the code")
        implement.assert_called_once()
        propose.assert_not_called()  # no separate diagnosis: the rewrite is told which tests failed
        job = json.loads(self.path.read_text())
        self.assertEqual(job["debug_feedback"], "the CRLF case is the code")
        self.assertEqual(job["status"], "human-needed")  # not pushed into the production debug loop

    def test_other_jobs_still_get_the_debug_loop(self):
        self.write({"job_id": "j", "type": "bug-fix", "status": "human-needed", "plan": {}})
        with patch.object(debug_job, "run_propose") as propose, patch.object(debug_job, "run_implement") as implement:
            debug_job.run_debug_iteration(self.path)
        propose.assert_called_once()
        implement.assert_not_called()
        self.assertEqual(propose.call_args.args[0]["status"], "debugging")

    def test_auth_failure_in_propose_records_blocker_and_does_not_burn_iteration(self):
        self.write({
            "job_id": "j-auth",
            "type": "quick-fix",
            "status": "debugging",
            "debug_phase": "propose",
            "reviewer": "claude-sonnet-4-6",
            "iteration": 0,
            "plan": {},
        })
        err = RuntimeError(
            "No model could answer (tried: claude-sonnet-4-6). Last error: Failed to authenticate: "
            "this login reached its time limit and cannot be renewed. Run `claude auth login` to sign in again."
        )
        with patch.object(debug_job, "OUTPUT_DIR", self.path.parent / "output"), \
             patch.object(debug_job, "run_llm", side_effect=err):
            with self.assertRaises(RuntimeError):
                debug_job.run_debug_iteration(self.path)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["iteration"], 0)
        self.assertEqual(saved["status"], "human-needed")
        self.assertEqual(saved["blocker"]["kind"], "sign_in")
        self.assertEqual(saved["blocker"]["provider"], "claude")

    def test_iteration_ledger_tracks_ruled_out_hypotheses_and_filters_tool_files(self):
        job = {
            "iteration": 2,
            "ai_modified_files": [".claude/settings.json", ".orchestrator/jobs/j.json", "src/vegetation.swift"],
            "test_validation": {
                "failures": [{"test": "VegetationTests.testDensity", "message": "expected 0.8 got 0.2"}]
            },
            "debug_history": [
                {
                    "iteration": 1,
                    "model": "claude-sonnet-4-6",
                    "action": "patch",
                    "hypothesis": "Shader alpha clip too low",
                    "result": {"build_ok": True, "tests_ok": False, "notes": "testDensity failed"},
                }
            ],
        }
        ledger = debug_job.build_iteration_ledger(job)
        self.assertEqual(ledger["changed_files"], ["src/vegetation.swift"])
        self.assertEqual(ledger["ruled_out_hypotheses"], ["Shader alpha clip too low"])
        self.assertEqual(ledger["failing_tests"], ["VegetationTests.testDensity: expected 0.8 got 0.2"])
        text = debug_job.format_iteration_ledger(job)
        self.assertIn("Do NOT repeat ruled-out hypotheses", text)
        self.assertIn("Shader alpha clip too low", text)
        self.assertIn("src/vegetation.swift", text)
        self.assertNotIn(".claude/settings.json", text)

    def test_propose_persists_iteration_ledger_and_targeted_tests_and_clears_old_blocker(self):
        self.write({
            "job_id": "j-ledger",
            "type": "bug-fix",
            "status": "debugging",
            "debug_phase": "propose",
            "reviewer": "claude-sonnet-4-6",
            "iteration": 1,
            "blocker": {"kind": "sign_in", "provider": "claude"},
            "ai_modified_files": ["src/app.swift", ".claude/settings.json"],
            "debug_history": [{
                "iteration": 1,
                "model": "claude-sonnet-4-6",
                "action": "patch",
                "hypothesis": "Wrong cache key",
                "result": {"build_ok": True, "tests_ok": False, "notes": "still failing"},
            }],
            "plan": {},
        })
        proposal = json.dumps({
            "evidence": "test failed at line 42",
            "hypothesis": "Stale observer retained after reset",
            "confidence": 0.9,
            "action": "patch",
            "files_changed": ["src/app.swift"],
            "targeted_tests": ["AppTests.testResetObserver"],
            "implementation_plan": "Remove observer in reset()",
            "expected_signal": "AppTests.testResetObserver passes",
            "stop_condition": "Failure persists with observer removed",
            "summary": "Fix retained observer on reset",
        })
        out_dir = self.path.parent / "output"
        with patch.object(debug_job, "OUTPUT_DIR", out_dir), \
             patch.object(debug_job, "run_llm", return_value=(proposal, "claude-sonnet-4-6", "sess-2")), \
             patch.object(debug_job, "run_implement") as implement:
            debug_job.run_debug_iteration(self.path)
        implement.assert_called_once()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["iteration"], 2)
        self.assertNotIn("blocker", saved)
        self.assertEqual(saved["last_blocker"]["provider"], "claude")
        self.assertEqual(saved["targeted_tests"], ["AppTests.testResetObserver"])
        self.assertEqual(saved["iteration_ledger"]["ruled_out_hypotheses"], ["Wrong cache key"])
        prompt_text = (out_dir / "j-ledger" / "iter_2" / "prompt.md").read_text(encoding="utf-8")
        self.assertIn("Do NOT repeat ruled-out hypotheses", prompt_text)
        self.assertIn("Wrong cache key", prompt_text)

    def test_lean_yagni_and_anti_relearning_rules_present_in_prompts(self):
        prompts = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts"
        self.assertIn("Anti-Relearning", (prompts / "debug_agent.md").read_text(encoding="utf-8"))
        self.assertIn("targeted_tests", (prompts / "debug_agent.md").read_text(encoding="utf-8"))
        for fname in ("planner_feature.md", "planner_bug.md", "planner_quick.md"):
            self.assertIn("YAGNI", (prompts / fname).read_text(encoding="utf-8"), fname)


if __name__ == "__main__":
    unittest.main()
