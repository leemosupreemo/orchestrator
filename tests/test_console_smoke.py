from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Mock interactive modules before importing dev_console
orig_common = sys.modules.get('common')
sys.modules['common'] = MagicMock()
import dev_console  # noqa: E402
if orig_common is not None:
    sys.modules['common'] = orig_common
else:
    del sys.modules['common']

class ConsoleSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_root = dev_console.ROOT
        self.temp_root = Path("/tmp/orchestrator-smoke-root")
        self.temp_root.mkdir(parents=True, exist_ok=True)
        dev_console.ROOT = self.temp_root
        dev_console.CONFIG_DIR = self.temp_root / ".orchestrator" / "config"
        dev_console.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        
        # Setup dummy PROJECT_CONFIG
        dev_console.PROJECT_CONFIG = MagicMock()
        dev_console.PROJECT_CONFIG.project_name = "SmokeTestProject"
        dev_console.PROJECT_CONFIG.runtime_dir = self.temp_root / ".orchestrator"
        dev_console.PROJECT_CONFIG.validate_distribution_config.return_value = []

    def tearDown(self) -> None:
        dev_console.ROOT = self.old_root
        import shutil
        if self.temp_root.exists():
            shutil.rmtree(self.temp_root)

    def _mock_get_key_side_effect(self, planned_inputs: list[str]):
        """Helper to provide planned inputs followed by empty strings for non-blocking loops."""
        for inp in planned_inputs:
            yield inp
        while True:
            yield ""

    @patch("dev_console.get_key")
    @patch("dev_console.input")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.print_phase")
    @patch("dev_console.print_header")
    @patch("dev_console.save_job")
    @patch("dev_console.refresh_job")
    @patch("dev_console.read_json")
    @patch("dev_console.write_json")
    @patch("dev_console.PROJECT_CONFIG")
    @patch("dev_console.prompt_input")
    def test_job_menu_smoke(self, mock_prompt_input, mock_config, mock_write, mock_read, mock_refresh, mock_save, mock_header, mock_phase, mock_status, mock_clear, mock_input, mock_get_key):
        """Superficially run through Job Selection menu options to ensure no NameErrors or obvious crashes."""
        job = {
            "job_id": "smoke-job",
            "status": "planned",
            "type": "feature-plan",
            "planner": "gemini",
            "builder": "gemini",
            "reviewer": "gemini",
            "llm_sessions": [
                {"id": "session-1", "model": "gemini-3.1-pro-preview"},
                {"id": "session-2", "model": "gpt-5.5"}
            ],
            "_path": self.temp_root / "smoke-job.json"
        }
        mock_refresh.return_value = job
        mock_read.return_value = job
        
        mock_get_key.side_effect = self._mock_get_key_side_effect(["j", "q", "b"])
        mock_prompt_input.side_effect = ["Tell me a joke", ""]
        mock_input.return_value = ""
        
        # Mock run_llm for Ask AI
        with patch("dev_console.run_llm", return_value=("AI response", "actual-model", "new-session-id")):
             dev_console.handle_job_selection(job, ["local"], ["gemini"])
        
        self.assertTrue(True)

    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    @patch("dev_console.input")
    @patch("dev_console.prompt_input", return_value="b")
    def test_config_menu_smoke(self, _mock_prompt, mock_input, _mock_status, _mock_clear, mock_get_key):
        """Superficially run through Configuration menu options."""
        # Traversal:
        # 'i' (Instructions) -> 'b' (Back)
        # 's' (Setup Guide) -> Enter
        # 'b' (Back/Exit menu)
        mock_get_key.side_effect = self._mock_get_key_side_effect(["i", "b", "s", "b", "b"])
        mock_input.return_value = "" # For 's' setup guide exit
        
        # Avoid running actual scripts during smoke test
        with patch("dev_console.run_script") as mock_run:
            dev_console.handle_configuration_menu(["local"], ["gemini"])
        
        self.assertTrue(True)

    @patch("dev_console.get_key")
    @patch("dev_console.clear_screen")
    @patch("dev_console.StatusBar")
    def test_self_test_menu_smoke(self, _mock_status, _mock_clear, mock_get_key):
        """Superficially run through Tooling/Self-Test menu options."""
        # Traversal:
        # 'b' (Back/Exit menu)
        mock_get_key.side_effect = self._mock_get_key_side_effect(["b"])
        
        dev_console.handle_tooling_tests(["local"], ["gemini"])
        self.assertTrue(True)

if __name__ == "__main__":
    unittest.main()
