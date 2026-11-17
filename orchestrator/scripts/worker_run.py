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
    dirty_file_hashes,
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
from llm import extract_json_block
from orchestrator.project_config import PROJECT_CONFIG
from orchestrator import blockers, reuse_brief, scope_check, task_growth
from model_router import builder_for_task


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


def prepare_git_branch(job: dict, issue_number: int | None) -> Tuple[str, str]:
    base_branch = job.get("base_branch") or os.environ.get("BASE_BRANCH", PROJECT_CONFIG.base_branch)
    generated_branch = job_branch_name(job, issue_number)
    branch_mode = normalize_branch_mode(job.get("branch_mode"))

    if branch_mode == "manual":
        branch = job.get("branch") or current_git_branch()
        print(f"\n[1/4] Manual branch mode. Using current branch metadata: {branch}")
        print("      - Branch mode is 'manual'. Skipping git operations.")
        return branch, base_branch

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
    summary = str(job.get("builder_summary") or "")
    blocked = any(w in summary.lower() for w in BLOCKED_EDIT_WORDS)
    what = "The fix attempt" if debug_attempt and ever_changed else "The build"
    reason = (" The AI said its file edits were blocked, so check that its tool may edit files, then run the job again."
              if blocked else " If the change was already in place, close the job; otherwise run it again, or describe the change more specifically.")
    return f"{what} finished without changing any files, so nothing was built.{reason}"


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
        touched_this_run: set[str] = set()  # files the plan's tasks changed in this run, committed or not
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
            completed = job.get("completed_task_indices", [])
            all_ok = True
            for i, task in enumerate(tasks):
                if i in completed:
                    print(f"      - Task {i+1} already completed. Skipping.")
                    continue

                print_phase(f"sub-task {i+1}/{len(tasks)}", subtext=task['title'])
                
                # Create a temporary, task-specific job object for the builder
                task_job = copy.deepcopy(job)
                task_job['job_id'] = f"{job['job_id']}_task_{i+1}" # Unique ID per task
                task_job['title'] = f"{job['title']} (Task {i+1}: {task['title']})"
                task_builder = builder_for_task(task, job)
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
                
                task_job['plan']['summary'] = task['description'] # Use task description as summary
                task_job['plan']['acceptance_criteria'] = task['acceptance_criteria']
                task_job['plan']['likely_files'] = task['likely_files']
                # Only this task's test cases (plus untasked ones); `task` is 1-based.
                task_job['plan']['test_cases'] = [
                    c for c in job.get('plan', {}).get('test_cases') or []
                    if isinstance(c, dict) and c.get('task') in (i + 1, None)
                ]
                
                # Write this temp view to a temporary path to pass to run_builder
                temp_job_path = job_path.parent / f"{job_path.stem}_task_{i+1}.json"
                write_json(temp_job_path, task_job)
                
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
                        send_notifications(
                            job,
                            "Job Paused: Clarification Needed",
                            f"Builder needs clarification for {job_label}.\nQuestion: {clarification.question}\nTitle: {job['title']}"
                        )
                        return
                    except Exception as e:
                        # SYSTEM/LLM ERROR: Do not trigger automated debugging
                        print(f"\n❌ Automation error during Task {i+1}: {e}")
                        job["last_error"] = str(e)
                        job["status"] = "human-needed"
                        job["worker_pid"] = None
                        job["updated_at"] = now_iso()
                        write_json(job_path, job)
                        return
                
                # IMPORTANT: Only mark task done if code was actually changed (patch)
                # and validation passed. If AI only 'investigated', it's not done yet.
                # VERIFY: Did the AI actually touch the files it said it would?
                # This prevents "phantom completion" where the AI runs old passing tests 
                # but doesn't implement the current task.
                task_post_state = get_repo_state()
                task_modified = [f for f in task_post_state["modified"] if f not in pre_state["modified"]]
                task_untracked = [f for f in task_post_state["untracked"] if f not in pre_state["untracked"]]
                files_touched = task_modified + task_untracked
                touched_this_run.update(files_touched)

                # run_builder records which model it used on the per-task copy; keep that on the real job.
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

                # Only the debug prompts ask for an "action". A feature builder reports files and a summary, so with no action
                # the files on disk decide; an explicit "investigate" still means nothing was changed.
                action = task_output.get("action")
                was_implemented = action in ("patch", "add_logging") or (not action and bool(files_touched))
                
                likely_files = task.get("likely_files", [])
                actual_likely_touched = any(any(f.endswith(lf) for lf in likely_files) for f in files_touched)
                
                # VERIFY: Did the AI run the right tests?
                # If the builder overrode the test command to something unrelated to the task, flag it.
                test_command_used = task_output.get("test_command", "")
                expected_tests = task.get("tests", [])
                # Running the project's whole configured suite (or saying nothing, so the default runs) covers every planned test.
                # Only a command that names something narrower, and not the planned tests, is a downgrade.
                configured = " ".join((PROJECT_CONFIG.test_command or "").split())
                used = " ".join((test_command_used or "").split())
                runs_whole_suite = not used or (bool(configured) and used == configured)
                test_command_ok = not expected_tests or runs_whole_suite or any(et in used for et in expected_tests)
                
                if build_ok and test_ok and was_implemented:
                    if likely_files and not actual_likely_touched:
                         print(f"      - ⚠️  Task {i+1} CLAIMED success, but none of the likely files were touched.")
                         print(f"      - Files touched: {files_touched}")
                         print(f"      - Likely files: {likely_files}")
                         job["status"] = "human-needed"
                         job["last_error"] = f"Task {i+1} phantom completion: AI claimed success but didn't modify expected files."
                         write_json(job_path, job)
                         all_ok = False
                         break

                    if not test_command_ok:
                         print(f"      - ⚠️  Task {i+1} passed tests, but the test command was downgraded.")
                         print(f"      - Test command used: {test_command_used}")
                         print(f"      - Expected tests: {expected_tests}")
                         job["status"] = "human-needed"
                         job["last_error"] = f"Task {i+1} phantom completion: AI ran unrelated tests to get a green signal."
                         write_json(job_path, job)
                         all_ok = False
                         break

                    job["completed_task_indices"].append(i)
                    checkpoint_task(job, i, len(tasks), task['title'], files_touched)
                    write_json(job_path, job)

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
                elif build_ok and test_ok and not was_implemented:
                    print(f"      - Task {i+1} investigation successful, but no code changed. Halting to prevent phantom completion.")
                    # We mark it as human-needed to break the loop and avoid infinite recursion
                    job["status"] = "human-needed"
                    job["last_error"] = f"Task {i+1} was not implemented (AI did not write code)."
                    write_json(job_path, job)
                    all_ok = False 
                    break 
                else:
                    print(f"!!! Sub-task {i+1} failed validation. Halting feature implementation.")
                    all_ok = False
                    break # Exit the loop on first failure
            
            remove_task_views(job_path)
            build_ok = all_ok
            test_ok = all_ok
        else:
            # Original single-task execution
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

        # Identify files changed/added by AI
        ai_modified = [f for f in post_state["modified"] if f not in pre_state["modified"]]
        ai_untracked = [f for f in post_state["untracked"] if f not in pre_state["untracked"]]
        if committing_only:  # the files the passing run changed, if still uncommitted
            saved = set(job["passed_uncommitted"].get("files") or [])
            ai_modified = [f for f in post_state["modified"] if f in saved]
            ai_untracked = [f for f in post_state["untracked"] if f in saved]

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
        changed_now = sorted(set(ai_modified) | set(ai_untracked) | touched_this_run | set(files_changed_between(pre_hashes, dirty_file_hashes(ROOT))))
        # The task loop may already have stopped the job with a more specific reason (phantom completion, unrelated tests).
        already_stopped = job.get("type") == "feature-plan" and job.get("status") == "human-needed"
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
            write_json(job_path, job)

        # Commit changes if any
        if build_ok and test_ok and (ai_modified or ai_untracked):
            # Say it passed before committing: if the commit fails, the job shows "Tests passed, not committed" and Resume
            # commits it, rather than "debug failing tests" from the previous attempt.
            job["passed_uncommitted"] = {"files": sorted(set(ai_modified) | set(ai_untracked)), "at": now_iso()}
            if job.get("debug_history") and job["debug_history"][-1].get("result") == "pending":
                job["debug_history"][-1]["result"] = {"build_ok": True, "tests_ok": True, "notes": "Tests passed"}
            write_json(job_path, job)
        if ai_modified or ai_untracked:
            print(f"      - Committing {len(ai_modified)} modified and {len(ai_untracked)} untracked files...")
            for f in ai_modified + ai_untracked:
                _git_commit_step(f"git add {shlex.quote(f)}")
            
            commit_msg = f"feat: {job['title']} (AI generated)"
            if job.get("type") == "bug-fix":
                commit_msg = f"fix: {job['title']} (AI generated)"
            
            _git_commit_step(f"git commit -m {shlex.quote(commit_msg)}")
            backup_push(job)
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
            if blocker:  # the machine, not the code: a fix attempt can't help, so pause on it (Home resumes it once cleared)
                pause_on_blocker(job_path, job, blocker, issue_number)
                return
            # Set job status to debugging to trigger an automatic iteration
            job["status"] = "debugging"
            job["debug_phase"] = "propose"
            job["iteration"] = job.get("iteration", 0) + 1
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


def checkpoint_task(job: dict, index: int, total: int, title: str, files: list[str]) -> None:
    """Commits one finished task by itself and records the commit, so the task can be undone later."""
    if not files:
        return
    try:
        for f in files:
            subprocess.run(["git", "add", "--", f], cwd=str(ROOT), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"feat: task {index + 1}/{total}: {title} (AI generated)"], cwd=str(ROOT), check=True, capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), check=True, capture_output=True, text=True).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"      - Could not checkpoint task {index + 1} ({exc}); it will be committed with the rest.")
        return
    job.setdefault("task_commits", {})[str(index)] = sha
    job["ai_modified_files"] = sorted(set(job.get("ai_modified_files") or []) | set(files))
    print(f"      - Task {index + 1} committed ({sha[:7]}).")
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_file")
    parser.add_argument("--resume", action="store_true", help="Resume from existing artifacts")
    args = parser.parse_args()

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
