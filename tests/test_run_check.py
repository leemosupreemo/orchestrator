from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

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

    def test_default_models_are_narrowed_to_ones_a_machine_runs(self):
        machines = [{"name": "a", "models": ["m2"]}, {"name": "off", "enabled": False, "models": ["m3"]}]
        self.assertEqual(run_check.models_machines_can_run(["m1", "m2", "m3"], machines), ["m2"])
        self.assertEqual(run_check.models_machines_can_run(["m1"], machines), [])

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

    def test_low_disk_is_visible_alongside_a_model_mismatch(self):
        self.assertEqual(self.ids({"allowed_models": ["other-model"]}, [machine()], disk=12),
                         ["model-not-on-machine", "low-disk"])

    def test_low_disk_says_how_much_space_to_free(self):
        self.assertIn("Free at least 6 GB", check({}, [machine()], disk=9)[0]["text"])

    def test_low_disk_is_not_blamed_when_a_remote_machine_could_run_it(self):
        self.assertEqual(self.ids({}, [machine(), machine("box", mode="remote")], disk=2), [])

    def test_only_jobs_about_to_be_scheduled_are_checked(self):
        for status in ("review-needed", "completed", "executing", "human-needed"):
            self.assertEqual(run_check.blockers({"status": status}, [], 1, 15, overlap), [], status)


@unittest.skipUnless(shutil.which("node"), "node is required")
class JobStartWarningTests(unittest.TestCase):
    def test_startup_reasons_are_short_with_full_wrapping_details(self):
        app = (Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js").read_text()
        start = app.index("function jobStartBlockers(")
        end = app.index("\nfunction ", start + 1)
        script = f"""
const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
{app[start:end]}
const blockers = [
  {{ id: "low-disk", text: "Only 12 GB free", route: "" }},
  {{ id: "build-tool", text: "<long build command>", route: "#/checkup", fix: "Setup check" }},
  {{ id: "test-tool", text: "dotnet test", route: "" }},
];
console.log(JSON.stringify({{ warning: jobStartBlockers(blockers), empty: jobStartBlockers([]) }}));
"""
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out["empty"], "")
        summary, details = out["warning"].split('<details class="fold">')
        self.assertEqual(out["warning"].count("This job can't start yet"), 1)
        self.assertIn("Low disk space · Build command unavailable · Test command unavailable", summary)
        self.assertNotIn("dotnet test", summary)
        self.assertIn('class="assumptions wrap-anywhere"', details)
        self.assertIn("Only 12 GB free", details)
        self.assertIn("&lt;long build command>", details)
        self.assertIn('href="#/checkup">Setup check</a>', details)
        self.assertIn("dotnet test", details)
        self.assertIn("Check again", details)


if __name__ == "__main__":
    unittest.main()
