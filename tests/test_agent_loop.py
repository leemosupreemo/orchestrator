from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "orchestrator" / "scripts"))

import agent_loop  # noqa: E402


class AgentLoopStreamingShellTests(unittest.TestCase):
    def test_run_streaming_shell_executes_and_captures_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, code = agent_loop.run_streaming_shell("echo 'hello agent loop'", Path(tmp))
            self.assertEqual(code, 0)
            self.assertIn("hello agent loop", out)


class AgentLoopMainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prompts_dir = Path(self.tmp.name) / "prompts"
        self.prompts_dir.mkdir(parents=True, exist_ok=True)
        (self.prompts_dir / "agent_lead.md").write_text("Lead prompt instructions")
        (self.prompts_dir / "architect.md").write_text("Architect sub-agent instructions")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_lead_prompt_returns_error(self):
        empty_dir = Path(self.tmp.name) / "empty_prompts"
        empty_dir.mkdir(parents=True, exist_ok=True)
        with patch.object(agent_loop, "PROMPTS_DIR", empty_dir), \
             patch("sys.argv", ["agent_loop.py", "Refactor models"]):
            code = agent_loop.main()
            self.assertEqual(code, 1)

    def test_goal_completed_immediately_returns_zero(self):
        completed_response = json.dumps({
            "status": "completed",
            "final_response": "Successfully verified project architecture.",
        })
        with patch.object(agent_loop, "PROMPTS_DIR", self.prompts_dir), \
             patch.object(agent_loop, "run_llm", return_value=(completed_response, "gemini", "sess-1")), \
             patch("sys.argv", ["agent_loop.py", "Check architecture"]):
            code = agent_loop.main()
            self.assertEqual(code, 0)

    def test_invalid_json_from_model_returns_error(self):
        with patch.object(agent_loop, "PROMPTS_DIR", self.prompts_dir), \
             patch.object(agent_loop, "run_llm", return_value=("Not valid json at all", "gemini", "sess-1")), \
             patch("sys.argv", ["agent_loop.py", "Build feature"]):
            code = agent_loop.main()
            self.assertEqual(code, 1)

    def test_clarification_needed_prompts_user_then_completes(self):
        turn1_response = json.dumps({
            "status": "clarification-needed",
            "clarification": "Should we use CoreData or SwiftData?",
        })
        turn2_response = json.dumps({
            "status": "completed",
            "final_response": "Configured SwiftData storage layer.",
        })

        llm_calls = [
            (turn1_response, "gemini", "s1"),
            (turn2_response, "gemini", "s2"),
        ]

        with patch.object(agent_loop, "PROMPTS_DIR", self.prompts_dir), \
             patch.object(agent_loop, "run_llm", side_effect=llm_calls), \
             patch.object(agent_loop, "prompt_input", return_value="SwiftData"), \
             patch("sys.argv", ["agent_loop.py", "Setup storage"]):
            code = agent_loop.main()
            self.assertEqual(code, 0)

    def test_autonomous_yolo_mode_executes_delegation(self):
        turn1_response = json.dumps({
            "status": "in-progress",
            "thought": "Delegate schema analysis to architect",
            "actions": [
                {"type": "delegate", "agent": "architect", "instruction": "Review models.swift"}
            ],
        })
        turn2_response = json.dumps({
            "status": "completed",
            "final_response": "Architect approved the schema.",
        })

        llm_mock = MagicMock()
        llm_mock.side_effect = [
            (turn1_response, "gemini", "s1"),
            ("Schema review passed without issues.", "gemini", "s2"),
            (turn2_response, "gemini", "s3"),
        ]

        with patch.object(agent_loop, "PROMPTS_DIR", self.prompts_dir), \
             patch.object(agent_loop, "run_llm", llm_mock), \
             patch("sys.argv", ["agent_loop.py", "Inspect schema", "--yolo", "--max-turns", "3"]):
            code = agent_loop.main()
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
