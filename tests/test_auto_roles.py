"""Auto role assignment: the best usable model for each role, for this job, from what the person can run."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import model_registry  # noqa: E402
import model_router  # noqa: E402
from model_registry import ModelCapability as C, ModelMetadata, ModelTier as T  # noqa: E402

CODE, REASON, CONTEXT, VISION, SPEED = C.CODING, C.REASONING, C.CONTEXT, C.VISION, C.SPEED
CATALOG = {m.id: m for m in [
    ModelMetadata("claude-opus-5", "claude", T.EXTREME, [CODE, REASON, CONTEXT, VISION], 3.0, required_clis=["claude"]),
    ModelMetadata("claude-sonnet-5", "claude", T.HIGH, [CODE, REASON, CONTEXT, VISION], 1.0, required_clis=["claude"]),
    ModelMetadata("claude-haiku-4-5", "claude", T.MEDIUM, [CODE, SPEED], 0.3, required_clis=["claude"]),
    ModelMetadata("gpt-6.1-sol", "openai", T.EXTREME, [CODE, REASON, CONTEXT], 2.5, required_clis=["codex"]),
    ModelMetadata("gpt-6.1-terra", "openai", T.HIGH, [CODE, REASON, CONTEXT], 1.0, required_clis=["codex"]),
    ModelMetadata("gpt-6-luna", "openai", T.MEDIUM, [CODE, SPEED], 0.3, required_clis=["codex"]),
    ModelMetadata("gemini-4-argon-medium", "gemini", T.HIGH, [CODE, REASON, CONTEXT, VISION], 0.8, required_clis=["gemini"]),
    ModelMetadata("opencode/big-pickle", "opencode", T.HIGH, [CODE, REASON], 0.0, required_clis=["opencode"]),
    ModelMetadata("opencode/beam-free", "opencode", T.MEDIUM, [CODE, SPEED], 0.0, required_clis=["opencode"]),
    ModelMetadata("deepseek", "ollama", T.HIGH, [CODE], 0.0, required_clis=["ollama"]),
    ModelMetadata("alibaba-token-plan/qwen-image-2.0", "opencode", T.HIGH, [CODE, REASON], 1.0, required_clis=["opencode"]),
    ModelMetadata("gemini-3.1-pro-preview", "gemini", T.EXTREME, [CODE, REASON, CONTEXT], 1.0, required_clis=["gemini"]),
]}
UNUSABLE = {"gemini-3.1-pro-preview"}  # the account no longer offers it
ALL = list(CATALOG)
FREE = [m for m, meta in CATALOG.items() if meta.cost_factor == 0]


class RoleTestCase(unittest.TestCase):
    def setUp(self):
        for module, target, fake in ((model_router, "get_model", CATALOG.get),
                                     (model_registry, "model_usable", lambda m: m.id not in UNUSABLE),
                                     (model_registry, "cli_is_available", lambda cli: True)):
            p = patch.object(module, target, side_effect=fake)
            p.start()
            self.addCleanup(p.stop)

    def assign(self, allowed=ALL, job_type="feature", text="VISION: add a favourites screen", **kwargs):
        return model_router.auto_assign_roles(allowed, model_router.profile_job(job_type, text, **kwargs))


class JobProfileTests(unittest.TestCase):
    def test_complexity_comes_from_the_job_and_its_brief(self):
        hard = model_router.profile_job("bug", "intermittent crash from a race condition in sync")
        self.assertEqual(hard.complexity, "complex")
        self.assertTrue(any("race condition" in r or "intermittent" in r for r in hard.reasons))
        self.assertEqual(model_router.profile_job("feature", "fix a typo on the settings screen").complexity, "simple")
        self.assertEqual(model_router.profile_job("quick", "Add a button").complexity, "simple")
        self.assertEqual(model_router.profile_job("quick", "Refactor the sync engine").complexity, "complex")
        self.assertEqual(model_router.profile_job("feature", "add a favourites screen").complexity, "standard")

    def test_long_briefs_need_context_and_designs_need_vision(self):
        long = model_router.profile_job("feature", "x" * 5000)
        self.assertEqual((long.complexity, long.needs_context), ("standard", True))
        self.assertTrue(model_router.profile_job("design", "a new look").needs_vision)


class AutoRoleAssignmentTests(RoleTestCase):
    def test_every_role_gets_a_model_the_person_can_run(self):
        for text in ("add a screen", "fix a typo", "rewrite the architecture"):
            roles = self.assign(text=text)
            self.assertEqual(set(roles), {"architect", "planner", "builder", "reviewer"})
            self.assertFalse(set(roles.values()) & UNUSABLE)

    def test_image_and_video_models_are_never_picked(self):
        self.assertNotIn("alibaba-token-plan/qwen-image-2.0", self.assign(["alibaba-token-plan/qwen-image-2.0", "opencode/big-pickle"]).values())

    def test_harder_jobs_get_stronger_models_and_easy_ones_cost_less(self):
        hard = self.assign(text="fix the race condition")
        self.assertEqual(CATALOG[hard["planner"]].tier, T.EXTREME)
        self.assertEqual(CATALOG[hard["builder"]].tier, T.EXTREME)
        standard = self.assign()
        self.assertNotIn(T.EXTREME, {CATALOG[m].tier for m in (standard["planner"], standard["builder"], standard["reviewer"])})
        cost = lambda roles: sum(CATALOG[roles[r]].cost_factor for r in ("planner", "builder", "reviewer"))
        self.assertLess(cost(self.assign(text="fix a typo")), cost(hard))

    def test_role_needs_are_met(self):
        self.assertIn(VISION, CATALOG[self.assign(job_type="design", text="a new home screen")["planner"]].capabilities)
        self.assertIn(REASON, CATALOG[self.assign(job_type="bug", text="the save button does nothing")["builder"]].capabilities)
        self.assertEqual(self.assign(["gpt-6-luna", "gpt-6.1-terra"], text="x" * 5000)["planner"], "gpt-6.1-terra")

    def test_reviewer_is_independent_of_the_builder(self):
        roles = self.assign()
        self.assertNotEqual(CATALOG[roles["reviewer"]].family, CATALOG[roles["builder"]].family)
        self.assertNotEqual(self.assign(["gpt-6.1-sol", "gpt-6.1-terra"])["reviewer"], self.assign(["gpt-6.1-sol", "gpt-6.1-terra"])["builder"])

    def test_free_only_stays_free_and_prefers_hosted_over_small_local_models(self):
        roles = self.assign(FREE)
        self.assertTrue(set(roles.values()) <= set(FREE))
        self.assertEqual({CATALOG[m].family for m in roles.values()}, {"opencode"})

    def test_free_only_prefers_hosted_free_models_even_when_they_are_rated_low(self):
        low = {"opencode/big-pickle": ModelMetadata("opencode/big-pickle", "opencode", T.LOW, [CODE, SPEED], 0.0, required_clis=["opencode"]),
               "opencode/beam-free": ModelMetadata("opencode/beam-free", "opencode", T.LOW, [CODE, SPEED], 0.0, required_clis=["opencode"]),
               "hermes": ModelMetadata("hermes", "hermes", T.HIGH, [CODE, REASON], 0.0, required_clis=["hermes"])}
        with patch.dict(CATALOG, low):
            roles = self.assign(["opencode/big-pickle", "opencode/beam-free", "deepseek", "hermes"])
        self.assertEqual({CATALOG[m].family for m in roles.values()}, {"opencode"}, roles)

    def test_results_stay_within_allowed_models_and_nothing_usable_gives_nothing(self):
        self.assertTrue(set(self.assign(["gemini-4-argon-medium", "gpt-6.1-sol"]).values()) <= {"gemini-4-argon-medium", "gpt-6.1-sol"})
        self.assertEqual(set(self.assign(["gpt-6.1-sol"]).values()), {"gpt-6.1-sol"})
        self.assertEqual(self.assign(["gemini-3.1-pro-preview"]), {})

    def test_an_agent_running_an_unknown_model_does_not_outrank_a_known_hosted_one(self):
        hermes = ModelMetadata("hermes", "hermes", T.HIGH, [CODE, REASON], 0.0, required_clis=["hermes"])
        with patch.dict(CATALOG, {"hermes": hermes}):
            self.assertNotIn("hermes", self.assign(["hermes", "opencode/big-pickle", "gpt-6.1-terra"], text="fix the race condition").values())

    def test_uninstalled_tools_are_skipped(self):
        with patch.object(model_registry, "cli_is_available", side_effect=lambda cli: cli != "claude"):
            self.assertFalse(any(m.startswith("claude") for m in self.assign().values()))


class CopilotAvailabilityTests(unittest.TestCase):
    def test_copilot_needs_its_gh_extension_not_just_gh(self):
        import os, tempfile
        data = Path(tempfile.mkdtemp())
        copilot = model_registry.get_model("copilot")
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}), patch.object(model_registry.shutil, "which", return_value="/usr/bin/gh"):
            self.assertFalse(model_registry.model_usable(copilot))
            (data / "gh" / "extensions" / "gh-copilot").mkdir(parents=True)
            self.assertTrue(model_registry.model_usable(copilot))


class SettingsReachJobsTests(unittest.TestCase):
    """What the AI tools page saves is what jobs read: the same file, the role choices and the YOLO switches."""

    def setUp(self):
        import json, tempfile, common
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "settings.json").write_text(json.dumps({"default_models": {"builder": "claude-opus-4-8", "planner": "auto"},
                                                            "cli_yolo": {"opencode": False}}))
        for target in ("common.CONFIG_DIR", "llm.CONFIG_DIR"):
            p = patch(target, self.dir)
            p.start()
            self.addCleanup(p.stop)

    def test_the_server_and_jobs_use_the_same_settings_file(self):
        import common
        from orchestrator.web import server
        self.assertEqual(server.settings_path(common.ROOT), common.ORCHESTRATOR_RUNTIME_DIR / "config" / "settings.json")

    def test_jobs_read_chosen_roles_and_leave_auto_ones_to_pick(self):
        import new_job
        self.assertEqual(new_job.team_role_settings(), {"builder": "claude-opus-4-8"})

    def test_turning_yolo_off_on_the_page_removes_the_flag_from_the_command(self):
        import llm
        with patch.object(llm, "get_model", return_value=None):
            command = llm.get_llm_command("opencode/big-pickle", "/tmp/prompt")
            self.assertIn("opencode run --model opencode/big-pickle", command)
            self.assertNotIn("--auto", command)
            self.assertIn("--dangerously-skip-permissions", llm._apply_yolo_choice("agy --dangerously-skip-permissions"))


if __name__ == "__main__":
    unittest.main()
