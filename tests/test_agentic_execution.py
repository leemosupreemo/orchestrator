from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import llm  # noqa: E402
from model_router import ModelRole  # noqa: E402
from run_builder import format_work_package_context, validate_execution_handoff  # noqa: E402


def sample_package() -> dict:
    return {
        "id": "profile-migration",
        "role": "database_specialist",
        "objective": "Implement the profile migration",
        "requested_mode": "agentic",
        "required_tools": ["read", "edit", "shell", "tests"],
        "dependencies": ["schema-design"],
        "scopes": ["database/", "tests/"],
        "acceptance_criteria": ["Existing profiles survive"],
        "tests": ["python3 -m unittest tests.test_migration"],
        "budgets": {"iterations": 4, "minutes": 20},
    }


class AgenticExecutionTests(unittest.TestCase):
    @patch("llm._run_llm_single")
    def test_agentic_failure_automatically_downgrades_and_records_attempts(self, mock_run) -> None:
        mock_run.side_effect = [RuntimeError("agent tool failed"), '{"action":"patch"}']
        attempts: list[dict] = []

        output, actual_model, session_id = llm.run_llm(
            "gpt-5.4-mini",
            "Implement a bounded task",
            allowed_models=["gpt-5.4-mini", "deepseek"],
            role=ModelRole.BUILDER,
            required_execution_mode="agentic",
            attempt_log=attempts,
        )

        self.assertEqual(output, '{"action":"patch"}')
        self.assertEqual(actual_model, "deepseek")
        self.assertTrue(session_id)
        self.assertEqual([item["outcome"] for item in attempts], ["failed", "succeeded"])
        self.assertEqual(attempts[0]["actual_mode"], "agentic")
        self.assertEqual(attempts[1]["actual_mode"], "guided")
        self.assertIn("downgraded", attempts[1]["downgrade_reason"])
        self.assertTrue(all(item["requested_mode"] == "agentic" for item in attempts))

    def test_work_package_context_bounds_sub_agent_authority(self) -> None:
        context = format_work_package_context(sample_package())

        self.assertIn("database_specialist", context)
        self.assertIn("Implement the profile migration", context)
        self.assertIn("Existing profiles survive", context)
        self.assertIn("database/", context)
        self.assertIn("may not broaden scope", context)
        self.assertIn("4 iterations", context)

    def test_handoff_is_synthesized_from_verified_execution_evidence(self) -> None:
        handoff = validate_execution_handoff(
            {
                "action": "patch",
                "summary": "Added the migration",
                "test_command": "python3 -m unittest tests.test_migration",
            },
            sample_package(),
            files_changed=["database/001_profiles.sql", "tests/test_migration.py"],
            tests_ok=True,
        )

        self.assertEqual(handoff["work_performed"], "Added the migration")
        self.assertEqual(handoff["acceptance_criteria_status"], "passed")
        self.assertEqual(handoff["files_changed"], ["database/001_profiles.sql", "tests/test_migration.py"])
        self.assertEqual(handoff["remaining_risks"], [])

    def test_handoff_rejects_success_claim_when_validation_failed(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot claim completion"):
            validate_execution_handoff(
                {"action": "patch", "summary": "Done", "acceptance_criteria_status": "passed"},
                sample_package(),
                files_changed=["database/001_profiles.sql"],
                tests_ok=False,
            )


if __name__ == "__main__":
    unittest.main()
