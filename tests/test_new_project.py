from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import new_project as np
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


class BriefTests(unittest.TestCase):
    def test_brief_captures_every_answer_and_numbers_the_features(self):
        md = np.render_brief(ANSWERS)
        for needle in ("# Word Duel: product brief", "A turn-based word game.", "Friends who like games", "Async games are clunky.",
                       "1. Start a match", "2. Take turns", "3. See scores", "iOS app", "SwiftUI", "Two phones can finish a match", "Out of scope"):
            self.assertIn(needle, md)

    def test_optional_sections_are_left_out_when_blank(self):
        md = np.render_brief({**ANSWERS, "stack": "", "done": ""})
        self.assertNotIn("Technology preferences", md)
        self.assertNotIn("How we'll know", md)

    def test_only_the_lean_questions_are_required(self):
        self.assertEqual(len(np.REQUIRED), 6)
        self.assertEqual(np.missing_answers({"name": "x"}), [q["label"] for q in np.QUESTIONS if q["required"] and q["key"] != "name"])


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
        self.assertIn("Word Duel", (root / "docs" / "product-brief.md").read_text())
        self.assertIn("docs/product-brief.md", (root / "AGENTS.md").read_text())
        self.assertTrue(all(s["ok"] for s in steps), steps)
        log = subprocess.run(["git", "log", "--oneline"], cwd=root, capture_output=True, text=True).stdout
        self.assertIn("product brief", log)

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
        self.assertTrue(Path(result["root"], "docs", "product-brief.md").exists())
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
        root = None

        def set_root(self, root):
            self.root = root

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

    def drive(self, answers, picks, gh_states, confirms=(True,), logins=None):
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
        result = cli.run(ask=ask, choose=choose, confirm=lambda label, default: confirms.pop(0) if confirms else default, out=said.append,
                         github_state=lambda: states.pop(0) if len(states) > 1 else states[0], github_login=logins or (lambda: None))
        return result, asked, said

    def local_answers(self):
        # name, pitch, audience, problem(area: 2 lines + blank), features(area: 2 lines + blank), stack(blank), done(blank), parent
        return ["Pocket Notes", "A tiny notes app.", "Busy people", "Notes apps are heavy.", "", "Write a note", "Search notes", "", "", "", str(self.home)]

    def test_local_only_flow_creates_the_project_and_returns_it_for_the_wizard(self):
        (code, root), asked, said = self.drive(self.local_answers(), ["iOS app", "Local only"], [{"installed": False, "user": None}])
        self.assertEqual(code, 0)
        self.assertEqual(root.name, "Pocket-Notes")
        brief = (root / "docs" / "product-brief.md").read_text()
        self.assertIn("1. Write a note", brief)
        self.assertIsNone(np.load_draft())

    def test_github_chosen_but_not_signed_in_saves_progress_and_resumes_to_finish(self):
        # first run: choose GitHub, can't sign in, stop
        (code, root), _, said = self.drive(self.local_answers()[:-1], ["iOS app", "GitHub", "Stop"], [{"installed": True, "user": None}])
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
            (code, root), _, said = self.drive(self.local_answers(), ["iOS app", "GitHub", "Sign in", "Only me"], states, logins=lambda: logins.append(1))
        self.assertEqual(code, 0)
        self.assertEqual(logins, [1])
        self.assertTrue(any("Signed in to GitHub as me" in line for line in said))

    def test_github_publish_failure_leaves_a_resumable_local_project(self):
        fail = {"name": "Create the GitHub repository", "ok": False, "detail": "name taken"}
        with patch.object(np, "publish_to_github", return_value=fail):
            (code, root), _, said = self.drive(self.local_answers(), ["iOS app", "GitHub", "Only me"], [{"installed": True, "user": "me"}])
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
        (code, root), asked, _ = self.drive(self.local_answers(), ["iOS app", "Local only"], [{"installed": False, "user": None}], confirms=[False])
        self.assertEqual(root.name, "Pocket-Notes")
        self.assertIn("What's it called?", asked)
