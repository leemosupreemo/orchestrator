import unittest
from pathlib import Path

from orchestrator import ai_providers
from orchestrator.setup_checklist import API_KEY_ENV, LLM_CLIS

REPO = Path(__file__).resolve().parents[1]


class AiProviderTests(unittest.TestCase):
    def status(self, installed=(), ready=(), keys=()):
        return ai_providers.with_status(lambda cli: cli in installed, lambda cli: cli in ready, set(keys))

    def test_free_options_come_first(self):
        costs = [p["cost"] for p in self.status()["providers"]]
        self.assertEqual(costs, sorted(costs, key=ai_providers.COST_ORDER.__getitem__))
        self.assertEqual(costs[0], "free")

    def test_ready_needs_installed_and_signed_in_or_a_saved_key(self):
        by_id = {p["id"]: p for p in self.status(installed=("claude", "codex"), ready=("claude", "opencode"),
                                                    keys=("gemini_api_key",))["providers"]}
        self.assertTrue(by_id["claude"]["ready"])
        self.assertFalse(by_id["codex"]["ready"])        # installed, not signed in
        self.assertFalse(by_id["opencode"]["ready"])     # "ready" without being installed can't happen
        self.assertTrue(by_id["gemini"]["ready"])        # an API key is enough
        self.assertTrue(self.status(keys=("gemini_api_key",))["any_ready"])
        self.assertFalse(self.status()["any_ready"])

    def test_every_provider_is_complete_and_matches_what_the_app_can_run(self):
        for p in ai_providers.PROVIDERS:
            with self.subTest(p["id"]):
                for field in ("name", "cli", "cost", "what", "install", "sign_in", "link"):
                    self.assertTrue(p[field].strip(), field)
                self.assertIn(p["cost"], ai_providers.COST_ORDER)
                self.assertTrue(p["link"].startswith("https://"))
                self.assertIn(p["cli"], LLM_CLIS)  # setup_checklist can detect it
                if p.get("key"):
                    self.assertIn(p["key"], API_KEY_ENV)  # and the API Keys page can save it
        self.assertEqual({p["cli"] for p in ai_providers.PROVIDERS}, set(LLM_CLIS))

    def test_plugins_match_the_recommendations_doc(self):
        doc = (REPO / "docs" / "recommended-mcp-plugins.md").read_text()
        for plugin in ai_providers.PLUGINS:
            self.assertIn(plugin["name"], doc)


if __name__ == "__main__":
    unittest.main()
