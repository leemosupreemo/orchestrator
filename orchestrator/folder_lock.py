"""One job at a time in a project folder.

A job builds on its own branch, but every job on a computer shares the project folder, and the first thing a job's
worker does is check out its branch there (`git checkout -f`). Two jobs at once would switch the branch under each other
and throw away each other's uncommitted work. So a worker takes this lock before it touches Git and keeps it until it
ends, and a second job waits its turn, saying which job it's waiting for.

The lock is an flock on `<runtime>/locks/folder.lock`: the operating system lets go of it when the process ends, however
it ends, so a crashed worker never leaves the folder locked. The file also says who holds it, for the page and for
whoever is waiting. A worker's own children (Run fix starting the worker again) already have the folder: the holder
sets ORCHESTRATOR_FOLDER_LOCK to the folder, and a child that sees it doesn't wait for its own parent.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterator

ENV = "ORCHESTRATOR_FOLDER_LOCK"
POLL_SECONDS = 2.0


def lock_path(root: Path) -> Path:
    return Path(root) / ".orchestrator" / "locks" / "folder.lock"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def holder(root: Path) -> dict[str, Any] | None:
    """Who has the folder now ({job, title, pid, since}), or None when it's free. Doesn't take it."""
    path = lock_path(root)
    if not path.exists():
        return None
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read(path) or {"job": "", "title": "another job"}
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


@contextlib.contextmanager
def hold(root: Path, job: str = "", title: str = "",
         on_wait: Callable[[dict[str, Any]], None] | None = None,
         poll: float = POLL_SECONDS, timeout: float | None = None) -> Iterator[None]:
    """Keep the project folder for one job. Waits while another job has it, calling `on_wait(holder)` once when the
    wait starts and again whenever the job ahead changes. Raises TimeoutError after `timeout` seconds of waiting."""
    root = Path(root).resolve()
    if os.environ.get(ENV) == str(root):  # a child of the job that has the folder
        yield
        return
    path = lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        started, ahead = time.monotonic(), None
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                now = _read(path) or {"job": "", "title": "another job"}
                if on_wait and now.get("job") != (ahead or {}).get("job", object()):
                    on_wait(now)
                ahead = now
                if timeout is not None and time.monotonic() - started > timeout:
                    raise TimeoutError(f"Still waiting for {now.get('title') or 'another job'} to finish.")
                time.sleep(poll)
        os.ftruncate(fd, 0)
        os.pwrite(fd, json.dumps({"job": job, "title": title, "pid": os.getpid(), "since": time.time()}).encode(), 0)
        previous = os.environ.get(ENV)
        os.environ[ENV] = str(root)
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(ENV, None)
            else:
                os.environ[ENV] = previous
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
