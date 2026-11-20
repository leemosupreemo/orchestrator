import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.desktop_runtime import bundle_environment, is_packaged_install
from orchestrator.cli import update_command


class DesktopRuntimeTests(unittest.TestCase):
    def test_bundle_with_spaces_and_clean_search_path(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder) / "Folder with spaces/Orchestrator.app"
            cert = bundle / "Contents/Resources/certificates/cacert.pem"
            cert.parent.mkdir(parents=True)
            cert.write_text("certificate fixture")
            paths = [Path("/chosen tools/bin"), Path("/usr/bin")]
            with patch.dict(os.environ, {"PATH": "/untrusted/bin"}):
                env = bundle_environment(bundle, paths)
            self.assertNotIn("/untrusted/bin", env["PATH"])
            self.assertIn("/chosen tools/bin", env["PATH"].split(":"))
            self.assertEqual(env["PATH"].split(":").count("/usr/bin"), 1)
            self.assertEqual(env["SSL_CERT_FILE"], str(cert.resolve()))
            self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertEqual(env["PYTHONNOUSERSITE"], "1")

    def test_invalid_user_tool_directory_is_rejected(self):
        with self.assertRaises(ValueError):
            bundle_environment(Path("/test.app"), [Path("relative")])

    def test_packaged_update_never_invokes_package_manager(self):
        with patch.dict(os.environ, {"ORCHESTRATOR_PACKAGED_APP": "/Applications/Orchestrator.app"}), patch("orchestrator.cli.subprocess.run") as run:
            self.assertTrue(is_packaged_install())
            self.assertEqual(update_command(argparse.Namespace()), 0)
            run.assert_not_called()
