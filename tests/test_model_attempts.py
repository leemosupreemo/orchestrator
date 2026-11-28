import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.model_attempts import AttemptAudit


class AttemptAuditTests(unittest.TestCase):
    def test_shadow_uses_comparable_responses_and_ignores_availability(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "attempts.jsonl"
            for _ in range(3):
                audit = AttemptAudit(path, "builder", "feature-plan", ["first", "second"])
                audit.record("first", "invalid_json", 1.0)
                audit.record("second", "response_accepted", 1.0)
                audit.record("second", "quota", 1.0)
                audit.record("outside", "response_accepted", 1.0)
            other = AttemptAudit(path, "planner", "feature-plan", ["first"])
            other.record("first", "response_accepted", 1.0)
            other = AttemptAudit(path, "builder", "quick-fix", ["first"])
            other.record("first", "response_accepted", 1.0)
            audit = AttemptAudit(path, "builder", "feature-plan", ["first", "second"])
            audit.finish("first")
            comparison = json.loads(path.read_text().splitlines()[-1])
            self.assertEqual(comparison["recommended_model"], "second")
            self.assertEqual(comparison["actual_model"], "first")
            self.assertEqual(comparison["evidence"], {"first": {"accepted": 0, "total": 3},
                                                       "second": {"accepted": 3, "total": 3}})

    def test_thin_evidence_keeps_the_catalog_pick(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AttemptAudit(Path(tmp) / "attempts.jsonl", "builder", "feature-plan", ["first", "second"])
            self.assertEqual(audit.recommended, "first")
            self.assertEqual(audit.basis, "insufficient_evidence")
