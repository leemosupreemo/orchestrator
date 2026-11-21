import unittest

from orchestrator import model_outcomes


class ModelOutcomesTests(unittest.TestCase):
    def test_records_the_selected_and_actual_model_with_a_stage_result(self):
        job = {"type": "feature-plan"}
        model_outcomes.record(job, "builder", "model-a", "model-b", "build_checks_passed", tasks=[1, 2])
        entry, = job["model_outcomes"]
        self.assertEqual(
            {k: v for k, v in entry.items() if k not in {"id", "at"}},
            {"role": "builder", "selected_model": "model-a", "actual_model": "model-b",
             "job_type": "feature-plan", "result": "build_checks_passed", "tasks": [1, 2]},
        )

    def test_missing_model_is_not_an_outcome_and_sync_deduplicates(self):
        job = {}
        model_outcomes.record(job, "planner", "model-a", None, "plan_gate_passed")
        self.assertEqual(job, {})
        model_outcomes.record(job, "reviewer", "model-a", "model-a", "review_generated")
        entries = job["model_outcomes"]
        self.assertEqual(model_outcomes.merge(entries, entries + [{"unexpected": "raw notes"}]), entries)
