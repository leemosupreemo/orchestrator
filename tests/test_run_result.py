from __future__ import annotations

import unittest

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
        self.assertEqual(out["details"], ["test_a (x.Y)"])
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


class GeneralTests(unittest.TestCase):
    def test_other_runs_lead_with_the_last_line_that_says_how_it_went(self):
        out = rr.summarize("update_local", "Updating local package...\n  ✅ Local update complete.\n[exited]\n", 0)
        self.assertEqual(out["headline"], "Local update complete.")
        self.assertEqual(out["tone"], "ok")

    def test_a_failure_leads_with_its_error(self):
        out = rr.summarize("git_pull", "From origin\n❌ Error: branches have diverged\n[exited with code 1]\n", 1)
        self.assertEqual((out["headline"], out["tone"]), ("branches have diverged", "bad"))

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


if __name__ == "__main__":
    unittest.main()
