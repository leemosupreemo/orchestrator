"""Run an approved helper check with bounded output and a cancellable lease."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from orchestrator.verification_attempts import AttemptKey, WorkerJournal

OUTPUT_BYTES = 2048


class LeaseLost(RuntimeError):
    """The owner no longer holds the attempt lease."""


def execute(
    command: str, cwd: Path, timeout: int, *, journal: WorkerJournal, key: AttemptKey, slot: int,
) -> tuple[int, str, bool]:
    if journal.status(key).status != "active":
        raise LeaseLost
    tail = bytearray()
    deadline = time.monotonic() + timeout
    next_lease_check = time.monotonic()
    timed_out = False
    with subprocess.Popen(
        ["/bin/bash", "-c", command], cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(slot,),
    ) as process, selectors.DefaultSelector() as ready:
        ready.register(process.stdout, selectors.EVENT_READ)
        try:
            while ready.get_map() or process.poll() is None:
                now = time.monotonic()
                if now >= next_lease_check:
                    if journal.status(key).status != "active":
                        raise LeaseLost
                    next_lease_check = now + 0.5
                if now >= deadline:
                    timed_out = True
                    break
                for event, _ in ready.select(min(0.5, deadline - now)):
                    chunk = os.read(event.fd, 8192)
                    if not chunk:
                        ready.unregister(event.fileobj)
                    else:
                        tail.extend(chunk)
                        del tail[:-OUTPUT_BYTES]
        finally:
            if process.poll() is None or timed_out or ready.get_map():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
    output = tail.decode("utf-8", errors="replace")
    return (124 if timed_out else process.returncode), output, timed_out
