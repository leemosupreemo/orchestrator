"""Work that was done and passed its tests, where only the commit failed (a git lock), says so and is committed on Resume,
instead of reading "Partially complete · Next: debug failing tests" and building again."""
from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator.web import server as ui

ROOT = Path(__file__).resolve().parents[1]


class PassedUncommittedTests(unittest.TestCase):
    def test_the_job_says_its_tests_passed_and_resume_commits_it(self):
        job = {"status": "human-needed", "last_error": "Command 'git add a.cs' returned non-zero exit status 128.",
               "passed_uncommitted": {"files": ["a.cs", "b.cs"]}, "ai_modified_files": ["a.cs", "b.cs"] * 5,
               "debug_history": [{"result": {"build_ok": True, "tests_ok": False}}]}
        state = ui.job_state(job)
        self.assertEqual((state["label"], state["next"]["action"], state["next"]["label"]),
                         ("Tests passed, not committed", "resume", "Commit the work"))
        self.assertIn("2 files changed", state["reason"])
        self.assertIn("nothing is built again", state["reason"])

    def test_the_worker_records_the_pass_before_committing_and_resume_skips_the_builder(self):
        source = (ROOT / "orchestrator" / "scripts" / "worker_run.py").read_text(encoding="utf-8")
        marked = source.index('job["passed_uncommitted"] = {"files"')
        self.assertLess(marked, source.index('_git_commit_step(f"git add {shlex.quote(f)}")'))
        self.assertIn('committing_only = bool(resume and isinstance(job.get("passed_uncommitted"), dict))', source)
        self.assertIn("while not committing_only:", source)
        self.assertIn('if job.pop("passed_uncommitted", None) is not None:', source)
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Next:</span> commit the finished work", app)


    def test_a_job_ready_to_merge_offers_no_fix_for_an_old_error(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('!["debug", "answer", "merge", "complete", "approve", "schedule", "execute"].includes(next?.action)', app)
        source = (ROOT / "orchestrator" / "scripts" / "worker_run.py").read_text(encoding="utf-8")
        self.assertIn('        # SUCCESS POINT\n        job.pop("last_error", None)', source)

if __name__ == "__main__":
    unittest.main()
