from __future__ import annotations

import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"
for entry in (PACKAGE_ROOT, SCRIPTS_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import review_ready  # noqa: E402
import run_builder  # noqa: E402
import team_roles  # noqa: E402
import new_job  # noqa: E402


def role_based_job() -> dict:
    team = team_roles.assemble_team("Add an accessible settings screen", "feature")
    team["intent_brief"] = team_roles.build_intent_brief(
        "Add an accessible settings screen",
        {"summary": "Let all users manage settings", "acceptance_criteria": ["VoiceOver labels controls"]},
    )
    return {
        "development_approach": "role-based",
        "team": team,
        "type": "feature-plan",
        "issue_number": 7,
        "title": "Accessible settings",
        "plan": {"summary": "Let all users manage settings", "acceptance_criteria": ["VoiceOver labels controls"], "likely_files": []},
    }


class TeamPromptContextTests(unittest.TestCase):
    def test_recursive_planner_input_keeps_team_context(self) -> None:
        job = role_based_job()

        prompt = new_job.build_planner_input(
            "Planner rules",
            "Add an accessible settings screen",
            team=job["team"],
            revision_context="Architect feedback: cover empty states",
        )

        self.assertIn("ROLE-BASED TEAM CONTEXT", prompt)
        self.assertIn("Accessibility Specialist", prompt)
        self.assertIn("Architect feedback: cover empty states", prompt)

    def test_builder_brief_contains_shared_team_context(self) -> None:
        brief = run_builder.make_brief(role_based_job())

        self.assertIn("ROLE-BASED TEAM CONTEXT", brief)
        self.assertIn("Accessibility Specialist", brief)
        self.assertIn("VoiceOver labels controls", brief)

    def test_review_prompt_requires_independent_qa_against_intent(self) -> None:
        job = role_based_job()

        prompt = review_ready.make_review_prompt("Review rules", "Brief", "metadata", "diff", job)

        self.assertIn("ROLE-BASED TEAM CONTEXT", prompt)
        self.assertIn("QA validates against the Intent Brief independently", prompt)

    def test_standard_job_prompt_remains_unchanged(self) -> None:
        prompt = review_ready.make_review_prompt("Review rules", "Brief", "metadata", "diff", {})

        self.assertNotIn("ROLE-BASED TEAM CONTEXT", prompt)
        self.assertEqual(prompt, "Review rules\n\nBrief:\nBrief\n\nPR metadata:\nmetadata\n\nDiff:\ndiff\n")


if __name__ == "__main__":
    unittest.main()
