"""Per-user instance ownership and atomic admission of work during shutdown."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


class BusyError(RuntimeError):
    pass


class InstanceLease:
    def __init__(self, fd: int):
        self._fd: int | None = fd

    @classmethod
    def acquire(cls, path: Path, kind: str = "desktop") -> InstanceLease:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise OSError("Unsafe instance lock")
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BusyError("Orchestrator is already running for this user.") from None
            data = json.dumps({"pid": os.getpid(), "kind": kind}).encode()
            os.ftruncate(fd, 0)
            os.write(fd, data)
            return cls(fd)
        except BaseException:
            os.close(fd)
            raise

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class ActivityGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self._reason = ""

    def hold(self) -> Callable[[], None]:
        """Admit work and return an idempotent release callback for async owners."""
        with self._lock:
            if self._reason:
                raise BusyError("Orchestrator is preparing to stop or update. Try again shortly.")
            self._active += 1
        released = False

        def release():
            nonlocal released
            with self._lock:
                if not released:
                    released = True
                    self._active -= 1
        return release

    @contextmanager
    def admit(self) -> Iterator[None]:
        release = self.hold()
        try:
            yield
        finally:
            release()

    def prepare(self, reason: str) -> bool:
        if not reason:
            raise ValueError("A preparation reason is required")
        with self._lock:
            if self._active or (self._reason and not (self._reason == "update" and reason == "stop")):
                return False
            self._reason = reason
            return True

    def cancel(self) -> None:
        with self._lock:
            self._reason = ""

    def snapshot(self) -> dict:
        with self._lock:
            return {"active": self._active, "preparing": self._reason}
