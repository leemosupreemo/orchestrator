"""SSH entry point for verification checks against a configured local project."""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from functools import partial
from pathlib import Path

from orchestrator import test_commands, verification_candidate
from orchestrator.project_config import load_project_config
from orchestrator.verification import command_digest
from orchestrator.verification_attempts import AttemptKey, AttemptStatus, WorkerJournal
from orchestrator.verification_execution import LeaseLost, execute
from orchestrator.verification_placement import PROTOCOL_VERSION, WorkerProbe
from orchestrator.verification_transport import MAX_MESSAGE_BYTES, repository_identity

VERSION_COMMANDS = {
    "dotnet": ("dotnet", "--version"),
    "python3": ("python3", "--version"),
    "swift": ("swift", "--version"),
    "xcodebuild": ("xcodebuild", "-version"),
}


def toolchain(command: test_commands.ExtraTestCommand, cwd: Path) -> str | None:
    tools = [value for kind, _, value in (requirement.partition(":") for requirement in command.requires)
             if kind == "command"]
    versions = [platform.system(), platform.machine()]
    for tool in sorted(set(tools)):
        argv = VERSION_COMMANDS.get(tool)
        if argv is None:
            return None
        try:
            result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        version = (result.stdout or result.stderr).strip()
        if not version or len(version) > 512:
            return None
        versions.append(f"{tool}={version}")
    return " | ".join(versions)


def approved_checks(root: Path, request: dict) -> tuple[str, verification_candidate.Candidate, list[test_commands.ExtraTestCommand]]:
    checks = request.get("checks")
    ref = request.get("ref")
    candidate = request.get("candidate")
    if (not isinstance(checks, dict) or not 1 <= len(checks) <= 2
            or not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in checks.values())
            or not isinstance(ref, str) or not isinstance(candidate, dict)
            or set(candidate) != {"commit", "tree"}
            or not all(isinstance(value, str) for value in candidate.values())):
        raise ValueError("Malformed verification probe.")
    config = load_project_config(root)
    commands, errors = test_commands.from_config(config)
    approved = [] if errors else [command for command in commands if command.parallel_verify
                                  and checks.get(command.name) == command_digest(command)]
    return ref, verification_candidate.Candidate(**candidate), approved


def matches_project(request: dict, repository: str) -> bool:
    return (type(request.get("protocol")) is int and request["protocol"] == PROTOCOL_VERSION
            and bool(repository) and request.get("repository") == repository)


def probe(root: Path, request: dict) -> WorkerProbe:
    repository = repository_identity(root)
    free_bytes = min(shutil.disk_usage(root).free, shutil.disk_usage(tempfile.gettempdir()).free)
    unavailable = WorkerProbe(PROTOCOL_VERSION, repository, free_bytes, {}, False)
    if not matches_project(request, repository):
        return unavailable
    ref, snapshot, approved = approved_checks(root, request)
    if not approved:
        return unavailable
    tools: dict[str, str] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="orchestrator-verification-probe-") as directory:
            with verification_candidate.checkout(root, "origin", ref, snapshot, Path(directory) / "checkout") as worktree:
                for command in approved:
                    cwd = (worktree / command.cwd).resolve()
                    if (not cwd.is_dir() or not cwd.is_relative_to(worktree.resolve())
                            or test_commands.unmet_requirements(command.requires, worktree)):
                        continue
                    version = toolchain(command, cwd)
                    if version is not None:
                        tools[command.name] = version
    except (OSError, verification_candidate.CandidateError):
        return unavailable
    return WorkerProbe(PROTOCOL_VERSION, repository, free_bytes, tools, True)


def attempt_key(request: dict, repository: str) -> AttemptKey:
    if not matches_project(request, repository) or not isinstance(request.get("attempt"), dict):
        raise ValueError("Verification attempt does not match this project.")
    key = AttemptKey(**request["attempt"])
    if (key.repository != repository
            or not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value)
                       for value in (key.invocation_id, key.owner_generation, key.attempt_id))
            or not isinstance(key.candidate, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", key.candidate)
            or not isinstance(key.command_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", key.command_digest)
            or not isinstance(key.check, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", key.check)):
        raise ValueError("Malformed verification attempt identity.")
    return key


def run_attempt(
    root: Path, ref: str, snapshot: verification_candidate.Candidate, command: test_commands.ExtraTestCommand,
    journal: WorkerJournal, key: AttemptKey,
) -> None:
    with journal.execution_slot(key) as slot:
        if slot is None:
            # Another supervisor may have claimed this attempt. The owner can
            # reconcile the existing reservation or wait for its lease to expire.
            return
        try:
            with tempfile.TemporaryDirectory(prefix="orchestrator-verification-run-") as directory:
                checkout = Path(directory) / "checkout"
                with verification_candidate.checkout(root, "origin", ref, snapshot, checkout):
                    cwd = (checkout / command.cwd).resolve()
                    if not cwd.is_dir() or not cwd.is_relative_to(checkout.resolve()):
                        journal.unavailable(key, "The check directory is unavailable in the candidate.")
                        return
                    version = toolchain(command, cwd)
                    if version is None:
                        journal.unavailable(key, "The required toolchain is unavailable.")
                        return
                    result = test_commands.run_one(
                        command, checkout, run=partial(execute, journal=journal, key=key, slot=slot),
                    )
                    result.pop("command", None)
                    failures = result.get("failing_tests") or []
                    result["failing_tests"] = [str(failure)[:100] for failure in failures[:10]]
                    result["toolchain"] = version
            journal.finish(key, result)
        except LeaseLost:
            return
        except (OSError, verification_candidate.CandidateError):
            journal.unavailable(key, "The helper could not prepare or run the verification check.")


def handle_attempt(root: Path, request: dict, raw: bytes, *, supervise: bool = False) -> AttemptStatus:
    key = attempt_key(request, repository_identity(root))
    directory = Path(os.environ.get("ORCHESTRATOR_VERIFICATION_WORKER_DIR",
                                   str(Path.home() / ".orchestrator" / "verification-worker")))
    journal = WorkerJournal(directory)
    operation = request["operation"]
    if operation == "renew":
        journal.renew(key)
    elif operation == "cancel":
        journal.cancel(key)
    elif operation == "start":
        ref, snapshot, approved = approved_checks(root, request)
        if (len(approved) != 1 or approved[0].name != key.check or snapshot.commit != key.candidate
                or command_digest(approved[0]) != key.command_digest):
            return AttemptStatus("unavailable", result={"reason": "The helper has not approved this check or candidate."})
        if supervise:
            run_attempt(root, ref, snapshot, approved[0], journal, key)
            return journal.status(key)
        reservation = journal.reserve(key)
        if reservation.status == "reserved":
            try:
                with tempfile.TemporaryFile() as message:
                    message.write(raw)
                    message.seek(0)
                    subprocess.Popen(
                        [sys.executable, "-m", "orchestrator.scripts.verification_worker", str(root), "--supervise"],
                        stdin=message, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                    )
            except OSError:
                journal.unavailable(key, "The helper could not start the verification supervisor.")
                return journal.status(key)
        return reservation
    return journal.status(key)


def main() -> int:
    supervise = len(sys.argv) == 3 and sys.argv[2] == "--supervise"
    if len(sys.argv) != 2 and not supervise:
        return 2
    try:
        raw = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
        if len(raw) > MAX_MESSAGE_BYTES:
            return 2
        request = json.loads(raw)
        if not isinstance(request, dict):
            return 2
        root = Path(sys.argv[1]).resolve(strict=True)
        operation = request.get("operation")
        if operation == "probe" and not supervise:
            response = probe(root, request)
        elif operation in ("start", "status", "result", "renew", "cancel") and (not supervise or operation == "start"):
            response = handle_attempt(root, request, raw, supervise=supervise)
        else:
            return 2
    except (OSError, TypeError, ValueError):
        return 2
    print(json.dumps(asdict(response), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
