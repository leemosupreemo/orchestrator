from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import inbox  # noqa: E402

ME = {"name": "Demo", "root": "/p/demo"}


def job(jid, group="needs_you", tone="attention", updated=1, **extra):
    return {"id": jid, "title": jid.upper(), "updated": updated, "state": {"group": group, "tone": tone, "label": "L", "reason": "R", "next": {"action": "approve", "label": "Go"}}, **extra}


class InboxTests(unittest.TestCase):
    def test_only_jobs_waiting_on_you_and_not_mid_run_are_listed(self):
        out = inbox.build(ME, [job("a"), job("b", "working", "working"), job("c", "done", "done"), job("d", active_run=True)], [])
        self.assertEqual([i["job_id"] for i in out["here"]], ["a"])
        self.assertEqual(out["count"], 1)

    def test_failures_rank_above_decisions_then_newest_first(self):
        out = inbox.build(ME, [job("old", updated=1), job("new", updated=9), job("red", tone="failed", updated=0)], [])
        self.assertEqual([i["job_id"] for i in out["here"]], ["red", "new", "old"])

    def test_waiting_runs_are_included_and_running_ones_are_not(self):
        runs = [{"id": "r1", "running": True, "waiting": True, "title": "Plan", "last_line": "Continue? (y/n)", "started": 5},
                {"id": "r2", "running": True, "waiting": False}, {"id": "r3", "running": False, "waiting": True}]
        out = inbox.build(ME, [], runs)
        self.assertEqual([(i["kind"], i["run_id"], i["reason"]) for i in out["here"]], [("run", "r1", "Continue? (y/n)")])

    def test_other_projects_are_listed_after_and_not_counted(self):
        others = [{"name": "Zed", "root": "/p/zed", "jobs": [job("z1")]}, {"name": "Abe", "root": "/p/abe", "jobs": [job("a1"), job("a2", "done", "done")]}]
        out = inbox.build(ME, [job("h")], [], others)
        self.assertEqual(out["count"], 1)
        self.assertEqual([(i["project"]["name"], i["job_id"]) for i in out["elsewhere"]], [("Abe", "a1"), ("Zed", "z1")])

    def test_one_projects_flood_is_capped(self):
        flood = [job(f"j{i}", updated=i) for i in range(inbox.MAX_OTHER_PER_PROJECT + 5)]
        out = inbox.build(ME, [], [], [{"name": "Big", "root": "/p/big", "jobs": flood}])
        self.assertEqual(len(out["elsewhere"]), inbox.MAX_OTHER_PER_PROJECT)

    def test_a_reminder_is_waiting_on_you_and_counts(self):
        rem = {"id": "product-review", "title": "Review the product", "label": "Review due", "reason": "No review yet.", "href": "#/product"}
        out = inbox.build(ME, [job("a")], [], None, [rem])
        self.assertEqual(out["count"], 2)
        item = next(i for i in out["here"] if i["kind"] == "reminder")
        self.assertEqual((item["id"], item["href"], item["tone"], item["next"]), ("reminder:product-review", "#/product", "attention", None))

    def test_failures_still_come_before_reminders(self):
        rem = {"id": "r", "title": "t", "label": "l", "reason": "x", "href": "#/product"}
        out = inbox.build(ME, [job("red", tone="failed")], [], None, [rem])
        self.assertEqual([i["kind"] for i in out["here"]], ["job", "reminder"])

    def test_empty(self):
        self.assertEqual(inbox.build(ME, [], []), {"here": [], "elsewhere": [], "count": 0})


if __name__ == "__main__":
    unittest.main()
