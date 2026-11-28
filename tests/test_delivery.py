from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import delivery  # noqa: E402


def fake_git(answers):
    def run(*args):
        return answers.get(" ".join(args), "")
    return run


class LiveTests(unittest.TestCase):
    def test_reports_the_base_branch_tag_and_unreleased_commits(self):
        git = fake_git({"rev-parse --verify --quiet main": "abc", "log -1 --format=%h%x09%s%x09%cr main": "abc1234\tFix seats\t2 days ago",
                        "describe --tags --abbrev=0 main": "v1.2.0", "rev-list --count v1.2.0..main": "7"})
        self.assertEqual(delivery.live(git, "main"), {"branch": "main", "commit": "abc1234", "subject": "Fix seats", "when": "2 days ago",
                                                       "tag": "v1.2.0", "unreleased": 7})

    def test_without_tags_everything_is_unreleased(self):
        git = fake_git({"rev-parse --verify --quiet main": "abc", "log -1 --format=%h%x09%s%x09%cr main": "a\tb\tc", "rev-list --count main": "12"})
        live = delivery.live(git, "main")
        self.assertEqual((live["tag"], live["unreleased"]), (None, 12))

    def test_missing_branch_or_git_is_none_not_an_error(self):
        self.assertIsNone(delivery.live(fake_git({}), "main"))


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name)
        self.dir = self.rt / "output" / "delivery"
        self.dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data, age=0):
        path = self.dir / f"{name}.json"
        path.write_text(json.dumps(data) if not isinstance(data, str) else data)
        import os, time
        os.utime(path, (time.time() - age, time.time() - age))

    def test_newest_first_and_only_delivered_receipts(self):
        self.write("old", {"status": "delivered", "job_id": "old", "version": "1.0", "build": "5", "groups": "internal", "branch": "ai/issue-1"}, age=100)
        self.write("new", {"status": "delivered", "job_id": "new", "title": "Seats", "version": "1.1", "build": "6", "testers": "a@b.c"}, age=1)
        self.write("failed", {"status": "failed", "job_id": "failed"})
        self.write("junk", "{nope")
        out = delivery.receipts(self.rt)
        self.assertEqual([r["job_id"] for r in out], ["new", "old"])
        self.assertEqual((out[0]["version"], out[0]["build"], out[0]["recipients"]), ("1.1", "6", "a@b.c"))
        self.assertEqual(out[1]["recipients"], "internal")

    def test_no_folder_is_empty(self):
        self.assertEqual(delivery.receipts(self.rt / "nowhere"), [])


class PipelineTests(unittest.TestCase):
    ROWS = [{"name": "CI", "status": "completed", "conclusion": "success", "url": "u1", "headBranch": "main", "displayTitle": "Merge", "databaseId": 11},
            {"name": "CI", "status": "completed", "conclusion": "failure", "url": "u2", "headBranch": "ai/issue-3", "databaseId": 12},
            {"name": "CI", "status": "in_progress", "conclusion": "", "url": "u3", "headBranch": "main"}]

    def test_maps_outcomes_to_tones_and_flags_base_runs(self):
        out = delivery.pipeline(lambda argv: json.dumps(self.ROWS), "main")
        self.assertTrue(out["available"])
        self.assertEqual([(r["outcome"], r["tone"], r["on_base"]) for r in out["runs"]],
                         [("success", "done", True), ("failure", "failed", False), ("in_progress", "working", True)])

    def test_only_finished_unsuccessful_runs_with_an_id_can_be_rerun(self):
        rows = self.ROWS + [{"name": "CI", "status": "completed", "conclusion": "cancelled", "headBranch": "x", "databaseId": 14},
                            {"name": "CI", "status": "completed", "conclusion": "failure", "headBranch": "x"}]  # no id
        out = delivery.pipeline(lambda argv: json.dumps(rows), "main")
        self.assertEqual([r["can_rerun"] for r in out["runs"]], [False, True, False, True, False])

    def test_unavailable_when_gh_is_missing_or_output_is_garbage(self):
        self.assertEqual(delivery.pipeline(lambda argv: None, "main"), {"available": False, "runs": []})
        self.assertEqual(delivery.pipeline(lambda argv: "not json", "main"), {"available": False, "runs": []})


class ReadyToShipTests(unittest.TestCase):
    def test_only_reviewed_jobs_with_a_branch_not_already_sent(self):
        jobs = [{"id": "a", "status": "review-needed", "branch": "ai/issue-1", "title": "A", "state": {"next": {"action": "merge"}}},
                {"id": "b", "status": "review-needed", "branch": "ai/issue-2"},
                {"id": "c", "status": "review-needed"},
                {"id": "d", "status": "debugging", "branch": "ai/issue-4"}]
        out = delivery.ready_to_ship(jobs, {"b"})
        self.assertEqual([j["id"] for j in out], ["a"])
        self.assertEqual(out[0]["next"], {"action": "merge"})


class BuildDistributeTests(unittest.TestCase):
    def test_build_distribute_includes_all_flags(self):
        from orchestrator.web import server
        root = Path("/tmp")
        argv = server.build_distribute({
            "branch": "feature/my-branch",
            "group": "qa-testers",
            "testers": "user@example.com",
            "notes": "Testing notes",
        }, root)
        self.assertIn("--branch", argv)
        self.assertIn("feature/my-branch", argv)
        self.assertIn("--groups", argv)
        self.assertIn("qa-testers", argv)
        self.assertIn("--testers", argv)
        self.assertIn("user@example.com", argv)
        self.assertIn("--notes", argv)
        self.assertIn("Testing notes", argv)

    def test_build_distribute_empty_optional_fields(self):
        from orchestrator.web import server
        root = Path("/tmp")
        argv = server.build_distribute({}, root)
        self.assertNotIn("--branch", argv)
        self.assertNotIn("--groups", argv)
        self.assertNotIn("--testers", argv)
        self.assertNotIn("--notes", argv)


if __name__ == "__main__":
    unittest.main()
