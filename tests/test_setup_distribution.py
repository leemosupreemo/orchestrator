from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from orchestrator.project_config import find_project_root  # noqa: E402
from setup_distribution import infer_firebase_plist_path, setup_distribution  # noqa: E402


class SetupDistributionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_project_root = os.environ.get("ORCHESTRATOR_PROJECT_ROOT")

    def tearDown(self) -> None:
        if self.old_project_root is None:
            os.environ.pop("ORCHESTRATOR_PROJECT_ROOT", None)
        else:
            os.environ["ORCHESTRATOR_PROJECT_ROOT"] = self.old_project_root

    def test_env_project_root_overrides_current_repo_markers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-app-") as temp_dir:
            app_root = Path(temp_dir).resolve()
            os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(app_root)

            self.assertEqual(find_project_root(PACKAGE_ROOT), app_root)

    def test_setup_distribution_creates_files_and_project_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-app-") as temp_dir:
            app_root = Path(temp_dir).resolve()
            runtime = app_root / ".orchestrator"
            runtime.mkdir()
            (app_root / "Themis.xcodeproj").mkdir()
            (app_root / "Themis").mkdir()
            (runtime / "project.json").write_text(
                json.dumps({
                    "project_name": "Themis",
                    "xcode_project": "Themis.xcodeproj",
                    "firebase_plist_path": "Themis/GoogleService-Info.plist",
                    "firebase_distribution": False,
                }),
                encoding="utf-8",
            )
            os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(app_root)

            setup_distribution(team_id="ABC123DEFG")

            project = json.loads((runtime / "project.json").read_text(encoding="utf-8"))
            self.assertTrue((app_root / "scripts" / "distribute_ios.sh").exists())
            self.assertTrue((app_root / "scripts" / "ExportOptions.plist").exists())
            self.assertTrue((app_root / "scripts" / "distribute_ios.sh").stat().st_mode & 0o111)
            self.assertTrue(project["firebase_distribution"])
            self.assertEqual(project["delivery_provider"], "firebase")
            self.assertEqual(project["distribution_script_path"], "scripts/distribute_ios.sh")
            self.assertEqual(project["firebase_plist_path"], "Themis/GoogleService-Info.plist")
            self.assertEqual(project["development_team"], "ABC123DEFG")
            
            # Verify teamID in ExportOptions.plist
            export_opts = (app_root / "scripts" / "ExportOptions.plist").read_text(encoding="utf-8")
            self.assertIn("<key>teamID</key>", export_opts)
            self.assertIn("<string>ABC123DEFG</string>", export_opts)

            # Verify distribute_ios.sh contains automated build number logic
            dist_script_content = (app_root / "scripts" / "distribute_ios.sh").read_text(encoding="utf-8")
            self.assertIn("--build-number", dist_script_content)
            self.assertIn("CURRENT_PROJECT_VERSION", dist_script_content)

    def test_infer_firebase_plist_path_prefers_project_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-app-") as temp_dir:
            app_root = Path(temp_dir).resolve()
            (app_root / "SampleApp").mkdir()
            (app_root / "SampleApp" / "GoogleService-Info.plist").write_text("<plist />", encoding="utf-8")

            self.assertEqual(infer_firebase_plist_path(app_root, "SampleApp"), "SampleApp/GoogleService-Info.plist")

    @patch("pathlib.Path.home")
    def test_setup_distribution_secures_asc_key(self, mock_home) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-app-") as temp_dir:
            app_root = Path(temp_dir).resolve()
            
            # Create a mock home directory
            mock_home_dir = app_root / "mock_home"
            mock_home_dir.mkdir()
            mock_home.return_value = mock_home_dir
            
            # Setup project.json
            runtime = app_root / ".orchestrator"
            runtime.mkdir()
            (runtime / "project.json").write_text(
                json.dumps({
                    "project_name": "Themis",
                    "xcode_project": "Themis.xcodeproj",
                }),
                encoding="utf-8",
            )
            os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(app_root)
            
            # Create a fake key file in an insecure location
            insecure_key_dir = app_root / ".secrets"
            insecure_key_dir.mkdir()
            insecure_key_file = insecure_key_dir / "AuthKey_KEY123.p8"
            insecure_key_file.write_text("my-private-key-data")
            insecure_key_file.chmod(0o777)
            
            # Call setup_distribution
            setup_distribution(
                team_id="ABC123DEFG",
                asc_key_id="KEY123",
                asc_issuer_id="ISSUER123",
                asc_key_path=str(insecure_key_file),
            )
            
            # Verify the key was copied to the secure location in the mock home
            secure_key_file = mock_home_dir / ".private_keys" / "AuthKey_KEY123.p8"
            self.assertTrue(secure_key_file.exists())
            self.assertEqual(secure_key_file.read_text(), "my-private-key-data")
            
            # Verify file permissions on the secure key are 600
            self.assertEqual(secure_key_file.stat().st_mode & 0o777, 0o600)
            
            # Verify that project.json has been updated with the secure key path
            project = json.loads((runtime / "project.json").read_text(encoding="utf-8"))
            self.assertEqual(project["asc_key_path"], str(secure_key_file))


if __name__ == "__main__":
    unittest.main()
