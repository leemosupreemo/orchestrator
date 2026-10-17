from __future__ import annotations

import unittest

from orchestrator import run_check


def overlap(a, b):
    return a == b


def machine(name="local", models=("m1",), mode="local", enabled=True):
    return {"name": name, "models": list(models), "execution_mode": mode, "enabled": enabled}


def check(job, machines, disk=100.0, floor=15):
    return run_check.blockers({"status": "planned", **job}, machines, disk, floor, overlap)


class RunCheckTests(unittest.TestCase):
    def ids(self, *a, **k):
        return [b["id"] for b in check(*a, **k)]

    def test_a_runnable_job_has_no_blockers(self):
        self.assertEqual(check({"allowed_models": ["m1"], "allowed_machines": ["local"]}, [machine()]), [])

    def test_no_machine_points_at_adding_one(self):
        b = check({}, [])
        self.assertEqual([x["id"] for x in b], ["no-machine"])
        self.assertEqual(b[0]["route"], "#/config/fleet")
        self.assertEqual(self.ids({}, [machine(enabled=False)]), ["no-machine"])

    def test_a_job_limited_to_a_machine_that_is_gone(self):
        self.assertEqual(self.ids({"allowed_machines": ["laptop"]}, [machine("local")]), ["machine-not-allowed"])

    def test_allowed_models_that_no_usable_machine_runs(self):
        b = check({"allowed_models": ["paid-model"]}, [machine(models=["m1"])])
        self.assertEqual([x["id"] for x in b], ["model-not-on-machine"])
        self.assertEqual(b[0]["route"], "#/config/models")

    def test_no_model_on_any_machine(self):
        self.assertEqual(self.ids({}, [machine(models=[])]), ["no-model"])

    def test_low_disk_on_a_local_only_setup_is_flagged_with_the_numbers(self):
        b = check({}, [machine()], disk=6.2)
        self.assertEqual([x["id"] for x in b], ["low-disk"])
        self.assertIn("6 GB", b[0]["text"])
        self.assertIn("15 GB", b[0]["text"])

    def test_low_disk_is_not_blamed_when_a_remote_machine_could_run_it(self):
        self.assertEqual(self.ids({}, [machine(), machine("box", mode="remote")], disk=2), [])

    def test_only_jobs_about_to_be_scheduled_are_checked(self):
        for status in ("review-needed", "completed", "executing", "human-needed"):
            self.assertEqual(run_check.blockers({"status": status}, [], 1, 15, overlap), [], status)


if __name__ == "__main__":
    unittest.main()
