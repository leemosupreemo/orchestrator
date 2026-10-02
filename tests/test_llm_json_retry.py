from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "orchestrator" / "scripts"))

import llm  # noqa: E402
from model_router import ModelRole  # noqa: E402

MODEL = "opencode/nemotron-3-ultra-free"
PROSE = "I analysed the code and saved the plan to plan.json. One clarification: should length count punctuation?"
PLAN = '```json\n{"title": "Add bonus", "tasks": []}\n```'


class JsonRetryTests(unittest.TestCase):
    def run_llm(self, replies, role=ModelRole.PLANNER, allowed=None):
        prompts = []

        def fake(model, prompt, cwd, timeout, role=None, session_id=None):
            prompts.append(prompt)
            return replies.pop(0)

        with patch.object(llm, "_run_llm_single", side_effect=fake):
            return llm.run_llm(MODEL, "PLAN IT", allowed_models=allowed or [MODEL], role=role), prompts

    def test_a_model_that_answers_in_prose_is_asked_once_to_restate_it_as_json(self):
        (out, model, _), prompts = self.run_llm([PROSE, PLAN])
        self.assertEqual((out, model), (PLAN, MODEL))
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[0], "PLAN IT")
        self.assertTrue(prompts[1].startswith("PLAN IT"))
        self.assertIn("FORMAT CORRECTION", prompts[1])
        self.assertIn("Do not write it to a file", prompts[1])

    def test_valid_json_costs_no_extra_call(self):
        (_, _, _), prompts = self.run_llm([PLAN])
        self.assertEqual(len(prompts), 1)

    def test_a_model_that_still_fails_after_the_retry_is_reported_not_looped(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_llm([PROSE, PROSE, PROSE])
        self.assertIn("produced invalid JSON", str(ctx.exception))

    def test_roles_that_do_not_need_json_are_left_alone(self):
        (out, _, _), prompts = self.run_llm([PROSE], role=ModelRole.REVIEWER)
        self.assertEqual((out, len(prompts)), (PROSE, 1))


if __name__ == "__main__":
    unittest.main()
