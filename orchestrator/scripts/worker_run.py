#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
os.environ["AI_REQUEST_SOURCE"] = "orchestrator"
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Force line-buffering for stdout/stderr to ensure logs stream immediately
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
except:
    pass

SCRIPTS_DIR = Path(__file__).resolve().parent

from common import (
    interactive,
    CONFIG_DIR,
    LOGS_DIR,
    OUTPUT_DIR,
    ROOT,
    StatusBar,
    find_latest_runtime_log,
    format_job_id,
    get_repo_state,
    PROJECT_CONFIG_FILE,
    dirty_file_hashes,
    project_config_hash,
    files_changed_between,
    gh_comment,
    is_firebase_configured,
    now_iso,
    print_phase,
    read_json,
    record_clarification,
    autorespond,
    can_autorespond,
    run,
    run_shell,
    slugify,
    update_issue_status,
    write_json as _write_json,
)
from open_or_update_pr import open_or_update_pr, push_branch_for_review
from run_builder import BuilderClarificationNeeded, run_builder
from run_build_and_tests import DiskFullError, require_build_space, run_build_and_tests
from llm import extract_json_block
from orchestrator.project_config import PROJECT_CONFIG
from orchestrator import blockers, builder_check, job_runs, model_outcomes, reuse_brief, scope_check, task_growth
from model_router import builder_for_task, resolve_builder_model


from typing import List, Optional, Tuple

YOLO_KEYS = ("yolo", "is_yolo")  # the app's YOLO switch; the app writes both spellings


def refresh_yolo(job: dict[str, Any], job_path: Path) -> bool:
    """Pick up the YOLO switch from the job file: the app can turn it on or off while this worker runs (YOLO all)."""
    try:
        on_disk = read_json(job_path)
    except Exception:
        return bool(job.get("is_yolo"))
    for key in YOLO_KEYS:
        if key in on_disk:
            job[key] = on_disk[key]
    return bool(job.get("is_yolo") or job.get("yolo"))


def write_json(path: Path, data: Any) -> None:
    """Save as usual, except a job's YOLO switch: what the file says wins, so this worker's older copy of the job doesn't
    undo a change made in the app while it ran."""
    if isinstance(data, dict) and path.parent.name == "jobs" and path.is_file():
        refresh_yolo(data, path)
    _write_json(path, data)

def is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def normalize_branch_mode(branch_mode: Optional[str]) -> str:
    if branch_mode in {"manual", "manual (no git actions)"}:
        return "manual"
    if branch_mode in {"current", "current-branch (git pull)"}:
        return "current"
    return "new"


def current_git_branch() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(ROOT),
    ).decode("utf-8").strip()


def job_branch_name(job: dict, issue_number: int | None) -> str:
    """`ai/issue-<n>-<title>` for a GitHub job; plain git has no issue, so its local reference names the branch."""
    if issue_number:
        return f'ai/issue-{issue_number}-{slugify(job["title"])}'
    return f'ai/job-{job.get("job_ref") or job["job_id"].rsplit("-", 1)[-1]}-{slugify(job["title"])}'


def _cleanup_stale_git_state(cwd: Path) -> None:
    """Best-effort cleanup of stale git lockfiles and orphaned worktree registrations."""
    git_dir = cwd / ".git"
    if git_dir.is_file():
        try:
            content = git_dir.read_text(encoding="utf-8", errors="replace").strip()
            if content.startswith("gitdir:"):
                raw = Path(content.split(":", 1)[1].strip())
                git_dir = raw if raw.is_absolute() else (cwd / raw)
        except OSError:
            pass
    if git_dir.is_dir():
        for lock_name in ("index.lock", "HEAD.lock"):
            lock_path = git_dir / lock_name
            if lock_path.is_file():
                try:
                    lock_path.unlink()
                except OSError:
                    pass
        worktrees_dir = git_dir / "worktrees"
        if worktrees_dir.is_dir():
            try:
                for wt in worktrees_dir.iterdir():
                    locked = wt / "locked"
                    gitdir_ref = wt / "gitdir"
                    if locked.is_file() and gitdir_ref.is_file():
                        target = Path(gitdir_ref.read_text(encoding="utf-8", errors="replace").strip())
                        if not target.exists():
                            locked.unlink(missing_ok=True)
            except OSError:
                pass
    subprocess.run(["git", "worktree", "prune"], cwd=str(cwd), capture_output=True, text=True, check=False)


def _format_git_checkout_error(branch: str, base_branch: str, exc: subprocess.CalledProcessError, detail: str = "") -> RuntimeError:
    """Turn a raw git checkout CalledProcessError into an actionable message with steps to fix it."""
    msg = (detail or getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or "").strip()
    lower = msg.lower()
    if "already used by worktree" in lower or "already checked out" in lower:
        steps = (
            "How to fix:\n"
            "- Switch away from that branch in the other worktree, or open that worktree folder to run the job.\n"
            "- Or change the job's branch mode to 'Create a new branch' (or 'Don't create or switch branches')."
        )
        headline = f"Could not check out branch '{branch}' because it is already checked out in another git worktree."
    elif "invalid reference" in lower or "did not match any file" in lower or "pathspec" in lower:
        steps = (
            "How to fix:\n"
            "- Run `git fetch origin` and `git branch -a` in the project folder to verify the branch name.\n"
            f"- Or change the job's branch mode to 'Create a new branch' to start from '{base_branch}'."
        )
        headline = f"Could not check out branch '{branch}' because it does not exist locally or on origin."
    elif ".lock" in lower or "unable to create" in lower:
        steps = (
            "How to fix:\n"
            "- Wait for any other git command or IDE git operation to finish, then retry.\n"
            "- If no git process is running, remove the stale `.git/index.lock` file and retry."
        )
        headline = f"Could not check out branch '{branch}' because git's lockfile is held by another process."
    else:
        steps = (
            "How to fix:\n"
            "- Run `git status` and `git worktree list` in the project folder to see what is blocking checkout.\n"
            "- Or change the job's branch mode to 'Create a new branch' (or 'Don't create or switch branches')."
        )
        headline = f"Could not check out branch '{branch}' (git exited with status {exc.returncode})."
    parts = [headline]
    if msg:
        parts.append(msg)
    parts.append(steps)
    return RuntimeError("\n\n".join(parts))


def _checkout_branch_with_recovery(
    branch: str,
    base_branch: str,
    job: dict,
    *,
    allow_current_fallback: bool = False,
) -> str:
    """Check out `branch`, autocorrecting stale locks, orphaned worktrees, remote-only refs, or stale branch metadata."""
    try:
        run(["git", "checkout", "-f", branch], cwd=ROOT)
        return branch
    except subprocess.CalledProcessError as exc:
        _cleanup_stale_git_state(ROOT)
        retry = subprocess.run(["git", "checkout", "-f", branch], cwd=str(ROOT), capture_output=True, text=True, check=False)
        if retry.returncode == 0:
            return branch

        has_remote = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=str(ROOT), capture_output=True, check=False,
        ).returncode == 0
        if has_remote:
            track = subprocess.run(
                ["git", "checkout", "-f", "-B", branch, f"origin/{branch}"],
                cwd=str(ROOT), capture_output=True, text=True, check=False,
            )
            if track.returncode == 0:
                return branch

        if allow_current_fallback:
            try:
                active = current_git_branch()
            except Exception:
                active = ""
            if active and active != "HEAD" and active != branch:
                print(f"      - Could not check out '{branch}'; continuing on current branch '{active}'.")
                job["branch"] = active
                return active
            if base_branch and base_branch != branch:
                print(f"      - Could not check out '{branch}'; falling back to '{base_branch}'.")
                run(["git", "checkout", "-f", base_branch], cwd=ROOT)
                job["branch"] = base_branch
                return base_branch

        raise _format_git_checkout_error(branch, base_branch, exc, getattr(retry, "stderr", "") or getattr(retry, "stdout", "")) from exc


def _resolve_git_dir(root: Path) -> Path:
    """Resolve the real .git directory, following gitdir: files for worktrees."""
    git_dir = root / ".git"
    if git_dir.is_file():
        try:
            content = git_dir.read_text(encoding="utf-8", errors="replace").strip()
            if content.startswith("gitdir:"):
                resolved = Path(content.split(":", 1)[1].strip())
                if not resolved.is_absolute():
                    resolved = (root / resolved).resolve()
                return resolved
        except OSError:
            pass
    return git_dir


def _try_clear_stale_index_lock(root: Path, min_age_seconds: float = 10.0) -> bool:
    """Remove .git/index.lock only when older than min_age_seconds and lsof confirms no process holds it."""
    lock = _resolve_git_dir(root) / "index.lock"
    if not lock.exists():
        return False
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False
    if age < min_age_seconds:
        return False
    lsof_bin = shutil.which("lsof") or ("/usr/sbin/lsof" if Path("/usr/sbin/lsof").exists() else None)
    if not lsof_bin:
        return False
    try:
        probe = subprocess.run([lsof_bin, str(lock)], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    # lsof exits 1 with empty stdout when no process has the file open; fail closed on 0 or >1.
    if probe.returncode != 1 or probe.stdout.strip():
        return False
    try:
        lock.unlink(missing_ok=True)
        print(f"      - Removed stale {lock.name} (no active process, age {int(age)}s).")
        return True
    except OSError:
        return False


def _parse_untracked_overwrite_paths(detail: str) -> list[str]:
    """Extract relative file paths from git's 'untracked working tree files would be overwritten' error."""
    paths: list[str] = []
    collecting = False
    for raw_line in detail.splitlines():
        lower = raw_line.lower()
        if "untracked working tree files would be overwritten" in lower:
            collecting = True
            continue
        if not collecting:
            continue
        if "please move or remove them" in lower or (raw_line and not raw_line[0].isspace()):
            break
        item = raw_line.strip()
        if not item:
            continue
        if len(item) >= 2 and item[0] == '"' and item[-1] == '"':
            try:
                item = item[1:-1].encode("utf-8").decode("unicode_escape")
            except UnicodeDecodeError:
                item = item[1:-1]
        paths.append(item)
    return paths


def _remove_identical_untracked_files(root: Path, branch: str, paths: list[str]) -> tuple[list[str], list[str]]:
    """Unlink colliding untracked files whose git blob hash matches origin/<branch>:<path>."""
    removed: list[str] = []
    remaining: list[str] = []
    root_resolved = root.resolve()
    env = {**os.environ, "LC_ALL": "C"}
    for rel in paths:
        if not rel or rel.startswith("-") or Path(rel).is_absolute():
            remaining.append(rel)
            continue
        candidate = root / rel
        try:
            resolved = candidate.resolve()
        except OSError:
            remaining.append(rel)
            continue
        if not resolved.is_relative_to(root_resolved) or not candidate.exists() or candidate.is_dir() or candidate.is_symlink():
            remaining.append(rel)
            continue
        local_hash = subprocess.run(
            ["git", "hash-object", "--", rel],
            cwd=str(root), env=env, capture_output=True, text=True, check=False,
        )
        remote_hash = subprocess.run(
            ["git", "rev-parse", f"origin/{branch}:{rel}"],
            cwd=str(root), env=env, capture_output=True, text=True, check=False,
        )
        local_sha = local_hash.stdout.strip()
        remote_sha = remote_hash.stdout.strip()
        if local_hash.returncode == 0 and remote_hash.returncode == 0 and local_sha and local_sha == remote_sha:
            try:
                candidate.unlink()
                removed.append(rel)
                continue
            except OSError:
                pass
        remaining.append(rel)
    if removed:
        print(f"      - Removed {len(removed)} untracked file(s) identical to origin/{branch}.")
    return removed, remaining


def _is_index_lock_error(detail: str) -> bool:
    return "index.lock" in detail or "Another git process seems to be running" in detail


GIT_LOCK_RETRY_SECONDS = float(os.environ.get("ORCHESTRATOR_GIT_LOCK_RETRY_SECONDS") or 10)  # between commit retries on a held lock


def _git_commit_step(cmd: str) -> None:
    """One git command committing the job's finished work. A leftover .git/index.lock (a git command that was killed, often a
    tool's status check that timed out) is cleared and the command retried, instead of failing a job whose tests passed."""
    try:
        run_shell(cmd, cwd=ROOT)
        return
    except subprocess.CalledProcessError as exc:
        res = exc
    detail = f"{res.stderr or ''}\n{res.stdout or ''}"
    if _is_index_lock_error(detail):
        for _ in range(3):  # a live git command finishes in moments; wait out the clear-by-age rule and retry
            if _try_clear_stale_index_lock(ROOT) or not (_resolve_git_dir(ROOT) / "index.lock").exists():
                try:
                    run_shell(cmd, cwd=ROOT)
                    return
                except subprocess.CalledProcessError as retry_exc:
                    res = retry_exc
                detail = f"{res.stderr or ''}\n{res.stdout or ''}"
                if not _is_index_lock_error(detail):
                    break
            time.sleep(GIT_LOCK_RETRY_SECONDS)
        if _is_index_lock_error(detail):
            raise RuntimeError(
                "The work is done and its tests passed, but committing it failed: another git command is holding this project's "
                f"git lock ({_resolve_git_dir(ROOT) / 'index.lock'}).\n\nHow to fix:\n- Wait for other git tools (an IDE, Codex, a "
                "status line) to finish, then Resume.\n- If nothing is running git, delete that file, then Resume.")
    raise res


def commit_paths_command(files: list[str], message: str) -> str:
    """`git commit` of exactly these paths. A bare `git commit` also takes whatever else is staged in the project (a tool's
    helper files, a parallel session's work), which once put six `.claude/helpers` files into a job's commit; a pathspec
    commits only the named files and leaves everything else staged where it was."""
    return f"git commit -m {shlex.quote(message)} -- " + " ".join(shlex.quote(f) for f in files)


def commit_job_files(files: list[str], message: str) -> None:
    """Stage and commit the job's own files, and nothing else."""
    for f in files:
        _git_commit_step(f"git add -- {shlex.quote(f)}")
    _git_commit_step(commit_paths_command(files, message))


def _pull_branch_with_recovery(branch: str, *, allow_missing_remote: bool = False) -> None:
    """Fast-forward `branch` from origin; recover stale locks/identical untracked files or rebase, else raise."""
    git_env = {"LC_ALL": "C"}

    def _detail_from_exc(exc: subprocess.CalledProcessError) -> str:
        return "\n".join(
            part.strip()
            for part in (getattr(exc, "stderr", "") or "", getattr(exc, "stdout", "") or "")
            if part and part.strip()
        )

    def _raise_lock_error(detail: str, cause: Exception | None = None) -> None:
        raise RuntimeError(
            f"Could not update branch '{branch}' from 'origin/{branch}' because a Git index lock file (.git/index.lock) is present.\n\n"
            f"{detail}\n\n"
            "How to fix:\n"
            "- Remove the stale `.git/index.lock` file (or use the UI 'Remove lock & Rerun' recovery button) if no git process is running, then rerun the job."
        ) from cause

    def _raise_untracked_error(detail: str, cause: Exception | None = None, *, remaining: list[str] | None = None, removed: list[str] | None = None) -> None:
        shown_detail = detail
        if removed and remaining:
            shown_detail = (
                "The following untracked working tree files would be overwritten:\n"
                + "\n".join(f"\t{p}" for p in remaining)
                + "\nPlease move or remove them before you merge."
            )
        raise RuntimeError(
            f"Could not update branch '{branch}' from 'origin/{branch}' because untracked local files would be overwritten by incoming commits.\n\n"
            f"{shown_detail}\n\n"
            "How to fix:\n"
            "- Move, commit, stash, or remove the listed untracked files in the project folder, then rerun the job.\n"
            "- Or change the job's branch mode to 'Create a new branch' (or 'Don't create or switch branches')."
        ) from cause

    try:
        run(["git", "pull", "--ff-only", "origin", branch], cwd=ROOT, env=git_env)
        return
    except subprocess.CalledProcessError as exc:
        has_remote = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=str(ROOT), capture_output=True, check=False,
        ).returncode == 0
        if allow_missing_remote and not has_remote:
            print(f"      - Remote branch origin/{branch} does not exist yet; continuing with local branch.")
            return

        ff_detail = _detail_from_exc(exc)
        if _is_index_lock_error(ff_detail):
            if _try_clear_stale_index_lock(ROOT):
                try:
                    run(["git", "pull", "--ff-only", "origin", branch], cwd=ROOT, env=git_env)
                    return
                except subprocess.CalledProcessError as retry_exc:
                    ff_detail = _detail_from_exc(retry_exc)
                    if _is_index_lock_error(ff_detail):
                        _raise_lock_error(ff_detail, retry_exc)
            else:
                _raise_lock_error(ff_detail, exc)

        if "untracked working tree files would be overwritten" in ff_detail:
            paths = _parse_untracked_overwrite_paths(ff_detail)
            removed, remaining = _remove_identical_untracked_files(ROOT, branch, paths) if paths else ([], [])
            if paths and not remaining:
                try:
                    run(["git", "pull", "--ff-only", "origin", branch], cwd=ROOT, env=git_env)
                    return
                except subprocess.CalledProcessError as retry_exc:
                    ff_detail = _detail_from_exc(retry_exc)
                    if "untracked working tree files would be overwritten" in ff_detail:
                        _raise_untracked_error(ff_detail, retry_exc)
            else:
                _raise_untracked_error(ff_detail, exc, removed=removed, remaining=remaining)

        print(f"      - Fast-forward pull failed for '{branch}'; attempting rebase onto origin/{branch}...")
        rebase_env = {**os.environ, **git_env}
        rebase = subprocess.run(
            ["git", "pull", "--rebase", "origin", branch],
            cwd=str(ROOT), env=rebase_env, capture_output=True, text=True, check=False,
        )
        if rebase.returncode == 0:
            print(f"      - Rebased '{branch}' onto origin/{branch}.")
            return

        detail = (rebase.stderr or rebase.stdout or ff_detail or "").strip()
        if _is_index_lock_error(detail) and _try_clear_stale_index_lock(ROOT):
            rebase = subprocess.run(
                ["git", "pull", "--rebase", "origin", branch],
                cwd=str(ROOT), env=rebase_env, capture_output=True, text=True, check=False,
            )
            if rebase.returncode == 0:
                print(f"      - Rebased '{branch}' onto origin/{branch}.")
                return
            detail = (rebase.stderr or rebase.stdout or ff_detail or "").strip()

        if "untracked working tree files would be overwritten" in detail:
            paths = _parse_untracked_overwrite_paths(detail)
            _, remaining = _remove_identical_untracked_files(ROOT, branch, paths) if paths else ([], [])
            if paths and not remaining:
                rebase = subprocess.run(
                    ["git", "pull", "--rebase", "origin", branch],
                    cwd=str(ROOT), env=rebase_env, capture_output=True, text=True, check=False,
                )
                if rebase.returncode == 0:
                    print(f"      - Rebased '{branch}' onto origin/{branch}.")
                    return
                detail = (rebase.stderr or rebase.stdout or ff_detail or "").strip()

        subprocess.run(["git", "rebase", "--abort"], cwd=str(ROOT), capture_output=True, check=False)
        if _is_index_lock_error(detail):
            _raise_lock_error(detail, exc)
        if "untracked working tree files would be overwritten" in detail:
            _raise_untracked_error(detail, exc)
        raise RuntimeError(
            f"Could not update branch '{branch}' from 'origin/{branch}' because local and remote commits have diverged"
            f" and automatic rebase could not complete cleanly.\n\n"
            f"{(detail + chr(10) + chr(10)) if detail else ''}"
            "How to fix:\n"
            f"- In the project folder, run `git pull --rebase origin {branch}` and resolve any conflicts, then rerun the job.\n"
            f"- Or run `git reset --hard origin/{branch}` if you want to discard unpushed local commits on '{branch}'.\n"
            "- Or change the job's branch mode to 'Create a new branch' (or 'Don't create or switch branches')."
        ) from exc


def keep_unfinished_work(job: dict, branch: str) -> bool:
    """Uncommitted changes to tracked files must survive branch preparation: `git checkout -f` throws them away, even on the
    branch they were made on. True when they're on `branch` already, so nothing may be checked out; otherwise they're set
    aside in a stash before anything is."""
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all"],
                            cwd=str(ROOT), capture_output=True, text=True, check=False)
    changed = [line for line in str(getattr(status, "stdout", "") or "").splitlines() if line.strip()]
    if not changed:
        return False
    current = current_git_branch()
    if current == branch:
        return True
    files = f"{len(changed)} uncommitted file{'' if len(changed) == 1 else 's'}"
    label = f"orchestrator-kept-{job.get('job_id') or 'job'}-{int(time.time())}"
    stashed = subprocess.run(["git", "stash", "push", "-m", label], cwd=str(ROOT), capture_output=True, text=True, check=False)
    if stashed.returncode != 0:
        raise RuntimeError(f"This project has {files} on '{current}', and they couldn't be set aside before switching to "
                           f"'{branch}': {(stashed.stderr or stashed.stdout).strip()[-300:]}\n\n"
                           "How to fix:\n- Commit or stash them, then run the job again.")
    print(f"      - Set aside {files} from '{current}' in a git stash named {label}. `git stash list` shows it; "
          "`git stash pop` brings it back.")
    return False


def prepare_git_branch(job: dict, issue_number: int | None) -> Tuple[str, str]:
    base_branch = job.get("base_branch") or os.environ.get("BASE_BRANCH", PROJECT_CONFIG.base_branch)
    generated_branch = job_branch_name(job, issue_number)
    branch_mode = normalize_branch_mode(job.get("branch_mode"))

    if branch_mode == "manual":
        branch = job.get("branch") or current_git_branch()
        print(f"\n[1/4] Manual branch mode. Using current branch metadata: {branch}")
        print("      - Branch mode is 'manual'. Skipping git operations.")
        return branch, base_branch

    target = job.get("branch") or (current_git_branch() if branch_mode == "current" else generated_branch)
    if keep_unfinished_work(job, target):  # an earlier attempt's work, waiting here for this one
        print(f"\n[1/4] Staying on {target}: it has unfinished changes, which checking it out again would discard.")
        run(["git", "fetch", "origin"], cwd=ROOT, check=False)
        pulled = run(["git", "pull", "--ff-only", "origin", target], cwd=ROOT, check=False)  # refuses to overwrite them
        if pulled.returncode != 0:
            print(f"      - Couldn't update from origin/{target}; carrying on with what is here.")
        job["branch"] = target
        return target, base_branch

    if branch_mode == "current":
        branch = job.get("branch") or current_git_branch()
        print(f"\n[1/4] Preparing current branch: {branch}...")
        print(f"      - Pulling latest for current branch {branch}...")
        if PROJECT_CONFIG.git_remote:
            run(["git", "remote", "set-url", "origin", PROJECT_CONFIG.git_remote], cwd=ROOT)
        try:
            from orchestrator.setup_checklist import ensure_gh_git_credentials
            ensure_gh_git_credentials()
        except Exception:
            pass
        run(["git", "fetch", "origin"], cwd=ROOT)
        branch = _checkout_branch_with_recovery(branch, base_branch, job, allow_current_fallback=True)
        _pull_branch_with_recovery(branch, allow_missing_remote=True)
        return branch, base_branch

    branch = job.get("branch") or generated_branch
    from orchestrator import parallel_jobs
    if os.environ.get(parallel_jobs.ENV_MAIN):
        return prepare_worktree_branch(job, branch, base_branch)
    print(f"\n[1/4] Preparing git branch: {branch}...")
    print(f"      - Fetching origin and updating {base_branch}...")
    if PROJECT_CONFIG.git_remote:
        run(["git", "remote", "set-url", "origin", PROJECT_CONFIG.git_remote], cwd=ROOT)
    run(["git", "fetch", "origin"], cwd=ROOT)
    _checkout_branch_with_recovery(base_branch, base_branch, job, allow_current_fallback=False)
    _pull_branch_with_recovery(base_branch, allow_missing_remote=False)

    checkout_res = subprocess.run(["git", "checkout", "-f", branch], cwd=str(ROOT), capture_output=True)
    if checkout_res.returncode == 0:
        print(f"      - Switched to existing branch: {branch}")
        subprocess.run(["git", "pull", "origin", branch], cwd=str(ROOT), capture_output=True)
    else:
        print(f"      - Creating new branch from {base_branch}: {branch}")
        run(["git", "checkout", "-f", "-b", branch], cwd=ROOT)

    return branch, base_branch


def mark_human_needed(job_path: Path, job: dict, question: str, task_index: Optional[int] = None) -> None:
    job["status"] = "human-needed"
    job["human_clarification_question"] = question
    job["clarification_asker"] = "builder"
    job["clarification_asked_at"] = now_iso()
    if os.environ.get("ORCHESTRATOR_RUN_ID"):
        job["clarification_run_id"] = os.environ["ORCHESTRATOR_RUN_ID"]
    job["last_error"] = f"Builder clarification needed: {question}"
    if task_index is not None:
        job["paused_at_task_index"] = task_index
    job["updated_at"] = now_iso()
    write_json(job_path, job)


def format_distributed_status(status_str: Optional[str]) -> str:
    if not status_str:
        return ""
    if status_str.startswith("SUCCESS"):
        rest = status_str[len("SUCCESS"):]
        return f"\033[1;92mSUCCESS\033[0m{rest}"
    elif status_str.startswith("FAILED"):
        rest = status_str[len("FAILED"):]
        return f"\033[1;91mFAILED\033[0m{rest}"
    elif status_str.startswith("Skipped") or status_str.startswith("Not Triggered"):
        return f"\033[90m{status_str}\033[0m"
    return status_str


def print_status_report(job: dict, build_ok: bool, test_ok: bool, pr_number: Optional[int] = None, pr_url: Optional[str] = None, distributed_status: Optional[str] = None) -> None:
    is_success = build_ok and test_ok
    status = job.get("status", "unknown")
    
    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = 80
    box_width = max(20, min(60, cols - 2))
    
    if is_success:
        print("\n\033[92m" + "=" * box_width)
        print("  🎉 SUCCESS: Implementation Verified  🎉")
        print("=" * box_width)
        
        accomplishment = job.get("builder_summary")
        if not accomplishment and job.get("debug_history"):
            last_debug = job["debug_history"][-1]
            accomplishment = last_debug.get("summary") or last_debug.get("action")
        if not accomplishment:
            brief_dir = OUTPUT_DIR / job.get("job_id", "")
            summary_file = brief_dir / "builder_summary.md"
            if summary_file.exists():
                try:
                    import json
                    content = summary_file.read_text(encoding="utf-8")
                    parsed = json.loads(extract_json_block(content))
                    accomplishment = parsed.get("summary")
                except Exception:
                    pass

        summary = accomplishment or job.get("plan", {}).get("summary")
        summary_label = "Accomplishment:" if accomplishment else "Plan Summary:"
        if summary:
            import textwrap
            wrap_width = max(20, min(80, cols - 4))
            wrapped = textwrap.fill(
                summary,
                width=wrap_width,
                initial_indent="  ",
                subsequent_indent="  ",
                break_long_words=False,
                break_on_hyphens=False
            )
            print(f"\n  \033[1;97m{summary_label}\033[0m")
            print(f"\033[90m{wrapped}\033[0m")

        print("""
     _      _      _
    ( )    ( )    ( )
     X      X      X
    / \\    / \\    / \\ 
        """)
        print(f"  Job ID:      {job['job_id']}")
        print(f"  Status:      {status}")
        if pr_number:
            print(f"  PR:          #{pr_number}")
        if distributed_status:
            print(f"  Distributed: {format_distributed_status(distributed_status)}")
        print("\033[0m")
        
        print("\n\033[1;97mNEXT STEPS:\033[0m")
        if pr_url:
            clickable_pr = f"\033]8;;{pr_url}\033\\{pr_url}\033]8;;\033\\"
            print(f"  1. Review PR changes on GitHub:\n     \033[4;96m{clickable_pr}\033[0m")
        elif pr_number:
            print(f"  1. Review PR changes on GitHub:\n     \033[1;96mgh pr view {pr_number} --web\033[0m")
        elif job.get("merge_request_url"):
            print(f"  1. Review the branch {job.get('branch')}, or open a merge request:\n     \033[4;96m{job['merge_request_url']}\033[0m")
        else:
            print(f"  1. Review the changes on branch {job.get('branch')}.")
        
        print("  2. Test and verify manually on device (via Firebase App Distribution) or simulator.")
        print("  3. Follow up based on your verification:")
        print("     • \033[1;92mIf verified & working:\033[0m Merge PR on GitHub or run \033[1;96morchestrator console\033[0m -> \033[92m[M] Merge\033[0m.")
        job_type = job.get("type", "")
        if job_type in {"feature-plan", "feature", "feature-design"}:
            print("     • \033[1;93mIf bug / missing functionality:\033[0m Enter feedback below or run \033[1;96morchestrator console\033[0m -> \033[1;93m[H] Bug/Missing Functionality\033[0m.")
        else:
            print("     • \033[1;93mIf issues persist / iterate:\033[0m Enter feedback below or run \033[1;96morchestrator console\033[0m -> \033[1;91m[H] Bug Still Happening?\033[0m.")
        print("       \033[90m💡 Tip: Runtime logs are automatically detected and attached for the next AI pass.\033[0m")
    else:
        print("\n\033[1;91m" + "!" * box_width)
        print("  ⚠️  FAILURE: Human Intervention Needed  ⚠️")
        print("!" * box_width)
        print("""
     _______
    |  ___  |
    | |   | |
    | |___| |
    |  ___  |
    | |   | |
    |_|   |_|
        """)
        print(f"  Job ID:      {job['job_id']}")
        print(f"  Status:      {status}")
        print(f"  Build:       {'✅ OK' if build_ok else '❌ FAILED'}")
        print(f"  Tests:       {'✅ OK' if test_ok else '❌ FAILED'}")
        if distributed_status:
            print(f"  Distributed: {format_distributed_status(distributed_status)}")
        print("\033[0m")
        
        print("\n\033[1;97mNEXT STEPS:\033[0m")
        print(f"  1. Inspect build/test failure logs in: \033[1;96m.orchestrator/output/{job['job_id']}/\033[0m")
        print(f"  2. Open \033[1;96morchestrator console\033[0m to link reproduction logs (\033[1;93m[L] Link Logs\033[0m) or provide hints (\033[1;93m[F] Tweak\033[0m).")
        print(f"  3. Resume or auto-fix (\033[1;92m[D]\033[0m Auto-Fix / \033[1;93m[U]\033[0m Resume) once feedback or logs are linked.")


def print_clarification_report(job: dict, question: Optional[str] = None) -> None:
    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = 80
    box_width = max(20, min(60, cols - 2))
    
    print("\n\033[1;93m" + "!" * box_width)
    print("  ⏸️  PAUSED: Builder Needs Clarification  ")
    print("!" * box_width + "\033[0m")
    print(f"Job ID:      {job['job_id']}")
    print(f"Status:      {job.get('status', 'human-needed')}")
    print("\nQuestion:")
    q_text = question or job.get("human_clarification_question", "No clarification question recorded.")
    import textwrap
    wrap_width = max(20, min(80, cols - 4))
    wrapped_q = textwrap.fill(
        q_text,
        width=wrap_width,
        initial_indent="  ",
        subsequent_indent="  ",
        break_long_words=False,
        break_on_hyphens=False
    )
    print(wrapped_q)
    print("\nNEXT STEPS:")
    print("  1. Open \033[1;96morchestrator console\033[0m to answer the question (\033[1;96m[A] Answer Question\033[0m).")
    print("  2. The answer will be supplied to the AI builder and execution will resume automatically.")
    print("=" * box_width + "\n")


def trigger_followup_iteration(job_path: Path, job: dict, feedback: str) -> None:
    recent_log = find_latest_runtime_log(job.get("job_id"))
    log_paths = job.get("last_manual_log_paths", [])
    if not isinstance(log_paths, list):
        log_paths = []
    if recent_log and recent_log not in log_paths:
        log_paths.append(recent_log)
        print(f"\n  ✅ Attached latest log file: \033[97m{recent_log}\033[0m")

    try:
        from orchestrator import experience_loop
        print_phase("analyzing_feedback")
        exp_outcome = experience_loop.apply_experience_feedback(job, feedback, root=ROOT, now_iso=now_iso())
        cls = exp_outcome.get("classification") or {}
        print("[experience] " + json.dumps({
            "version": exp_outcome.get("version", "0.2"),
            "feedback_type": cls.get("feedback_type", "behavior_change"),
            "impact": f"{cls.get('feedback_type', 'behavior_change')} · {cls.get('default_action', '')}",
            "stage": "Experience loop · Refining",
            "label": "Consolidating & refactoring" if exp_outcome.get("refactor_recommended") else "Refining from user feedback",
        }), flush=True)
        if exp_outcome.get("refactor_recommended"):
            print_phase("refactoring")
        else:
            print_phase("refining")
        job["refinement_context"] = experience_loop.format_refinement_prompt(job, feedback, exp_outcome)
    except Exception:
        pass
        
    job["status"] = "debugging"
    job["debug_phase"] = "propose"
    job["last_manual_log_paths"] = log_paths
    
    if "debug_history" not in job:
        job["debug_history"] = []
        
    iter_num = job.get("iteration", 0) + 1
    job_type = job.get("type", "")
    if job_type in ["feature-plan", "feature", "feature-design"]:
        hypothesis = "User reported bug or missing functionality in new feature."
    else:
        hypothesis = "User follow-up / observation on latest run."

    job["debug_history"].append({
        "iteration": iter_num,
        "hypothesis": hypothesis,
        "action": "Iterate fix based on user feedback.",
        "implementation_plan": feedback,
        "expected_signal": "Validation successful",
        "result": "pending"
    })
    job["iteration"] = iter_num
    job["updated_at"] = now_iso()
    write_json(job_path, job)
    
    print("\n🚀 Starting automated debugging iteration based on your follow-up...")
    from debug_job import run_debug_iteration
    run_debug_iteration(job_path)


def send_notifications(job: dict, title: str, message: str, summary: str | None = None) -> None:
    job_id = job.get("job_id")
    try:
        args = [sys.executable, str(SCRIPTS_DIR / "notify.py"), title, message, job_id or ""]
        if summary:
            args.append(summary)
        subprocess.run(args, cwd=str(ROOT), check=False)
    except:
        pass


def unpack_builder_result(result):
    if len(result) == 3:
        build_ok, test_ok, summary_path = result
        return build_ok, test_ok, summary_path, {}
    return result


# Job types whose whole point is to change code. An investigation may rightly change nothing.
NO_CHANGE_EXEMPT_TYPES = {"bug-investigate"}
BLOCKED_EDIT_WORDS = ("permission", "blocked", "not allowed", "denied", "read-only", "read only")
ALREADY_COVERED_WORDS = ("already in place", "already implemented", "already covered", "already exists", "no changes needed", "unchanged")


def _job_target_files(job: dict, task: Optional[dict] = None) -> set[str]:
    files: set[str] = set()
    if isinstance(task, dict):
        for f in task.get("likely_files") or []:
            if isinstance(f, str) and f.strip():
                files.add(f.strip().removeprefix("./"))
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    for f in plan.get("likely_files") or []:
        if isinstance(f, str) and f.strip():
            files.add(f.strip().removeprefix("./"))
    for t in plan.get("tasks") or []:
        if isinstance(t, dict):
            for f in t.get("likely_files") or []:
                if isinstance(f, str) and f.strip():
                    files.add(f.strip().removeprefix("./"))
    return files


def overlapping_completed_job_question(job: dict, job_path: Optional[Path] = None, task: Optional[dict] = None) -> str:
    """When a build changes no files and another completed job already touched the same target files (or the builder
    explicitly reports the change is already in place), return a clear stopping clarification question for the user."""
    target_files = _job_target_files(job, task)
    my_id = str(job.get("job_id") or (job_path.stem if job_path else ""))
    model_used = str(job.get("actual_builder_used") or job.get("builder") or "The builder")
    summary = " ".join(str(job.get("builder_summary") or "").split())
    short_summary = (summary[:220] + "…") if len(summary) > 220 else summary

    if job_path and job_path.parent.is_dir() and target_files:
        folders = [job_path.parent, job_path.parent / "archive"]
        candidates: list[Path] = []
        for folder in folders:
            if folder.is_dir():
                candidates.extend(sorted(folder.glob("*.json"), reverse=True))
        for other_path in candidates:
            if other_path.stem == my_id or re.search(r"_task_\d+$", other_path.stem):
                continue
            try:
                other = read_json(other_path)
            except Exception:
                continue
            if not isinstance(other, dict) or other.get("status") != "completed":
                continue
            other_files = {
                str(f).strip().removeprefix("./")
                for f in (
                    *(other.get("ai_modified_files") or []),
                    *(other.get("ai_untracked_files") or []),
                    *_job_target_files(other),
                )
                if isinstance(f, str) and f.strip()
            }
            shared = sorted(
                tf for tf in target_files
                if any(tf == of or tf.endswith("/" + of) or of.endswith("/" + tf) for of in other_files)
            )
            if shared:
                other_title = str(other.get("title") or other.get("job_id") or other_path.stem)
                files_list = ", ".join(shared[:3]) + (f" (+{len(shared) - 3} more)" if len(shared) > 3 else "")
                note = f' Builder note: "{short_summary}"' if short_summary else ""
                return (
                    f'Completed job "{other_title}" ({other.get("job_id") or other_path.stem}) already modified {files_list}, '
                    f"and {model_used} made no file changes in this run.{note} "
                    "Does that completed job already cover this requirement (so this job can be closed), or what specific changes still need to be built?"
                )

    if summary and any(w in summary.lower() for w in ALREADY_COVERED_WORDS) and not any(w in summary.lower() for w in BLOCKED_EDIT_WORDS):
        return (
            f'{model_used} made no file changes and reported that the work may already be in place: "{short_summary}". '
            "Should this job be closed as already covered, or what specific changes still need to be made?"
        )
    return ""


def no_change_stop(job: dict, changed_now: list[str], *, debug_attempt: bool = False) -> str:
    """Why the job must stop because the builder changed no files, or "" to carry on. The build step only counts as done
    when files changed: a job that has never changed a file stops; a fix attempt that changed nothing stops too, since
    running the same failing tests again can't tell anyone anything new."""
    if job.get("type") in NO_CHANGE_EXEMPT_TYPES or changed_now:
        return ""
    # Finished tasks count: each was only marked done after it changed files, and those were committed then (checkpoint_task).
    ever_changed = bool(job.get("ai_modified_files") or job.get("ai_untracked_files") or job.get("completed_task_indices"))
    if ever_changed and not debug_attempt:
        return ""
    summary = " ".join(str(job.get("builder_summary") or "").split())
    blocked = any(w in summary.lower() for w in BLOCKED_EDIT_WORDS)
    what = "The fix attempt" if debug_attempt and ever_changed else "The build"
    model_used = str(job.get("actual_builder_used") or "").strip()
    model_note = f" ({model_used})" if model_used else ""
    summary_note = f' Builder reported: "{summary[:180]}".' if summary and not blocked and summary != "Implemented it." else ""
    reason = (" The AI said its file edits were blocked, so check that its tool may edit files, then run the job again."
              if blocked else f"{summary_note} If the change was already in place, close the job; otherwise run it again, or describe the change more specifically.")
    return f"{what}{model_note} finished without changing any files, so nothing was built.{reason}"


def blocker_in_logs(job: dict) -> Optional[dict]:
    """A blocker named in this run's build or test log (a git lock, a sign-in, a missing tool, a full disk), if any."""
    out_dir = OUTPUT_DIR / str(job.get("job_id") or "")
    text = ""
    for name in ("build.log", "test.log", "environment_failure.md"):
        try:
            text += (out_dir / name).read_text(encoding="utf-8", errors="replace")[-200_000:] + "\n"
        except OSError:
            pass
    return blockers.detect(text)


def other_job_in_folder(job_path: Path) -> Optional[dict]:
    """A blocker when another open job's uncommitted work is in the project folder (blockers.other_jobs_work)."""
    jobs = []
    for path in sorted(Path(job_path).parent.glob("*.json")):
        try:
            jobs.append((path.stem, read_json(path)))
        except Exception:
            continue
    try:
        return blockers.other_jobs_work(ROOT, Path(job_path).stem, jobs, current_git_branch())
    except Exception:
        return None  # can't tell: carry on as before rather than stop every job


def missing_build_tool_blocker(job: dict) -> Optional[dict]:
    """Check before starting work whether this computer is missing a tool required by build_command/test_command."""
    if os.environ.get("ORCHESTRATOR_SKIP_TOOL_PREFLIGHT") == "1":
        return None
    if job.get("passed_uncommitted") or job.get("type") in ("design", "feature-design", "bug-investigate"):
        return None
    cfg_path = ROOT / ".orchestrator" / "project.json"
    if not cfg_path.is_file():
        return None
    try:
        from orchestrator import build_tools, manifest_deps
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            return None
        missing = build_tools.missing_project_tools(cfg)
        if missing:
            return blockers.missing_tool_blocker(missing)
        unhydrated = manifest_deps.unhydrated_manifests(ROOT, cfg)
        if unhydrated:
            names = ", ".join(m["name"] for m in unhydrated)
            return {
                "kind": "tool",
                "scope": "project",
                "key": unhydrated[0]["tool"],
                "title": "Project packages aren't installed yet",
                "detail": f"Found lockfiles without local packages installed ({names}). Click Set up environment to install them.",
            }
        return None
    except Exception:
        return None


def pause_on_blocker(job_path: Path, job: dict, blocker: dict, issue_number: Optional[int]) -> None:
    print(f"\n⚠️ {blocker['title']}. {blocker['detail']} This isn't a code problem, so no fix attempt is started; "
          "the job resumes once it's cleared.")
    blockers.record(job, blocker)
    job["status"] = "human-needed"
    job["last_error"] = f"{blocker['title']}. {blocker['detail']}"
    job["worker_pid"] = None
    job["updated_at"] = now_iso()
    write_json(job_path, job)
    update_issue_status(issue_number, "status:human-needed", ["status:executing"])
    send_notifications(job, f"Job Paused: {blocker['title']}", f"{blocker['detail']}\nTitle: {job['title']}")


def link_run_to_tasks(job_path: Path, job: dict, tasks: list, indices: list[int]) -> None:
    members = [tasks[index] for index in indices if isinstance(tasks[index], dict)]
    if not members:
        return
    if job_runs.ensure_task_ids([task for task in tasks if isinstance(task, dict)]):
        write_json(job_path, job)
    print(job_runs.task_marker(job["job_id"], members), flush=True)


MISSING_TESTS = "The run used unrelated tests or omitted planned tests"  # the project's own tests can settle this one
PROJECT_TESTS_FAILED = ("The project's own tests, run to check this, failed. The failure may not come from this job: "
                        "see the test log in the job's output.")


def task_validation(task: dict, peers: list[dict], files: list[str], output: dict,
                    build_ok: bool, test_ok: bool) -> tuple[str, str]:
    if not build_ok or not test_ok:
        return "failed", "The build or tests failed. See the run log."
    action = output.get("action")
    if action not in ("patch", "add_logging") and (action or not files):
        return "incomplete", "AI did not write code. The task was not implemented."

    def matches(path: str, planned: str) -> bool:
        planned = planned.removeprefix("./")
        return path == planned or path.endswith("/" + planned)

    expected_files = task.get("likely_files") or []
    matched = [f for f in files if any(matches(f, name) for name in expected_files)]
    if expected_files and not matched:
        return "incomplete", "None of the expected files changed."

    expected_tests = task.get("tests") or []
    requested = " ".join((output.get("test_command") or "").split())
    execution = output.get("_test_execution")
    if expected_tests and execution and execution.get("fell_back"):
        return "incomplete", "The selected tests could not be used. The configured suite ran instead."
    configured = " ".join((PROJECT_CONFIG.test_command or "").split())
    command = execution.get("command", "") if execution is not None else requested
    full_suite = execution.get("full_suite", False) if execution is not None else not requested or requested == configured
    # A selected test name must stand on its own. Exclusion selectors cannot prove coverage. Steps chained with && do count:
    # the run passed, so every one of them did. Other joins (;, ||, |, &) can hide a step that failed or never ran.
    excluded = re.search(r"\bnot\b|--(?:skip|exclude|ignore)|-skip-testing|[#;&|]", command.replace("&&", " "), re.IGNORECASE)

    def selected(test: str) -> bool:
        return not excluded and bool(re.search(r"(?<![\w-])" + re.escape(test) + r"(?![\w-])", command))

    missing = [test for test in expected_tests if not full_suite and not selected(test)]
    if missing:
        return "incomplete", f"{MISSING_TESTS}: " + ", ".join(missing)

    if peers:
        peer_files = [name for peer in peers for name in (peer.get("likely_files") or [])]
        peer_tests = {test for peer in peers for test in (peer.get("tests") or [])}
        own_files = [f for f in matched if not any(matches(f, name) for name in peer_files)]
        own_test = any(test not in peer_tests and selected(test) for test in expected_tests)
        if not own_files and not own_test:
            return "incomplete", "The batch produced shared evidence only. Verify this task separately."
        if own_files:
            matched = own_files

    evidence = "Configured tests passed" if full_suite else "Selected tests passed" if command else "Build passed"
    if matched:
        evidence += " · Changed " + ", ".join(matched)
    if command and not full_suite and expected_tests:
        evidence += " · " + ", ".join(expected_tests)
    return "passed", evidence


def confirm_with_project_tests(task_job: dict, summary_file: Path, output: dict) -> tuple[bool, bool, dict]:
    """The AI's test command left out planned tests: run the project's own test command over the same work instead of
    asking anyone. Returns the new build and test results, and the output describing what ran."""
    print("\n      - The AI's test command left out some of this task's planned tests. Running the project's own tests to confirm it...",
          flush=True)
    check = {**task_job, "test_command_override": None}
    build_ok, test_ok = run_build_and_tests(check, summary_file)
    return build_ok, test_ok, {**output, "_test_execution": check.get("_test_execution") or {}}


def note_unconfirmed(job: dict, outcomes: dict[int, tuple[str, str]]) -> None:
    """Marks the job when its build and tests passed but tasks still lack evidence (nothing failed, so nothing to fix)."""
    open_tasks = {index: detail for index, (status, detail) in outcomes.items() if status != "passed"}
    if not open_tasks or any(status == "failed" for status, _ in outcomes.values()):
        return
    numbers = [str(index + 1) for index in sorted(open_tasks)]
    detail = " ".join(dict.fromkeys(open_tasks.values()))
    job["unverified_task"] = {"label": ("Task " if len(numbers) == 1 else "Tasks ") + ", ".join(numbers),
                              "detail": detail, "error": job.get("last_error")}
    if PROJECT_TESTS_FAILED in detail:
        job["unverified_task"]["project_tests_failed"] = True


def pause_unconfirmed(job_path: Path, job: dict, issue_number: Optional[int]) -> None:
    note = job["unverified_task"]
    unconfirmed = f"{note['label']} {'are' if note['label'].startswith('Tasks') else 'is'} not confirmed"
    failed = bool(note.get("project_tests_failed"))
    said = f"{unconfirmed}: the project's own tests failed." if failed else f"The build and tests passed, but {unconfirmed}."
    why = "No fix attempt is started on a failure that may not be this job's." if failed else "Nothing failed, so no fix attempt is started."
    print(f"\n⏸️  {said} {note['detail']}\n      - {why} The changes are kept; the job waits for you.")
    history = job.get("debug_history")
    if history and history[-1].get("result") == "pending":
        history[-1]["result"] = {"build_ok": True, "tests_ok": not failed, "notes": f"{said} {note['detail']}"}
    job.update(status="human-needed", worker_pid=None, updated_at=now_iso())
    write_json(job_path, job)
    update_issue_status(issue_number, "status:human-needed", ["status:executing"])
    send_notifications(job, "Job Paused: a task isn't confirmed", f"{said}\nTitle: {job['title']}")


def report_task_attempt(job: dict, task: dict, status: str, detail: str) -> None:
    if task.get("run_history_id"):
        print(job_runs.task_attempt_marker(job["job_id"], task, status, detail), flush=True)


def execute_job(job_path: Path, resume: bool = False) -> None:
    start_epoch = time.time()
    remove_task_views(job_path)  # left over from a run that was stopped part way
    job = read_json(job_path)
    if job.get("moved_to"):  # one owner per job: it was continued on another computer
        print(f"\nThis job moved to {job['moved_to']} and is worked on there now. Nothing was run here.")
        return
    if isinstance(job.get("blocker"), dict):  # running again: what it waited on is cleared (or the person chose to try)
        job["last_blocker"] = job.pop("blocker")  # kept so a blocker that comes straight back isn't auto-resumed again
        write_json(job_path, job)

    if resume and job.get("worker_pid"):
        old_pid = job["worker_pid"]
        if old_pid != os.getpid() and is_process_running(old_pid):
            print(f"      - Worker already running (PID: {old_pid}). Tailing logs...")
            try:
                # Tail the runtime log path so resume works outside the repo-root ai/ directory too.
                subprocess.run(["tail", "-f", str(LOGS_DIR / "batch_test_results.log")])
            except KeyboardInterrupt:
                print("\n      - Detached from logs.")
            return

    from orchestrator.job_scope import NONPRODUCTION_TYPES
    if job.get("type") in NONPRODUCTION_TYPES:
        from nonproduction_job import execute_nonproduction_job
        execute_nonproduction_job(job_path)
        return

    # Task 1: Update Job Schema and PID Tracking
    job["worker_pid"] = os.getpid()
    job["worker_host"] = socket.gethostname()  # a process id only means something on the machine that wrote it
    job["updated_at"] = now_iso()
    job.pop("last_error", None)  # stale error from a prior run shouldn't mask this run's outcome
    job.pop("unverified_task", None)
    if "completed_task_indices" not in job:
        job["completed_task_indices"] = []
    if job.pop("task_growth", None):
        print("      - Continuing past the task that grew beyond its plan.")  # running this again is the person saying keep going
    write_json(job_path, job)

    status_bar = StatusBar(job, is_processing=True)

    print_phase("execution")
    if resume:
        print("      - RESUME MODE ENABLED")
    status_bar.render()

    issue_number = job.get("issue_number")
    job_label = f"Issue #{issue_number}" if issue_number else f"job {job['job_id']}"
    job["code_host"] = PROJECT_CONFIG.code_host_mode

    from orchestrator import run_check
    if run_check.switches_branches(job) and run_check.runs_from(ROOT):
        # Switching branches here would replace the code this worker is running (and the web app's files).
        print(f"\n❌ {run_check.SELF_CHECKOUT_TEXT}")
        job["status"] = "human-needed"
        job["last_error"] = "This project is the folder Orchestrator runs from; jobs here can't switch branches."
        job["worker_pid"] = None
        job["updated_at"] = now_iso()
        write_json(job_path, job)
        return

    try:
        require_build_space()
        if missing_tool := missing_build_tool_blocker(job):
            pause_on_blocker(job_path, job, missing_tool, issue_number)
            return
        print_phase("git_prep")
        status_bar.render()
        if other := other_job_in_folder(job_path):
            # Another open job's work is uncommitted here: going on would commit it as this job's, or check it out of existence.
            pause_on_blocker(job_path, job, other, issue_number)
            return
        if resume and isinstance(job.get("passed_uncommitted"), dict):
            # Only the commit is left, and the finished work is uncommitted in this checkout: preparing the branch (a forced
            # checkout and pull) would throw it away. Stay where it is, on the job's branch.
            branch, base_branch = commit_pending_branch(job)
        else:
            branch, base_branch = prepare_git_branch(job, issue_number)

        print_phase("status_update")
        status_bar.render()
        is_debug = job.get("status") == "debugging"

        job["branch"] = branch
        job["base_branch"] = base_branch

        if not is_debug:
            job["status"] = "executing"

        job["updated_at"] = now_iso()
        write_json(job_path, job)

        update_issue_status(
            issue_number,
            "status:executing",
            ["status:planned", "status:fix-requested", "status:debugging"]
        )
        
        # Add comment about worker start
        machine_name = os.environ.get("MACHINE_NAME", "local")
        start_msg = f"🚀 **Worker Started** on `{machine_name}`\n\n- **Job ID**: `{job['job_id']}`\n- **Branch**: `{branch}`"
        gh_comment(issue_number, start_msg)
        try:
            from orchestrator import analytics
            all_jobs_count = len(list(job_path.parent.glob("*.json")))
            analytics.track_job_run(
                job_id=job.get("job_id", ""),
                job_type=job.get("type", "feature-plan"),
                status="started",
                planner_model=job.get("planner"),
                builder_model=job.get("builder"),
                reviewer_model=job.get("reviewer"),
                job_seq=max(1, all_jobs_count),
                runtime_root=job_path.parent.parent,
            )
        except Exception:
            pass

        # Clarification and an already completed task list need these before any builder call.
        is_yolo = refresh_yolo(job, job_path)
        task_output = {}
        # Capture state before builder
        pre_state = get_repo_state()
        pre_hashes = dirty_file_hashes(ROOT)
        pre_config = project_config_hash(ROOT)
        touched_this_run: set[str] = set()  # files the plan's tasks changed in this run, committed or not
        builder_session_count = None
        # Done and tested last time, only the commit failed (a git lock): commit it now instead of building again, whether
        # the job is one task or a plan of several.
        committing_only = bool(resume and isinstance(job.get("passed_uncommitted"), dict))
        if committing_only:
            print("      - The work was done and its tests passed last time; only saving it to git failed. Committing it now...")
            build_ok, test_ok = True, True

        if committing_only:
            pass
        elif job.get("type") == "feature-plan" and job.get("approved"):
            tasks = job.get("plan", {}).get("tasks", [])
            job_runs.ensure_task_ids([t for t in tasks if isinstance(t, dict)])
            job["run_breakdown"] = job_runs.breakdown(job)
            write_json(job_path, job)
            completed = job.get("completed_task_indices", [])
            all_ok = True
            batch_indices = [] if job.get("retry_tasks_individually") else can_batch_cohesive_tasks(tasks, completed)
            for i, task in enumerate(tasks):
                if i in completed:
                    print(f"      - Task {i+1} already completed. Skipping.")
                    continue

                parallel_indices = (
                    []
                    if (batch_indices or job.get("retry_tasks_individually"))
                    else ready_parallel_wave_indices(job, tasks, completed, i)
                )
                if len(parallel_indices) >= 2 and i == parallel_indices[0]:
                    wave_ok, wave_touched, wave_paused = execute_parallel_task_wave(
                        job_path, job, tasks, parallel_indices, issue_number, job_label, pre_state, pre_hashes, is_yolo
                    )
                    touched_this_run.update(wave_touched)
                    job["run_breakdown"] = job_runs.breakdown(job)
                    write_json(job_path, job)
                    if wave_paused:
                        return
                    if not wave_ok:
                        all_ok = False
                        break
                    continue

                is_batched = bool(batch_indices and i == batch_indices[0])
                active_indices = batch_indices if is_batched else [i]
                active_task = merge_batched_tasks(tasks, batch_indices) if is_batched else task
                if is_batched:
                    print(
                        f"      - Cohesive task batching: executing tasks {', '.join(str(idx + 1) for idx in batch_indices)} "
                        f"in a single builder pass (overlapping files: {', '.join(active_task['likely_files'])}).",
                        flush=True,
                    )
                    print_phase(f"sub-task {batch_indices[0]+1}-{batch_indices[-1]+1}/{len(tasks)}", subtext=active_task["title"])
                else:
                    print_phase(f"sub-task {i+1}/{len(tasks)}", subtext=task['title'])
                
                # Create a temporary, task-specific job object for the builder
                task_job = copy.deepcopy(job)
                task_job['job_id'] = f"{job['job_id']}_task_{i+1}" # Unique ID per task
                task_job['title'] = f"{job['title']} (Task {i+1}: {active_task['title']})"
                task_builder = builder_for_task(active_task, job)
                if task_builder:
                    task_job['builder'] = task_builder
                if task_builder and task_builder != job.get("builder"):
                    print(f"      - Routing this small task to {task_builder} (lower-cost coding model).", flush=True)
                
                # CLEAN SLATE: Don't inherit parent errors or history that might distract the worker
                task_job['last_error'] = None
                task_job['debug_history'] = []
                task_job['ai_modified_files'] = []
                task_job['ai_untracked_files'] = []
                task_job['iteration'] = 0
                
                task_job['plan']['summary'] = active_task['description'] # Use task description as summary
                task_job['plan']['acceptance_criteria'] = active_task['acceptance_criteria']
                task_job['plan']['likely_files'] = active_task['likely_files']
                if job.get("retry_tasks_individually"):
                    planned_tests = ", ".join(active_task.get("tests") or [])
                    task_job['plan']['summary'] += (
                        "\nThe last batch left this task without enough evidence. Inspect the existing change, "
                        "finish any missing work and add or run checks specific to this task. Preserve completed tasks."
                    )
                    if planned_tests:
                        task_job['plan']['summary'] += f"\nPlanned tests: {planned_tests}"
                # Only this task's (or batch's) test cases (plus untasked ones); `task` is 1-based.
                due_task_nums = {idx + 1 for idx in active_indices} | {None}
                task_job['plan']['test_cases'] = [
                    c for c in job.get('plan', {}).get('test_cases') or []
                    if isinstance(c, dict) and c.get('task') in due_task_nums
                ]
                
                # Write this temp view to a temporary path to pass to run_builder
                temp_job_path = job_path.parent / f"{job_path.stem}_task_{i+1}.json"
                write_json(temp_job_path, task_job)
                link_run_to_tasks(job_path, job, tasks, active_indices)
                for idx in active_indices:
                    report_task_attempt(job, tasks[idx], "running", "")
                task_pre_hashes = dirty_file_hashes(ROOT)

                
                while True:
                    try:
                        # In YOLO mode sub-tasks, we disable the 'resume' forensics because it often 
                        # hits false positives from previous tasks.
                        build_ok, test_ok, _summary_path, task_output = unpack_builder_result(run_builder(temp_job_path, resume=False))
                        break
                    except BuilderClarificationNeeded as clarification:
                        if can_autorespond(job):  # YOLO: nobody is waiting to answer, so answer for them and keep going
                            answer = autorespond(job, clarification.question, "builder")
                            record_clarification(task_job, clarification.question, answer)
                            task_job["clarification_history"][-1].update(auto=True, asker="builder")
                            job["status"] = task_job["status"] = "executing"
                            write_json(job_path, job)
                            write_json(temp_job_path, task_job)
                            continue
                        mark_human_needed(job_path, job, clarification.question, task_index=i)
                        update_issue_status(issue_number, "status:human-needed", ["status:executing"])
                        if interactive() and not job.get("headless", False) and not is_yolo:
                            print_clarification_report(job, question=clarification.question)
                            print("\033[97m💡 Answer now to resume the builder immediately, or press Enter to pause.\033[0m")
                            try:
                                user_ans = input("\033[1;96mYour Answer (or Enter to pause):\033[0m ").strip()
                            except (EOFError, KeyboardInterrupt):
                                user_ans = ""
                                print(f"\n\033[90m  💾 Progress saved: Job paused at Task {i+1} ({task.get('name', f'Task {i+1}')}).\033[0m")
                                print(f"\033[90m  💡 Resume anytime by running: orchestrator console\033[0m\n")
                            if user_ans:
                                record_clarification(job, clarification.question, user_ans)
                                record_clarification(task_job, clarification.question, user_ans)
                                write_json(job_path, job)
                                write_json(temp_job_path, task_job)
                                print(f"\n      - Resuming Task {i+1} with your clarification...", flush=True)
                                continue
                        for idx in active_indices:
                            report_task_attempt(job, tasks[idx], "paused", "Waiting for an answer.")
                        send_notifications(
                            job,
                            "Job Paused: Clarification Needed",
                            f"Builder needs clarification for {job_label}.\nQuestion: {clarification.question}\nTitle: {job['title']}"
                        )
                        return
                    except DiskFullError:
                        for idx in active_indices:
                            report_task_attempt(job, tasks[idx], "paused", "The machine ran out of disk space.")
                        raise
                    except Exception as e:
                        for idx in active_indices:
                            report_task_attempt(job, tasks[idx], "failed", "The worker stopped. See the run log.")
                        # SYSTEM/LLM ERROR: Do not trigger automated debugging
                        print(f"\n❌ Automation error during Task {i+1}: {e}")
                        err_msg = str(e)
                        updated = now_iso()
                        job["last_error"] = err_msg
                        job["status"] = "human-needed"
                        job["worker_pid"] = None
                        job["updated_at"] = updated
                        write_json(job_path, job)
                        task_job["last_error"] = err_msg
                        task_job["status"] = "human-needed"
                        task_job["updated_at"] = updated
                        write_json(temp_job_path, task_job)
                        return
                
                # IMPORTANT: Only mark task done if code was actually changed (patch)
                # and validation passed. If AI only 'investigated', it's not done yet.
                # VERIFY: Did the AI actually touch the files it said it would?
                # This prevents "phantom completion" where the AI runs old passing tests 
                # but doesn't implement the current task.
                task_post_state = get_repo_state()
                task_modified = [f for f in task_post_state["modified"] if f not in pre_state["modified"] and not builder_check.is_ignored_tool_path(f)]
                task_untracked = [f for f in task_post_state["untracked"] if f not in pre_state["untracked"] and not builder_check.is_ignored_tool_path(f)]
                task_post_hashes = dirty_file_hashes(ROOT)
                changed_paths = files_changed_between(task_pre_hashes, task_post_hashes)
                # Leave pre-existing user changes out of job commits.
                files_touched = sorted(
                    {f for f in changed_paths if f not in pre_hashes and not builder_check.is_ignored_tool_path(f)}
                    if task_pre_hashes or task_post_hashes else set(task_modified + task_untracked)
                )
                touched_this_run.update(files_touched)

                # run_builder records which model it used on the per-task copy; keep that on the real job.
                new_sessions = []
                try:
                    built = read_json(temp_job_path)
                    if built.get("actual_builder_used"):
                        job["actual_builder_used"] = built["actual_builder_used"]
                    job["reuse_suggested"] = reuse_brief.merge(job.get("reuse_suggested"), built.get("reuse_suggested"))
                    new_sessions = (built.get("llm_sessions") or [])[len(job.get("llm_sessions") or []):]
                    if new_sessions:
                        job["llm_sessions"] = (job.get("llm_sessions") or []) + new_sessions
                except Exception:
                    pass

                outcomes = {
                    idx: task_validation(tasks[idx], [tasks[other] for other in active_indices if other != idx],
                                         files_touched, task_output, build_ok, test_ok)
                    for idx in active_indices
                }
                if (not is_batched and PROJECT_CONFIG.test_command and (task_output or {}).get("_test_execution")
                        and outcomes[i][1].startswith(MISSING_TESTS)):
                    confirmed_build, confirmed_tests, confirmed_output = confirm_with_project_tests(task_job, _summary_path, task_output)
                    if confirmed_build and confirmed_tests:
                        task_output = confirmed_output
                        outcomes = {i: task_validation(tasks[i], [], files_touched, task_output, build_ok, test_ok)}
                    else:  # it may have been failing before this job: show it and wait, don't set the AI on it
                        outcomes = {i: ("incomplete", f"{outcomes[i][1]}. {PROJECT_TESTS_FAILED}")}
                passed = [idx for idx, (status, _) in outcomes.items() if status == "passed"]

                if is_batched and build_ok and test_ok and len(passed) < len(active_indices):
                    # Preserve the combined patch, including any work still being verified. A partial
                    # batch has no independently revertible task checkpoint.
                    if files_touched:
                        commit_job_files(files_touched, f"feat: {job['title']} (partial task batch)")
                    job["completed_task_indices"].extend(passed)
                    job["retry_tasks_individually"] = True
                    job["ai_modified_files"] = sorted(set(job.get("ai_modified_files") or []) | set(files_touched))
                    pending = ", ".join(str(idx + 1) for idx in active_indices if idx not in passed)
                    job.update(status="human-needed", worker_pid=None, updated_at=now_iso(),
                               last_error=f"Task evidence is incomplete for {pending}. Resume to check the remaining work individually.")
                    note_unconfirmed(job, outcomes)
                    write_json(job_path, job)
                    print(f"      - {job['last_error']}")
                    for idx, (status, detail) in outcomes.items():
                        report_task_attempt(job, tasks[idx], status, detail)
                    if files_touched:
                        backup_push(job)
                    remove_task_views(job_path)
                    update_issue_status(issue_number, "status:human-needed", ["status:executing"])
                    return

                if len(passed) == len(active_indices):
                    job["completed_task_indices"].extend(passed)
                    checkpoint_task(job, i, len(tasks), active_task['title'], files_touched, batch_indices=active_indices if is_batched else None)
                    if new_sessions and files_touched:
                        model_outcomes.record(job, "builder", resolve_builder_model(task_job), new_sessions[-1].get("model"),
                                              "build_checks_passed", tasks=[idx + 1 for idx in active_indices])
                    write_json(job_path, job)
                    for idx, (status, detail) in outcomes.items():
                        report_task_attempt(job, tasks[idx], status, detail)

                    if is_batched:
                        break

                    # A task that has outgrown its plan is where a small job starts to grow: stop and ask before building
                    # more on top. (After the last task there is nothing left to stop; the review's scope check covers it.)
                    growth = check_task_growth(job, i, task) if i < len(tasks) - 1 else None
                    if growth:
                        said = task_growth.sentence(i, growth["reasons"])
                        print(f"\n      - ⚠️  {said} Pausing before Task {i+2}.")
                        job["task_growth"] = {"task": i, "text": said, "added": growth["added"], "files": growth["files"]}
                        job["status"] = "human-needed"
                        job["worker_pid"] = None
                        job["updated_at"] = now_iso()
                        write_json(job_path, job)
                        remove_task_views(job_path)
                        update_issue_status(issue_number, "status:human-needed", ["status:executing"])
                        send_notifications(job, "Job Paused: a task grew past its plan", f"{said}\nTitle: {job['title']}")
                        return
                elif build_ok and test_ok:
                    if not files_touched:
                        if task_output and isinstance(task_output, dict) and task_output.get("summary"):
                            job["builder_summary"] = task_output["summary"]
                        overlap_q = overlapping_completed_job_question(job, job_path, active_task)
                        if overlap_q:
                            mark_human_needed(job_path, job, overlap_q, task_index=i)
                            job["last_error"] = f"Task {i+1} paused for clarification: overlapping completed work detected."
                            write_json(job_path, job)
                            report_task_attempt(job, task, "paused", "Waiting for clarification on overlapping completed work.")
                            remove_task_views(job_path)
                            update_issue_status(issue_number, "status:human-needed", ["status:executing"])
                            send_notifications(job, "Job Paused: Clarification Needed", f"{overlap_q}\nTitle: {job['title']}")
                            return
                    model_tag = f" ({job['actual_builder_used']})" if job.get("actual_builder_used") else ""
                    note_raw = " ".join(str((task_output or {}).get("summary") or (task_output or {}).get("risks") or "").split())
                    note_tag = f' — "{note_raw[:160]}"' if note_raw else ""
                    job["status"] = "human-needed"
                    job["last_error"] = f"Task {i+1}{model_tag}: {outcomes[i][1]}{note_tag}"
                    print(f"      - {job['last_error']}")
                    note_unconfirmed(job, outcomes)
                    write_json(job_path, job)
                    report_task_attempt(job, task, *outcomes[i])
                    all_ok = False
                    break
                else:
                    for idx, (status, detail) in outcomes.items():
                        report_task_attempt(job, tasks[idx], status, detail)
                    print(f"!!! Sub-task {i+1} failed validation. Halting feature implementation.")
                    all_ok = False
                    break # Exit the loop on first failure
            
            remove_task_views(job_path)
            if all_ok:
                job.pop("retry_tasks_individually", None)
            build_ok = all_ok
            test_ok = all_ok
        else:
            # Original single-task execution
            builder_session_count = len(job.get("llm_sessions") or [])
            if job.get("type") == "quick-fix":
                quick_tasks = (job.get("plan") or {}).get("tasks") or []
                link_run_to_tasks(job_path, job, quick_tasks, list(range(len(quick_tasks))))
            while True:
                try:
                    build_ok, test_ok, _summary_path, task_output = unpack_builder_result(run_builder(job_path, resume=resume))
                    break
                except BuilderClarificationNeeded as clarification:
                    if can_autorespond(job):  # YOLO: nobody is waiting to answer, so answer for them and keep going
                        job = read_json(job_path)  # the builder marked it as waiting on a question
                        autorespond(job, clarification.question, "builder")
                        job["status"] = "executing"
                        write_json(job_path, job)
                        resume = False
                        continue
                    mark_human_needed(job_path, job, clarification.question)
                    update_issue_status(issue_number, "status:human-needed", ["status:executing"])
                    if interactive() and not job.get("headless", False) and not is_yolo:
                        print_clarification_report(job, question=clarification.question)
                        print("\033[97m💡 Answer now to resume the builder immediately, or press Enter to pause.\033[0m")
                        try:
                            user_ans = input("\033[1;96mYour Answer (or Enter to pause):\033[0m ").strip()
                        except (EOFError, KeyboardInterrupt):
                            user_ans = ""
                            print(f"\n\033[90m  💾 Progress saved: Job paused in 'human-needed' state.\033[0m")
                            print(f"\033[90m  💡 Resume anytime by running: orchestrator console\033[0m\n")
                        if user_ans:
                            record_clarification(job, clarification.question, user_ans)
                            write_json(job_path, job)
                            print(f"\n      - Resuming builder with your clarification...", flush=True)
                            resume = False
                            continue
                    send_notifications(
                        job,
                        "Job Paused: Clarification Needed",
                        f"Builder needs clarification for {job_label}.\nQuestion: {clarification.question}\nTitle: {job['title']}"
                    )
                    return

        # Capture state after builder
        post_state = get_repo_state()

        # Identify files changed/added by AI (never sweep .claude/, .orchestrator/, .worktrees/ tool state into commits).
        # Also include files already recorded as belonging to this job from an earlier attempt, still uncommitted, so commits
        # don't reference missing work (e.g. a .csproj referencing a previously created source file).
        prior_job_untracked = set(job.get("ai_untracked_files") or [])
        carried = {f for f in set(job.get("ai_modified_files") or []) & set(pre_state["modified"])}
        ai_modified = [f for f in post_state["modified"]
                       if (f not in pre_state["modified"] or f in carried) and not builder_check.is_ignored_tool_path(f)]
        ai_untracked = [
            f for f in post_state["untracked"]
            if (f not in pre_state["untracked"] or f in prior_job_untracked) and not builder_check.is_ignored_tool_path(f)
        ]
        if committing_only:  # the files the passing run changed, if still uncommitted
            saved = set(job["passed_uncommitted"].get("files") or [])
            ai_modified = [f for f in post_state["modified"] if f in saved and not builder_check.is_ignored_tool_path(f)]
            ai_untracked = [f for f in post_state["untracked"] if f in saved and not builder_check.is_ignored_tool_path(f)]

        # Re-read job to get updates from run_builder (like test_command_override)
        job = read_json(job_path)

        if task_output and isinstance(task_output, dict):
            if task_output.get("summary"):
                job["builder_summary"] = task_output["summary"]
            if task_output.get("hypothesis"):
                job["builder_hypothesis"] = task_output["hypothesis"]

        # Files already committed task by task (checkpoint_task) are still this job's changes.
        job["ai_modified_files"] = sorted(set(job.get("ai_modified_files") or []) | set(ai_modified))
        job["ai_untracked_files"] = sorted(set(job.get("ai_untracked_files") or []) | set(ai_untracked))
        write_json(job_path, job)

        # A build that changed nothing hasn't built anything, whatever its tests say: stop here rather than verify untouched code
        # or start a fix loop that can't help (the usual cause is the AI's tool refusing file edits, which a fix attempt hits too).
        changed_now = sorted(
            f for f in ((set(ai_modified) - carried) | set(ai_untracked) | touched_this_run  # carried work isn't this run's change
                        | set(files_changed_between(pre_hashes, dirty_file_hashes(ROOT))))
            if not builder_check.is_ignored_tool_path(f)
        )
        # The project's own config (a test command) lives in ignored tool state but is a real change when the builder edits it.
        if project_config_hash(ROOT) != pre_config:
            changed_now = sorted({*changed_now, PROJECT_CONFIG_FILE})
        # The task loop may already have stopped the job with a more specific reason (phantom completion, unrelated tests).
        already_stopped = job.get("type") == "feature-plan" and job.get("status") == "human-needed"
        if not already_stopped and not changed_now and job.get("type") not in NO_CHANGE_EXEMPT_TYPES:
            overlap_q = overlapping_completed_job_question(job, job_path)
            if overlap_q:
                print(f"\n⏸️  {overlap_q}")
                mark_human_needed(job_path, job, overlap_q)
                job["last_error"] = "Paused for clarification: another completed job or existing commit may already cover this change."
                job["worker_pid"] = None
                job["updated_at"] = now_iso()
                write_json(job_path, job)
                update_issue_status(issue_number, "status:human-needed", ["status:executing"])
                send_notifications(job, "Job Paused: Clarification Needed", f"{overlap_q}\nTitle: {job['title']}")
                return
        stop = "" if already_stopped else no_change_stop(job, changed_now, debug_attempt=os.environ.get("AI_DEBUG_CHILD") == "1")
        if stop:
            print(f"\n⚠️ {stop}")
            job["build_no_changes"] = True
            job["status"] = "human-needed"
            job["last_error"] = stop
            job["worker_pid"] = None
            job["updated_at"] = now_iso()
            if job.get("debug_history") and job["debug_history"][-1].get("result") == "pending":
                job["debug_history"][-1]["result"] = {"build_ok": build_ok, "tests_ok": test_ok, "notes": "The fix attempt changed no files"}
            write_json(job_path, job)
            update_issue_status(issue_number, "status:human-needed", ["status:executing"])
            send_notifications(job, "Job Paused: nothing was built", f"{stop}\nTitle: {job['title']}")
            return
        if changed_now:
            job.pop("build_no_changes", None)
            if builder_session_count is not None and build_ok and test_ok:
                sessions = job.get("llm_sessions") or []
                if len(sessions) > builder_session_count:
                    model_outcomes.record(job, "builder", resolve_builder_model(job), sessions[-1].get("model"),
                                          "build_checks_passed")
            # A Quick change builds its whole fast-tracked plan in one pass, so a passing pass completes every task.
            quick_tasks = (job.get("plan") or {}).get("tasks") or [] if job.get("type") == "quick-fix" else []
            if quick_tasks and build_ok and test_ok:
                job["completed_task_indices"] = list(range(len(quick_tasks)))
            write_json(job_path, job)

        if job.get("unverified_task"):
            pause_unconfirmed(job_path, job, issue_number)
            return

        # Commit changes if any
        if build_ok and test_ok and (ai_modified or ai_untracked):
            if (ROOT / ".git").exists():
                try:
                    import test_cases as tc
                    for tc_path in tc.sync_job_cases(ROOT, job):
                        try:
                            rel_tc = tc_path.relative_to(ROOT).as_posix()
                            if rel_tc not in ai_modified and rel_tc not in ai_untracked:
                                ai_untracked.append(rel_tc)
                        except ValueError:
                            pass
                except Exception:
                    pass
            # Say it passed before committing: if the commit fails, the job shows "Tests passed, not committed" and Resume
            # commits it, rather than "debug failing tests" from the previous attempt.
            job["passed_uncommitted"] = {"files": sorted(set(ai_modified) | set(ai_untracked)), "at": now_iso()}
            if job.get("debug_history") and job["debug_history"][-1].get("result") == "pending":
                job["debug_history"][-1]["result"] = {"build_ok": True, "tests_ok": True, "notes": "Tests passed"}
            write_json(job_path, job)
        if build_ok and (ai_modified or ai_untracked):
            print(f"      - Committing {len(ai_modified)} modified and {len(ai_untracked)} untracked files...")
            commit_msg = f"feat: {job['title']} (AI generated)"
            if job.get("type") == "bug-fix":
                commit_msg = f"fix: {job['title']} (AI generated)"
            
            commit_job_files(ai_modified + ai_untracked, commit_msg)
            backup_push(job)
        elif ai_modified or ai_untracked:
            print(f"      - Build did not pass; keeping {len(ai_modified)} modified and {len(ai_untracked)} untracked file(s) uncommitted for the next fix attempt.")
        if job.pop("passed_uncommitted", None) is not None:
            write_json(job_path, job)

        tasks = job.get("plan", {}).get("tasks", [])
        completed = job.get("completed_task_indices", [])
        has_remaining_feature_tasks = (
            job.get("type") == "feature-plan"
            and bool(tasks)
            and len(completed) < len(tasks)
        )

        if has_remaining_feature_tasks and build_ok and test_ok:
            job["status"] = "executing" if job.get("is_yolo") else "review-needed"
            job["updated_at"] = now_iso()
            write_json(job_path, job)
            if job.get("is_yolo"):
                print(
                    f"\n\033[1;93m🚀 YOLO MODE: {len(completed)}/{len(tasks)} tasks complete; continuing automatically...\033[0m"
                )
                time.sleep(2)
                return execute_job(job_path, resume=True)

        write_json(job_path, job)

        if not build_ok or not test_ok:
            blocker = blocker_in_logs(job)
            if blocker:  # the machine, not the code: normal jobs pause; YOLO tries one fix attempt before pausing
                yolo_fix_attempts = int(job.get("yolo_blocker_fix_attempts") or 0)
                if (job.get("yolo") or job.get("is_yolo")) and yolo_fix_attempts < 1 and os.environ.get("AI_DEBUG_CHILD") != "1":
                    job["yolo_blocker_fix_attempts"] = yolo_fix_attempts + 1
                    job["debug_feedback"] = f"Blocker reached during build/tests: {blocker.get('title', 'Blocker')} — {blocker.get('detail', '')}".strip()
                    print(f"\n🤖 YOLO MODE: Blocker detected ({blocker.get('title', blocker.get('kind'))}); attempting an automated fix before pausing...")
                else:
                    pause_on_blocker(job_path, job, blocker, issue_number)
                    return
            # Set job status to debugging to trigger an automatic iteration (debug_job owns incrementing `iteration`)
            job["status"] = "debugging"
            job["debug_phase"] = "propose"
            job.setdefault("iteration", 0)
            if "max_iterations" not in job:
                job["max_iterations"] = 8
            
            # Record failure in history if this was a debug iteration
            if job.get("debug_history"):
                last = job["debug_history"][-1]
                if last.get("result") == "pending":
                    last["result"] = {
                        "build_ok": build_ok,
                        "tests_ok": test_ok,
                        "notes": "Validation failed during worker run"
                    }
                
            job["updated_at"] = now_iso()
            write_json(job_path, job)

            if os.environ.get("AI_DEBUG_CHILD") == "1":
                print("\n⚠️ The tests still fail after this fix attempt. The job stays open on its branch and nothing is lost. "
                      "It does not retry by itself: press Run another fix (or open the job and describe what you saw) to try again.")
                return

            # Trigger debug_job.py automatically
            print("\n⚠️ Tests failed. Triggering automated debug iteration...")
            from debug_job import run_debug_iteration
            run_debug_iteration(job_path)

            return

        # SUCCESS POINT
        job.pop("last_error", None)  # built and tested: an error from an earlier attempt no longer describes this job
        likely_files = job.get("plan", {}).get("likely_files", [])
        is_ios = any(f.endswith(".swift") or f.endswith(".storyboard") or f.endswith(".plist") for f in likely_files)

        is_yolo = job.get("yolo", False)
        tasks = job.get("plan", {}).get("tasks", [])
        completed = job.get("completed_task_indices", [])
        all_tasks_done = len(completed) >= len(tasks)

        distributed_status = "Not Triggered"
        # iOS delivers only for jobs that touched iOS files; Android and web projects deliver every verified job.
        delivers_here = is_ios or getattr(PROJECT_CONFIG, "delivery_platform", "ios") in ("android", "web", "file")
        if PROJECT_CONFIG.firebase_distribution and delivers_here and (not is_yolo or all_tasks_done):
            print("\n🚀 Automated build delivery triggered after verification...")
            try:
                res = subprocess.run([sys.executable, str(SCRIPTS_DIR / "deliver_build.py"), str(job_path)], cwd=str(ROOT))
                if res.returncode == 0:
                    formatted_time = now_iso().replace("T", " ")[:19]
                    platform = getattr(PROJECT_CONFIG, "delivery_platform", "ios")
                    if platform in ("android", "web", "file"):
                        distributed_status = f"SUCCESS via Firebase ({platform}) at {formatted_time}"
                    else:
                        method = PROJECT_CONFIG.delivery_method or "ad-hoc"
                        distributed_status = f"SUCCESS via Firebase App Distribution ({method}) at {formatted_time}"
                else:
                    distributed_status = f"FAILED (exit code {res.returncode})"
            except Exception as e:
                print(f"\n⚠️ Build automated delivery failed: {e}")
                distributed_status = f"FAILED ({e})"
        else:
            if not PROJECT_CONFIG.firebase_distribution:
                distributed_status = "Skipped (Firebase distribution disabled)"
            elif not is_ios:
                distributed_status = "Skipped (Non-iOS task)"
            else:
                distributed_status = "Skipped (YOLO mode - awaiting final task)"

        print_phase("pull_request")
        status_bar.render()
        print(f"[4/4] Implementation successful. Opening/updating Pull Request...")
        base = job.get("base_branch") or PROJECT_CONFIG.base_branch
        if job.get("branch") == base:
            # The job worked on the base branch itself ("Use the current branch"): there's no branch to review or merge,
            # and pushing would publish unreviewed work on the base branch.
            print(f"      - The changes are committed on {base} on this computer. Nothing was pushed: push {base} when you're ready.")
            pr_number, pr_url = None, None
        elif PROJECT_CONFIG.uses_github:
            pr_number, pr_url = open_or_update_pr(job_path, publish_result=job)
        else:
            print("      - Plain git (no GitHub): pushing the branch for review instead of opening a pull request.")
            pr_number, pr_url = None, None
            job["merge_request_url"] = push_branch_for_review(job)
        if pr_url:
            try:
                from orchestrator.integrations_sync import notify_job_event
                notify_job_event(job, "pr_opened", CONFIG_DIR / "settings.json", job_path, pr_url=pr_url, pr_number=pr_number)
            except Exception as exc:  # linked-app updates are best effort
                print(f"      - Note: couldn't update linked apps: {exc}")

        if is_debug:
            # Update history with success
            history = job.get("debug_history")
            if history:
                history[-1]["result"] = {
                    "build_ok": True,
                    "tests_ok": True,
                    "notes": "Implementation succeeded and PR updated"
                }
            job["debug_phase"] = "propose" # Ready for next iteration or verification

            # Add a commit with iteration info
            iter_num = job["iteration"]
            prop = job.get("debug_proposal", {})
            commit_msg = f"debug(iter {iter_num}): {prop.get('action', 'update')}"
            run_shell(f"git commit --allow-empty -m '{commit_msg}'", cwd=ROOT, check=False)
            run_shell(f"git push origin {branch}", cwd=ROOT, check=False)

        job["pr_number"] = pr_number
        job["pr_url"] = pr_url
        if not is_debug:
            job["status"] = "review-needed"
        try:
            from orchestrator import experience_loop
            head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
        except Exception:
            head_sha = ""
        try:
            from orchestrator import experience_loop
            experience_loop.record_first_experience(job, commit_sha=head_sha, now_iso=now_iso())
            if head_sha and isinstance(job.get("experience_versions"), list) and job["experience_versions"]:
                if not job["experience_versions"][-1].get("commit"):
                    job["experience_versions"][-1]["commit"] = head_sha
            print("[experience] " + json.dumps({
                "version": job.get("iteration_version", "0.1"),
                "stage": f"Preview ready (v{job.get('iteration_version', '0.1')})",
            }), flush=True)
        except Exception:
            pass

        job["updated_at"] = now_iso()
        write_json(job_path, job)

        if not is_debug:
            update_issue_status(
                issue_number,
                "status:review-needed",
                ["status:executing"]
            )
            
            # Post success comment
            success_msg = f"✅ **Implementation Complete**\n\n- **PR**: #{pr_number}\n- **Status**: Ready for review/merge."
            gh_comment(issue_number, success_msg)

        print_phase("review")
        status_bar.render()
        # A change that touches the interface also gets the UX and design checklist (a warning, never a gate).
        run_shell(f'{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPTS_DIR / "ux_review_run.py"))} job {shlex.quote(str(job_path))}',
                  cwd=ROOT, check=False, capture=False)
        brief_file = OUTPUT_DIR / job["job_id"] / "brief.md"
        if pr_number:
            print(f"\n[5/5] Triggering local AI review for PR #{pr_number}...")
            review_target = str(pr_number)
        else:  # no pull request (plain git, or GitHub couldn't open one): review the branch's own diff
            print(f"\n[5/5] Triggering local AI review for branch {branch}...")
            review_target = f"--branch {shlex.quote(branch)} --base {shlex.quote(base_branch)}"
        run_shell(
            f'{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPTS_DIR / "review_ready.py"))} {review_target} --reviewer {shlex.quote(job["reviewer"])} --brief-file {shlex.quote(str(brief_file))} --job-file {shlex.quote(str(job_path))}',
            cwd=ROOT,
            check=True,
            capture=False,
        )

        job = read_json(job_path)
        review_outcome = job.get("review_outcome") if isinstance(job.get("review_outcome"), dict) else {}
        if review_outcome.get("needs_changes"):
            attempts = int(job.get("review_fix_attempts") or 0)
            if attempts < 1:
                job["review_fix_attempts"] = attempts + 1
                write_json(job_path, job)
                feedback = review_outcome.get("feedback") or "Address the blockers and unmet acceptance criteria in review.md."
                print("\n⚠️  Reviewer flagged blockers or unmet acceptance criteria. Triggering automated gap-closure fix...")
                trigger_followup_iteration(job_path, job, feedback)
                return
            job["status"] = "human-needed"
            job["last_error"] = "Review found unmet acceptance criteria or blockers after automatic fix attempt."
            job["worker_pid"] = None
            job["updated_at"] = now_iso()
            write_json(job_path, job)
            update_issue_status(issue_number, "status:human-needed", ["status:review-needed", "status:executing"])
            send_notifications(
                job,
                "Job Paused: Review Requested Changes",
                f"Review still found blockers or unmet criteria for {job_label}.\nTitle: {job['title']}",
            )
            return

        update_product_requirements(job_path)
        print_status_report(job, True, True, pr_number=pr_number, pr_url=pr_url, distributed_status=distributed_status)
        
        # Determine if we should distribute
        is_yolo = refresh_yolo(job, job_path)
        tasks = job.get("plan", {}).get("tasks", [])
        completed = job.get("completed_task_indices", [])
        all_tasks_done = len(completed) >= len(tasks)

        # Send completion notification
        job_summary = job.get("plan", {}).get("summary")
        send_notifications(
            job, 
            "Job Complete: SUCCESS", 
            f"Implementation successful for {job_label}.\n" + (f"PR: #{pr_number}" if pr_number else f"Branch: {branch}") + f"\nTitle: {job['title']}",
            summary=job_summary
        )
        try:
            from orchestrator import analytics
            all_jobs_count = len(list(job_path.parent.glob("*.json")))
            duration_s = time.time() - start_epoch if "start_epoch" in locals() else None
            analytics.track_job_run(
                job_id=job.get("job_id", ""),
                job_type=job.get("type", "feature-plan"),
                status="succeeded",
                duration_s=duration_s,
                planner_model=job.get("planner"),
                builder_model=job.get("builder"),
                reviewer_model=job.get("reviewer"),
                job_seq=max(1, all_jobs_count),
                runtime_root=job_path.parent.parent,
            )
        except Exception:
            pass

        # YOLO MODE: Automatically continue to next task
        if job.get("is_yolo", False):
            tasks = job.get("plan", {}).get("tasks", [])
            completed = job.get("completed_task_indices", [])
            if len(completed) < len(tasks):
                print("\n\033[1;93m🚀 YOLO MODE: Proceeding to next task automatically...\033[0m")
                time.sleep(2)
                # Re-run execute_job (recursive continuation)
                return execute_job(job_path, resume=True)
            else:
                print("\n\033[1;92m🏁 YOLO MODE: All tasks completed successfully!\033[0m")

        # Interactive Follow-up / Bug / Missing Functionality prompt
        if interactive() and not job.get("headless", False) and not job.get("is_yolo", False):
            job_type = job.get("type", "")
            is_feature = job_type in ["feature-plan", "feature", "feature-design"]

            print("\n" + "─" * 60)
            if is_feature:
                print("\033[1;97m💬 New Feature Verification (Optional):\033[0m")
                print("\033[90mIf you tested this feature and noticed any bug, missing functionality, or UI adjustments\n(e.g., 'Button added but tap handler doesn\\'t submit' or 'Add empty state message'),\nenter it below to immediately trigger an automated iteration with your latest runtime logs attached.\nPress Enter to finish and exit.\033[0m\n")
                prompt_label = "\033[1;96mBug / Missing Functionality (or Enter to finish):\033[0m "
            else:
                print("\033[1;97m💬 Follow-up / Bug Still Happening? (Optional):\033[0m")
                print("\033[90mIf you tested this build and noticed remaining issues or have follow-up feedback\n(e.g., 'Risk tab is visible now but not populated'), enter it below to immediately\ntrigger an automated fix iteration with your latest runtime logs automatically attached.\nPress Enter to finish and exit.\033[0m\n")
                prompt_label = "\033[1;96mFollow-up / Feedback (or Enter to finish):\033[0m "

            try:
                followup_ans = input(prompt_label).strip()
            except (EOFError, KeyboardInterrupt):
                followup_ans = ""
                print("")

            if followup_ans:
                trigger_followup_iteration(job_path, job, followup_ans)
                return

    except BuilderClarificationNeeded as e:
        print("\n" + "!"*60)
        print(f"⏸️  EXECUTION PAUSED: {e}")
        print("!"*60 + "\n")

        mark_human_needed(job_path, job, e.question)
        print_clarification_report(job)
        update_issue_status(issue_number, "status:human-needed", ["status:executing"])
        
        # Post pause comment
        pause_msg = f"⏸️ **Execution Paused**\n\nThe Builder requires human clarification to proceed:\n> {e.question}"
        gh_comment(issue_number, pause_msg)
        
        job_summary = job.get("plan", {}).get("summary")
        send_notifications(
            job,
            "Job Paused: Clarification Needed",
            f"Builder needs clarification for {job_label}.\nQuestion: {e.question}\nTitle: {job['title']}",
            summary=job_summary
        )
        return

    except Exception as e:
        print(f"\n" + "!"*60)
        print(f"❌ CRITICAL ERROR during execution: {e}")
        print("!"*60 + "\n")

        # Mark as human-needed so it's obvious in the console
        job["status"] = "human-needed"
        job["updated_at"] = now_iso()
        job["last_error"] = str(e)
        if blocker := blockers.detect(f"{e}\n{getattr(e, 'stderr', '') or ''}"):
            blockers.record(job, blocker)
        job["worker_pid"] = None
        write_json(job_path, job)

        # Update GitHub too
        try:
            update_issue_status(issue_number, "status:human-needed", ["status:executing"])
            
            # Post error comment
            err_msg = f"❌ **Execution Failed**\n\nA critical error occurred during implementation:\n```\n{e}\n```"
            gh_comment(issue_number, err_msg)
        except:
            pass

        # Send failure notification
        job_summary = job.get("plan", {}).get("summary")
        send_notifications(
            job, 
            "Job Complete: FAILED", 
            f"Critical error during execution for {job_label}.\nError: {e}\nTitle: {job['title']}",
            summary=job_summary
        )
        try:
            from orchestrator import analytics
            all_jobs_count = len(list(job_path.parent.glob("*.json")))
            duration_s = time.time() - start_epoch if "start_epoch" in locals() else None
            analytics.track_job_run(
                job_id=job.get("job_id", ""),
                job_type=job.get("type", "feature-plan"),
                status="failed",
                duration_s=duration_s,
                failure_reason=str(e)[:200],
                planner_model=job.get("planner"),
                builder_model=job.get("builder"),
                reviewer_model=job.get("reviewer"),
                job_seq=max(1, all_jobs_count),
                runtime_root=job_path.parent.parent,
            )
        except Exception:
            pass

        # Re-raise so the user still gets the full traceback for debugging
        raise



def update_product_requirements(job_path: Path) -> None:
    """After a job finishes: see whether what it taught us changes the product requirements document, and keep it true.

    The document is the person's, so this edits only when something clearly changed, keeps their words, records a version they
    can restore, and tells them (see orchestrator/prd.py). It never stops a job: any failure is a printed note."""
    try:
        from orchestrator import prd
        from llm import run_llm
        from model_router import ModelRole
        from common import ORCHESTRATOR_RUNTIME_DIR
        job = read_json(job_path)
        doc = prd.Prd(ROOT, ORCHESTRATOR_RUNTIME_DIR)
        if not prd.should_check(job):
            return
        print("      - Checking whether the product requirements need updating...", flush=True)

        def ask(prompt: str) -> str:
            # An empty scratch folder, not the project: this is a question, and an agentic model must not be able to write the
            # document itself (that would skip the history) or touch anything else.
            with tempfile.TemporaryDirectory(prefix="orchestrator-model-") as scratch:
                reply, _model, _sid = run_llm(job.get("reviewer") or job.get("planner"), prompt, cwd=Path(scratch), timeout=300,
                                              allowed_models=job.get("allowed_models"), role=ModelRole.PLANNER)
            return reply

        result = prd.run_update(doc, job, ask)
        if result["status"] in {"updated", "nochange", "rejected", "error"}:
            fresh = read_json(job_path)  # the job may have changed while the model thought
            fresh["prd_checked"] = True
            write_json(job_path, fresh)
        note = {"updated": f"Updated the product requirements: {result.get('summary', '')}", "nochange": "No change needed.",
                "rejected": result.get("error", ""), "error": f"Couldn't check ({result.get('error', '')})."}.get(result["status"], "")
        if note:
            print(f"      - {note}")
    except Exception as exc:  # never let a convenience stop a finished job
        print(f"      - Note: couldn't check the product requirements: {exc}")


PROTECTED_BRANCHES = {"main", "master", "develop", "trunk"}


def backup_push(job: dict) -> bool:
    """Pushes the job's branch to origin after work is committed, so a computer that dies mid-job loses at most the task in
    progress. Only the job's own branch, never the base branch; best effort (no remote, offline, or rejected: it's tried again
    after the next task, and the PR step pushes anyway)."""
    branch = str(job.get("branch") or "").strip()
    base = str(job.get("base_branch") or PROJECT_CONFIG.base_branch or "main").strip()
    if (not branch or branch.startswith("-") or branch in (base, "HEAD") or branch in PROTECTED_BRANCHES
            or normalize_branch_mode(job.get("branch_mode")) == "manual"):
        return False
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        if getattr(subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(ROOT), capture_output=True, timeout=10), "returncode", 1) != 0:
            return False
        res = subprocess.run(["git", "push", "-u", "origin", "--", branch], cwd=str(ROOT), capture_output=True, text=True, timeout=120, env=env)
    except Exception as exc:  # a backup must never stop the job
        job["backup_error"] = str(exc)[:300]
        return False
    if getattr(res, "returncode", 1) != 0:
        job["backup_error"] = re.sub(r"(https?://)[^/@\s]+@", r"\1", str(getattr(res, "stderr", "") or getattr(res, "stdout", "") or "").strip())[-300:]
        print(f"      - Couldn't back up {branch} to origin yet ({job['backup_error'].splitlines()[-1] if job['backup_error'] else 'push failed'}).")
        return False
    job.pop("backup_error", None)
    job["backed_up_at"] = now_iso()
    print(f"      - Backed up {branch} to origin.")
    return True


def prepare_worktree_branch(job: dict, branch: str, base_branch: str) -> Tuple[str, str]:
    """The job's branch in its own worktree. The base branch is checked out in the project folder, and git won't check a
    branch out twice, so a new branch starts from origin's base (or the local one) without checking the base out here."""
    print(f"\n[1/4] Preparing git branch in this job's own checkout: {branch}...")
    if PROJECT_CONFIG.git_remote:
        subprocess.run(["git", "fetch", "origin"], cwd=str(ROOT), capture_output=True, check=False)
    exists = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=str(ROOT), capture_output=True).returncode == 0
    if exists:
        res = subprocess.run(["git", "checkout", "-f", branch], cwd=str(ROOT), capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Could not check out '{branch}' in this job's own checkout: {(res.stderr or res.stdout).strip()}\n\n"
                               "How to fix:\n- If the project folder has this branch checked out, switch the project folder to another branch, then Resume.")
        subprocess.run(["git", "pull", "--ff-only", "origin", branch], cwd=str(ROOT), capture_output=True, check=False)
    else:
        remote_base = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base_branch}"], cwd=str(ROOT), capture_output=True).returncode == 0
        run(["git", "checkout", "-f", "-b", branch, f"origin/{base_branch}" if remote_base else base_branch], cwd=ROOT)
        print(f"      - Created {branch} from {'origin/' if remote_base else ''}{base_branch}.")
    job["branch"] = branch
    return branch, base_branch


def commit_pending_branch(job: dict) -> Tuple[str, str]:
    """The branch to commit finished work on without checking anything out: the one this checkout is on, which must be the
    job's own branch (else the work isn't where the job thinks, and nothing is touched)."""
    base_branch = job.get("base_branch") or os.environ.get("BASE_BRANCH", PROJECT_CONFIG.base_branch)
    current = current_git_branch()
    expected = str(job.get("branch") or "").strip()
    if expected and current != expected:
        raise RuntimeError(f"The finished work waits to be committed on '{expected}', but this project is on '{current}'. "
                           f"Switch back to '{expected}' (your uncommitted changes go with you), then press Commit the work.")
    print(f"\n[1/4] Committing finished work on {current} (no checkout, so the uncommitted work stays).")
    return current or expected, base_branch


def can_batch_cohesive_tasks(tasks: list[dict], completed: list[int]) -> list[int]:
    """Return contiguous uncompleted task indices (2-3 tasks) that can be executed in one combined builder call.

    Tasks are batched only when:
    - There are 2 to 3 remaining contiguous tasks,
    - Every task in the batch has small/low complexity and non-empty `likely_files`,
    - Every adjacent task shares at least one overlapping `likely_files` target (so disjoint tasks like code + docs
      still get separate per-task commits for independent revert), and
    - Total unique `likely_files` across the batch is at most 4.
    """
    if not isinstance(tasks, list) or len(tasks) < 2:
        return []
    done = set(completed or [])
    remaining = [i for i in range(len(tasks)) if i not in done]
    if not (2 <= len(remaining) <= 3):
        return []
    if remaining != list(range(remaining[0], remaining[0] + len(remaining))):
        return []
    all_files: set[str] = set()
    prev_files: set[str] = set()
    for pos, idx in enumerate(remaining):
        t = tasks[idx]
        if not isinstance(t, dict):
            return []
        comp = str(t.get("complexity") or "").strip().lower()
        if comp not in ("small", "low", "trivial"):
            return []
        t_files = {str(f).strip() for f in (t.get("likely_files") or []) if str(f).strip()}
        if not t_files:
            return []
        if pos > 0 and not (prev_files & t_files):
            return []
        prev_files = t_files
        all_files.update(t_files)
    if len(all_files) > 4:
        return []
    return remaining


def merge_batched_tasks(tasks: list[dict], indices: list[int]) -> dict:
    """Combine a cohesive batch of small tasks into a single builder task brief while preserving all deliverables."""
    titles: list[str] = []
    desc_lines: list[str] = []
    ac: list[str] = []
    likely_files: list[str] = []
    tests: list[str] = []
    for idx in indices:
        t = tasks[idx]
        title = str(t.get("title") or f"Task {idx + 1}").strip()
        titles.append(title)
        desc = str(t.get("description") or title).strip()
        criteria = "; ".join(t.get("acceptance_criteria") or [])
        task_tests = ", ".join(t.get("tests") or [])
        desc_lines.append(f"{idx + 1}. **{title}**: {desc}")
        if criteria:
            desc_lines.append(f"   Acceptance criteria: {criteria}")
        if task_tests:
            desc_lines.append(f"   Planned tests: {task_tests}")
        for item in t.get("acceptance_criteria") or []:
            s = str(item).strip()
            if s and s not in ac:
                ac.append(s)
        for lf in t.get("likely_files") or []:
            s = str(lf).strip()
            if s and s not in likely_files:
                likely_files.append(s)
        for tst in t.get("tests") or []:
            s = str(tst).strip()
            if s and s not in tests:
                tests.append(s)
    return {
        "title": " + ".join(titles),
        "description": "Implement these cohesive tasks together in a single pass:\n" + "\n".join(desc_lines),
        "acceptance_criteria": ac,
        "likely_files": likely_files,
        "tests": tests,
        "complexity": "small",
    }


def checkpoint_task(job: dict, index: int, total: int, title: str, files: list[str], *, batch_indices: list[int] | None = None) -> None:
    """Commits one finished task (or cohesive batch) by itself and records the commit, so the task can be undone later."""
    if not files:
        return
    indices = batch_indices or [index]
    label = f"{indices[0] + 1}-{indices[-1] + 1}/{total}" if len(indices) > 1 else f"{index + 1}/{total}"
    try:
        for f in files:
            subprocess.run(["git", "add", "--", f], cwd=str(ROOT), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"feat: task {label}: {title} (AI generated)", "--", *files], cwd=str(ROOT), check=True, capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), check=True, capture_output=True, text=True).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"      - Could not checkpoint task {label} ({exc}); it will be committed with the rest.")
        return
    commits = job.setdefault("task_commits", {})
    for idx in indices:
        commits[str(idx)] = sha
    job["ai_modified_files"] = sorted(set(job.get("ai_modified_files") or []) | set(files))
    print(f"      - Task {label} committed ({sha[:7]}).")
    backup_push(job)


def check_task_growth(job: dict, index: int, task: dict) -> Optional[dict]:
    """What task `index` committed, against what its plan said it would touch. None when it stayed inside the plan,
    or when it can't be measured (no commit was recorded)."""
    sha = (job.get("task_commits") or {}).get(str(index))
    if not sha:
        return None

    def git(*args: str) -> Optional[str]:
        res = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True)
        return res.stdout if res.returncode == 0 else None

    numstat = git("diff", "--numstat", "--no-renames", f"{sha}~1", sha)
    names = git("diff", "--name-status", "--no-renames", f"{sha}~1", sha)
    if numstat is None or names is None:
        return None
    return task_growth.check(task, scope_check.parse_numstat(numstat, names))


def remove_task_views(job_path: Path) -> None:
    """Deletes the per-task copies of a job (`<job>_task_N.json`) that execute_job writes for the builder.

    They are scratch files; left behind they look like extra jobs with the same id in the job lists."""
    for stale in job_path.parent.glob(f"{job_path.stem}_task_*.json"):
        try:
            stale.unlink()
        except OSError:
            pass


def ready_parallel_wave_indices(job: dict, tasks: list, completed: list[int], current_idx: int) -> list[int]:
    """Return >=2 task indices in the same parallel dependency wave when `current_idx` is the first ready task in that wave."""
    bd = job_runs.breakdown({**job, "completed_task_indices": list(completed)})
    if not bd.get("parallel"):
        return []
    groups = bd.get("groups") or []
    by_id = {g["group_id"]: g for g in groups if isinstance(g, dict) and g.get("group_id")}
    done_set = set(completed or [])
    target_group = next((g for g in groups if current_idx in (g.get("task_indices") or [])), None)
    if not target_group or not target_group.get("parallel"):
        return []
    target_wave = target_group.get("wave")
    ready_indices: list[int] = []
    for g in groups:
        if g.get("wave") != target_wave:
            continue
        g_indices = [idx for idx in (g.get("task_indices") or []) if idx not in done_set]
        if not g_indices:
            continue
        deps_done = all(
            all(d_idx in done_set for d_idx in (by_id.get(dep, {}).get("task_indices") or []))
            for dep in (g.get("depends_on") or [])
        )
        if deps_done:
            ready_indices.extend(g_indices)
    return ready_indices if len(ready_indices) >= 2 and ready_indices[0] == current_idx else []


def _record_parallel_subrun(
    job: dict,
    run_id: str,
    task: dict,
    idx: int,
    started: float,
    *,
    running: bool,
    exit_code: int | None = None,
    status: str = "running",
    detail: str = "",
) -> None:
    """Write a sidecar run record for a parallel wave task so the job's Runs and Tasks tables show each parallel run."""
    try:
        runtime = PROJECT_CONFIG.runtime_dir
    except Exception:
        return
    if not isinstance(runtime, Path):
        return
    tid = task.get("run_history_id")
    refs = {job["job_id"]: [tid]} if isinstance(tid, str) else {}
    attempts = {job["job_id"]: {tid: {"status": status, "detail": detail[:500]}}} if isinstance(tid, str) else {}
    summary = {
        "id": run_id,
        "action": "execute",
        "title": f"Task {idx + 1}: {task.get('title') or f'Task {idx + 1}'}",
        "job": job["job_id"],
        "started": started,
        "ended": None if running else time.time(),
        "running": running,
        "pid": os.getpid() if running else None,
        "exit_code": None if running else exit_code,
        "stopped": False,
        "result": {"headline": detail or f"Task {idx + 1}: {task.get('title') or 'Parallel task'}",
                   "tone": "done" if exit_code == 0 else ("failed" if exit_code else "")},
        "task_refs": refs,
        "task_attempts": attempts,
    }
    job_runs.record(runtime, summary)


def execute_parallel_task_wave(
    job_path: Path,
    job: dict,
    tasks: list[dict],
    wave_indices: list[int],
    issue_number: Optional[int],
    job_label: str,
    pre_state: dict,
    pre_hashes: dict,
    is_yolo: bool,
) -> tuple[bool, set[str], bool]:
    """Execute independent tasks in a parallel wave concurrently, isolating their file lanes and checkpointing each task."""
    import concurrent.futures

    titles = [str(tasks[idx].get("title") or f"Task {idx + 1}") for idx in wave_indices]
    print(
        f"      - ⚡ Parallel task wave: executing tasks {', '.join(str(idx + 1) for idx in wave_indices)} "
        f"concurrently ({' || '.join(titles)}).",
        flush=True,
    )
    print_phase(f"parallel wave ({', '.join(str(idx + 1) for idx in wave_indices)}/{len(tasks)})", subtext=" || ".join(titles))
    link_run_to_tasks(job_path, job, tasks, wave_indices)

    temp_paths: dict[int, Path] = {}
    task_jobs: dict[int, dict] = {}
    subrun_ids: dict[int, str] = {}
    started_at: dict[int, float] = {}

    for idx in wave_indices:
        task = tasks[idx]
        sibling_files = sorted({
            str(f).strip()
            for other in wave_indices if other != idx
            for f in (tasks[other].get("likely_files") or []) if str(f).strip()
        })
        t_job = copy.deepcopy(job)
        t_job["job_id"] = f"{job['job_id']}_task_{idx + 1}"
        t_job["title"] = f"{job['title']} (Task {idx + 1}: {task.get('title', f'Task {idx + 1}')})"
        t_builder = builder_for_task(task, job)
        if t_builder:
            t_job["builder"] = t_builder
        t_job["last_error"] = None
        t_job["debug_history"] = []
        t_job["ai_modified_files"] = []
        t_job["ai_untracked_files"] = []
        t_job["iteration"] = 0
        summary_desc = str(task.get("description") or task.get("title") or "")
        if sibling_files:
            summary_desc += f"\nParallel wave sibling files (hands-off — do not edit): {', '.join(sibling_files)}"
        t_job["plan"]["summary"] = summary_desc
        t_job["plan"]["acceptance_criteria"] = task.get("acceptance_criteria") or []
        t_job["plan"]["likely_files"] = task.get("likely_files") or []
        due_task_nums = {idx + 1, None}
        t_job["plan"]["test_cases"] = [
            c for c in (job.get("plan", {}).get("test_cases") or [])
            if isinstance(c, dict) and c.get("task") in due_task_nums
        ]
        t_path = job_path.parent / f"{job_path.stem}_task_{idx + 1}.json"
        write_json(t_path, t_job)
        temp_paths[idx] = t_path
        task_jobs[idx] = t_job
        run_id = f"{int(time.time() * 1000)}-wave-t{idx + 1}"
        subrun_ids[idx] = run_id
        started_at[idx] = time.time()
        report_task_attempt(job, task, "running", "Building in parallel wave")
        _record_parallel_subrun(job, run_id, task, idx, started_at[idx], running=True)

    task_pre_hashes = dirty_file_hashes(ROOT)
    use_worktree_subproc = (
        (ROOT / ".git").exists()
        and getattr(run_builder, "__module__", "") == "run_builder"
        and normalize_branch_mode(job.get("branch_mode")) == "new"
    )

    def _run_one(idx: int) -> tuple[int, bool, bool, dict, set[str], BaseException | None]:
        t_path = temp_paths[idx]
        if use_worktree_subproc:
            from orchestrator import parallel_jobs, worktrees
            wt_path = parallel_jobs.worktree_for(ROOT, f"{job_path.stem}_task_{idx + 1}")
            wt = None
            try:
                wt = worktrees.ensure(ROOT, wt_path)
                res_file = t_path.with_suffix(".result.json")
                argv = [
                    sys.executable, "-P", "-m", "orchestrator", "script", "worker_run.py",
                    str(job_path.resolve()), "--run-task-view", str(t_path.resolve()),
                ]
                proc = subprocess.run(
                    argv,
                    cwd=str(wt),
                    env=parallel_jobs.worker_env(ROOT, wt, PROJECT_CONFIG.runtime_dir),
                    capture_output=True,
                    text=True,
                )
                if proc.stdout:
                    print(proc.stdout, end="", flush=True)
                if proc.stderr:
                    print(proc.stderr, end="", file=sys.stderr, flush=True)
                if res_file.is_file():
                    payload = read_json(res_file)
                    res_file.unlink(missing_ok=True)
                    if payload.get("clarification"):
                        return idx, False, False, {}, set(), BuilderClarificationNeeded(payload["clarification"])
                    if payload.get("disk_full"):
                        return idx, False, False, {}, set(), DiskFullError(payload["disk_full"])
                    if payload.get("error"):
                        return idx, False, False, {}, set(), RuntimeError(payload["error"])
                    wt_files: set[str] = set()
                    for rel_f in payload.get("files_touched") or []:
                        src = wt / rel_f
                        dst = ROOT / rel_f
                        if src.is_file():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dst)
                            wt_files.add(rel_f)
                    return idx, bool(payload.get("build_ok")), bool(payload.get("test_ok")), payload.get("task_output") or {}, wt_files, None
                if proc.returncode != 0:
                    return idx, False, False, {}, set(), RuntimeError(f"Parallel task {idx + 1} exited with code {proc.returncode}")
            except BaseException as exc:
                return idx, False, False, {}, set(), exc
            finally:
                if wt is not None:
                    try:
                        worktrees.remove(ROOT, wt, force=True)
                    except Exception:
                        pass
        try:
            b_ok, t_ok, _summary, t_out = unpack_builder_result(run_builder(t_path, resume=False))
            return idx, b_ok, t_ok, t_out, set(), None
        except BaseException as exc:
            return idx, False, False, {}, set(), exc

    results_by_idx: dict[int, tuple[bool, bool, dict, set[str], BaseException | None]] = {}
    max_workers = max(1, min(len(wave_indices), 4))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run_one, idx): idx for idx in wave_indices}
        for fut in concurrent.futures.as_completed(futures):
            idx, b_ok, t_ok, t_out, wt_files, err = fut.result()
            results_by_idx[idx] = (b_ok, t_ok, t_out, wt_files, err)

    post_state = get_repo_state()
    post_hashes = dirty_file_hashes(ROOT)
    changed_paths = files_changed_between(task_pre_hashes, post_hashes)
    task_modified = [f for f in post_state["modified"] if f not in pre_state["modified"] and not builder_check.is_ignored_tool_path(f)]
    task_untracked = [f for f in post_state["untracked"] if f not in pre_state["untracked"] and not builder_check.is_ignored_tool_path(f)]
    shared_touched = sorted(
        {f for f in changed_paths if f not in pre_hashes and not builder_check.is_ignored_tool_path(f)}
        if task_pre_hashes or post_hashes else set(task_modified + task_untracked)
    )

    wave_touched: set[str] = set(shared_touched)
    wave_ok = True
    outcomes: dict[int, tuple[str, str]] = {}

    for idx in wave_indices:
        task = tasks[idx]
        t_path = temp_paths[idx]
        t_job = task_jobs[idx]
        b_ok, t_ok, t_out, wt_files, err = results_by_idx[idx]
        wave_touched.update(wt_files)

        if err is not None:
            if isinstance(err, BuilderClarificationNeeded):
                if can_autorespond(job):
                    answer = autorespond(job, err.question, "builder")
                    record_clarification(t_job, err.question, answer)
                    t_job["clarification_history"][-1].update(auto=True, asker="builder")
                    job["status"] = t_job["status"] = "executing"
                    write_json(job_path, job)
                    write_json(t_path, t_job)
                    _, b_ok, t_ok, t_out, wt_files, err = _run_one(idx)
                    wave_touched.update(wt_files)
                if err is not None and isinstance(err, BuilderClarificationNeeded):
                    mark_human_needed(job_path, job, err.question, task_index=idx)
                    update_issue_status(issue_number, "status:human-needed", ["status:executing"])
                    report_task_attempt(job, task, "paused", "Waiting for an answer.")
                    _record_parallel_subrun(job, subrun_ids[idx], task, idx, started_at[idx], running=False, exit_code=1, status="paused", detail="Waiting for an answer.")
                    send_notifications(
                        job,
                        "Job Paused: Clarification Needed",
                        f"Builder needs clarification for {job_label}.\nQuestion: {err.question}\nTitle: {job['title']}",
                    )
                    return False, wave_touched, True
            if err is not None and isinstance(err, DiskFullError):
                report_task_attempt(job, task, "paused", "The machine ran out of disk space.")
                _record_parallel_subrun(job, subrun_ids[idx], task, idx, started_at[idx], running=False, exit_code=1, status="paused", detail="The machine ran out of disk space.")
                raise err
            if err is not None:
                report_task_attempt(job, task, "failed", "The worker stopped. See the run log.")
                _record_parallel_subrun(job, subrun_ids[idx], task, idx, started_at[idx], running=False, exit_code=1, status="failed", detail=str(err))
                print(f"\n❌ Automation error during parallel Task {idx + 1}: {err}")
                err_msg = str(err)
                updated = now_iso()
                job["last_error"] = err_msg
                job["status"] = "human-needed"
                job["worker_pid"] = None
                job["updated_at"] = updated
                write_json(job_path, job)
                t_job["last_error"] = err_msg
                t_job["status"] = "human-needed"
                t_job["updated_at"] = updated
                write_json(t_path, t_job)
                return False, wave_touched, True

        new_sessions = []
        try:
            built = read_json(t_path)
            if built.get("actual_builder_used"):
                job["actual_builder_used"] = built["actual_builder_used"]
            job["reuse_suggested"] = reuse_brief.merge(job.get("reuse_suggested"), built.get("reuse_suggested"))
            new_sessions = (built.get("llm_sessions") or [])[len(job.get("llm_sessions") or []):]
            if new_sessions:
                job["llm_sessions"] = (job.get("llm_sessions") or []) + new_sessions
        except Exception:
            pass

        planned = [str(f).strip().removeprefix("./") for f in (task.get("likely_files") or []) if str(f).strip()]
        if wt_files:
            task_files = sorted(wt_files)
        else:
            matched_planned = [
                f for f in shared_touched
                if any(f == p or f.endswith("/" + p) for p in planned)
            ]
            task_files = matched_planned or list(shared_touched)

        status, detail = task_validation(task, [], task_files, t_out, b_ok, t_ok)
        outcomes[idx] = (status, detail)
        if status == "passed":
            if idx not in job["completed_task_indices"]:
                job["completed_task_indices"].append(idx)
            checkpoint_task(job, idx, len(tasks), task.get("title", f"Task {idx + 1}"), task_files)
            if new_sessions and task_files:
                model_outcomes.record(job, "builder", resolve_builder_model(t_job), new_sessions[-1].get("model"),
                                      "build_checks_passed", tasks=[idx + 1])
            write_json(job_path, job)
            report_task_attempt(job, task, status, detail)
            _record_parallel_subrun(job, subrun_ids[idx], task, idx, started_at[idx], running=False, exit_code=0, status="passed", detail=detail)
        elif b_ok and t_ok:
            job["status"] = "human-needed"
            job["last_error"] = f"Task {idx + 1}: {detail}"
            print(f"      - {job['last_error']}")
            write_json(job_path, job)
            report_task_attempt(job, task, status, detail)
            _record_parallel_subrun(job, subrun_ids[idx], task, idx, started_at[idx], running=False, exit_code=1, status=status, detail=detail)
            wave_ok = False
        else:
            report_task_attempt(job, task, status, detail)
            _record_parallel_subrun(job, subrun_ids[idx], task, idx, started_at[idx], running=False, exit_code=1, status=status, detail=detail)
            print(f"!!! Sub-task {idx + 1} failed validation in parallel wave.")
            wave_ok = False

    note_unconfirmed(job, outcomes)  # only when no task in the wave really failed
    return wave_ok, wave_touched, False


def _execute_task_view(task_job_path: Path) -> int:
    """Run one task view inside an isolated task worktree and write `<task_job>.result.json`."""
    res_file = task_job_path.with_suffix(".result.json")
    pre_state = get_repo_state()
    pre_hashes = dirty_file_hashes(ROOT)
    try:
        b_ok, t_ok, _summary, t_out = unpack_builder_result(run_builder(task_job_path, resume=False))
        post_state = get_repo_state()
        post_hashes = dirty_file_hashes(ROOT)
        changed = files_changed_between(pre_hashes, post_hashes)
        mod = [f for f in post_state["modified"] if f not in pre_state["modified"] and not builder_check.is_ignored_tool_path(f)]
        untr = [f for f in post_state["untracked"] if f not in pre_state["untracked"] and not builder_check.is_ignored_tool_path(f)]
        files = sorted(
            {f for f in changed if f not in pre_hashes and not builder_check.is_ignored_tool_path(f)}
            if pre_hashes or post_hashes else set(mod + untr)
        )
        write_json(res_file, {"build_ok": b_ok, "test_ok": t_ok, "task_output": t_out, "files_touched": files})
        return 0 if (b_ok and t_ok) else 1
    except BuilderClarificationNeeded as clar:
        write_json(res_file, {"clarification": clar.question})
        return 2
    except DiskFullError as disk:
        write_json(res_file, {"disk_full": str(disk)})
        return 3
    except Exception as exc:
        write_json(res_file, {"error": str(exc)})
        return 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_file")
    parser.add_argument("--resume", action="store_true", help="Resume from existing artifacts")
    parser.add_argument("--run-task-view", help="Internal: run a single task view in the current worktree")
    args = parser.parse_args()

    if args.run_task_view:
        sys.exit(_execute_task_view(Path(args.run_task_view)))

    job_path = Path(args.job_file)
    code = run_local(job_path, resume=args.resume)
    if code:
        sys.exit(code)


def run_local(job_path: Path, resume: bool = False) -> int:
    """Runs a job on this computer, the one way every local start goes (the worker itself, and schedule_job dispatching to
    this computer): in its own worktree when it builds alongside another job (parallel_jobs.py), otherwise in the project
    folder once no other job is using it (folder_lock)."""
    if worktree := own_worktree(Path(job_path).resolve()):
        # The worktree is the project folder for this run; the job's records stay in the project.
        from orchestrator import parallel_jobs
        argv = [sys.executable, "-P", "-m", "orchestrator", "script", "worker_run.py", str(Path(job_path).resolve()), *(["--resume"] if resume else [])]
        return subprocess.run(argv, cwd=str(worktree), env=parallel_jobs.worker_env(ROOT, worktree, PROJECT_CONFIG.runtime_dir)).returncode
    with hold_project_folder(job_path):
        execute_job(job_path, resume=resume)
    return 0


def own_worktree(job_path: Path) -> Optional[Path]:
    """The worktree this run should build in instead of the project folder (parallel_jobs.decide), made if needed; None to
    work in the project folder, waiting for it if another job has it."""
    from orchestrator import folder_lock, parallel_jobs
    if os.environ.get(parallel_jobs.ENV_MAIN):
        return None  # already running in one
    try:
        job = read_json(job_path)
        jobs = [(p.stem, read_json(p)) for p in sorted(job_path.parent.glob("*.json")) if p != job_path]
        machines_file = PROJECT_CONFIG.runtime_dir / "config" / "machines.json"
        machines = json.loads(machines_file.read_text(encoding="utf-8")).get("machines", []) if machines_file.is_file() else []
    except (OSError, ValueError):
        return None
    use, why = parallel_jobs.decide(ROOT, job_path.stem, job, jobs, folder_lock.holder(ROOT), parallel_jobs.limit(machines))
    if not use:
        if why:
            print(f"\n⏳ {why}")
        return None
    try:
        worktree = parallel_jobs.create(ROOT, job_path.stem)
    except Exception as exc:  # no worktree: fall back to waiting for the project folder, as before
        print(f"\n⚠️ Couldn't make a separate checkout for this job ({exc}); it waits for the project folder instead.")
        return None
    if job.get("worktree") != str(worktree):
        job["worktree"] = str(worktree)
        write_json(job_path, job)
    print(f"\n⚡ Another job is using the project folder: this one builds alongside it, in its own checkout ({worktree}).")
    return worktree


@contextlib.contextmanager
def hold_project_folder(job_path: Path):
    """Wait until no other job is working in this project folder, then keep it until this run ends (see folder_lock).
    While it waits, the job says which job it's waiting for, so the page can show it."""
    from orchestrator import folder_lock

    def note(change) -> bool:
        """Change the job file if it can be read (a missing or broken one is execute_job's to report)."""
        try:
            current = read_json(job_path)
        except (OSError, ValueError):
            return False
        if change(current) is False:
            return False
        write_json(job_path, current)
        return True

    try:
        job = read_json(job_path)
    except (OSError, ValueError):
        job = {}
    title = job.get("title") or job.get("job_id") or job_path.stem

    def waiting(ahead: dict) -> None:
        name = ahead.get("title") or "another job"
        print(f"\n⏳ Waiting for “{name}” to finish: one job works in the project folder at a time. This one starts on its own after that.")
        note(lambda j: j.update(waiting_for={"job": ahead.get("job") or "", "title": name, "since": time.time()},
                                worker_pid=os.getpid(), worker_host=socket.gethostname()))  # pid: a waiter that was stopped doesn't look queued

    stop_waiting = lambda j: j.pop("waiting_for", None) is not None
    from orchestrator import parallel_jobs
    if os.environ.get(parallel_jobs.ENV_MAIN):  # in the job's own worktree: nobody else works here
        yield
        return
    try:
        with folder_lock.hold(ROOT, job=job.get("job_id") or job_path.stem, title=title, on_wait=waiting):
            if note(stop_waiting):
                print("      - The project folder is free. Starting.")
            yield
    finally:
        note(stop_waiting)  # stopped while waiting: it isn't waiting any more


if __name__ == "__main__":
    main()
