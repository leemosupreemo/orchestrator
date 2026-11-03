import tempfile
import unittest
from pathlib import Path

from orchestrator import plan_run

RUN = {"id": "r1", "features": ["play", "chat", "lobby", "stats"], "auto_approve": False, "paused": False, "starting": {}}
FEATURES = [
    {"id": "play", "name": "Play a round", "depends_on": [], "status": "planned"},
    {"id": "lobby", "name": "Lobby", "depends_on": [], "status": "planned"},
    {"id": "chat", "name": "Chat", "depends_on": ["play"], "status": "planned"},
    {"id": "stats", "name": "Stats", "depends_on": ["play", "chat"], "status": "planned"},
]


def job(fid, group, label="Working", action=None, run="r1", jid=None):
    return {"id": jid or f"job-{fid}", "feature": fid, "plan_run": run, "updated": 1,
            "state": {"group": group, "label": label, "next": {"action": action} if action else None}}


def run_with(**changes):
    return {**RUN, **changes, "starting": changes.get("starting", {})}


class DecideTests(unittest.TestCase):
    def decide(self, run=None, jobs=(), slots=4, **kw):
        return plan_run.decide(run or run_with(), FEATURES, list(jobs), slots, **kw)

    def rows(self, result):
        return {r["feature"]: r for r in result["rows"]}

    def test_independent_features_start_together_and_dependents_wait(self):
        result = self.decide()
        self.assertEqual(result["start"], ["play", "lobby"])
        rows = self.rows(result)
        self.assertEqual(rows["chat"]["state"], "waiting")
        self.assertIn("Play a round", rows["chat"]["detail"])
        self.assertFalse(result["finished"])

    def test_starts_are_limited_by_free_machines(self):
        self.assertEqual(self.decide(slots=1)["start"], ["play"])
        busy = self.decide(jobs=[job("play", "working")], slots=1)
        self.assertEqual(busy["start"], [])
        self.assertEqual(self.rows(busy)["lobby"]["detail"], "Ready: waiting for a free machine")

    def test_a_planning_run_counts_as_started_so_nothing_starts_twice(self):
        run = run_with(starting={"play": {"at": 100}})
        result = self.decide(run=run, now=200)
        self.assertNotIn("play", result["start"])
        self.assertEqual(self.rows(result)["play"]["state"], "planning")

    def test_planning_that_died_or_stalled_holds_back_its_dependents(self):
        run = run_with(starting={"play": {"at": 100}})
        died = self.decide(run=run, starting_alive={"play": False}, now=200)
        self.assertEqual(self.rows(died)["play"]["state"], "failed_start")
        self.assertIn("needs attention", self.rows(died)["chat"]["detail"])
        stalled = self.decide(run=run, now=100 + plan_run.STARTING_TIMEOUT + 1)
        self.assertEqual(self.rows(stalled)["play"]["state"], "failed_start")

    def test_finished_dependencies_release_the_next_layer(self):
        result = self.decide(jobs=[job("play", "done"), job("lobby", "working")])
        self.assertEqual(result["start"], ["chat"])
        self.assertEqual(self.rows(result)["stats"]["state"], "waiting")  # still needs chat

    def test_a_job_needing_attention_holds_back_only_its_dependents(self):
        result = self.decide(jobs=[job("play", "needs_you", "Tests failing")])
        rows = self.rows(result)
        self.assertEqual(rows["play"]["state"], "attention")
        self.assertEqual(rows["play"]["detail"], "Tests failing")
        self.assertIn("needs attention", rows["chat"]["detail"])
        self.assertIn("lobby", result["start"])  # unrelated work carries on

    def test_auto_approve_only_approves_plans_ready_for_approval(self):
        jobs = [job("play", "needs_you", "Approve plan", "approve"), job("lobby", "needs_you", "Review suggestions", "approve")]
        self.assertEqual(self.decide(jobs=jobs)["approve"], [])
        self.assertEqual(self.decide(run=run_with(auto_approve=True), jobs=jobs)["approve"], ["job-play"])

    def test_paused_starts_nothing(self):
        result = self.decide(run=run_with(paused=True))
        self.assertEqual(result["start"], [])
        self.assertEqual(self.rows(result)["play"]["detail"], "Paused")

    def test_a_view_starts_and_approves_nothing_but_shows_the_same_rows(self):
        run = run_with(auto_approve=True)
        jobs = [job("lobby", "needs_you", "Approve plan", "approve")]
        view = self.decide(run=run, jobs=jobs, can_start=False)
        self.assertEqual((view["start"], view["approve"]), ([], []))
        self.assertEqual(self.rows(view)["play"]["state"], "ready")
        self.assertEqual(self.rows(view)["play"]["detail"], "Ready")
        self.assertEqual(self.rows(view)["lobby"]["detail"], "Approve plan")

    def test_jobs_from_another_run_or_unrelated_do_not_count(self):
        result = self.decide(jobs=[job("play", "done", run="old"), {"id": "x", "feature": "play", "state": {"group": "done"}}])
        self.assertIn("play", result["start"])

    def test_dependency_outside_the_run_counts_once_complete(self):
        features = FEATURES + [{"id": "accounts", "name": "Accounts", "depends_on": [], "status": "in-progress"}]
        features[0] = {**features[0], "depends_on": ["accounts"]}
        waiting = plan_run.decide(run_with(), features, [], 4)
        self.assertNotIn("play", waiting["start"])
        features[-1] = {**features[-1], "status": "complete"}
        self.assertIn("play", plan_run.decide(run_with(), features, [], 4)["start"])

    def test_finished_when_every_feature_is_done(self):
        jobs = [job(f["id"], "done") for f in FEATURES]
        self.assertTrue(self.decide(jobs=jobs)["finished"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_keeps_known_features_and_refuses_a_second_run(self):
        run = plan_run.start(self.runtime, ["play", "nope", "play"], FEATURES, auto_approve=True)
        self.assertEqual(run["features"], ["play"])
        self.assertEqual(plan_run.load(self.runtime)["id"], run["id"])
        with self.assertRaises(plan_run.PlanRunError):
            plan_run.start(self.runtime, ["lobby"], FEATURES, auto_approve=False)
        with self.assertRaises(plan_run.PlanRunError):
            plan_run.start(Path(self.tmp.name) / "other", ["nope"], FEATURES, auto_approve=False)
        plan_run.save(self.runtime, {**run, "finished": True})  # a finished run can be replaced
        self.assertEqual(plan_run.start(self.runtime, ["lobby"], FEATURES, auto_approve=False)["features"], ["lobby"])
        plan_run.clear(self.runtime)  # Stop
        self.assertIsNone(plan_run.load(self.runtime))

    def test_capacity_counts_enabled_machines(self):
        self.assertEqual(plan_run.capacity([]), 1)
        self.assertEqual(plan_run.capacity([{"max_concurrent_jobs": 2}, {"max_concurrent_jobs": 3, "enabled": False}, {}]), 3)


if __name__ == "__main__":
    unittest.main()
