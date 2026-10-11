from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import job_chat  # noqa: E402


def read(path: Path):
    return json.loads(path.read_text())


def write(path: Path, data):
    path.write_text(json.dumps(data))


class JobChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "job.json"
        write(self.path, {"title": "Fix lobby", "status": "review-needed", "tasks": ["a", "b"], "completed_tasks": ["0"]})

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, question, llm):
        return job_chat.ask(self.path, question, llm, self.root, read, write)

    def test_persists_both_messages_on_the_job(self):
        thread = self.ask("Why?", lambda prompt, model: " Because. ")
        self.assertEqual([m["role"] for m in thread], ["user", "assistant"])
        self.assertEqual(read(self.path)["conversation"][1]["text"], "Because.")

    def test_prompt_has_job_context_and_prior_turns(self):
        prompts = []

        def llm(prompt, model):
            prompts.append(prompt)
            return "ok"

        self.ask("first", llm)
        self.ask("second", llm)
        self.assertIn("Fix lobby", prompts[1])
        self.assertIn("- [x] a", prompts[1])
        self.assertIn("- [ ] b", prompts[1])
        self.assertIn("User: first", prompts[1])
        self.assertIn("second", prompts[1])

    def test_uses_the_jobs_reviewer_model(self):
        write(self.path, {**read(self.path), "reviewer": "model-x"})
        seen = []
        self.ask("hi", lambda prompt, model: seen.append(model) or "ok")
        self.assertEqual(seen, ["model-x"])

    def test_rejects_empty_and_oversized_questions(self):
        with self.assertRaises(job_chat.ChatError):
            self.ask("   ", lambda p, m: "x")
        with self.assertRaises(job_chat.ChatError):
            self.ask("x" * (job_chat.MAX_MESSAGE_CHARS + 1), lambda p, m: "x")

    def test_empty_answer_is_an_error_and_saves_nothing(self):
        with self.assertRaises(job_chat.ChatError):
            self.ask("hi", lambda p, m: "  ")
        self.assertNotIn("conversation", read(self.path))

    def test_job_updates_during_the_model_call_are_kept(self):
        def llm(prompt, model):
            write(self.path, {**read(self.path), "status": "completed"})  # a worker finishing meanwhile
            return "ok"

        self.ask("hi", llm)
        job = read(self.path)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(job["conversation"]), 2)

    def test_model_failure_leaves_the_job_untouched(self):
        def llm(prompt, model):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.ask("hi", llm)
        self.assertNotIn("conversation", read(self.path))


if __name__ == "__main__":
    unittest.main()
