"""Jobs at once: more than one job building in a project at the same time, each in its own git worktree.

Every job used to share the project folder, so a second job waited for the first (folder_lock). With `parallel_jobs` above 1
in project.json, a job that would wait runs in its own worktree instead: a separate checkout of its branch that shares the
repository, kept outside the project (`~/.orchestrator/worktrees/<project>-<hash>/<job>`) so test discovery, Xcode and Unity
never see the copies. The job's records stay in the project's `.orchestrator` folder.

Guards, so jobs at once never step on each other:
- A job on the current branch ("Use the current branch") or with branch switching off still waits: git can't check out one
  branch in two places, and that work belongs in the project folder.
- A job whose planned files overlap a job that is running waits for it (job_overlap.planned_files), rather than build
  conflicting changes side by side.
- A job keeps its worktree until it's merged, discarded or deleted, so a resume or a fix continues where its work is.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any

from orchestrator import job_overlap

MAX_AT_ONCE = 4
ENV_MAIN = "ORCHESTRATOR_WORKTREE_OF"  # set in a worker running in a worktree: the project folder it belongs to
SHARED_FOLDER_MODES = ("current", "manual")


def limit(project: dict[str, Any]) -> int:
    try:
        return max(1, min(int(project.get("parallel_jobs") or 1), MAX_AT_ONCE))
    except (TypeError, ValueError):
        return 1


def worktrees_home(main_root: Path) -> Path:
    from orchestrator.project_config import user_state_dir
    main_root = Path(main_root).resolve()
    tag = hashlib.sha256(str(main_root).encode()).hexdigest()[:8]
    return user_state_dir() / "worktrees" / f"{main_root.name}-{tag}"


def worktree_for(main_root: Path, job_id: str) -> Path:
    return worktrees_home(main_root) / job_id


def _alive(job: dict[str, Any]) -> bool:
    pid = job.get("worker_pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_in_worktrees(jobs: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    return [(i, j) for i, j in jobs if j.get("worktree") and _alive(j) and not j.get("waiting_for")]


def decide(main_root: Path, job_id: str, job: dict[str, Any], jobs: list[tuple[str, dict[str, Any]]],
           holder: dict[str, Any] | None, project: dict[str, Any]) -> tuple[bool, str]:
    """(run in its own worktree?, why not, in words). `holder` is who has the project folder now (folder_lock.holder)."""
    existing = job.get("worktree")
    if existing and Path(existing).is_dir():
        return True, ""  # its work is there: always carry on there
    if limit(project) <= 1:
        return False, ""
    if not holder or holder.get("job") in ("", job_id):
        return False, ""  # the folder is free: work there, as before
    mode = str(job.get("branch_mode") or "new")
    if mode in SHARED_FOLDER_MODES:
        return False, "It works on the project folder's current branch, so it waits its turn there."
    if isinstance(job.get("passed_uncommitted"), dict):
        return False, "Its finished work is uncommitted in the project folder, so it's committed there."
    by_id = dict(jobs)
    running = [(holder.get("job"), by_id.get(holder.get("job"), {}))] + [(i, j) for i, j in running_in_worktrees(jobs) if i != job_id]
    if len(running) >= limit(project):
        return False, f"{limit(project)} jobs are already running in this project (Jobs at once)."
    mine = job_overlap.planned_files(job)
    for other_id, other in running:
        shared = sorted(mine & job_overlap.planned_files(other))
        if shared:
            return False, f"It changes the same files as “{other.get('title') or other_id}” ({', '.join(shared[:2])}), so it waits for that job."
    return True, ""


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check, timeout=600,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def create(main_root: Path, job_id: str) -> Path:
    """The job's worktree, made now (detached; the worker checks out or creates the job's branch in it) or reused."""
    path = worktree_for(main_root, job_id)
    if (path / ".git").exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(main_root, "worktree", "prune", check=False)
    _git(main_root, "worktree", "add", "--detach", str(path), "HEAD")
    return path


def remove(main_root: Path, job: dict[str, Any], force: bool = False) -> bool:
    """Removes a job's worktree once the job is merged, completed, discarded or deleted. The branch and its commits stay.
    Without `force` a worktree with uncommitted changes is kept (and says so), so finishing a job never deletes work."""
    path = job.get("worktree")
    if not path or not Path(path).exists():
        return False
    result = _git(main_root, "worktree", "remove", *(["--force"] if force else []), str(path), check=False)
    if result.returncode != 0:
        print(f"Kept this job's own checkout ({path}): it has uncommitted changes. Remove it with `git worktree remove` once they're saved.")
    _git(main_root, "worktree", "prune", check=False)
    return result.returncode == 0


def worker_env(main_root: Path, worktree: Path, runtime_dir: Path) -> dict[str, str]:
    """The environment a worker runs with in a worktree: the worktree is the project folder, the records stay in the project."""
    return {**os.environ, "ORCHESTRATOR_PROJECT_ROOT": str(worktree), "ORCHESTRATOR_RUNTIME_DIR": str(runtime_dir),
            "ORCHESTRATOR_CONFIG": str(Path(runtime_dir) / "project.json"), ENV_MAIN: str(Path(main_root).resolve())}
