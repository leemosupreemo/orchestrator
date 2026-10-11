"""Undo the most recent finished task of a job.

The worker commits each task as it finishes and records the commit in `task_commits`. Undoing is a `git revert` of
that commit, so history stays honest and nothing is lost. Only the latest finished task can be undone (undoing an
earlier one would pull the rug from the tasks built on it), only on the job's own branch, and only with a clean tree.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


class RevertError(ValueError):
    pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def latest_revertable(job: dict[str, Any]) -> int | None:
    """Index of the last finished task that has a recorded commit, or None."""
    commits = job.get("task_commits") or {}
    done = [i for i in (job.get("completed_task_indices") or []) if isinstance(i, int) and str(i) in commits]
    return max(done) if done else None


def revert_latest(root: Path, job: dict[str, Any], index: int) -> str:
    """Reverts task `index` (must be the latest). Updates `job` in place; returns the new commit id."""
    if latest_revertable(job) != index:
        raise RevertError("Only the most recently finished task can be undone.")
    sha = job["task_commits"][str(index)]
    branch = job.get("branch")
    current = _git(root, "branch", "--show-current").stdout.strip()
    if branch and current != branch:
        raise RevertError(f"Switch to {branch} first; the task's changes are on that branch.")
    if _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise RevertError("There are uncommitted changes. Commit or set them aside first.")
    if _git(root, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        raise RevertError("That task's commit is no longer in this repository.")
    res = _git(root, "revert", "--no-edit", sha)
    if res.returncode != 0:
        _git(root, "revert", "--abort")
        raise RevertError("Undoing it conflicts with later changes, so nothing was changed. " + (res.stderr or res.stdout).strip().splitlines()[-1][:160])
    job["completed_task_indices"] = [i for i in job.get("completed_task_indices", []) if i != index]
    del job["task_commits"][str(index)]
    job["reverted_tasks"] = [*job.get("reverted_tasks", []), index]
    if job.get("status") in {"completed", "review-needed", "debugging"}:
        job["status"] = "review-needed"
    return _git(root, "rev-parse", "HEAD").stdout.strip()
