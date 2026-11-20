"""Ask AI's Free tier is ranked by what each model really is. The registry labels every local model "high", so a 1-billion-parameter
model was tried first and answered as if it had never heard of the project. Local models are now judged by the size Ollama reports,
and a decent one on this computer still comes before a hosted one (the question and project text stay here)."""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "orchestrator" / "scripts"))

from orchestrator import ollama_api, project_chat  # noqa: E402
from tests.test_ollama_context import FakeOllama  # noqa: E402


def model(model_id, tier=2, family="opencode", clis=("opencode",), cost=0.0):
    return type("M", (), {"id": model_id, "tier": tier, "family": family, "required_clis": list(clis), "cost_factor": cost,
                          "capabilities": ["coding"]})()


def local(model_id, tier=2):
    return model(model_id, tier=tier, family="ollama", clis=("ollama",))


class SizeParsingTests(unittest.TestCase):
    def test_parameter_sizes_as_ollama_writes_them(self):
        for text, expected in (("7.6B", 7.6), ("1B", 1.0), ("30B", 30.0), ("566M", 0.566), ("1.2T", 1200.0), (" 8.2b ", 8.2)):
            with self.subTest(text=text):
                self.assertAlmostEqual(ollama_api.billions(text), expected)
        for text in (None, "", "big", "7", "B"):
            with self.subTest(text=text):
                self.assertIsNone(ollama_api.billions(text))

    def test_the_size_and_quantization_come_from_the_same_question_as_the_window(self):
        ollama_api._CACHE.clear()
        self.addCleanup(ollama_api._CACHE.clear)
        fake = FakeOllama(context_length=32768, parameter_size="7.6B", quantization="Q4_K_M")
        self.addCleanup(fake.close)
        with patch.dict(os.environ, {"OLLAMA_HOST": fake.address}):
            self.assertEqual(ollama_api.size_of("qwen2.5-coder"), (7.6, "Q4_K_M"))
            self.assertEqual(ollama_api.context_length("qwen2.5-coder"), 32768)
        self.assertEqual(len([r for r in fake.requests if r[0] == "/api/show"]), 1)

    def test_a_server_that_cannot_be_reached_or_says_nothing_gives_no_size(self):
        ollama_api._CACHE.clear()
        self.addCleanup(ollama_api._CACHE.clear)
        with patch.dict(os.environ, {"OLLAMA_HOST": "127.0.0.1:9"}):
            self.assertEqual(ollama_api.size_of("x", timeout=0.5), (None, ""))
        ollama_api._CACHE.clear()
        fake = FakeOllama()
        self.addCleanup(fake.close)
        with patch.dict(os.environ, {"OLLAMA_HOST": fake.address}):
            self.assertEqual(ollama_api.size_of("y"), (None, ""))


class QualityTierTests(unittest.TestCase):
    def test_a_local_model_is_judged_by_its_size_not_its_label(self):
        label = local("m", tier=2)  # every local model is labelled high
        for params, expected in ((70, 2), (30, 2), (8.2, 3), (6.0, 3), (3.9, 4), (3.0, 4), (1.0, 5), (0.5, 5)):
            with self.subTest(params=params):
                self.assertEqual(project_chat.quality_tier(label, (params, "Q4_K_M")), expected)

    def test_a_hosted_model_keeps_its_label(self):
        for tier in (2, 3, 4):
            self.assertEqual(project_chat.quality_tier(model("opencode/x-free", tier=tier)), tier)

    def test_a_heavily_squeezed_copy_is_a_step_worse(self):
        label = local("m")
        self.assertEqual(project_chat.quality_tier(label, (30.0, "Q4_K_M")), 2)
        self.assertEqual(project_chat.quality_tier(label, (30.0, "Q2_K")), 3)
        self.assertEqual(project_chat.quality_tier(label, (8.0, "Q3_K_S")), 4)
        self.assertEqual(project_chat.quality_tier(label, (3.9, "Q4_0")), 4)

    def test_a_size_that_is_not_known_is_not_trusted_with_the_label(self):
        self.assertEqual(project_chat.quality_tier(local("m", tier=2), (None, "")), 3)

    def test_a_label_worse_than_the_size_suggests_wins(self):
        self.assertEqual(project_chat.quality_tier(local("m", tier=4), (30.0, "Q4_K_M")), 4)

    def test_it_never_goes_below_the_bottom(self):
        self.assertEqual(project_chat.quality_tier(local("m", tier=5), (None, "")), 5)
        self.assertEqual(project_chat.quality_tier(local("m", tier=4), (1.0, "Q2_K")), 5)


class FreeOrderTests(unittest.TestCase):
    SIZES = {"deepseek": (1.0, "Q4_0"), "gemma3:4b": (3.9, "Q4_K_M"), "qwen2.5-coder": (7.6, "Q4_K_M"), "qwen3:8b": (8.2, "Q4_K_M"),
             "glm-30b-q2": (29.9, "Q2_K")}

    def models(self):
        return [local("deepseek"), local("gemma3:4b"), local("qwen3:8b"), local("qwen2.5-coder"), local("glm-30b-q2", tier=3),
                model("opencode/beam-free", tier=2), model("opencode/flash-free", tier=3), model("opencode/inkling-free", tier=4)]

    def order(self, models=None, sizes=None):
        return [m.id for m in project_chat._order("free", models or self.models(), self.SIZES if sizes is None else sizes)]

    def test_the_one_billion_parameter_model_is_last(self):
        self.assertEqual(self.order()[-1], "deepseek")

    def test_decent_models_come_first_a_decent_local_one_before_a_hosted_one(self):
        order = self.order()
        decent = order[:6]
        self.assertEqual(decent[:3], ["qwen2.5-coder", "qwen3:8b", "glm-30b-q2"])  # on this computer, smaller (quicker) first
        self.assertLess(order.index("glm-30b-q2"), order.index("opencode/beam-free"))
        self.assertLess(order.index("opencode/beam-free"), order.index("opencode/flash-free"))  # then hosted, better first

    def test_weak_models_come_after_every_decent_one(self):
        order = self.order()
        for weak in ("gemma3:4b", "opencode/inkling-free"):
            for decent in ("qwen2.5-coder", "opencode/flash-free"):
                self.assertLess(order.index(decent), order.index(weak))

    def test_a_weak_local_model_still_comes_before_a_weak_hosted_one(self):
        order = self.order()
        self.assertLess(order.index("gemma3:4b"), order.index("opencode/inkling-free"))

    def test_with_no_local_model_it_is_the_hosted_ones_by_their_labels(self):
        hosted = [model("opencode/c-free", tier=3), model("opencode/a-free", tier=2), model("opencode/b-free", tier=4)]
        self.assertEqual(self.order(hosted, sizes={}), ["opencode/a-free", "opencode/c-free", "opencode/b-free"])

    def test_the_same_ollama_model_under_two_ids_is_tried_once(self):
        twice = [local("qwen2.5-coder"), local("qwen2.5-coder:latest"), local("deepseek"), local("deepseek-coder:latest")]
        sizes = {"qwen2.5-coder": (7.6, "Q4_K_M"), "qwen2.5-coder:latest": (7.6, "Q4_K_M"), "deepseek": (1.0, "Q4_0"), "deepseek-coder:latest": (1.0, "Q4_0")}
        order = self.order(twice, sizes)
        self.assertEqual(order, ["qwen2.5-coder", "deepseek"])

    def test_the_other_tiers_are_ordered_as_before(self):
        paid = [model("a", tier=3, cost=2.0), model("b", tier=2, cost=5.0), model("c", tier=2, cost=3.0)]
        self.assertEqual([m.id for m in project_chat._order("balanced", paid, self.SIZES)], ["c", "b", "a"])
        top = [model("x", tier=1, cost=9.0), model("y", tier=1, cost=12.0)]
        self.assertEqual([m.id for m in project_chat._order("complex", top, self.SIZES)], ["y", "x"])


class TierUsesTheSizesTests(unittest.TestCase):
    def test_free_tries_the_decent_local_model_first_and_the_tiny_one_last(self):
        models = [local("deepseek"), local("qwen2.5-coder"), model("opencode/beam-free")]
        sizes = {"deepseek": (1.0, "Q4_0"), "qwen2.5-coder": (7.6, "Q4_K_M")}
        free = project_chat.chat_tiers(models, checks={}, now=1000.0, sizes=sizes)["free"]
        self.assertEqual(free["models"], ["qwen2.5-coder", "opencode/beam-free", "deepseek"])
        self.assertEqual(free["model"], "qwen2.5-coder")

    def test_only_models_that_run_in_ollama_are_measured(self):
        models = [local("qwen2.5-coder"), model("opencode/beam-free"), model("claude-sonnet-4-6", family="claude", clis=("claude",), cost=5.0)]
        asked = []
        with patch.object(ollama_api, "size_of", side_effect=lambda name, **kw: asked.append(name) or (7.6, "Q4_K_M")):
            sizes = project_chat.local_sizes(models)
        self.assertEqual(asked, ["qwen2.5-coder"])
        self.assertEqual(list(sizes), ["qwen2.5-coder"])


if __name__ == "__main__":
    unittest.main()
