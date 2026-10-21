from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "orchestrator" / "scripts"))

import llm  # noqa: E402


class LLMEnvironmentAndQuotaTests(unittest.TestCase):
    def test_get_llm_env_populates_path_with_local_tools(self):
        env = llm.get_llm_env()
        self.assertIn("PATH", env)
        paths = env["PATH"].split(os.pathsep)
        home = str(Path.home())
        self.assertTrue(any("bin" in p or home in p for p in paths))

    def test_is_quota_error_detection(self):
        self.assertTrue(llm.is_quota_error("google.api_core.exceptions.ResourceExhausted: quota exceeded"))
        self.assertTrue(llm.is_quota_error("rate limit reached for current tier"))
        self.assertTrue(llm.is_quota_error("insufficient credits"))
        self.assertTrue(llm.is_quota_error("invalid api key supplied"))
        self.assertFalse(llm.is_quota_error("SyntaxError: invalid syntax on line 42"))
        self.assertFalse(llm.is_quota_error(""))


class LLMWebSearchTests(unittest.TestCase):
    def test_claude_and_gemini_can_be_allowed_to_search_the_web_and_nothing_else_changes(self):
        plain = llm.get_llm_command("claude-sonnet-4-6", "p.md")
        web = llm.get_llm_command("claude-sonnet-4-6", "p.md", web=True)
        self.assertNotIn("WebSearch", plain)
        self.assertIn('--allowedTools "WebSearch,WebFetch"', web)
        self.assertTrue(web.startswith(plain.split(" | ")[0]) and plain.split(" | ")[1] in web)  # the same command, plus the tool list
        self.assertIn('--allowedTools "WebSearch,WebFetch"', llm.get_llm_command("claude", "p.md", web=True))
        with patch.object(llm, "preferred_cli", return_value="gemini"):
            gemini = llm.get_llm_command("gemini-3-flash-preview", "p.md", web=True)
            self.assertIn("google_web_search,web_fetch", gemini)
            self.assertNotIn("google_web_search", llm.get_llm_command("gemini-3-flash-preview", "p.md"))

    def test_other_models_are_unchanged_and_say_they_cannot_search(self):
        for model in ("deepseek", "opencode/big-pickle", "gpt-5.5-medium"):
            self.assertEqual(llm.get_llm_command(model, "p.md", web=True), llm.get_llm_command(model, "p.md"), model)
            self.assertFalse(llm.web_capable(model), model)
        self.assertTrue(llm.web_capable("claude-sonnet-4-6"))
        with patch.object(llm, "preferred_cli", return_value="agy"):
            self.assertFalse(llm.web_capable("gemini-3-flash-preview"))  # Antigravity has no search switch to turn on

    def test_the_flag_reaches_the_command_from_run_llm(self):
        with patch.object(llm, "_run_llm_single", return_value="ok") as single:
            llm.run_llm("claude-sonnet-4-6", "prompt", web=True)
        self.assertTrue(single.call_args.kwargs["web"])


class CodexLoginRunTests(unittest.TestCase):
    def test_a_model_its_provider_cannot_run_is_swapped_before_it_fails_and_the_new_one_runs_codex(self):
        stand_in = llm.get_model("gpt-5.6-sol") or llm.get_model("claude-sonnet-4-6")
        with patch.object(llm, "model_usable", side_effect=lambda m: m.id != "gpt-5.4"), \
             patch.object(llm, "model_substitute", return_value=llm.get_model("gpt-5.4-mini")), \
             patch.object(llm, "_run_llm_single", return_value="ok") as single:
            llm.run_llm("gpt-5.4", "prompt")
        self.assertEqual(single.call_args.args[0], "gpt-5.4-mini")
        self.assertTrue(stand_in)

    def test_openai_models_not_named_gpt_5_still_get_a_codex_command(self):
        for model in ("gpt-6.1-sol", "o1", "gpt-4.5-preview"):
            self.assertIn("codex exec --model", llm.get_llm_command(model, "p.md") if llm.get_model(model) else "codex exec --model", model)
        self.assertIn("codex exec --model o1", llm.get_llm_command("o1", "p.md"))


class LLMRunSingleTests(unittest.TestCase):
    def _create_mock_popen(self, stdout_lines: list[str], stderr_lines: list[str] = None, returncode: int = 0):
        mock_proc = MagicMock()
        mock_proc.returncode = returncode
        mock_proc.poll.side_effect = [None, returncode]

        r_out, w_out = os.pipe()
        if stdout_lines:
            os.write(w_out, "".join(stdout_lines).encode("utf-8"))
        os.close(w_out)
        stdout_file = os.fdopen(r_out, "r")

        r_err, w_err = os.pipe()
        if stderr_lines:
            os.write(w_err, "".join(stderr_lines).encode("utf-8"))
        os.close(w_err)
        stderr_file = os.fdopen(r_err, "r")

        mock_proc.stdout = stdout_file
        mock_proc.stderr = stderr_file
        return mock_proc

    @patch("subprocess.Popen")
    @patch.object(llm, "append_log")
    def test_run_llm_single_success(self, mock_append, mock_popen):
        expected_json = '{"decision": "proceed", "confidence": 0.95}'
        mock_popen.return_value = self._create_mock_popen([expected_json + "\n"])

        result = llm._run_llm_single("gemini", "Analyze this diff", cwd=ROOT_DIR)
        self.assertEqual(result, expected_json)
        mock_append.assert_called_once()

    @patch("subprocess.Popen")
    @patch.object(llm, "append_log")
    def test_run_llm_single_with_request_source(self, mock_append, mock_popen):
        expected_json = '{"plan": []}'
        mock_popen.return_value = self._create_mock_popen([expected_json + "\n"])

        with patch.dict(os.environ, {"AI_REQUEST_SOURCE": "unit_test_suite"}):
            result = llm._run_llm_single("opencode/model-test", "Generate plan", cwd=ROOT_DIR)
            self.assertEqual(result, expected_json)

    @patch("subprocess.Popen")
    @patch.object(llm, "append_log")
    def test_run_llm_single_with_protected_paths(self, mock_append, mock_popen):
        mock_popen.return_value = self._create_mock_popen(['{"ok": true}\n'])

        with patch("orchestrator.job_scope.protect_command", side_effect=lambda cmd, p: f"PROTECTED({cmd})") as mock_protect:
            res = llm._run_llm_single(
                "gemini", "Do not touch config", cwd=ROOT_DIR, protected_paths=[Path(".orchestrator/config.json")]
            )
            self.assertEqual(res, '{"ok": true}')
            mock_protect.assert_called_once()

    @patch("subprocess.Popen")
    @patch.object(llm, "append_log")
    def test_run_llm_single_failure_raises_runtime_error(self, mock_append, mock_popen):
        mock_popen.return_value = self._create_mock_popen([], ["API key rejected\n"], returncode=1)

        with self.assertRaises(RuntimeError) as ctx:
            llm._run_llm_single("gemini", "Failing query", cwd=ROOT_DIR)
        self.assertIn("gemini failed (code 1)", str(ctx.exception))
        self.assertIn("API key rejected", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
