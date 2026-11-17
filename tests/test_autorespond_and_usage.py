"""Keeping the AI going: a tool that runs out of usage is set aside until it resets, every job switches to the next one with
a note that the work is already started, and in YOLO mode Orchestrator answers the AI's questions for the person (a few
times a job, then it waits). Model calls are faked; nothing reaches a real model."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "orchestrator" / "scripts"))

import common  # noqa: E402
import llm  # noqa: E402
from model_router import ModelRole  # noqa: E402
from orchestrator import project_chat, usage_limits  # noqa: E402

CLAUDE, OTHER_CLAUDE, OPENAI = "claude-opus-4-8", "claude-sonnet-4-6", "gpt-5.5"
LIMIT = "Claude AI usage limit reached. Try again in 2 hours 30 minutes."


class StateDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)


class UsageLimitTests(StateDirCase):
    def test_the_tools_own_words_are_recognised_and_other_errors_are_not(self):
        for text in (LIMIT, "You've hit your usage limit. Upgrade or try later.", "Error: insufficient credits", "429 Too Many Requests"):
            self.assertTrue(usage_limits.detect(text), text)
        for text in ("Invalid API key", "SyntaxError: unexpected token", "model not found"):
            self.assertEqual(usage_limits.detect(text), "", text)

    def test_when_it_is_back_comes_from_the_message(self):
        now = datetime(2026, 11, 16, 13, 0).timestamp()
        self.assertEqual(usage_limits.until(LIMIT, now), now + 2.5 * 3600)
        self.assertEqual(datetime.fromtimestamp(usage_limits.until("Your limit will reset at 3pm.", now)).hour, 15)
        self.assertEqual(datetime.fromtimestamp(usage_limits.until("resets at 9am", now)).day, 17)  # tomorrow morning
        self.assertEqual(usage_limits.until("usage limit reached", now), now + usage_limits.DEFAULT_USAGE_HOLD)
        self.assertEqual(usage_limits.until("429 Too Many Requests", now), now + usage_limits.DEFAULT_RATE_HOLD)

    def test_the_whole_tool_is_set_aside_until_it_resets_then_comes_back(self):
        now = time.time()
        hold = project_chat.record_out_of_usage(CLAUDE, LIMIT, now=now)
        self.assertEqual((hold["family"], hold["kind"]), ("claude", "usage"))
        for model in (CLAUDE, OTHER_CLAUDE):  # a Claude limit is the account's, not one model's
            self.assertTrue(project_chat.is_model_blocked(model, now=now + 60), model)
        self.assertFalse(project_chat.is_model_blocked(OPENAI, now=now + 60))
        self.assertEqual([h["family"] for h in project_chat.usage_holds(now=now + 60)], ["claude"])
        self.assertFalse(project_chat.is_model_blocked(CLAUDE, now=now + 3 * 3600))  # back in rotation by itself
        self.assertEqual(project_chat.usage_holds(now=now + 3 * 3600), [])


class SwitchingTests(StateDirCase):
    def run_llm(self, replies, role=ModelRole.BUILDER):
        prompts = []

        def fake(model, prompt, cwd, timeout, role=None, session_id=None, images=None, **_options):
            prompts.append((model, prompt))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with patch.object(llm, "_run_llm_single", side_effect=fake), \
             patch.object(llm, "get_prioritized_models", return_value=[CLAUDE, OTHER_CLAUDE, OPENAI]):
            result = llm.run_llm(CLAUDE, "BUILD IT", allowed_models=[CLAUDE, OTHER_CLAUDE, OPENAI], role=role)
        return result, prompts

    def test_out_of_usage_moves_to_another_tool_with_the_work_handed_over(self):
        (out, model, _), prompts = self.run_llm([RuntimeError(LIMIT), '```json\n{"summary": "done"}\n```'])
        self.assertEqual(model, OPENAI)
        self.assertEqual([m for m, _ in prompts], [CLAUDE, OPENAI])  # the other Claude model is skipped: same account
        self.assertNotIn("HANDOFF", prompts[0][1])
        self.assertIn("### HANDOFF ###", prompts[1][1])
        self.assertIn(f"Another model ({CLAUDE})", prompts[1][1])
        self.assertIn("git diff", prompts[1][1])
        self.assertTrue(project_chat.out_of_usage(CLAUDE))  # remembered for every job, not only this call

    def test_a_planner_gets_no_handoff_note(self):
        _, prompts = self.run_llm([RuntimeError(LIMIT), '```json\n{"title": "x", "tasks": []}\n```'], role=ModelRole.PLANNER)
        self.assertNotIn("HANDOFF", prompts[1][1])  # nothing was half done in the files


class AutorespondTests(unittest.TestCase):
    def test_yolo_answers_up_to_three_times_then_waits(self):
        job = {"yolo": True}
        for n in range(common.MAX_AUTO_RESPONSES):
            self.assertTrue(common.can_autorespond(job))
            common.autorespond(job, f"Which icon set should I use? ({n})", "builder")
        self.assertFalse(common.can_autorespond(job))
        entry = job["clarification_history"][0]
        self.assertEqual((entry["auto"], entry["asker"]), (True, "builder"))
        self.assertEqual(entry["answer"], common.AUTO_ANSWER)
        self.assertFalse(common.can_autorespond({"clarification_history": []}))  # not YOLO: a person answers

    def test_should_i_continue_is_answered_carry_on(self):
        self.assertEqual(common.auto_answer("Should I continue with the remaining files?"), common.AUTO_CONTINUE)
        self.assertEqual(common.auto_answer("Would you like me to proceed?"), common.AUTO_CONTINUE)
        self.assertEqual(common.auto_answer("Which database should the cache use?"), common.AUTO_ANSWER)

    def test_the_planner_builder_and_page_use_it(self):
        worker = (ROOT_DIR / "orchestrator" / "scripts" / "worker_run.py").read_text(encoding="utf-8")
        self.assertEqual(worker.count("if can_autorespond(job):  # YOLO"), 2)  # one task, and each task of a plan
        planner = (ROOT_DIR / "orchestrator" / "scripts" / "new_job.py").read_text(encoding="utf-8")
        self.assertIn('auto = not ask_person and can_autorespond({"yolo": yolo_on, "clarification_history": clarification_history})', planner)
        app = (ROOT_DIR / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("🤖 Answered for you (YOLO)", app)  # never shown as the person's own answer
        self.assertIn("${autoresponseNote(job)}", app)
        self.assertIn("is out of\n      ${h.kind === \"rate\" ? \"requests for now\" : \"usage\"}</strong> until about", app)


if __name__ == "__main__":
    unittest.main()
