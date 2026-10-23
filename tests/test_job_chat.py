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



class WebResearchRunTests(unittest.TestCase):
    """job_chat_run.py --web: the same one-turn script, with a model that can search when one is available."""

    def run_script(self, candidates, args=("--web",)):
        import io
        from contextlib import redirect_stdout
        scripts = Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"
        sys.path.insert(0, str(scripts))
        import job_chat_run
        out = io.StringIO()
        with patch.object(job_chat_run, "get_prioritized_models", return_value=candidates), \
                patch.object(job_chat_run, "run_llm", return_value=("the answer", "m", "s")) as llm_call, \
                patch.object(sys, "stdin", io.StringIO("prompt")), redirect_stdout(out):
            code = job_chat_run.main(list(args))
        return code, out.getvalue(), llm_call

    def test_it_picks_a_model_that_can_search_and_says_so(self):
        code, out, llm_call = self.run_script(["deepseek", "claude-sonnet-4-6"])
        self.assertEqual(code, 0)
        self.assertEqual(llm_call.call_args.args[0], "claude-sonnet-4-6")  # not the first, which can't search
        self.assertTrue(llm_call.call_args.kwargs["web"])
        self.assertLess(out.index("<<<ORCHESTRATOR-WEB>>>on"), out.index("<<<ORCHESTRATOR-REPLY>>>"))
        self.assertTrue(out.rstrip().endswith("the answer"))

    def test_with_no_model_that_can_search_it_still_answers_and_says_it_did_not(self):
        code, out, llm_call = self.run_script(["deepseek"])
        self.assertEqual(code, 0)
        self.assertEqual(llm_call.call_args.args[0], "deepseek")
        self.assertFalse(llm_call.call_args.kwargs["web"])
        self.assertIn("<<<ORCHESTRATOR-WEB>>>off", out)

    def test_without_the_flag_nothing_searches(self):
        _, out, llm_call = self.run_script(["claude-sonnet-4-6"], args=())
        self.assertFalse(llm_call.call_args.kwargs["web"])
        self.assertIn("<<<ORCHESTRATOR-WEB>>>off", out)


if __name__ == "__main__":
    unittest.main()
