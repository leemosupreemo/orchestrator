import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "orchestrator/web/static/install.sh"


class InstallScriptTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True, timeout=30)

    def test_the_script_parses(self):
        self.assertEqual(subprocess.run(["sh", "-n", str(SCRIPT)]).returncode, 0)

    def test_a_dry_run_shows_each_step_without_changing_anything(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        for step in ("pipx install --force", "orchestrator connect", "orchestrator service install"):
            self.assertIn(step, result.stdout)
        self.assertIn("leemosupreemo/orchestrator.git", result.stdout)

    def test_no_service_skips_starting_at_login(self):
        result = self.run_script("--dry-run", "--no-service")
        self.assertNotIn("service install", result.stdout)
        self.assertIn("orchestrator ui --tunnel", result.stdout)

    def test_an_unknown_option_is_refused(self):
        result = self.run_script("--bogus")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
