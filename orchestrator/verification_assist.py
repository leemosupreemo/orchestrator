"""Coordinate a configured verification pair through the job's extra-test stage."""
from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable
from uuid import uuid4

from orchestrator import test_commands, verification_candidate
from orchestrator.scripts.verification_worker import toolchain
from orchestrator.verification import command_digest
from orchestrator.verification_attempts import AttemptKey, LEASE_SECONDS
from orchestrator.verification_coordinator import run_remote_check
from orchestrator.verification_placement import CheckTiming, MachineOffer, choose_placement, helper_blockers, helper_policy_blocker
from orchestrator.verification_transport import ProbeRequest, repository_identity, ssh_probe

MINIMUM_DISK_BYTES = 1024 ** 3


def _read_json(path: Path) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def _timing(values: object) -> CheckTiming:
    if not isinstance(values, list):
        return CheckTiming()
    return CheckTiming(tuple(value for value in values[-20:]
                             if type(value) in (float, int) and 0 <= value <= 86_400 and math.isfinite(value)))


def _checks_timing(data: dict, name: str, checks: dict[str, str]) -> dict[str, CheckTiming]:
    row = data.get(name)
    row = row if isinstance(row, dict) else {}
    return {check: _timing(row.get(digest)) for check, digest in checks.items()}


def _pair(
    pair: list[test_commands.ExtraTestCommand], root: Path, out_dir: Path, runtime: Path, job: dict,
    *, job_owned: bool, is_current: Callable[[], bool],
) -> tuple[dict[str, dict], str] | None:
    allowed = job.get("allowed_machines")
    owner_name = job.get("assigned_machine") or "local"
    machines = _read_json(runtime / "config" / "machines.json").get("machines")
    helper = next((entry for entry in machines if isinstance(entry, dict) and isinstance(entry.get("name"), str)
                   and entry["name"] != owner_name
                   and not helper_policy_blocker(entry, allowed)), None) if isinstance(machines, list) else None
    if helper is None:
        return None
    helper_name = helper["name"]
    timings = _read_json(runtime / "config" / "verification-timings.json")
    digests = {command.name: command_digest(command) for command in pair}
    local_checks = {command.name: toolchain(command, root / command.cwd) for command in pair
                    if not test_commands.unmet_requirements(command.requires, root)}
    local_checks = {name: version for name, version in local_checks.items() if version is not None}
    remote_times = _checks_timing(timings, helper_name, digests)
    if len(local_checks) == len(pair) and not any(timing.estimate() is not None for timing in remote_times.values()):
        return None
    try:
        snapshot = verification_candidate.worktree_candidate(root, job_owned=job_owned)
        repository = repository_identity(root)
        if not repository or not is_current():
            return None
        with verification_candidate.publish(root, "origin", snapshot) as ref:
            request = ProbeRequest(repository, ref, snapshot, digests)
            probe = ssh_probe(helper, allowed, request)
            if helper_blockers(helper, allowed, probe, repository=repository,
                               minimum_disk_bytes=MINIMUM_DISK_BYTES, expected_toolchains={}):
                return None
            local_times = _checks_timing(timings, owner_name, digests)
            remote_row = timings.get(helper_name)
            remote_setup = _timing(remote_row.get("setup") if isinstance(remote_row, dict) else None)
            owner = MachineOffer(owner_name, {name: local_times[name] for name in local_checks}, CheckTiming((0, 0, 0)))
            offer = MachineOffer(helper_name, {name: remote_times[name] for name, version in probe.toolchains.items()
                                               if name in digests and local_checks.get(name, version) == version}, remote_setup)
            placement = choose_placement(tuple(digests), owner, offer)
            if not placement.assignments or helper_name not in placement.assignments:
                return None
            print(f"      - Verification placement: {', '.join(placement.assignments)} ({placement.reason})")
            invocation, generation = uuid4().hex, uuid4().hex

            def remote(command: test_commands.ExtraTestCommand) -> dict | None:
                if not is_current():
                    return None
                digest = digests[command.name]
                key = AttemptKey(invocation, command.name, snapshot.commit, generation, uuid4().hex, repository, digest)
                one = ProbeRequest(repository, ref, snapshot, {command.name: digest})
                reply = run_remote_check(
                    out_dir / "verification" / invocation / f"{command.name}.json",
                    helper, allowed, key=key, request=one, expected_toolchain=probe.toolchains[command.name],
                    timeout=command.timeout * (command.retries + 1) + 2 * LEASE_SECONDS, is_current=is_current,
                )
                if reply.status == "finished" and reply.result and reply.result.get("status") != "blocked":
                    return {**reply.result, "command": command.command, "machine": helper_name}
                print(f"      - Verification fallback for {command.name}: {reply.status}")
                return None

            assignments = list(zip(pair, placement.assignments, strict=True))

            def run_on(name: str) -> dict:
                return {command.name: (remote(command) if name == helper_name else test_commands.run_one(command, root))
                        for command, machine in assignments if machine == name}

            with ThreadPoolExecutor(max_workers=2) as pool:
                remote_work = pool.submit(run_on, helper_name)
                results = {**run_on(owner_name), **remote_work.result()}
            if verification_candidate.worktree_candidate(root, job_owned=job_owned).tree != snapshot.tree:
                print("      - Verification candidate changed during checks. Running the pair locally.")
                return None
            return {name: result for name, result in results.items() if result is not None}, snapshot.tree
    except (OSError, verification_candidate.CandidateError) as exc:
        print(f"      - Verification candidate unavailable: {exc}")
        return None


def run(
    commands: list[test_commands.ExtraTestCommand], root: Path, out_dir: Path, runtime: Path, job: dict,
    *, job_owned: bool = False, is_current: Callable[[], bool] = lambda: True,
) -> list[dict]:
    """Run the pair at its original position and require evidence for both checks."""
    pair = [command for command in commands if command.parallel_verify]
    results: dict[str, dict] | None = None
    candidate_tree: str | None = None

    def run_command(command: test_commands.ExtraTestCommand, root: Path) -> dict:
        nonlocal results, candidate_tree
        if command.parallel_verify:
            if results is None:
                assisted = _pair(pair, root, out_dir, runtime, job, job_owned=job_owned, is_current=is_current)
                results, candidate_tree = assisted if assisted is not None else ({}, None)
            if not is_current():
                return {"name": command.name, "command": command.command, "tier": command.tier, "status": "blocked",
                        "reason": "Job ownership changed during verification.", "output": "", "required": True,
                        "seconds": 0, "attempts": 0, "flaky": False}
            result = results.get(command.name)
            if result is None:
                result = test_commands.run_one(command, root)
            return {**result, "required": True}
        return test_commands.run_one(command, root)

    def finalize(results: list[dict]) -> None:
        reason = ""
        if not is_current():
            reason = "Job ownership changed during verification."
        elif candidate_tree and any(result.get("machine") for result in results):
            try:
                unchanged = verification_candidate.worktree_candidate(root, job_owned=job_owned).tree == candidate_tree
            except verification_candidate.CandidateError:
                unchanged = False
            if not unchanged:
                reason = "Job source changed after remote verification. Run verification again."
        if reason:
            for result in results:
                if result.get("required"):
                    result.update(status="blocked", reason=reason, output="")
                    (out_dir / result["log"]).write_text(reason + "\n", encoding="utf-8")

    return test_commands.run_all(commands, root, out_dir, run_command=run_command, finalize=finalize)
