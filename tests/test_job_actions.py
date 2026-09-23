from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"
for p in (PACKAGE_ROOT, SCRIPTS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import job_actions  # noqa: E402


class JobActionsTests(unittest.TestCase):
    def test_answer_records_clarification_and_replans(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text(json.dumps({"status": "human-needed", "type": "bug-fix",
                                        "human_clarification_question": "Which lobby size?"}))
            with patch("job_actions.subprocess.call", return_value=0) as call:
                self.assertEqual(job_actions.main(["answer", str(path), "--answer", "Four players"]), 0)
            job = json.loads(path.read_text())
            argv = call.call_args[0][0]
        self.assertEqual(job["clarification_history"][-1]["answer"], "Four players")
        self.assertIsNone(job["human_clarification_question"])
        self.assertEqual(argv[2:6], ["bug", "--no-dispatch", "--update", str(path.resolve())])
        self.assertIn("Four players", argv[-1])

    def test_answer_refuses_when_no_question_is_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text(json.dumps({"status": "planned"}))
            with patch("job_actions.subprocess.call") as call:
                self.assertEqual(job_actions.main(["answer", str(path), "--answer", "x"]), 1)
            call.assert_not_called()

    def test_merge_uses_the_console_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text(json.dumps({"status": "review-needed", "pr_number": 141}))
            with patch("dev_console.handle_merge_cleanup") as merge:
                self.assertEqual(job_actions.main(["merge", str(path)]), 0)
        job = merge.call_args[0][0]
        self.assertEqual((job["pr_number"], job["_path"]), (141, path.resolve()))


if __name__ == "__main__":
    unittest.main()
