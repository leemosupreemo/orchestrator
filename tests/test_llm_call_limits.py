"""A model call can't hang the run: half-written lines don't freeze it, chatter can't keep it alive forever, and stopping
it stops the model tool, not just the shell around it."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"))
import llm  # noqa: E402


class ModelCallLimitTests(unittest.TestCase):
    def setUp(self):
        logs = tempfile.mkdtemp()
        for p in (patch.object(llm, "append_log", lambda *a, **k: None), patch.object(llm, "get_llm_env", lambda: dict(os.environ)),
                  patch.dict(os.environ, {"AI_REQUEST_SOURCE": ""})):
            p.start()
            self.addCleanup(p.stop)
        self.marker = f"orchestrator-test-{os.getpid()}-{time.time_ns()}"

    def call(self, command: str, timeout: int):
        with patch.object(llm, "get_llm_command", lambda *a, **k: command):
            started = time.time()
            try:
                return llm._run_llm_single("fake-model", "hi", timeout=timeout, raw=True), time.time() - started
            except llm.LLMTimeoutError as exc:
                return exc, time.time() - started

    def survivors(self):
        out = subprocess.run(["pgrep", "-f", self.marker], capture_output=True, text=True).stdout.split()
        return [p for p in out if int(p) != os.getpid()]

    def test_a_half_written_line_then_silence_still_times_out(self):
        result, took = self.call(f"printf 'thinking'; exec -a {self.marker} sleep 60", timeout=1)
        self.assertIsInstance(result, llm.LLMTimeoutError)  # it used to block on readline forever
        self.assertLess(took, 8)
        time.sleep(0.3)
        self.assertEqual(self.survivors(), [])  # the model tool was stopped too, not just bash

    def test_endless_chatter_hits_the_hard_cap(self):
        result, took = self.call("while true; do echo tick >&2; sleep 0.2; done", timeout=1)
        self.assertIsInstance(result, llm.LLMTimeoutError)
        self.assertGreaterEqual(took, 3)  # three times the no-output limit
        self.assertLess(took, 8)
        self.assertIn("Still running after", str(result.last_milestone))

    def test_a_normal_answer_comes_back_whole(self):
        result, _ = self.call("printf 'line one\\nline two'; echo 'warn' >&2", timeout=5)
        self.assertEqual(result, "line one\nline two")


if __name__ == "__main__":
    unittest.main()
