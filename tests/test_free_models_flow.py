from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"
for path in (PACKAGE_ROOT, SCRIPTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import llm  # noqa: E402
import model_registry  # noqa: E402
from model_router import ModelRole, get_prioritized_models  # noqa: E402


class FreeModelsFlowTests(unittest.TestCase):
    def test_ollama_commands_have_nowordwrap(self):
        cmd = llm.get_llm_command("deepseek", "prompt.md")
        self.assertIn("ollama run deepseek-coder --nowordwrap", cmd)

        cmd_qwen = llm.get_llm_command("qwen2.5-coder", "prompt.md")
        self.assertIn("ollama run qwen2.5-coder --nowordwrap", cmd_qwen)

    def test_extract_json_block_strips_ansi_and_cursor_escapes(self):
        # Simulate realistic Ollama wrapped output with ANSI cursor movement codes
        dirty = (
            "\x1b[32m```json\x1b[0m\n"
            "{\n"
            '  "title": "[Storage] Audit\x1b[2D\x1b[K persistence tests",\n'
            '  "summary": "Check error handling in\x1b[5D\x1b[K network layer",\n'
            '  "audit_goals": ["Verify edge cases\x1b[1D\x1b[K in state machine"],\n'
            '  "test_cases": []\n'
            "}\n"
            "```"
        )
        cleaned = llm.extract_json_block(dirty)
        self.assertNotIn("\x1b", cleaned)
        parsed = json.loads(cleaned, strict=False)
        self.assertEqual(parsed["test_cases"], [])
        self.assertIn("Audit", parsed["title"])

    def test_extract_json_block_handles_unescaped_control_characters_with_strict_false(self):
        text = '```json\n{"summary": "Line 1\nLine 2", "test_cases": []}\n```'
        cleaned = llm.extract_json_block(text)
        parsed = json.loads(cleaned, strict=False)
        self.assertEqual(parsed["summary"], "Line 1\nLine 2")

    def test_free_models_prioritize_reasoning_for_planner(self):
        free_models = [m.id for m in model_registry.get_free_models()]
        self.assertIn("copilot", free_models)
        planners = get_prioritized_models(role=ModelRole.PLANNER, allowed_models=free_models)
        # Copilot has REASONING, CODING, and SPEED capabilities and tier HIGH
        self.assertIn("copilot", planners[:3])

    def test_free_verifier_models_are_prioritized(self):
        free_models = [m.id for m in model_registry.get_free_models()]
        verifiers = get_prioritized_models(role=ModelRole.VERIFIER, allowed_models=free_models)
        self.assertTrue(len(verifiers) > 0)


if __name__ == "__main__":
    unittest.main()
