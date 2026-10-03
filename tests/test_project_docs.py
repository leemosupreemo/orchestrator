from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestrator import project_docs as pd

JOB = {"job_id": "j1", "title": "Add rematch", "type": "feature-plan", "status": "review-needed", "issue_number": 12, "pr_number": 14, "branch": "ai/issue-12-rematch",
       "created_at": "2026-10-01T10:30:00", "builder_summary": "Added a rematch button and the endpoint behind it.", "completed_task_indices": [0],
       "plan": {"summary": "Players can start another game with the same opponent.", "assumptions": ["Rematch keeps the same settings"], "constraints": ["No new dependencies"],
                "risks": ["Two taps could start two games"],
                "tasks": [{"title": "Rematch button", "description": "Show it after a game", "acceptance_criteria": ["One tap starts a new game"]}, {"title": "Remember settings"}],
                "test_cases": [{"title": "One tap rematch", "type": "unit", "expected": "A new game starts", "tests": ["test_rematch"]}]},
       "clarification_history": [{"question": "After a loss only?", "answer": "After any game"}],
       "verification": {"status": "concerns", "comments": "Say what happens mid-game.", "risks_identified": ["Race on double tap"]}}


class JobDocTests(unittest.TestCase):
    def test_a_job_page_says_what_it_is_what_was_decided_built_tested_and_reviewed(self):
        md = pd.job_doc(JOB, review="Looks right. One nit.", changed_files=["app/rematch.py", "tests/test_rematch.py"], feature_name="Rematch", links=[{"title": "Pull request #14", "url": "https://x/pull/14"}])
        for needle in ("# Add rematch", "**Status:** Built, ready for review", "**Feature:** Rematch", "`ai/issue-12-rematch`", "#12", "#14",
                       "## What it is", "same opponent", "## Assumptions the AI made", "- Rematch keeps the same settings", "## Constraints", "## Risks",
                       "## Decisions you made", "After a loss only?", "After any game", "## Architect check", "**Concerns.**", "Race on double tap",
                       "## Tasks (1 of 2 done)", "- [x] **Rematch button**", "Done when: One tap starts a new game", "- [ ] **Remember settings**",
                       "## How it is tested", "`test_rematch`", "## What was built", "rematch button and the endpoint", "## Files changed", "`app/rematch.py`",
                       "## Review", "One nit.", "[Pull request #14](https://x/pull/14)"):
            self.assertIn(needle, md, needle)

    def test_sections_with_nothing_in_them_are_left_out_and_an_empty_job_says_so(self):
        md = pd.job_doc({"job_id": "j2", "title": "Bare", "status": "planned"})
        self.assertNotIn("## Risks", md)
        self.assertNotIn("## Review", md)
        self.assertIn("has not recorded anything more yet", md)

    def test_long_text_is_shortened(self):
        md = pd.job_doc({**JOB, "builder_summary": "x" * 9000}, review="y" * 9000)
        self.assertLess(len(md), 9000)
        self.assertIn("(shortened)", md)

    def test_many_changed_files_are_counted_not_all_listed(self):
        md = pd.job_doc(JOB, changed_files=[f"f{i}.py" for i in range(60)])
        self.assertIn("…and 20 more", md)


class FeatureDocTests(unittest.TestCase):
    FEATURE = {"id": "rematch", "name": "Rematch", "status": "in-progress", "summary": "Play again with the same person", "serves": "Friends who want a quick second game",
               "paths": ["app/rematch/"], "depends_on": ["matches"], "waiting_on": ["matches"],
               "kpis": [{"name": "Rematch rate", "event": "rematch_started", "direction": "up", "target": 30, "unit": "%", "status": {"latest": {"value": 22}}}]}

    def test_a_feature_page_lists_what_it_is_where_it_lives_its_jobs_and_how_it_is_measured(self):
        jobs = [{"id": "a", "title": "Rematch button", "state": {"label": "Done", "group": "done"}, "blurb": "Adds the button"},
                {"id": "b", "title": "Remember settings", "state": {"label": "Approve plan", "group": "needs_you"}}]
        md = pd.feature_doc(self.FEATURE, jobs, {"matches": "Matches"})
        for needle in ("# Rematch", "**Status:** In progress", "**Builds on:** Matches", "**Waiting on:** Matches", "Play again with the same person",
                       "Friends who want a quick second game", "`app/rematch/`", "## Jobs (1 of 2 done)", "**Rematch button** (Done): Adds the button",
                       "**Remember settings** (Approve plan)", "## How it is measured", "at least 30 %", "latest 22"):
            self.assertIn(needle, md, needle)

    def test_a_feature_with_no_jobs_says_so(self):
        self.assertIn("_No jobs yet._", pd.feature_doc({"id": "x", "name": "X", "status": "planned"}, []))


class ProjectFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, rel, text="x"):
        f = self.root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    def test_readme_helper_notes_and_docs_are_listed_with_titles_and_groups(self):
        self.write("README.md", "# Word Duel\n\nA game")
        self.write("AGENTS.md", "rules")
        self.write("docs/architecture.md", "# How it fits together")
        self.write("docs/plans/launch-plan.md", "no heading here")
        files = {f["path"]: f for f in pd.project_files(self.root)}
        self.assertEqual(files["README.md"]["title"], "Word Duel")
        self.assertEqual(files["README.md"]["group"], "Project notes")
        self.assertEqual(files["docs/architecture.md"]["title"], "How it fits together")
        self.assertEqual(files["docs/architecture.md"]["group"], "docs")
        self.assertEqual(files["docs/plans/launch-plan.md"]["title"], "Launch plan")  # from the file name when there is no heading
        self.assertEqual(files["docs/plans/launch-plan.md"]["group"], "docs/plans")

    def test_tooling_dependencies_designs_and_hidden_files_are_left_out(self):
        self.write("docs/ok.md")
        for rel in ("docs/designs/sketch.md", "docs/node_modules/x.md", "docs/.hidden.md", ".orchestrator/notes.md", "node_modules/pkg/README.md", "src/README.md"):
            self.write(rel)
        self.assertEqual([f["path"] for f in pd.project_files(self.root)], ["docs/ok.md"])

    def test_only_listed_files_can_be_read(self):
        self.write("README.md", "# Hi")
        self.write("secrets.md", "TOKEN")
        self.write(".orchestrator/prd.json", "{}")
        self.assertEqual(pd.read_project_file(self.root, "README.md"), "# Hi")
        for bad in ("secrets.md", "../outside.md", "/etc/passwd", ".orchestrator/prd.json", "docs/missing.md", ""):
            with self.assertRaises(pd.DocsError, msg=bad):
                pd.read_project_file(self.root, bad)

    def test_a_huge_file_is_cut(self):
        self.write("README.md", "x" * (pd.MAX_FILE_CHARS + 5000))
        self.assertEqual(len(pd.read_project_file(self.root, "README.md")), pd.MAX_FILE_CHARS)


class ExportTests(unittest.TestCase):
    def test_everything_goes_into_one_document_with_headings_nested_under_the_title(self):
        md = pd.export_all("Word Duel", "# Product\n\n## Pitch\n\nA game", [({"id": "f"}, "# Rematch\n\n## Jobs\n\nthree")], [({"id": "j"}, "# Add rematch\n\n## Review\n\nok")],
                           [("docs/a.md", "# File\n\ntext")])
        self.assertTrue(md.startswith("# Word Duel: project documentation"))
        self.assertIn("## Product\n\n### Pitch", md)  # demoted one level
        self.assertIn("## Rematch", md)
        self.assertIn("## Add rematch", md)
        self.assertIn("## File: `docs/a.md`", md)
        self.assertIn("\n### File\n", md)  # a file's own title sits under its "File:" heading
        self.assertEqual([l for l in md.splitlines() if l.startswith("# ")], ["# Word Duel: project documentation"])  # one top-level title only

    def test_without_a_prd_the_rest_still_exports(self):
        md = pd.export_all("X", None, [], [], [])
        self.assertIn("# X: project documentation", md)
        self.assertNotIn("Product", md.split("\n", 1)[1][:40])


if __name__ == "__main__":
    unittest.main()
