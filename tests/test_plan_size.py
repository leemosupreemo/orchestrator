"""How big a plan is allowed to be before it has to be split (orchestrator/plan_size.py)."""
from __future__ import annotations

import unittest

from orchestrator import plan_size


def task(*files):
    return {"title": "t", "likely_files": list(files)}


class PlanSizeTests(unittest.TestCase):
    def test_a_few_tasks_on_a_few_files_is_small(self):
        size = plan_size.measure({"tasks": [task("a.swift", "b.swift"), task("b.swift"), task("c.swift")]})
        self.assertEqual((size["label"], size["tasks"], size["files"], size["too_large"]), ("Small", 3, 3, False))

    def test_files_are_counted_once_however_many_tasks_touch_them(self):
        self.assertEqual(plan_size.measure({"tasks": [task("a"), task("a"), task("a")]})["files"], 1)

    def test_up_to_the_budget_is_medium_and_past_it_is_too_large(self):
        medium = plan_size.measure({"tasks": [task("a")] * plan_size.MAX_TASKS})
        self.assertEqual((medium["label"], medium["too_large"]), ("Medium", False))
        many_tasks = plan_size.measure({"tasks": [task("a")] * (plan_size.MAX_TASKS + 1)})
        self.assertTrue(many_tasks["too_large"])
        self.assertEqual(many_tasks["label"], "Too large")
        self.assertIn(f"at most {plan_size.MAX_TASKS}", many_tasks["reasons"][0])
        many_files = plan_size.measure({"tasks": [task(*[f"f{i}.py" for i in range(plan_size.MAX_FILES + 1)])]})
        self.assertTrue(many_files["too_large"])
        self.assertIn(f"at most {plan_size.MAX_FILES}", many_files["reasons"][0])

    def test_both_limits_give_both_reasons(self):
        size = plan_size.measure({"tasks": [task(*[f"f{i}" for i in range(20)])] * 7})
        self.assertEqual(len(size["reasons"]), 2)

    def test_a_plan_that_lists_no_files_is_judged_on_its_tasks_alone(self):
        size = plan_size.measure({"tasks": [{"title": "t"}, {"title": "u"}]})
        self.assertEqual((size["label"], size["files"]), ("Small", 0))

    def test_odd_input_never_raises(self):
        for plan in (None, {}, {"tasks": "nope"}, {"tasks": [None, "x", {"likely_files": "a.py"}]}, {"deferred": "x"}):
            plan_size.measure(plan)

    def test_whatever_the_planner_left_out_is_carried_for_the_page(self):
        size = plan_size.measure({"tasks": [task("a")], "deferred": [" Add a high-score table ", "", "Sound effects"]})
        self.assertEqual(size["deferred"], ["Add a high-score table", "Sound effects"])

    def test_the_replan_request_states_the_budget_and_the_reasons(self):
        text = plan_size.replan_request(["It has 7 tasks; one job should have at most 5."])
        self.assertIn(f"at most {plan_size.MAX_TASKS} tasks", text)
        self.assertIn("`deferred`", text)
        self.assertIn("It has 7 tasks", text)


if __name__ == "__main__":
    unittest.main()
