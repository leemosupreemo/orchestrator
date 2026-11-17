"""Guards for overlapping work: the same request made twice (typos and all), two open jobs changing the same files, and a job
starting in a folder that holds another job's uncommitted work (which it would commit as its own, or check out of existence).
Git runs only in throwaway repositories."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import blockers, job_overlap

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator" / "scripts"))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class DuplicateTests(unittest.TestCase):
    def test_a_typo_doesnt_hide_the_same_request(self):
        self.assertGreater(job_overlap.similarity("Quick: The vegetatation needs to look more realistic",
                                                  "Quick: Make vegetation more realistic"), 0)

    def test_short_words_and_different_work_still_dont_match(self):
        self.assertEqual(job_overlap.similarity("Add lgoin page", "Add logout page"), 0)
        self.assertEqual(job_overlap.similarity("Fix the login bug", "Add a login page"), 0)
        self.assertFalse(job_overlap.same_word("login", "logout"))
        self.assertTrue(job_overlap.same_word("vegetation", "vegetatation"))


class FileOverlapTests(unittest.TestCase):
    def test_open_jobs_changing_the_same_files_are_named(self):
        mine = {"status": "planned", "plan": {"likely_files": ["World/GrassSystem.cs"], "tasks": [{"likely_files": ["World/TreeWind.cs"]}]}}
        others = [("a", {"status": "executing", "title": "Vegetation", "ai_modified_files": ["World/GrassSystem.cs", "World/TreeWind.cs"]}),
                  ("b", {"status": "completed", "title": "Old", "ai_modified_files": ["World/GrassSystem.cs"]}),
                  ("c", {"status": "planned", "title": "Moved", "moved_to": "Air", "plan": {"likely_files": ["World/TreeWind.cs"]}}),
                  ("me_task_1", {"status": "executing", "ai_modified_files": ["World/GrassSystem.cs"]}),
                  ("d", {"status": "planned", "title": "Docs", "plan": {"likely_files": ["README.md", "docs/"]}})]
        found = job_overlap.file_overlaps("me", mine, others)
        self.assertEqual([(o["id"], o["files"]) for o in found], [("a", ["World/GrassSystem.cs", "World/TreeWind.cs"])])

    def test_the_job_page_says_so(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Both change the same files: finish one first, or merge them", app)
        server = (ROOT / "orchestrator" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn('"overlaps": job_overlap.file_overlaps(path.stem, job,', server)


class OtherJobsWorkTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        git(self.root, "init", "-q", "-b", "feature/trees")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Test")
        for name in ("Grass.cs", "Notes.md"):
            (self.root / name).write_text("old")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "first")
        self.other = ("quick-16", {"title": "Make vegetation more realistic", "status": "human-needed", "branch": "feature/trees",
                                   "passed_uncommitted": {"files": ["Grass.cs"]}})

    def test_a_job_wont_start_on_another_jobs_uncommitted_work(self):
        (self.root / "Grass.cs").write_text("the other job's finished work")
        blocker = blockers.other_jobs_work(self.root, "quick-15", [self.other], "feature/trees")
        self.assertEqual((blocker["kind"], blocker["key"], blocker["files"]), ("job_work", "quick-16", ["Grass.cs"]))
        self.assertIn("uncommitted work here", blocker["title"])
        self.assertFalse(blockers.is_cleared(blocker, self.root))
        self.assertIn("Commit the work", blockers.steps(blocker, self.root)[0])
        git(self.root, "commit", "-qam", "the other job commits its work")
        self.assertTrue(blockers.is_cleared(blocker, self.root))  # so this job starts by itself

    def test_unrelated_changes_another_branch_a_finished_job_or_its_own_task_copy_dont_block(self):
        (self.root / "Grass.cs").write_text("changed")
        (self.root / "Notes.md").write_text("your own edit")
        other_branch = ("x", {**self.other[1], "branch": "feature/other"})
        finished = ("y", {**self.other[1], "status": "completed"})
        own_copy = ("quick-15_task_1", self.other[1])
        self.assertIsNone(blockers.other_jobs_work(self.root, "quick-15", [other_branch, finished, own_copy], "feature/trees"))
        self.assertIsNone(blockers.other_jobs_work(self.root, "quick-15", [("z", {"status": "executing", "branch": "feature/trees",
                                                                                   "ai_modified_files": ["Other.cs"]})], "feature/trees"))

    def test_the_worker_checks_before_touching_git(self):
        source = (ROOT / "orchestrator" / "scripts" / "worker_run.py").read_text(encoding="utf-8")
        check = source.index("        if other := other_job_in_folder(job_path):")
        self.assertLess(check, source.index("            branch, base_branch = prepare_git_branch(job, issue_number)"))
        import worker_run
        with mock.patch.object(worker_run, "ROOT", self.root), mock.patch.object(worker_run, "current_git_branch", return_value="feature/trees"):
            jobs = Path(tempfile.mkdtemp())
            (jobs / "quick-15.json").write_text("{}")
            import json
            (jobs / "quick-16.json").write_text(json.dumps(self.other[1]))
            self.assertIsNone(worker_run.other_job_in_folder(jobs / "quick-15.json"))  # nothing uncommitted yet
            (self.root / "Grass.cs").write_text("the other job's finished work")
            self.assertEqual(worker_run.other_job_in_folder(jobs / "quick-15.json")["key"], "quick-16")


if __name__ == "__main__":
    unittest.main()
