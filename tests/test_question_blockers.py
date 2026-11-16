"""A paused job's "question" that is really a blocker (no write access, a sign-in, a missing tool) is shown as a blocker with a way
on, not as a question to answer: no answer can fix it."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import blocker_fix

from orchestrator.web import server as ui

STATIC = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"
PROMPTS = Path(__file__).resolve().parents[1] / "orchestrator" / "prompts"
WRITE_ACCESS = ("Rerun this task with workspace write access so the mandatory failing test, implementation, documentation, "
                "and screenshot inventory can be created.")


class QuestionBlockerTests(unittest.TestCase):
    def test_a_blocker_saved_before_planning_offers_to_plan_again_not_to_answer(self):
        state = ui.job_state({"status": "human-needed", "human_clarification_question": WRITE_ACCESS})
        self.assertEqual(state["label"], "The AI couldn't continue")
        self.assertIn("planning doesn't need it", state["reason"])  # fixed by the button itself: nothing for the person to do
        self.assertEqual((state["next"]["action"], state["next"]["label"]), ("revise", "Plan again"))
        self.assertIn("don't need write access", state["next"]["params"]["change"])  # the re-plan tells the planner so
        self.assertEqual(state["steps"], [])
        self.assertEqual(state["blocker"], WRITE_ACCESS)

    def test_a_blocker_during_the_build_offers_to_try_again(self):
        state = ui.job_state({"status": "human-needed", "human_clarification_question": "Permission denied writing App/Main.swift",
                              "plan": {"tasks": [{"title": "Build it"}]}})
        self.assertEqual((state["next"]["action"], state["next"]["label"]), ("resume", "Try again"))

    def test_a_blocker_that_is_still_there_gives_the_steps_to_fix_it(self):
        planned = {"plan": {"tasks": [{"title": "Build it"}]}}
        with tempfile.TemporaryDirectory() as folder, mock.patch("os.access", return_value=False):
            fix = blocker_fix.diagnose("Permission denied writing App/Main.swift", planned, Path(folder))
        self.assertIn("can't write to the project folder", fix["reason"])
        self.assertTrue(fix["steps"] and "Read & Write" in fix["steps"][0])
        fixed = blocker_fix.diagnose("Permission denied writing App/Main.swift", planned, Path(tempfile.gettempdir()))
        self.assertEqual((fixed["steps"], fixed["action"]), ([], "resume"))  # writable now: just try again

    def test_a_missing_tool_is_checked_before_asking_to_install_it(self):
        with mock.patch("shutil.which", return_value=None):
            missing = blocker_fix.diagnose("xcbeautify: command not found", {})
        self.assertIn("needs xcbeautify", missing["reason"])
        self.assertEqual(missing["steps"][0], "Install xcbeautify.")
        with mock.patch("shutil.which", return_value="/opt/homebrew/bin/xcbeautify"):
            self.assertEqual(blocker_fix.diagnose("xcbeautify: command not found", {})["steps"], [])

    def test_an_expired_sign_in_offers_sign_in_unless_done_since(self):
        job = {"updated_at": "2026-01-01T00:00:00Z"}
        with mock.patch.object(blocker_fix.job_problem, "signed_in_at", return_value=0.0):
            fix = blocker_fix.diagnose("Claude: OAuth access token has expired", job)
        self.assertEqual(fix["sign_in"], {"provider": "claude", "name": "Claude"})
        with mock.patch.object(blocker_fix.job_problem, "signed_in_at", return_value=4_000_000_000.0):
            self.assertIsNone(blocker_fix.diagnose("Claude: OAuth access token has expired", job)["sign_in"])

    def test_planners_are_told_not_to_ask_for_write_access(self):
        for name in ("planner_feature.md", "planner_bug.md", "planner_coverage.md"):
            self.assertIn("Never use `clarification_needed` to ask for write access", (PROMPTS / name).read_text(encoding="utf-8"))

    def test_a_real_question_is_still_asked_and_says_who_asks(self):
        planner = ui.job_state({"status": "human-needed", "human_clarification_question": "Should the timer reset on rematch?"})
        self.assertEqual((planner["label"], planner["next"]["action"]), ("Question for you", "answer"))
        self.assertIn("The planner needs an answer", planner["reason"])
        builder = ui.job_state({"status": "human-needed", "human_clarification_question": "Which icon set should I use?",
                                "last_error": "Builder clarification needed: Which icon set should I use?"})
        self.assertIn("The builder needs an answer", builder["reason"])

    def test_the_question_card_is_not_shown_for_a_blocker(self):
        self.assertTrue(ui.question_is_blocker(WRITE_ACCESS))
        self.assertFalse(ui.question_is_blocker("Should the timer reset on rematch?"))
        self.assertFalse(ui.question_is_blocker(None))
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('"A question during the build" : "A question before planning"', app)
        # a dialog that can't load its data shows the error, so Answer never seems to do nothing
        self.assertIn("return Promise.resolve().then(() => dialogs[action](params)).catch((err) => toast(", app)
        self.assertIn("job.summary?.question || job.job?.human_clarification_question", app)
        # opened anyway (an old page): Answer does the blocker's fix instead of asking
        self.assertIn("if (job.summary?.state?.blocker && fix) {", app)
        self.assertIn("act(next.action, { job: s.id, ...(next.params || {}) })", app)
        self.assertIn('if (params?.change) return runAction("revise"', app)


if __name__ == "__main__":
    unittest.main()
