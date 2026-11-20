"""A Quick change gets a fast-tracked plan and test cases from one planner call, and never stops for want of one."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import new_job  # noqa: E402

PLAN = {
    "summary": "Wind gusts and slope-aware grass placement.",
    "assumptions": ["'Realistic' means motion and placement, not new art."],
    "likely_files": ["Assets/Scripts/World/GrassSystem.cs"],
    "acceptance_criteria": ["Trees sway out of phase"],
    "tasks": [{"title": "Per-tree wind phase", "description": "Hash position into the shader"},
              {"title": "Slope attenuation"}, {"title": "Third"}, {"title": "Fourth is dropped"}],
    "test_cases": [{"title": "Trees out of phase", "type": "functionality", "steps": ["Render two trees"],
                    "expected": "Different offsets", "covers": ["Trees sway out of phase"], "tests": ["test_wind"]}],
}


class FastTrackTests(unittest.TestCase):
    def fast(self, reply=None, error=None):
        def run_llm(*args, **kwargs):
            if error:
                raise error
            return f"```json\n{json.dumps(reply)}\n```", "claude-sonnet-4-6", "sid-1"
        with patch.object(new_job, "run_llm", side_effect=run_llm):
            return new_job.fast_track_quick_plan("Make the vegetation look more realistic", planner="auto", allowed_models=None)

    def test_plan_and_test_cases_come_back_capped_at_three_tasks(self):
        fields, used, sessions, note = self.fast(PLAN)
        self.assertEqual(len(fields["tasks"]), 3)
        self.assertTrue(fields["fast_tracked"])
        self.assertEqual(fields["test_cases"][0]["title"], "Trees out of phase")
        self.assertEqual(fields["plan_summary"], PLAN["summary"])
        self.assertEqual((used, sessions, note), ("claude-sonnet-4-6", [{"id": "sid-1", "model": "claude-sonnet-4-6"}], ""))

    def test_a_failed_call_falls_back_to_the_instruction(self):
        fields, used, sessions, note = self.fast(error=RuntimeError("no model"))
        self.assertIsNone(fields)
        self.assertEqual(used, "user-prompt")
        self.assertIn("instruction alone", note)

    def test_no_tasks_falls_back_too(self):
        fields, _, _, note = self.fast({"summary": "x", "tasks": []})
        self.assertIsNone(fields)
        self.assertIn("no tasks", note)

    def test_brief_lists_the_fast_plan(self):
        plan = {"summary": "s", "instructions": "do it", **{k: PLAN[k] for k in ("tasks", "acceptance_criteria", "assumptions")},
                "fast_tracked": True}
        body = new_job.build_quick_issue_body(plan)
        self.assertIn("## Fast-tracked plan\n1. **Per-tree wind phase**: Hash position into the shader", body)
        self.assertIn("## Acceptance Criteria\n- Trees sway out of phase", body)
        self.assertNotIn("Fast-tracked", new_job.build_quick_issue_body({"summary": "s", "instructions": "do it"}))


if __name__ == "__main__":
    unittest.main()
