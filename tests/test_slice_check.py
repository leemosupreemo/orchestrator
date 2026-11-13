from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import slice_check as S  # noqa: E402


def task(title, description="", criteria=("Works",)):
    return {"title": title, "description": description, "acceptance_criteria": list(criteria)}


class SliceTests(unittest.TestCase):
    def test_a_vertical_plan_is_fine(self):
        plan = {"tasks": [task("Player can start a match from the home screen", "Simplest version: a button that creates a match and shows it"),
                          task("Player can take a turn", "Adds the turn form and saves it"),
                          task("Show the score after each turn")]}
        self.assertEqual(S.problems(plan), [])

    def test_a_single_task_plan_is_its_own_slice(self):
        self.assertEqual(S.problems({"tasks": [task("Create database schema")]}), [])
        self.assertEqual(S.problems({"tasks": []}), [])

    def test_a_first_task_that_only_sets_up_foundations_is_flagged(self):
        plan = {"tasks": [task("Create the data models and database schema", "Entities and migrations"), task("Add the match screen")]}
        found = S.problems(plan)
        self.assertEqual(len(found), 1)
        self.assertIn("only prepares foundations", found[0])

    def test_foundation_work_that_also_delivers_something_visible_is_fine(self):
        plan = {"tasks": [task("Add the match model and show matches on the home screen", "Persist matches; the screen lists them"), task("Add rematch")]}
        self.assertEqual(S.problems(plan), [])

    def test_words_are_matched_whole_so_overview_is_not_view(self):
        plan = {"tasks": [task("Set up project overview and types", "interfaces only"), task("Next")]}
        self.assertTrue(any("foundations" in p for p in S.problems(plan)))  # 'overview' must not count as the user-facing word 'view'

    def test_a_plan_split_into_layers_is_flagged(self):
        plan = {"tasks": [task("Database schema for matches"), task("Matches API endpoint"), task("Match screen UI")]}
        found = S.problems(plan)
        self.assertTrue(any("split by layer" in p for p in found))

    def test_tasks_that_span_layers_are_not_layer_splits(self):
        plan = {"tasks": [task("Start a match: schema, API and screen"), task("Take a turn: API and screen"), task("Scores: schema and screen")]}
        self.assertEqual([p for p in S.problems(plan) if "split by layer" in p], [])

    def test_a_first_task_without_acceptance_criteria_is_flagged(self):
        plan = {"tasks": [task("Player can start a match", criteria=()), task("Player can take a turn")]}
        self.assertEqual(len(S.problems(plan)), 1)
        self.assertIn("no acceptance criteria", S.problems(plan)[0])

    def test_string_tasks_from_older_plans_do_not_crash(self):
        found = S.problems({"tasks": ["Build the schema", "Build the screen"]})  # only titles to go on, but it must not raise
        self.assertTrue(any("foundations" in p for p in found))
        self.assertEqual(S.problems({"tasks": ["Add the match screen", "Add scores"]}), [])

    def test_the_replan_request_lists_what_to_fix(self):
        text = S.replan_request(["The first task only prepares foundations."])
        self.assertIn("vertical slices", text)
        self.assertIn("- The first task only prepares foundations.", text)
        self.assertIn("FIRST task", text)


if __name__ == "__main__":
    unittest.main()
