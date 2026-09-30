from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from work_packages import (  # noqa: E402
    build_work_packages,
    non_conflicting_batch,
    ready_work_packages,
    scopes_overlap,
)
from schedule_job import assign_group, choose_model  # noqa: E402


class WorkPackageTests(unittest.TestCase):
    def test_plan_tasks_become_bounded_role_tagged_packages(self) -> None:
        packages = build_work_packages({
            "job_id": "job-7",
            "team": {"roles": [{"id": "database_specialist"}]},
            "plan": {"tasks": [{
                "id": "profile-migration",
                "title": "Migrate profiles",
                "description": "Add the profile schema migration",
                "role": "database_specialist",
                "depends_on": ["schema-design"],
                "likely_files": ["database/", "tests/test_migration.py"],
                "acceptance_criteria": ["Existing profiles survive"],
                "tests": ["python3 -m unittest tests.test_migration"],
                "complexity": "medium",
            }]},
        })

        self.assertEqual(packages[0]["id"], "profile-migration")
        self.assertEqual(packages[0]["role"], "database_specialist")
        self.assertEqual(packages[0]["requested_mode"], "agentic")
        self.assertEqual(packages[0]["dependencies"], ["schema-design"])
        self.assertEqual(packages[0]["scopes"], ["database/", "tests/test_migration.py"])
        self.assertEqual(packages[0]["status"], "pending")
        self.assertEqual(packages[0]["attempts"], [])

    def test_unavailable_explicit_role_falls_back_to_implementation_role(self) -> None:
        packages = build_work_packages({
            "team": {"roles": [{"id": "qa_engineer"}]},
            "plan": {"tasks": [{"title": "Add endpoint", "description": "Build API", "role": "backend_engineer"}]},
        })

        self.assertEqual(packages[0]["role"], "implementation_engineer")

    def test_legacy_plan_without_tasks_becomes_one_compatible_package(self) -> None:
        packages = build_work_packages({
            "job_id": "legacy-job",
            "title": "Fix login",
            "plan": {
                "summary": "Repair login",
                "likely_files": ["auth/login.py"],
                "acceptance_criteria": ["Login works"],
            },
        })

        self.assertEqual(len(packages), 1)
        self.assertEqual(packages[0]["id"], "main")
        self.assertEqual(packages[0]["objective"], "Repair login")

    def test_dependencies_gate_package_readiness(self) -> None:
        packages = [
            {"id": "schema", "status": "accepted", "dependencies": [], "scopes": ["db/schema.sql"]},
            {"id": "migration", "status": "pending", "dependencies": ["schema"], "scopes": ["db/migration.sql"]},
            {"id": "ui", "status": "pending", "dependencies": ["missing"], "scopes": ["ui/view.py"]},
        ]

        self.assertEqual([item["id"] for item in ready_work_packages(packages)], ["migration"])

    def test_unknown_or_nested_scopes_conflict(self) -> None:
        self.assertTrue(scopes_overlap([], ["ui/view.py"]))
        self.assertTrue(scopes_overlap(["database/"], ["database/migrations/001.sql"]))
        self.assertFalse(scopes_overlap(["ui/"], ["database/"]))

    def test_batch_contains_only_ready_non_overlapping_packages(self) -> None:
        packages = [
            {"id": "api", "status": "pending", "dependencies": [], "scopes": ["server/api.py"]},
            {"id": "api-tests", "status": "pending", "dependencies": [], "scopes": ["server/"]},
            {"id": "ui", "status": "pending", "dependencies": [], "scopes": ["ui/view.py"]},
            {"id": "later", "status": "pending", "dependencies": ["api"], "scopes": ["docs/"]},
        ]

        self.assertEqual(
            [item["id"] for item in non_conflicting_batch(packages, limit=3)],
            ["api", "ui"],
        )

    def test_scheduler_prefers_agentic_model_and_records_actual_route(self) -> None:
        group = {
            "group_id": "MAIN",
            "resource_profile": "implementation",
            "preferred_models": ["deepseek"],
            "required_execution_mode": "agentic",
            "status": "pending",
        }
        machine = {
            "name": "local",
            "models": ["deepseek", "gpt-5.4-mini"],
        }
        job = {
            "builder": "deepseek",
            "allowed_models": ["deepseek", "gpt-5.4-mini"],
            "task_groups": [group],
            "dispatch_history": [],
        }

        selected = choose_model(group, machine, job)
        assignment = assign_group(
            job,
            "MAIN",
            machine,
            {"binaries": {"codex": True}, "probed_at": "2026-09-15T12:00:00Z"},
            selected,
        )

        self.assertEqual(selected, "gpt-5.4-mini")
        self.assertEqual(assignment["requested_execution_mode"], "agentic")
        self.assertEqual(assignment["actual_execution_mode"], "agentic")
        self.assertEqual(assignment["execution_profile"]["adapter"], "codex")


if __name__ == "__main__":
    unittest.main()
