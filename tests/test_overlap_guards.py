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

    def test_each_overlap_says_what_decides_the_next_step(self):
        mine = {"status": "review-needed", "title": "Quick: The vegetatation needs to look more realistic",
                "ai_modified_files": ["World/TreeWind.cs"]}
        others = [("dup", {"status": "review-needed", "title": "Quick: Make vegetation more realistic", "pr_number": 18,
                           "ai_modified_files": ["World/TreeWind.cs"]}),
                  ("busy", {"status": "executing", "title": "Night lighting", "worker_pid": 4242,
                            "ai_modified_files": ["World/TreeWind.cs"]})]
        with mock.patch("os.kill") as kill:  # pid 4242 is alive
            found = {o["id"]: o for o in job_overlap.file_overlaps("me", mine, others)}
        kill.assert_called_with(4242, 0)
        self.assertEqual({k: found["dup"][k] for k in ("duplicate", "running", "ready", "pr")},
                         {"duplicate": True, "running": False, "ready": True, "pr": True})
        self.assertEqual({k: found["busy"][k] for k in ("duplicate", "running", "ready", "pr")},
                         {"duplicate": False, "running": True, "ready": False, "pr": False})

    @unittest.skipUnless(__import__("shutil").which("node"), "node is required")
    def test_the_job_page_gives_one_plain_next_step(self):
        import json
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        start = app.index("function overlapAdviceHtml(")
        fn = app[start:app.index("\nfunction jobChangeSummary(", start)]
        script = f"""
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const attrJSON = (o) => esc(JSON.stringify(o));
{fn}
const base = {{ id: "o", title: "Other", files: ["A.cs", "B.cs"], count: 3, duplicate: false, running: false, ready: false, pr: false }};
console.log(JSON.stringify({{
  dup: overlapAdviceHtml([{{ ...base, duplicate: true }}]),
  busy: overlapAdviceHtml([{{ ...base, running: true }}], {{ readyHere: true }}),
  mergeFirst: overlapAdviceHtml([{{ ...base, pr: true }}], {{ readyHere: true }}),
  otherFirst: overlapAdviceHtml([{{ ...base, ready: true }}]),
  none: overlapAdviceHtml([]),
}}));
"""
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertIn("the same request as", out["dup"])
        self.assertIn(">Keep this one</button>", out["dup"])
        self.assertIn("data-archive-stay", out["dup"])
        self.assertIn("Wait for it to finish before you merge this one", out["busy"])
        self.assertNotIn("Keep this one", out["busy"])
        self.assertIn("Merge this one first", out["mergeFirst"])
        self.assertIn("Merge it first, then come back to this one", out["otherFirst"])
        self.assertIn("<summary class=\"linklike muted\">Which files?</summary>", out["dup"])
        self.assertIn("+1 more", out["dup"])
        self.assertEqual(out["none"], "")
        self.assertIn('overlapHold ? ` disabled data-why="Wait for', app)
        self.assertIn('!("archiveStay" in button.dataset)', app)

    def test_the_server_sends_them(self):
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
