import unittest
from unittest.mock import patch
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
                if p.get("key"):  # the API Keys page and the checklist read the key's setting, label and variable from here
                    self.assertTrue(p["env"].isupper() and p["key_label"].strip())
                    self.assertEqual(API_KEY_ENV[p["key"]], p["env"])
        self.assertEqual(LLM_CLIS, [p["cli"] for p in ai_providers.PROVIDERS])  # one list, not two kept in step

    def test_plugins_match_the_recommendations_doc(self):
        doc = (REPO / "docs" / "recommended-mcp-plugins.md").read_text()
        for plugin in ai_providers.PLUGINS:
            self.assertIn(plugin["name"], doc)

    @patch("orchestrator.ai_providers.subprocess.run")
    def test_install_runs_only_the_registered_provider_command(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = "installed"
        result = ai_providers.install_provider("opencode")
        self.assertEqual(result["provider"], "OpenCode")
        self.assertEqual(run.call_args.args[0], ["bash", "-lc", ai_providers.PROVIDERS[0]["install"]])
        self.assertIs(run.call_args.kwargs["stdin"], ai_providers.subprocess.DEVNULL)

    def test_install_rejects_unknown_provider_without_running_a_command(self):
        with patch("orchestrator.ai_providers.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "Unknown AI provider"):
                ai_providers.install_provider("anything; touch /tmp/not-allowed")
        run.assert_not_called()

    def test_auto_approve_classification(self):
        # yolo providers auto-accept tool changes (gemini/agy/pool/opencode); the rest are plan-only / non-interactive
        status = self.status(installed=("gemini", "agy", "pool", "opencode", "claude", "codex", "ollama"),
                             ready=("gemini", "agy", "pool", "opencode", "claude", "codex", "ollama"))
        by_id = {p["id"]: p for p in status["providers"]}
        for pid in ("gemini", "agy", "pool", "opencode"):
            self.assertTrue(by_id[pid]["auto_approve"], f"{pid} should auto-approve (yolo)")
        for pid in ("claude", "codex", "ollama"):
            self.assertFalse(by_id[pid]["auto_approve"], f"{pid} should not auto-approve")

    def test_pool_readiness_uses_credentials_file(self):
        from orchestrator.setup_checklist import _cli_ready
        import shutil as _shutil
        home = Path("/tmp/orchestrator_pool_test_home")
        creds = home / ".config" / "poolside" / "credentials.json"
        creds.parent.mkdir(parents=True, exist_ok=True)
        if creds.exists():
            creds.unlink()
        env = {"HOME": str(home), "XDG_CONFIG_HOME": ""}
        try:
            with patch("orchestrator.setup_checklist.shutil.which", return_value="/usr/local/bin/pool"), \
                 patch.dict("os.environ", env, clear=False):
                # Signed out: `pool login` has not written credentials yet -> not ready
                self.assertFalse(_cli_ready("pool"))
                # Signed in: `pool login` writes the credentials file -> ready
                creds.write_text("{}")
                self.assertTrue(_cli_ready("pool"))
        finally:
            _shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
