import unittest

from orchestrator.verification_placement import (
    CheckTiming, MachineOffer, WorkerProbe, choose_placement, helper_blockers,
)


def timing(seconds, samples=3):
    return CheckTiming((seconds,) * samples)


def offer(name, *, setup=0, queue=0, a=60, b=60, samples=3):
    checks = {check: timing(seconds, samples) for check, seconds in (("a", a), ("b", b)) if seconds is not None}
    return MachineOffer(name, checks, timing(setup, samples), queue)


class PlacementTests(unittest.TestCase):
    def test_split_shaves_time_from_local_sequence(self):
        result = choose_placement(("a", "b"), offer("pro"), offer("air", setup=5))
        self.assertEqual(result.assignments, ("pro", "air"))
        self.assertEqual(result.seconds, 65)
        self.assertEqual(result.local_seconds, 120)

    def test_setup_queue_and_uncertainty_can_favor_local(self):
        for helper in (offer("air", setup=130), offer("air", queue=125),
                       offer("air", setup=51), offer("air", samples=2), None):
            with self.subTest(helper=helper):
                result = choose_placement(("a", "b"), offer("pro"), helper)
                self.assertEqual(result.assignments, ("pro", "pro"))

    def test_helper_only_capability_wins_even_without_calibration(self):
        result = choose_placement(("a", "b"), offer("pro", b=None), offer("air", a=None, samples=0))
        self.assertEqual(result.assignments, ("pro", "air"))
        self.assertIsNone(result.seconds)
        self.assertEqual(result.reason, "helper_required")

    def test_reports_missing_calibration_separately_from_a_measured_loss(self):
        unknown = choose_placement(("a", "b"), offer("pro"), offer("air", samples=2))
        slow = choose_placement(("a", "b"), offer("pro"), offer("air", setup=130))
        self.assertEqual(unknown.reason, "insufficient_timing")
        self.assertEqual(slow.reason, "owner_preferred")

    def test_no_runner_does_not_invent_an_assignment(self):
        result = choose_placement(("a", "b"), offer("pro", b=None), offer("air", b=None))
        self.assertIsNone(result.assignments)
        self.assertEqual(result.reason, "no_eligible_placement")

    def test_both_checks_on_helper_pay_setup_once(self):
        result = choose_placement(("a", "b"), offer("pro"), offer("air", setup=10, a=5, b=5))
        self.assertEqual(result.assignments, ("air", "air"))
        self.assertEqual(result.seconds, 20)

    def test_helper_needs_project_authorization_and_toolchain_but_no_models(self):
        machine = {"name": "air", "enabled": True, "execution_mode": "ssh", "verification_helper": True}
        probe = WorkerProbe(protocol=1, repository="repo", disk_free_bytes=1000,
                            toolchains={"b": "dotnet 10"}, candidate_available=True)
        request = {"repository": "repo", "minimum_disk_bytes": 100, "expected_toolchains": {"b": "dotnet 10"}}
        self.assertEqual(helper_blockers(machine, ["air"], probe, **request), ())
        self.assertIn("job_policy", helper_blockers(machine, ["local"], probe, **request))
        self.assertIn("project_policy", helper_blockers({**machine, "verification_helper": False}, None, probe, **request))
        stale = WorkerProbe(protocol=0, repository="other", disk_free_bytes=0,
                            toolchains={}, candidate_available=False)
        self.assertEqual(set(helper_blockers(machine, None, stale, **request)),
                         {"protocol", "repository", "disk", "candidate", "toolchain:b"})
        self.assertEqual(helper_blockers(machine, None, None, **request), ("unreachable",))
