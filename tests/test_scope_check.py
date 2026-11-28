from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import scope_check as S  # noqa: E402


def ch(path, status="M", added=10, deleted=0):
    return {"path": path, "status": status, "added": added, "deleted": deleted}


def kinds(result):
    return [f["kind"] for f in result["findings"]]


class ParseTests(unittest.TestCase):
    def test_merges_numstat_and_status(self):
        out = S.parse_numstat("10\t2\tsrc/a.py\n-\t-\timg/logo.png\n3\t0\tsrc/new.py\n", "M\tsrc/a.py\nM\timg/logo.png\nA\tsrc/new.py\n")
        self.assertEqual([(c["path"], c["status"], c["added"], c["deleted"]) for c in out],
                         [("src/a.py", "M", 10, 2), ("img/logo.png", "M", 0, 0), ("src/new.py", "A", 3, 0)])

    def test_ignores_malformed_lines(self):
        self.assertEqual(S.parse_numstat("garbage\n\n", ""), [])


class ScopeTests(unittest.TestCase):
    PLAN = ["src/lobby/seat.py", "src/lobby/"]

    def test_a_change_inside_the_plan_is_clear(self):
        r = S.evaluate(["src/lobby/seat.py"], 1, [ch("src/lobby/seat.py"), ch("tests/test_seat.py", "A")])
        self.assertTrue(r["clear"])
        self.assertEqual((r["files"], r["in_scope"], r["flagged"]), (2, 2, 0))

    def test_directory_plans_cover_what_is_under_them(self):
        self.assertTrue(S.evaluate(self.PLAN, 1, [ch("src/lobby/deep/new.py", "A")])["clear"])

    def test_unplanned_new_and_existing_files_are_separate_findings(self):
        r = S.evaluate(["src/a.py"], 1, [ch("src/a.py"), ch("src/b.py"), ch("src/utils/helper.py", "A")])
        self.assertEqual(kinds(r), ["new_files", "outside_plan"])
        self.assertEqual(r["findings"][0]["files"], ["src/utils/helper.py"])
        self.assertEqual(r["findings"][1]["files"], ["src/b.py"])

    def test_tests_for_planned_files_are_in_scope_but_unrelated_tests_are_not(self):
        plan = ["src/lobby/seat.py"]
        self.assertTrue(S.evaluate(plan, 1, [ch("tests/test_seat.py", "A"), ch("Tests/SeatTests.swift", "A")])["clear"])
        r = S.evaluate(plan, 1, [ch("tests/test_payments.py", "A")])
        self.assertEqual(kinds(r), ["new_files"])

    def test_manifest_changes_are_high_severity_and_come_first(self):
        r = S.evaluate(["src/a.py"], 1, [ch("src/b.py"), ch("package.json", added=3)])
        self.assertEqual(kinds(r)[0], "manifest")
        self.assertEqual(r["findings"][0]["severity"], "high")
        self.assertTrue(S.evaluate(["package.json"], 1, [ch("package.json")])["clear"])

    def test_lockfiles_runtime_dirs_and_case_library_are_ignored(self):
        r = S.evaluate(["src/a.py"], 1, [ch("src/a.py"), ch("package-lock.json", added=900), ch("poetry.lock"), ch(".orchestrator/jobs/x.json"),
                                        ch("docs/test-cases/lobby/TC-1-01.json", "A"), ch("Package.resolved")])
        self.assertTrue(r["clear"])
        self.assertEqual(r["files"], 1)

    def test_size_is_checked_against_the_number_of_tasks(self):
        big = [ch("src/a.py", added=S.LINES_PER_TASK + 1)]
        self.assertEqual(kinds(S.evaluate(["src/a.py"], 1, big)), ["oversized"])
        self.assertTrue(S.evaluate(["src/a.py"], 2, big)["clear"])
        self.assertTrue(S.evaluate(["src/a.py"], 0, [ch("src/a.py", added=S.LINES_PER_TASK)])["clear"])

    def test_code_outside_the_features_paths_is_flagged(self):
        r = S.evaluate(["src/lobby/", "src/shared/net.py"], 1, [ch("src/lobby/a.py"), ch("src/chat/b.py")], owned_paths=["src/lobby/"])
        self.assertIn("outside_feature", kinds(r))
        paths = next(f for f in r["findings"] if f["kind"] == "outside_feature")["files"]
        self.assertEqual(paths, ["src/chat/b.py"])

    def test_without_planned_files_only_size_can_be_checked(self):
        r = S.evaluate([], 1, [ch("src/a.py")])
        self.assertEqual(kinds(r), ["no_plan_files"])
        self.assertEqual(r["findings"][0]["severity"], "low")

    def test_accepted_files_stop_being_flagged(self):
        changed = [ch("src/a.py"), ch("src/b.py")]
        r = S.evaluate(["src/a.py"], 1, changed, accepted=["src/b.py"])
        self.assertTrue(r["clear"])
        r2 = S.evaluate(["src/a.py"], 1, changed, accepted=[])
        self.assertEqual(r2["flagged"], 1)


class TrimRequestTests(unittest.TestCase):
    def test_lists_every_flagged_file_and_forbids_new_work(self):
        r = S.evaluate(["src/a.py"], 1, [ch("src/a.py"), ch("src/b.py"), ch("package.json")])
        text = S.trim_request(r["findings"], "Add seats")
        self.assertIn('"Add seats"', text)
        self.assertIn("- src/b.py", text)
        self.assertIn("- package.json", text)
        self.assertIn("Do not add anything new", text)


if __name__ == "__main__":
    unittest.main()
