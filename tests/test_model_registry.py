from __future__ import annotations

import sys
import unittest
import json
import tempfile
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Import model_registry after path setup
import model_registry

class ModelRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_home = Path(self.tmp_dir.name)
        codex = patch.dict(os.environ, {"CODEX_HOME": str(self.tmp_home / "no-codex"), "CLAUDE_CONFIG_DIR": str(self.tmp_home / "no-claude"),
                                        "ORCHESTRATOR_USER_STATE_DIR": str(self.tmp_home / "state")})  # not this computer's logins or model lists
        codex.start()
        self.addCleanup(codex.stop)
        # Clear cache before each test
        model_registry._ALL_MODELS_CACHE = None
        
    def tearDown(self):
        self.tmp_dir.cleanup()
        # Clear environment after each test
        if "ORCHESTRATOR_PROJECT_ROOT" in os.environ:
            del os.environ["ORCHESTRATOR_PROJECT_ROOT"]

    @patch("pathlib.Path.home")
    @patch("urllib.request.urlopen")
    def test_sync_models_success(self, mock_urlopen, mock_home):
        mock_home.return_value = self.tmp_home
        
        # Setup mock response
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "models": [
                {
                    "id": "claude-opus-4-8",
                    "family": "claude",
                    "tier": "EXTREME",
                    "capabilities": ["reasoning", "coding"],
                    "cost_factor": 10.0,
                    "required_clis": ["claude"]
                }
            ]
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response
        
        success, msg = model_registry.sync_models()
        
        self.assertTrue(success)
        self.assertIn("Successfully synced 1 models", msg)
        
        # Verify file creation
        config_path = self.tmp_home / ".orchestrator" / "custom_models.json"
        self.assertTrue(config_path.exists())
        
        data = json.loads(config_path.read_text())
        self.assertEqual(len(data["models"]), 1)
        self.assertEqual(data["models"][0]["id"], "claude-opus-4-8")
        
        # Verify cache invalidation and loading
        all_models = model_registry.get_all_models()
        model_ids = [m.id for m in all_models]
        self.assertIn("claude-opus-4-8", model_ids)

    @patch("pathlib.Path.home")
    @patch("urllib.request.urlopen")
    def test_sync_models_merges_with_existing(self, mock_urlopen, mock_home):
        mock_home.return_value = self.tmp_home
        
        # Create existing local custom model
        config_dir = self.tmp_home / ".orchestrator"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "custom_models.json"
        config_path.write_text(json.dumps({
            "models": [
                {
                    "id": "my-local-model",
                    "tier": "LOW",
                    "capabilities": ["speed"]
                }
            ]
        }))
        
        # Setup mock response for new model
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "models": [
                {
                    "id": "claude-opus-4-8",
                    "tier": "EXTREME",
                    "capabilities": ["coding"]
                }
            ]
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response
        
        success, msg = model_registry.sync_models()
        self.assertTrue(success)
        
        data = json.loads(config_path.read_text())
        ids = [m["id"] for m in data["models"]]
        self.assertIn("my-local-model", ids)
        self.assertIn("claude-opus-4-8", ids)

    @patch("pathlib.Path.home")
    @patch("urllib.request.urlopen")
    def test_sync_models_falls_back_to_bundled_registry_on_404(self, mock_urlopen, mock_home):
        mock_home.return_value = self.tmp_home
        
        # Mock HTTPError 404
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None
        )
        
        success, msg = model_registry.sync_models()
        self.assertTrue(success)
        self.assertIn("bundled registry", msg)

        config_path = self.tmp_home / ".orchestrator" / "custom_models.json"
        data = json.loads(config_path.read_text())
        ids = [m["id"] for m in data["models"]]
        self.assertIn("gemini-3.1-pro-preview", ids)

    @patch("pathlib.Path.home")
    @patch("urllib.request.urlopen")
    def test_sync_models_falls_back_to_bundled_registry_on_network_failure(self, mock_urlopen, mock_home):
        mock_home.return_value = self.tmp_home
        mock_urlopen.side_effect = Exception("Network error")
        
        success, msg = model_registry.sync_models()
        self.assertTrue(success)
        self.assertIn("bundled registry", msg)

    def test_antigravity_alias_resolves_to_gemini_family(self):
        model = model_registry.get_model("antigravity")
        self.assertIsNotNone(model)
        self.assertEqual(model.id, "gemini-3.1-pro-preview")
        self.assertEqual(model.family, "gemini")

    def test_agy_alias_resolves_to_gemini_family(self):
        model = model_registry.get_model("agy")
        self.assertIsNotNone(model)
        self.assertEqual(model.id, "gemini-3.1-pro-preview")
        self.assertEqual(model.family, "gemini")

    def test_bundled_models_load_without_custom_registry(self):
        all_models = model_registry.get_all_models()
        model_ids = [m.id for m in all_models]
        self.assertIn("gemini-3.1-pro-preview", model_ids)
        self.assertIn("gpt-5.4", model_ids)

    @patch("pathlib.Path.home")
    def test_custom_models_override_bundled_without_duplicates(self, mock_home):
        mock_home.return_value = self.tmp_home
        config_dir = self.tmp_home / ".orchestrator"
        config_dir.mkdir(parents=True)
        (config_dir / "custom_models.json").write_text(json.dumps({
            "models": [
                {
                    "id": "gpt-5.4",
                    "family": "openai",
                    "tier": "LOW",
                    "capabilities": ["speed"],
                    "aliases": ["codex"]
                }
            ]
        }))

        all_models = model_registry.get_all_models()
        gpt54_models = [m for m in all_models if m.id == "gpt-5.4"]
        self.assertEqual(len(gpt54_models), 1)
        self.assertEqual(gpt54_models[0].tier, model_registry.ModelTier.LOW)

    def test_preferred_cli_prefers_antigravity_when_available(self):
        installed = {"agy": True, "antigravity": True, "gemini": True}
        self.assertTrue(model_registry.cli_is_available("gemini", installed))
        self.assertEqual(model_registry.preferred_cli("gemini", installed), "agy")

    def test_parse_agy_models_output_extracts_gemini_ids(self):
        output = """
Available models:
  gemini-3.1-pro-preview
* models/gemini-3-flash-preview
  claude-sonnet-4-6
  Gemini 3.5 Flash (Medium)
"""
        self.assertEqual(
            model_registry.parse_agy_models_output(output),
            ["claude-sonnet-4-6", "gemini-3-flash-preview", "gemini-3.1-pro-preview", "gemini-3.5-flash-medium"],
        )

    def test_summarize_cli_error_prefers_actionable_error_line(self):
        output = """
E0619 05:00:20.212383 main.go:273] Failed to redirect output for CLI
Error: Please sign in to view available models.
"""
        self.assertEqual(
            model_registry.summarize_cli_error(output),
            "Error: Please sign in to view available models.",
        )

    def test_summarize_cli_error_compacts_permission_failure(self):
        output = "E0619 main.go:273] opening log file: operation not permitted"
        self.assertEqual(
            model_registry.summarize_cli_error(output),
            "CLI could not start (operation not permitted)",
        )

    @patch("urllib.request.urlopen")
    @patch("model_registry.subprocess.run")
    @patch("model_registry.shutil.which")
    def test_live_discovery_asks_anthropic_using_a_key_saved_in_settings(self, mock_which, mock_run, mock_urlopen):
        mock_which.return_value = None
        seen = []

        def answer(request, *args, **kwargs):
            url = getattr(request, "full_url", request)
            seen.append((url, dict(getattr(request, "headers", {}))))
            if "api.anthropic.com" not in url:
                raise Exception("no network")
            body = MagicMock()
            body.read.return_value = json.dumps({"data": [{"id": "claude-fable-5-1"}, {"id": "claude-opus-5-5"}, {"id": "not-a-claude"}]}).encode()
            body.__enter__.return_value = body
            return body

        mock_urlopen.side_effect = answer
        with patch.dict(os.environ, {"CODEX_HOME": str(self.tmp_home / "no-codex"), "CLAUDE_CONFIG_DIR": str(self.tmp_home / "no-claude"), "ORCHESTRATOR_USER_STATE_DIR": str(self.tmp_home / "state")}, clear=True):
            result = model_registry.discover_from_sources_with_details({"ANTHROPIC_API_KEY": "sk-saved"})
        self.assertEqual(sorted(m.id for m in result.models), ["claude-fable-5-1", "claude-opus-5-5"])
        self.assertEqual({m.family for m in result.models}, {"claude"})
        self.assertIn("Anthropic API: found 2 model(s).", result.details)
        anthropic = next(headers for url, headers in seen if "api.anthropic.com" in url)
        self.assertEqual(anthropic.get("X-api-key"), "sk-saved")
        with patch.dict(os.environ, {"CODEX_HOME": str(self.tmp_home / "no-codex"), "CLAUDE_CONFIG_DIR": str(self.tmp_home / "no-claude"), "ORCHESTRATOR_USER_STATE_DIR": str(self.tmp_home / "state")}, clear=True):
            self.assertIn("Anthropic API: skipped (ANTHROPIC_API_KEY not set).", model_registry.discover_from_sources_with_details().details)

    @patch("urllib.request.urlopen")
    @patch("model_registry.subprocess.run")
    @patch("model_registry.shutil.which")
    def test_live_discovery_uses_agy_models_when_signed_in(self, mock_which, mock_run, mock_urlopen):
        mock_which.side_effect = lambda name: "/usr/bin/agy" if name == "agy" else None
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="gemini-3.1-pro-preview\ngemini-3-flash-preview\n",
            stderr="",
        )
        mock_urlopen.side_effect = Exception("no network")

        with patch.dict(os.environ, {"CODEX_HOME": str(self.tmp_home / "no-codex"), "CLAUDE_CONFIG_DIR": str(self.tmp_home / "no-claude"), "ORCHESTRATOR_USER_STATE_DIR": str(self.tmp_home / "state")}, clear=True):
            models = model_registry.discover_from_sources()

        ids = [m.id for m in models]
        self.assertIn("gemini-3.1-pro-preview", ids)
        self.assertIn("gemini-3-flash-preview", ids)
        mock_run.assert_called_with(["agy", "models"], capture_output=True, text=True, timeout=10)

    @patch("urllib.request.urlopen")
    @patch("model_registry.subprocess.run")
    @patch("model_registry.shutil.which")
    def test_live_discovery_reports_each_source(self, mock_which, mock_run, mock_urlopen):
        mock_which.side_effect = lambda name: "/usr/bin/agy" if name == "agy" else None
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: Please sign in to view available models.",
        )
        mock_urlopen.side_effect = Exception("no network")

        with patch.dict(os.environ, {"CODEX_HOME": str(self.tmp_home / "no-codex"), "CLAUDE_CONFIG_DIR": str(self.tmp_home / "no-claude"), "ORCHESTRATOR_USER_STATE_DIR": str(self.tmp_home / "state")}, clear=True):
            success, msg = model_registry.sync_models(live_discovery=True)

        self.assertFalse(success)
        self.assertIn("OpenAI API: skipped", msg)
        self.assertIn("Gemini API: skipped", msg)
        self.assertIn("Antigravity CLI (agy): unavailable", msg)
        self.assertIn("Ollama: unavailable", msg)
        self.assertIn("OpenCode CLI: skipped", msg)

    def test_parse_opencode_models_output(self) -> None:
        raw_output = """
opencode/big-pickle
opencode/deepseek-v4-flash-free
opencode/laguna-s-2.1-free
opencode/ling-3.0-flash-free
opencode/longcat-2.0-free
opencode/mimo-v2.5-free
opencode/nemotron-3-ultra-free
opencode/north-mini-code-free
"""
        parsed = model_registry.parse_opencode_models_output(raw_output)
        self.assertEqual(parsed, [
            "opencode/big-pickle",
            "opencode/deepseek-v4-flash-free",
            "opencode/laguna-s-2.1-free",
            "opencode/ling-3.0-flash-free",
            "opencode/longcat-2.0-free",
            "opencode/mimo-v2.5-free",
            "opencode/nemotron-3-ultra-free",
            "opencode/north-mini-code-free",
        ])

    def test_llm_command_routing_for_qwen_and_ollama(self) -> None:
        from orchestrator.scripts.llm import get_llm_command
        
        # 1. Test qwen2.5-coder routing (should route to: ollama run qwen2.5-coder --nowordwrap)
        cmd_qwen = get_llm_command("qwen2.5-coder", "prompt.txt")
        self.assertIn("ollama run qwen2.5-coder --nowordwrap", cmd_qwen)
        
        # 2. Test qwen-3.7-max routing (should route to: ollama run qwen-3.7-max --nowordwrap)
        cmd_qwen_max = get_llm_command("qwen-3.7-max", "prompt.txt")
        self.assertIn("ollama run qwen-3.7-max --nowordwrap", cmd_qwen_max)

        # 3. Test deepseek routing (should route to: ollama run deepseek-coder --nowordwrap)
        cmd_deepseek = get_llm_command("deepseek", "prompt.txt")
        self.assertIn("ollama run deepseek-coder --nowordwrap", cmd_deepseek)
        
        # 4. Test opencode/qwen-3.7-max routing (should route to opencode, in auto-approve/--auto mode so it won't stall)
        cmd_opencode_qwen = get_llm_command("opencode/qwen-3.7-max", "prompt.txt")
        self.assertIn("opencode run --model opencode/qwen-3.7-max", cmd_opencode_qwen)
        self.assertIn("--auto", cmd_opencode_qwen)
        cmd_opencode_default = get_llm_command("opencode", "prompt.txt")
        self.assertIn("opencode run --model opencode/big-pickle --auto", cmd_opencode_default)

        # 5. Test pool routing (should run non-interactive via the Poolside CLI, reading the prompt from stdin)
        cmd_pool = get_llm_command("pool", "prompt.txt")
        self.assertIn('cat "prompt.txt" | pool exec', cmd_pool)
        self.assertIn("--unsafe-auto-allow", cmd_pool)
        self.assertIn("-p -", cmd_pool)

    def test_copilot_has_reasoning_and_is_free(self) -> None:
        copilot = model_registry.get_model("copilot")
        self.assertIsNotNone(copilot)
        self.assertEqual(copilot.cost_factor, 0.0)
        self.assertIn(model_registry.ModelCapability.REASONING, copilot.capabilities)

    @patch("orchestrator.scripts.llm.preferred_cli")
    def test_llm_command_routing_for_gemini(self, mock_preferred_cli) -> None:
        from orchestrator.scripts.llm import get_llm_command
        
        # Test routing when preferred_cli is "agy"
        mock_preferred_cli.return_value = "agy"
        cmd_gemini_agy = get_llm_command("gemini-3.1-pro-preview", "prompt.txt", session_id="abc-123")
        self.assertIn("agy --model 'Gemini 3.1 Pro (High)' --dangerously-skip-permissions --conversation abc-123", cmd_gemini_agy)
        self.assertNotIn("--prompt -", cmd_gemini_agy)
        
        # Test routing when preferred_cli is "gemini"
        mock_preferred_cli.return_value = "gemini"
        cmd_gemini_cli = get_llm_command("gemini-3.1-pro-preview", "prompt.txt", session_id="abc-123")
        self.assertIn("gemini --model 'Gemini 3.1 Pro (High)' --skip-trust --prompt - --yolo --allowed-mcp-server-names context7,exa,swiftlens --allowed-tools read_file,grep_search,glob --raw-output --accept-raw-output-risk --session-id abc-123", cmd_gemini_cli)

        # Test routing when preferred_cli is "agy" without session_id
        mock_preferred_cli.return_value = "agy"
        cmd_gemini_agy_no_session = get_llm_command("gemini-3.1-pro-preview", "prompt.txt")
        self.assertEqual(cmd_gemini_agy_no_session, "cat \"prompt.txt\" | agy --model 'Gemini 3.1 Pro (High)' --dangerously-skip-permissions")

    @patch("orchestrator.scripts.llm._run_llm_single")
    def test_run_llm_fallback_on_runtime_error(self, mock_run_single) -> None:
        from orchestrator.scripts.llm import run_llm
        from model_router import ModelRole
        
        # Primary fails with auth/runtime error, second succeeds with valid json
        mock_run_single.side_effect = [
            RuntimeError("claude-opus-4-7 failed (code 1): Invalid API key"),
            '{"title": "Test Plan", "status": "ok"}'
        ]
        
        output, model, session_id = run_llm(
            "claude-opus-4-7",
            "Generate plan",
            allowed_models=["claude-opus-4-7", "gemini-3.1-pro-preview"],
            role=ModelRole.PLANNER
        )
        self.assertEqual(model, "gemini-3.1-pro-preview")
        self.assertIn('"title": "Test Plan"', output)
        self.assertEqual(mock_run_single.call_count, 2)


class CodexLoginTests(unittest.TestCase):
    """Codex signed in with a ChatGPT account can only run the models OpenAI lists for it; the CLI keeps that list fresh."""
    MODELS = [
        {"slug": "gpt-6.1-sol", "description": "Latest workhorse model for coding and everyday work.", "visibility": "list", "priority": 1},
        {"slug": "gpt-6-astra", "description": "Frontier intelligence for the most demanding work.", "visibility": "list", "priority": 2},
        {"slug": "gpt-6-luna", "description": "Fast and affordable model for easier tasks.", "visibility": "list", "priority": 5},
        {"slug": "gpt-reserve", "description": "hidden", "visibility": "hide", "priority": 4},
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        env = patch.dict(os.environ, {"CODEX_HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / "no-claude"),
                                      "ORCHESTRATOR_USER_STATE_DIR": str(self.home / "state")})
        env.start()
        self.addCleanup(env.stop)
        model_registry._ALL_MODELS_CACHE = None
        self.addCleanup(setattr, model_registry, "_ALL_MODELS_CACHE", None)

    def login(self, mode="chatgpt", models=None, config=""):
        (self.home / "auth.json").write_text(json.dumps({"auth_mode": mode, "tokens": {"id_token": "secret"}}))
        (self.home / "models_cache.json").write_text(json.dumps({"models": self.MODELS if models is None else models}))
        (self.home / "config.toml").write_text(config)
        model_registry._ALL_MODELS_CACHE = None

    def test_the_models_the_login_lists_are_read_best_first_and_hidden_ones_left_out(self):
        self.login()
        self.assertEqual([m["slug"] for m in model_registry.codex_login_models()], ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"])
        tiers = {m.id: m.tier for m in model_registry.codex_login_metadata()}
        self.assertEqual(tiers, {"gpt-6.1-sol": model_registry.ModelTier.HIGH, "gpt-6-astra": model_registry.ModelTier.EXTREME, "gpt-6-luna": model_registry.ModelTier.MEDIUM})

    def test_they_join_the_registry_without_a_refresh(self):
        self.login()
        self.assertIn("gpt-6.1-sol", {m.id for m in model_registry.get_all_models()})
        self.assertEqual(model_registry.get_model("gpt-6.1-sol").required_clis, ["codex"])

    def test_an_api_key_login_or_no_list_means_no_restriction(self):
        for mode, models in (("apikey", None), ("chatgpt", [])):
            self.login(mode, models)
            self.assertIsNone(model_registry.codex_login_models(), mode)
            self.assertTrue(model_registry.codex_usable(model_registry.get_model("gpt-5.4")), mode)
        (self.home / "auth.json").unlink()
        self.assertIsNone(model_registry.codex_login_models())  # not signed in to Codex at all

    def test_models_the_login_cannot_run_are_unusable_and_get_a_stand_in_of_the_same_standing(self):
        self.login(config='model = "gpt-6-luna"\n')
        old = model_registry.get_model("gpt-5.4")  # a mid-tier model
        self.assertFalse(model_registry.codex_usable(old))
        self.assertTrue(model_registry.codex_usable(model_registry.get_model("gpt-6.1-sol")))
        self.assertTrue(model_registry.codex_usable(model_registry.get_model("claude-opus-4-7")))  # other providers are not affected
        self.assertEqual(model_registry.codex_substitute(old).id, "gpt-6.1-sol")  # same standing
        self.assertEqual(model_registry.codex_substitute(model_registry.get_model("gpt-5.4-mini")).id, "gpt-6-luna")  # same standing, and the person's own default
        self.assertEqual(model_registry.codex_substitute(model_registry.get_model("o1")).id, "gpt-6-astra")

    def test_the_router_leaves_out_unusable_models_unless_nothing_else_is_left(self):
        from model_router import get_prioritized_models
        self.login()
        self.assertNotIn("gpt-5.4", get_prioritized_models(role="builder"))
        self.assertIn("gpt-6.1-sol", get_prioritized_models(role="builder"))
        self.assertEqual(get_prioritized_models(role="builder", allowed_models=["gpt-5.4"]), ["gpt-5.4"])  # nothing else to try

    def test_the_usable_list_follows_the_codex_files_without_a_restart(self):
        self.login()
        ids = model_registry.usable_model_ids()
        self.assertIn("gpt-6.1-sol", ids)
        self.assertNotIn("gpt-5.4", ids)  # the login can't run it
        self.login(models=self.MODELS + [{"slug": "gpt-7-new", "description": "Frontier", "visibility": "list", "priority": 0}])
        os.utime(self.home / "models_cache.json", ns=(1, 2_000_000_000_000_000_000))  # a later modification time, whatever the clock's resolution
        self.assertIn("gpt-7-new", model_registry.usable_model_ids())

    def test_discovery_reports_the_login_and_a_refresh_keeps_the_models(self):
        self.login()
        result = model_registry.discover_from_sources_with_details()
        self.assertIn("gpt-6-astra", {m.id for m in result.models})
        self.assertTrue(any(d.startswith("Codex ChatGPT login: 3 model(s)") for d in result.details))


class ProviderModelsTests(unittest.TestCase):
    """Each provider CLI says which models it can run; those answers are kept, read without waiting, and used like the Codex list."""
    AGY = ("gemini-4-argon-high\tGemini 4 Argon (High)\ngemini-4-argon-low\tGemini 4 Argon (Low)\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
           "claude-opus-5-5-high\tClaude Opus 5.5 (High)\ngpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        env = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(base / "state"), "CODEX_HOME": str(base / "no-codex"),
                                      "CLAUDE_CONFIG_DIR": str(base / "claude")})
        env.start()
        self.addCleanup(env.stop)
        (base / "claude").mkdir()
        model_registry._ALL_MODELS_CACHE = None
        self.addCleanup(setattr, model_registry, "_ALL_MODELS_CACHE", None)
        self.base = base

    def cache(self, **data):
        path = model_registry.provider_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": 1, **data}))
        model_registry._ALL_MODELS_CACHE = None

    def test_the_antigravity_list_is_read_as_slug_and_display_name(self):
        self.assertEqual(model_registry.parse_agy_models_with_names(self.AGY)[0], ("gemini-4-argon-high", "Gemini 4 Argon (High)"))
        self.assertIn("gemini-4-argon-high", model_registry.parse_agy_models_output(self.AGY))  # not one long slug of both columns

    def test_gemini_models_the_login_offers_are_added_and_run_by_display_name(self):
        self.cache(agy=[{"slug": "gemini-4-argon-high", "name": "Gemini 4 Argon (High)"}, {"slug": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)"}])
        with patch.object(model_registry, "preferred_cli", return_value="agy"):
            new = model_registry.get_model("gemini-4-argon-high")
            self.assertEqual((new.family, new.api_model_id), ("gemini", "Gemini 4 Argon (High)"))
            self.assertEqual(model_registry.get_model("gemini-3.8-flash-low").tier, model_registry.ModelTier.LOW)  # low effort, small model

    def test_a_gemini_model_antigravity_no_longer_offers_is_unusable_and_gets_a_stand_in(self):
        self.cache(agy=[{"slug": "gemini-4-argon-low", "name": "Gemini 4 Argon (Low)"}, {"slug": "gemini-4-argon-high", "name": "Gemini 4 Argon (High)"},
                        {"slug": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)"}])
        with patch.object(model_registry, "preferred_cli", return_value="agy"):
            old = model_registry.get_model("gemini-3.1-pro-preview")
            self.assertFalse(model_registry.model_usable(old))
            self.assertEqual(model_registry.model_substitute(old).id, "gemini-4-argon-high")  # same standing, highest effort
            self.assertEqual(model_registry.model_substitute(model_registry.get_model("gemini-3-flash-preview")).id, "gemini-3.8-flash-high")
            self.assertTrue(model_registry.model_usable(model_registry.get_model("gemini-4-argon-low")))
        with patch.object(model_registry, "preferred_cli", return_value="gemini"):  # the Gemini CLI itself says nothing, so nothing is ruled out
            self.assertTrue(model_registry.model_usable(old))

    def test_opencode_and_ollama_models_follow_what_is_installed(self):
        self.cache(opencode=["opencode/beam-free", "opencode/big-pickle"], ollama=["qwen3:8b", "deepseek-coder:6.7b"])
        ids = {m.id for m in model_registry.get_all_models()}
        self.assertTrue({"opencode/beam-free", "qwen3:8b"} <= ids)
        self.assertEqual(model_registry.get_model("qwen3:8b").cost_factor, 0.0)  # local models are free
        self.assertFalse(model_registry.model_usable(model_registry.get_model("opencode/nemotron-3-super-free")))
        self.assertTrue(model_registry.model_usable(model_registry.get_model("opencode/big-pickle")))
        self.assertTrue(model_registry.model_usable(model_registry.get_model("deepseek")))  # runs deepseek-coder, which is installed
        self.assertFalse(model_registry.model_usable(model_registry.get_model("qwen2.5-coder")))  # not installed
        self.assertIn(model_registry.model_substitute(model_registry.get_model("qwen2.5-coder")).id, {"qwen3:8b", "deepseek-coder:6.7b"})  # an installed one

    def test_with_no_answers_nothing_is_added_or_ruled_out(self):
        for mid in ("gemini-3.1-pro-preview", "opencode/nemotron-3-super-free", "qwen2.5-coder", "claude-opus-4-7"):
            self.assertTrue(model_registry.model_usable(model_registry.get_model(mid)), mid)

    def test_claude_models_it_offered_or_you_used_are_added_without_a_key(self):
        claude = self.base / "claude"
        (claude / "stats-cache.json").write_text(json.dumps({"modelUsage": {"claude-opus-5-5": {}, "claude-sonnet-5-5": {}, "<synthetic>": {}}}))
        (claude / ".claude.json").write_text(json.dumps({"additionalModelOptionsCache": [{"value": "claude-fable-5-1[1m]", "label": "Fable"}]}))
        self.assertEqual(model_registry.claude_seen_models(), ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5"])
        self.assertEqual(model_registry.get_model("claude-fable-5-1").tier, model_registry.ModelTier.EXTREME)

    def test_refreshing_keeps_each_providers_last_answer_when_it_cannot_be_reached(self):
        self.cache(agy=[{"slug": "gemini-4-argon-high", "name": "Gemini 4 Argon (High)"}], opencode=["opencode/beam-free"])
        with patch.object(model_registry, "preferred_cli", return_value="agy"), \
             patch.object(model_registry.shutil, "which", return_value="/bin/x"), \
             patch.object(model_registry.subprocess, "run", side_effect=[MagicMock(returncode=1, stdout="", stderr="Please sign in"),
                                                                        MagicMock(returncode=0, stdout="opencode/beam-free\nopencode/new-free\n", stderr="")]), \
             patch("urllib.request.urlopen", side_effect=OSError("no ollama")):
            data = model_registry.refresh_provider_models()
        self.assertEqual(data["agy"][0]["slug"], "gemini-4-argon-high")  # Antigravity wasn't signed in: its last answer stays
        self.assertEqual(data["opencode"], ["opencode/beam-free", "opencode/new-free"])
        self.assertEqual(model_registry.read_provider_cache()["opencode"], data["opencode"])
        self.assertLess(model_registry.provider_cache_age(), 5)


class ModelsNoticeTests(unittest.TestCase):
    """The page points out a change to the list of models once, until it is dismissed."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        env = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(base / "state"), "CODEX_HOME": str(base / "no-codex"), "CLAUDE_CONFIG_DIR": str(base / "no-claude")})
        env.start()
        self.addCleanup(env.stop)
        self.ids = ["a-model", "b-model"]
        ids = patch.object(model_registry, "usable_model_ids", side_effect=lambda: list(self.ids))
        ids.start()
        self.addCleanup(ids.stop)

    def test_the_first_look_only_records_the_list(self):
        self.assertIsNone(model_registry.models_notice())
        self.assertIsNone(model_registry.models_notice())

    def test_a_change_is_described_and_stays_until_dismissed(self):
        model_registry.models_notice()
        self.ids = ["b-model", "c-model", "d-model"]
        note = model_registry.models_notice()
        self.assertEqual((note["added"], note["removed"]), (["c-model", "d-model"], ["a-model"]))
        self.assertEqual(note["summary"], "New: c-model, d-model. No longer available: a-model.")
        self.assertEqual(model_registry.models_notice()["id"], note["id"])  # same note on every look, so the page shows it once
        model_registry.dismiss_models_notice()
        self.assertIsNone(model_registry.models_notice())

    def test_a_second_change_before_dismissing_adds_to_the_same_note(self):
        model_registry.models_notice()
        self.ids = ["a-model", "b-model", "c-model"]
        first = model_registry.models_notice()
        self.ids = ["a-model", "b-model", "c-model", "d-model"]
        second = model_registry.models_notice()
        self.assertEqual(second["added"], ["c-model", "d-model"])
        self.assertNotEqual(first["id"], second["id"])  # a changed note is shown again

    def test_long_lists_are_shortened(self):
        model_registry.models_notice()
        self.ids = self.ids + [f"new-{i}" for i in range(9)]
        self.assertIn("and 5 more", model_registry.models_notice()["summary"])


if __name__ == "__main__":
    unittest.main()
