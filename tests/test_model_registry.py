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
    def test_live_discovery_uses_agy_models_when_signed_in(self, mock_which, mock_run, mock_urlopen):
        mock_which.side_effect = lambda name: "/usr/bin/agy" if name == "agy" else None
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="gemini-3.1-pro-preview\ngemini-3-flash-preview\n",
            stderr="",
        )
        mock_urlopen.side_effect = Exception("no network")

        with patch.dict(os.environ, {}, clear=True):
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

        with patch.dict(os.environ, {}, clear=True):
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
        
        # 1. Test qwen2.5-coder routing (should route to: ollama run qwen2.5-coder)
        cmd_qwen = get_llm_command("qwen2.5-coder", "prompt.txt")
        self.assertIn("ollama run qwen2.5-coder", cmd_qwen)
        
        # 2. Test qwen-3.7-max routing (should route to: ollama run qwen-3.7-max)
        cmd_qwen_max = get_llm_command("qwen-3.7-max", "prompt.txt")
        self.assertIn("ollama run qwen-3.7-max", cmd_qwen_max)
        
        # 3. Test opencode/qwen-3.7-max routing (should route to opencode)
        cmd_opencode_qwen = get_llm_command("opencode/qwen-3.7-max", "prompt.txt")
        self.assertIn("opencode run --model opencode/qwen-3.7-max", cmd_opencode_qwen)

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


if __name__ == "__main__":
    unittest.main()
