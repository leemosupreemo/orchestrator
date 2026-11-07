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
        self.assertEqual(model.id, "gemini")
        self.assertEqual(model.family, "gemini")

    def test_agy_alias_resolves_to_gemini_family(self):
        model = model_registry.get_model("agy")
        self.assertIsNotNone(model)
        self.assertEqual(model.id, "gemini")
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


class GeminiAccessStatusTests(unittest.TestCase):
    MODELS_OUTPUT = "Fetching available models...\ngemini-4-argon-high\tGemini 4 Argon (High)\n"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        home = patch.object(model_registry.Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)

    def sign_in(self):
        token = self.home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        token.parent.mkdir(parents=True)
        token.touch()

    def leave_old_cli_files(self):
        (self.home / ".gemini").mkdir(exist_ok=True)
        (self.home / ".gemini" / "google_accounts.json").write_text("{}")

    def status(self, installed, returncode=0, stdout=MODELS_OUTPUT, run_error=None):
        result = MagicMock(returncode=returncode, stdout=stdout)
        with patch("shutil.which", side_effect=lambda cli: cli if cli in installed else None), \
                patch("subprocess.run", return_value=result, side_effect=run_error):
            return model_registry.gemini_access_status()

    def test_old_cli_alone_is_discontinued_even_with_its_credentials(self):
        self.leave_old_cli_files()
        self.assertEqual(self.status({"gemini"}), "Discontinued")

    def test_nothing_installed(self):
        self.assertEqual(self.status(set()), "Not installed")

    def test_old_cli_credentials_do_not_make_antigravity_ready(self):
        self.leave_old_cli_files()
        self.assertEqual(self.status({"agy", "gemini"}), "Not ready")

    def test_antigravity_signed_in_and_listing_models_is_ready(self):
        self.sign_in()
        self.assertEqual(self.status({"agy", "gemini"}), "Ready")

    def test_antigravity_failing_to_list_models_is_not_ready(self):
        self.sign_in()
        self.assertEqual(self.status({"agy"}, returncode=1), "Not ready")

    def test_antigravity_listing_nothing_is_not_ready(self):
        self.sign_in()
        self.assertEqual(self.status({"agy"}, stdout="Please sign in\n"), "Not ready")

    def test_antigravity_timeout_is_unknown(self):
        import subprocess
        self.sign_in()
        self.assertEqual(self.status({"agy"}, run_error=subprocess.TimeoutExpired("agy", 8)), "Unknown")

    def test_discontinued_cli_is_not_supported_but_antigravity_is(self):
        with patch("shutil.which", side_effect=lambda cli: cli if cli == "gemini" else None):
            self.assertFalse(model_registry.cli_is_supported("gemini"))
        with patch("shutil.which", side_effect=lambda cli: cli if cli == "agy" else None):
            self.assertTrue(model_registry.cli_is_supported("gemini"))


SNAPSHOT = {"fetched_at": 0, "providers": {
    "agy": ["gemini-3.8-flash-high", "gemini-3.8-flash-low", "gemini-3.8-flash-medium",
            "gemini-4-argon-high", "gemini-4-argon-low", "gemini-4-argon-medium", "claude-opus-5-5-high"],
    "codex": ["gpt-6-astra", "gpt-6-luna", "gpt-6.1-sol", "gpt-6.1-terra"],
    "opencode": ["opencode/big-pickle", "opencode/glm-5.3-flash-free", "opencode/beam-free", "meta/muse-spark-1.3"],
    "ollama": ["deepseek-coder:latest"],
    "claude": ["*"],
    "copilot": [],
}}


class IsolatedCatalogTestCase(unittest.TestCase):
    def setUp(self):
        model_registry._ALL_MODELS_CACHE = None
        self.addCleanup(setattr, model_registry, "_ALL_MODELS_CACHE", None)
        custom_models = patch.object(model_registry, "load_custom_models", return_value=[])
        custom_models.start()
        self.addCleanup(custom_models.stop)
        self.bundled = [m.id for m in model_registry.load_bundled_models()]


class ModelAvailabilityTests(IsolatedCatalogTestCase):
    def available(self, model_id, snapshot=SNAPSHOT):
        from model_availability import is_available
        return is_available(model_registry.get_model(model_id), snapshot)

    def test_listed_models_are_available_and_stale_ones_are_not(self):
        self.assertTrue(self.available("gemini-4-argon-medium"))
        self.assertFalse(self.available("gemini-3.1-pro-preview"))
        self.assertTrue(self.available("gpt-6.1-sol"))
        self.assertFalse(self.available("gpt-5.4"))
        self.assertTrue(self.available("opencode/big-pickle"))
        self.assertFalse(self.available("opencode/qwen-3.7-max"))

    def test_only_pulled_ollama_models_are_available(self):
        self.assertTrue(self.available("deepseek"))
        self.assertFalse(self.available("qwen2.5-coder"))

    def test_copilot_needs_the_copilot_command_not_just_gh(self):
        self.assertFalse(self.available("copilot"))

    def test_account_default_ids_need_a_working_provider(self):
        self.assertTrue(self.available("codex"))
        self.assertFalse(self.available("codex", {"providers": {"codex": []}}))

    def test_unchecked_provider_is_unknown_rather_than_unavailable(self):
        self.assertIsNone(self.available("gemini-4-argon-medium", {"providers": {"agy": None}}))

    def test_failing_probe_is_recorded_as_unknown(self):
        import subprocess
        from model_availability import _probe_safely
        def times_out():
            raise subprocess.TimeoutExpired("agy", 8)
        self.assertIsNone(_probe_safely(times_out))
        self.assertEqual(_probe_safely(lambda: []), [])

    def test_codex_probe_reads_only_listed_models(self):
        import model_availability
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".codex").mkdir()
            (home / ".codex" / "models_cache.json").write_text(json.dumps({"models": [
                {"slug": "gpt-6.1-sol", "visibility": "list"}, {"slug": "gpt-reserve", "visibility": "hide"}]}))
            with patch.object(model_availability.Path, "home", return_value=home), \
                    patch("shutil.which", return_value="/bin/codex"), \
                    patch("subprocess.run", return_value=MagicMock(returncode=0)):
                self.assertEqual(model_availability._probe_codex(), ["gpt-6.1-sol"])

    def test_snapshot_is_reused_until_stale(self):
        import model_availability, time
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(model_availability.Path, "home", return_value=Path(tmp)), \
                patch.dict(model_availability.PROBES, {name: (lambda: ["x"]) for name in model_availability.PROBES}):
            first = model_availability.get_snapshot()
            self.assertEqual(first["providers"]["agy"], ["x"])
            with patch.object(model_availability, "refresh_snapshot") as refresh:
                model_availability.get_snapshot()
                refresh.assert_not_called()
                model_availability.get_snapshot(max_age=-1)
                refresh.assert_called_once()

    def test_agy_output_uses_the_id_column(self):
        output = "Fetching available models...\ngemini-4-argon-high\tGemini 4 Argon (High)\nclaude-opus-5-5-low\tClaude Opus 5.5 (Low)\n"
        self.assertEqual(model_registry.parse_agy_models_output(output), ["claude-opus-5-5-low", "gemini-4-argon-high"])

    def test_saved_copy_cannot_keep_an_alias_the_catalog_moved(self):
        stale = model_registry.ModelMetadata(id="gemini-3.1-pro-preview", family="gemini", tier=model_registry.ModelTier.HIGH, aliases=["agy", "my-own-name"])
        with patch.object(model_registry, "load_custom_models", return_value=[stale]):
            model_registry._ALL_MODELS_CACHE = None
            self.assertEqual(model_registry.get_model("agy").id, "gemini")
            self.assertEqual(model_registry.get_model("my-own-name").id, "gemini-3.1-pro-preview")

    def test_exact_id_beats_a_stale_alias(self):
        stale = model_registry.ModelMetadata(id="gpt-5.4", family="openai", tier=model_registry.ModelTier.HIGH, aliases=["codex"])
        with patch.object(model_registry, "load_custom_models", return_value=[stale]):
            model_registry._ALL_MODELS_CACHE = None
            self.assertEqual(model_registry.get_model("codex").id, "codex")


class JobProfileTests(unittest.TestCase):
    def profile(self, *args, **kwargs):
        from model_router import profile_job
        return profile_job(*args, **kwargs)

    def test_hard_problem_is_complex(self):
        profile = self.profile("bug", "SUMMARY: intermittent crash from a race condition in sync")
        self.assertEqual(profile.complexity, "complex")
        self.assertTrue(any("intermittent" in reason or "race condition" in reason for reason in profile.reasons))

    def test_small_change_is_simple(self):
        self.assertEqual(self.profile("feature", "VISION: fix a typo on the settings screen").complexity, "simple")

    def test_quick_job_is_simple_unless_the_brief_says_otherwise(self):
        self.assertEqual(self.profile("quick", "Add a button").complexity, "simple")
        self.assertEqual(self.profile("quick", "Refactor the sync engine").complexity, "complex")

    def test_ordinary_feature_is_standard(self):
        self.assertEqual(self.profile("feature", "VISION: add a favourites screen").complexity, "standard")

    def test_long_brief_needs_context_without_becoming_complex(self):
        profile = self.profile("feature", "x" * 5000)
        self.assertEqual(profile.complexity, "standard")
        self.assertTrue(profile.needs_context)

    def test_design_and_stitch_need_vision(self):
        self.assertTrue(self.profile("design", "A new look").needs_vision)
        self.assertTrue(self.profile("feature", "A new look", stitch=True).needs_vision)


class AutoRoleAssignmentTests(IsolatedCatalogTestCase):
    def assign(self, allowed, job_type="feature", text="VISION: add a favourites screen", snapshot=SNAPSHOT, **kwargs):
        from model_router import auto_assign_roles, profile_job
        return auto_assign_roles(allowed, snapshot, profile_job(job_type, text, **kwargs))

    def tier(self, model_id):
        return model_registry.get_model(model_id).tier

    def test_never_picks_a_model_the_account_cannot_use(self):
        from model_availability import is_available
        for text in ("add a screen", "fix a typo", "rewrite the architecture"):
            for model_id in self.assign(self.bundled, text=text).values():
                self.assertTrue(is_available(model_registry.get_model(model_id), SNAPSHOT), model_id)

    def test_standard_job_does_not_use_frontier_models(self):
        for model_id in self.assign(self.bundled).values():
            self.assertNotEqual(self.tier(model_id), model_registry.ModelTier.EXTREME, model_id)

    def test_complex_job_uses_frontier_models_for_planner_and_builder(self):
        roles = self.assign(self.bundled, text="fix the race condition")
        self.assertEqual(self.tier(roles["planner"]), model_registry.ModelTier.EXTREME)
        self.assertEqual(self.tier(roles["builder"]), model_registry.ModelTier.EXTREME)

    def test_simple_job_is_cheaper_than_complex_job(self):
        cost = lambda roles: sum(model_registry.get_model(m).cost_factor for m in roles.values())
        self.assertLess(cost(self.assign(self.bundled, text="fix a typo")), cost(self.assign(self.bundled, text="fix the race condition")))

    def test_bug_builder_can_reason_and_code(self):
        builder = model_registry.get_model(self.assign(self.bundled, job_type="bug", text="the save button does nothing")["builder"])
        self.assertIn(model_registry.ModelCapability.REASONING, builder.capabilities)
        self.assertIn(model_registry.ModelCapability.CODING, builder.capabilities)

    def test_long_brief_planner_has_large_context(self):
        roles = self.assign(["gpt-6-luna", "gpt-6.1-terra"], text="x" * 5000)
        self.assertEqual(roles["planner"], "gpt-6.1-terra")

    def test_design_planner_has_vision(self):
        planner = model_registry.get_model(self.assign(self.bundled, job_type="design", text="a new home screen")["planner"])
        self.assertIn(model_registry.ModelCapability.VISION, planner.capabilities)

    def test_reviewer_comes_from_another_provider_when_possible(self):
        roles = self.assign(self.bundled)
        self.assertNotEqual(model_registry.get_model(roles["reviewer"]).family, model_registry.get_model(roles["builder"]).family)

    def test_reviewer_differs_from_builder_within_one_provider(self):
        roles = self.assign(["gpt-6.1-sol", "gpt-6.1-terra"])
        self.assertNotEqual(roles["reviewer"], roles["builder"])

    def test_builder_has_coding_capability(self):
        builder = model_registry.get_model(self.assign(self.bundled)["builder"])
        self.assertIn(model_registry.ModelCapability.CODING, builder.capabilities)

    def test_results_stay_within_allowed_models(self):
        allowed = ["gemini-4-argon-medium", "gpt-6.1-sol"]
        self.assertTrue(set(self.assign(allowed).values()) <= set(allowed))

    def test_single_model_fills_every_role(self):
        self.assertEqual(set(self.assign(["gpt-6.1-sol"]).values()), {"gpt-6.1-sol"})

    def test_free_only_stays_free_and_prefers_hosted_over_small_local_models(self):
        free = [m.id for m in model_registry.load_bundled_models() if m.cost_factor == 0]
        roles = self.assign(free)
        for model_id in roles.values():
            self.assertEqual(model_registry.get_model(model_id).cost_factor, 0)
            self.assertEqual(model_registry.get_model(model_id).family, "opencode")

    def test_reviewer_quality_outranks_provider_variety(self):
        roles = self.assign(["gpt-6.1-sol", "gpt-6.1-terra", "opencode/big-pickle"])
        self.assertEqual(roles["reviewer"], "gpt-6.1-terra")

    def test_unchecked_models_are_used_only_without_confirmed_ones(self):
        snapshot = {"providers": {"agy": None, "codex": ["gpt-6.1-sol"]}}
        self.assertEqual(set(self.assign(["gemini-4-argon-medium", "gpt-6.1-sol"], snapshot=snapshot).values()), {"gpt-6.1-sol"})
        self.assertEqual(set(self.assign(["gemini-4-argon-medium"], snapshot=snapshot).values()), {"gemini-4-argon-medium"})

    def test_nothing_usable_returns_no_assignment(self):
        self.assertEqual(self.assign(["gpt-5.4"]), {})
        self.assertEqual(self.assign(["not-a-real-model"]), {})

    def test_alias_allowed_entries_keep_the_auto_pick(self):
        from model_router import fit_to_allowed
        roles = self.assign(["codex", "gemini"])
        self.assertEqual(fit_to_allowed(roles["planner"], ["codex", "gemini"]), roles["planner"])
        self.assertEqual(fit_to_allowed(roles["reviewer"], ["gemini", "codex"]), roles["reviewer"])

    def test_model_outside_allowed_falls_back_to_first_entry(self):
        from model_router import fit_to_allowed
        self.assertEqual(fit_to_allowed("claude-opus-4-8", ["codex", "gemini"]), "codex")

    def test_catalog_models_auto_can_pick_route_to_their_own_cli(self):
        from llm import get_llm_command
        with patch("llm.preferred_cli", return_value="agy"):
            self.assertIn("agy --model gemini-4-argon-medium", get_llm_command("gemini-4-argon-medium", "/tmp/p"))
            self.assertIn("| agy --dangerously-skip-permissions", get_llm_command("gemini", "/tmp/p"))
            self.assertIn("| agy --dangerously-skip-permissions", get_llm_command("agy", "/tmp/p"))
        self.assertIn("codex exec --model gpt-6.1-sol", get_llm_command("gpt-6.1-sol", "/tmp/p"))
        self.assertIn("claude -p --model claude-sonnet-5-5", get_llm_command("claude-sonnet-5-5", "/tmp/p"))
        self.assertIn("opencode run --model opencode/glm-5.3-flash-free", get_llm_command("opencode/glm-5.3-flash-free", "/tmp/p"))

    def test_yolo_flags_follow_project_settings(self):
        import llm
        from llm import get_llm_command
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            with patch.object(llm, "CONFIG_DIR", config), patch("llm.preferred_cli", return_value="agy"):
                self.assertIn("agy --model gemini-4-argon-medium --dangerously-skip-permissions", get_llm_command("gemini-4-argon-medium", "/p"))
                self.assertNotIn("dangerously", get_llm_command("claude-sonnet-5-5", "/p"))
                self.assertNotIn("dangerously", get_llm_command("gpt-6.1-sol", "/p"))
                self.assertNotIn("--auto", get_llm_command("opencode/big-pickle", "/p"))
                (config / "settings.json").write_text(json.dumps({"cli_yolo": {"agy": False, "claude": True, "codex": True, "opencode": True}}))
                self.assertNotIn("dangerously", get_llm_command("gemini", "/p"))
                self.assertIn("claude -p --dangerously-skip-permissions --model claude-sonnet-5-5", get_llm_command("claude-sonnet-5-5", "/p"))
                self.assertIn("codex exec --dangerously-bypass-approvals-and-sandbox --model gpt-6.1-sol", get_llm_command("gpt-6.1-sol", "/p"))
                self.assertIn("codex exec --dangerously-bypass-approvals-and-sandbox", get_llm_command("codex", "/p"))
                self.assertIn("opencode run --auto --model opencode/big-pickle", get_llm_command("opencode/big-pickle", "/p"))

    def test_machine_listing_a_cli_can_run_its_other_models(self):
        from schedule_job import machine_supports_model
        machine = {"models": ["gemini", "codex", "claude"]}
        for model_id in ("gpt-6.1-sol", "gemini-4-argon-medium", "claude-opus-5-5"):
            self.assertTrue(machine_supports_model(machine, model_id), model_id)
        self.assertFalse(machine_supports_model(machine, "opencode/big-pickle"))


if __name__ == "__main__":
    unittest.main()
