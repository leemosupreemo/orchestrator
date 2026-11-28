"""Versioned preflight request and SSH transport for verification helpers."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from collections.abc import Collection, Iterator, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from orchestrator.code_host import origin_url, parse_remote
from orchestrator.verification_attempts import AttemptKey, AttemptStatus
from orchestrator.verification_candidate import Candidate
from orchestrator.verification_placement import PROTOCOL_VERSION, WorkerProbe, helper_policy_blocker

PROBE_TIMEOUT = 160
MAX_MESSAGE_BYTES = 16_384


def repository_identity(root: Path) -> str:
    origin = origin_url(root)
    if not origin:
        return ""
    parsed = parse_remote(origin)
    if parsed:
        host, path = parsed
        identity = f"{host}/{path.removesuffix('.git')}"
    else:
        identity = origin
    return hashlib.sha256(identity.encode()).hexdigest()


@dataclass(frozen=True)
class ProbeRequest:
    repository: str
    ref: str
    candidate: Candidate
    checks: Mapping[str, str]

    def to_wire(self) -> dict:
        return {"protocol": PROTOCOL_VERSION, "operation": "probe", **asdict(self)}


def ssh_probe(
    machine: Mapping[str, object], allowed_machines: Collection[str] | None, request: ProbeRequest,
) -> WorkerProbe | None:
    """Ask an explicitly opted-in machine using its configured SSH endpoints."""
    for payload in _responses(machine, allowed_machines, request.to_wire(), timeout=PROBE_TIMEOUT):
        try:
            report = WorkerProbe(**payload)
        except TypeError:
            continue
        if (type(report.protocol) is int and isinstance(report.repository, str)
                and type(report.disk_free_bytes) is int and report.disk_free_bytes >= 0
                and isinstance(report.toolchains, dict)
                and all(isinstance(value, str) for value in report.toolchains.values())
                and type(report.candidate_available) is bool):
            return report
    return None


def ssh_attempt(
    machine: Mapping[str, object], allowed_machines: Collection[str] | None, key: AttemptKey,
    operation: str, *, request: ProbeRequest | None = None,
) -> AttemptStatus | None:
    """Send one lifecycle request. A missing reply leaves the attempt indeterminate."""
    if operation not in ("start", "status", "result", "renew", "cancel") or (operation == "start") != (request is not None):
        raise ValueError("Invalid verification attempt request.")
    if request is not None and (request.repository != key.repository or request.candidate.commit != key.candidate
                                or request.checks != {key.check: key.command_digest}):
        raise ValueError("Verification start does not match the attempt.")
    wire = request.to_wire() if request else {"protocol": PROTOCOL_VERSION, "repository": key.repository}
    wire.update(operation=operation, attempt=asdict(key))
    for payload in _responses(machine, allowed_machines, wire, timeout=15):
        try:
            status = AttemptStatus(**payload)
        except TypeError:
            continue
        if (status.status in ("reserved", "active", "busy", "finished", "expired", "cancelled",
                              "unavailable", "unknown", "conflict")
                and (status.lease_until is None or type(status.lease_until) in (int, float))
                and (status.result is None or isinstance(status.result, dict))):
            return status
    return None


def _responses(
    machine: Mapping[str, object], allowed_machines: Collection[str] | None, wire: dict, *, timeout: int,
) -> Iterator[dict]:
    if helper_policy_blocker(machine, allowed_machines):
        return
    targets = machine.get("ssh_target")
    if isinstance(targets, str):
        targets = [targets]
    root = machine.get("repo_path")
    if not isinstance(targets, list) or not isinstance(root, str) or not Path(root).is_absolute():
        return
    remote_command = shlex.join(["python3", "-m", "orchestrator.scripts.verification_worker", root])
    for target in targets:
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_.@:\[\]-]+", target) or target.startswith("-"):
            continue
        try:
            process = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", target, remote_command],
                input=json.dumps(wire), capture_output=True, text=True, timeout=timeout, check=True,
            )
            if len(process.stdout) > MAX_MESSAGE_BYTES:
                continue
            payload = json.loads(process.stdout)
            if isinstance(payload, dict):
                yield payload
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            continue
