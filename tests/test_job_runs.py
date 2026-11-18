"""A job and its runs: labelled by what each run did, all listed on the job, and adding up to what the job page shows."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from orchestrator import job_runs as jr

ROOT = Path(__file__).resolve().parents[1]


def live(rid, action, started, *, job="job-1", running=False, exit_code=0, now="", stopped=False, result=None, ended=None):
    return {"id": rid, "action": action, "title": action, "job": job, "result_job": None, "started": started,
            "ended": None if running else (ended or started + 30), "running": running, "exit_code": None if running else exit_code,
            "stopped": stopped, "waiting": False, "now": now, "result": result}


class KindTests(unittest.TestCase):
    def test_each_action_names_what_the_run_did_and_its_stage(self):
        self.assertEqual(jr.kind("new_job"), ("Planning", "plan"))
        self.assertEqual(jr.kind("execute"), ("Build", "build"))
        self.assertEqual(jr.kind("debug"), ("Fix attempt", "verify"))
        self.assertEqual(jr.kind("merge"), ("Merge", "finalize"))
        self.assertEqual(jr.kind("export_job"), ("Export", None))  # a side errand, not work on the job
        self.assertEqual(jr.kind("something_new"), ("Something new", None))

    def test_statuses(self):
        self.assertEqual(jr.status({"running": True}), "running")
        self.assertEqual(jr.status({"running": True, "waiting": True}), "waiting")
        self.assertEqual(jr.status({"stopped": True, "exit_code": -15}), "stopped")
        self.assertEqual(jr.status({"exit_code": 0}), "succeeded")
        self.assertEqual(jr.status({"exit_code": 1}), "failed")
        self.assertEqual(jr.status({"exit_code": None}), "unknown")


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name) / ".orchestrator"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_finished_run_is_kept_beside_its_log_with_its_job(self):
        path = jr.record(self.rt, live("r1", "debug", 100.0, exit_code=1, result={"headline": "Tests still fail", "tone": "failed"}))
        self.assertEqual(path, self.rt / "logs" / "ui" / "r1.json")
        saved = jr.recorded(self.rt, "job-1")[0]
        self.assertEqual((saved["job"], saved["exit_code"], saved["headline"], saved["log"]), ("job-1", 1, "Tests still fail", "logs/ui/r1-debug.log"))
        self.assertEqual(jr.recorded(self.rt, "other"), [])

    def test_runs_for_no_job_are_not_recorded_and_bad_files_are_skipped(self):
        self.assertIsNone(jr.record(self.rt, live("r2", "check", 1.0, job=None)))
        (self.rt / "logs" / "ui").mkdir(parents=True, exist_ok=True)
        (self.rt / "logs" / "ui" / "broken.json").write_text("{nope", encoding="utf-8")
        (self.rt / "logs" / "ui" / "list.json").write_text("[]", encoding="utf-8")
        self.assertEqual(jr.recorded(self.rt), [])

    def test_a_planning_run_counts_for_the_job_it_created(self):
        run = {**live("r3", "new_job", 1.0, job=None), "result_job": "job-9"}
        jr.record(self.rt, run)
        self.assertEqual(jr.recorded(self.rt, "job-9")[0]["id"], "r3")


class ForJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name) / ".orchestrator"

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_and_recorded_runs_merge_newest_first_with_numbered_repeats(self):
        jr.record(self.rt, live("a-plan", "new_job", 100.0))
        jr.record(self.rt, live("b-build", "execute", 200.0, exit_code=1))
        jr.record(self.rt, live("c-fix", "debug", 300.0, exit_code=1, result={"headline": "Still failing"}))
        runs = jr.for_job(self.rt, "job-1", [live("c-fix", "debug", 300.0, exit_code=1), live("d-fix", "debug", 400.0, running=True, now="Running tests"),
                                             live("e-export", "export_job", 410.0), live("x", "debug", 500.0, job="someone-else")], now=460.0)
        self.assertEqual([r["label"] for r in runs], ["Export", "Fix attempt 2", "Fix attempt 1", "Build", "Planning"])
        self.assertEqual([r["status"] for r in runs], ["succeeded", "running", "failed", "failed", "succeeded"])
        self.assertEqual(runs[1]["seconds"], 60)  # still running: counted up to now
        self.assertTrue(runs[0]["side"])
        self.assertEqual(runs[2]["headline"], "Still failing")  # the record's headline survives when the live copy has none
        self.assertTrue(runs[1]["live"] and not runs[3]["live"])

    def test_a_kind_that_happened_once_is_not_numbered(self):
        runs = jr.for_job(self.rt, "job-1", [live("1", "execute", 1.0), live("2", "debug", 2.0)])
        self.assertEqual([r["label"] for r in runs], ["Fix attempt", "Build"])


class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name) / ".orchestrator"

    def tearDown(self):
        self.tmp.cleanup()

    def rel(self, runs, job=None):
        return jr.relationship(jr.for_job(self.rt, "job-1", runs, now=1000.0), job or {})

    def test_a_small_job_done_in_one_run_is_one_to_one(self):
        r = self.rel([live("1", "execute", 100.0), live("2", "web_preview", 200.0)])
        self.assertTrue(r["single"])
        self.assertEqual((r["count"], r["per_phase"]["build"], r["active"], r["unreported"]), (1, 1, None, None))
        self.assertTrue(self.rel([])["single"])

    def test_several_runs_count_per_stage_and_the_working_one_bubbles_up(self):
        r = self.rel([live("1", "new_job", 100.0), live("2", "execute", 200.0), live("3", "debug", 300.0),
                      live("4", "debug", 900.0, running=True, now="Running tests")])
        self.assertFalse(r["single"])
        self.assertEqual(r["per_phase"], {"plan": 1, "build": 1, "verify": 2, "document": 0, "finalize": 0})
        self.assertEqual((r["active"]["label"], r["active"]["phase"], r["active"]["now"], r["active"]["seconds"]),
                         ("Fix attempt 2", "verify", "Running tests", 100))
        self.assertIsNone(r["unreported"])  # nothing to report while a run is working

    def test_a_failed_run_the_job_record_does_not_reflect_is_brought_up(self):
        runs = [live("1", "execute", 100.0), live("2", "execute", 500.0, exit_code=1)]
        r = self.rel(runs, {"updated_at": "1970-01-01T00:05:00+00:00"})  # saved at 300s, the run ended at 530s
        self.assertEqual((r["unreported"]["id"], r["unreported"]["label"], r["unreported"]["status"]), ("2", "Build 2", "failed"))
        later = self.rel(runs, {"updated_at": "1970-01-01T00:10:00+00:00"})  # the worker saved the job after the run
        self.assertIsNone(later["unreported"])
        self.assertEqual(self.rel(runs, {"updated_at": "not a date"})["unreported"]["id"], "2")  # an unreadable date can't hide a failure

    def test_a_run_the_app_never_saw_end_counts_as_unreported(self):
        r = self.rel([{**live("1", "execute", 100.0), "exit_code": None}])
        self.assertEqual(r["unreported"]["status"], "unknown")


class SessionTests(unittest.TestCase):
    def test_a_real_run_records_itself_for_its_job_when_it_ends(self):
        from orchestrator.web.server import PtySession, runtime_dir
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = PtySession("rec-test", "debug", "Run fix", [sys.executable, "-c", "print('[1/2] Reading the failure'); print('[2/2] Patching')"], root, {})
            session.job_id = "job-7"
            deadline = time.monotonic() + 8
            offset = 0
            while time.monotonic() < deadline and not (runtime_dir(root) / "logs" / "ui" / "rec-test.json").exists():
                offset, _, _ = session.read(offset, 0.1)
            saved = jr.recorded(runtime_dir(root), "job-7")
            self.assertEqual([(r["id"], r["action"], r["exit_code"]) for r in saved], [("rec-test", "debug", 0)])
            self.assertTrue(session.now())  # the last step's title, for the job page

    def test_saved_runs_say_which_job_they_were_for(self):
        from orchestrator.web import server as ui
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = ui.runtime_dir(root) / "logs" / "ui"
            logs.mkdir(parents=True)
            (logs / "20261115-101500-abc123-debug.log").write_text("[exited with code 1]\n", encoding="utf-8")
            (logs / "20261115-101600-def456-check.log").write_text("[exited]\n", encoding="utf-8")
            jr.record(ui.runtime_dir(root), live("20261115-101500-abc123", "debug", 1.0, exit_code=1))
            history = {r["id"]: r for r in ui.run_history(root)}
            self.assertEqual((history["20261115-101500-abc123"]["job"], history["20261115-101500-abc123"]["exit_code"]), ("job-1", 1))
            self.assertIsNone(history["20261115-101600-def456"]["job"])


class PageTests(unittest.TestCase):
    def setUp(self):
        self.app = (ROOT / "orchestrator/web/static/app.js").read_text(encoding="utf-8")
        self.server = (ROOT / "orchestrator/web/server.py").read_text(encoding="utf-8")

    def test_the_job_page_lists_every_run_and_labels_the_live_one(self):
        self.assertIn("${jobRunsCard(jobRuns, runRel, ", self.app)
        self.assertIn("activeRunCard(r, (jobRuns || []).find((x) => x.id === r.id)", self.app)
        self.assertIn("· run in progress", self.app)
        self.assertIn("This job ran in one go", self.app)  # 1:1
        self.assertIn('detail.update(self._job_runs_view(root, parts[1], detail["runs"], detail["job"]))', self.server)

    def test_runs_bubble_up_into_the_job_stages_and_hero(self):
        self.assertIn("phasesInfo[i].subtitle = `${runRel.active.label} running`", self.app)
        self.assertIn("· ${n} runs`", self.app)
        self.assertIn("runRel?.active?.now", self.app)
        self.assertIn("before it could update this job", self.app)

    def test_the_run_page_says_whose_run_it_is(self):
        self.assertIn('api(`jobs/${encodeURIComponent(familyOf)}/runs`)', self.app)
        self.assertIn("The only run of the job", self.app)
        self.assertIn('Run ${at + 1} of ${work.length}${partial ? " recorded runs" : ""} of the job', self.app)
        family = self.app[self.app.index("function runFamilyHtml"):self.app.index("function runHeader")]
        self.assertNotIn("Previous run:", family)
        self.assertNotIn("Next run:", family)
        self.assertIn("(runHeader.families ||= new Map()).set(run.id, family)", self.app)
        self.assertIn('parts[0] == "jobs" and parts[2] == "runs"', self.server)


if __name__ == "__main__":
    unittest.main()
