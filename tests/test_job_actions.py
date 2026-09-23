from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"
for p in (PACKAGE_ROOT, SCRIPTS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import job_actions  # noqa: E402


class JobActionsTests(unittest.TestCase):
    def test_answer_records_clarification_and_replans(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text(json.dumps({"status": "human-needed", "type": "bug-fix",
                                        "human_clarification_question": "Which lobby size?"}))
            with patch("job_actions.subprocess.call", return_value=0) as call:
                self.assertEqual(job_actions.main(["answer", str(path), "--answer", "Four players"]), 0)
            job = json.loads(path.read_text())
            argv = call.call_args[0][0]
        self.assertEqual(job["clarification_history"][-1]["answer"], "Four players")
        self.assertIsNone(job["human_clarification_question"])
        self.assertEqual(argv[2:6], ["bug", "--no-dispatch", "--update", str(path.resolve())])
        self.assertIn("Four players", argv[-1])

    def test_answer_refuses_when_no_question_is_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text(json.dumps({"status": "planned"}))
            with patch("job_actions.subprocess.call") as call:
                self.assertEqual(job_actions.main(["answer", str(path), "--answer", "x"]), 1)
            call.assert_not_called()

    def test_merge_uses_the_console_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text(json.dumps({"status": "review-needed", "pr_number": 141}))
            with patch("dev_console.handle_merge_cleanup") as merge:
                self.assertEqual(job_actions.main(["merge", str(path)]), 0)
        job = merge.call_args[0][0]
        self.assertEqual((job["pr_number"], job["_path"]), (141, path.resolve()))


class ApproveReviseTests(unittest.TestCase):
    def run_action(self, job, *args):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text(json.dumps(job))
            with patch("job_actions.subprocess.call", return_value=0) as call:
                code = job_actions.main([args[0], str(path), *args[1:]])
            return code, json.loads(path.read_text()), (call.call_args[0][0] if call.called else None), path.resolve()

    def test_approve_plan_marks_approved_and_schedules(self):
        code, job, argv, path = self.run_action({"status": "planned", "type": "feature-plan"}, "approve")
        self.assertEqual((code, job["approved"]), (0, True))
        self.assertTrue(argv[1].endswith("schedule_job.py"))
        self.assertEqual(argv[2], str(path))

    def test_approve_design_turns_it_into_a_feature_plan(self):
        design = {"summary": "Rematch", "vibe": "calm", "visual_components": ["Button"], "interaction_flows": ["Tap"]}
        code, job, argv, _ = self.run_action({"status": "designing", "plan": design}, "approve")
        self.assertEqual((job["status"], job["type"], job["design_spec"]), ("planned", "feature-plan", design))
        self.assertIn("APPROVED DESIGN SPEC", job["raw_input"])
        self.assertEqual(argv[2:5], ["feature", "--no-dispatch", "--update"])

    def test_approve_architect_suggestions_replans_with_them(self):
        job = {"status": "human-needed", "type": "bug-fix",
               "verification": {"status": "concerns", "comments": "Add tests", "suggested_additions": ["Unit test"]}}
        _, _, argv, _ = self.run_action(job, "approve")
        self.assertEqual(argv[2], "bug")
        self.assertIn("Add tests", argv[-1])
        self.assertIn("- Unit test", argv[-1])

    def test_approve_refuses_other_states(self):
        code, _, argv, _ = self.run_action({"status": "completed"}, "approve")
        self.assertEqual((code, argv), (1, None))

    def test_revise_builds_the_console_feedback_and_uses_stitch_for_designs(self):
        _, _, argv, _ = self.run_action({"status": "designing", "type": "feature-design"}, "revise",
                                        "--change", "Bigger button", "--where", "Results", "--done-when", "Tappable")
        self.assertEqual(argv[2], "design")
        feedback = argv[argv.index("--feedback") + 1]
        self.assertEqual(feedback, "Requested change: Bigger button\nAffected area: Results\nDone when: Tappable")
        self.assertIn("--stitch", argv)


if __name__ == "__main__":
    unittest.main()
