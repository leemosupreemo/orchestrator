from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.project_config import (
    ProjectConfig,
    active_project_root,
    find_project_root,
    forget_project,
    load_project_config,
    load_recent_projects,
    project_display_name,
    recent_projects_path,
    remember_project,
    resolve_project_reference,
    safe_cwd,
    safe_resolve,
    user_state_dir,
)


def make_test_config(root: Path, **overrides) -> ProjectConfig:
    data = {
        "root": root,
        "runtime_dir": root / ".orchestrator",
        "package_prompts_dir": root / "prompts",
        "project_name": "TestApp",
        "base_branch": "main",
        "git_remote": None,
        "xcode_project": "TestApp.xcodeproj",
        "xcode_workspace": None,
        "scheme": "TestApp",
        "test_target": "TestAppTests",
        "derived_data_path": "/tmp/dd",
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
        "notification_display_name": "TestApp AI Orchestrator",
        "signing_style": "manual",
    }
    data.update(overrides)
    return ProjectConfig(**data)


class ProjectConfigTests(unittest.TestCase):

    def test_safe_cwd_and_resolve_retries(self):
        call_count_cwd = [0]
        orig_cwd = Path.cwd

        def interrupted_cwd():
            call_count_cwd[0] += 1
            if call_count_cwd[0] <= 2:
                raise InterruptedError("transient")
            return orig_cwd()

        with patch.object(Path, "cwd", side_effect=interrupted_cwd):
            res = safe_cwd()
            self.assertEqual(res, orig_cwd())

        # Test safe_cwd fallback to PWD
        with patch.object(Path, "cwd", side_effect=InterruptedError("all failed")), \
             patch.dict(os.environ, {"PWD": str(orig_cwd())}):
            res2 = safe_cwd()
            self.assertEqual(res2, orig_cwd())

        # Test safe_resolve retry
        call_count_resolve = [0]
        target = Path("/tmp")
        orig_resolve = target.resolve

        def interrupted_resolve(*args, **kwargs):
            call_count_resolve[0] += 1
            if call_count_resolve[0] <= 2:
                raise InterruptedError("transient")
            return orig_resolve(*args, **kwargs)

        with patch.object(Path, "resolve", side_effect=interrupted_resolve):
            res3 = safe_resolve(target)
            self.assertEqual(res3, orig_resolve())

    def test_user_state_dir_and_recent_projects_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp_state, tempfile.TemporaryDirectory() as tmp_proj:
            with patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": tmp_state}):
                self.assertEqual(user_state_dir(), safe_resolve(Path(tmp_state)))
                self.assertEqual(recent_projects_path(), safe_resolve(Path(tmp_state)) / "projects.json")

                # Initially empty
                initial = load_recent_projects()
                self.assertEqual(initial["projects"], [])
                self.assertIsNone(initial["active"])

                proj_root = safe_resolve(Path(tmp_proj))
                remember_project(proj_root, name="TestProject", active=True, source_type="git", source_label="Test")

                loaded = load_recent_projects(prune_missing=True)
                self.assertEqual(loaded["active"], "TestProject")
                self.assertEqual(len(loaded["projects"]), 1)
                self.assertEqual(loaded["projects"][0]["name"], "TestProject")

                # Test resolve_project_reference and active_project_root
                self.assertEqual(active_project_root(), proj_root)
                self.assertEqual(resolve_project_reference("TestProject"), proj_root)
                self.assertEqual(resolve_project_reference(str(proj_root)), proj_root)
                self.assertIsNone(resolve_project_reference("NonExistent"))
                self.assertIsNone(resolve_project_reference(None))

                # Test forget_project
                forgotten = forget_project("TestProject")
                self.assertTrue(forgotten)
                after_forget = load_recent_projects(prune_missing=False)
                self.assertEqual(after_forget["projects"], [])
                self.assertIsNone(after_forget["active"])

    def test_find_project_root_precedence_and_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = safe_resolve(Path(tmp))
            nested = root / "subdir" / "deep"
            nested.mkdir(parents=True)

            # Test explicit ORCHESTRATOR_PROJECT_ROOT
            with patch.dict(os.environ, {"ORCHESTRATOR_PROJECT_ROOT": str(root)}):
                self.assertEqual(find_project_root(nested), root)

            # Test discovery via .git
            (root / ".git").mkdir()
            self.assertEqual(find_project_root(nested), root)
            (root / ".git").rmdir()

            # Test discovery via Package.swift
            (root / "Package.swift").write_text("// swift-tools-version:5.5")
            self.assertEqual(find_project_root(nested), root)
            (root / "Package.swift").unlink()

            # Test discovery via .orchestrator/project.json
            (root / ".orchestrator").mkdir(exist_ok=True)
            (root / ".orchestrator" / "project.json").write_text("{}")
            self.assertEqual(find_project_root(nested), root)
            (root / ".orchestrator" / "project.json").unlink()

            # Test discovery via *.xcodeproj
            (root / "MyApp.xcodeproj").mkdir()
            self.assertEqual(find_project_root(nested), root)

    def test_project_display_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orch_dir = root / ".orchestrator"
            orch_dir.mkdir()
            (orch_dir / "project.json").write_text(json.dumps({"project_name": "CustomAppName"}))
            self.assertEqual(project_display_name(root), "CustomAppName")

            (orch_dir / "project.json").write_text(json.dumps({}))
            self.assertEqual(project_display_name(root), root.name)

    def test_validate_distribution_config_security_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = safe_resolve(Path(tmp))
            home = safe_resolve(Path.home())

            # Missing development_team and delivery_method
            config = make_test_config(root, signing_style="manual")
            errors = config.validate_distribution_config()
            self.assertTrue(any("Apple Development Team ID" in e for e in errors))
            self.assertTrue(any("Distribution method" in e for e in errors))
            self.assertTrue(any("Neither App Store Connect API keys nor a manual Provisioning Profile" in e for e in errors))

            # Partial ASC keys with manual profile configured
            config2 = make_test_config(
                root,
                signing_style="manual",
                development_team="TEAM123",
                delivery_method="app-store",
                provisioning_profile_specifier="MyProfile",
                asc_key_id="KEY123",
            )
            errors2 = config2.validate_distribution_config()
            self.assertTrue(any("partially configured" in e for e in errors2))

            # Insecure ASC key location inside repo
            repo_key = root / "key.p8"
            repo_key.write_text("dummy")
            config3 = make_test_config(
                root,
                signing_style="manual",
                development_team="TEAM123",
                delivery_method="app-store",
                asc_key_id="KEY123",
                asc_issuer_id="ISSUER123",
                asc_key_path=str(repo_key),
            )
            errors3 = config3.validate_distribution_config()
            self.assertTrue(any("not in a designated secure Xcode directory" in e for e in errors3))

            # Key in ~/.private_keys with insecure permissions (0o666)
            priv_dir = home / ".private_keys"
            priv_dir.mkdir(parents=True, exist_ok=True)
            secure_key = priv_dir / "test_key_temp.p8"
            secure_key.write_text("dummy")
            try:
                secure_key.chmod(0o666)
                config4 = make_test_config(
                    root,
                    signing_style="manual",
                    development_team="TEAM123",
                    delivery_method="app-store",
                    asc_key_id="KEY123",
                    asc_issuer_id="ISSUER123",
                    asc_key_path=str(secure_key),
                )
                errors4 = config4.validate_distribution_config()
                self.assertTrue(any("insecure permissions" in e for e in errors4))

                # Correct permissions 0600
                secure_key.chmod(0o600)
                config5 = make_test_config(
                    root,
                    signing_style="manual",
                    development_team="TEAM123",
                    delivery_method="app-store",
                    asc_key_id="KEY123",
                    asc_issuer_id="ISSUER123",
                    asc_key_path=str(secure_key),
                )
                errors5 = config5.validate_distribution_config()
                self.assertFalse(any("insecure permissions" in e for e in errors5))
            finally:
                if secure_key.exists():
                    secure_key.unlink()

            # Outdated distribution script lacking --firebase-plist
            dist_script = root / "dist.sh"
            dist_script.write_text("#!/bin/bash\necho dist")
            config6 = make_test_config(
                root,
                signing_style="manual",
                development_team="TEAM123",
                delivery_method="app-store",
                distribution_script_path="dist.sh",
                firebase_plist_path="firebase.plist",
            )
            errors6 = config6.validate_distribution_config()
            self.assertTrue(any("outdated" in e and "--firebase-plist" in e for e in errors6))


if __name__ == "__main__":
    unittest.main()
