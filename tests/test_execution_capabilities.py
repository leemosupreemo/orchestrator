from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from execution_capabilities import ExecutionMode, mode_for_role, profile_for_model  # noqa: E402
from model_router import ModelRole, get_prioritized_models  # noqa: E402


class ExecutionCapabilitiesTests(unittest.TestCase):
    def test_workspace_cli_route_is_agentic_for_builder_work(self) -> None:
        profile = profile_for_model(
            "gpt-5.5",
            machine={"name": "local"},
            probe={"binaries": {"codex": True}},
        )

        self.assertIn(ExecutionMode.AGENTIC, profile.execution_modes)
        self.assertEqual(mode_for_role(profile, ModelRole.BUILDER), ExecutionMode.AGENTIC)
        self.assertTrue({"read", "edit", "shell", "tests"} <= set(profile.tools))

    def test_text_only_route_defaults_conservatively(self) -> None:
        profile = profile_for_model(
            "deepseek",
            machine={"name": "local"},
            probe={"binaries": {"ollama": True}},
        )

        self.assertNotIn(ExecutionMode.AGENTIC, profile.execution_modes)
        self.assertEqual(mode_for_role(profile, ModelRole.BUILDER), ExecutionMode.GUIDED)
        self.assertNotIn("edit", profile.tools)

    def test_missing_cli_removes_agentic_execution(self) -> None:
        profile = profile_for_model(
            "gpt-5.5",
            machine={"name": "worker-1"},
            probe={"binaries": {"codex": False}},
        )

        self.assertFalse(profile.available)
        self.assertEqual(profile.execution_modes, (ExecutionMode.ADVISORY,))

    def test_unknown_route_never_receives_mutating_tools(self) -> None:
        profile = profile_for_model("unknown-provider-model")

        self.assertEqual(profile.execution_modes, (ExecutionMode.ADVISORY,))
        self.assertEqual(profile.tools, ("read",))

    def test_builder_routing_prefers_allowed_agentic_route(self) -> None:
        prioritized = get_prioritized_models(
            role=ModelRole.BUILDER,
            allowed_models=["deepseek", "gpt-5.4-mini"],
            required_execution_mode="agentic",
        )

        self.assertEqual(prioritized[0], "gpt-5.4-mini")

    def test_profile_serialization_records_complete_route(self) -> None:
        payload = profile_for_model("claude-sonnet-4-6", machine={"name": "mac2"}).as_dict()

        self.assertEqual(payload["model"], "claude-sonnet-4-6")
        self.assertEqual(payload["adapter"], "claude")
        self.assertEqual(payload["machine"], "mac2")
        self.assertIn("agentic", payload["execution_modes"])
        self.assertTrue(payload["supports_structured_handoff"])


if __name__ == "__main__":
    unittest.main()
