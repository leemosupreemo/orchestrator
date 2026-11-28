"""Persistent worker reservations for leased verification attempts."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LEASE_SECONDS = 60
TERMINAL_CHECK_STATUSES = frozenset({"passed", "failed", "timeout", "blocked"})


@dataclass(frozen=True)
class AttemptKey:
    invocation_id: str
    check: str
    candidate: str
    owner_generation: str
    attempt_id: str
    repository: str = ""
    command_digest: str = ""


@dataclass(frozen=True)
class AttemptStatus:
    status: str
    lease_until: float | None = None
    result: dict[str, Any] | None = None


class WorkerJournal:
    """One journal per worker account, shared by all participating projects."""

    def __init__(self, directory: Path, *, clock: Callable[[], float] = time.time):
        self.directory = directory
        self.clock = clock

    @contextmanager
    def _transaction(self) -> Iterator[tuple[dict[str, Any], float]]:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.directory / "worker.json"
        with (self.directory / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            original = path.read_text(encoding="utf-8") if path.exists() else ""
            state = json.loads(original) if original else {"active": None, "attempts": {}}
            now = self.clock()
            if (active := state["active"]) is not None:
                entry = state["attempts"][active]
                if now >= entry["lease_until"]:
                    entry.update(status="expired", lease_until=None)
                    state["active"] = None
            yield state, now
            updated = json.dumps(state, separators=(",", ":"), allow_nan=False)
            if updated != original:
                fd, temporary = tempfile.mkstemp(prefix="worker-", dir=self.directory)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        stream.write(updated)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _current(state: dict[str, Any], key: AttemptKey) -> dict[str, Any] | None:
        if state["active"] == key.attempt_id:
            entry = state["attempts"][key.attempt_id]
            if entry["key"] == asdict(key):
                return entry
        return None

    @staticmethod
    def _status(entry: dict[str, Any], key: AttemptKey) -> AttemptStatus:
        if entry["key"] != asdict(key):
            return AttemptStatus("conflict")
        return AttemptStatus(entry["status"], entry["lease_until"], entry.get("result"))

    def status(self, key: AttemptKey) -> AttemptStatus:
        with self._transaction() as (state, _):
            entry = state["attempts"].get(key.attempt_id)
            return self._status(entry, key) if entry is not None else AttemptStatus("unknown")

    def reserve(self, key: AttemptKey) -> AttemptStatus:
        with self._transaction() as (state, now):
            entry = state["attempts"].get(key.attempt_id)
            if entry is not None:
                return self._status(entry, key)
            if state["active"] is not None:
                return AttemptStatus("busy", state["attempts"][state["active"]]["lease_until"])
            lease_until = now + LEASE_SECONDS
            state["active"] = key.attempt_id
            state["attempts"][key.attempt_id] = {"key": asdict(key), "status": "active", "lease_until": lease_until}
            return AttemptStatus("reserved", lease_until)

    def renew(self, key: AttemptKey) -> bool:
        with self._transaction() as (state, now):
            entry = self._current(state, key)
            if entry is None:
                return False
            entry["lease_until"] = now + LEASE_SECONDS
            return True

    @contextmanager
    def execution_slot(self, key: AttemptKey) -> Iterator[int | None]:
        """Claim execution once and hold the machine slot until the runner exits.

        Pass the returned descriptor to the child process so an unexpectedly
        terminated supervisor cannot release the slot while the child runs.
        """
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (self.directory / "execution.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield None
                return
            claimed = False
            with self._transaction() as (state, _):
                entry = self._current(state, key)
                if entry is not None and not entry.get("started"):
                    entry["started"] = True
                    claimed = True
            yield lock.fileno() if claimed else None

    def _end(self, key: AttemptKey, status: str, result: dict[str, Any] | None = None) -> bool:
        with self._transaction() as (state, _):
            entry = self._current(state, key)
            if entry is None:
                return False
            entry.update(status=status, lease_until=None, result=result)
            state["active"] = None
            return True

    def finish(self, key: AttemptKey, result: dict[str, Any]) -> bool:
        if result.get("status") not in TERMINAL_CHECK_STATUSES:
            raise ValueError("Verification result must have a terminal check status.")
        return self._end(key, "finished", result)

    def cancel(self, key: AttemptKey) -> bool:
        return self._end(key, "cancelled")

    def unavailable(self, key: AttemptKey, reason: str) -> bool:
        return self._end(key, "unavailable", {"reason": reason})
