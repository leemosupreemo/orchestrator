"""Unfinished work, backed up where another computer can reach it.

A job's committed work already goes to its branch on origin (worker_run.backup_push). What a paused or failed job left
uncommitted lives only on this computer's disk, so when this computer is offline nobody else can continue from it. This
saves those files as a commit on a hidden ref, refs/orchestrator/wip/<job id>, built in a scratch index: the working tree,
the real index and the branch are never touched. Another computer that continues the job (Continue here) can bring the
files back with restore().
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

REF_PREFIX = "refs/orchestrator/wip/"
RETRY_SECONDS = 600  # a failed push (offline, no rights) is tried again after this, not on every heartbeat
DONE = {"completed", "archived", "discarded", "done"}
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
TOOL_DIRS = (".orchestrator/", ".claude/", ".claude-flow/", ".worktrees/")  # tools' own state, never the project's work


def _project_changes(repo: Path) -> list[str]:
    """Uncommitted changes that are the project's (not Orchestrator's or an AI tool's own folders)."""
    lines = _git(repo, "status", "--porcelain").stdout.splitlines()
    return [l for l in lines if not l[3:].strip().strip('"').startswith(TOOL_DIRS)]


def _git(repo: Path, *args: str, env: dict[str, str] | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **_ENV, **(env or {})})


def ref_for(job_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", job_id):
        raise ValueError("bad job id")
    return REF_PREFIX + job_id


def dirty_files(repo: Path, files: list[str]) -> list[str]:
    """Which of `files` differ from HEAD here (changed, new, or deleted)."""
    files = [f for f in files if f and not f.startswith("-")]
    if not files:
        return []
    res = _git(repo, "status", "--porcelain", "--untracked-files=all", "--", *files)
    if res.returncode != 0:
        return []
    out = []
    for line in res.stdout.splitlines():
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if path:
            out.append(path)
    return sorted(set(out))


def digest(repo: Path, files: list[str]) -> str:
    h = hashlib.sha256()
    for f in sorted(files):
        p = repo / f
        h.update(f.encode() + b"\0")
        h.update(p.read_bytes() if p.is_file() else b"<deleted>")
    return h.hexdigest()[:24]


def save(repo: Path, job_id: str, files: list[str], message: str = "") -> str:
    """Commit `files` as they are on disk, on top of HEAD, in a scratch index, and push it to the job's hidden ref.
    Returns the commit. Raises RuntimeError with git's words when it can't."""
    head = _git(repo, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise RuntimeError("This project has no commits yet.")
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        for args in (("read-tree", "HEAD"), ("add", "-A", "--", *files)):
            res = _git(repo, *args, env=env)
            if res.returncode != 0:
                raise RuntimeError((res.stderr or res.stdout).strip()[-300:])
        tree = _git(repo, "write-tree", env=env)
        if tree.returncode != 0:
            raise RuntimeError((tree.stderr or tree.stdout).strip()[-300:])
    commit = _git(repo, "commit-tree", tree.stdout.strip(), "-p", head.stdout.strip(),
                  "-m", message or f"Unfinished work of job {job_id} (Orchestrator backup)")
    if commit.returncode != 0:
        raise RuntimeError((commit.stderr or commit.stdout).strip()[-300:])
    sha = commit.stdout.strip()
    push = _git(repo, "push", "--force", "origin", f"{sha}:{ref_for(job_id)}", timeout=120)
    if push.returncode != 0:
        raise RuntimeError(re.sub(r"(https?://)[^/@\s]+@", r"\1", (push.stderr or push.stdout).strip())[-300:])
    return sha


def backup_job(repo: Path, job_id: str, job: dict[str, Any], machine: str = "", now: float | None = None) -> dict[str, Any] | None:
    """The job's wip_backup record after backing up its uncommitted files, or None when there is nothing new to do."""
    now = time.time() if now is None else now
    if job.get("moved_to") or job.get("status") in DONE or not job.get("branch"):
        return None
    files = sorted(set(job.get("ai_modified_files") or []) | set(job.get("ai_untracked_files") or []))
    current = _git(repo, "branch", "--show-current").stdout.strip()
    if current != job["branch"]:
        return None  # the files on disk belong to another branch now
    dirty = dirty_files(repo, files)
    if not dirty:
        return None
    previous = job.get("wip_backup") if isinstance(job.get("wip_backup"), dict) else {}
    stamp = digest(repo, dirty)
    if previous.get("digest") == stamp and (previous.get("commit") or now - float(previous.get("failed_at") or 0) < RETRY_SECONDS):
        return None
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    try:
        sha = save(repo, job_id, dirty)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return {"digest": stamp, "files": dirty, "failed_at": now, "error": str(exc)[:300], "machine": machine}
    return {"commit": sha, "ref": ref_for(job_id), "base": base, "files": dirty, "digest": stamp, "at": now, "machine": machine}


def restore(repo: Path, job_id: str, job: dict[str, Any]) -> list[str]:
    """Bring a job's backed-up unfinished files into this checkout, on the job's branch. Only when none of those files has
    changes here (nothing is overwritten). Returns the files restored; raises RuntimeError saying why not."""
    backup = job.get("wip_backup") if isinstance(job.get("wip_backup"), dict) else {}
    sha, files, branch = str(backup.get("commit") or ""), list(backup.get("files") or []), str(job.get("branch") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or not files or not branch:
        raise RuntimeError("This job has no unfinished work backed up.")
    fetched = _git(repo, "fetch", "origin", ref_for(job_id), timeout=120)
    if fetched.returncode != 0:
        raise RuntimeError("Couldn't fetch the unfinished work: " + (fetched.stderr or fetched.stdout).strip()[-200:])
    if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        raise RuntimeError("The backed-up work was replaced by a newer backup. Reload the job and try again.")
    current = _git(repo, "branch", "--show-current").stdout.strip()
    if current != branch:
        if _project_changes(repo):
            raise RuntimeError(f"This project has uncommitted changes on '{current}'. Commit or stash them, then try again.")
        _git(repo, "fetch", "origin", "--", branch, timeout=120)
        switched = _git(repo, "checkout", branch)
        if switched.returncode != 0:
            raise RuntimeError(f"Couldn't switch to '{branch}': " + (switched.stderr or switched.stdout).strip()[-200:])
    busy = dirty_files(repo, files)
    if busy:
        raise RuntimeError(f"These files already have changes here: {', '.join(busy[:3])}. Nothing was overwritten.")
    exists = set(_git(repo, "ls-tree", "-r", "--name-only", sha, "--", *files).stdout.split())
    present = [f for f in files if f in exists]
    if present:
        res = _git(repo, "checkout", sha, "--", *present)
        if res.returncode != 0:
            raise RuntimeError((res.stderr or res.stdout).strip()[-300:])
        _git(repo, "reset", "-q", "--", *present)  # back to uncommitted, as they were
    for f in files:
        if f not in exists and (repo / f).is_file():
            (repo / f).unlink()  # deleted in the unfinished work
    return files


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()[:40] or "another-computer"


def keep_stranded(repo: Path, job_id: str, job: dict[str, Any], machine: str) -> dict[str, Any] | None:
    """The job moved to another computer while this one held work it never uploaded: commits not on origin, or uncommitted
    files. Neither is thrown away or forced onto the job's branch: the commits go to a side branch,
    <branch>-from-<this computer>, and the files to the hidden ref <job id>-from-<this computer>. Returns what was kept."""
    branch = str(job.get("branch") or "")
    if not branch or branch.startswith("-") or not re.fullmatch(r"[A-Za-z0-9._/-]{1,160}", branch):
        return None
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
        return None
    kept: dict[str, Any] = {}
    _git(repo, "fetch", "origin", "--", branch, timeout=120)
    remote = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}")
    ahead = _git(repo, "rev-list", "--count", f"origin/{branch}..{branch}" if remote.returncode == 0 else branch).stdout.strip()
    if ahead and ahead != "0":
        side = f"{branch}-from-{_slug(machine)}"
        push = _git(repo, "push", "--force", "origin", f"refs/heads/{branch}:refs/heads/{side}", timeout=120)
        kept.update({"branch": side, "commits": int(ahead)} if push.returncode == 0 else {"error": (push.stderr or push.stdout).strip()[-300:]})
    current = _git(repo, "branch", "--show-current").stdout.strip()
    files = sorted(set(job.get("ai_modified_files") or []) | set(job.get("ai_untracked_files") or []))
    dirty = dirty_files(repo, files) if current == branch else []
    if dirty:
        try:
            kept.update({"wip_commit": save(repo, f"{job_id}-from-{_slug(machine)}", dirty), "wip_files": dirty})
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            kept["error"] = str(exc)[:300]
    if kept:
        kept.update({"machine": machine, "at": time.time()})
    return kept or None
