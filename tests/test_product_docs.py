from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import product_docs as P  # noqa: E402

FILLED = "# Use cases\n\n## Primary user\n\nCasual players who want a quick game with a friend.\n\n## Core use cases\n\n- As a player, I can start a match\n- As a player, I can take my turn\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class StorageTests(Base):
    def test_unknown_documents_are_refused(self):
        for bad in ("../etc/passwd", "nope", ""):
            with self.assertRaises(P.ProductDocError):
                P.get(bad)

    def test_write_is_atomic_creates_folders_and_validates(self):
        path = P.write(self.root, "use-cases", FILLED)
        self.assertEqual(path, self.root / "docs" / "product" / "use-cases.md")
        self.assertEqual(P.read(self.root, "use-cases"), FILLED.rstrip("\n") + "\n")
        self.assertFalse(list(path.parent.glob("*.tmp")))
        for bad in ("", "  \n", "x" * (P.MAX_DOC_CHARS + 1), None):
            with self.assertRaises(P.ProductDocError):
                P.write(self.root, "use-cases", bad)

    def test_scaffold_creates_only_missing_docs_and_never_overwrites(self):
        P.write(self.root, "journey", "# My own journey\n\nmine\n")
        created = P.scaffold(self.root)
        self.assertEqual(created, ["use-cases", "screens", "architecture", "plan"])  # the brief has no template; journey existed
        self.assertEqual(P.read(self.root, "journey"), "# My own journey\n\nmine\n")
        self.assertEqual(P.scaffold(self.root), [])

    def test_fresh_templates_do_not_count_as_filled(self):
        P.scaffold(self.root)
        self.assertFalse(any(d["filled"] for d in P.listing(self.root)))
        P.write(self.root, "use-cases", FILLED)
        self.assertEqual({d["id"]: d["filled"] for d in P.listing(self.root)}["use-cases"], True)

    def test_listing_reports_missing_and_present(self):
        P.write(self.root, "plan", FILLED)
        rows = {d["id"]: d for d in P.listing(self.root)}
        self.assertTrue(rows["plan"]["exists"] and rows["plan"]["words"] > 5 and rows["plan"]["updated"])
        self.assertFalse(rows["brief"]["exists"])
        self.assertIsNone(rows["brief"]["updated"])


class ContextTests(Base):
    def test_nothing_filled_means_no_context(self):
        P.scaffold(self.root)
        self.assertEqual(P.context_block(self.root), "")

    def test_filled_docs_are_included_with_paths_and_the_brief_gets_more_room(self):
        (self.root / "docs").mkdir()
        (self.root / "docs" / "product-brief.md").write_text("# Brief\n\n" + "line of brief text\n" * 400)
        P.write(self.root, "use-cases", FILLED + "\n" + "more\n" * 800)
        P.scaffold(self.root)
        block = P.context_block(self.root)
        self.assertIn("## Product context (source of truth)", block)
        self.assertIn("`docs/product-brief.md`", block)
        self.assertIn("`docs/product/use-cases.md`", block)
        self.assertNotIn("Journey", block.replace("User journey", ""))  # an unfilled template isn't included
        self.assertIn("...(trimmed)", block)
        self.assertLess(len(block), P.BRIEF_CONTEXT_CHARS + P.OTHER_CONTEXT_CHARS + 1500)

    def test_the_context_tells_the_planner_to_respect_non_goals(self):
        P.write(self.root, "use-cases", FILLED)
        self.assertIn("stay inside the non-goals", P.context_block(self.root))


class PromptAndReplyTests(Base):
    def test_prompts_carry_the_document_the_request_and_the_other_docs(self):
        P.write(self.root, "use-cases", FILLED)
        P.write(self.root, "plan", "# Plan\n\nNow: start a match\nNext: scores\nLater: chat\n")
        prompt = P.refine_prompt(self.root, "plan", "Make Now smaller", [{"question": "Who plays?", "answer": "Two friends"}, {"question": "x", "answer": " "}])
        self.assertIn("Make Now smaller", prompt)
        self.assertIn("Now: start a match", prompt)
        self.assertIn("Casual players", prompt)  # the other document
        self.assertIn("Two friends", prompt)
        self.assertNotIn("Q: x", prompt)  # unanswered questions are left out
        self.assertIn("Do not invent facts", prompt)
        self.assertIn("ONLY JSON", prompt)
        q = P.questions_prompt(self.root, "journey", "")
        self.assertIn("at most 6 questions", q)
        self.assertIn("Finding it and starting", q)  # a missing doc uses its template

    def test_empty_instruction_asks_to_fill_the_blanks(self):
        self.assertIn("Fill in the empty sections", P.refine_prompt(self.root, "use-cases", "  "))

    def test_replies_are_parsed_from_noisy_output(self):
        reply = 'Sure!\n```json\n{"summary": "Added a user", "markdown": "# Doc\\n\\ntext"}\n```\nDone.'
        self.assertEqual(P.parse_proposal(reply), {"markdown": "# Doc\n\ntext\n", "summary": "Added a user"})
        qs = P.parse_questions('{"questions": [{"question": "Who is it for?", "why": "Shapes the tone"}, {"question": ""}, "junk"]}')
        self.assertEqual(qs, [{"question": "Who is it for?", "why": "Shapes the tone"}])

    def test_bad_replies_are_explained(self):
        for fn, reply in ((P.parse_proposal, "no json here"), (P.parse_proposal, '{"summary": "x"}'), (P.parse_proposal, '{"markdown": "  "}'),
                          (P.parse_proposal, "[1,2]"), (P.parse_questions, '{"questions": []}'), (P.parse_questions, "{broken")):
            with self.assertRaises(P.ProductDocError, msg=reply):
                fn(reply)

    def test_questions_are_capped_at_six(self):
        many = json.dumps({"questions": [{"question": f"Q{i}", "why": ""} for i in range(10)]})
        self.assertEqual(len(P.parse_questions(many)), 6)

    def test_diff_shows_what_changed(self):
        diff = P.unified_diff("a\nb\nc\n", "a\nB\nc\n")
        self.assertIn("-b", diff)
        self.assertIn("+B", diff)
        self.assertEqual(P.unified_diff("same\n", "same\n"), [])


class PlanUpkeepTests(Base):
    def test_a_finished_job_is_listed_under_done_recently_newest_first(self):
        P.scaffold(self.root)
        self.assertTrue(P.note_done(self.root, "Add rematch", "20260901-feature-5"))
        self.assertTrue(P.note_done(self.root, "Fix rejoin  crash", "20260902-bug-6"))
        text = P.read(self.root, "plan")
        done = text.split("## Done recently")[1]
        self.assertLess(done.index("Fix rejoin crash"), done.index("Add rematch"))
        self.assertIn("(`20260902-bug-6`, " + time.strftime("%Y-%m-%d") + ")", done)
        self.assertNotIn("What shipped", done)  # the template's guidance line is replaced by real entries
        for heading in ("## Now", "## Next", "## Later"):
            self.assertIn(heading, text)  # the rest of the plan is untouched

    def test_it_never_adds_a_job_twice_creates_a_plan_or_accepts_junk(self):
        self.assertFalse(P.note_done(self.root, "Something", "j1"))  # no plan file: leave the project alone
        self.assertIsNone(P.read(self.root, "plan"))
        P.scaffold(self.root)
        self.assertTrue(P.note_done(self.root, "Something", "j1"))
        self.assertFalse(P.note_done(self.root, "Something", "j1"))
        self.assertFalse(P.note_done(self.root, "", "j2"))
        self.assertFalse(P.note_done(self.root, "Title", ""))
        self.assertEqual(P.read(self.root, "plan").count("`j1`"), 1)

    def test_the_list_is_kept_short_and_other_sections_after_it_survive(self):
        P.write(self.root, "plan", "# Plan\n\n## Now\n\nthe slice\n\n## Done recently\n\n- old (`a`, 2026-01-01)\n\n## Notes\n\nkeep me\n")
        for i in range(P.MAX_DONE + 5):
            P.note_done(self.root, f"Job {i}", f"id{i}")
        text = P.read(self.root, "plan")
        done = text.split("## Done recently")[1].split("## Notes")[0]
        self.assertEqual(done.count("\n- "), P.MAX_DONE)
        self.assertIn(f"Job {P.MAX_DONE + 4}", done)
        self.assertNotIn("(`a`", done)  # the oldest fell off
        self.assertIn("## Notes\n\nkeep me", text)

    def test_a_plan_without_the_heading_gets_one(self):
        P.write(self.root, "plan", "# Plan\n\n## Now\n\nthe slice\n")
        P.note_done(self.root, "First", "j1")
        self.assertTrue(P.read(self.root, "plan").rstrip().endswith("- First (`j1`, " + time.strftime("%Y-%m-%d") + ")"))


class ReviewDueTests(Base):
    def test_no_reminder_until_there_is_enough_to_review(self):
        self.assertIsNone(P.review_due(self.root, P.REVIEW_AFTER_JOBS - 1))

    def test_never_reviewed_and_stale_reviews_are_due_fresh_ones_are_not(self):
        never = P.review_due(self.root, P.REVIEW_AFTER_JOBS)
        self.assertIn("No review has been run yet", never["reason"])
        folder = self.root / P.REVIEW_DIR
        folder.mkdir(parents=True)
        review = folder / "2026-09-01.md"
        review.write_text("findings")
        self.assertIsNone(P.review_due(self.root, 20))
        old = time.time() - 30 * 86400
        os.utime(review, (old, old))
        due = P.review_due(self.root, 20)
        self.assertEqual(due["days"], 30)
        self.assertIn("30 days ago", due["reason"])


class ReviewTests(Base):
    def test_the_review_asks_for_findings_not_code_changes(self):
        P.write(self.root, "use-cases", FILLED)
        text = P.review_instruction(self.root)
        self.assertIn("Do NOT change any code", text)
        self.assertIn(f"`{P.REVIEW_DIR}/{time.strftime('%Y-%m-%d')}.md`", text)
        self.assertIn("`docs/product/use-cases.md`", text)
        for section in ("Drift from the brief", "Architecture", "Duplication", "UX consistency", "next three slices"):
            self.assertIn(section, text)

    def test_last_review_and_staleness(self):
        self.assertIsNone(P.last_review(self.root))
        folder = self.root / P.REVIEW_DIR
        folder.mkdir(parents=True)
        recent = folder / "2026-09-30.md"
        recent.write_text("findings")
        self.assertEqual((P.last_review(self.root)["stale"], P.last_review(self.root)["days"]), (False, 0))
        old = time.time() - 20 * 86400
        os.utime(recent, (old, old))
        review = P.last_review(self.root)
        self.assertEqual((review["stale"], review["days"], review["path"]), (True, 20, "docs/product/reviews/2026-09-30.md"))


if __name__ == "__main__":
    unittest.main()
