"""A Claude model call says why it might never answer, before it starts: a project-scoped Claude Code plugin installed
inside a macOS-protected folder makes `claude -p` wait for a permission question that a headless job can't answer."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"))
import llm  # noqa: E402

FOUND = [{"plugin": "playwright@claude-plugins-official", "path": "/Users/me/Documents/Thirteen", "folder": "Documents"}]


class ClaudePluginWarningTests(unittest.TestCase):
    def setUp(self):
        repo = Path(tempfile.mkdtemp(prefix="orchestrator-claude-warning-"))
        (repo / ".git").mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(repo, ignore_errors=True))
        self.repo = repo
        for p in (patch.object(llm, "append_log", lambda *a, **k: None),
                  patch.object(llm, "get_llm_env", lambda: dict(os.environ)),
                  patch.dict(os.environ, {"AI_REQUEST_SOURCE": ""})):
            p.start()
            self.addCleanup(p.stop)

    def run_model(self, model: str, findings=FOUND):
        from orchestrator import claude_plugins
        out = io.StringIO()
        with patch.object(llm, "get_llm_command", lambda *a, **k: "true"), \
                patch.object(claude_plugins, "findings", lambda *a, **k: findings), \
                contextlib.redirect_stdout(out):
            llm._run_llm_single(model, "hi", cwd=self.repo, timeout=10, raw=True)
        return out.getvalue()

    def test_a_claude_call_says_what_may_make_it_hang(self):
        text = self.run_model("claude-sonnet-4-6")
        self.assertIn("playwright@claude-plugins-official", text)
        self.assertIn("/Users/me/Documents/Thirteen", text)
        self.assertIn("installed_plugins.json", text)

    def test_other_models_are_not_warned_about_claude_plugins(self):
        from orchestrator import claude_plugins
        asked = []
        with patch.object(claude_plugins, "findings", lambda *a, **k: asked.append(a) or FOUND):
            text = self.run_model("gemini-3-pro", findings=None)
        self.assertEqual(asked, [])
        self.assertNotIn("installed_plugins.json", text)

    def test_a_broken_plugin_file_never_blocks_the_call(self):
        from orchestrator import claude_plugins

        def explode(*a, **k):
            raise OSError("no plugins file")

        with patch.object(claude_plugins, "findings", explode):
            self.assertEqual(llm._claude_plugin_warnings("claude-sonnet-4-6", self.repo), [])


if __name__ == "__main__":
    unittest.main()
