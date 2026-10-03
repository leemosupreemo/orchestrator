from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from orchestrator import prd


def project():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    return tmp, root, prd.Prd(root, root / ".orchestrator")


PITCH = "A word game for two friends.\n\nBuilt for: iOS app, web app"
FILLED = prd.replace_section(prd.replace_section(prd.template(), "pitch", PITCH), "who", "Two friends who have a minute.")


class ParsingTests(unittest.TestCase):
    def test_a_new_document_has_five_sections_with_guidance_and_nothing_filled(self):
        view = prd.sections_view(prd.template())
        self.assertEqual([s["id"] for s in view], ["pitch", "who", "features", "look", "not"])
        self.assertFalse(any(s["filled"] for s in view))
        self.assertTrue(all(s["hint"] for s in view))
        self.assertTrue(all(s["optional"] for s in view))  # every section is optional
        self.assertNotIn("Optional", " ".join(s["hint"] for s in view))  # and none says so as if the others weren't

    def test_guidance_lines_do_not_count_as_content(self):
        self.assertEqual(prd.split(prd.template())["pitch"], "")
        self.assertFalse(prd.is_filled("_Say what you have in mind._"))
        self.assertFalse(prd.is_filled("TBD: who is it for?"))
        self.assertTrue(prd.is_filled("A word game"))

    def test_replacing_one_section_keeps_the_rest_including_headings_the_person_added(self):
        text = FILLED + "\n## My own notes\n\nKeep this.\n"
        out = prd.replace_section(text, "features", "- Score a word\n- Compare two words")
        self.assertIn("Keep this.", out)
        self.assertIn("## My own notes", out)
        self.assertEqual(prd.split(out)["pitch"], prd.split(FILLED)["pitch"])
        self.assertEqual(prd.split(out)["features"], "- Score a word\n- Compare two words")

    def test_clearing_a_section_puts_its_guidance_back(self):
        out = prd.replace_section(FILLED, "pitch", "  ")
        self.assertFalse(prd.is_filled(prd.split(out)["pitch"]))
        self.assertIn("## Pitch", out)

    def test_a_missing_section_is_put_back_at_the_end(self):
        out = prd.normalise("# X\n\n## Pitch\n\nA thing.\n")
        self.assertEqual([s["id"] for s in prd.sections_view(out)], ["pitch", "who", "features", "look", "not"])
        self.assertEqual(prd.split(out)["pitch"], "A thing.")

    def test_platforms_come_from_the_pitch(self):
        self.assertEqual(prd.platforms(FILLED), (["iOS app", "web app"], False))
        self.assertEqual(prd.platforms("Built for: not decided yet, please recommend"), ([], True))
        self.assertEqual(prd.platforms("nothing"), ([], False))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.prd = project()
        self.addCleanup(self.tmp.cleanup)

    def test_nothing_is_added_until_something_is_written(self):
        self.assertEqual(prd.context_block(self.root), "")
        self.prd.write(prd.template(), record=False)
        self.assertEqual(prd.context_block(self.root), "")

    def test_each_reader_gets_a_rule_that_fits_it_and_only_filled_sections(self):
        text = prd.replace_section(FILLED, "not", "No chat\nNo accounts")
        self.prd.write(text)
        planner = prd.context_block(self.root, "planner")
        self.assertIn("## Product context (source of truth", planner)
        self.assertIn("### Pitch", planner)
        self.assertIn("No chat", planner)
        self.assertNotIn("### Core features", planner)  # unfilled sections are left out
        self.assertIn("clarification_needed", prd.context_block(self.root, "builder"))
        self.assertIn("Flag work", prd.context_block(self.root, "reviewer"))
        self.assertIn("Reject or flag", prd.context_block(self.root, "verifier"))

    def test_long_sections_are_trimmed_more_for_builders(self):
        self.prd.write(prd.replace_section(FILLED, "features", "- " + "x" * 5000))
        self.assertGreater(len(prd.context_block(self.root, "planner")), len(prd.context_block(self.root, "builder")))
        self.assertIn("(trimmed)", prd.context_block(self.root, "builder"))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.prd = project()
        self.addCleanup(self.tmp.cleanup)

    def test_every_change_is_a_version_and_an_unchanged_save_is_not(self):
        self.assertIsNotNone(self.prd.write(FILLED, "you", "First draft"))
        self.assertIsNone(self.prd.write(FILLED, "you"))
        self.prd.set_section("features", "- Score a word")
        self.assertEqual([v["source"] for v in self.prd.history()], ["you", "you"])
        self.assertEqual(self.prd.history()[1]["summary"], "First draft")
        self.assertNotIn("content", self.prd.history()[0])  # the list stays light

    def test_a_document_that_existed_before_tracking_is_kept_as_the_first_version(self):
        self.prd.path.parent.mkdir(parents=True)
        self.prd.path.write_text(FILLED)
        self.prd.set_section("features", "- Score a word")
        self.assertEqual([v["source"] for v in self.prd.history()], ["you", "earlier"])

    def test_restoring_a_version_brings_back_its_text_and_is_itself_a_version(self):
        self.prd.write(FILLED)
        first = self.prd.history()[0]["id"]
        self.prd.set_section("pitch", "Something else entirely")
        self.prd.revert(first)
        self.assertEqual(prd.split(self.prd.read())["pitch"], prd.split(FILLED)["pitch"])
        self.assertEqual(self.prd.history()[0]["source"], "revert")
        self.assertEqual(len(self.prd.history()), 3)  # nothing was lost
        with self.assertRaises(prd.PrdError):
            self.prd.revert("nope")

    def test_history_is_capped(self):
        for i in range(prd.MAX_VERSIONS + 5):
            self.prd.set_section("pitch", f"version {i}")
        self.assertEqual(len(self.prd.history()), prd.MAX_VERSIONS)

    def test_designs_and_links_are_added_under_look_and_feel_once(self):
        self.prd.write(FILLED)
        self.prd.add_reference("Home sketch", "designs/home.png")
        self.prd.add_reference("Home sketch", "designs/home.png")
        look = prd.split(self.prd.read())["look"]
        self.assertEqual(look.count("[Home sketch](designs/home.png)"), 1)
        self.prd.add_reference("Figma: Onboarding", "https://figma.com/file/abc")
        self.assertIn("figma.com/file/abc", prd.split(self.prd.read())["look"])

    def test_oversized_documents_are_refused(self):
        with self.assertRaises(prd.PrdError):
            self.prd.write("x" * (prd.MAX_DOC_CHARS + 1))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.prd = project()
        self.addCleanup(self.tmp.cleanup)

    def test_automatic_updates_are_on_by_default_and_the_switch_sticks(self):
        self.assertTrue(self.prd.auto_update())
        self.prd.set_auto_update(False)
        self.assertFalse(prd.Prd(self.root, self.root / ".orchestrator").auto_update())
        self.prd.set_auto_update(True)
        self.assertTrue(self.prd.auto_update())

    def test_overview_carries_everything_the_page_needs(self):
        self.prd.write(FILLED, "you", "Draft")
        o = self.prd.overview()
        self.assertEqual((o["exists"], o["auto_update"], o["notice"], o["path"]), (True, True, None, prd.PATH))
        self.assertEqual(len(o["sections"]), 5)
        self.assertEqual(o["history"][0]["summary"], "Draft")
        empty = prd.Prd(self.root / "other", self.root / "other" / ".orchestrator").overview()
        self.assertFalse(empty["exists"])
        self.assertIn("## Pitch", empty["text"])


class MigrationTests(unittest.TestCase):
    def test_the_earlier_documents_become_the_prd_and_stay_where_they_are(self):
        tmp, root, p = project()
        self.addCleanup(tmp.cleanup)
        (root / "docs" / "product").mkdir(parents=True)
        (root / "docs" / "product-brief.md").write_text("# X: product brief\n\n## What it is\n\nA word game.\n\n## Who it's for\n\nFriends.\n\n## The problem\n\nArguing.\n\n## Version 1 must do\n\n1. Score a word\n\n## Platforms\n\n- iOS app\n")
        (root / "docs" / "product" / "use-cases.md").write_text("# U\n\n## Primary user\n\nA casual player\n\n## Non-goals for version 1\n\n- No chat\n")
        self.assertTrue(p.migrate_legacy())
        parts = prd.split(p.read())
        self.assertIn("A word game.", parts["pitch"])
        self.assertIn("Built for: iOS app", parts["pitch"])
        self.assertIn("Arguing", parts["pitch"])
        self.assertIn("A casual player", parts["who"])
        self.assertIn("Score a word", parts["features"])
        self.assertEqual(parts["not"], "- No chat")
        self.assertTrue((root / "docs" / "product-brief.md").exists())
        self.assertEqual(p.history()[0]["source"], "migration")
        self.assertFalse(p.migrate_legacy())  # only once

    def test_nothing_to_migrate_means_no_document(self):
        tmp, root, p = project()
        self.addCleanup(tmp.cleanup)
        self.assertFalse(p.migrate_legacy())
        self.assertFalse(p.exists())


class ImportTests(unittest.TestCase):
    def docx(self, paragraphs):
        buf = BytesIO()
        body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in paragraphs)
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", f'<w:document xmlns:w="x"><w:body>{body}</w:body></w:document>')
        return buf.getvalue()

    def test_markdown_and_text_are_read_as_is(self):
        self.assertEqual(prd.extract_text("PRD.md", "# Hi\n\nThere".encode()), "# Hi\n\nThere")
        self.assertEqual(prd.extract_text("notes.txt", "plain".encode()), "plain")

    def test_word_documents_keep_their_paragraphs(self):
        text = prd.extract_text("prd.docx", self.docx(["Vision", "Make it &amp; ship it"]))
        self.assertEqual(text.splitlines(), ["Vision", "Make it & ship it"])
        with self.assertRaises(prd.PrdError):
            prd.extract_text("bad.docx", b"not a zip")

    def test_pdfs_need_the_tool_and_say_so(self):
        with patch("orchestrator.prd.shutil.which", return_value=None):
            with self.assertRaisesRegex(prd.PrdError, "pdftotext"):
                prd.extract_text("x.pdf", b"%PDF")

    def test_other_formats_are_refused_with_what_to_use_instead(self):
        with self.assertRaisesRegex(prd.PrdError, "markdown, text, Word"):
            prd.extract_text("deck.pptx", b"x")

    def test_the_import_prompt_carries_their_text_and_the_shape(self):
        p = prd.import_prompt("MY OLD PRD TEXT", "old.docx")
        self.assertIn("MY OLD PRD TEXT", p)
        for s in prd.SECTIONS:
            self.assertIn(f"## {s['title']}", p)


class ModelReplyTests(unittest.TestCase):
    def test_proposals_parse_and_bad_replies_are_explained(self):
        p = prd.parse_proposal(json.dumps({"summary": "s", "markdown": "## Pitch\n\nA game"}))
        self.assertIn("## Who it's for", p["markdown"])  # structure restored
        with self.assertRaises(prd.PrdError):
            prd.parse_proposal("no json")
        with self.assertRaises(prd.PrdError):
            prd.parse_proposal('{"markdown": ""}')

    def test_a_reply_in_the_wrong_format_can_be_asked_for_again(self):
        self.assertIn("ONLY the JSON object", prd.FORMAT_REMINDER)

    def test_prompts_say_to_keep_the_persons_words_and_not_to_invent(self):
        for text in (prd.import_prompt("old prd"), prd.update_prompt(FILLED, "job")):
            self.assertIn("own words", text)
            self.assertIn("TBD", text)


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, rel, text="x"):
        f = self.root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    def test_an_empty_folder_has_nothing_to_draft_from(self):
        self.assertFalse(prd.can_draft(self.root))
        (self.root / ".git").mkdir()
        self.write(".orchestrator/config.json")
        self.write("node_modules/a.js")
        self.assertFalse(prd.can_draft(self.root))  # tooling and dependencies don't count

    def test_a_readme_a_manifest_or_source_files_are_enough(self):
        self.write("README.md", "# Thing")
        self.assertTrue(prd.can_draft(self.root))
        for rel in ("package.json", "src/app.swift", "app.py"):
            other = Path(tempfile.mkdtemp(dir=self.tmp.name))
            (other / Path(rel).parent).mkdir(parents=True, exist_ok=True)
            (other / rel).write_text("x")
            self.assertTrue(prd.can_draft(other), rel)

    def test_the_digest_carries_notes_manifests_layout_languages_and_history_and_skips_the_noise(self):
        self.write("README.md", "# Word Duel\n\nA word game for two friends.")
        self.write("AGENTS.md", "Use python3 only")
        self.write("pyproject.toml", '[project]\nname = "wordduel"\ndescription = "scoring"')
        self.write("docs/guide.md", "How to play")
        self.write("docs/prd.md", "OLD PRD TEXT")  # an existing PRD is not evidence of the project
        self.write("wordgame/scoring.py")
        self.write("wordgame/rules.py")
        self.write("web/app.js")
        self.write("node_modules/dep/x.js")
        self.write(".orchestrator/secret.json", "TOKEN")
        d = prd.project_digest(self.root, ["Add rematch", "Fix scoring"])
        for needle in ("A word game for two friends", "Use python3 only", 'name = "wordduel"', "How to play", "wordgame/", ".py x2", ".js x1", "- Add rematch", "- Fix scoring"):
            self.assertIn(needle, d, needle)
        for noise in ("OLD PRD TEXT", "TOKEN", "node_modules", "dep/x.js"):
            self.assertNotIn(noise, d, noise)

    def test_the_digest_is_bounded(self):
        self.write("README.md", "x" * 50_000)
        self.write("pyproject.toml", "y" * 50_000)
        self.assertLessEqual(len(prd.project_digest(self.root, ["c"] * 100)), prd.DIGEST_CHARS)

    def test_the_prompt_says_to_report_what_it_does_today_and_to_say_what_it_is_unsure_of(self):
        text = prd.draft_prompt("### README.md\nA word game", prd.template())
        for needle in ("A word game", "DOES TODAY", "Do not invent", "TBD:", "Not this: leave it empty", "Built for:", "own words"):
            self.assertIn(needle, text, needle)
        self.assertIn("nothing readable", prd.draft_prompt("", ""))
        self.assertNotIn("already written some", text)  # an empty template is not "something the person wrote"
        self.assertIn("already written some", prd.draft_prompt("d", FILLED))
        self.assertIn("Two friends who have a minute", prd.draft_prompt("d", FILLED))  # so it keeps and adds

    def test_a_draft_can_never_write_not_this_but_keeps_what_the_person_wrote(self):
        reply = json.dumps({"summary": "Inferred from the README.", "markdown": prd.replace_section(prd.replace_section(prd.template(), "pitch", "A word game."), "not", "- Not an IDE\n- Not a SaaS")})
        fresh = prd.parse_draft(reply, "")
        self.assertFalse(prd.is_filled(prd.split(fresh["markdown"])["not"]))  # nothing invented
        self.assertEqual(prd.split(fresh["markdown"])["pitch"], "A word game.")
        self.assertIn("only you can say", fresh["summary"])
        mine = prd.parse_draft(reply, prd.replace_section(prd.template(), "not", "- No ads"))
        self.assertEqual(prd.split(mine["markdown"])["not"], "- No ads")
        with self.assertRaises(prd.PrdError):
            prd.parse_draft("not json", "")

    def test_overview_says_whether_a_draft_is_possible(self):
        tmp, root, p = project()
        self.addCleanup(tmp.cleanup)
        self.assertFalse(p.overview()["can_draft"])
        (root / "README.md").write_text("# x")
        self.assertTrue(p.overview()["can_draft"])


JOB = {"job_id": "j1", "title": "Add rematch", "type": "feature-plan", "builder_summary": "Added a rematch button",
       "plan": {"summary": "Players can rematch", "assumptions": ["Rematch keeps the same opponent"],
                "tasks": [{"acceptance_criteria": ["A rematch starts with one tap"]}]},
       "clarification_history": [{"question": "Should rematch be offered after a loss only?", "answer": "No, after any game"}]}


class AutoUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root, self.prd = project()
        self.addCleanup(self.tmp.cleanup)
        self.prd.write(prd.replace_section(FILLED, "not", "No chat"), "you")
        self.calls = []

    def llm(self, reply):
        def call(prompt):
            self.calls.append(prompt)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return call

    def updated(self, **changes):
        text = self.prd.read()
        text = prd.replace_section(text, "features", changes.get("features", "- Rematch after any game"))
        return json.dumps({"changed": True, "summary": changes.get("summary", "Added rematch"), "markdown": text})

    def test_a_useful_change_is_applied_versioned_and_announced(self):
        out = prd.run_update(self.prd, JOB, self.llm(self.updated()))
        self.assertEqual(out["status"], "updated")
        self.assertIn("Rematch after any game", prd.split(self.prd.read())["features"])
        latest = self.prd.history()[0]
        self.assertEqual((latest["source"], latest["job"], latest["summary"]), ("auto", "j1", "Added rematch"))
        notice = self.prd.notice()
        self.assertEqual((notice["summary"], notice["job"], notice["job_title"]), ("Added rematch", "j1", "Add rematch"))
        self.prd.dismiss_notice()
        self.assertIsNone(self.prd.notice())

    def test_the_model_is_shown_the_document_and_what_the_job_and_the_person_decided(self):
        prd.run_update(self.prd, JOB, self.llm('{"changed": false}'))
        prompt = self.calls[0]
        self.assertIn("A word game for two friends", prompt)
        self.assertIn("A rematch starts with one tap", prompt)
        self.assertIn("No, after any game", prompt)

    def test_the_result_can_be_undone_from_the_history(self):
        before = self.prd.read()
        prd.run_update(self.prd, JOB, self.llm(self.updated()))
        self.prd.revert(self.prd.history()[1]["id"])
        self.assertEqual(self.prd.read(), before)

    def test_nothing_is_called_when_switched_off_or_when_the_job_cannot_teach_anything(self):
        self.prd.set_auto_update(False)
        self.assertEqual(prd.run_update(self.prd, JOB, self.llm(self.updated()))["status"], "off")
        self.prd.set_auto_update(True)
        self.assertEqual(prd.run_update(self.prd, {"type": "quick-fix", "job_id": "q"}, self.llm(self.updated()))["status"], "skipped")
        self.assertEqual(prd.run_update(self.prd, {**JOB, "prd_checked": True}, self.llm(self.updated()))["status"], "skipped")
        self.assertEqual(self.calls, [])
        self.assertEqual(prd.run_update(self.prd, {"type": "bug-fix", "job_id": "b", "clarification_history": [{"question": "q", "answer": "a"}]}, self.llm('{"changed": false}'))["status"], "nochange")

    def test_a_document_with_anything_written_is_kept_true_but_an_empty_one_is_left_alone(self):
        tmp, root, other = project()
        self.addCleanup(tmp.cleanup)
        other.write(prd.replace_section(prd.template(), "features", "- Score a word"), record=False)  # no pitch, and that is fine
        self.assertEqual(prd.run_update(other, JOB, self.llm('{"changed": false}'))["status"], "nochange")

    def test_an_empty_document_is_left_alone(self):
        tmp, root, other = project()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(prd.run_update(other, JOB, self.llm(self.updated()))["status"], "empty")

    def test_no_change_is_no_version_and_no_notice(self):
        n = len(self.prd.history())
        self.assertEqual(prd.run_update(self.prd, JOB, self.llm('{"changed": false, "summary": "", "markdown": ""}'))["status"], "nochange")
        self.assertEqual(len(self.prd.history()), n)
        self.assertIsNone(self.prd.notice())

    def test_edits_that_gut_the_document_are_refused(self):
        gutted = prd.replace_section(self.prd.read(), "pitch", "")
        out = prd.run_update(self.prd, JOB, self.llm(json.dumps({"changed": True, "summary": "x", "markdown": gutted})))
        self.assertEqual(out["status"], "rejected")
        self.assertIn("emptied Pitch", out["error"])
        no_nots = prd.replace_section(self.prd.read(), "not", "Something else")
        self.assertIn("Not this", prd.run_update(self.prd, JOB, self.llm(json.dumps({"changed": True, "summary": "x", "markdown": no_nots})))["error"])
        self.assertIn("No chat", self.prd.read())

    def test_a_failing_or_garbled_model_never_raises_and_changes_nothing(self):
        before = self.prd.read()
        self.assertEqual(prd.run_update(self.prd, JOB, self.llm(RuntimeError("model down")))["status"], "error")
        self.assertEqual(prd.run_update(self.prd, JOB, self.llm("I think it is fine"))["status"], "error")
        self.assertEqual(self.prd.read(), before)

    def test_which_jobs_are_worth_checking(self):
        self.assertTrue(prd.should_check({"type": "feature-plan"}))
        self.assertTrue(prd.should_check({"type": "feature-design"}))
        self.assertTrue(prd.should_check({"type": "bug-fix", "clarification_history": [{"q": 1}]}))
        self.assertFalse(prd.should_check({"type": "bug-fix"}))
        self.assertFalse(prd.should_check({"type": "feature-plan", "prd_checked": True}))


if __name__ == "__main__":
    unittest.main()
