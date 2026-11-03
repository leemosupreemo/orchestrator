import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.project_setup import apply_project_setup, inspect_project
from orchestrator.project_config import load_project_config


class ProjectSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "project"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(Path(self.tmp.name) / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_unconfigured_folder_requires_apply(self):
        result = inspect_project(self.root)
        self.assertFalse(result["configured"])
        self.assertFalse((self.root / ".orchestrator").exists())
        saved = apply_project_setup(self.root, {"project_name": "Example", "build_command": "python3 -m compileall .",
                                               "test_command": "python3 -m unittest discover", "models": ["codex"]})
        self.assertTrue(saved["ok"])
        self.assertEqual(load_project_config(self.root).project_name, "Example")
        self.assertTrue(inspect_project(self.root)["configured"])

    def test_invalid_settings_write_nothing(self):
        result = apply_project_setup(self.root, {"project_name": "Example", "models": ["codex"]})
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])
        self.assertFalse((self.root / ".orchestrator").exists())

    def test_existing_configuration_keeps_unknown_settings(self):
        runtime = self.root / ".orchestrator"
        runtime.mkdir()
        path = runtime / "project.json"
        path.write_text(json.dumps({"project_name": "Original", "build_command": "true", "test_command": "true",
                                   "custom_setting": {"keep": True}}))
        result = apply_project_setup(self.root, {"project_name": "Renamed", "models": ["codex"]})
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(path.read_text())["custom_setting"], {"keep": True})

    def test_explicit_root_does_not_use_other_project_environment(self):
        with patch.dict(os.environ, {"ORCHESTRATOR_CONFIG": "/does/not/exist", "ORCHESTRATOR_RUNTIME_DIR": "/outside"}):
            config = load_project_config(self.root)
        self.assertEqual(config.root, self.root)
        self.assertEqual(config.runtime_dir, self.root / ".orchestrator")

    def test_runtime_symlink_cannot_write_outside_selected_folder(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.root / ".orchestrator").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            apply_project_setup(self.root, {"build_command": "true", "test_command": "true", "models": ["codex"]})
        self.assertEqual(list(outside.iterdir()), [])
