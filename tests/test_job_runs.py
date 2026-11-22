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

    def test_task_runs_follow_task_identity_across_reorder_and_keep_shared_runs(self):
        first = {"title": "Earlier title", "run_history_id": "a" * 32}
        second = {"title": "Second", "run_history_id": "b" * 32}
        attempts = {"job-1": {"a" * 32: {"status": "passed", "detail": "Changed app.py"},
                              "b" * 32: {"status": "incomplete", "detail": "Missing planned test"}}}
        initial = {**live("initial", "execute", 1.0), "task_refs": {"job-1": ["a" * 32, "b" * 32]},
                   "task_attempts": attempts}
        jr.record(self.rt, initial)
        latest = {**live("retry", "debug", 2.0, running=True), "task_refs": {"job-1": ["a" * 32]}}
        runs = jr.for_job(self.rt, "job-1", [latest, live("plan", "revise", 1.5)])
        first["title"] = "Edited title"
        groups = jr.task_runs(self.rt, "job-1", [second, first, {"title": "Untouched"}], runs)
        self.assertEqual([[run["id"] for run in group] for group in groups],
                         [["initial"], ["retry", "initial"], []])
        self.assertEqual(groups[0][0]["task_attempts"], attempts)

    def test_legacy_logs_match_exact_unique_titles_and_skip_ambiguous_ones(self):
        jr.record(self.rt, live("historical", "execute", 1.0))
        log = self.rt / "logs/ui/historical-execute.log"
        log.write_text("\x1b[96m==== ⚙️ SUB-TASK 1/3: IMPORTANT FIX ====\x1b[0m\r\n"
                       "==== ⚙️ SUB-TASK 2/3: DUPLICATE ====\n")
        runs = jr.for_job(self.rt, "job-1", [])
        groups = jr.task_runs(self.rt, "job-1", ["Duplicate", "Duplicate", "Important fix"], runs)
        self.assertEqual([[run["id"] for run in group] for group in groups], [[], [], ["historical"]])

    def test_unattributed_work_runs_fall_back_to_current_task_index(self):
        jr.record(self.rt, live("early-fail", "debug", 1.0, exit_code=1))
        runs = jr.for_job(self.rt, "job-1", [])
        groups = jr.task_runs(self.rt, "job-1", [{"title": "Task 1"}, {"title": "Task 2"}], runs, current_task_index=0)
        self.assertEqual([[run["id"] for run in group] for group in groups], [["early-fail"], []])


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
        self.assertNotIn("activeRunCard(", self.app)
        self.assertIn('>View run</a>', self.app)
        self.assertIn('>Stop</button>', self.app)
        self.assertNotIn("This job ran in one go", self.app)
        self.assertIn('<section class="card mb-16" id="job-runs">', self.app)
        self.assertIn('class="job-table-body" id="job-run-list"', self.app)
        self.assertNotIn('fold("Activity & Runs"', self.app)
        self.assertIn('detail.update(self._job_runs_view(root, parts[1], detail["runs"], detail["job"]))', self.server)

    def test_runs_bubble_up_into_the_job_stages_and_hero(self):
        self.assertIn("phasesInfo[i].subtitle = `${runRel.active.label} running`", self.app)
        self.assertIn("· ${n} runs`", self.app)
        self.assertIn("runRel?.active?.now", self.app)
        self.assertIn("before it could update this job", self.app)

    def test_the_run_page_says_whose_run_it_is(self):
        self.assertIn('api(`jobs/${encodeURIComponent(familyOf)}/runs`)', self.app)
        self.assertNotIn("The only run of the job", self.app)
        self.assertNotIn("The only recorded run of the job", self.app)
        self.assertIn("if (!me?.side && work.length <= 1) return \"\";", self.app)
        self.assertIn('Run ${at + 1} of ${work.length}${partial ? " recorded runs" : ""} of the job', self.app)
        family = self.app[self.app.index("function runFamilyHtml"):self.app.index("function runHeader")]
        self.assertNotIn("Previous run:", family)
        self.assertNotIn("Next run:", family)
        self.assertIn('sub: runTaskSub ? `<span class="status-line"><span>${esc(runTaskSub)}</span></span>` : "",', self.app[self.app.index("function runHeader"):][:1800])
        self.assertIn("(runHeader.families ||= new Map()).set(run.id, family)", self.app)
        self.assertIn('parts[0] == "jobs" and parts[2] == "runs"', self.server)

    def test_job_runs_card_uses_job_table_with_5_cutoff_and_25_max(self):
        self.assertIn("const JOB_RUNS_SHOWN = 5;", self.app)
        self.assertIn("const JOB_RUNS_MAX = 25;", self.app)
        self.assertIn("Show more (${visibleRuns.length})", self.app)
        self.assertLess(self.app.index("${jobRunsCard(jobRuns, runRel,"), self.app.index('id="verify-section"'))


class BreakdownTests(unittest.TestCase):
    def test_taskless_and_quick_jobs_use_single_run(self):
        bug = jr.breakdown({"type": "bug-fix", "title": "Fix crash", "plan": {"likely_files": ["app.py"]}})
        self.assertEqual((bug["mode"], bug["planned_runs"], bug["parallel"]), ("single_run", 1, False))
        self.assertEqual(bug["groups"][0]["group_id"], "MAIN")

        quick = jr.breakdown({"type": "quick-fix", "title": "Polish", "plan": {"tasks": [
            {"title": "A", "likely_files": ["a.py"]}, {"title": "B", "likely_files": ["b.py"]}
        ]}})
        self.assertEqual((quick["mode"], quick["planned_runs"], quick["parallel"]), ("single_run", 1, False))

        one_task = jr.breakdown({"type": "feature-plan", "plan": {"tasks": [{"title": "Only", "likely_files": ["a.py"]}]}})
        self.assertEqual((one_task["mode"], one_task["planned_runs"], one_task["groups"][0]["group_id"]), ("single_run", 1, "T1"))

    def test_cohesive_small_tasks_sharing_files_batch_into_single_run(self):
        batched = jr.breakdown({"type": "feature-plan", "plan": {"tasks": [
            {"title": "T1", "complexity": "small", "likely_files": ["ui.py"]},
            {"title": "T2", "complexity": "small", "likely_files": ["ui.py", "style.css"]},
        ]}})
        self.assertEqual((batched["mode"], batched["label"], batched["planned_runs"]), ("single_run", "1 batched run", 1))
        self.assertTrue(batched["groups"][0]["cohesive_batch"])

    def test_overlapping_files_or_shared_branch_schedule_sequential_runs(self):
        seq = jr.breakdown({"type": "feature-plan", "plan": {"tasks": [
            {"title": "T1", "complexity": "medium", "likely_files": ["app.py"]},
            {"title": "T2", "complexity": "medium", "likely_files": ["app.py", "extra.py"]},
        ]}})
        self.assertEqual((seq["mode"], seq["parallel"], seq["waves"]), ("sequential_runs", False, 2))
        self.assertEqual([g["wave"] for g in seq["groups"]], [0, 1])
        self.assertEqual(seq["groups"][1]["depends_on"], ["T1"])

        cur_branch = jr.breakdown({"type": "feature-plan", "branch_mode": "current", "plan": {"tasks": [
            {"title": "T1", "likely_files": ["a.py"]},
            {"title": "T2", "likely_files": ["b.py"]},
        ]}})
        self.assertEqual((cur_branch["mode"], cur_branch["parallel"], cur_branch["waves"]), ("sequential_runs", False, 2))

    def test_disjoint_files_and_foundation_dag_produce_parallel_waves(self):
        # 2 immediately disjoint user-facing tasks -> Wave 0 in parallel
        par = jr.breakdown({"type": "feature-plan", "plan": {"tasks": [
            {"title": "Add iOS leaderboard view", "likely_files": ["ios/LeaderboardView.swift"]},
            {"title": "Add CLI leaderboard export", "likely_files": ["cli/export.py"]},
        ]}})
        self.assertEqual((par["mode"], par["parallel"], par["waves"]), ("parallel_runs", True, 1))
        self.assertEqual([g["wave"] for g in par["groups"]], [0, 0])
        self.assertTrue(all(g["parallel"] for g in par["groups"]))

        # Diamond DAG: T1 (foundation schema) -> (T2 iOS screen || T3 CLI command) -> T4 (integration touching both)
        diamond = jr.breakdown({"type": "feature-plan", "plan": {"tasks": [
            {"title": "Create database schema and data model", "likely_files": ["db/schema.py"]},
            {"title": "Build iOS profile screen", "likely_files": ["ios/ProfileScreen.swift"]},
            {"title": "Build CLI profile command", "likely_files": ["cli/profile_cmd.py"]},
            {"title": "Wire end-to-end profile integration", "likely_files": ["ios/ProfileScreen.swift", "cli/profile_cmd.py"]},
        ]}})
        self.assertEqual((diamond["mode"], diamond["parallel"], diamond["waves"]), ("parallel_runs", True, 3))
        self.assertEqual([g["wave"] for g in diamond["groups"]], [0, 1, 1, 2])
        self.assertEqual([g["parallel"] for g in diamond["groups"]], [False, True, True, False])
        self.assertEqual(diamond["groups"][1]["depends_on"], ["T1"])
        self.assertEqual(diamond["groups"][2]["depends_on"], ["T1"])
        self.assertEqual(diamond["groups"][3]["depends_on"], ["T2", "T3"])


if __name__ == "__main__":
    unittest.main()
