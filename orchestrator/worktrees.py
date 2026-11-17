"""Git worktrees: separate checkouts that share the project's repository, so work can happen without touching your folder.

One place for what four features used to do each their own way:
- `scratch`: a throwaway checkout at a commit, always removed afterwards (integration check, web preview link).
- `ensure` / `remove`: a checkout kept between runs at a known path (a job building alongside others, the live preview of
  a branch that isn't checked out).
- `checked_out_in`: where a branch is checked out already, since git keeps a branch in one place at a time.

A worktree here is always detached at first (`--detach`), so it never fails because a branch is checked out elsewhere.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator


class WorktreeError(RuntimeError):
    pass


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=600,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def _add(root: Path, path: Path, ref: str) -> None:
    _git(root, "worktree", "prune")  # a checkout the computer cleared away (temp folders) would block adding it again
    result = _git(root, "worktree", "add", "--detach", str(path), ref)
    if result.returncode != 0:
        raise WorktreeError(f"Couldn't make a separate checkout of {ref}: {(result.stderr or result.stdout).strip()[-300:]}")


@contextlib.contextmanager
def scratch(root: Path, ref: str, prefix: str = "orchestrator-") -> Iterator[Path]:
    """A throwaway checkout of `ref`, removed afterwards however the block ends."""
    folder = Path(tempfile.mkdtemp(prefix=prefix))
    work = folder / "work"
    try:
        _add(root, work, ref)
        yield work
    finally:
        _git(root, "worktree", "remove", "--force", str(work))
        shutil.rmtree(folder, ignore_errors=True)


def ensure(root: Path, path: Path, ref: str = "HEAD") -> Path:
    """A checkout kept at `path`: made at `ref` if it isn't there yet, otherwise left as it is."""
    path = Path(path)
    if (path / ".git").exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    _add(root, path, ref)
    return path


def remove(root: Path, path: Path | str | None, force: bool = False) -> bool:
    """Removes a kept checkout. Without `force`, one with uncommitted changes is left alone (False)."""
    if not path or not Path(path).exists():
        return False
    result = _git(root, "worktree", "remove", *(["--force"] if force else []), str(path))
    _git(root, "worktree", "prune")
    return result.returncode == 0


def checked_out_in(root: Path, branch: str) -> Path | None:
    """The folder where `branch` is checked out, if any (the project folder or one of its worktrees)."""
    path = None
    for line in _git(root, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None
