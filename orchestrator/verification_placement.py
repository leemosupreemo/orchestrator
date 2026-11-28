"""Capability checks and finish-time estimates for a two-check verification pair."""
from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from itertools import product
from statistics import median

PROTOCOL_VERSION = 1
MINIMUM_SAMPLES = 3


@dataclass(frozen=True)
class WorkerProbe:
    protocol: int
    repository: str
    disk_free_bytes: int
    toolchains: Mapping[str, str]
    candidate_available: bool


def helper_policy_blocker(machine: Mapping[str, object], allowed_machines: Collection[str] | None) -> str | None:
    if machine.get("execution_mode") != "ssh" or machine.get("enabled") is not True \
            or machine.get("verification_helper") is not True:
        return "project_policy"
    if allowed_machines and machine.get("name") not in allowed_machines:
        return "job_policy"
    return None


def helper_blockers(
    machine: Mapping[str, object], allowed_machines: Collection[str] | None, probe: WorkerProbe | None,
    *, repository: str, minimum_disk_bytes: int, expected_toolchains: Mapping[str, str],
) -> tuple[str, ...]:
    """Check project and job policy before interpreting a helper's capability report."""
    if policy := helper_policy_blocker(machine, allowed_machines):
        return (policy,)
    if probe is None:
        return ("unreachable",)
    blockers = []
    if probe.protocol != PROTOCOL_VERSION:
        blockers.append("protocol")
    if probe.repository != repository:
        blockers.append("repository")
    if probe.disk_free_bytes < minimum_disk_bytes:
        blockers.append("disk")
    if not probe.candidate_available:
        blockers.append("candidate")
    blockers.extend(f"toolchain:{check}" for check, version in expected_toolchains.items()
                    if probe.toolchains.get(check) != version)
    return tuple(blockers)


@dataclass(frozen=True)
class CheckTiming:
    seconds: tuple[float, ...] = ()

    def estimate(self) -> float | None:
        return median(self.seconds) if len(self.seconds) >= MINIMUM_SAMPLES else None


@dataclass(frozen=True)
class MachineOffer:
    name: str
    # Membership means eligible. An empty sample list means eligible but uncalibrated.
    checks: Mapping[str, CheckTiming]
    setup: CheckTiming
    queue_seconds: float = 0


@dataclass(frozen=True)
class Placement:
    assignments: tuple[str, str] | None
    seconds: float | None
    local_seconds: float | None
    reason: str


def _finish_time(
    checks: tuple[str, str], assignment: tuple[str, str], machines: Mapping[str, MachineOffer],
) -> float | None:
    finish: dict[str, float] = {}
    for check, name in zip(checks, assignment, strict=True):
        machine = machines[name]
        estimate = machine.checks[check].estimate()
        if estimate is None:
            return None
        if name not in finish:
            setup = machine.setup.estimate()
            if setup is None:
                return None
            finish[name] = machine.queue_seconds + setup
        finish[name] += estimate
    return max(finish.values())


def choose_placement(
    checks: tuple[str, str], owner: MachineOffer, helper: MachineOffer | None,
) -> Placement:
    """Compare every eligible assignment with the owner-only baseline."""
    machines = {owner.name: owner}
    if helper is not None:
        if helper.name == owner.name:
            raise ValueError("The verification helper must have its own machine name.")
        machines[helper.name] = helper
    choices = [[name for name, machine in machines.items() if check in machine.checks] for check in checks]
    predictions = {(first, second): _finish_time(checks, (first, second), machines)
                   for first, second in product(*choices)}
    if not predictions:
        return Placement(None, None, None, "no_eligible_placement")

    local = (owner.name, owner.name)
    local_seconds = predictions.get(local)
    measured = [(seconds, assignment) for assignment, seconds in predictions.items() if seconds is not None]
    if local not in predictions:
        if measured:
            seconds, assignment = min(measured, key=lambda option: option[0])
        else:
            assignment, seconds = next(iter(predictions)), None
        return Placement(assignment, seconds, None, "helper_required")

    if local_seconds is None:
        return Placement(local, None, None, "insufficient_timing")
    fastest_seconds, fastest = min(measured, key=lambda option: option[0])
    if local_seconds - fastest_seconds > max(10, local_seconds * 0.1):
        return Placement(fastest, fastest_seconds, local_seconds, "faster_finish")
    if len(measured) < len(predictions):
        return Placement(local, local_seconds, local_seconds, "insufficient_timing")
    return Placement(local, local_seconds, local_seconds, "owner_preferred")
