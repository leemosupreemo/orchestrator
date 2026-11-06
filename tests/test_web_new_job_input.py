from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "orchestrator" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from new_job import web_raw_input


class WebNewJobInputTests(unittest.TestCase):
    def test_quick_job_preserves_full_prompt(self):
        self.assertEqual(web_raw_input("quick", {"prompt": "Improve lobby\nKeep tests"}), "Improve lobby\nKeep tests")

    def test_bug_job_preserves_repro_and_expected_behavior(self):
        self.assertEqual(
            web_raw_input("bug", {"prompt": "Lobby fails", "repro": "Join twice", "expected": "Reconnect"}),
            "SUMMARY: Lobby fails\n\nREPRO STEPS:\nJoin twice\n\nEXPECTED BEHAVIOR:\nReconnect",
        )

    def test_empty_prompt_is_rejected(self):
        with self.assertRaises(ValueError):
            web_raw_input("feature", {"prompt": "   "})


if __name__ == "__main__":
    unittest.main()
