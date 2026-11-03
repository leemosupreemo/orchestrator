"""When a finished task has outgrown its plan enough to stop and ask (orchestrator/task_growth.py)."""
from __future__ import annotations

import unittest

from orchestrator import scope_check, task_growth


def change(path, added=10, status="M"):
    return {"path": path, "status": status, "added": added, "deleted": 0}


TASK = {"title": "Collisions", "likely_files": ["app/game/Sources/GameEngine.swift", "app/game/Tests/CollisionTests.swift"]}


class TaskGrowthTests(unittest.TestCase):
    def test_a_task_that_stayed_inside_its_plan_is_not_stopped(self):
        changed = [change("app/game/Sources/GameEngine.swift", 120), change("app/game/Tests/CollisionTests.swift", 90, "A")]
        self.assertIsNone(task_growth.check(TASK, changed))

    def test_running_a_little_over_is_allowed(self):
        limit = scope_check.LINES_PER_TASK * task_growth.GROWTH_FACTOR
        self.assertIsNone(task_growth.check(TASK, [change("app/game/Sources/GameEngine.swift", limit)]))
        grown = task_growth.check(TASK, [change("app/game/Sources/GameEngine.swift", limit + 1)])
        self.assertIn(f"added {limit + 1} lines", grown["reasons"][0])

    def test_a_few_unplanned_files_are_fine_more_than_that_is_not(self):
        few = [change(f"app/extra{i}.swift", 5, "A") for i in range(task_growth.MAX_UNPLANNED)]
        self.assertIsNone(task_growth.check(TASK, few))
        many = [change(f"app/extra{i}.swift", 5, "A") for i in range(task_growth.MAX_UNPLANNED + 1)]
        grown = task_growth.check(TASK, many)
        self.assertEqual(grown["reasons"], [f"touched {len(many)} files the plan didn't mention"])
        self.assertEqual(len(grown["files"]), len(many))

    def test_changing_dependencies_the_plan_didnt_mention_always_stops_it(self):
        grown = task_growth.check(TASK, [change("Package.swift", 3)])
        self.assertIn("changed dependencies or build settings (Package.swift)", grown["reasons"][0])

    def test_a_dependency_file_the_plan_names_is_planned(self):
        task = {"title": "Bootstrap", "likely_files": ["Package.swift"]}
        self.assertIsNone(task_growth.check(task, [change("Package.swift", 20, "A")]))

    def test_lockfiles_and_runtime_files_do_not_count(self):
        changed = [change("Package.resolved", 900, "A"), change(".orchestrator/output/x/brief.md", 500, "A")]
        self.assertIsNone(task_growth.check(TASK, changed))

    def test_a_task_that_lists_no_files_is_judged_on_size_and_dependencies_alone(self):
        self.assertIsNone(task_growth.check({"title": "t"}, [change("a.swift", 50), change("b.swift", 50)]))
        self.assertIsNotNone(task_growth.check({"title": "t"}, [change("a.swift", 900)]))

    def test_odd_input_never_raises(self):
        for task in (None, {}, {"likely_files": "a.py"}, {"likely_files": [None, ""]}):
            task_growth.check(task, [])

    def test_the_sentence_names_the_task_and_every_reason(self):
        one = task_growth.sentence(1, ["added 950 lines (about 400 is typical)"])
        self.assertEqual(one, "Task 2 added 950 lines (about 400 is typical).")
        three = task_growth.sentence(0, ["a", "b", "c"])
        self.assertEqual(three, "Task 1 a, b and c.")


if __name__ == "__main__":
    unittest.main()
