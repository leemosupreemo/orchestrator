from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator import run_result as rr

SYNC_LOCAL = """\x1b[1;96m[scheduling]\x1b[0m fleet-wide synchronization
Local state: branch='main', hash='5fe39fa0123456', dirty=False

--- Machine: local (Local) ---
      - Skipping synchronization for local machine.

========================================
Fleet Synchronization Summary:
  local          : ✅ OK
[exited]
"""

SYNC_MIXED = """Local state: branch='web-ui', hash='cf7635a99999', dirty=True

--- Machine: studio ---
      - Probing studio...
      - code synced

--- Machine: mini ---
      - ❌ ERROR: Machine unreachable: ssh: connect to host mini port 22: Operation timed out

========================================
Fleet Synchronization Summary:
  studio         : ✅ OK
  mini           : ❌ FAILED
"""


class FleetTests(unittest.TestCase):
    def test_a_sync_with_only_this_computer_says_there_was_nothing_to_sync(self):
        out = rr.summarize("sync_fleet", SYNC_LOCAL, 0)
        self.assertEqual(out["headline"], "No other machines to sync")
        self.assertEqual(out["tone"], "ok")
        self.assertIn("From main at 5fe39fa0", out["details"][0])
        self.assertIn("local: this computer, nothing to sync", out["details"])
        self.assertTrue(any("Add a worker" in d for d in out["details"]))

    def test_a_sync_names_who_worked_and_who_did_not_and_why(self):
        out = rr.summarize("sync_fleet", SYNC_MIXED, 1)
        self.assertEqual(out["headline"], "1 machine failed to sync, 1 machine synced")
        self.assertEqual(out["tone"], "bad")
        self.assertIn("studio: code synced", out["details"])
        self.assertTrue(any(d.startswith("mini: failed, Machine unreachable") for d in out["details"]))
        self.assertIn("uncommitted changes that are not synced", out["details"][0])

    def test_a_clean_remote_sync_counts_the_machines(self):
        out = rr.summarize("sync_fleet", SYNC_MIXED.replace("❌ FAILED", "✅ OK").replace("❌ ERROR", "fine"), 0)
        self.assertEqual(out["headline"], "Synced 2 machines")

    def test_worker_check_reads_its_own_summary_line_and_the_machines_that_are_not_ready(self):
        text = ("\n[here] local\n  repo: /r\n  package: OK\n  xcodebuild: OK\n\n[far] ssh me@far\n  repo: /r\n  worker: NOT READY\n"
                "  details: ssh: connect to host far: timed out\n  hint: run orchestrator worker-install --machine far\n\n"
                "⚠️ 1 of 2 machines ready. Not ready: far\n")
        out = rr.summarize("worker_check", text, 1)
        self.assertEqual(out["headline"], "1 of 2 machines ready. Not ready: far")
        self.assertEqual(out["tone"], "warn")
        self.assertIn("far: worker not ready", out["details"])
        self.assertIn("far: ssh: connect to host far: timed out", out["details"])
        self.assertEqual(rr.summarize("worker_check", "No machines are enabled. Add one.\n", 0)["tone"], "ok")

    def test_the_model_check_reports_per_machine(self):
        text = ("--- Machine: local ---\n  ✅ PASSED (3): a, b, c\n  🟡 LIMITED (1): d\n\n--- Machine: far ---\n  ❌ FAILED (1):\n     - m: no key\n")
        out = rr.summarize("fleet_llm_check", text, 0)
        self.assertEqual(out["headline"], "Models failed on 1 machine: far")
        self.assertEqual(out["tone"], "bad")
        self.assertIn("local: 3 models responded, some limited", out["details"])


class TestsAndCoverage(unittest.TestCase):
    def test_reads_each_runners_totals(self):
        self.assertEqual(rr.summarize("test", "Ran 12 tests in 0.5s\n\nOK\n", 0)["headline"], "12 tests passed")
        out = rr.summarize("test", "FAIL: test_a (x.Y)\nRan 12 tests in 0.5s\n\nFAILED (failures=2, errors=1)\n", 1)
        self.assertEqual((out["headline"], out["tone"]), ("3 of 12 tests failed", "bad"))
        self.assertEqual(out["details"], ["test_a"])  # the name, short; a reason follows when the output gives one
        self.assertEqual(rr.summarize("test", "==== 3 passed, 1 failed in 0.12s ====\n", 1)["headline"], "1 of 4 tests failed")
        self.assertEqual(rr.summarize("test_suite", "==== 5 passed in 0.12s ====\n", 0)["headline"], "5 tests passed")
        self.assertEqual(rr.summarize("test", "Test Suite 'All tests' passed.\n\t Executed 8 tests, with 0 failures (0 unexpected) in 1.2 (1.3) seconds\n", 0)["headline"], "8 tests passed")
        self.assertEqual(rr.summarize("test", "✔ Test run with 4 tests passed after 0.01 seconds.\n", 0)["headline"], "4 tests passed")

    def test_coverage_and_screenshots_and_ux(self):
        self.assertEqual(rr.summarize("coverage", "Measuring...\nOverall coverage: 82.4% of lines\n", 0)["headline"], "Coverage is 82.4%")
        self.assertEqual(rr.summarize("visual_check", "   • Screenshots:       2 screenshot(s) (3.0s)\n", 0)["headline"], "Captured 2 screenshots")
        out = rr.summarize("ux_pass", "[4/4] Save report\n\n3 findings (1 major or worse). Report: out/report.md\n", 0)
        self.assertEqual((out["headline"], out["tone"]), ("3 findings, 1 major or worse", "bad"))
        self.assertEqual(rr.summarize("ux_pass", "Nothing to review. Report: x\n", 0)["tone"], "warn")


class PlanEpicTests(unittest.TestCase):
    OUTPUT = ("      - No branch is created while planning; one is made when the job starts building.\nCREATING NEW FEATURE\nPLANNING\n"
              "231202-feature-3.json\n- Created issue: #3\n[3/3] Skipping dispatch (--no-dispatch).\n[exited]\n")

    def test_planning_an_epic_says_where_the_plan_is_and_that_nothing_was_branched(self):
        out = rr.summarize("plan_epic", self.OUTPUT, 0, "[UI] Nokia-style presentation")
        self.assertEqual((out["headline"], out["tone"]), ("Plan ready to review", "ok"))
        self.assertEqual(out["details"][0], "Job: [UI] Nokia-style presentation")
        self.assertTrue(any("no branch was created" in d for d in out["details"]))
        self.assertTrue(any("under Jobs on Home" in d for d in out["details"]))
        self.assertIn("GitHub issue #3 was created for it.", out["details"])

    def test_a_failed_planning_run_falls_back_to_the_general_reader(self):
        out = rr.summarize("plan_epic", "PLANNING\n❌ Error: the planner timed out\n", 1, "x")
        self.assertEqual((out["headline"], out["tone"]), ("the planner timed out", "bad"))

    def test_replan_with_architect_concerns_explains_nothing_was_built_and_strips_protocol_json(self):
        output = (
            "Answer recorded. Re-planning with it...\n"
            "      - Updating existing job: 20261121-084115-feature-19\n"
            "      - Verification complete: CONCERNS\n"
            "Senior Architect raised concerns.\n"
            '[gate] {"state": "Plan gate \\u00b7 architect review needs attention"}\n'
            "      - Updated job file: .orchestrator/jobs/20261121-084115-feature-19.json\n"
            "      - Updated issue: #19\n"
            "[3/3] Skipping dispatch (--no-dispatch).\n"
            "[exited]\n"
        )
        job = {
            "status": "human-needed",
            "verification": {"status": "concerns", "comments": "Keep vegetation density tuning in scope."},
            "plan": {"tasks": [{"title": "Drop stale #18 remnants"}, {"title": "Tune Camelot Hills density"}]},
        }
        out = rr.summarize("answer", output, 0, job=job)
        self.assertEqual(out["tone"], "warn")
        self.assertEqual(out["headline"], "Plan updated — architect raised concerns (nothing built yet)")
        self.assertIn("Planning only: no code files were changed yet.", out["details"])
        self.assertTrue(any("Updated 2 tasks: Drop stale #18 remnants; Tune Camelot Hills density" in d for d in out["details"]))
        self.assertTrue(any("Keep vegetation density tuning in scope." in d for d in out["details"]))
        self.assertFalse(any(d.startswith("[gate]") for d in out["details"]))

    def test_replan_ready_for_approval_explains_nothing_was_built_yet(self):
        output = (
            "Revising the plan with your feedback...\n"
            "      - Updating existing job: 20261121-084115-feature-19\n"
            "      - Updated job file: .orchestrator/jobs/20261121-084115-feature-19.json\n"
            "[3/3] Skipping dispatch (--no-dispatch).\n"
        )
        job = {"status": "planned", "plan": {"tasks": [{"title": "Increase grassland density"}]}}
        out = rr.summarize("revise", output, 0, job=job)
        self.assertEqual(out["tone"], "warn")
        self.assertEqual(out["headline"], "Plan updated — ready for approval (nothing built yet)")
        self.assertIn("Planning only: no code files were changed yet.", out["details"])
        self.assertTrue(any("Updated 1 task: Increase grassland density" in d for d in out["details"]))


class GeneralTests(unittest.TestCase):
    def test_other_runs_lead_with_the_last_line_that_says_how_it_went(self):
        out = rr.summarize("update_local", "Updating local package...\n  ✅ Local update complete.\n[exited]\n", 0)
        self.assertEqual(out["headline"], "Local update complete.")
        self.assertEqual(out["tone"], "ok")

    def test_a_failure_leads_with_its_error(self):
        out = rr.summarize("git_pull", "From origin\n❌ Error: branches have diverged\n[exited with code 1]\n", 1)
        self.assertEqual((out["headline"], out["tone"]), ("branches have diverged", "bad"))

    def test_server_start_has_clean_title_and_container_subpoints_without_waiting_noise(self):
        sample = (
            "Start OpenDAoC-Core …\n"
            "  Running: docker compose up -d  (in .)\n"
            "  Container opendaoc-server Started\n"
            "  Container opendaoc-db Started\n"
            "  Waiting for every service to be up and healthy …\n"
            "Done: OpenDAoC-Core is running.\n"
        )
        out = rr.summarize("server_start", sample, 0)
        self.assertEqual(out["headline"], "OpenDAoC-Core is running")
        self.assertEqual(out["tone"], "ok")
        self.assertEqual(out["details"], [
            "opendaoc-server — started",
            "opendaoc-db — started",
        ])

    def test_server_start_records_auto_started_docker_and_failure_reason(self):
        sample = (
            "Start OpenDAoC-Core …\n"
            "  Docker isn't running: starting Docker Desktop …\n"
            "  Docker is running.\n"
            "  Container opendaoc-server Started\n"
            "Not done: OpenDAoC-Core is unhealthy. A service is running but reports unhealthy.\n"
        )
        out = rr.summarize("server_start", sample, 1)
        self.assertEqual(out["tone"], "bad")
        self.assertIn("OpenDAoC-Core is unhealthy", out["headline"])
        self.assertIn("Docker started automatically", out["details"])
        self.assertIn("opendaoc-server — started", out["details"])

    def test_without_marks_it_falls_back_to_the_last_lines_and_ignores_noise(self):
        out = rr.summarize("git_fetch", "From github.com:x/y\n * [new branch] a -> origin/a\n==========\n⠋⠙\nAlready up to date.\n[exited]\n", 0)
        self.assertEqual(out["headline"], "Already up to date.")
        self.assertEqual(out["details"], ["From github.com:x/y", "[new branch] a -> origin/a"])

    def test_empty_or_unreadable_output_says_nothing(self):
        self.assertIsNone(rr.summarize("sync_fleet", "", 0))
        self.assertIsNone(rr.summarize("update_local", "\x1b[2J\n[exited]\n", 0))

    def test_a_reader_that_trips_never_hides_the_run(self):
        out = rr.summarize("sync_fleet", "--- Machine: x ---\nLocal state: branch='a', hash='', dirty=False\n  x : ✅ OK\n", 0)
        self.assertIsNotNone(out)

    def test_a_good_looking_reader_result_cannot_hide_a_failed_exit(self):
        out = rr.summarize("test", "Ran 3 tests in 0.1s\n\nOK\n", 2)
        self.assertEqual(out["tone"], "bad")


class OpenQuestionTests(unittest.TestCase):
    LOG = ("❓ Debugger needs your input: Should I leave it unverified?\n"
           "      - Skipped email notification (no recipient emails configured in settings.json).\n")
    JOB = {"job_id": "job-19", "status": "human-needed", "human_clarification_question": "Should I leave it unverified?"}

    def test_a_run_that_stopped_to_ask_leads_with_the_question_and_offers_to_answer(self):
        for action in ("resume", "debug", "execute", "answer"):
            found = rr.summarize(action, self.LOG, 0, job=self.JOB)
            self.assertEqual((found["headline"], found["tone"]), ("Paused: the job has a question for you", "warn"), action)
            self.assertEqual(found["details"][0], "Should I leave it unverified?")
            self.assertEqual(found["next"], {"label": "Answer question", "action": "answer", "params": {"job": "job-19"}})

    def test_the_answer_form_says_a_debugger_question_resumes_the_fix(self):
        app = (Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js").read_text()
        form = app[app.index("  async answer(params) {"):app.index("  async revise(params) {")]
        self.assertIn('asker === "debugger" ? "The fix attempt resumes with your answer."', form)
        self.assertIn("<small>${next}</small>", form)

    def test_a_long_question_is_cut_between_words(self):
        found = rr.summarize("resume", self.LOG, 0, job={**self.JOB, "human_clarification_question": "evidence " * 60})
        self.assertTrue(found["details"][0].endswith("evidence…"), found["details"][0])
        self.assertLessEqual(len(found["details"][0]), rr.MAX_LINE)

    def test_failed_runs_other_runs_and_answered_jobs_are_left_to_their_own_summary(self):
        for action, code, job in (("resume", 1, self.JOB), ("build", 0, self.JOB),
                                  ("resume", 0, {**self.JOB, "status": "review-ready"}),
                                  ("resume", 0, {**self.JOB, "human_clarification_question": ""})):
            found = rr.summarize(action, self.LOG, code, job=job)
            self.assertNotEqual(found["headline"], "Paused: the job has a question for you", (action, code, job))


class TestsJobOutcomeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        folder = self.root / "AppTests"
        folder.mkdir()
        (folder / "ParserTests.swift").write_text("class A {\n func testOne() {}\n func testTwo() {}\n func helper() {}\n}\n")
        (folder / "MatcherTests.swift").write_text("func testOnly() {}\n")
        (folder / "notes.md").write_text("text")
        self.job = {"output_scope": "tests-only", "output_digests": {"AppTests/ParserTests.swift": "x", "AppTests/MatcherTests.swift": "y"},
                    "test_validation": {"passed": True, "kind": "passed"}}

    def test_a_checked_job_says_how_many_files_and_tests_were_added_and_where(self):
        out = rr.job_outcome(self.job, self.root)
        self.assertEqual(out["headline"], "2 test files added with 3 tests, checked and passing")
        self.assertEqual(out["tone"], "ok")
        self.assertNotIn("partial", out)
        self.assertEqual(out["details"][0], "Location: AppTests/")
        self.assertIn("ParserTests.swift: 2 tests", out["details"])
        self.assertIn("MatcherTests.swift: 1 test", out["details"])

    def test_tests_that_could_not_be_checked_are_a_warning_with_the_way_forward(self):
        self.job["test_validation"] = {"passed": False, "kind": "environment", "detail": "Unable to find a device matching the destination"}
        out = rr.job_outcome(self.job, self.root)
        self.assertEqual((out["tone"], out["headline"].split(", ", 1)[1]), ("warn", "not checked yet: the build environment failed"))
        self.assertTrue(any("Unable to find a device" in d for d in out["details"]))
        self.assertTrue(any("Try again" in d for d in out["details"]))
        self.assertTrue(out["partial"])  # the run page marks it partially complete, not just completed

    def test_tests_that_failed_are_bad_and_point_at_the_log(self):
        self.job["test_validation"] = {"passed": False, "kind": "tests"}
        out = rr.job_outcome(self.job, self.root)
        self.assertEqual(out["tone"], "bad")
        self.assertTrue(any("test.log" in d for d in out["details"]))

    def test_files_that_never_ran_and_files_added_to_the_test_target_are_both_reported(self):
        self.job["test_validation"] = {"passed": False, "kind": "unregistered", "not_run": ["AppTests/ParserTests.swift"],
                                       "detail": "They aren't part of the project's test target."}
        out = rr.job_outcome(self.job, self.root)
        self.assertEqual((out["tone"], out["partial"]), ("warn", True))
        self.assertIn("1 file never ran", out["headline"])
        self.assertTrue(any("test target" in d for d in out["details"]))
        self.job["test_validation"] = {"passed": True, "kind": "passed", "registered": ["AppTests/ParserTests.swift"],
                                       "note": "App.xcodeproj has uncommitted changes, so more files weren't added."}
        done = rr.job_outcome(self.job, self.root)
        self.assertEqual(done["tone"], "ok")
        self.assertIn("Added 1 file to the Xcode test target so they run.", done["details"])
        self.assertTrue(any("uncommitted changes" in d for d in done["details"]))

    def test_failed_tests_and_defects_found_in_the_code_are_named(self):
        self.job["test_validation"] = {"passed": False, "kind": "tests",
                                       "failures": [{"test": "ParserTests.testCRLF", "message": "1 is not equal to 2"}, {"test": "ParserTests.testB", "message": "b"}, {"test": "X.testC", "message": "c"}]}
        out = rr.job_outcome(self.job, self.root)
        self.assertEqual(out["tone"], "bad")
        self.assertIn("Failed: testCRLF: 1 is not equal to 2", out["details"])
        self.assertNotIn("Failed: testC: c", out["details"])  # the first two only
        self.assertTrue(any("report it as a bug" in d for d in out["details"]))
        self.job["test_validation"] = {"passed": True, "kind": "passed"}
        self.job["production_defects"] = [{"summary": "CRLF is not a line break", "test": "testCRLF"}]
        found = rr.job_outcome(self.job, self.root)
        self.assertEqual(found["tone"], "warn")
        self.assertTrue(found["headline"].endswith("1 defect found in the code"))
        self.assertIn("Found 1 production defect: CRLF is not a line break", found["details"])
        self.assertNotIn("partial", found)  # the tests passed; the finding is about the code

    def test_the_delivery_report_lists_suites_and_tests_then_what_was_held_back_then_the_next_step(self):
        (self.root / "AppTests" / "ParserTests.swift").write_text("final class ParserTests: XCTestCase {\n func testOne() {}\n func testTwo() {}\n}\n")
        (self.root / "AppTests" / "MatcherTests.swift").write_text("@Suite\nstruct MatcherTests {\n @Test func only() {}\n}\nfunc testOnly() {}\n")
        self.job["production_defects"] = [{"summary": "CRLF is not a line break", "test": "testCRLF", "expected": "2 items", "actual": "1 item", "code": "ignored"}]
        out = rr.delivery_report(self.job, self.root)
        self.assertEqual([(a["file"], a["suites"]) for a in out["added"]],
                         [("AppTests/MatcherTests.swift", ["MatcherTests"]), ("AppTests/ParserTests.swift", ["ParserTests"])])
        self.assertEqual((out["suites"], out["tests"], out["check"]), (2, 3, "Checked and passing"))
        self.assertEqual(out["blocked"], [{"summary": "CRLF is not a line break", "test": "testCRLF", "expected": "2 items", "actual": "1 item"}])
        self.assertIn("mark this job complete", out["recommendation"])
        self.assertIn("Create a bug job", out["recommendation"])
        self.assertIn("1 test held back", out["recommendation"])

    def test_the_delivery_report_recommends_the_right_step_for_each_state_and_skips_other_jobs(self):
        self.job["test_validation"] = {"passed": False, "kind": "environment"}
        self.assertTrue(rr.delivery_report(self.job, self.root)["recommendation"].startswith("Try again"))
        self.job["test_validation"] = {"passed": False, "kind": "tests"}
        self.assertIn("report it as a bug", rr.delivery_report(self.job, self.root)["recommendation"])
        self.job["test_validation"] = {"passed": True, "kind": "passed"}
        self.assertTrue(rr.delivery_report(self.job, self.root)["recommendation"].startswith("Nothing more to do"))
        self.assertIsNone(rr.delivery_report({"output_scope": None}, self.root))

    def test_the_new_coverage_is_reported_with_what_it_was(self):
        self.job["coverage"] = {"before": 61.5, "after": 70.25}
        out = rr.job_outcome(self.job, self.root)
        self.assertIn("Coverage is now 70.2% (was 61.5%)", out["details"])
        self.assertEqual(rr.delivery_report(self.job, self.root)["coverage"], {"before": 61.5, "after": 70.25})
        self.job["coverage"] = {"before": None, "after": None, "error": "Coverage couldn't be measured for this project."}
        self.assertEqual(rr.delivery_report(self.job, self.root)["coverage"], {"error": "Coverage couldn't be measured for this project."})
        self.assertFalse(any("Coverage is now" in d for d in rr.job_outcome(self.job, self.root)["details"]))

    def test_a_job_that_added_no_files_says_so_and_other_jobs_are_left_alone(self):
        out = rr.job_outcome({"output_scope": "tests-only", "last_error": "Needs a production change"}, self.root)
        self.assertEqual((out["headline"], out["tone"]), ("No test files were added", "warn"))
        self.assertIsNone(rr.job_outcome({"output_scope": None, "output_digests": {"a": "b"}}, self.root))

    def test_a_clean_exit_reports_the_job_but_a_failed_exit_keeps_the_general_reader(self):
        self.assertIn("2 test files added", rr.summarize("schedule", "done\n", 0, job=self.job, root=self.root)["headline"])
        failed = rr.summarize("schedule", "❌ boom\n", 1, job=self.job, root=self.root)
        self.assertEqual(failed["headline"], "boom")



class FixAttemptOutcomeTests(unittest.TestCase):
    """A fix attempt that ends with the tests still failing leaves the job in debugging: say so, and offer the next step."""
    JOB = {"job_id": "job-7", "status": "debugging", "iteration": 3, "max_iterations": 8, "debug_phase": "propose"}
    TAIL = "⚠️ The tests still fail after this fix attempt. The job stays open on its branch and nothing is lost.\n"

    def test_it_says_where_the_job_stands_and_offers_another_fix(self):
        out = rr.summarize("resume", "FAIL: test_slope (tests.T)\nAssertionError: 3 != 4\n" + self.TAIL, 0, job=dict(self.JOB), root=Path("."))
        self.assertEqual(out["headline"], "The tests still fail after this fix attempt")
        self.assertEqual(out["tone"], "warn")
        self.assertEqual(out["next"], {"label": "Run another fix", "action": "debug", "params": {"job": "job-7"}})
        self.assertIn("test_slope", out["details"][0])
        self.assertIn("does not retry by itself", out["details"][-1])

    def test_a_run_that_found_no_tests_says_it_is_a_test_setup_problem(self):
        out = rr.summarize("execute", "Ran 0 tests in 0.000s\n\nNO TESTS RAN\n" + self.TAIL, 0, job=dict(self.JOB), root=Path("."))
        self.assertIn("No tests ran", out["details"][0])
        self.assertIn("not with the code", out["details"][0])
        self.assertEqual(out["next"]["action"], "debug")

    def test_a_paused_loop_says_why(self):
        job = dict(self.JOB, debug_phase="paused", debug_pause_reason="Loop/Stall detected: same hypothesis three times")
        out = rr.summarize("debug", "", 0, job=job, root=Path("."))
        self.assertTrue(out["headline"].startswith("Automatic fixing paused"))
        self.assertIn("Loop/Stall detected", " ".join(out["details"]))

    def test_other_runs_and_other_states_are_left_alone(self):
        text = "✅ done\n"
        self.assertNotIn("next", rr.summarize("resume", text, 0, job=dict(self.JOB, status="review-needed"), root=Path(".")) or {})
        self.assertNotIn("next", rr.summarize("export_job_bundle", text, 0, job=dict(self.JOB), root=Path(".")) or {})  # not a run that tests the job
        self.assertNotIn("next", rr.summarize("resume", "❌ Build failed\n", 1, job=dict(self.JOB), root=Path(".")) or {})  # a failed run keeps its own failure card

    def test_the_run_page_shows_the_status_headline_and_button(self):
        app = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn('doneNext ? "Needs your next step"', app)
        self.assertIn('doneNext ? "Not finished: the job needs another fix attempt"', app)
        self.assertIn('${act(doneNext.action, doneNext.params || {})}>${esc(doneNext.label)}</button>', app)


if __name__ == "__main__":
    unittest.main()
