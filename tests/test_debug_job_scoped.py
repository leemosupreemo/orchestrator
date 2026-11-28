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

    def test_manual_run_fix_breaks_paused_loop_deadlock_while_auto_retry_stays_paused(self):
        stuck_job = {
            "job_id": "j-stuck",
            "type": "feature-plan",
            "status": "debugging",
            "debug_phase": "paused",
            "debug_pause_reason": "Loop/Stall detected: Loop detected: Attempt #3 repeated hypothesis from Attempt #1",
            "iteration": 3,
            "plan": {},
            "debug_history": [
                {"iteration": 1, "hypothesis": "Same repeated hypothesis about flowers", "result": {"build_ok": True, "tests_ok": False}},
                {"iteration": 2, "hypothesis": "Different hypothesis about shaders", "result": {"build_ok": True, "tests_ok": False}},
                {"iteration": 3, "hypothesis": "Same repeated hypothesis about flowers", "result": {"build_ok": True, "tests_ok": False}},
            ],
        }
        self.write(stuck_job)
        # Automated call (no manual flag, no feedback) still pauses on the loop
        with patch.object(debug_job, "run_propose") as propose, patch.object(debug_job, "run_shell"):
            debug_job.run_debug_iteration(self.path)
        propose.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())["debug_phase"], "paused")

        # Manual Run fix (as invoked by UI build_debug --manual) breaks the deadlock and resets convergence window
        with patch.object(debug_job, "run_propose") as propose:
            debug_job.run_debug_iteration(self.path, feedback="Increase flower contrast directly", manual=True)
        propose.assert_called_once()
        saved = json.loads(self.path.read_text())
        self.assertNotIn("debug_pause_reason", saved)
        self.assertEqual(saved["debug_convergence_reset_at"], 3)
        self.assertEqual(propose.call_args.args[0]["iteration"], 4)

    def test_run_implement_marks_pending_entry_aborted_on_early_crash(self):
        self.write({
            "job_id": "j-crash",
            "status": "debugging",
            "debug_history": [{"iteration": 1, "hypothesis": "Test", "result": "pending"}],
        })
        with patch.object(debug_job, "run_shell", side_effect=RuntimeError("Not enough disk space")):
            with self.assertRaises(RuntimeError):
                debug_job.run_implement(json.loads(self.path.read_text()), self.path)
        saved = json.loads(self.path.read_text())
        res = saved["debug_history"][-1]["result"]
        self.assertIsInstance(res, dict)
        self.assertTrue(res["aborted"])
        self.assertIn("Not enough disk space", res["notes"])

    def test_ask_human_action_flags_question_for_ui_and_skips_implement(self):
        self.write({
            "job_id": "j-ask",
            "title": "Camelot Hills vegetation",
            "type": "feature-plan",
            "status": "debugging",
            "debug_phase": "propose",
            "reviewer": "claude-sonnet-4-6",
            "iteration": 1,
            "yolo": True,
            "plan": {},
        })
        proposal = json.dumps({
            "evidence": "No paired iPhone or Unity editor available on this computer",
            "hypothesis": "Cannot capture player-height gameplay screenshots on this machine",
            "confidence": 0.95,
            "action": "ask_human",
            "clarification_question": "Should we skip live iPhone captures and verify using the Unity probe only, or wait until an iPhone is connected?",
            "files_changed": [],
            "implementation_plan": "",
            "expected_signal": "User decides verification scope",
            "stop_condition": "",
            "summary": "Need decision on hardware verification",
        })
        out_dir = self.path.parent / "output"
        with patch.object(debug_job, "OUTPUT_DIR", out_dir), \
             patch.object(debug_job, "run_llm", return_value=(proposal, "claude-sonnet-4-6", "sess-ask")), \
             patch.object(debug_job, "run_implement") as implement, \
             patch.object(debug_job, "run_shell") as notify_shell:
            debug_job.run_debug_iteration(self.path)
        implement.assert_not_called()
        notify_shell.assert_called_once()
        self.assertIn("Question for you", notify_shell.call_args.args[0])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["status"], "human-needed")
        self.assertEqual(saved["clarification_asker"], "debugger")
        self.assertIn("skip live iPhone captures", saved["human_clarification_question"])
        self.assertEqual(saved["debug_history"][-1]["action"], "ask_human")
        self.assertTrue(saved["debug_history"][-1]["result"]["aborted"])

        from orchestrator.web import server as web_server
        st = web_server.job_state(saved)
        self.assertEqual(st["label"], "Question for you")
        self.assertIn("debugger needs an answer", st["reason"])

    def test_answering_debugger_question_resumes_debug_loop_and_in_app_notice_wired(self):
        import job_actions
        self.write({
            "job_id": "j-ask-reply",
            "title": "Camelot Hills vegetation",
            "type": "feature-plan",
            "status": "human-needed",
            "clarification_asker": "debugger",
            "human_clarification_question": "Skip iPhone captures?",
            "last_error": "Debugger clarification needed: Skip iPhone captures?",
            "debug_phase": "paused",
            "debug_pause_reason": "Waiting for your answer",
            "debug_history": [{"iteration": 1, "action": "ask_human", "hypothesis": "Missing iPhone", "result": {"aborted": True}}],
            "plan": {},
        })
        with patch.object(job_actions, "run_script", return_value=0) as run_script:
            code = job_actions.answer(self.path, "Yes, verify with Unity probe only.")
        self.assertEqual(code, 0)
        run_script.assert_called_once()
        self.assertEqual(run_script.call_args.args[0], "debug_job.py")
        self.assertIn("--manual", run_script.call_args.args[1])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["status"], "debugging")
        self.assertEqual(saved["debug_phase"], "propose")
        self.assertEqual(saved["clarification_history"][-1]["asker"], "debugger")
        self.assertIsNone(saved["human_clarification_question"])

        # Verify in-app notification & prompt schema wiring
        root = Path(__file__).resolve().parents[1]
        app = (root / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('ev.title === "Question for you"', app)
        self.assertIn('notify("warning", `Question for you — ${ev.body}`', app)
        self.assertIn('label: "Answer question"', app)
        prompt = (root / "orchestrator" / "prompts" / "debug_agent.md").read_text(encoding="utf-8")
        self.assertIn('"ask_human"', prompt)
        self.assertIn('"clarification_question"', prompt)

    def test_answering_debugger_question_restores_question_if_followup_fails(self):
        import job_actions
        self.write({
            "job_id": "j-ask-fail",
            "title": "Camelot Hills vegetation",
            "type": "feature-plan",
            "status": "human-needed",
            "clarification_asker": "debugger",
            "human_clarification_question": "Skip iPhone captures?",
            "last_error": "Debugger clarification needed: Skip iPhone captures?",
            "plan": {},
        })
        with patch.object(job_actions, "run_script", return_value=1):
            code = job_actions.answer(self.path, "Yes")
        self.assertEqual(code, 1)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["status"], "human-needed")
        self.assertEqual(saved["human_clarification_question"], "Skip iPhone captures?")
        self.assertEqual(saved["clarification_asker"], "debugger")




class PausedAttemptTests(unittest.TestCase):
    def test_a_fix_attempt_that_ended_paused_is_not_left_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            for status, expected in (("human-needed", True), ("debugging", False)):
                path.write_text(json.dumps({"status": status, "last_error": "Task 2 paused for clarification",
                                            "debug_history": [{"iteration": 1, "result": "pending"}]}))
                debug_job._close_paused_attempt(path)
                result = json.loads(path.read_text())["debug_history"][-1]["result"]
                with self.subTest(status=status):
                    if expected:
                        self.assertEqual((result["paused"], result["notes"]), (True, "Task 2 paused for clarification"))
                    else:
                        self.assertEqual(result, "pending")  # still running, or the worker records the outcome itself

if __name__ == "__main__":
    unittest.main()
