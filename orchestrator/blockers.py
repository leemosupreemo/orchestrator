"""Blockers: what stops a job that no code change can fix (a git lock, an expired sign-in, a missing tool, a full disk).

A failed build used to go to the AI fix loop whatever the cause, and every job hit by the same machine problem failed on its
own. Now the worker records a blocker on the job (`job["blocker"]`) and pauses it instead of starting fix attempts; Home shows
each blocker once with the jobs it holds up; and once it is cleared (checked here, not assumed) those jobs resume by
themselves, at most once an hour each, so a blocker that comes straight back can't loop.

    {"kind": "git_lock", "scope": "project" | "machine", "key": "git", "title": "...", "detail": "...", "detected_at": 1.0}
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from orchestrator import disk_space, job_problem

AUTO_RESUME_EVERY = 3600  # seconds: one automatic resume per job per hour, per blocker
LOCK_STALE_AFTER = 10

_GIT_LOCK = re.compile(r"index\.lock'?: File exists|Another git process seems to be running|holding this project's git lock", re.I)
_SIGN_IN = re.compile(r"oauth access token has expired|failed to authenticate|re-?authenticate to continue|not logged in|"
                      r"invalid api key|missing api key|please run /login", re.I)
_TOOL = re.compile(r"(?:^|[\s:'\"`])([\w.+-]{2,40}): command not found", re.M)
_READ_ONLY = re.compile(r"Read-only file system", re.I)


def detect(text: str) -> dict[str, Any] | None:
    """The blocker named in an error or a build log, or None when it reads like a problem in the code."""
    text = str(text or "")
    if not text.strip():
        return None
    if disk_space.is_disk_error(text):
        return _make("disk_full", "machine", "disk", "Low disk space", "Free up space on the computer running this job.")
    if _GIT_LOCK.search(text):
        return _make("git_lock", "project", "git", "Git is locked in this project",
                     "A git command that didn't finish left .git/index.lock behind, so every git step fails.")
    if _SIGN_IN.search(text):
        tool = job_problem._tool(text) or ("ai", "the AI tool")
        return _make("sign_in", "machine", tool[0], f"{tool[1]} sign-in expired", f"Jobs can't use {tool[1]} until you sign in again.",
                     provider=tool[0], name=tool[1])
    if match := _TOOL.search(text):
        name = match.group(1)
        return _make("tool", "machine", name, f"{name} isn't installed", f"The build needs {name}, which this Mac can't find.")
    if _READ_ONLY.search(text):
        return _make("write_access", "project", "folder", "The project folder is read-only", "Files in the project can't be written.")
    try:
        from orchestrator.env_issues import detect_simulator_environment_issue
        if issue := detect_simulator_environment_issue(text):
            return _make("simulator", "machine", "xcode", "Xcode or the simulator needs attention", issue)
    except Exception:
        pass
    return None


def _make(kind: str, scope: str, key: str, title: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "scope": scope, "key": key, "title": title, "detail": detail, **extra}


FINISHED = ("completed", "archived")


def claimed_files(job: dict[str, Any]) -> set[str]:
    """Files a job has changed or is about to commit (its own work, committed or not)."""
    files = set(job.get("ai_modified_files") or []) | set(job.get("ai_untracked_files") or [])
    pending = job.get("passed_uncommitted")
    if isinstance(pending, dict):
        files |= set(pending.get("files") or [])
    return {f for f in files if isinstance(f, str)}


def dirty_files(root: Path) -> set[str]:
    try:
        out = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=str(root), capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {line[3:].strip().strip('"') for line in out.splitlines() if len(line) > 3}


def other_jobs_work(root: Path, job_id: str, jobs: list[tuple[str, dict[str, Any]]], branch: str,
                    dirty: set[str] | None = None) -> dict[str, Any] | None:
    """Another open job's uncommitted work in this folder (files it changed, still uncommitted, on the branch the folder is on).
    A job that started here anyway would commit that work as its own, or a checkout would throw it away."""
    dirty = dirty_files(root) if dirty is None else dirty
    if not dirty:
        return None
    for other_id, other in jobs:
        related = other_id == job_id or other_id.startswith(f"{job_id}_task") or job_id.startswith(f"{other_id}_task")  # its own task copies
        if related or other.get("status") in FINISHED or other.get("moved_to"):
            continue
        if str(other.get("branch") or "") != branch:
            continue
        mine = sorted(dirty & claimed_files(other))
        if mine:
            title = str(other.get("title") or other_id)
            return _make("job_work", "project", other_id, f"“{title}” has uncommitted work here",
                         f"{len(mine)} of its changed files aren't committed yet ({', '.join(mine[:3])}{', …' if len(mine) > 3 else ''}). "
                         "Starting now would take that work into this job, or throw it away.",
                         job=other_id, job_title=title, files=mine[:50])
    return None


def record(job: dict[str, Any], blocker: dict[str, Any], now: float | None = None) -> None:
    """Puts `blocker` on the job, keeping when it was last resumed automatically if it's the same blocker again."""
    before = next((b for b in (job.get("blocker"), job.get("last_blocker")) if isinstance(b, dict)), {})
    same = (before.get("kind"), before.get("key")) == (blocker.get("kind"), blocker.get("key"))
    job["blocker"] = {**blocker, "detected_at": time.time() if now is None else now,
                      **({"auto_resumed_at": before["auto_resumed_at"]} if same and before.get("auto_resumed_at") else {})}


def git_lock_path(root: Path) -> Path | None:
    dot_git = Path(root) / ".git"
    if dot_git.is_dir():
        return dot_git / "index.lock"
    if dot_git.is_file():  # a worktree: ".git" points at its own git folder
        text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            git_dir = Path(text.split(":", 1)[1].strip())
            return (git_dir if git_dir.is_absolute() else Path(root) / git_dir) / "index.lock"
    return None


def lock_in_use(lock: Path) -> bool:
    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if Path("/usr/sbin/lsof").exists() else None)
    if not lsof:
        return True  # can't tell: leave it alone
    try:
        probe = subprocess.run([lsof, str(lock)], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return True
    return probe.returncode != 1 or bool(probe.stdout.strip())  # lsof exits 1 with no output when nothing has it open


def clear_git_lock(root: Path, now: float | None = None) -> bool:
    """Removes a leftover git lock: only when it is a few seconds old and no process has it open. True when none is left."""
    lock = git_lock_path(root)
    if not lock or not lock.exists():
        return True
    try:
        if (time.time() if now is None else now) - lock.stat().st_mtime < LOCK_STALE_AFTER or lock_in_use(lock):
            return False
        lock.unlink()
    except OSError:
        return False
    return True


def is_cleared(blocker: dict[str, Any], root: Path) -> bool | None:
    """True when the blocker is gone, False when it's still there, None when this can't be checked (Xcode, the simulator)."""
    kind = blocker.get("kind")
    if kind == "git_lock":
        lock = git_lock_path(root)
        return not (lock and lock.exists())
    if kind == "sign_in":
        return job_problem.signed_in_at(str(blocker.get("provider") or "")) > float(blocker.get("detected_at") or 0)
    if kind == "tool":
        return shutil.which(str(blocker.get("key") or "")) is not None
    if kind == "disk_full":
        needed = disk_space.status(root)["needed_bytes"]
        return needed == 0 if needed is not None else None
    if kind == "write_access":
        return os.access(root, os.W_OK)
    if kind == "job_work":  # the other job's files are committed (or put away) now
        return not (dirty_files(root) & set(blocker.get("files") or []))
    return None


def steps(blocker: dict[str, Any], root: Path) -> list[str]:
    kind = blocker.get("kind")
    if kind == "git_lock":
        return ["Wait for other git tools (an editor, Codex, a status line) to finish.",
                "If nothing is running git, Clear the lock; waiting jobs then resume."]
    if kind == "sign_in":
        return [f"Sign in to {blocker.get('name') or 'the AI tool'}; waiting jobs then resume."]
    if kind == "tool":
        return [f"Install {blocker.get('key')}; waiting jobs then resume."]
    if kind == "disk_full":
        return ["Open Free up space to check the build's storage target and available cleanup."]
    if kind == "write_access":
        return [f"In Finder, Get Info on {Path(root).name}, and under Sharing & Permissions give your account Read & Write."]
    if kind == "job_work":
        return [f"Open “{blocker.get('job_title') or blocker.get('key')}” and finish committing its work (Commit the work, or Resume).",
                "This job then starts by itself."]
    return ["Fix it as it says, then press Try again."]


def due_for_auto_resume(blocker: dict[str, Any], now: float | None = None) -> bool:
    return (time.time() if now is None else now) - float(blocker.get("auto_resumed_at") or 0) >= AUTO_RESUME_EVERY


def groups(root: Path, jobs: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Paused jobs by the blocker they wait on: one entry per blocker, with whether it's cleared now and its jobs."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for job_id, job in jobs:
        blocker = job.get("blocker")
        if job.get("status") != "human-needed" or not isinstance(blocker, dict) or not blocker.get("kind"):
            continue
        group = out.setdefault((blocker["kind"], str(blocker.get("key") or "")),
                               {**{k: blocker.get(k) for k in ("kind", "scope", "key", "title", "detail", "provider", "name")},
                                "cleared": is_cleared(blocker, root), "steps": steps(blocker, root), "jobs": []})
        group["jobs"].append({"id": job_id, "title": job.get("title") or job_id,
                              "auto_resume": due_for_auto_resume(blocker)})
    return list(out.values())
