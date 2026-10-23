from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.audit import AuditLog


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state" / "audit.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_newest_first_with_private_permissions_and_bounded_values(self):
        log = AuditLog(self.path)
        with patch("orchestrator.audit.time.time", side_effect=[100.125, 101.5]):
            log.record("sign_in", "  owner@example.com  ", ip="127.0.0.1")
            log.record("denied", "friend@example.com", what="x" * 250)

        recent = log.recent()
        self.assertEqual([entry["event"] for entry in recent], ["denied", "sign_in"])
        self.assertEqual(recent[1], {"at": 100.125, "event": "sign_in", "who": "owner@example.com", "ip": "127.0.0.1"})
        self.assertEqual(len(recent[0]["what"]), 200)
        self.assertTrue(recent[0]["what"].endswith("…"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_rotates_before_appending_and_keeps_the_previous_file(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"event": "old"}) + "\n")
        log = AuditLog(self.path, max_bytes=1)
        log.record("new", "owner@example.com")

        rotated = self.path.with_name("audit.jsonl.1")
        self.assertEqual(json.loads(rotated.read_text())["event"], "old")
        self.assertEqual(log.recent()[0]["event"], "new")

    def test_missing_and_malformed_lines_do_not_break_recent_activity(self):
        log = AuditLog(self.path)
        self.assertEqual(log.recent(), [])
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"event":"good","who":"owner"}\nnot json\n')
        self.assertEqual(log.recent(), [{"event": "good", "who": "owner"}])


if __name__ == "__main__":
    unittest.main()
