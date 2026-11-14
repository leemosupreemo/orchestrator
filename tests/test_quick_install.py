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

    def test_it_is_for_a_mac_and_stops_on_anything_else_before_changing_anything(self):
        """Linux used to be accepted; Orchestrator runs on a Mac (iOS, Android and web projects are built from there)."""
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as fake:
            uname = Path(fake) / "uname"
            uname.write_text("#!/bin/sh\necho Linux\n")
            uname.chmod(0o755)
            result = subprocess.run(["sh", str(SCRIPT), "--dry-run"], capture_output=True, text=True, timeout=30,
                                    env={**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("runs on a Mac", result.stderr)
        self.assertNotIn("would run", result.stdout)  # nothing was planned, let alone done

    def test_the_script_has_no_linux_branches_left(self):
        source = SCRIPT.read_text().lower()
        for word in ("linux", "apt install", "systemd", "$os"):
            self.assertNotIn(word, source)

    def test_the_install_docs_do_not_offer_linux(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("docs/getting-started.md", "docs/user-guide.md", "README.md"):
            text = (root / name).read_text()
            self.assertNotIn("Mac or Linux", text, name)
            self.assertNotIn("Linux computer", text, name)

    def test_an_unknown_option_is_refused(self):
        result = self.run_script("--bogus")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
