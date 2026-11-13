"""Loaded first by `discover -s tests`, so the run uses a throwaway state folder (see tests/__init__.py)."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests  # noqa: E402,F401  sets ORCHESTRATOR_USER_STATE_DIR

from orchestrator.project_config import recent_projects_path  # noqa: E402


class UserStateIsolationTests(unittest.TestCase):
    def test_tests_do_not_use_this_computers_orchestrator_state(self):
        self.assertTrue(os.environ.get("ORCHESTRATOR_USER_STATE_DIR"))
        self.assertNotEqual(recent_projects_path().parent, (Path.home() / ".orchestrator").resolve())


if __name__ == "__main__":
    unittest.main()
