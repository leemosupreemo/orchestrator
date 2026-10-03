from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import new_project as np
from orchestrator import prd
from orchestrator.project_config import load_recent_projects
from orchestrator.web import server as ui

ANSWERS = {"name": "Word Duel", "pitch": "A turn-based word game.", "audience": "Friends who like games",
           "problem": "Async games are clunky.", "features": "- Start a match\n2) Take turns\n\nSee scores",
           "platform": "iOS app", "stack": "SwiftUI", "done": "Two phones can finish a match"}
GIT_ENV = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"}


class Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        (self.home / "state").mkdir()
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "ORCHESTRATOR_USER_STATE_DIR": str(self.home / "state"), **GIT_ENV})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class PrdTests(unittest.TestCase):
    def test_the_prd_is_filled_in_from_every_answer(self):
        md = np.render_prd(ANSWERS)
        parts = prd.split(md)
        self.assertTrue(md.startswith("# Word Duel: product requirements"))
        self.assertIn("A turn-based word game.", parts["pitch"])
        self.assertIn("Problem: Async games are clunky.", parts["pitch"])
        self.assertIn("Built for: iOS app", parts["pitch"])
        self.assertIn("SwiftUI", parts["pitch"])
        self.assertIn("Friends who like games", parts["who"])
        self.assertEqual([l for l in parts["features"].splitlines() if l.startswith("- ")], ["- Start a match", "- Take turns", "- See scores"])
        self.assertIn("Version 1 is done when: Two phones can finish a match", parts["features"])

    def test_sections_nothing_was_asked_about_keep_their_guidance_and_stay_empty(self):
        parts = prd.split(np.render_prd({**ANSWERS, "stack": "", "done": ""}))
        self.assertFalse(prd.is_filled(parts["look"]))
        self.assertFalse(prd.is_filled(parts["not"]))
        self.assertNotIn("Technology preferences", parts["pitch"])
        self.assertNotIn("Version 1 is done when", parts["features"])
        self.assertIn("## Look and feel", np.render_prd(ANSWERS))  # the section is there to fill in later

    def test_look_and_not_sections_populated_when_provided(self):
        answers = {**ANSWERS, "look": "Minimal, typography-driven style.", "not": "No social login, no ads."}
        parts = prd.split(np.render_prd(answers))
        self.assertTrue(prd.is_filled(parts["look"]))
        self.assertEqual(parts["look"], "Minimal, typography-driven style.")
        self.assertTrue(prd.is_filled(parts["not"]))
        self.assertEqual(parts["not"], "No social login, no ads.")

    def test_only_the_lean_questions_are_required(self):
        self.assertEqual(len(np.REQUIRED), 6)
        self.assertEqual(np.missing_answers({"name": "x"}), [q["label"] for q in np.QUESTIONS if q["required"] and q["key"] != "name"])


class PlatformTests(unittest.TestCase):
    def test_several_platforms_are_listed_in_the_pitch(self):
        md = np.render_prd({**ANSWERS, "platform": "iOS app, Android app, Backend / API"})
        self.assertEqual(prd.platforms(md), (["iOS app", "Android app", "Backend / API"], False))

    def test_recommend_me_tells_the_ai_to_choose_and_explain(self):
        md = np.render_prd({**ANSWERS, "platform": np.RECOMMEND})
        self.assertEqual(prd.platforms(md), ([], True))
        self.assertIn("recommend platforms, with reasons", md)
        self.assertNotIn("Not sure", md)

    def test_recommend_alongside_a_pick_keeps_what_was_picked(self):
        self.assertEqual(prd.platforms(np.render_prd({**ANSWERS, "platform": f"Web app, {np.RECOMMEND}"})), (["Web app"], False))

    def test_old_single_choice_values_still_work(self):
        self.assertEqual(np.platform_list("iOS app"), ["iOS app"])
        self.assertEqual(np.platform_list(" Web app,Web app , Backend / API\n"), ["Web app", "Backend / API"])
        self.assertEqual(prd.platforms(np.render_prd({**ANSWERS, "platform": "Something else"}))[0], ["Something else"])

    def test_platform_is_required_and_next_steps_exist_for_each_real_choice(self):
        self.assertEqual(np.missing_answers({k: v for k, v in ANSWERS.items() if k != "platform"}), ["What are you building it for?"])
        real = [p for p in np.PLATFORMS if p != np.RECOMMEND]
        self.assertTrue(all(np.PLATFORM_NEEDS[p] for p in real))
        self.assertEqual([n["platform"] for n in np.platform_needs("iOS app, Not sure: recommend for me, Web app")], ["iOS app", "Web app"])

    def test_cli_asks_for_several_platforms_and_joins_them(self):
        pass


class DraftTests(Isolated):
    def test_draft_roundtrip_drops_unknown_fields_and_remembers_the_step(self):
        saved = np.save_draft({"answers": {**ANSWERS, "evil": "x"}, "step": "where", "host": "github", "waiting_on_github": True, "extra": 1})
        self.assertNotIn("evil", saved["answers"])
        self.assertNotIn("extra", saved)
        loaded = np.load_draft()
        self.assertEqual((loaded["step"], loaded["host"], loaded["waiting_on_github"]), ("where", "github", True))
        np.clear_draft()
        self.assertIsNone(np.load_draft())

    def test_a_bad_step_or_host_falls_back_to_what_was_saved(self):
        np.save_draft({"answers": ANSWERS, "step": "where", "host": "github"})
        again = np.save_draft({"answers": ANSWERS, "step": "hack", "host": "gitlab"})
        self.assertEqual((again["step"], again["host"]), ("where", "github"))


class CreateTests(Isolated):
    def test_creates_folder_brief_agents_and_a_git_commit(self):
        root, steps = np.create_local(ANSWERS, str(self.home / "Projects"))
        self.assertEqual(root.name, "Word-Duel")
        self.assertIn("Word Duel", (root / "docs" / "product" / "prd.md").read_text())
        self.assertIn("docs/product/prd.md", (root / "AGENTS.md").read_text())
        self.assertTrue(all(s["ok"] for s in steps), steps)
        log = subprocess.run(["git", "log", "--oneline"], cwd=root, capture_output=True, text=True).stdout
        self.assertIn("product requirements", log)

    def test_refuses_unsafe_or_incomplete_requests(self):
        with self.assertRaises(np.NewProjectError):
            np.create_local({**ANSWERS, "pitch": ""}, str(self.home))
        with self.assertRaises(np.NewProjectError):
            np.create_local(ANSWERS, "/tmp/elsewhere")  # outside the home folder
        with self.assertRaises(np.NewProjectError):
            np.create_local({**ANSWERS, "name": "!!!"}, str(self.home))
        np.create_local(ANSWERS, str(self.home))
        with self.assertRaises(np.NewProjectError):  # already there and not empty
            np.create_local(ANSWERS, str(self.home))

    def test_local_only_project_is_registered_and_never_touches_github(self):
        with patch.object(np, "publish_to_github") as publish:
            result = np.create_project({"answers": ANSWERS, "parent": str(self.home), "host": "local"})
        publish.assert_not_called()
        self.assertTrue(result["github_ok"])
        self.assertEqual(load_recent_projects()["projects"][0]["name"], "Word Duel")

    def test_github_not_ready_keeps_the_local_project_and_reports_it(self):
        with patch("orchestrator.setup_checklist.github_cli_state", return_value={"installed": True, "user": None}):
            result = np.create_project({"answers": ANSWERS, "parent": str(self.home), "host": "github"})
        self.assertTrue(Path(result["root"], "docs", "product", "prd.md").exists())
        self.assertFalse(result["github_ok"])
        self.assertIn("Not signed in", result["steps"][-2]["detail"])

    def test_github_publish_uses_a_private_repo_by_default(self):
        root, _ = np.create_local(ANSWERS, str(self.home))
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "https://github.com/me/Word-Duel\n", "")
        with patch("orchestrator.setup_checklist.github_cli_state", return_value={"installed": True, "user": "me"}), \
             patch.object(np.subprocess, "run", fake_run):
            step = np.publish_to_github(root, "Word Duel", "private")
        self.assertTrue(step["ok"])
        self.assertIn("--private", calls[0])
        self.assertEqual(step["url"], "https://github.com/me/Word-Duel")


class ServerFlowTests(Isolated):
    class FakeServer:
        bootstrap_enabled = False
        root = None

        def set_root(self, root):
            self.root = root

    def test_new_project_state_includes_prd_sections(self):
        state = ui.new_project_state()
        self.assertIn("prd_sections", state)
        self.assertEqual([s["id"] for s in state["prd_sections"]], ["pitch", "who", "features", "look", "not"])

    def test_success_switches_project_and_clears_the_draft(self):
        np.save_draft({"answers": ANSWERS, "parent": str(self.home), "host": "local"})
        srv = self.FakeServer()
        result = ui.create_new_project(srv)
        self.assertEqual(srv.root, Path(result["root"]))
        self.assertIsNone(np.load_draft())

    def test_github_failure_keeps_a_draft_waiting_on_github(self):
        np.save_draft({"answers": ANSWERS, "parent": str(self.home), "host": "github"})
        with patch("orchestrator.setup_checklist.github_cli_state", return_value={"installed": False, "user": None}):
            result = ui.create_new_project(self.FakeServer())
        self.assertFalse(result["github_ok"])
        draft = np.load_draft()
        self.assertTrue(draft["waiting_on_github"])
        self.assertEqual(draft["step"], "create")

    def test_nothing_to_create_without_a_draft(self):
        with self.assertRaises(ui.UIError):
            ui.create_new_project(self.FakeServer())

    def test_publish_only_works_on_known_projects_without_a_remote(self):
        with self.assertRaises(ui.UIError):
            ui.publish_known_project(str(self.home), "private")
        root, _ = np.create_local(ANSWERS, str(self.home))
        from orchestrator.project_config import remember_project
        remember_project(root, "Word Duel")
        with patch("orchestrator.setup_checklist.github_cli_state", return_value={"installed": True, "user": None}):
            step = ui.publish_known_project(str(root), "private")
        self.assertFalse(step["ok"])


if __name__ == "__main__":
    unittest.main()


class CliFlowTests(Isolated):
    """`orchestrator new`, driven by scripted answers."""

    def drive(self, answers, picks, gh_states, confirms=(True,), logins=None, platforms=(("iOS app",),)):
        from orchestrator import new_project_cli as cli
        asked, said = [], []
        answers, picks, states, confirms = list(answers), list(picks), list(gh_states), list(confirms)

        def ask(label, default=None, optional=False):
            asked.append(label)
            return answers.pop(0) if answers else (default or "")

        def choose(label, options):
            pick = picks.pop(0)
            match = next((o for o in options if o.startswith(pick)), None)
            assert match, f"{pick!r} not in {options}"
            return match
        platforms = [list(p) for p in platforms]

        def choose_many(label, options):
            wanted = platforms.pop(0) if platforms else ["iOS app"]
            assert all(w in options for w in wanted), f"{wanted} not in {options}"
            return wanted
        result = cli.run(ask=ask, choose=choose, choose_many=choose_many, confirm=lambda label, default: confirms.pop(0) if confirms else default, out=said.append,
                         github_state=lambda: states.pop(0) if len(states) > 1 else states[0], github_login=logins or (lambda: None))
        return result, asked, said

    def local_answers(self):
        # name, pitch, audience, problem(area: 2 lines + blank), features(area: 2 lines + blank), stack(blank), done(blank), look(blank), not(blank), parent
        return ["Pocket Notes", "A tiny notes app.", "Busy people", "Notes apps are heavy.", "", "Write a note", "Search notes", "", "", "", "", "", str(self.home)]

    def test_local_only_flow_creates_the_project_and_returns_it_for_the_wizard(self):
        (code, root), asked, said = self.drive(self.local_answers(), ["Local only"], [{"installed": False, "user": None}])
        self.assertEqual(code, 0)
        self.assertEqual(root.name, "Pocket-Notes")
        text = (root / "docs" / "product" / "prd.md").read_text()
        self.assertIn("- Write a note", prd.split(text)["features"])
        self.assertIsNone(np.load_draft())

    def test_a_new_project_gets_one_product_requirements_doc_that_every_ai_reads_first(self):
        with patch.object(np, "publish_to_github"):
            result = np.create_project({"answers": ANSWERS, "parent": str(self.home), "host": "local"})
        root = Path(result["root"])
        self.assertTrue((root / "docs" / "product" / "prd.md").is_file())
        self.assertFalse((root / "docs" / "product-brief.md").exists())  # no separate brief or six scaffolds any more
        self.assertEqual(sorted(p.name for p in (root / "docs" / "product").iterdir()), ["prd.md"])
        agents = (root / "AGENTS.md").read_text()
        self.assertIn("docs/product/prd.md", agents)
        self.assertIn("one working end-to-end slice at a time", agents)
        self.assertIn("Not this", agents)
        context = prd.context_block(root)
        self.assertIn("### Pitch", context)
        self.assertNotIn("### Look and feel", context)  # unfilled sections are not context

    def test_creation_result_lists_what_the_chosen_platforms_need(self):
        with patch.object(np, "publish_to_github"):
            result = np.create_project({"answers": {**ANSWERS, "platform": f"iOS app, Web app, {np.RECOMMEND}"}, "parent": str(self.home), "host": "local"})
        self.assertEqual([n["platform"] for n in result["platform_needs"]], ["iOS app", "Web app"])
        self.assertTrue(result["recommend_platform"])

    def test_several_platforms_chosen_in_the_terminal_are_saved_together(self):
        (code, root), _, _ = self.drive(self.local_answers(), ["Local only"], [{"installed": False, "user": None}], platforms=(("iOS app", "Backend / API"),))
        self.assertEqual(code, 0)
        self.assertEqual(prd.platforms((root / "docs" / "product" / "prd.md").read_text())[0], ["iOS app", "Backend / API"])

    def test_github_chosen_but_not_signed_in_saves_progress_and_resumes_to_finish(self):
        # first run: choose GitHub, can't sign in, stop
        (code, root), _, said = self.drive(self.local_answers()[:-1], ["GitHub", "Stop"], [{"installed": True, "user": None}])
        self.assertEqual((code, root), (0, None))
        draft = np.load_draft()
        self.assertEqual(draft["answers"]["name"], "Pocket Notes")
        self.assertTrue(draft["waiting_on_github"])
        self.assertTrue(any("Progress saved" in line for line in said))
        # second run: nothing is asked again; signed in now, so it creates the project + repo
        with patch.object(np, "publish_to_github", return_value={"name": "Create the GitHub repository", "ok": True, "detail": "u", "url": "u"}):
            (code, root), asked, _ = self.drive([str(self.home)], ["GitHub", "Only me"], [{"installed": True, "user": "me"}], confirms=[True])
        self.assertEqual(code, 0)
        self.assertNotIn("What's it called?", asked)
        self.assertTrue(root.exists())
        self.assertIsNone(np.load_draft())

    def test_signing_in_from_the_prompt_then_continuing(self):
        states = [{"installed": True, "user": None}, {"installed": True, "user": "me"}]
        logins = []
        with patch.object(np, "publish_to_github", return_value={"name": "Create the GitHub repository", "ok": True, "detail": "u"}):
            (code, root), _, said = self.drive(self.local_answers(), ["GitHub", "Sign in", "Only me"], states, logins=lambda: logins.append(1))
        self.assertEqual(code, 0)
        self.assertEqual(logins, [1])
        self.assertTrue(any("Signed in to GitHub as me" in line for line in said))

    def test_github_publish_failure_leaves_a_resumable_local_project(self):
        fail = {"name": "Create the GitHub repository", "ok": False, "detail": "name taken"}
        with patch.object(np, "publish_to_github", return_value=fail):
            (code, root), _, said = self.drive(self.local_answers(), ["GitHub", "Only me"], [{"installed": True, "user": "me"}])
        self.assertTrue(root.exists())
        self.assertEqual(np.load_draft()["created_root"], str(root))
        self.assertTrue(any("finish GitHub" in line for line in said))
        # next run only does GitHub, and doesn't ask the questions again
        with patch.object(np, "publish_to_github", return_value={**fail, "ok": True, "detail": "u"}):
            (code2, root2), asked, _ = self.drive([], [], [{"installed": True, "user": "me"}], confirms=[True])
        self.assertEqual((code2, root2), (0, root))
        self.assertEqual(asked, [])
        self.assertIsNone(np.load_draft())

    def test_declining_to_resume_starts_clean(self):
        np.save_draft({"answers": {"name": "Old idea"}, "step": "where"})
        (code, root), asked, _ = self.drive(self.local_answers(), ["Local only"], [{"installed": False, "user": None}], confirms=[False])
        self.assertEqual(root.name, "Pocket-Notes")
        self.assertIn("What's it called?", asked)
