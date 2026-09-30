from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import team_roles  # noqa: E402


class TeamRolesTests(unittest.TestCase):
    def test_automatic_team_always_contains_small_foundation(self) -> None:
        team = team_roles.assemble_team("Rename a local helper", "quick")

        self.assertEqual(
            [role["id"] for role in team["roles"]],
            ["technical_lead", "implementation_engineer", "qa_engineer"],
        )
        self.assertEqual(team["mode"], "auto")

    def test_automatic_team_adds_roles_from_distinct_job_evidence(self) -> None:
        team = team_roles.assemble_team(
            "Redesign the SwiftUI onboarding flow and persist profiles in a database migration",
            "feature",
        )

        selected = {role["id"] for role in team["roles"]}
        self.assertTrue({"ux_designer", "frontend_engineer", "database_specialist", "platform_specialist"} <= selected)
        database = next(role for role in team["roles"] if role["id"] == "database_specialist")
        self.assertIn("database", database["reason"].lower())

    def test_automatic_team_caps_situational_roles_to_avoid_overstaffing(self) -> None:
        team = team_roles.assemble_team(
            "UX UI API backend database migration auth encryption accessibility latency CI deploy iOS Android",
            "feature",
        )

        self.assertLessEqual(len(team["roles"]), 7)
        self.assertEqual(len({role["id"] for role in team["roles"]}), len(team["roles"]))

    def test_signal_matching_does_not_treat_substrings_as_specialties(self) -> None:
        team = team_roles.assemble_team("Build a command-line guide for required values", "feature")

        self.assertEqual(
            [role["id"] for role in team["roles"]],
            ["technical_lead", "implementation_engineer", "qa_engineer"],
        )

    def test_single_generic_terms_do_not_overstaff_the_team(self) -> None:
        for request in (
            "Update the release workflow documentation",
            "Request permission to view logs",
        ):
            with self.subTest(request=request):
                team = team_roles.assemble_team(request, "feature")
                self.assertEqual(
                    [role["id"] for role in team["roles"]],
                    ["technical_lead", "implementation_engineer", "qa_engineer"],
                )

    def test_requested_roles_are_pinned_and_keep_foundation(self) -> None:
        team = team_roles.assemble_team(
            "Small feature",
            "feature",
            requested_roles=["database_specialist", "unknown", "database_specialist"],
        )

        self.assertEqual(team["mode"], "custom")
        self.assertEqual(
            [role["id"] for role in team["roles"]],
            ["technical_lead", "implementation_engineer", "qa_engineer", "database_specialist"],
        )
        self.assertTrue(next(role for role in team["roles"] if role["id"] == "database_specialist")["pinned"])

    def test_pinned_roles_augment_instead_of_disabling_automatic_staffing(self) -> None:
        team = team_roles.assemble_team(
            "Redesign the onboarding user flow",
            "feature",
            requested_roles=["security_engineer"],
        )

        selected = {role["id"] for role in team["roles"]}
        self.assertTrue({"ux_designer", "security_engineer"} <= selected)
        self.assertTrue(next(role for role in team["roles"] if role["id"] == "security_engineer")["pinned"])

    def test_pinned_roles_count_toward_the_compact_roster_limit(self) -> None:
        team = team_roles.assemble_team(
            "UX frontend API database migration accessibility performance CI deploy iOS",
            "feature",
            requested_roles=["security_engineer"],
        )

        self.assertLessEqual(len(team["roles"]), 7)
        self.assertTrue(next(role for role in team["roles"] if role["id"] == "security_engineer")["pinned"])

    def test_evidence_for_pinned_role_does_not_waste_an_automatic_slot(self) -> None:
        team = team_roles.assemble_team(
            "UX UI API database accessibility",
            "feature",
            requested_roles=["ux_designer"],
        )

        selected = {role["id"] for role in team["roles"]}
        self.assertEqual(len(team["roles"]), 7)
        self.assertTrue(
            {"ux_designer", "frontend_engineer", "backend_engineer", "database_specialist"} <= selected
        )

    def test_refinement_globally_reranks_roles_when_the_team_is_capped(self) -> None:
        team = team_roles.assemble_team(
            "Design onboarding UX with a frontend API and database migration",
            "feature",
        )
        refined = team_roles.refine_team(
            team,
            {
                "risks": ["Authentication authorization and sensitive token encryption"],
                "tasks": [{"title": "Secure access", "description": "Harden authentication and authorization"}],
            },
        )

        selected = {role["id"] for role in refined["roles"]}
        self.assertIn("security_engineer", selected)
        self.assertLessEqual(len(refined["roles"]), 7)
        self.assertEqual(len(refined["handoffs"]), 1)

    def test_refinement_reads_task_acceptance_criteria_and_tests(self) -> None:
        team = team_roles.assemble_team("Add account settings", "feature")
        refined = team_roles.refine_team(
            team,
            {
                "tasks": [{
                    "title": "Preserve records",
                    "description": "Update settings",
                    "acceptance_criteria": ["A database migration preserves every profile"],
                    "tests": ["Run SQL migration rollback tests"],
                }],
            },
        )

        self.assertIn("database_specialist", {role["id"] for role in refined["roles"]})

    def test_intent_brief_anchors_roles_to_request_and_plan(self) -> None:
        brief = team_roles.build_intent_brief(
            "Let users recover an account without losing data",
            {
                "summary": "Add account recovery",
                "acceptance_criteria": ["Recovery preserves saved projects"],
                "constraints": ["No new dependency"],
                "assumptions": ["Email is verified"],
                "risks": ["Token replay"],
            },
        )

        self.assertEqual(brief["user_request"], "Let users recover an account without losing data")
        self.assertEqual(brief["user_outcome"], "Add account recovery")
        self.assertEqual(brief["acceptance_criteria"], ["Recovery preserves saved projects"])
        self.assertEqual(brief["risks"], ["Token replay"])

    def test_intent_brief_preserves_original_request_and_aggregates_task_criteria(self) -> None:
        original = team_roles.build_intent_brief(
            "Let users recover an account without losing data",
            {"constraints": ["No new dependency"]},
        )

        revised = team_roles.build_intent_brief(
            "Use a shorter recovery code",
            {
                "summary": "Revise account recovery",
                "constraints": ["No new dependency", "Codes expire"],
                "acceptance_criteria": ["Recovery works"],
                "tasks": [{
                    "acceptance_criteria": ["Recovery works", "Saved projects remain intact"],
                }],
            },
            existing_brief=original,
        )

        self.assertEqual(revised["user_request"], "Let users recover an account without losing data")
        self.assertEqual(revised["current_request"], "Use a shorter recovery code")
        self.assertEqual(revised["constraints"], ["No new dependency", "Codes expire"])
        self.assertEqual(revised["acceptance_criteria"], ["Recovery works", "Saved projects remain intact"])

    def test_empty_replan_preserves_every_existing_intent_field(self) -> None:
        existing = {
            "user_request": "Original request",
            "current_request": "Original request",
            "user_outcome": "Original outcome",
            "acceptance_criteria": ["Original criterion"],
            "constraints": ["Original constraint"],
            "assumptions": ["Original assumption"],
            "risks": ["Original risk"],
            "non_goals": ["Original non-goal"],
        }

        brief = team_roles.build_intent_brief("Revision feedback", {}, existing_brief=existing)

        for field in ("user_request", "user_outcome", "acceptance_criteria", "constraints", "assumptions", "risks", "non_goals"):
            self.assertEqual(brief[field], existing[field])

        team = team_roles.assemble_team("", "feature")
        team["intent_brief"] = brief
        context = team_roles.format_team_context(team)
        for value in (
            "Original outcome", "Original criterion", "Original constraint", "Original assumption",
            "Original risk", "Original non-goal",
        ):
            self.assertIn(value, context)

    def test_context_explains_roles_and_structured_handoff_contract(self) -> None:
        team = team_roles.assemble_team("Add a database migration", "feature")
        team["intent_brief"] = team_roles.build_intent_brief(
            "Add a database migration",
            {"summary": "Preserve existing user records", "acceptance_criteria": ["No data loss"]},
        )

        context = team_roles.format_team_context(team)

        self.assertIn("ROLE-BASED TEAM CONTEXT", context)
        self.assertIn("Preserve existing user records", context)
        self.assertIn("Database Specialist", context)
        self.assertIn("Impact on the user goal", context)

    def test_replacing_team_preserves_foundation_and_records_change(self) -> None:
        team = team_roles.assemble_team("Add an API", "feature")

        changed = team_roles.replace_team_roles(team, ["accessibility_specialist", "unknown"])

        self.assertEqual(changed["mode"], "custom")
        self.assertEqual(
            [role["id"] for role in changed["roles"]],
            ["technical_lead", "implementation_engineer", "qa_engineer", "accessibility_specialist"],
        )
        self.assertEqual(changed["handoffs"][-1]["type"], "team_changed")
        self.assertTrue({"action_required", "remaining_risk", "created_at"} <= changed["handoffs"][-1].keys())


if __name__ == "__main__":
    unittest.main()
