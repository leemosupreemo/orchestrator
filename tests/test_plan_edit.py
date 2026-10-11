from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import plan_edit as P  # noqa: E402


def job(done=(), status="planned"):
    return {"status": status, "completed_task_indices": list(done), "plan": {
        "tasks": [{"title": "A"}, {"title": "B"}, {"title": "C"}, {"title": "D"}],
        "test_cases": [{"id": "1", "task": 1}, {"id": "2", "task": 2}, {"id": "3", "task": 3}, {"id": "4", "task": 4}, {"id": "5", "task": None}]}}


def titles(j):
    return [t["title"] for t in j["plan"]["tasks"]]


def case_tasks(j):
    return {c["id"]: c["task"] for c in j["plan"]["test_cases"]}


class EditTests(unittest.TestCase):
    def test_edit_changes_only_the_given_fields_and_cleans_lists(self):
        j = job()
        P.edit(j, 1, {"title": "  B2 ", "likely_files": "src/a.py\n\n src/b.py ", "acceptance_criteria": ["x", " "]})
        task = j["plan"]["tasks"][1]
        self.assertEqual((task["title"], task["likely_files"], task["acceptance_criteria"]), ("B2", ["src/a.py", "src/b.py"], ["x"]))
        P.edit(j, 1, {"description": "why"})
        self.assertEqual(j["plan"]["tasks"][1]["title"], "B2")

    def test_validation(self):
        j = job()
        for fields in ({"title": " "}, {"title": "x" * 201}, {"description": "x" * 2001}):
            with self.assertRaises(P.PlanEditError):
                P.edit(j, 0, fields)
        for bad in (9, -1, "0", None, True):
            with self.assertRaises(P.PlanEditError):
                P.edit(j, bad, {"title": "x"})

    def test_finished_tasks_and_finished_jobs_are_locked(self):
        j = job(done=[0])
        with self.assertRaises(P.PlanEditError):
            P.edit(j, 0, {"title": "x"})
        for status in P.LOCKED_STATUSES:
            with self.assertRaises(P.PlanEditError):
                P.edit(job(status=status), 1, {"title": "x"})
            with self.assertRaises(P.PlanEditError):
                P.add(job(status=status), {"title": "x"})

    def test_lists_are_capped(self):
        j = job()
        P.edit(j, 0, {"likely_files": [f"f{i}" for i in range(50)]})
        self.assertEqual(len(j["plan"]["tasks"][0]["likely_files"]), P.MAX_LIST)


class AddRemoveMoveTests(unittest.TestCase):
    def test_add_appends_with_defaults(self):
        j = job()
        P.add(j, {"title": "E"})
        self.assertEqual((titles(j)[-1], j["plan"]["tasks"][-1]["complexity"]), ("E", "low"))
        with self.assertRaises(P.PlanEditError):
            P.add(j, {})
        self.assertEqual(len(j["plan"]["tasks"]), 5)

    def test_removing_keeps_completed_indices_and_test_cases_consistent(self):
        j = job(done=[0, 3])
        P.remove(j, 1)  # B
        self.assertEqual(titles(j), ["A", "C", "D"])
        self.assertEqual(j["completed_task_indices"], [0, 2])  # D is still the finished one
        self.assertEqual(case_tasks(j), {"1": 1, "2": None, "3": 2, "4": 3, "5": None})

    def test_move_swaps_tasks_and_their_test_cases(self):
        j = job(done=[0])
        P.move(j, 2, "up")
        self.assertEqual(titles(j), ["A", "C", "B", "D"])
        self.assertEqual(case_tasks(j), {"1": 1, "2": 3, "3": 2, "4": 4, "5": None})
        self.assertEqual(j["completed_task_indices"], [0])

    def test_move_refuses_to_pass_finished_work_or_the_ends(self):
        j = job(done=[0])
        with self.assertRaises(P.PlanEditError):
            P.move(j, 1, "up")
        with self.assertRaises(P.PlanEditError):
            P.move(j, 3, "down")
        with self.assertRaises(P.PlanEditError):
            P.move(j, 1, "sideways")

    def test_completion_recorded_as_digit_strings_is_respected_and_shifted(self):
        j = {"status": "planned", "completed_tasks": ["0", "2"], "plan": {"tasks": [{"title": "A"}, {"title": "B"}, {"title": "C"}]}}
        with self.assertRaises(P.PlanEditError):
            P.edit(j, 0, {"title": "x"})
        P.remove(j, 1)
        self.assertEqual(j["completed_tasks"], ["0", "1"])
        with self.assertRaises(P.PlanEditError):
            P.edit(j, 1, {"title": "x"})

    def test_plain_string_tasks_from_older_plans_can_be_edited(self):
        j = {"status": "planned", "plan": {"tasks": ["Repro", "Fix"]}}
        P.edit(j, 1, {"title": "Fix it", "description": "details"})
        self.assertEqual(j["plan"]["tasks"], [{"title": "Repro"}, {"title": "Fix it", "description": "details"}])

    def test_job_without_a_plan_gets_an_empty_one(self):
        j = {"status": "planned"}
        P.add(j, {"title": "First"})
        self.assertEqual(titles(j), ["First"])


if __name__ == "__main__":
    unittest.main()
