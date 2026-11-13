from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import plan_edit  # noqa: E402
from orchestrator.scripts import test_cases as tc  # noqa: E402

EIGHT = ("functionality", "user-interface", "performance", "integration", "usability", "database", "security", "user-acceptance")


class CategoryTests(unittest.TestCase):
    def test_there_are_the_eight_standard_categories(self):
        self.assertEqual(tc.CASE_TYPES, EIGHT)
        self.assertEqual(set(tc.CASE_TYPE_LABELS), set(EIGHT))

    def test_older_and_loose_names_map_onto_them(self):
        cases = {"unit": "functionality", "UI": "user-interface", "manual": "user-acceptance", "e2e": "integration",
                 "Security Test Cases": "security", "user interface": "user-interface", "user_acceptance": "user-acceptance",
                 "db": "database", "": "functionality", "nonsense": "functionality", None: "functionality"}
        for raw, expected in cases.items():
            self.assertEqual(tc.normalize_type(raw), expected, raw)

    def test_only_usability_and_user_acceptance_are_checked_by_hand(self):
        self.assertEqual({t for t in EIGHT if tc.is_manual({"type": t})}, {"usability", "user-acceptance"})
        self.assertTrue(tc.is_manual({"type": "manual"}))
        self.assertFalse(tc.is_manual({"type": "unit"}))

    def test_planner_output_is_normalized_and_manual_cases_carry_no_tests(self):
        cases = tc.normalize_cases([
            {"title": "Log in", "type": "Functionality", "expected": "Dashboard loads", "tests": ["test_login"]},
            {"title": "Signs off", "type": "user-acceptance", "expected": "ok", "tests": ["x"]},
            {"title": "Odd", "type": "weird", "expected": "ok"},
        ], 7)
        self.assertEqual([c["type"] for c in cases], ["functionality", "user-acceptance", "functionality"])
        self.assertEqual(cases[1]["tests"], [])

    def test_plan_needs_an_automated_case_not_just_hand_checked_ones(self):
        plan = {"test_cases": [{"title": "t", "type": "usability", "expected": "e"}]}
        self.assertTrue(any("automated" in p for p in tc.plan_problems(plan)))

    def test_the_planner_is_told_the_categories_and_the_four_fields(self):
        for text in (tc.PLANNER_INSTRUCTIONS, tc.TEST_CASE_SCHEMA):
            for category in EIGHT:
                self.assertIn(category, text)
        for field in ("title", "preconditions", "steps", "expected"):
            self.assertIn(f'"{field}"', tc.TEST_CASE_SCHEMA)

    def test_plan_editor_accepts_the_categories(self):
        job = {"status": "planned", "plan": {"tasks": [{"title": "A"}], "test_cases": []}}
        added = plan_edit.add_case(job, {"title": "Reject weak password", "type": "security", "expected": "Rejected",
                                         "preconditions": "Signup open", "steps": "Enter 'abc'\nSubmit"}, issue_number=3)
        self.assertEqual(added["type"], "security")
        self.assertEqual(added["steps"], ["Enter 'abc'", "Submit"])
        edited = plan_edit.edit_case(job, added["id"], {"type": "usability"})
        self.assertEqual((edited["type"], edited["tests"]), ("usability", []))


if __name__ == "__main__":
    unittest.main()
