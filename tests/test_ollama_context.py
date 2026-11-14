"""Local Ollama models and long prompts: the window is sized to the prompt instead of silently cutting its start.

A stand-in Ollama server records what it is asked for, so what is checked is the real request: the whole prompt, and a window big
enough for it."""
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "orchestrator" / "scripts"))

from orchestrator import ollama_api  # noqa: E402
from orchestrator.scripts import ollama_generate  # noqa: E402


class FakeOllama:
    """A tiny Ollama: /api/show reports a context length, /api/generate streams a reply (or an error) and remembers the request."""

    def __init__(self, context_length=16384, reply=("Hello", " world"), error=None):
        self.requests, self.context_length, self.reply, self.error = [], context_length, reply, error
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body))
                if self.path == "/api/show":
                    info = {"model_info": {"llama.context_length": outer.context_length}} if outer.context_length else {"model_info": {}}
                    return self._send(200, json.dumps(info).encode())
                if outer.error:
                    return self._send(404, json.dumps({"error": outer.error}).encode())
                lines = [json.dumps({"response": part, "done": False}) for part in outer.reply] + [json.dumps({"response": "", "done": True})]
                self._send(200, ("\n".join(lines) + "\n").encode())

            def _send(self, status, data):
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def generate_requests(self):
        return [body for path, body in self.requests if path == "/api/generate"]


class OllamaTestCase(unittest.TestCase):
    def setUp(self):
        ollama_api._CACHE.clear()
        self.addCleanup(ollama_api._CACHE.clear)

    def serve(self, **kw) -> FakeOllama:
        fake = FakeOllama(**kw)
        self.addCleanup(fake.close)
        patcher = patch.dict(os.environ, {"OLLAMA_HOST": fake.address})
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


class WindowSizingTests(OllamaTestCase):
    def test_the_window_is_the_smallest_step_that_holds_the_prompt_and_an_answer(self):
        self.assertEqual([ollama_api.num_ctx_for(c, 32768) for c in (500, 7_000, 9_000, 24_000, 60_000, 400_000)], [4096, 4096, 8192, 16384, 32768, 32768])

    def test_it_never_asks_for_more_than_the_model_supports(self):
        self.assertEqual(ollama_api.num_ctx_for(24_000, 16384), 16384)
        self.assertEqual(ollama_api.num_ctx_for(24_000, 8192), 8192)
        self.assertEqual(ollama_api.num_ctx_for(24_000, 2048), 2048)  # a model with a tiny window gets exactly its own

    def test_it_never_asks_for_more_than_memory_should_be_spent_on(self):
        self.assertEqual(ollama_api.num_ctx_for(900_000, 262144), 32768)

    def test_the_longest_prompt_ollama_run_is_trusted_with_leaves_room_in_the_default_window(self):
        tokens = ollama_api.RUN_MAX_CHARS / ollama_api.CHARS_PER_TOKEN + ollama_api.ANSWER_ROOM
        self.assertLess(tokens, ollama_api.DEFAULT_WINDOW)

    def test_the_models_own_limit_is_read_from_the_server_and_remembered(self):
        fake = self.serve(context_length=16384)
        self.assertEqual(ollama_api.context_length("deepseek-coder"), 16384)
        self.assertEqual(ollama_api.context_length("deepseek-coder"), 16384)
        self.assertEqual(len([r for r in fake.requests if r[0] == "/api/show"]), 1)

    def test_an_unreachable_server_or_a_model_that_does_not_say_is_just_unknown(self):
        with patch.dict(os.environ, {"OLLAMA_HOST": "127.0.0.1:9"}):
            self.assertIsNone(ollama_api.context_length("x", timeout=0.5))
        ollama_api._CACHE.clear()
        self.serve(context_length=None)
        self.assertIsNone(ollama_api.context_length("y"))

    def test_how_much_prompt_a_local_model_can_take_whole(self):
        self.serve(context_length=16384)
        self.assertEqual(ollama_api.window_chars("deepseek-coder"), (16384 - ollama_api.ANSWER_ROOM) * ollama_api.CHARS_PER_TOKEN)
        ollama_api._CACHE.clear()
        self.assertEqual(ollama_api.window_chars("deepseek-coder", env={"OLLAMA_HOST": "127.0.0.1:9"}, timeout=0.5), None)

    def test_a_model_that_supports_a_huge_window_is_capped(self):
        self.serve(context_length=1_000_000)
        self.assertEqual(ollama_api.window_chars("big"), (32768 - ollama_api.ANSWER_ROOM) * ollama_api.CHARS_PER_TOKEN)

    def test_which_registry_models_run_through_ollama(self):
        self.assertEqual(ollama_api.model_for("deepseek"), "deepseek-coder")
        self.assertEqual(ollama_api.model_for("qwen2.5-coder", ["ollama"], "qwen"), "qwen2.5-coder")
        self.assertEqual(ollama_api.model_for("qwen3:8b", ["ollama"], "ollama"), "qwen3:8b")
        self.assertIsNone(ollama_api.model_for("opencode/beam-free", ["opencode"], "opencode"))
        self.assertIsNone(ollama_api.model_for("claude-sonnet-4-6", ["claude"], "claude"))

    def test_the_server_address_accepts_the_forms_ollama_host_comes_in(self):
        for host, expected in ((None, "http://127.0.0.1:11434"), ("0.0.0.0", "http://0.0.0.0:11434"), ("box:9999", "http://box:9999"),
                               ("https://box.local", "https://box.local:11434"), ("[::1]:5000", "http://[::1]:5000")):
            self.assertEqual(ollama_api.base_url({"OLLAMA_HOST": host} if host else {}), expected)


class StreamTests(OllamaTestCase):
    def test_the_whole_prompt_is_sent_with_the_window_asked_for(self):
        fake = self.serve()
        prompt = "A" * 24_000
        self.assertEqual("".join(ollama_api.stream("deepseek-coder", prompt, 16384)), "Hello world")
        sent = fake.generate_requests()[0]
        self.assertEqual((sent["model"], len(sent["prompt"]), sent["options"]["num_ctx"], sent["stream"]), ("deepseek-coder", 24_000, 16384, True))

    def test_an_error_ollama_gives_is_raised_in_its_own_words(self):
        self.serve(error="model 'nope' not found")
        with self.assertRaisesRegex(ollama_api.OllamaError, "model 'nope' not found"):
            list(ollama_api.stream("nope", "hi", 4096))

    def test_an_unreachable_server_raises_a_connection_error_not_an_ollama_error(self):
        with patch.dict(os.environ, {"OLLAMA_HOST": "127.0.0.1:9"}), self.assertRaises((urllib.error.URLError, OSError)):
            list(ollama_api.stream("x", "hi", 4096, timeout=0.5))


class GenerateScriptTests(OllamaTestCase):
    def run_script(self, prompt, *extra, **kw):
        out, err = io.StringIO(), io.StringIO()
        code = ollama_generate.main(["--model", "deepseek-coder", *extra], stdin=io.StringIO(prompt), stdout=out, stderr=err, **kw)
        return code, out.getvalue(), err.getvalue()

    def test_a_project_sized_prompt_gets_a_window_that_holds_it(self):
        fake = self.serve(context_length=16384)
        code, out, err = self.run_script("P" * 24_000)
        self.assertEqual((code, out, err), (0, "Hello world\n", ""))
        sent = fake.generate_requests()[0]
        self.assertEqual(len(sent["prompt"]), 24_000)  # none of it dropped on the way
        self.assertEqual(sent["options"]["num_ctx"], 16384)  # the default 4096 would have cut the project off

    def test_a_window_can_be_forced(self):
        fake = self.serve()
        self.run_script("hi", "--num-ctx", "12288")
        self.assertEqual(fake.generate_requests()[0]["options"]["num_ctx"], 12288)

    def test_when_ollama_refuses_its_message_is_shown_and_nothing_else_is_tried(self):
        self.serve(error="model requires more system memory")
        ran = []
        code, out, err = self.run_script("hi", runner=lambda *a, **k: ran.append(a))
        self.assertEqual((code, out, ran), (1, "", []))
        self.assertEqual(err.strip(), "model requires more system memory")

    def test_when_the_server_cannot_be_reached_ollama_run_gets_the_same_prompt(self):
        calls = []

        class Done:
            returncode = 0

        def runner(argv, **kw):
            calls.append((argv, kw))
            return Done()
        with patch.dict(os.environ, {"OLLAMA_HOST": "127.0.0.1:9"}):
            code, out, err = self.run_script("the whole prompt", runner=runner, context=lambda m: None)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][0], ["ollama", "run", "deepseek-coder", "--nowordwrap"])
        self.assertEqual(calls[0][1]["input"], "the whole prompt")

    def test_a_connection_that_drops_mid_answer_does_not_print_a_second_answer(self):
        def dropping(model, prompt, num_ctx, **kw):
            yield "partial "
            raise urllib.error.URLError("connection reset")
        ran = []
        code, out, err = self.run_script("hi", stream=dropping, runner=lambda *a, **k: ran.append(a), context=lambda m: 4096)
        self.assertEqual((code, out, ran), (1, "partial ", []))
        self.assertIn("dropped", err)


class CommandRoutingTests(unittest.TestCase):
    def command(self, model: str, size: int, exists: bool = True) -> str:
        import llm
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prompt.txt"
            if exists:
                path.write_text("x" * size)
            return llm.get_llm_command(model, str(path))

    def test_a_prompt_that_fits_the_default_window_runs_exactly_as_before(self):
        for model, name in (("deepseek", "deepseek-coder"), ("qwen2.5-coder", "qwen2.5-coder")):
            with self.subTest(model=model):
                cmd = self.command(model, ollama_api.RUN_MAX_CHARS)
                self.assertIn(f"ollama run {name} --nowordwrap", cmd)
                self.assertNotIn("ollama_generate", cmd)

    def test_a_longer_prompt_goes_through_the_api_with_a_bigger_window(self):
        for model, name in (("deepseek", "deepseek-coder"), ("qwen2.5-coder", "qwen2.5-coder")):
            with self.subTest(model=model):
                cmd = self.command(model, ollama_api.RUN_MAX_CHARS + 1)
                self.assertIn("-m orchestrator script ollama_generate.py --model " + name, cmd)
                self.assertNotIn("ollama run", cmd)

    def test_a_prompt_file_that_cannot_be_measured_falls_back_to_ollama_run(self):
        self.assertIn("ollama run deepseek-coder --nowordwrap", self.command("deepseek", 0, exists=False))

    def test_other_tools_are_not_affected(self):
        self.assertNotIn("ollama_generate", self.command("opencode/beam-free", 50_000))


if __name__ == "__main__":
    unittest.main()
