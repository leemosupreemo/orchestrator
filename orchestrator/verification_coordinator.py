"""Reconcile one remote check against a durable owner-side attempt record."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import asdict
from pathlib import Path

from orchestrator.verification_attempts import LEASE_SECONDS, TERMINAL_CHECK_STATUSES, AttemptKey, AttemptStatus
from orchestrator.verification_transport import ProbeRequest, ssh_attempt

HEARTBEAT_SECONDS = 10


def _save(path: Path, state: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix="verification-owner-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _unavailable(reason: str) -> AttemptStatus:
    return AttemptStatus("unavailable", result={"reason": reason})


def _validate_result(key: AttemptKey, reply: AttemptStatus, expected_toolchain: str) -> AttemptStatus:
    result = reply.result or {}
    status = result.get("status")
    if (result.get("name") != key.check or not isinstance(status, str) or status not in TERMINAL_CHECK_STATUSES
            or not isinstance(result.get("output"), str)):
        return _unavailable("The helper did not return valid check evidence.")
    if result.get("toolchain") != expected_toolchain:
        return _unavailable("The helper toolchain changed after preflight.")
    return reply


def run_remote_check(
    path: Path, machine: Mapping[str, object], allowed_machines: Collection[str] | None,
    *, key: AttemptKey, request: ProbeRequest, expected_toolchain: str, timeout: float,
    is_current: Callable[[], bool] = lambda: True, rpc: Callable = ssh_attempt,
    clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
) -> AttemptStatus:
    """Run or recover the same attempt. A new attempt must use a new journal path."""
    if (request.repository != key.repository or request.candidate.commit != key.candidate
            or request.checks != {key.check: key.command_digest}):
        raise ValueError("Verification request does not match the owner attempt.")
    if timeout <= 0:
        raise ValueError("Verification timeout must be positive.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    binding = {"key": asdict(key), "request": request.to_wire(), "machine": machine.get("name"),
               "toolchain": expected_toolchain}
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        recovering = path.exists()
        if recovering:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state["binding"] != binding:
                return AttemptStatus("conflict")
            operation = "status"
        else:
            state = {"binding": binding, "deadline": clock() + timeout,
                     "contact_deadline": clock() + LEASE_SECONDS, "starts": 0}
            _save(path, state)
            operation = "start"

        def finish(reply: AttemptStatus) -> AttemptStatus:
            state["terminal"] = asdict(reply)
            _save(path, state)
            return reply

        if not is_current():
            rpc(machine, allowed_machines, key, "cancel")
            return finish(AttemptStatus("cancelled"))
        if "terminal" in state:
            return AttemptStatus(**state["terminal"])

        while recovering or clock() < min(state["deadline"], state["contact_deadline"]):
            recovering = False
            if not is_current():
                rpc(machine, allowed_machines, key, "cancel")
                return finish(AttemptStatus("cancelled"))
            if operation == "start":
                state["starts"] += 1
            if operation in ("start", "renew"):
                # A lost response may still have started or extended a lease.
                # Persist the full possible lease window before sending.
                state["contact_deadline"] = min(state["deadline"], clock() + LEASE_SECONDS)
                _save(path, state)
            reply = rpc(machine, allowed_machines, key, operation, **({"request": request} if operation == "start" else {}))
            if not is_current():
                rpc(machine, allowed_machines, key, "cancel")
                return finish(AttemptStatus("cancelled"))
            if reply is None:
                if operation == "start":
                    operation = "status"
                    continue
                operation = "status"
            elif reply.status == "finished":
                return finish(_validate_result(key, reply, expected_toolchain))
            elif reply.status in ("busy", "expired", "cancelled", "unavailable", "conflict"):
                return finish(reply)
            elif reply.status in ("reserved", "active"):
                operation = "renew"
            elif reply.status == "unknown" and state["starts"] < 2:
                operation = "start"
                continue
            else:
                return finish(_unavailable("The helper lost the verification attempt."))
            remaining = min(state["deadline"], state["contact_deadline"]) - clock()
            if remaining > 0:
                sleep(min(HEARTBEAT_SECONDS, remaining))

        rpc(machine, allowed_machines, key, "cancel")
        return finish(_unavailable("The helper did not return evidence before the attempt deadline."))
