from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from orchestrator.config_validation import validate_machine_config, validate_project_config  # noqa: E402
from orchestrator.project_config import ProjectConfig  # noqa: E402


def make_config(root: Path, **overrides) -> ProjectConfig:
    data = {
        "root": root,
        "runtime_dir": root / ".orchestrator",
        "package_prompts_dir": PACKAGE_ROOT / "orchestrator" / "prompts",
        "project_name": "SampleApp",
        "base_branch": "main",
        "git_remote": None,
        "xcode_project": "SampleApp.xcodeproj",
        "xcode_workspace": None,
        "scheme": "SampleApp",
        "test_target": "SampleAppTests",
        "derived_data_path": "/tmp/sampleapp_dd",
        "build_command": None,
        "test_command": None,
        "app_bundle_id": None,
        "backend_test_command": None,
        "branch_prefix": "ai/issue",
        "pr_base_branch": "main",
        "delivery_provider": None,
        "distribution_script_path": None,
        "delivery_method": None,
        "firebase_plist_path": None,
        "provisioning_profile_specifier": None,
        "development_team": None,
        "asc_key_id": None,
        "asc_issuer_id": None,
        "asc_key_path": None,
        "visual_app_path": None,
        "remote_package_install_path": "~/.orchestrator/package",
        "firebase_distribution": False,
        "notification_display_name": "SampleApp AI Orchestrator",
    }
    data.update(overrides)
    return ProjectConfig(**data)


class ConfigValidationTests(unittest.TestCase):
    def test_generic_project_does_not_require_scheme_or_test_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp), xcode_project=None, scheme=None,
                                 test_target="", build_command="cargo build", test_command="cargo test")
            self.assertEqual(validate_project_config(config), [])

    def test_xcode_project_still_requires_scheme(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SampleApp.xcodeproj").mkdir()
            self.assertTrue(any("scheme is required" in error
                                for error in validate_project_config(make_config(root, scheme=None))))

    def test_generic_project_requires_test_command_even_with_legacy_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp), xcode_project=None, build_command="cargo build")
            self.assertIn("Configure test_command for a non-Xcode project.", validate_project_config(config))

    def test_project_config_accepts_basic_xcode_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()

            self.assertEqual(validate_project_config(make_config(root)), [])

    def test_project_config_requires_existing_xcode_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            errors = validate_project_config(make_config(Path(temp_dir)))

            self.assertIn("xcode_project does not exist: SampleApp.xcodeproj", errors)

    def test_project_config_validates_firebase_distribution_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()
            errors = validate_project_config(make_config(
                root,
                firebase_distribution=True,
                delivery_provider="firebase",
                distribution_script_path="scripts/distribute_ios.sh",
                firebase_plist_path="SampleApp/GoogleService-Info.plist",
            ))

            self.assertIn("distribution_script_path does not exist: scripts/distribute_ios.sh. Run 'orchestrator wizard' to generate it.", errors)
            self.assertIn("firebase_plist_path does not exist: SampleApp/GoogleService-Info.plist. Download this from Firebase Console.", errors)

    def test_distribution_config_flags_stale_script_missing_firebase_plist_arg(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "distribute_ios.sh").write_text(
                "#!/bin/bash\n"
                "while [[ \"$#\" -gt 0 ]]; do\n"
                "  case $1 in\n"
                "    --project) shift ;;\n"
                "    *) echo \"Unknown parameter passed: $1\"; exit 1 ;;\n"
                "  esac\n"
                "  shift\n"
                "done\n",
                encoding="utf-8",
            )

            errors = make_config(
                root,
                distribution_script_path="scripts/distribute_ios.sh",
                firebase_plist_path="SampleApp/GoogleService-Info.plist",
                development_team="ABC123DEFG",
                delivery_method="debugging",
            ).validate_distribution_config()

            self.assertEqual(len(errors), 1)
            self.assertIn("does not accept --firebase-plist", errors[0])
            self.assertIn("orchestrator wizard", errors[0])

    def test_distribution_config_validates_asc_key_path_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            # 1. Test invalid asc_key_path (not in secure directory)
            config = make_config(
                root,
                development_team="ABC123DEFG",
                delivery_method="debugging",
                asc_key_id="KEY123",
                asc_issuer_id="ISSUER123",
                asc_key_path=".secrets/AuthKey_KEY123.p8",
            )
            errors = config.validate_distribution_config()
            self.assertTrue(any("is not in a designated secure Xcode directory" in e for e in errors))

            # 2. Test valid secure path (e.g. ~/.private_keys/AuthKey_KEY123.p8)
            secure_key_path = Path.home() / ".private_keys" / "AuthKey_KEY123_test.p8"
            config_secure = make_config(
                root,
                development_team="ABC123DEFG",
                delivery_method="debugging",
                asc_key_id="KEY123",
                asc_issuer_id="ISSUER123",
                asc_key_path=str(secure_key_path),
            )
            
            # Since the file doesn't exist, we shouldn't get a permissions error, only checks the parent dir.
            errors_secure = config_secure.validate_distribution_config()
            self.assertFalse(any("is not in a designated secure Xcode directory" in e for e in errors_secure))

            # 3. Test insecure file permissions if the file actually exists
            secure_key_path.parent.mkdir(parents=True, exist_ok=True)
            secure_key_path.write_text("fake-key-data")
            try:
                secure_key_path.chmod(0o777)
                errors_perms = config_secure.validate_distribution_config()
                self.assertTrue(any("has insecure permissions" in e for e in errors_perms))
                
                # Set secure permissions (0o600)
                secure_key_path.chmod(0o600)
                errors_perms_ok = config_secure.validate_distribution_config()
                self.assertFalse(any("has insecure permissions" in e for e in errors_perms_ok))
            finally:
                if secure_key_path.exists():
                    secure_key_path.unlink()

    def test_project_config_validates_visual_check_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()
            errors = validate_project_config(make_config(root, visual_app_path="build/SampleApp.app"))

            self.assertIn("visual_app_path does not exist: build/SampleApp.app", errors)
            self.assertIn("visual_app_path requires app_bundle_id for simulator launch.", errors)

    def test_project_config_flags_insecure_git_remote(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "SampleApp.xcodeproj").mkdir()
            
            # 1. Test with safe remote
            self.assertEqual(validate_project_config(make_config(root, git_remote="git@github.com:user/repo.git")), [])
            
            # 2. Test with PAT remote
            errors = validate_project_config(make_config(
                root,
                git_remote="https://github_pat_123@github.com/user/repo.git"
            ))
            self.assertTrue(any("contains a hardcoded Personal Access Token" in e for e in errors))

    def test_machine_config_validates_ssh_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir)
            (config_dir / "machines.json").write_text(
                json.dumps({
                    "machines": [
                        {
                            "name": "remote",
                            "execution_mode": "ssh",
                            "repo_path": "/Users/me/App",
                        }
                    ]
                }),
                encoding="utf-8",
            )

            errors = validate_machine_config(config_dir)

            self.assertIn("remote: ssh_target is required for SSH machines.", errors)


if __name__ == "__main__":
    unittest.main()
