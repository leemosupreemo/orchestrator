"""Every role that reads or judges work is handed the product documents, in the form that suits it."""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "orchestrator" / "scripts"))

from orchestrator import product_docs  # noqa: E402

USE_CASES = "# Use cases\n\n## Core use cases\n\n- As a player I can start a match\n\n## Non-goals for version 1\n\n- No chat\n- No accounts\n- No ads\n"


class AudienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        product_docs.write(self.root, "use-cases", USE_CASES + "filler line\n" * 400)

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_audience_gets_its_own_rule(self):
        rules = {a: product_docs.context_block(self.root, a) for a in product_docs.AUDIENCES}
        self.assertIn("Serve the use cases", rules["planner"])
        self.assertIn("Build only what this task asks", rules["builder"])
        self.assertIn("`clarification_needed`", rules["builder"])
        self.assertIn("Flag work that serves no use case", rules["reviewer"])
        self.assertIn("Reject or flag a plan", rules["verifier"])
        self.assertEqual(len({r.split("\n\n")[1] for r in rules.values()}), 4)

    def test_task_sized_readers_get_less_text_than_planners(self):
        planner = product_docs.context_block(self.root, "planner")
        builder = product_docs.context_block(self.root, "builder")
        self.assertLess(len(builder), len(planner))
        self.assertIn("No chat", builder)  # the non-goals come first in the document, so they survive trimming

    def test_an_unknown_audience_falls_back_to_the_planner(self):
        self.assertEqual(product_docs.context_block(self.root, "nobody"), product_docs.context_block(self.root, "planner"))

    def test_nothing_filled_means_nothing_added_for_anyone(self):
        with tempfile.TemporaryDirectory() as tmp:
            for audience in product_docs.AUDIENCES:
                self.assertEqual(product_docs.context_block(Path(tmp), audience), "")


class BuilderWiringTests(unittest.TestCase):
    def build(self, docs):
        import run_builder as rb
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for doc_id, text in docs.items():
                product_docs.write(root, doc_id, text)
            job = {"job_id": "j1", "issue_number": 1, "branch": None, "type": "feature-plan", "title": "T", "builder": "x",
                   "plan": {"summary": "s", "tasks": [{"title": "a"}], "likely_files": ["src/a.py"]}}
            job_path = root / "job.json"
            job_path.write_text(json.dumps(job))
            prompts = []

            def fake(model, prompt, **kwargs):
                prompts.append(prompt)
                return ('{"summary": "done", "changes": []}', "m", "sid")

            with patch.object(rb, "ROOT", root), patch.object(rb, "OUTPUT_DIR", root / "out"), patch.object(rb, "run_llm", side_effect=fake), \
                    patch.object(rb, "run_build_and_tests", return_value=(True, True)):  # never run the real build or tests from a unit test
                with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
                    rb.run_builder(job_path)
        return prompts

    def test_the_builder_sees_the_non_goals_and_the_stop_and_ask_rule(self):
        prompts = self.build({"use-cases": USE_CASES})
        self.assertTrue(prompts)
        self.assertIn("## Product context (source of truth)", prompts[0])
        self.assertIn("No accounts", prompts[0])
        self.assertIn("use `clarification_needed` instead of guessing", prompts[0])
        self.assertLess(prompts[0].index("Product context"), prompts[0].index("Brief:"))

    def test_the_builder_prompt_is_unchanged_without_documents(self):
        self.assertNotIn("## Product context (source of truth)", self.build({})[0])


class ReviewerWiringTests(unittest.TestCase):
    def test_a_review_without_a_job_file_no_longer_crashes(self):
        import review_ready as rr
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brief = root / "brief.md"
            brief.write_text("the brief")
            argv = ["review_ready.py", "7", "--brief-file", str(brief), "--reviewer", "x"]
            with patch.object(sys, "argv", argv), patch.object(rr, "ROOT", root), patch.object(rr, "OUTPUT_DIR", root / "out"), \
                    patch.object(rr, "gh_text", return_value="{}"), patch.object(rr, "load_pr_diff", return_value="diff"), \
                    patch.object(rr, "run_llm", return_value=("Looks fine.", "m", "sid")):
                with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
                    rr.main()
            self.assertEqual((root / "out" / "pr-7" / "review.md").read_text().strip(), "Looks fine.")

    def test_the_reviewer_judges_the_change_against_the_documents(self):
        import review_ready as rr
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            product_docs.write(root, "use-cases", USE_CASES)
            brief = root / "brief.md"
            brief.write_text("the brief")
            prompts = []

            def fake(model, prompt, **kwargs):
                prompts.append(prompt)
                return ("Looks fine.", "m", "sid")

            argv = ["review_ready.py", "7", "--brief-file", str(brief), "--reviewer", "x"]
            with patch.object(sys, "argv", argv), patch.object(rr, "ROOT", root), patch.object(rr, "OUTPUT_DIR", root / "out"), \
                    patch.object(rr, "gh_text", return_value="{}"), patch.object(rr, "load_pr_diff", return_value="diff"), patch.object(rr, "run_llm", side_effect=fake):
                with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink):
                    rr.main()
        self.assertTrue(prompts)
        self.assertIn("Flag work that serves no use case", prompts[0])
        self.assertIn("No chat", prompts[0])


if __name__ == "__main__":
    unittest.main()
