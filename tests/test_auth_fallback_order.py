"""When a tool's sign-in is rejected, the work moves to the cheapest model that is still up to it, not the strongest.

A debug run's Sonnet call got "401 OAuth access token has expired" and the fallback went to claude-opus-5-5-high first (cost 10)
though the hosted claude-sonnet-5-5 (cost 5) was ready. These use real catalog entries and a fixed set of ready models."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator/scripts"))
import llm  # noqa: E402
import model_router  # noqa: E402
from model_router import ModelRole  # noqa: E402
from orchestrator import project_setup  # noqa: E402

import model_registry  # noqa: E402
from model_registry import ModelCapability as Cap, ModelMetadata, ModelTier  # noqa: E402

ALL_CAPS = [Cap.REASONING, Cap.CODING, Cap.CONTEXT, Cap.SPEED]


def entry(model_id, family, tier, cost, cli):
    return ModelMetadata(id=model_id, family=family, tier=tier, capabilities=list(ALL_CAPS), cost_factor=cost, required_clis=[cli])


# A fixed registry, not this computer's: the hosted Claude models come from a per-machine provider cache.
REGISTRY = [
    entry("claude-sonnet-4-6", "claude", ModelTier.HIGH, 5.0, "claude"),
    entry("claude-opus-5-5", "claude", ModelTier.EXTREME, 10.0, "claude"),
    *[entry(f"claude-opus-5-5-{effort}", "gemini", ModelTier.EXTREME, 10.0, "gemini") for effort in ("high", "medium", "low")],
    *[entry(f"claude-sonnet-5-5-{effort}", "gemini", ModelTier.HIGH, 5.0, "gemini") for effort in ("high", "medium", "low")],
    entry("gemini-3.5-flash-medium", "gemini", ModelTier.MEDIUM, 0.8, "gemini"),
    entry("gpt-5.5", "openai", ModelTier.EXTREME, 10.0, "codex"),
    entry("gpt-5.5-medium", "openai", ModelTier.HIGH, 5.0, "codex"),
    entry("copilot", "copilot", ModelTier.HIGH, 0.0, "gh"),
]
READY = ["claude-opus-5-5-high", "claude-opus-5-5-medium", "claude-opus-5-5-low", "claude-sonnet-5-5-high", "claude-sonnet-5-5-medium",
         "claude-sonnet-5-5-low", "gemini-3.5-flash-medium", "gpt-5.5", "gpt-5.5-medium", "copilot"]


class AuthFallbackOrderTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(model_registry, "_ALL_MODELS_CACHE", list(REGISTRY))
        patcher.start()
        self.addCleanup(patcher.stop)

    def fallbacks(self, role, primary, failed, ready=READY, tried=(), limit=20):
        with patch.object(project_setup, "models_ready_here", return_value=list(ready)), patch.object(llm, "model_usable", return_value=True), \
                patch.object(model_router, "model_usable", return_value=True):
            return llm._auth_fallback_models(role, set(failed), set(tried), primary, max_fallbacks=limit)

    def first_index(self, order, prefix):
        return next(i for i, mid in enumerate(order) if mid.startswith(prefix))

    def test_a_sonnet_that_lost_its_sign_in_falls_back_to_hosted_sonnet_before_opus(self):
        for role in (ModelRole.BUILDER, ModelRole.DEBUGGER, ModelRole.PLANNER):
            order = self.fallbacks(role, "claude-sonnet-4-6", {"claude"})
            self.assertTrue(order[0].startswith("claude-sonnet-5-5"), (role, order))
            self.assertLess(max(i for i, m in enumerate(order) if m.startswith("claude-sonnet")), self.first_index(order, "claude-opus"), role)

    def test_the_hosted_claude_models_still_come_before_other_providers(self):
        order = self.fallbacks(ModelRole.BUILDER, "claude-sonnet-4-6", {"claude"})
        hosted = [m for m in order if m.startswith("claude-")]
        self.assertEqual(order[:len(hosted)], hosted)

    def test_a_role_that_needs_the_top_tier_still_gets_it_first(self):
        for role in (ModelRole.VERIFIER, ModelRole.ARCHITECT):
            order = self.fallbacks(role, "claude-sonnet-4-6", {"claude"})
            self.assertTrue(order[0].startswith("claude-opus"), (role, order))

    def test_a_model_is_never_replaced_by_a_weaker_one_while_a_capable_one_is_ready(self):
        order = self.fallbacks(ModelRole.BUILDER, "claude-opus-5-5", {"claude"})
        self.assertTrue(order[0].startswith("claude-opus"), order)  # the failed model was top tier: no step down first
        order = self.fallbacks(ModelRole.BUILDER, "gpt-5.5-medium", {"codex"})
        self.assertEqual(order[-1], "gemini-3.5-flash-medium")  # weaker than the failed model: only after everything adequate

    def test_other_providers_are_ordered_cheapest_adequate_first(self):
        order = self.fallbacks(ModelRole.BUILDER, "gpt-5.5-medium", {"codex"})
        self.assertLess(order.index("copilot"), order.index("claude-sonnet-5-5-high"))     # free, tier 2
        self.assertLess(order.index("claude-sonnet-5-5-high"), order.index("claude-opus-5-5-high"))  # 5 before 10
        self.assertFalse({"gpt-5.5", "gpt-5.5-medium"} & set(order))  # the tool whose sign-in failed is never a fallback

    def test_what_was_already_tried_is_left_out_and_nothing_ready_means_nothing(self):
        order = self.fallbacks(ModelRole.BUILDER, "claude-sonnet-4-6", {"claude"}, tried={"claude-sonnet-5-5-high"})
        self.assertNotIn("claude-sonnet-5-5-high", order)
        self.assertEqual(self.fallbacks(ModelRole.BUILDER, "claude-sonnet-4-6", {"claude"}, ready=[]), [])
        self.assertEqual(len(self.fallbacks(ModelRole.BUILDER, "claude-sonnet-4-6", {"claude"}, limit=3)), 3)

    def test_the_ordering_helper_leaves_unknown_roles_and_models_alone(self):
        ids = ["gpt-5.5", "copilot"]
        self.assertEqual(llm._cheapest_adequate_first(ids, None, "not-a-model"), ids)
        self.assertEqual(llm._cheapest_adequate_first(ids, "nonsense", "not-a-model"), ids)
        self.assertEqual(llm._cheapest_adequate_first(ids, None, "claude-sonnet-4-6"), ["copilot", "gpt-5.5"])  # no role floor: only "not weaker"

    def test_builder_and_debugger_exclude_text_only_ollama_and_demote_opencode_behind_paid(self):
        extended = list(REGISTRY) + [
            entry("deepseek", "ollama", ModelTier.HIGH, 0.0, "ollama"),
            entry("opencode/iceman-free", "opencode", ModelTier.HIGH, 0.0, "opencode"),
        ]
        ready = list(READY) + ["deepseek", "opencode/iceman-free"]
        with patch.object(model_registry, "_ALL_MODELS_CACHE", extended):
            for role in (ModelRole.BUILDER, ModelRole.DEBUGGER):
                order = self.fallbacks(role, "gpt-5.5-medium", {"codex"}, ready=ready)
                self.assertNotIn("deepseek", order, role)  # text-only ollama CLI cannot edit files
                self.assertIn("opencode/iceman-free", order, role)
                self.assertLess(order.index("claude-sonnet-5-5-high"), order.index("opencode/iceman-free"), role)


if __name__ == "__main__":
    unittest.main()
