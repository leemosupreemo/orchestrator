"""Local web UI for the orchestrator: `orchestrator ui`.

Everything the UI runs goes through the same CLI entry points a terminal
would, inside a pseudo-terminal, so interactive prompts (y/n keys, arrow-key
menus, pasted logs, passwords) still work — the browser shows a real terminal
and forwards keystrokes. The UI adds structured pages on top: project state,
jobs, a new-job form, device logs, and the history of runs.

Security model:
- Binds 127.0.0.1 by default. Every API call needs the per-server token (given
  once in the printed URL, then kept in an HttpOnly SameSite=Strict cookie).
- Requests whose Host header isn't this server's are refused (DNS rebinding).
- State-changing calls must be JSON POSTs carrying `X-Orchestrator-UI: 1`,
  which a cross-site form can't send.
- The browser can only start actions from a fixed allowlist; it never sends a
  command line. Paths it names (jobs, files) are confined to the project's
  runtime dir.
"""
from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import hashlib
import hmac
import io
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import zipfile
import signal
import ssl
import struct
import subprocess
import tempfile
import sys
import termios
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
import urllib.error
import urllib.request

from orchestrator import account
from orchestrator import audit
from orchestrator import integrations
from orchestrator import analytics
from orchestrator import delivery as delivery_view
from orchestrator import features as feature_store
from orchestrator import feature_map
from orchestrator import plan_run
from orchestrator import inbox as inbox_view
from orchestrator import task_revert
from orchestrator import preflight
from orchestrator import integration_check
from orchestrator import new_job_form
from orchestrator import notifier
from orchestrator import plan_edit
from orchestrator import prd as prd_doc
from orchestrator import project_docs
from orchestrator import project_health
from orchestrator import scope_check
from orchestrator.stack_detection import detect_project_stack
from orchestrator.scripts import test_cases as test_case_lib
from orchestrator import job_chat
from orchestrator import new_project
from orchestrator.runtime_control import ActivityGate, BusyError, InstanceLease
from orchestrator.setup_checklist import setup_checklist
from orchestrator.project_config import (
    DEFAULT_RUNTIME_DIRNAME,
    find_project_root,
    forget_project,
    load_recent_projects,
    project_display_name,
    remember_project,
    safe_resolve,
    user_state_dir,
)

PUBLIC_URL: dict[str, str] = {}  # {"url": ...} once the server knows where people reach it
STATIC_DIR = Path(__file__).resolve().parent / "static"
PACKAGE_PARENT = Path(__file__).resolve().parents[2]  # folder holding the `orchestrator` package serving this UI
COOKIE_NAME = "orchestrator_ui"
FIREBASE_API_KEY = "AIzaSyBg8h8yiC8OCoezFLEq6mQLhlc260b8CcI"
FIREBASE_PROJECT_ID = account.FIREBASE_PROJECT_ID
HOSTED_APP_URL = account.HOSTED_APP_URL
HOSTED_ORIGINS = account.HOSTED_ORIGINS
# Who may do what. The owner is whoever set this computer up (the paired account, the git email, the environment
# variable); an email added under Configuration is a member. Members run jobs, approve, build, test and merge; they can't
# change who has access, see or change secrets and credentials, add folders from the disk, or run free-form tools.
OWNER_SOURCES = ("git", "account", "environment")
OWNER_ONLY_CONFIG = {"keys", "email", "analytics", "webhook", "allowed-email", "firebase", "fleet", "role-prompts"}
OWNER_ONLY_ACTIONS = {"console", "wizard", "config_menu", "worker_install", "sync_fleet", "fleet_llm_check", "update_local",
                      "update_fleet", "test_email", "logs_setup", "deliver", "distribute"}
SIGN_IN_TTL_SECONDS = 30 * 24 * 3600
ALLOWED_EMAILS_CACHE_SECONDS = 60
MAX_BUFFER_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SESSIONS_KEPT = 50
IDLE_PROMPT_SECONDS = 20
OUTPUT_MAX_WAIT_SECONDS = 20  # a long-poll answers within this, well inside the 100 s proxies such as Cloudflare allow
JOB_TYPES = ["bug", "feature", "design", "coverage", "quick"]
BRANCH_MODES = ["new", "current", "manual"]
# Runs a child as the leader of a new session with the pty as its controlling
# terminal, so /dev/tty (getpass) and job-control signals behave as in a real
# terminal. Done in the child's own interpreter, not preexec_fn, which is unsafe
# in this multithreaded server.
CTTY_SHIM = ("import fcntl, os, sys, termios\n"
             "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
             "os.execvp(sys.argv[1], sys.argv[1:])\n")


class UIError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- background tasks


class BackgroundTasks:
    """Slow model calls run here, and the page asks how they are getting on.

    One request held open for a minute or two is the wrong shape for a phone behind a tunnel (a quick tunnel closes any request after
    about 100 seconds, so a slower model turn looked like a failure). The request that starts the work returns at once with an id."""
    KEEP_SECONDS = 1800

    def __init__(self, gate: ActivityGate | None = None) -> None:
        self.gate = gate or ActivityGate()
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start(self, work: Callable[[], Any]) -> str:
        release = self.gate.hold()
        task_id = secrets.token_hex(6)
        item: dict[str, Any] = {"status": "running", "at": time.time()}
        with self._lock:
            for old in [k for k, v in self._items.items() if v["status"] != "running" and time.time() - v["at"] > self.KEEP_SECONDS]:
                del self._items[old]
            self._items[task_id] = item

        def run() -> None:
            try:
                result = work()
                item.update(status="done", result=result)
            except UIError as exc:
                item.update(status="error", error=str(exc), code=int(exc.status))
            except Exception as exc:  # whatever went wrong, the page gets a message rather than a task that never ends
                item.update(status="error", error=str(exc)[:300] or "Something went wrong.", code=500)
            finally:
                release()

        threading.Thread(target=run, daemon=True).start()
        return task_id

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(task_id)
        return None if item is None else {k: v for k, v in item.items() if k != "at"}


# --------------------------------------------------------------------------- terminal sessions


class PtySession:
    """A command running in a pseudo-terminal, with its output buffered so any
    number of browser tabs can attach, detach and catch up from an offset."""

    def __init__(self, sid: str, action: str, title: str, argv: list[str], cwd: Path,
                 env: dict[str, str], transcript: Path | None = None, cols: int = 110, rows: int = 32,
                 on_exit: Callable[[], None] | None = None):
        self._on_exit = on_exit
        self.id = sid
        self.action = action
        self.title = title
        self.argv = argv
        self.job_id: str | None = None
        self.result_job: str | None = None
        self.started = time.time()
        self.last_output = self.started
        self.ended: float | None = None
        self.exit_code: int | None = None
        self._buffer = bytearray()
        self._base = 0  # absolute offset of _buffer[0]
        self._cond = threading.Condition()
        self._transcript = transcript.open("ab") if transcript else None
        self.transcript_path = transcript

        master, slave = os.openpty()
        self._fd = master
        self._set_winsize(cols, rows)
        env = {**env, "TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows)}
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-c", CTTY_SHIM, *argv],
                stdin=slave, stdout=slave, stderr=slave, cwd=str(cwd), env=env,
                start_new_session=True, close_fds=True,
            )
        finally:
            os.close(slave)
        threading.Thread(target=self._pump, name=f"pty-{sid}", daemon=True).start()

    # -- lifecycle

    @property
    def running(self) -> bool:
        return self.exit_code is None

    def _pump(self) -> None:
        while True:
            try:
                chunk = os.read(self._fd, 65536)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                chunk = b""
            if not chunk:
                break
            self._append(chunk)
        code = self._proc.wait()
        try:
            os.close(self._fd)
        except OSError:
            pass
        status = "exited" if code == 0 else f"exited with code {code}"
        self._append(f"\r\n\x1b[90m[{status}]\x1b[0m\r\n".encode())
        if self._transcript:
            self._transcript.close()
        if self._on_exit:
            self._on_exit()
        with self._cond:
            self.exit_code = code
            self.ended = time.time()
            self._cond.notify_all()

    def _append(self, chunk: bytes) -> None:
        with self._cond:
            self.last_output = time.time()
            self._buffer.extend(chunk)
            overflow = len(self._buffer) - MAX_BUFFER_BYTES
            if overflow > 0:
                del self._buffer[:overflow]
                self._base += overflow
            self._cond.notify_all()
        if self._transcript:
            self._transcript.write(chunk)
            self._transcript.flush()

    def read(self, offset: int, timeout: float) -> tuple[int, bytes, bool]:
        """Returns (next_offset, data, finished), waiting up to `timeout` for data."""
        with self._cond:
            end = self._base + len(self._buffer)
            if offset >= end and self.running:
                self._cond.wait(timeout)
                end = self._base + len(self._buffer)
            start = max(offset, self._base)
            data = bytes(self._buffer[start - self._base:]) if start < end else b""
            return end, data, not self.running and start + len(data) >= end

    def write(self, data: bytes) -> None:
        if not self.running:
            return
        try:
            os.write(self._fd, data)
        except OSError:
            pass  # exited between the check and the write

    def resize(self, cols: int, rows: int) -> None:
        if self.running:
            self._set_winsize(cols, rows)
            try:
                os.killpg(self._proc.pid, signal.SIGWINCH)
            except OSError:
                pass

    def _set_winsize(self, cols: int, rows: int) -> None:
        cols = max(20, min(int(cols), 500))
        rows = max(5, min(int(rows), 200))
        fcntl.ioctl(self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def stop(self) -> None:
        if not self.running:
            return
        try:
            os.killpg(self._proc.pid, signal.SIGTERM)
        except OSError:
            return

        def escalate() -> None:
            time.sleep(3)
            if self.running:
                try:
                    os.killpg(self._proc.pid, signal.SIGKILL)
                except OSError:
                    pass

        threading.Thread(target=escalate, daemon=True).start()

    def summary(self) -> dict[str, Any]:
        idle = time.time() - self.last_output if self.running else 0
        return {
            "id": self.id, "action": self.action, "title": self.title,
            "command": " ".join(_display_argv(self.argv)), "started": self.started,
            "ended": self.ended, "running": self.running, "exit_code": self.exit_code,
            "job": self.job_id, "result_job": self.result_job,
            # Quiet for a while with a prompt-shaped last line: probably waiting on you.
            "waiting": self.running and idle > IDLE_PROMPT_SECONDS and self._looks_like_prompt(),
            "last_line": self._last_line(), "idle": int(idle),
        }

    def _last_line(self) -> str:
        """Newest non-empty output line, stripped of colour codes, for run cards."""
        with self._cond:
            tail = bytes(self._buffer[-2000:])
        text = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", tail).decode("utf-8", "replace").replace("\r", "\n")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        return lines[-1][:200] if lines else ""

    def _looks_like_prompt(self) -> bool:
        with self._cond:
            tail = bytes(self._buffer[-400:])
        text = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", tail).decode("utf-8", "replace").rstrip(" \t")
        last = text.splitlines()[-1].strip() if text.strip() else ""
        return bool(last) and (last.endswith((":", "?", ">", ")", "]")) or "Enter" in last or "(y/n" in last.lower())


def _display_argv(argv: list[str]) -> list[str]:
    """`python -m orchestrator ...` reads better as `orchestrator ...`."""
    if len(argv) >= 4 and argv[1:4] == ["-P", "-m", "orchestrator"]:
        return ["orchestrator", *argv[4:]]
    return argv


class SessionManager:
    def __init__(self, gate: ActivityGate | None = None) -> None:
        self.gate = gate or ActivityGate()
        self._sessions: dict[str, PtySession] = {}
        self._lock = threading.Lock()

    def start(self, action: str, title: str, argv: list[str], cwd: Path, env: dict[str, str],
              transcript_dir: Path | None, cols: int = 110, rows: int = 32) -> PtySession:
        sid = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        transcript = None
        if transcript_dir:
            transcript_dir.mkdir(parents=True, exist_ok=True)
            transcript = transcript_dir / f"{sid}-{action}.log"
        release = self.gate.hold()
        try:
            session = PtySession(sid, action, title, argv, cwd, env, transcript, cols=cols, rows=rows, on_exit=release)
        except BaseException:
            release()
            raise
        with self._lock:
            self._sessions[sid] = session
            finished = [s for s in self._sessions.values() if not s.running]
            for old in sorted(finished, key=lambda s: s.started)[:max(0, len(self._sessions) - MAX_SESSIONS_KEPT)]:
                self._sessions.pop(old.id, None)
        return session

    def get(self, sid: str) -> PtySession:
        with self._lock:
            session = self._sessions.get(sid)
        if not session:
            raise UIError("Unknown run", HTTPStatus.NOT_FOUND)
        return session

    def list(self, job_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
        if job_id:
            sessions = [s for s in sessions if job_id in (s.job_id, s.result_job)]
        return [s.summary() for s in sorted(sessions, key=lambda s: s.started, reverse=True)]

    def running_job_ids(self) -> set[str]:
        with self._lock:
            return {s.job_id for s in self._sessions.values() if s.running and s.job_id}

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.stop()


# --------------------------------------------------------------------------- project state


def runtime_dir(root: Path) -> Path:
    return root / DEFAULT_RUNTIME_DIRNAME


def jobs_dir(root: Path) -> Path:
    return runtime_dir(root) / "jobs"


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()


def verify_firebase_id_token(id_token: str) -> dict[str, Any]:
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={FIREBASE_API_KEY}"
    req = urllib.request.Request(
        url,
        data=json.dumps({"idToken": id_token}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            users = data.get("users") or []
            if not users:
                raise UIError("Invalid user identity", HTTPStatus.UNAUTHORIZED)
            return users[0]
    except urllib.error.HTTPError as exc:
        try:
            err_body = json.loads(exc.read().decode("utf-8"))
            err_msg = (err_body.get("error") or {}).get("message") or "Authentication failed"
        except Exception:
            err_msg = "Authentication failed"
        raise UIError(f"Authentication failed: {err_msg}", HTTPStatus.UNAUTHORIZED)
    except UIError:
        raise
    except Exception as exc:
        raise UIError(f"Authentication failed: {exc}", HTTPStatus.UNAUTHORIZED)


def allowed_auth_sources(root: Path) -> dict[str, str]:
    """Emails allowed to sign in, each with where it came from."""
    found: dict[str, str] = {}

    def add(email: Any, source: str) -> None:
        email = str(email or "").strip().lower()
        if email:
            found.setdefault(email, source)

    git_email = git(root, "config", "user.email")
    if not git_email:
        try:
            git_email = subprocess.run(["git", "config", "--global", "user.email"],
                                       capture_output=True, text=True, timeout=3).stdout
        except Exception:
            git_email = ""
    add(git_email, "git")
    add((account.load_machine() or {}).get("owner_email"), "account")
    for e in os.environ.get("ORCHESTRATOR_ALLOWED_EMAILS", "").split(","):
        add(e, "environment")
    for e in read_json_file(runtime_dir(root) / "project.json").get("allowed_emails", []):
        add(e, "project")
    for e in read_settings(root).get("allowed_emails", []):
        add(e, "settings")
    return found


def allowed_auth_emails(root: Path) -> set[str]:
    return set(allowed_auth_sources(root))


KIND_LABELS = {
    "bug": "Bug fix", "bug-fix": "Bug fix", "bug-investigate": "Bug fix", "quick": "Quick change",
    "quick-fix": "Quick change", "feature": "Feature", "feature-plan": "Feature", "feature-task": "Feature task",
    "feature-design": "Design", "design": "Design", "coverage": "Tests", "test-audit": "Tests",
}
FAILING_TEST_STATUSES = {"tests-failed", "build-failed", "failed"}


def job_task_list(job: dict[str, Any]) -> list[Any]:
    """The job's tasks. The plan's list is what the worker runs, so it wins; older jobs only have a top-level list."""
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    if isinstance(plan.get("tasks"), list) and plan["tasks"]:
        return plan["tasks"]
    return job["tasks"] if isinstance(job.get("tasks"), list) else []


def job_state(job: dict[str, Any]) -> dict[str, Any]:
    """What the job is waiting on, in plain words, and the one action that moves
    it forward. `group` is needs_you | working | done; `tone` drives colour:
    attention (amber) = your move, working (blue), done (green), failed (red)."""
    status = job.get("status") or "unknown"
    tests = job.get("test_status")
    pr = job.get("pr_number")
    tasks = job_task_list(job)
    remaining = len(tasks) - len(job.get("completed_tasks") or []) if tasks else 0

    def state(group, tone, label, reason, action=None, action_label=None):
        return {"group": group, "tone": tone, "label": label, "reason": reason,
                "next": {"action": action, "label": action_label} if action else None}

    verification = job.get("verification") or {}
    if status == "human-needed" and verification.get("status") in ("rejected", "concerns"):
        return state("needs_you", "attention", "Review suggestions",
                     f"The architect has {verification['status']}: {verification.get('comments') or 'see details'}".strip(),
                     "approve", "Accept suggestions")
    if status == "human-needed":
        if job.get("human_clarification_question"):
            return state("needs_you", "attention", "Question for you", "The planner needs an answer to continue.", "answer", "Answer")
        return state("needs_you", "attention", "Action required", "Waiting on a decision in the console.", "console", "Open in console")
    if status == "designing":
        return state("needs_you", "attention", "Design ready", "Approve the design to plan its implementation, or ask for changes.", "approve", "Approve design")
    if status == "planned":
        if job.get("type") == "feature-plan" and not job.get("approved"):
            return state("needs_you", "attention", "Approve plan", "Approve the plan to start building its tasks.", "approve", "Approve plan")
        return state("needs_you", "attention", "Planned", "Start building when you're happy with the plan.", "schedule", "Start")
    if status == "review-needed":
        if pr:
            return state("needs_you", "attention", "Ready to merge", f"PR #{pr} is ready for your review.", "merge", "Merge & complete")
        if job.get("code_host") == "git" and job.get("branch"):
            where = " A merge request link is on the job." if job.get("merge_request_url") else ""
            return state("needs_you", "attention", "Ready to merge",
                         f"The changes are on {job['branch']}. Merge & complete merges it into {job.get('base_branch') or 'the base branch'} "
                         f"on this computer; or merge it on your git host and mark it complete.{where}", "merge", "Merge & complete")
        return state("needs_you", "attention", "Ready for review", "Changes are ready. No pull request was opened, so there is nothing to merge here. Mark it complete when you're happy.", "complete", "Mark complete")
    if status == "debugging":
        if tests in FAILING_TEST_STATUSES:
            label = "Build failing" if tests == "build-failed" else "Tests failing"
            return state("needs_you", "failed", label, "Run a fix attempt, optionally with fresh device logs.", "debug", "Run fix")
        return state("needs_you", "attention", "Needs a fix", "Run a fix attempt, optionally with fresh device logs.", "debug", "Run fix")
    if status == "scheduled":
        return state("working", "working", "Queued", "Dispatched and waiting for a worker.", "execute", "Run now")
    if status in {"executing", "running", "in-progress", "decomposed"}:
        return state("working", "working", "Building", "A worker is implementing this.")
    if status == "completed":
        return state("done", "done", "Completed", "Merged and archived." if pr else "Finished.")
    if status == "failed":
        return state("needs_you", "failed", "Failed", "Last run failed; try a fix.", "debug", "Run fix")
    if remaining:
        return state("working", "working", status.replace("-", " ").capitalize(), f"{remaining} task(s) left.", "resume", "Resume")
    return state("working", "working", status.replace("-", " ").capitalize(), "")


def write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def job_summary(path: Path, job: dict[str, Any]) -> dict[str, Any]:
    tasks = job_task_list(job)
    completed = job.get("completed_tasks") or job.get("completed_task_indices") or []
    kind = job.get("type") or job.get("job_type")
    issue_num = job.get("issue_number")
    job_id = job.get("job_id") or path.stem
    display_id = f"#{issue_num} ({job_id})" if issue_num else str(job_id)
    approach = job.get("approach") or job.get("execution_strategy") or "Standard workflow"

    return {
        "id": path.stem,
        "job_id": job_id,
        "display_id": display_id,
        "title": job.get("title") or job.get("summary") or path.stem,
        "type": kind,
        "kind": KIND_LABELS.get(str(kind), str(kind or "Job").replace("-", " ").capitalize()),
        "status": job.get("status") or "unknown",
        "approach": approach,
        "state": job_state(job),
        "feature": job.get("feature") or None,
        "plan_run": job.get("plan_run") or None,
        "branch": job.get("branch"),
        "base_branch": job.get("base_branch") or "main",
        "issue_number": issue_num,
        "pr_number": job.get("pr_number"),
        "question": job.get("human_clarification_question") if job.get("status") == "human-needed" else None,
        "tasks_total": len(tasks) if isinstance(tasks, list) else 0,
        "tasks_done": len(completed) if isinstance(completed, list) else 0,
        "updated": path.stat().st_mtime,
    }


def job_touched_files(job: dict[str, Any]) -> list[str]:
    """Files a job changed or plans to change, for feature-overlap checks."""
    files = list(job.get("ai_modified_files") or []) + list(job.get("ai_untracked_files") or [])
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    for source in [plan] + [t for t in (plan.get("tasks") or []) if isinstance(t, dict)]:
        files += source.get("likely_files") or []
    return sorted({str(f).strip().lstrip("./") for f in files if str(f).strip()})


def archived_feature_jobs(root: Path) -> list[dict[str, Any]]:
    """Finished jobs live in jobs/archive; they still count toward their feature."""
    out = []
    for path in (jobs_dir(root) / "archive").glob("*.json"):
        job = read_json_file(path)
        if job.get("feature") and job.get("status") == "completed":
            out.append(job_summary(path, job))
    return sorted(out, key=lambda j: j["updated"], reverse=True)


def inbox_overview(root: Path, sessions: Any, with_others: bool = True,
                   runs: list[dict[str, Any]] | None = None, jobs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    visible_runs = sessions.list() if runs is None else runs
    # A privileged delivery can still make a job busy even when its terminal is hidden from a member.
    # Keep the manager's authoritative running-job set; only terminal-derived inbox entries use visible_runs.
    running = sessions.running_job_ids()
    jobs = list_jobs(root) if jobs is None else jobs
    for job in jobs:
        job["active_run"] = job["id"] in running
    active = {"name": project_display_name(root), "root": str(safe_resolve(root))}
    others = []
    if with_others:
        for p in load_recent_projects().get("projects", []):
            try:
                other = safe_resolve(Path(str(p.get("root") or "")).expanduser())
            except Exception:
                continue
            if str(other) == active["root"] or not jobs_dir(other).is_dir():
                continue
            others.append({"name": p.get("name") or project_display_name(other), "root": str(other), "jobs": list_jobs(other)})
    return inbox_view.build(active, jobs, visible_runs, others)


def inbox_state(root: Path, sessions: Any, runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """What the browser polls: the count for the badge and the items it can notify about."""
    here = inbox_overview(root, sessions, with_others=False, runs=runs)["here"]
    return {"inbox_count": len(here),
            "inbox": [{"id": i["id"], "title": i["title"], "label": i["label"], "reason": i["reason"],
                       "hash": i.get("href") or (f"#/runs/{i['run_id']}" if i["kind"] == "run" else f"#/jobs/{i['job_id']}")} for i in here]}


_GH_CACHE: dict[str, tuple[float, str | None]] = {}
GH_TTL = 60.0


def gh_cached(root: Path, args: list[str]) -> str | None:
    """`gh` output for a few seconds, or None when gh is missing, signed out, or the repo isn't on GitHub."""
    key = f"{root}:{' '.join(args)}"
    hit = _GH_CACHE.get(key)
    if hit and time.time() - hit[0] < GH_TTL:
        return hit[1]
    try:
        res = subprocess.run(["gh", *args], cwd=root, capture_output=True, text=True, timeout=15)
        out = res.stdout if res.returncode == 0 else None
    except Exception:
        out = None
    _GH_CACHE[key] = (time.time(), out)
    return out


def delivery_overview(root: Path) -> dict[str, Any]:
    settings = read_settings(root)
    config = read_json_file(runtime_dir(root) / "project.json")
    jobs = list_jobs(root)
    firebase = {"configured": bool(config.get("firebase_distribution")), "app_id_set": bool(settings.get("firebase_app_id")),
                "groups": [g.strip() for g in str(settings.get("firebase_tester_groups", "")).split(",") if g.strip()],
                "invite_url": settings.get("firebase_invite_url", ""),
                "cli_installed": shutil.which("firebase") is not None}
    ci = (root / "ci_scripts").is_dir() or (root / ".xcodecloud").exists()
    data = delivery_view.overview(lambda *a: git(root, *a), lambda argv: gh_cached(root, argv), runtime_dir(root),
                                  base_branch(root, settings), jobs, firebase, ci)
    data["web_url"] = repo_web_url(root)
    return data


def analytics_overview(root: Path) -> dict[str, Any]:
    state = analytics_state(read_settings(root))
    provider = analytics.PROVIDERS.get(state["provider"], {})
    return {**state, "provider_name": provider.get("name"), "key_label": provider.get("key_label"),
            "features": feature_store.rollup(feature_store.load(runtime_dir(root)), [])}


def project_facts(root: Path) -> dict[str, Any]:
    """Everything the check-up rules look at, read from the project."""
    settings = read_settings(root)
    config = read_json_file(runtime_dir(root) / "project.json")
    doc = prd_doc.Prd(root, runtime_dir(root))
    doc.migrate_legacy()
    prd_text = doc.read()
    platforms, recommend_pending = prd_doc.platforms(prd_text)
    try:
        stack = detect_project_stack(root)
        detected = "" if stack.language == "generic" else stack.display_name
    except Exception:
        detected = ""
    live = delivery_view.live(lambda *a: git(root, *a), base_branch(root, settings))
    jobs = list_jobs(root)
    features = feature_store.load(runtime_dir(root))
    with_jobs = {j["feature"] for j in jobs if j.get("feature")} | {j["feature"] for j in archived_feature_jobs(root)}
    cases = test_case_lib.load_library(root)
    gaps = 0
    if cases:
        cov = test_case_lib.coverage(root, cases, test_index(root))
        gaps = sum(1 for c in cases if c.get("type") != "manual" and cov[c["id"]]["status"] in ("unassigned", "planned"))
    kpis = [k for f in features for k in f.get("kpis") or []]
    pipeline = delivery_view.pipeline(lambda argv: gh_cached(root, argv), base_branch(root, settings))
    base_runs = [r for r in pipeline["runs"] if r["on_base"]]
    workflows = root / ".github" / "workflows"
    return {
        "prd_sections": {x["id"]: x["filled"] for x in prd_doc.sections_view(prd_text)} if prd_text else {}, "prd_auto_update": doc.auto_update(), "agents": any((root / n).is_file() for n in ("AGENTS.md", "CLAUDE.md", "GEMINI.md")),
        "readme": (root / "README.md").is_file(), "platforms": platforms, "recommend_pending": recommend_pending, "detected": detected,
        "git_repo": bool(git(root, "rev-parse", "--is-inside-work-tree")), "remote": bool(git(root, "remote", "get-url", "origin")),
        "tag": live["tag"] if live else None, "unreleased": live["unreleased"] if live else None,
        "jobs_total": len(jobs) + len(archived_feature_jobs(root)), "jobs_open": sum(1 for j in jobs if j["state"]["group"] != "done"),
        "jobs_unassigned": sum(1 for j in jobs if not j.get("feature") and j["state"]["group"] != "done"),
        "features": len(features), "suites": len(test_index(root)["suites"]), "cases_total": len(cases), "cases_gap": gaps,
        "kpis_total": len(kpis), "kpis_measured": sum(1 for k in kpis if k.get("measurements")),
        "features_needing_kpis": sum(1 for f in features if f["id"] in with_jobs and not f.get("kpis")),
        "ci": workflows.is_dir() and any(workflows.glob("*.y*ml")) or (root / "ci_scripts").is_dir() or (root / ".xcodecloud").exists(),
        "pipeline_failing": bool(base_runs) and base_runs[0]["tone"] == "failed",
        "distribution": bool(config.get("firebase_distribution")), "builds_sent": len(delivery_view.receipts(runtime_dir(root))),
    }


def features_overview(root: Path) -> dict[str, Any]:
    archived = archived_feature_jobs(root)
    jobs = list_jobs(root)
    features = feature_store.load(runtime_dir(root))
    files = {j["id"]: job_touched_files(read_json_file(jobs_dir(root) / f"{j['id']}.json"))
             for j in jobs if j.get("feature")}
    rolled = feature_store.rollup(features, jobs + archived)
    base = read_json_file(runtime_dir(root) / "project.json").get("base_branch") or "main"
    for f in rolled:
        branches = []
        for j in jobs:
            if j.get("feature") == f["id"] and j.get("branch") and j["status"] not in ("discarded", "archived") and j["branch"] not in branches:
                branches.append(j["branch"])
        result = integration_check.load_result(runtime_dir(root), f["id"])
        base_head = git(root, "rev-parse", f"refs/heads/{base}")
        f["combine"] = {"branches": branches, "result": result,
                        "stale": integration_check.is_stale(result, base_head, integration_check.branch_heads(root, branches))}
    return {"features": rolled, "archived_jobs": archived, "overlaps": feature_store.overlaps(features, jobs, files),
            "unassigned": [j["id"] for j in jobs if not j.get("feature") and j["state"]["group"] != "done"]}


def list_jobs(root: Path) -> list[dict[str, Any]]:
    directory = jobs_dir(root)
    if not directory.exists():
        return []
    jobs = []
    for path in directory.glob("*.json"):
        if re.search(r"_task_\d+$", path.stem):  # scratch copy the worker makes per task; not a job
            continue
        job = read_json_file(path)
        if job:
            jobs.append(job_summary(path, job))
    return sorted(jobs, key=lambda j: j["updated"], reverse=True)


def resolve_job_path(root: Path, job_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", job_id or ""):
        raise UIError("Invalid job id")
    path = jobs_dir(root) / f"{job_id}.json"
    if not path.is_file():
        raise UIError("Job not found", HTTPStatus.NOT_FOUND)
    return path


def delete_job(root: Path, job_id: str, revert: bool = False) -> None:
    job_path = resolve_job_path(root, job_id)
    job = read_json_file(job_path)

    if revert:
        ai_modified = job.get("ai_modified_files", [])
        ai_untracked = job.get("ai_untracked_files", [])
        for f in ai_modified:
            subprocess.run(["git", "checkout", "--", f], cwd=str(root), capture_output=True)
        for f in ai_untracked:
            full = root / f
            if full.exists():
                if full.is_dir():
                    shutil.rmtree(full, ignore_errors=True)
                else:
                    try:
                        full.unlink()
                    except OSError:
                        pass
        branch = job.get("branch")
        if branch and branch.startswith(("ai/issue-", "ai/job-")):  # only branches Orchestrator made for jobs
            try:
                curr = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(root)).decode("utf-8").strip()
            except Exception:
                curr = ""
            base_branch = job.get("base_branch")
            if not base_branch:
                try:
                    from orchestrator.project_config import load_project_config
                    base_branch = load_project_config(root).base_branch
                except Exception:
                    base_branch = "main"
            if curr == branch:
                subprocess.run(["git", "checkout", base_branch], cwd=str(root), capture_output=True)
            subprocess.run(["git", "branch", "-D", branch], cwd=str(root), capture_output=True)

    archive_dir = jobs_dir(root) / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / job_path.name
    if archive_path.exists():
        archive_path.unlink(missing_ok=True)
    job["status"] = "discarded"
    job["completed_at"] = datetime.now().isoformat()
    job.pop("_path", None)
    write_json_file(job_path, job)
    shutil.move(str(job_path), str(archive_path))


_TEST_SCAN_CACHE: dict[str, tuple[float, Any]] = {}
TEST_SCAN_TTL = 20.0


def test_index(root: Path) -> dict[str, Any]:
    """The repo's test code scan, reused for a few seconds (it walks every test file)."""
    key = str(root)
    hit = _TEST_SCAN_CACHE.get(key)
    if hit and time.time() - hit[0] < TEST_SCAN_TTL:
        return hit[1]
    index = test_case_lib.scan_tests(root)
    _TEST_SCAN_CACHE[key] = (time.time(), index)
    return index


def test_case_view(root: Path, cases: list[dict[str, Any]], due_ids: set[str] | None = None) -> dict[str, Any]:
    """Cases with their coverage status, plus counts by status and by kind of test."""
    cov = test_case_lib.coverage(root, cases, test_index(root))
    rows = []
    for c in cases:
        c_cov = cov[c["id"]]
        rows.append({"id": c["id"], "area": c.get("area") or "General", "title": c.get("title", ""), "type": c.get("type", "unit"),
                     "priority": c.get("priority", "medium"), "preconditions": c.get("preconditions") or [], "steps": c.get("steps") or [],
                     "expected": c.get("expected", ""), "covers": c.get("covers") or [], "task": c.get("task"),
                     "status": c_cov["status"], "found_in": c_cov["tests"], "assigned": c_cov["assigned"],
                     "due": due_ids is None or c["id"] in due_ids})
    by_type = {t: sum(1 for r in rows if r["type"] == t) for t in test_case_lib.CASE_TYPES}
    return {"cases": rows, "summary": {**test_case_lib.summarize(cases, cov), "by_type": by_type}}


def job_scope(root: Path, job: dict[str, Any]) -> dict[str, Any] | None:
    """Did the job's change stay inside its plan? None when there's no branch to compare."""
    branch, base = job.get("branch"), job.get("base_branch") or "main"
    if not branch or not git(root, "rev-parse", "--verify", "--quiet", branch):
        return None
    span = f"{base}...{branch}"
    changed = scope_check.parse_numstat(git(root, "diff", "--numstat", "--no-renames", span),
                                        git(root, "diff", "--name-status", "--no-renames", span))
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    tasks = [t for t in (plan.get("tasks") or []) if isinstance(t, dict)]
    planned_files = list(plan.get("likely_files") or [])
    for t in tasks:
        planned_files += t.get("likely_files") or []
    owned: list[str] = []
    if job.get("feature"):
        owned = next((f.get("paths", []) for f in feature_store.load(runtime_dir(root)) if f["id"] == job["feature"]), [])
    result = scope_check.evaluate([str(p) for p in planned_files], len(tasks), changed, owned, job.get("scope_accepted") or [])
    result["accepted"] = len(job.get("scope_accepted") or [])
    result["trim"] = {"summary": f"Trim “{job.get('title') or 'this job'}” back to its plan",
                      "spec": scope_check.trim_request([f for f in result["findings"] if f["files"]], job.get("title") or "this job")}
    return result


_PREFLIGHT_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
PREFLIGHT_TTL = 60.0


def is_apple_app(root: Path, config: dict[str, Any]) -> bool:
    try:
        return bool(config.get("xcode_project") or config.get("xcode_workspace") or any(root.glob("*.xcodeproj")) or any(root.glob("*.xcworkspace")) or (root / "Package.swift").is_file())
    except OSError:
        return False


def preflight_overview(root: Path, refresh: bool = False) -> list[dict[str, str]]:
    """Will this computer be able to build and test the project? Cheap probes, cached for a minute."""
    import shutil
    key = str(root)
    hit = _PREFLIGHT_CACHE.get(key)
    if hit and not refresh and time.time() - hit[0] < PREFLIGHT_TTL:
        return hit[1]
    search_path = tool_search_path()

    def run(argv: list[str]) -> tuple[int, str]:
        exe = shutil.which(argv[0], path=search_path)
        if not exe:
            return 127, ""
        try:
            res = subprocess.run([exe, *argv[1:]], cwd=root, capture_output=True, text=True, timeout=15, env={**os.environ, "PATH": search_path})
            return res.returncode, (res.stdout or "") + (res.stderr or "")
        except (OSError, subprocess.TimeoutExpired):
            return 124, "timed out"

    def model_clis(model: str) -> list[str]:
        try:
            from orchestrator.scripts.model_registry import get_model
            meta = get_model(model)
            return list(meta.required_clis) if meta else []
        except Exception:
            return []

    config = read_json_file(runtime_dir(root) / "project.json")
    machines = [m for m in read_json_file(runtime_dir(root) / "config" / "machines.json").get("machines", []) if isinstance(m, dict)]
    try:
        free = shutil.disk_usage(root).free / 1e9
    except OSError:
        free = None
    from orchestrator.code_host import origin_url, resolve_mode
    github = resolve_mode(config.get("code_host"), config.get("git_remote") or origin_url(root)) == "github"
    items = preflight.checks(config, machines, is_apple_app(root, config), free, run, lambda t: shutil.which(t, path=search_path) is not None,
                             model_clis, github=github)
    _PREFLIGHT_CACHE[key] = (time.time(), items)
    return items


def job_blockers(root: Path, job: dict[str, Any]) -> list[dict[str, str]]:
    import shutil
    from orchestrator import run_check
    try:
        free = shutil.disk_usage(root).free / 1e9
    except OSError:
        free = None
    machines = [m for m in read_json_file(runtime_dir(root) / "config" / "machines.json").get("machines", []) if isinstance(m, dict)]
    found = run_check.blockers(job, machines, free, run_check.MIN_DISK_GB, run_check.model_names_overlap)
    if job.get("status") in run_check.CHECKED_STATUSES:
        have = {b["id"] for b in found}
        for item in preflight.failures(preflight_overview(root)):
            if item["id"] != "disk" and item["id"] not in have:  # disk is already said, with this job's numbers
                hint = "" if item["route"] or not item["fix"] else f" {item['fix']}."
                found.append({"id": item["id"], "text": f"{item['title']}. {item['detail']}{hint}", "fix": item["fix"] if item["route"] else "", "route": item["route"]})
    return found


def job_detail(root: Path, job_id: str) -> dict[str, Any]:
    path = resolve_job_path(root, job_id)
    job = read_json_file(path)
    out_dir = runtime_dir(root) / "output" / job_id
    outputs = []
    if out_dir.is_dir():
        for f in sorted(out_dir.rglob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:60]:
            if f.is_file():
                outputs.append({"path": str(f.relative_to(runtime_dir(root))), "size": f.stat().st_size,
                                "mtime": f.stat().st_mtime})

    test_summary = None
    try:
        from orchestrator.scripts.common import get_job_test_summary
        test_summary = get_job_test_summary(job)
    except Exception:
        test_summary = {
            "status": job.get("test_status") or "pending",
            "created_count": 0,
            "passed_count": 0,
            "failed_count": 0,
            "failing_tests": [],
        }

    pipeline = {
        "planner": job.get("planner") or None,
        "builder": job.get("builder") or None,
        "reviewer": job.get("reviewer") or None,
    }

    tasks = job_task_list(job)
    completed = job.get("completed_tasks") or job.get("completed_task_indices") or []
    completed = completed if isinstance(completed, list) else []
    next_task = None
    if len(completed) < len(tasks):
        next_t = tasks[len(completed)]
        next_task = (next_t.get("name") or next_t.get("title") or next_t.get("description")) if isinstance(next_t, dict) else str(next_t)

    return {
        "summary": job_summary(path, job),
        "job": job,
        "outputs": outputs,
        "logs": [resolve_linked_log(root, ref) for ref in linked_logs(job)],
        "docs": job_documents(out_dir, root=root, job=job),
        "changes": job_changes(root, job),
        "links": github_links(root, job),
        "test_summary": test_summary,
        "scope": job_scope(root, job),
        "test_cases": test_case_view(root, test_case_lib.job_cases(job), {c["id"] for c in test_case_lib.due_cases(job)}),
        "pipeline": pipeline,
        "blockers": job_blockers(root, job),
        "tasks": tasks,
        "undoable_task": task_revert.latest_revertable(job),
        "completed_tasks": completed,
        "next_task": next_task,
        "approach": job.get("approach") or job.get("execution_strategy") or "Standard workflow",
    }


def read_limited(path: Path, limit: int = 200_000) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


def generate_job_brief(job: dict[str, Any]) -> str:
    try:
        import sys
        scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from run_builder import make_brief
        return make_brief(job)
    except Exception:
        title = job.get("title") or job.get("job_id") or "Untitled Job"
        desc = job.get("description") or job.get("prompt") or "No description provided."
        return f"# Brief: {title}\n\n{desc}\n"


def job_documents(out_dir: Path, root: Path | None = None, job: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The console's "View Brief / Summary": brief, builder summary, investigations."""
    brief_path = out_dir / "brief.md"
    if not brief_path.is_file() and job is not None:
        try:
            brief_text = generate_job_brief(job)
            if brief_text and brief_text.strip():
                out_dir.mkdir(parents=True, exist_ok=True)
                brief_path.write_text(brief_text, encoding="utf-8")
        except Exception:
            pass

    docs = []
    for name, title in (("brief.md", "Brief"), ("builder_summary.md", "Builder summary"), ("investigations.md", "Investigations")):
        p = out_dir / name
        text = read_limited(p)
        if text and text.strip():
            doc: dict[str, Any] = {"title": title, "name": name, "text": text}
            if root is not None:
                try:
                    doc["path"] = str(p.relative_to(root))
                except ValueError:
                    doc["path"] = str(p)
                try:
                    doc["runtime_path"] = str(p.relative_to(runtime_dir(root)))
                except ValueError:
                    doc["runtime_path"] = str(p)
            else:
                doc["path"] = str(p)
                doc["runtime_path"] = name
            docs.append(doc)
    return docs


def job_changes(root: Path, job: dict[str, Any]) -> dict[str, Any]:
    """The console's "View AI Changes": files the AI touched plus a diffstat of its branch."""
    files = [*(job.get("ai_modified_files") or []), *(job.get("ai_untracked_files") or [])]
    branch = job.get("branch")
    base = job.get("base_branch") or read_json_file(runtime_dir(root) / "project.json").get("base_branch") or "main"
    diffstat = ""
    summary_line = ""
    changed_files = []
    if branch and re.fullmatch(r"[A-Za-z0-9._/-]+", branch) and re.fullmatch(r"[A-Za-z0-9._/-]+", base):
        diffstat = git(root, "diff", "--stat", "--stat-width=100", f"{base}...{branch}", "--")
        summary_line = git(root, "diff", "--shortstat", f"{base}...{branch}", "--")
        changed_files = [f for f in git(root, "diff", "--name-only", f"{base}...{branch}", "--").splitlines() if f.strip()]

    local_summary = git(root, "diff", "--shortstat", "HEAD", "--")
    local_files = [f for f in git(root, "diff", "--name-only", "HEAD", "--").splitlines() if f.strip()]

    # Orchestrator's own state (jobs, receipts, config) isn't part of the work being reviewed.
    all_impacted = sorted(f for f in set(changed_files + local_files + [str(f) for f in files]) if not f.startswith(".orchestrator/"))

    return {
        "files": all_impacted[:200],
        "diffstat": diffstat[-20_000:],
        "summary_line": summary_line,
        "local_summary": local_summary,
        "local_files": local_files,
        "base": base,
        "hypothesis": job.get("builder_hypothesis") or "",
    }


MAX_DIFF_CHARS = 60_000


def job_file_diff(root: Path, job: dict[str, Any], path: str) -> dict[str, Any]:
    """One file's diff for a job: its branch against the base, or its uncommitted edits. Only files that
    are actually part of the job's changes can be asked for."""
    changes = job_changes(root, job)
    branch = job.get("branch")
    base = changes["base"]
    in_branch = bool(branch and re.fullmatch(r"[A-Za-z0-9._/-]+", branch) and re.fullmatch(r"[A-Za-z0-9._/-]+", base)
                     and path in git(root, "diff", "--name-only", f"{base}...{branch}", "--").splitlines())
    if in_branch:
        text = subprocess.run(["git", "diff", "--no-color", "-U3", f"{base}...{branch}", "--", path], cwd=root, capture_output=True, text=True, timeout=10).stdout
    elif path in changes["local_files"]:
        text = subprocess.run(["git", "diff", "--no-color", "-U3", "HEAD", "--", path], cwd=root, capture_output=True, text=True, timeout=10).stdout
    else:
        raise UIError("That file isn't part of this job's changes", HTTPStatus.NOT_FOUND)
    binary = text.startswith("Binary files") or "\nBinary files " in text
    return {"path": path, "source": "branch" if in_branch else "local", "binary": binary,
            "diff": "" if binary else text[:MAX_DIFF_CHARS], "truncated": len(text) > MAX_DIFF_CHARS}


def repo_web_url(root: Path) -> str | None:
    remote = read_json_file(runtime_dir(root) / "project.json").get("git_remote") or git(root, "remote", "get-url", "origin")
    match = re.match(r"^(?:git@([^:]+):|https?://(?:[^@/]+@)?([^/]+)/)([^/]+/[^/]+?)(?:\.git)?/?$", remote or "")
    if not match:
        return None
    host = match.group(1) or match.group(2)
    return f"https://{host}/{match.group(3)}"


def github_links(root: Path, job: dict[str, Any]) -> list[dict[str, str]]:
    base = repo_web_url(root)
    links = []
    for kind, key, path in (("Issue", "issue_number", "issues"), ("Pull request", "pr_number", "pull")):
        number = job.get(key)
        url = job.get("issue_url" if key == "issue_number" else "pr_url") or (f"{base}/{path}/{number}" if base and number else None)
        if number and url and str(url).startswith("https://"):
            links.append({"label": f"{kind} #{number}", "url": url})
    request = str(job.get("merge_request_url") or "")
    if request.startswith("https://") and not job.get("pr_number"):  # plain git: open a merge request on the host
        from orchestrator.code_host import host_kind
        kind = host_kind(request)
        links.append({"label": "pull request" if kind in ("github", "bitbucket") else "merge request", "url": request,
                      "where": {"github": "On GitHub", "gitlab": "On GitLab", "bitbucket": "On Bitbucket"}.get(kind, "On your git host")})
    return links


# --------------------------------------------------------------------------- documentation hub


def _all_job_files(root: Path) -> dict[str, Path]:
    """Every job file by id: running and waiting ones, and the finished ones in the archive."""
    found: dict[str, Path] = {}
    for folder in (jobs_dir(root) / "archive", jobs_dir(root)):  # a live job wins over an archived copy of the same id
        if folder.is_dir():
            for path in folder.glob("*.json"):
                if not re.search(r"_task_\d+$", path.stem):
                    found[path.stem] = path
    return found


def _blurb(job: dict[str, Any], n: int = 170) -> str:
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    text = re.sub(r"\s+", " ", str(plan.get("summary") or job.get("raw_input") or "")).strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def _job_review(root: Path, job: dict[str, Any]) -> str:
    n = job.get("pr_number")
    for rel in ((f"output/pr-{n}/review.md",) if n else ()) + (f"output/{job.get('job_id')}/review.md",):
        text = read_limited(runtime_dir(root) / rel, 12_000)
        if text and text.strip():
            return text
    return ""


def docs_job_markdown(root: Path, job_id: str) -> tuple[str, str]:
    path = _all_job_files(root).get(job_id)
    if not path:
        raise UIError("That job isn't here.", HTTPStatus.NOT_FOUND)
    job = read_json_file(path)
    features = {f["id"]: f.get("name") or f["id"] for f in feature_store.load(runtime_dir(root))}
    links = [{"title": l["label"], "url": l["url"]} for l in github_links(root, job)]
    links += [{"title": l.get("title") or l.get("ref"), "url": l.get("url", "")} for l in job.get("external_links") or [] if isinstance(l, dict)]
    changed = list(dict.fromkeys([*(job.get("ai_modified_files") or []), *(job.get("ai_untracked_files") or [])]))
    return str(job.get("title") or job_id), project_docs.job_doc(job, _job_review(root, job), changed, features.get(job.get("feature") or "", ""), links)


def docs_feature_markdown(root: Path, feature_id: str) -> tuple[str, str]:
    features = feature_store.load(runtime_dir(root))
    jobs = []
    for jid, path in _all_job_files(root).items():
        job = read_json_file(path)
        if job.get("feature") == feature_id:
            jobs.append({**job_summary(path, job), "blurb": _blurb(job)})
    jobs.sort(key=lambda j: j["updated"])
    rolled = feature_store.rollup(features, jobs)
    mine = next((f for f in rolled if f["id"] == feature_id), None)
    if not mine:
        raise UIError("That feature isn't here.", HTTPStatus.NOT_FOUND)
    return str(mine.get("name") or feature_id), project_docs.feature_doc(mine, jobs, {f["id"]: f.get("name") or f["id"] for f in features})


def docs_index(root: Path) -> dict[str, Any]:
    doc = prd_doc.Prd(root, runtime_dir(root))
    doc.migrate_legacy()
    features = feature_store.load(runtime_dir(root))
    names = {f["id"]: f.get("name") or f["id"] for f in features}
    jobs = []
    for jid, path in _all_job_files(root).items():
        job = read_json_file(path)
        if not job:
            continue
        s = job_summary(path, job)
        jobs.append({"id": jid, "title": s["title"], "kind": s["kind"], "status": s["status"], "label": (s.get("state") or {}).get("label") or s["status"],
                     "group": (s.get("state") or {}).get("group"), "updated": s["updated"], "feature": job.get("feature") or "", "feature_name": names.get(job.get("feature") or "", ""),
                     "blurb": _blurb(job), "archived": path.parent.name == "archive"})
    jobs.sort(key=lambda j: j["updated"], reverse=True)
    by_feature: dict[str, list[dict[str, Any]]] = {}
    for j in jobs:
        by_feature.setdefault(j["feature"], []).append(j)
    sections = prd_doc.sections_view(doc.read())
    return {"product": {"exists": doc.exists(), "written": sum(1 for x in sections if x["filled"]), "total": len(sections), "path": prd_doc.PATH,
                        "pitch": next((x["body"] for x in sections if x["id"] == "pitch" and x["filled"]), "")[:240]},
            "features": [{"id": f["id"], "name": f.get("name") or f["id"], "status": f.get("status"), "summary": str(f.get("summary") or "")[:200],
                          "jobs": len(by_feature.get(f["id"], [])), "jobs_done": sum(1 for j in by_feature.get(f["id"], []) if j["group"] == "done")} for f in features],
            "jobs": jobs, "files": project_docs.project_files(root)}


def docs_export(root: Path) -> str:
    doc = prd_doc.Prd(root, runtime_dir(root))
    idx = docs_index(root)
    feats = [(f, docs_feature_markdown(root, f["id"])[1]) for f in idx["features"]]
    jobs = [(j, docs_job_markdown(root, j["id"])[1]) for j in idx["jobs"]]
    files = [(f["path"], project_docs.read_project_file(root, f["path"])) for f in idx["files"] if f["path"] != prd_doc.PATH]
    return project_docs.export_all(project_display_name(root), doc.read(), feats, jobs, files)


def git_state(root: Path) -> dict[str, Any]:
    branch = git(root, "branch", "--show-current")
    counts = git(root, "rev-list", "--left-right", "--count", "@{upstream}...HEAD").split()
    behind, ahead = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (None, None)
    # Raw, NUL-separated: `git()` strips output, which would eat the leading
    # space of the first porcelain line and misalign its path.
    try:
        raw = subprocess.run(["git", "status", "--porcelain=v1", "-z"], cwd=root, capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        raw = ""
    entries = raw.split("\0")
    status, i = [], 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) > 3:
            status.append(entry)
            if entry[0] in "RC":  # renames/copies carry the source path as the next entry
                i += 1
        i += 1
    return {
        "branch": branch,
        "upstream": git(root, "rev-parse", "--abbrev-ref", "@{upstream}") or None,
        "ahead": ahead, "behind": behind,
        "last_commit": git(root, "log", "-1", "--format=%h %s (%an, %cr)"),
        "changes": [{"status": l[:2].strip() or "?", "path": l[3:]} for l in status[:100]],
        "changes_total": len(status),
        "branches": [b for b in git(root, "branch", "--format=%(refname:short)").splitlines() if b][:200],
        "elsewhere": branches_elsewhere(root),
        "web_url": repo_web_url(root),
    }


def linked_logs(job: dict[str, Any]) -> list[str]:
    logs = job.get("last_manual_log_paths") or []
    if not logs and job.get("last_manual_log_path"):
        logs = [job["last_manual_log_path"]]
    return [str(l) for l in logs if l]


def resolve_linked_log(root: Path, ref: str) -> dict[str, Any]:
    """A linked log is a file, a directory of *.log files, or a `cloud:` ref.
    Returns the viewable files (runtime-dir relative) behind it."""
    entry: dict[str, Any] = {"ref": ref, "files": []}
    if ref.startswith("cloud:"):
        entry["label"] = "Newest device launch (pulled on each fix run)" if ref == "cloud:latest" else f"Device launch {ref[6:]}"
        return entry
    base = safe_resolve(runtime_dir(root))
    candidate = safe_resolve(Path(ref) if Path(ref).is_absolute() else root / ref)
    entry["label"] = candidate.name
    if candidate.parent.name == "cloud_logs":
        meta = read_json_file(candidate / "meta.json")
        session = meta.get("session") or candidate.name.rsplit("-", 1)[-1]
        entry["label"] = f"Device logs · launch {session}"
    if base not in candidate.parents and candidate != base:
        return entry
    files = sorted(candidate.glob("*.log")) if candidate.is_dir() else ([candidate] if candidate.is_file() else [])
    entry["files"] = [str(f.relative_to(base)) for f in files[:10]]
    return entry


def resolve_runtime_file(root: Path, rel: str) -> Path:
    base = safe_resolve(runtime_dir(root))
    target = safe_resolve(base / rel)
    if base not in target.parents or not target.is_file():
        raise UIError("File not found", HTTPStatus.NOT_FOUND)
    return target


def device_log_pulls(root: Path) -> list[dict[str, Any]]:
    base = runtime_dir(root) / "output" / "cloud_logs"
    if not base.is_dir():
        return []
    pulls = []
    for d in sorted((p for p in base.iterdir() if p.is_dir() and not p.is_symlink()), reverse=True)[:20]:
        meta = read_json_file(d / "meta.json")
        pulls.append({"dir": d.name, "path": str((d / "cloud.log").relative_to(runtime_dir(root))),
                      "session": meta.get("session"), "rows": meta.get("rows"), "mtime": d.stat().st_mtime})
    return pulls


def detect_project_source(root: Path, p_config: dict[str, Any] | None = None) -> tuple[str, str, str | None]:
    """Determine if a project is GitHub tracked vs Local."""
    github_repo: str | None = None
    if isinstance(p_config, dict):
        cfg_repo = p_config.get("github_repo") or (p_config.get("github") or {}).get("repo")
        if cfg_repo and isinstance(cfg_repo, str) and "/" in cfg_repo:
            github_repo = cfg_repo.strip()

    has_git = (root / ".git").exists()
    if not github_repo and has_git:
        origin_url = git(root, "config", "--get", "remote.origin.url").strip()
        if origin_url and "github.com" in origin_url:
            m = re.search(r"github\.com[:/]([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", origin_url)
            if m:
                github_repo = m.group(1).removesuffix(".git")
            else:
                github_repo = "GitHub Tracked"

    if github_repo:
        return "github", "GitHub Tracked", github_repo
    return "local", "Local", None


GITHUB_LANG_COLORS: dict[str, str] = {
    "Swift": "#F05138",
    "Python": "#3572A5",
    "JavaScript": "#F1E05A",
    "TypeScript": "#3178C6",
    "Shell": "#89E051",
    "Bash": "#89E051",
    "HTML": "#E34C26",
    "CSS": "#563D7C",
    "C": "#555555",
    "C++": "#F34B7D",
    "Objective-C": "#438EFF",
    "Objective-C++": "#6866FB",
    "Rust": "#DEA584",
    "Go": "#00ADD8",
    "Ruby": "#701516",
    "Kotlin": "#A97BFF",
    "Java": "#B07219",
    "Dart": "#00B4AB",
    "PHP": "#4F5D95",
    "Perl": "#0298C3",
    "Lua": "#000080",
}

_LANG_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def scan_local_languages(root: Path) -> list[dict[str, Any]]:
    """Scan local directory files by extension to estimate language percentages."""
    if not root.is_dir():
        return []
    ext_to_lang = {
        ".swift": "Swift",
        ".py": "Python",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".sh": "Shell",
        ".bash": "Shell",
        ".zsh": "Shell",
        ".html": "HTML",
        ".css": "CSS",
        ".c": "C",
        ".h": "C",
        ".cpp": "C++",
        ".m": "Objective-C",
        ".rs": "Rust",
        ".go": "Go",
        ".rb": "Ruby",
        ".kt": "Kotlin",
        ".java": "Java",
    }
    ignored_dirs = {".git", ".build", "node_modules", "Pods", "DerivedData", ".venv", "venv", "__pycache__", ".orchestrator", "dist", "build"}
    byte_counts: dict[str, int] = {}
    try:
        for r, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".")]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in ext_to_lang:
                    lang_name = ext_to_lang[ext]
                    try:
                        f_size = os.path.getsize(os.path.join(r, f))
                        byte_counts[lang_name] = byte_counts.get(lang_name, 0) + f_size
                    except OSError:
                        pass
        total = sum(byte_counts.values())
        langs = []
        if total > 0:
            for lang_name, byte_count in sorted(byte_counts.items(), key=lambda x: -x[1]):
                pct = round((byte_count / total) * 100, 1)
                if pct >= 0.1:
                    color = GITHUB_LANG_COLORS.get(lang_name, "#8B949E")
                    langs.append({
                        "name": lang_name,
                        "bytes": byte_count,
                        "percent": pct,
                        "color": color,
                    })
        return langs
    except Exception:
        return []


def get_project_languages(root: Path, github_repo: str | None = None) -> list[dict[str, Any]]:
    """Fetch or calculate language percentages for a project, with in-memory caching."""
    cache_key = github_repo or str(safe_resolve(root))
    now = time.time()
    if cache_key in _LANG_CACHE:
        cached_time, cached_langs = _LANG_CACHE[cache_key]
        if now - cached_time < 3600:
            return cached_langs

    langs: list[dict[str, Any]] = []

    # 1. If github_repo, try `gh api repos/{github_repo}/languages`
    if github_repo and "/" in github_repo:
        try:
            res = subprocess.run(
                ["gh", "api", f"repos/{github_repo}/languages"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout)
                if isinstance(data, dict):
                    total = sum(data.values())
                    if total > 0:
                        for name, byte_count in data.items():
                            pct = round((byte_count / total) * 100, 1)
                            if pct >= 0.1:
                                color = GITHUB_LANG_COLORS.get(name, "#8B949E")
                                langs.append({
                                    "name": name,
                                    "bytes": byte_count,
                                    "percent": pct,
                                    "color": color,
                                })
        except Exception:
            pass

    # 2. If no GitHub data, fallback to local file extension scanning
    if not langs and root.is_dir():
        langs = scan_local_languages(root)

    _LANG_CACHE[cache_key] = (now, langs)
    return langs


def is_mobile_app(root: Path, config: dict[str, Any]) -> bool:
    """True when the project builds an app for a phone or simulator, which is when device logs and simulator checks mean something."""
    if config.get("xcode_project") or config.get("xcode_workspace") or config.get("app_bundle_id") or config.get("remote_logs"):
        return True
    try:
        if any(root.glob("*.xcodeproj")) or any(root.glob("*.xcworkspace")) or (root / "Package.swift").is_file():
            return True
        return (root / "android").is_dir() or (root / "app" / "src" / "main" / "AndroidManifest.xml").is_file() or (root / "ios").is_dir()
    except OSError:
        return False


def project_state(root: Path) -> dict[str, Any]:
    config = read_json_file(runtime_dir(root) / "project.json")
    status = git(root, "status", "--porcelain")
    recent = load_recent_projects().get("projects", [])
    machines = [m for m in read_json_file(runtime_dir(root) / "config" / "machines.json").get("machines", []) if isinstance(m, dict)]
    root_source_type, root_source_label, root_github_repo = detect_project_source(root, config)
    root_languages = get_project_languages(root, root_github_repo if root_source_type == "github" else None)

    recent_entries = []
    for p in recent:
        r_str = p.get("root")
        s_type, s_label, s_repo = "local", "Local", None
        langs_p = []
        if r_str:
            try:
                p_r = safe_resolve(Path(r_str).expanduser())
                p_cfg = read_json_file(runtime_dir(p_r) / "project.json") if (runtime_dir(p_r) / "project.json").is_file() else None
                s_type, s_label, s_repo = detect_project_source(p_r, p_cfg)
                langs_p = get_project_languages(p_r, s_repo if s_type == "github" else None)
            except Exception:
                pass
        recent_entries.append({
            "name": p.get("name"),
            "root": p.get("root"),
            "source_type": s_type,
            "source_label": s_label,
            "github_repo": s_repo,
            "languages": langs_p,
        })

    return {
        "machine_count": len(machines),
        "model_count": len({model for m in machines for model in m.get("models", [])}),
        "name": project_display_name(root),
        "root": str(root),
        "branch": git(root, "branch", "--show-current"),
        "branches": [b for b in git(root, "branch", "--format=%(refname:short)").splitlines() if b][:200],
        "elsewhere": branches_elsewhere(root),
        "dirty_files": len([l for l in status.splitlines() if l.strip()]),
        "scheme": config.get("scheme"),
        "firebase_distribution": bool(config.get("firebase_distribution")),
        "remote_logs": bool(config.get("remote_logs")),
        "mobile_app": is_mobile_app(root, config),
        "configured": bool(config),
        "source_type": root_source_type,
        "source_label": root_source_label,
        "github_repo": root_github_repo,
        "languages": root_languages,
        "recent": recent_entries,
    }


def all_projects_info(active_root: Path) -> list[dict[str, Any]]:
    recent = load_recent_projects().get("projects", [])
    active_resolved = safe_resolve(active_root)
    results = []
    seen = set()
    for p in recent:
        r_str = p.get("root")
        if not r_str:
            continue
        try:
            p_root = Path(r_str).expanduser()
            if not p_root.is_dir():
                continue
            p_resolved = safe_resolve(p_root)
        except Exception:
            continue
        if str(p_resolved) in seen:
            continue
        seen.add(str(p_resolved))
        is_active = (p_resolved == active_resolved)
        p_config = read_json_file(runtime_dir(p_resolved) / "project.json")
        has_git = (p_resolved / ".git").exists()
        p_status = git(p_resolved, "status", "--porcelain") if has_git else ""
        p_branch = git(p_resolved, "branch", "--show-current") if has_git else None

        active_jobs_count = 0
        needs_you_count = 0
        if (runtime_dir(p_resolved) / "jobs").is_dir():
            try:
                for job in list_jobs(p_resolved):
                    active_jobs_count += 1
                    if (job.get("state") or {}).get("group") == "needs_you":
                        needs_you_count += 1
            except Exception:
                pass

        source_type, source_label, github_repo = detect_project_source(p_resolved, p_config)
        p_languages = get_project_languages(p_resolved, github_repo if source_type == "github" else None)
        results.append({
            "name": p.get("name") or project_display_name(p_resolved),
            "root": str(p_resolved),
            "active": is_active,
            "configured": bool(p_config),
            "branch": p_branch,
            "dirty_files": len([l for l in p_status.splitlines() if l.strip()]),
            "jobs_count": active_jobs_count,
            "needs_you_count": needs_you_count,
            "source_type": source_type,
            "source_label": source_label,
            "github_repo": github_repo,
            "languages": p_languages,
        })

    if str(active_resolved) not in seen and active_resolved.is_dir():
        p_config = read_json_file(runtime_dir(active_resolved) / "project.json")
        has_git = (active_resolved / ".git").exists()
        p_status = git(active_resolved, "status", "--porcelain") if has_git else ""
        p_branch = git(active_resolved, "branch", "--show-current") if has_git else None
        active_jobs_count = 0
        needs_you_count = 0
        if (runtime_dir(active_resolved) / "jobs").is_dir():
            try:
                for job in list_jobs(active_resolved):
                    active_jobs_count += 1
                    if (job.get("state") or {}).get("group") == "needs_you":
                        needs_you_count += 1
            except Exception:
                pass
        active_source_type, active_source_label, active_github_repo = detect_project_source(active_resolved, p_config)
        active_languages = get_project_languages(active_resolved, active_github_repo if active_source_type == "github" else None)
        results.insert(0, {
            "name": project_display_name(active_resolved),
            "root": str(active_resolved),
            "active": True,
            "configured": bool(p_config),
            "branch": p_branch,
            "dirty_files": len([l for l in p_status.splitlines() if l.strip()]),
            "jobs_count": active_jobs_count,
            "needs_you_count": needs_you_count,
            "source_type": active_source_type,
            "source_label": active_source_label,
            "github_repo": active_github_repo,
            "languages": active_languages,
        })
    return results


def scan_for_projects(search_paths: list[Path] | None = None) -> list[dict[str, Any]]:
    known = {str(safe_resolve(Path(p["root"]).expanduser()))
             for p in load_recent_projects().get("projects", []) if p.get("root")}
    if search_paths is None:
        home = Path.home()
        candidates = [
            home / "Developer",
            home / "Projects",
            home / "Documents",
            home / "Code",
            home / "src",
            home / "Desktop",
            home,
        ]
        search_paths = [p for p in candidates if p.is_dir()]

    discovered = []
    seen = set(known)
    ignored_names = {
        ".git", "node_modules", "Pods", "DerivedData", "Library", ".Trash",
        "build", ".build", "venv", ".venv", "dist", ".cache", "tmp", ".gemini",
        "Applications", "Movies", "Music", "Pictures", "System"
    }

    for base in search_paths:
        try:
            for root_dir, dirs, _files in os.walk(base, followlinks=False):
                dirs[:] = [d for d in dirs if d not in ignored_names and not d.startswith(".")]
                curr_path = Path(root_dir)
                try:
                    curr_resolved = safe_resolve(curr_path)
                except Exception:
                    continue
                curr_str = str(curr_resolved)

                try:
                    rel_parts = curr_path.relative_to(base).parts
                    if len(rel_parts) > 2:
                        dirs.clear()
                        continue
                except ValueError:
                    continue

                if curr_str in seen:
                    continue

                is_orchestrator = (curr_resolved / DEFAULT_RUNTIME_DIRNAME / "project.json").is_file()
                has_xcode = any(curr_resolved.glob("*.xcodeproj")) or (curr_resolved / "Package.swift").is_file()
                is_git = (curr_resolved / ".git").is_dir()

                if is_orchestrator or has_xcode or (is_git and len(rel_parts) > 0):
                    seen.add(curr_str)
                    name = project_display_name(curr_resolved) if is_orchestrator else curr_resolved.name
                    p_cfg = read_json_file(runtime_dir(curr_resolved) / "project.json") if is_orchestrator else None
                    s_type, s_label, s_repo = detect_project_source(curr_resolved, p_cfg)
                    s_languages = get_project_languages(curr_resolved, s_repo if s_type == "github" else None)
                    discovered.append({
                        "name": name,
                        "root": curr_str,
                        "configured": is_orchestrator,
                        "type": "orchestrator" if is_orchestrator else ("swift" if has_xcode else "git"),
                        "tracked": curr_str in known,
                        "source_type": s_type,
                        "source_label": s_label,
                        "github_repo": s_repo,
                        "languages": s_languages,
                    })
                    dirs.clear()
        except (PermissionError, OSError):
            continue

    return discovered


# --------------------------------------------------------------------------- actions


@dataclass
class Action:
    title: str
    build: Callable[[dict[str, Any], Path], list[str]]
    # Shown in a confirm dialog before running: set for anything outward-facing.
    confirm: str | None = None
    fields: list[str] = field(default_factory=list)


def orchestrator_argv(*args: str) -> list[str]:
    return [sys.executable, "-P", "-m", "orchestrator", *args]


def _text(params: dict[str, Any], key: str, required: bool = False, limit: int = 20_000) -> str:
    value = params.get(key)
    value = "" if value is None else str(value).strip()
    if required and not value:
        raise UIError(f"'{key}' is required")
    if len(value) > limit:
        raise UIError(f"'{key}' is too long")
    return value


def _choice(params: dict[str, Any], key: str, choices: list[str], default: str | None = None) -> str | None:
    value = params.get(key) or default
    if value is not None and value not in choices:
        raise UIError(f"'{key}' must be one of: {', '.join(choices)}")
    return value


def _job_path(params: dict[str, Any], root: Path) -> str:
    return str(resolve_job_path(root, _text(params, "job", required=True)))


def _session_id(params: dict[str, Any]) -> str | None:
    session = _text(params, "session")
    if session and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session):
        raise UIError("Invalid session id")
    return session or None


def feature_context(chosen: dict[str, Any], features: list[dict[str, Any]]) -> str:
    """What the planner should know about where this work sits among the product's features."""
    names = {f["id"]: f["name"] for f in features}
    lines = ["", "", "## Feature context", f"This work belongs to the feature \"{chosen['name']}\"."]
    if chosen.get("summary"):
        lines.append(f"What it does: {chosen['summary']}")
    if chosen.get("serves"):
        lines.append(f"It serves this use case: {chosen['serves']}. Say so in the plan's summary, and check each task still serves it.")
    if chosen.get("stories"):
        lines.append("Its user stories (the plan should deliver these):\n" + "\n".join(f"- {s}" for s in chosen["stories"]))
    if chosen.get("paths"):
        lines.append("It owns these paths; keep changes inside them where you can: " + ", ".join(chosen["paths"]))
    deps = [names[d] for d in chosen.get("depends_on") or [] if d in names]
    if deps:
        lines.append("It builds on: " + ", ".join(deps) + ". Use their existing interfaces; don't change them unless required.")
    others = [f for f in features if f["id"] != chosen["id"] and f.get("paths")]
    if others:
        lines.append("Other features own these paths, so avoid editing them (call out in the plan if you must): "
                     + "; ".join(f"{f['name']}: {', '.join(f['paths'])}" for f in others))
    return "\n".join(lines)


def runtime_file(root: Path, rel: str) -> Path:
    """A file inside the project's runtime folder, given a project-relative path; anything else is refused."""
    base = safe_resolve(runtime_dir(root))
    try:
        target = safe_resolve(root / str(rel))
    except Exception:
        raise UIError("That attachment isn't available")
    if base not in target.parents or not target.is_file():
        raise UIError("That attachment isn't available")
    return target


def upload_display_name(path: Path) -> str:
    """The name the person gave the file, without the timestamp prefix we add when saving it."""
    return re.sub(r"^\d{8}-\d{6}-[0-9a-f]{6}-", "", path.name)


def build_new_job(params: dict[str, Any], root: Path) -> list[str]:
    job_type = _choice(params, "type", JOB_TYPES, "bug")
    summary_in = _text(params, "summary", required=True, limit=20_000 if job_type == "quick" else 500)
    fields = {"summary": summary_in, "details": _text(params, "spec", limit=200_000), "repro": _text(params, "repro", limit=20_000),
              "expected": _text(params, "expected", limit=20_000), "vibe": _text(params, "vibe", limit=200),
              "subsystems": _text(params, "subsystems", limit=5_000)}
    try:
        summary, spec = new_job_form.compose(job_type, fields)
    except new_job_form.FormError as exc:
        raise UIError(str(exc))
    block, linked = context_for_new_job(root, _clean_links(params.get("links")))
    params["_linked"] = linked  # picked up by _start_run to record the links on the created job
    spec += block

    # Attachments: logs chosen from the recent list, and files uploaded just now. Both live in the runtime folder.
    log_rels = [str(p) for p in (params.get("logs") or []) if isinstance(p, str)][:10]
    file_rels = [str(p) for p in (params.get("files") or []) if isinstance(p, str)][:10]
    logs, files = [], []
    for rel in log_rels + [r for r in file_rels if new_job_form.upload_kind(r) == "log"]:
        path = runtime_file(root, rel)
        logs.append((upload_display_name(path), path.read_text(encoding="utf-8", errors="replace")))
    refs = []
    for rel in file_rels:
        if new_job_form.upload_kind(rel) == "log":
            continue
        path = runtime_file(root, rel)
        rel_to_root = str(path.relative_to(safe_resolve(root)))
        refs.append({"path": rel_to_root, "name": upload_display_name(path), "type": new_job_form.reference_type(rel)})
        files.append((upload_display_name(path), new_job_form.reference_type(rel), rel_to_root))
    for url in [u.strip() for u in (params.get("urls") or []) if isinstance(u, str) and re.fullmatch(r"https?://\S{1,2000}", u.strip())][:5]:
        kind = "figma_url" if "figma.com" in urlparse(url).netloc else "url_reference"
        refs.append({"url": url, "name": url, "type": kind})
        files.append((url, kind, url))
    spec += new_job_form.attachments_block(logs, files)
    all_logs = [str(runtime_file(root, r).relative_to(safe_resolve(root))) for r in log_rels + [r for r in file_rels if new_job_form.upload_kind(r) == "log"]]
    params["_attachments"] = {"logs": all_logs, "refs": refs}

    feature = str(params.get("feature") or "").strip()
    if feature:
        features = feature_store.load(runtime_dir(root))
        try:
            chosen = feature_store.get(features, feature)
        except feature_store.FeatureError:
            raise UIError("That feature no longer exists")
        params["_feature"] = feature
        provider = analytics.PROVIDERS.get(read_settings(root).get("analytics_provider", ""), {}).get("name")
        spec += feature_context(chosen, features) + analytics.instrumentation_context(chosen, provider)
    if job_type in new_job_form.LATITUDE_TYPES:
        spec += new_job_form.LATITUDE_NOTE

    if job_type == "quick":  # the terminal reads only the instruction for a quick change, so anything extra rides along in it
        summary = (summary + spec).strip()
        spec = ""
    argv = orchestrator_argv("script", "new_job.py", job_type, "--summary", summary)
    if params.get("_feature"):
        argv += ["--feature", params["_feature"]]
    if spec:
        spec_dir = runtime_dir(root) / "ui" / "specs"
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_file = spec_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{job_type}.md"
        spec_file.write_text(spec.strip() + "\n", encoding="utf-8")
        argv += ["--spec-file", str(spec_file)]
    branch_mode = _choice(params, "branch_mode", BRANCH_MODES)
    if branch_mode:
        argv += ["--branch-mode", branch_mode]
    if params.get("no_dispatch"):
        argv.append("--no-dispatch")
    if params.get("yolo"):
        argv.append("--yolo")
    if params.get("free"):
        argv.append("--free")
    return argv


RECENT_LOG_ACTIONS = {"test": "Test run", "build": "Build", "distribute": "Tester build", "fix": "Fix attempt", "debug": "Fix attempt",
                      "visual_check": "Simulator check", "logs_pull": "Device logs", "execute": "Job run", "resume": "Job run", "new_job": "New job"}


def recent_logs(root: Path, limit: int = 8) -> list[dict[str, Any]]:
    """The newest logs worth attaching to a bug: pulled device launches, then test/build/fix runs."""
    out = []
    for pull in device_log_pulls(root)[:3]:
        path = runtime_dir(root) / pull["path"]
        if path.is_file():
            session = f" {pull['session']}" if pull.get("session") else ""
            out.append({"path": str(path.relative_to(root)), "label": f"Device launch{session}", "kind": "device", "mtime": pull["mtime"], "size": path.stat().st_size})
    folder = runtime_dir(root) / "logs" / "ui"
    if folder.is_dir():
        for path in sorted(folder.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:60]:
            action = path.stem.split("-", 3)[-1] if path.stem.count("-") >= 3 else path.stem
            if action in RECENT_LOG_ACTIONS and path.stat().st_size > 0:
                out.append({"path": str(path.relative_to(root)), "label": RECENT_LOG_ACTIONS[action], "kind": "run", "mtime": path.stat().st_mtime, "size": path.stat().st_size})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)[:limit]


PROVIDER_NAMES = {"jira": "Jira", "trello": "Trello", "sentry": "Sentry", "figma": "Figma"}


def save_upload(root: Path, name: str, data: bytes, folder: Path | None = None) -> dict[str, Any]:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", PurePosixPath(name or "").name).strip(".-")[:80]
    if not clean:
        raise UIError("Give the file a name")
    try:
        kind = new_job_form.upload_kind(clean)
    except new_job_form.FormError as exc:
        raise UIError(str(exc))
    if not data:
        raise UIError("That file is empty")
    folder = folder or runtime_dir(root) / "ui" / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}-{clean}"
    target.write_bytes(data)
    return {"path": str(target.relative_to(root)), "name": clean, "size": len(data), "kind": kind}


def build_debug(params: dict[str, Any], root: Path) -> list[str]:
    argv = orchestrator_argv("script", "debug_job.py", _job_path(params, root))
    logs = _text(params, "logs", limit=4000)
    if logs:
        argv += ["--logs", logs]
    feedback = _text(params, "feedback")
    if feedback:
        argv += ["--feedback", feedback]
    return argv


def build_verify_feature(params: dict[str, Any], root: Path) -> list[str]:
    feature = _text(params, "feature", required=True)
    try:
        feature_store.get(feature_store.load(runtime_dir(root)), feature)
    except feature_store.FeatureError as exc:
        raise UIError(str(exc))
    return orchestrator_argv("script", "verify_integration.py", feature)


def build_fix(params: dict[str, Any], root: Path) -> list[str]:
    argv = orchestrator_argv("fix", _text(params, "feedback", required=True))
    if params.get("job"):
        resolve_job_path(root, _text(params, "job"))
        argv += ["--job", _text(params, "job")]
    return argv


def build_logs_pull(params: dict[str, Any], root: Path) -> list[str]:
    session = _session_id(params)
    argv = orchestrator_argv("logs", "pull", *(["--session", session] if session else ["--latest"]))
    level = _choice(params, "level", ["trace", "debug", "info", "warn", "error", "fatal"])
    if level:
        argv += ["--level", level]
    query = _text(params, "query", limit=500)
    if query:
        argv += ["--query", query]
    return argv


def build_logs_tail(params: dict[str, Any], root: Path) -> list[str]:
    session = _session_id(params)
    return orchestrator_argv("logs", "tail", *(["--session", session] if session else []))


def build_distribute(params: dict[str, Any], root: Path) -> list[str]:
    notes = _text(params, "notes", limit=4000)
    return orchestrator_argv("distribute", *(["--notes", notes] if notes else []))


def build_answer(params: dict[str, Any], root: Path) -> list[str]:
    return orchestrator_argv("script", "job_actions.py", "answer", _job_path(params, root),
                             "--answer", _text(params, "answer", required=True, limit=10_000))


def build_revise(params: dict[str, Any], root: Path) -> list[str]:
    return orchestrator_argv("script", "job_actions.py", "revise", _job_path(params, root),
                             "--change", _text(params, "change", required=True, limit=4000),
                             "--where", _text(params, "where", limit=1000),
                             "--done-when", _text(params, "done_when", limit=1000))


def _name(params: dict[str, Any], key: str) -> str:
    value = _text(params, key, required=True, limit=200)
    if not re.fullmatch(r"[A-Za-z0-9_. -]+", value):
        raise UIError(f"Invalid {key}")
    return value


def branches_elsewhere(root: Path) -> dict[str, str]:
    """Branches checked out in another worktree of this repository, and the folder each is in. Git keeps a branch in
    one place at a time, so these can't be switched to here."""
    here, out, path = safe_resolve(root), {}, None
    for line in git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/") and path and safe_resolve(Path(path)) != here:
            out[line[len("branch refs/heads/"):]] = path
    return out


def build_git_checkout(params: dict[str, Any], root: Path) -> list[str]:
    branch = _text(params, "branch", required=True, limit=250)
    if branch not in git(root, "branch", "--format=%(refname:short)").splitlines():
        raise UIError("Not a local branch")
    elsewhere = branches_elsewhere(root).get(branch)
    if elsewhere:
        raise UIError(f"{branch} is checked out in another folder ({elsewhere}), and git keeps a branch in one place at a time. "
                      "Work on it in that folder, or choose another branch.")
    return ["git", "checkout", branch]


def build_stash_checkout(params: dict[str, Any], root: Path) -> list[str]:
    """Set uncommitted tracked changes aside (git stash), then switch. Branch and message
    are passed as arguments, never spliced into the shell string."""
    branch = build_git_checkout(params, root)[-1]
    message = f"orchestrator: before switching to {branch}"
    return ["sh", "-c", 'git stash push -m "$1" && git checkout "$2"', "sh", message, branch]


def build_git_new_branch(params: dict[str, Any], root: Path) -> list[str]:
    name = _text(params, "name", required=True, limit=250)
    check = subprocess.run(["git", "check-ref-format", "--branch", name], cwd=root, capture_output=True, text=True)
    if check.returncode != 0 or name.startswith("-"):
        raise UIError("That isn't a valid branch name")
    return ["git", "checkout", "-b", name]


def build_git_push(params: dict[str, Any], root: Path) -> list[str]:
    branch = git(root, "branch", "--show-current")
    if not branch:
        raise UIError("Not on a branch (detached HEAD)")
    return ["git", "push", "-u", "origin", branch]


def build_splinter(params: dict[str, Any], root: Path) -> list[str]:
    return orchestrator_argv("script", "splinter_job.py", _job_path(params, root))


def build_delete_job(params: dict[str, Any], root: Path) -> list[str]:
    job_file = _job_path(params, root)
    argv = orchestrator_argv("script", "job_actions.py", "delete", job_file)
    if params.get("keep_changes") or params.get("revert") is False:
        argv.append("--keep-changes")
    return argv


def build_export_job(params: dict[str, Any], root: Path) -> list[str]:
    argv = orchestrator_argv("script", "export_job.py", _job_path(params, root))
    dest = _choice(params, "destination", ["icloud", "gdrive", "downloads", "local"])
    if dest:
        argv += ["--destination", dest]
    return argv


def build_simulator_visual_check(params: dict[str, Any], root: Path) -> list[str]:
    argv = orchestrator_argv("script", "simulator_visual_check.py")
    job = _text(params, "job", limit=200)
    if job:
        argv += ["--job", resolve_job_path(root, job).stem]
    destination = _text(params, "destination", limit=200)
    if destination:
        argv += ["--destination", destination]
    if params.get("no_build"):
        argv.append("--no-build")
    wait = params.get("wait")
    if wait:
        try:
            wait_float = float(wait)
            argv += ["--wait", str(wait_float)]
        except (ValueError, TypeError):
            pass
    return argv


def build_worker_install(params: dict[str, Any], root: Path) -> list[str]:
    machine = _text(params, "machine", required=False, limit=100)
    return orchestrator_argv("worker-install", *(["--machine", machine] if machine else []))


VISUAL_RUN_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def visual_check_dirs(root: Path) -> list[Path]:
    """Every folder a visual check can live in: `visual-check-*` under output/, and the
    `<timestamp>-visual-check` folders the script writes under output/manual/."""
    found = []
    for out_base in (runtime_dir(root) / "output", root / "output"):
        if out_base.is_dir():
            found += [p for p in out_base.glob("visual-check-*") if p.is_dir()]
        manual = out_base / "manual"
        if manual.is_dir():
            found += [p for p in manual.glob("*-visual-check") if p.is_dir()]
    return found


def visual_checks_inventory(root: Path) -> list[dict[str, Any]]:
    runs, seen = [], set()
    for child in visual_check_dirs(root):
        if child.name in seen:
            continue
        seen.add(child.name)
        report_file = child / "report.md"
        report_text = report_file.read_text(encoding="utf-8", errors="replace") if report_file.is_file() else ""
        screenshots = sorted([p.name for p in child.glob("*.png")])
        mtime = child.stat().st_mtime
        runs.append({
            "id": child.name,
            "job": read_json_file(child / "meta.json").get("job_id"),
            "created_at": datetime.fromtimestamp(mtime).isoformat(),
            "report": report_text,
            "screenshots": screenshots,
            "count": len(screenshots),
        })
    return sorted(runs, key=lambda x: x["created_at"], reverse=True)


def visual_check_image(root: Path, run_id: str, name: str) -> Path | None:
    """A screenshot inside one visual-check folder, or None. Both parts are validated so a URL can't name
    anything else on disk."""
    if not VISUAL_RUN_RE.match(run_id) or not VISUAL_RUN_RE.match(name) or not name.lower().endswith(".png") or run_id.startswith("."):
        return None
    for folder in visual_check_dirs(root):
        if folder.name == run_id:
            candidate = (folder / name).resolve()
            if candidate.parent == folder.resolve() and candidate.is_file():
                return candidate
    return None


def get_role_prompts(root: Path) -> list[dict[str, Any]]:
    roles = [
        {"id": "architect", "name": "Senior Architect", "file": "agent_lead.md", "desc": "Reviews proposals, sets architecture standards & acceptance criteria"},
        {"id": "planner", "name": "Planner Agent", "file": "planner_feature.md", "desc": "Breaks down requirements, investigates codebase, designs task plans"},
        {"id": "builder", "name": "Builder Agent", "file": "builder_feature_task.md", "desc": "Implements code changes, tests, and resolves compilation errors"},
        {"id": "reviewer", "name": "Reviewer Agent", "file": "reviewer.md", "desc": "Evaluates diffs, checks test coverage and regression risks"},
    ]
    prompts_dir = runtime_dir(root) / "prompts"
    results = []
    for r in roles:
        custom_file = prompts_dir / r["file"]
        is_custom = custom_file.is_file()
        content = ""
        if is_custom:
            try:
                content = custom_file.read_text(encoding="utf-8")
            except Exception:
                content = ""
        results.append({
            "id": r["id"],
            "name": r["name"],
            "file": r["file"],
            "description": r["desc"],
            "custom": is_custom,
            "customized": is_custom,
            "content": content[:10_000] if is_custom else "",
        })
    return results


# --------------------------------------------------------------------------- configuration

from orchestrator.ai_providers import API_KEYS  # (setting, label, environment variable), one list of providers
EMAIL_SECRET_KEYS = ("smtp_password", "resend_api_key")
EMAIL_PROVIDERS = ["gmail", "resend"]
EMAIL_RE = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
INSTRUCTION_FILES = {
    "Antigravity": "GEMINI.md", "Claude": "CLAUDE.md", "Codex": "AGENTS.md",
    "Copilot": ".github/copilot-instructions.md", "Ollama": "OLLAMA.md",
    "DeepSeek": "DEEPSEEK.md", "OpenCode": "OPENCODE.md", "Qwen": "QWEN.md",
}
ORCHESTRATOR_DOCS = ["getting-started.md", "user-guide.md", "recommended-mcp-plugins.md"]
# Console menus the Configuration page can open in a terminal (see scripts/config_menu.py).
CONFIG_MENUS = ["github", "models", "keys", "instructions", "fleet", "project", "archived", "firebase",
                "xcode", "email", "audit", "selftests", "update", "all"]


def settings_path(root: Path) -> Path:
    return runtime_dir(root) / "config" / "settings.json"


def tool_search_path() -> str:
    """Where to look for command-line tools: this process's PATH plus where installers usually put them, which a server
    started from the Dock or launchd doesn't have on its PATH."""
    return os.pathsep.join([os.environ.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin")])


def ai_providers_state(root: Path) -> dict[str, Any]:
    """The Add an AI page: every provider, free first, with what's installed and signed in here."""
    from orchestrator import ai_providers
    from orchestrator import setup_checklist as checklist
    search = tool_search_path()
    ready = set(checklist.ready_llm_providers({}))  # cached briefly: signing-in checks run the tools
    return ai_providers.with_status(lambda cli: shutil.which(cli, path=search) is not None, lambda cli: cli in ready,
                                    checklist.saved_api_keys(read_settings(root)))


def read_settings(root: Path) -> dict[str, Any]:
    return read_json_file(settings_path(root))


def write_settings(root: Path, settings: dict[str, Any]) -> None:
    path = settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
    os.replace(tmp, path)


def base_branch(root: Path, settings: dict[str, Any]) -> str:
    return settings.get("global_base_branch") or read_json_file(runtime_dir(root) / "project.json").get("base_branch") or "main"


def doc_entries(root: Path) -> list[dict[str, str]]:
    """Orchestrator guides first, then the project's own docs, as the console lists them."""
    package_docs = Path(__file__).resolve().parents[2] / "docs"
    entries: list[dict[str, str]] = []
    seen: set[Path] = set()
    for section, paths in (
        ("Orchestrator docs", [package_docs / n for n in ORCHESTRATOR_DOCS]),
        ("Project docs", sorted((root / "docs").glob("*.md")) + [root / n for n in ("README.md", "AGENTS.md", "AI_AGENT_SETUP.md")]),
    ):
        for p in paths:
            if p.is_file() and (r := p.resolve()) not in seen:
                seen.add(r)
                entries.append({"id": f"{len(entries)}", "name": p.name, "section": section, "path": str(r)})
    return entries


def archived_jobs(root: Path) -> list[dict[str, Any]]:
    folder = jobs_dir(root) / "archive"
    items = []
    for f in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []:
        data = read_json_file(f)
        items.append({"id": f.stem, "job_id": data.get("job_id", f.stem), "title": data.get("title") or "Untitled",
                      "status": data.get("status") or "unknown", "corrupt": not data})
    return items


def analytics_state(settings: dict[str, Any]) -> dict[str, Any]:
    provider = settings.get("analytics_provider", "")
    return {"providers": analytics.public_providers(), "provider": provider if provider in analytics.PROVIDERS else "",
            "key_set": bool(settings.get("analytics_key")), "region": settings.get("analytics_region", "us")}


def config_state(root: Path) -> dict[str, Any]:
    settings = read_settings(root)
    machines_file = runtime_dir(root) / "config" / "machines.json"
    machines_raw = read_json_file(machines_file).get("machines", [])
    provider = settings.get("notification_provider", "gmail")
    return {
        "base_branch": base_branch(root, settings),
        "branches": [b for b in git(root, "branch", "--format=%(refname:short)").splitlines() if b][:200],
        "keys": [{"id": kid, "label": label, "saved": bool(settings.get(kid)), "env": bool(os.environ.get(env))}
                 for kid, label, env in API_KEYS],
        "ollama_host": settings.get("ollama_host", ""),
        "email": {
            "provider": provider, "providers": EMAIL_PROVIDERS,
            "recipients": list(settings.get("notification_emails", [])),
            "smtp_email": settings.get("smtp_email", ""), "smtp_password_set": bool(settings.get("smtp_password")),
            "resend_from_email": settings.get("resend_from_email", ""), "resend_display_name": settings.get("resend_display_name", ""),
            "resend_api_key_set": bool(settings.get("resend_api_key")),
        },
        "analytics": analytics_state(settings),
        "webhook": {"set": bool(settings.get("notification_webhook")), "host": notifier.host_of(settings.get("notification_webhook", ""))},
        "archived": archived_jobs(root),
        "docs": [{k: v for k, v in d.items() if k != "path"} for d in doc_entries(root)],
        "instructions": [{"cli": cli, "file": f, "exists": (root / f).is_file()} for cli, f in INSTRUCTION_FILES.items()],
        "machines": [{"name": m.get("name"), "mode": m.get("execution_mode", "remote"), "roles": m.get("roles", []),
                      "ssh_target": m.get("ssh_target", ""), "repo_path": m.get("repo_path", ""),
                      "enabled": m.get("enabled", True), "models": len(m.get("models", [])),
                      "priority": m.get("priority", 1),
                      "xcode": bool(m.get("supports_xcode")), "simulator": bool(m.get("supports_simulator"))}
                     for m in machines_raw if isinstance(m, dict)],
        "models": {
            "assignments": settings.get("default_models", {
                "architect": "claude-sonnet-4-6",
                "planner": "claude-sonnet-4-6",
                "builder": "claude-sonnet-4-6",
                "reviewer": "claude-sonnet-4-6",
            }),
            "available": [
                {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6 (Recommended)", "provider": "Anthropic"},
                {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "provider": "Anthropic"},
                {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro", "provider": "Google"},
                {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash", "provider": "Google"},
                {"id": "gpt-4o", "label": "GPT-4o", "provider": "OpenAI"},
                {"id": "o3-mini", "label": "o3-mini", "provider": "OpenAI"},
            ],
            **settings.get("default_models", {
                "architect": "claude-sonnet-4-6",
                "planner": "claude-sonnet-4-6",
                "builder": "claude-sonnet-4-6",
                "reviewer": "claude-sonnet-4-6",
            }),
        },
        "firebase": {
            "app_id": settings.get("firebase_app_id", ""),
            "tester_groups": settings.get("firebase_tester_groups", "testers"),
            "service_account_path": settings.get("firebase_service_account_path", ""),
            "invite_url": settings.get("firebase_invite_url", ""),
            "cli_installed": shutil.which("firebase") is not None,
        },
        "xcode_cloud": {
            "configured": (root / "ci_scripts").is_dir() or (root / ".xcodecloud").exists(),
            "ci_scripts_exists": (root / "ci_scripts").is_dir(),
        },
        "role_prompts": get_role_prompts(root),
        "allowed_emails": [{"email": e, "source": src} for e, src in allowed_auth_sources(root).items()],
        "menus": CONFIG_MENUS,
    }


def config_update(root: Path, part: str, body: dict[str, Any]) -> dict[str, Any]:
    settings = read_settings(root)
    if part == "base-branch":
        branch = _text(body, "branch", required=True, limit=250)
        if branch not in git(root, "branch", "--format=%(refname:short)").splitlines():
            raise UIError("Not a local branch")
        settings["global_base_branch"] = branch
    elif part == "keys":
        kid = _text(body, "id", required=True, limit=50)
        if kid not in {k[0] for k in API_KEYS}:
            raise UIError("Unknown key")
        if body.get("clear"):
            settings.pop(kid, None)
            if kid == "ollama_api_key":
                settings.pop("ollama_host", None)
        else:
            settings[kid] = _text(body, "value", required=True, limit=2000)
            if kid == "ollama_api_key" and _text(body, "host", limit=500):
                settings["ollama_host"] = _text(body, "host", limit=500)
    elif part == "email":
        op = _choice(body, "op", ["add", "remove", "provider"])
        emails = list(settings.get("notification_emails", []))
        if op == "add":
            address = _text(body, "email", required=True, limit=254)
            if not EMAIL_RE.match(address):
                raise UIError(f"Invalid email format '{address}'")
            if address not in emails:
                emails.append(address)
            settings["notification_emails"] = emails
        elif op == "remove":
            address = _text(body, "email", required=True, limit=254)
            settings["notification_emails"] = [e for e in emails if e != address]
        elif op == "provider":
            provider = _choice(body, "provider", EMAIL_PROVIDERS)
            if not provider:
                raise UIError("Choose a provider")
            settings["notification_provider"] = provider
            # Blank secret fields keep what's saved, as in the console.
            for key in ("smtp_email", "smtp_password", "resend_api_key", "resend_from_email", "resend_display_name"):
                value = _text(body, key, limit=500)
                if value:
                    settings[key] = value
        else:
            raise UIError("Unknown email operation")
    elif part == "analytics":
        op = _choice(body, "op", ["set", "clear", "test"])
        if op == "set":
            provider = _choice(body, "provider", list(analytics.PROVIDERS))
            if not provider:
                raise UIError("Choose a provider")
            region = _choice(body, "region", list(analytics.PROVIDERS[provider]["hosts"]), "us")
            settings["analytics_provider"], settings["analytics_region"] = provider, region
            key = _text(body, "key", limit=500)  # blank keeps the saved key, as with the other secrets
            if key:
                settings["analytics_key"] = key
            elif not settings.get("analytics_key"):
                raise UIError("Paste the " + analytics.PROVIDERS[provider]["key_label"].lower())
        elif op == "clear":
            for k in ("analytics_provider", "analytics_key", "analytics_region"):
                settings.pop(k, None)
        else:
            provider = settings.get("analytics_provider", "")
            if provider not in analytics.PROVIDERS:
                raise UIError("Choose a provider first")
            host = analytics.PROVIDERS[provider]["hosts"].get(settings.get("analytics_region", "us"), "")
            try:
                analytics.send_test(provider, settings.get("analytics_key", ""), host)
            except analytics.AnalyticsError as exc:
                raise UIError(str(exc), HTTPStatus.BAD_GATEWAY)
            return {"ok": True}
    elif part == "webhook":
        op = _choice(body, "op", ["set", "clear", "test"])
        if op == "set":
            url = _text(body, "url", required=True, limit=500)
            if not notifier.valid_webhook(url):
                raise UIError("The webhook must be an https:// URL")
            settings["notification_webhook"] = url
        elif op == "clear":
            settings.pop("notification_webhook", None)
        else:
            url = settings.get("notification_webhook", "")
            if not url:
                raise UIError("Save a webhook first")
            try:
                notifier.post_webhook(url, notifier.payload({"title": "Test notification", "body": "Orchestrator can reach this channel.", "path": "#/inbox"},
                                                            project_display_name(root), PUBLIC_URL.get("url", "")))
            except notifier.NotifyError as exc:
                raise UIError(str(exc), HTTPStatus.BAD_GATEWAY)
            return {"ok": True}
    elif part == "allowed-email":
        op = _choice(body, "op", ["add", "remove"])
        address = _text(body, "email", required=True, limit=254).lower()
        if not EMAIL_RE.match(address):
            raise UIError(f"Invalid email format '{address}'")
        emails = [e for e in settings.get("allowed_emails", []) if str(e).lower() != address]
        if op == "add":
            emails.append(address)
        elif address not in {str(e).lower() for e in settings.get("allowed_emails", [])}:
            raise UIError("Only emails added here can be removed here. Others come from git, the environment or project.json.")
        settings["allowed_emails"] = emails
    elif part == "setup-seen":
        settings["setup_seen"] = True
    elif part == "archived-restore":
        name = _text(body, "id", required=True, limit=200)
        src = jobs_dir(root) / "archive" / f"{name}.json"
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or not src.is_file():
            raise UIError("Archived job not found")
        dest = jobs_dir(root) / src.name
        if dest.exists():
            raise UIError("A job with that id already exists")
        shutil.move(str(src), str(dest))
        job = read_json_file(dest)
        if job.get("restore_status"):  # undo of "Mark complete": back to where it was, not "completed"
            job["status"] = job.pop("restore_status")
            job.pop("completed_at", None)
            write_json_file(dest, job)
        return {"ok": True}
    elif part == "models":
        assignments = body.get("models")
        if isinstance(assignments, dict):
            settings["default_models"] = {k: str(v).strip() for k, v in assignments.items()}
    elif part == "firebase":
        for k in ("firebase_app_id", "firebase_tester_groups", "firebase_service_account_path", "firebase_invite_url"):
            val = _text(body, k, limit=500)
            if k == "firebase_invite_url" and val and not notifier.valid_webhook(val):  # same rule: an https address, no credentials in it
                raise UIError("The invite link must be an https:// address")
            if val or k in body:
                settings[k] = val
    elif part == "role-prompts":
        role_id = _text(body, "id", required=True)
        content = _text(body, "content", limit=20_000)
        prompts_dir = runtime_dir(root) / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        filename_map = {
            "architect": "agent_lead.md",
            "planner": "planner_feature.md",
            "builder": "builder_feature_task.md",
            "reviewer": "reviewer.md",
        }
        if role_id not in filename_map:
            raise UIError("Unknown role ID")
        target_file = prompts_dir / filename_map[role_id]
        if body.get("revert"):
            if target_file.exists():
                target_file.unlink()
        else:
            target_file.write_text(content + "\n", encoding="utf-8")
        return {"ok": True}
    elif part == "fleet":
        op = _choice(body, "op", ["add", "remove", "toggle"])
        machines_file = runtime_dir(root) / "config" / "machines.json"
        machines_data = read_json_file(machines_file) or {}
        machines_list = list(machines_data.get("machines", []))
        if op == "add":
            name = _text(body, "name", required=True, limit=50)
            target = _text(body, "ssh_target", limit=200)
            repo = _text(body, "repo_path", limit=500)
            mode = _choice(body, "mode", ["local", "remote"], "remote")
            new_m = {
                "name": name,
                "execution_mode": mode,
                "ssh_target": target,
                "repo_path": repo,
                "enabled": True,
                "roles": ["builder", "verifier"] if mode == "remote" else ["architect", "planner", "builder", "reviewer"],
                "models": [],
                "priority": 2 if mode == "remote" else 1,
            }
            machines_list = [m for m in machines_list if m.get("name") != name]
            machines_list.append(new_m)
            machines_data["machines"] = machines_list
            write_json_file(machines_file, machines_data)
            return {"ok": True}
        elif op == "remove":
            name = _text(body, "name", required=True)
            machines_data["machines"] = [m for m in machines_list if m.get("name") != name]
            write_json_file(machines_file, machines_data)
            return {"ok": True}
        elif op == "toggle":
            name = _text(body, "name", required=True)
            for m in machines_list:
                if m.get("name") == name:
                    m["enabled"] = not m.get("enabled", True)
            machines_data["machines"] = machines_list
            write_json_file(machines_file, machines_data)
            return {"ok": True}
    else:
        raise UIError("Unknown configuration section", HTTPStatus.NOT_FOUND)
    write_settings(root, settings)
    return {"ok": True}


def build_config_menu(params: dict[str, Any], root: Path) -> list[str]:
    menu = _choice(params, "menu", CONFIG_MENUS)
    if not menu:
        raise UIError("'menu' is required")
    return orchestrator_argv("script", "config_menu.py", menu)


def build_test_email(params: dict[str, Any], root: Path) -> list[str]:
    provider = read_settings(root).get("notification_provider", "gmail")
    return orchestrator_argv("script", "notify.py", f"Test Notification ({provider})",
                             f"This is a test message from the Orchestrator web UI using {provider}.", "test-job-id")


# --------------------------------------------------------------------------- connections (Jira, Trello, Sentry, Figma)


def saved_integrations(root: Path) -> dict[str, dict[str, str]]:
    data = read_settings(root).get("integrations")
    return data if isinstance(data, dict) else {}


def integration_error(exc: "integrations.IntegrationError") -> UIError:
    return UIError(str(exc), HTTPStatus.BAD_GATEWAY)


def connect_integration(root: Path, provider_id: str, values: dict[str, Any]) -> dict[str, Any]:
    saved = saved_integrations(root)
    try:
        creds, who = integrations.connect(provider_id, {k: str(v) for k, v in values.items()}, saved.get(provider_id))
    except integrations.IntegrationError as exc:
        raise integration_error(exc)
    settings = read_settings(root)
    settings.setdefault("integrations", {})[provider_id] = creds
    write_settings(root, settings)
    return {"ok": True, "who": who}


def save_integration_options(root: Path, provider_id: str, raw: Any) -> dict[str, Any]:
    cls = integrations.PROVIDERS.get(provider_id)
    if not cls or not cls.can_write:
        raise UIError("That app has no write-back options")
    if not isinstance(raw, dict):
        raise UIError("Invalid options")
    options = integrations.writeback_options(raw)
    settings = read_settings(root)
    settings.setdefault("integration_options", {})[provider_id] = options
    write_settings(root, settings)
    return options


def disconnect_integration(root: Path, provider_id: str) -> None:
    if provider_id not in integrations.PROVIDERS:
        raise UIError("Unknown connection")
    settings = read_settings(root)
    (settings.get("integrations") or {}).pop(provider_id, None)
    write_settings(root, settings)


def search_integration(root: Path, provider_id: str, query: str) -> list[dict[str, str]]:
    saved = saved_integrations(root)
    if provider_id not in integrations.PROVIDERS or provider_id not in saved:
        raise UIError("That app isn't connected", HTTPStatus.NOT_FOUND)
    try:
        return [i.as_dict() for i in integrations.provider_for(provider_id, saved[provider_id]).search(query[:200])]
    except integrations.IntegrationError as exc:
        raise integration_error(exc)


def _clean_links(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    links = [{"provider": str(l.get("provider", "")), "ref": str(l.get("ref", "")).strip()[:500]}
             for l in raw if isinstance(l, dict) and str(l.get("ref", "")).strip()]
    if len(links) > 8:
        raise UIError("Link at most 8 items to one job.")
    return links


def _save_image(root: Path, subdir: Path, ctx: "integrations.Context") -> str | None:
    if not ctx.image or not ctx.image_name:
        return None
    subdir.mkdir(parents=True, exist_ok=True)
    target = subdir / ctx.image_name
    target.write_bytes(ctx.image)
    return str(target.relative_to(root))


def context_for_new_job(root: Path, links: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    """Markdown to append to a new job's spec, plus the items to record on the job afterwards."""
    if not links:
        return "", []
    try:
        contexts = integrations.build_context(links, saved_integrations(root))
    except integrations.IntegrationError as exc:
        raise integration_error(exc)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    extra = []
    for ctx in contexts:
        rel = _save_image(root, runtime_dir(root) / "ui" / "context" / stamp, ctx)
        if rel:
            extra.append(f"Reference image for {ctx.item.title}: `{rel}`")
    return integrations.spec_block(contexts) + ("\n" + "\n".join(extra) + "\n" if extra else ""), [c.item.as_dict() for c in contexts]


def attach_links_to_job(root: Path, job_id: str, links: list[dict[str, str]]) -> list[dict[str, str]]:
    """Add connected-app context to an existing job: written under its references, and recorded on it."""
    path = resolve_job_path(root, job_id)
    try:
        contexts = integrations.build_context(links, saved_integrations(root))
    except integrations.IntegrationError as exc:
        raise integration_error(exc)
    job = read_json_file(path)
    refs_dir = runtime_dir(root) / "output" / job_id / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)
    artifacts = list(job.get("reference_artifacts") or [])
    existing = list(job.get("external_links") or [])
    for ctx in contexts:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{ctx.item.provider}-{ctx.item.ref}").strip("-")[:80]
        md = refs_dir / f"{slug}.md"
        md.write_text(ctx.markdown + "\n", encoding="utf-8")
        artifacts.append({"type": "text_reference", "note": f"{ctx.item.provider}: {ctx.item.title}",
                          "url": ctx.item.url, "path": str(md.relative_to(root))})
        img = _save_image(root, refs_dir, ctx)
        if img:
            artifacts.append({"type": "image_reference", "note": f"Figma render: {ctx.item.title}", "path": img})
        existing = [e for e in existing if not (e.get("provider") == ctx.item.provider and e.get("ref") == ctx.item.ref)]
        existing.append(ctx.item.as_dict())
    job["reference_artifacts"] = artifacts
    job["external_links"] = existing
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return existing


# --------------------------------------------------------------------------- new project


def new_project_state() -> dict[str, Any]:
    from orchestrator.setup_checklist import github_cli_state
    return {"platform_needs": new_project.PLATFORM_NEEDS, "questions": new_project.QUESTIONS, "draft": new_project.load_draft(),
            "github": github_cli_state(fresh=True), "default_parent": new_project.default_parent(),
            "prd_sections": new_project.PRD_SECTIONS}


def create_new_project(server: "UIServer") -> dict[str, Any]:
    draft = new_project.load_draft()
    if not draft:
        raise UIError("There's no project in progress. Start by describing it.")
    if draft.get("created_root"):
        raise UIError("This project is already created. Finish the GitHub step to publish it.")
    try:
        result = new_project.create_project(draft)
    except new_project.NewProjectError as exc:
        raise UIError(str(exc))
    if server.bootstrap_enabled:
        server.selected_root = Path(result["root"])
    else:
        server.set_root(Path(result["root"]))
    # The draft stays until GitHub is done, so a failed publish can be retried from where they were.
    if result["github_ok"]:
        new_project.clear_draft()
    else:
        new_project.save_draft({**draft, "step": "create", "waiting_on_github": True, "created_root": result["root"]})
    return result


def publish_known_project(root_str: str, visibility: str) -> dict[str, Any]:
    root = safe_resolve(Path(root_str).expanduser())
    known = {str(safe_resolve(Path(p["root"]).expanduser())) for p in load_recent_projects().get("projects", []) if p.get("root")}
    if str(root) not in known or not (root / ".git").exists():
        raise UIError("That isn't one of your projects")
    if git(root, "remote", "get-url", "origin"):
        raise UIError("This project already has a GitHub remote")
    step = new_project.publish_to_github(root, root.name, "public" if visibility == "public" else "private")
    if step["ok"]:
        new_project.clear_draft()
    return step


def build_manual(mode: str) -> Callable[[dict[str, Any], Path], list[str]]:
    return lambda params, root: orchestrator_argv("script", "manual_run.py", mode)


ACTIONS: dict[str, Action] = {
    "console": Action("Interactive console", lambda p, r: orchestrator_argv("console")),
    "check": Action("Setup check", lambda p, r: orchestrator_argv("check")),
    "check_config": Action("Config check", lambda p, r: orchestrator_argv("check-config")),
    "wizard": Action("Setup wizard", lambda p, r: orchestrator_argv("wizard")),
    "worker_check": Action("Worker check", lambda p, r: orchestrator_argv("worker-check")),
    "new_job": Action("New job", build_new_job, fields=["type", "summary", "spec", "repro", "expected", "vibe", "subsystems", "branch_mode", "no_dispatch", "yolo", "free", "links", "feature", "logs", "files", "urls"]),
    "fix": Action("Fix", build_fix, fields=["feedback", "job"]),
    "schedule": Action("Start", lambda p, r: orchestrator_argv("script", "schedule_job.py", _job_path(p, r)), fields=["job"]),
    "execute": Action("Run now", lambda p, r: orchestrator_argv("script", "worker_run.py", _job_path(p, r)), fields=["job"]),
    "resume": Action("Resume", lambda p, r: orchestrator_argv("script", "worker_run.py", _job_path(p, r), "--resume"), fields=["job"]),
    "debug": Action("Run fix", build_debug, fields=["job", "logs", "feedback"]),
    "answer": Action("Answer", build_answer, fields=["job", "answer"]),
    "approve": Action("Approve", lambda p, r: orchestrator_argv("script", "job_actions.py", "approve", _job_path(p, r)), fields=["job"]),
    "revise": Action("Revise plan", build_revise, fields=["job", "change", "where", "done_when"]),
    "test_suite": Action("Run test suite", lambda p, r: orchestrator_argv("script", "job_actions.py", "run-suite", _name(p, "name")), fields=["name"]),
    "test_plan": Action("Run test plan", lambda p, r: orchestrator_argv("script", "job_actions.py", "run-plan", _name(p, "name")), fields=["name"]),
    "coverage": Action("Measure coverage", lambda p, r: orchestrator_argv("script", "job_actions.py", "coverage")),
    "git_pull": Action("Pull", lambda p, r: ["git", "pull"]),
    "git_push": Action("Push", build_git_push, confirm="Pushes the current branch to origin on GitHub."),
    "git_checkout": Action("Switch branch", build_git_checkout, fields=["branch"]),
    "stash_checkout": Action("Stash changes & switch branch", build_stash_checkout, fields=["branch"]),
    "git_new_branch": Action("New branch", build_git_new_branch, fields=["name"]),
    "merge": Action("Merge & complete", lambda p, r: orchestrator_argv("script", "job_actions.py", "merge", _job_path(p, r)),
                    confirm="Merges the job's PR on GitHub, deletes its AI branch and archives the job.", fields=["job"]),
    "discard": Action("Discard job", lambda p, r: orchestrator_argv("script", "job_actions.py", "discard", _job_path(p, r)),
                      confirm="Reverts the files this job changed, deletes its AI branch and archives the job. This can't be undone.", fields=["job"]),
    "delete_job": Action("Delete job", build_delete_job, fields=["job", "keep_changes"]),
    "complete": Action("Mark complete", lambda p, r: orchestrator_argv("script", "job_actions.py", "complete", _job_path(p, r)),
                       confirm="Archives the job as completed. Its branch is left as it is. You can bring it back from Configuration > Archived jobs.", fields=["job"]),
    "deliver": Action("Deliver to testers", lambda p, r: orchestrator_argv("script", "deliver_build.py", _job_path(p, r)),
                      confirm="Builds this job's branch and sends a real Firebase release to your testers.", fields=["job"]),
    "build": Action("Build", build_manual("build")),
    "test": Action("Run all tests", build_manual("test")),
    "distribute": Action("Distribute current branch", build_distribute,
                         confirm="Builds whatever branch is checked out now and sends a real Firebase release to your testers.",
                         fields=["notes"]),
    "config_menu": Action("Configuration", build_config_menu, fields=["menu"]),
    "test_email": Action("Send test email", build_test_email),
    "logs_setup": Action("Device logs setup", lambda p, r: orchestrator_argv("logs", "setup")),
    "logs_pull": Action("Pull device logs", build_logs_pull, fields=["session", "level", "query"]),
    "logs_tail": Action("Follow device logs", build_logs_tail, fields=["session"]),
    "splinter": Action("Splinter into sub-jobs", build_splinter,
                       confirm="Decomposes this feature plan into child task jobs and issues.", fields=["job"]),
    "export_job": Action("Export job bundle", build_export_job, fields=["job", "destination"]),
    "visual_check": Action("Simulator visual check", build_simulator_visual_check, fields=["destination", "wait", "no_build", "job"]),
    "worker_install": Action("Install worker dependencies", build_worker_install, fields=["machine"]),
    "verify_feature": Action("Check the feature's jobs together", build_verify_feature, fields=["feature"]),
    "sync_fleet": Action("Sync fleet code", lambda p, r: orchestrator_argv("script", "sync_fleet.py")),
    "fleet_llm_check": Action("Fleet LLM latency & quota check", lambda p, r: orchestrator_argv("script", "fleet_llm_check.py")),
    "update_local": Action("Update Orchestrator (local)", lambda p, r: orchestrator_argv("update")),
    "update_fleet": Action("Update Orchestrator (fleet-wide)", lambda p, r: orchestrator_argv("update", "--fleet")),
    "scaffold_canary": Action("Scaffold canary test suite", lambda p, r: orchestrator_argv("script", "job_actions.py", "scaffold-canary")),
}


# --------------------------------------------------------------------------- HTTP


class SignInStore:
    """People who signed in with Google (or another provider): each gets their own token that expires and can be
    revoked, never the server's access token. Only a hash of each token is kept on disk, so a restart doesn't sign
    everyone out and the file is no use to someone who reads it."""

    TOUCH_SAVE_SECONDS = 600  # how stale "last seen" may get on disk before a request writes it

    def __init__(self, path: Path, ttl: int = SIGN_IN_TTL_SECONDS):
        self.path = path
        self.ttl = ttl
        self._lock = threading.Lock()
        raw = read_json_file(path).get("sign_ins")
        now = time.time()
        self._items: dict[str, dict[str, Any]] = {
            h: s for h, s in (raw.items() if isinstance(raw, dict) else [])
            if isinstance(s, dict) and s.get("expires", 0) > now and s.get("email")}

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _save(self) -> None:  # caller holds the lock
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"sign_ins": self._items}))
            tmp.chmod(0o600)
            tmp.replace(self.path)
        except OSError as exc:
            print(f"  Couldn't save sign-ins: {exc}", flush=True)

    def create(self, email: str) -> str:
        token = "si_" + secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._items[self._hash(token)] = {"id": secrets.token_hex(6), "email": email, "created": now,
                                              "expires": now + self.ttl, "last_seen": now}
            self._save()
        return token

    def lookup(self, token: str) -> dict[str, Any] | None:
        h = self._hash(token)
        now = time.time()
        with self._lock:
            item = self._items.get(h)
            if item is None:
                return None
            if item["expires"] <= now:
                del self._items[h]
                self._save()
                return None
            if now - item.get("last_seen", 0) > self.TOUCH_SAVE_SECONDS:
                item["last_seen"] = now
                self._save()
            return dict(item)

    def list(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            live = [dict(s) for s in self._items.values() if s["expires"] > now]
        return sorted(live, key=lambda s: s.get("last_seen", 0), reverse=True)

    def _drop(self, keep: Callable[[dict[str, Any]], bool]) -> int:
        with self._lock:
            gone = [h for h, s in self._items.items() if not keep(s)]
            for h in gone:
                del self._items[h]
            if gone:
                self._save()
        return len(gone)

    def revoke(self, sign_in_id: str) -> bool:
        return self._drop(lambda s: s["id"] != sign_in_id) > 0

    def revoke_token(self, token: str) -> None:
        with self._lock:
            if self._items.pop(self._hash(token), None) is not None:
                self._save()

    def revoke_email(self, email: str) -> int:
        email = email.strip().lower()
        return self._drop(lambda s: s["email"] != email)


def get_or_create_ui_token(supplied: str | None = None) -> str:
    """Return the supplied token or read/persist a stable token in ~/.orchestrator/ui_token."""
    if supplied and supplied.strip():
        return supplied.strip()
    token_file = user_state_dir() / "ui_token"
    if token_file.is_file():
        try:
            existing = token_file.read_text().strip()
            if existing:
                return existing
        except Exception:
            pass
    token = secrets.token_urlsafe(24)
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=token_file.parent, prefix=".ui-token-")
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(token)
            os.replace(temporary, token_file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except Exception:
        pass
    return token


class UIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], root: Path | None, token: str | None = None,
                 bootstrap_mode: bool = False, desktop_listener: bool = False, shared: UIServer | None = None):
        super().__init__(address, UIHandler)
        if desktop_listener and address[0] not in ("127.0.0.1", "::1"):
            self.server_close()
            raise ValueError("Desktop handoff requires loopback")
        self.desktop_listener = desktop_listener
        self.accepting_requests = True
        self._context = shared._context if shared else SimpleNamespace(root=root, selected_root=root)
        if not shared:
            self.root = root
            self.selected_root = root
        self.bootstrap_enabled = bootstrap_mode or root is None
        self.token = get_or_create_ui_token(token)
        self.sign_ins = SignInStore(user_state_dir() / "ui_sign_ins.json")
        # Pages other than the server's own that may call it from a browser: the hosted app, plus any listed in
        # ORCHESTRATOR_ALLOWED_ORIGINS (comma-separated, e.g. a custom domain in front of the hosted app).
        self.allowed_origins = set(HOSTED_ORIGINS) | {
            o.strip().rstrip("/") for o in os.environ.get("ORCHESTRATOR_ALLOWED_ORIGINS", "").split(",") if o.strip()}
        self.audit = audit.AuditLog(user_state_dir() / "audit.jsonl")
        self._email_sources: tuple[float, Path, dict[str, str]] | None = None
        self._used_tickets: dict[str, float] = {}
        self._ticket_lock = threading.Lock()
        self.gate = ActivityGate()
        self.sessions = SessionManager(gate=self.gate)
        self.tasks = BackgroundTasks(gate=self.gate)
        self.allowed_hosts = self._allowed_hosts()
        self._stopping = threading.Event()
        self._plan_lock = threading.Lock()   # one plan-run decision at a time (background loop and page requests)
        self._plan_sessions: dict[str, Any] = {}  # feature id -> the planning run started for it
        from orchestrator.web.browser_grants import BrowserGrantStore
        self.browser_grants = BrowserGrantStore()
        if shared:
            for name in ("token", "sign_ins", "audit", "gate", "sessions", "tasks", "_used_tickets",
                         "_ticket_lock", "_stopping", "browser_grants", "bootstrap_enabled", "_plan_lock", "_plan_sessions"):
                setattr(self, name, getattr(shared, name))

    @property
    def root(self) -> Path | None:
        return self._context.root

    @root.setter
    def root(self, root: Path | None) -> None:
        self._context.root = root

    @property
    def selected_root(self) -> Path | None:
        return self._context.selected_root

    @selected_root.setter
    def selected_root(self, root: Path | None) -> None:
        self._context.selected_root = root

    def _allowed_hosts(self) -> set[str] | None:
        """Host headers accepted, or None when bound to every interface: the
        name a remote browser uses then can't be known, and the token remains
        the actual gate."""
        host, port = self.server_address[:2]
        if host in ("0.0.0.0", "::"):
            return None
        names = {host, "localhost", "127.0.0.1", "[::1]"}
        return {f"{n}:{port}" for n in names} | names

    def shutdown(self) -> None:
        self._stopping.set()
        super().shutdown()

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        return f"http://{shown}:{port}/"

    def set_root(self, root: Path) -> None:
        self.root = root
        self.selected_root = root
        self._email_sources = None

    def email_sources(self) -> dict[str, str]:
        """allowed_auth_sources, cached briefly: it is checked on every signed-in request and asks git."""
        if self.root is None:
            return {}
        cached = self._email_sources
        if cached and cached[1] == self.root and time.time() - cached[0] < ALLOWED_EMAILS_CACHE_SECONDS:
            return cached[2]
        sources = allowed_auth_sources(self.root)
        self._email_sources = (time.time(), self.root, sources)
        return sources

    def allowed_emails(self) -> set[str]:
        return set(self.email_sources())

    def role_of(self, email: str) -> str:
        return "owner" if self.email_sources().get(email) in OWNER_SOURCES else "member"

    def forget_allowed_emails(self) -> None:
        self._email_sources = None

    def use_ticket_nonce(self, nonce: str, expires: float) -> bool:
        """True the first time a ticket is presented; False on any replay while it is still valid."""
        now = time.time()
        with self._ticket_lock:
            for seen, until in list(self._used_tickets.items()):
                if until < now:
                    del self._used_tickets[seen]
            if nonce in self._used_tickets:
                return False
            self._used_tickets[nonce] = expires
            return True

    def start_heartbeat(self, endpoint: Callable[[], str], interval: float = account.HEARTBEAT_SECONDS) -> threading.Thread | None:
        # Also reports how many runs are going, so the account can say so if this computer disappears mid-run.
        """While this computer is paired, tell the control plane it is up and where to reach it."""
        if account.load_machine() is None:
            return None

        def beat() -> bool:
            machine = account.load_machine()
            if machine is None:
                return False
            try:
                account.heartbeat(machine, endpoint(), running=sum(1 for run in self.sessions.list() if run.get("running")))
            except account.AccountError as exc:
                if exc.status == 410:
                    print("  This computer was removed from its account. Run `orchestrator connect` to add it again.", flush=True)
                    return False
                print(f"  Account heartbeat: {exc}", flush=True)
            return True

        def loop() -> None:
            while beat() and not self._stopping.wait(interval):
                pass

        thread = threading.Thread(target=loop, daemon=True, name="heartbeat")
        thread.start()
        return thread

    def start_notifier(self, interval: float = 15.0) -> threading.Thread:
        """Push new inbox items and finished runs to the configured webhook, tab open or not."""
        tracker = notifier.Tracker()

        def loop() -> None:
            while not self._stopping.wait(interval):
                try:  # one read of the job list per round, shared by notifications and the plan
                    jobs = list_jobs(self.root) if self.root is not None else []
                except Exception:
                    jobs = None
                try:
                    self.notify_once(tracker, jobs)
                except Exception as exc:  # never let a bad webhook or odd job file kill the watcher
                    print(f"  Notifications: {exc}", flush=True)
                try:
                    self.plan_tick(jobs)
                except Exception as exc:  # a bad job file mustn't stop the loop; the run shows what's stuck
                    print(f"  Build the plan: {exc}", flush=True)

        thread = threading.Thread(target=loop, daemon=True, name="notifier")
        self._tracker = tracker
        self.notify_once(tracker)  # seed so existing items aren't announced
        thread.start()
        return thread

    def _plan_inputs(self, root: Path, jobs: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
        runtime = runtime_dir(root)
        machines = [m for m in read_json_file(runtime / "config" / "machines.json").get("machines", []) if isinstance(m, dict)]
        active = list_jobs(root) if jobs is None else jobs
        return feature_store.load(runtime), active + archived_feature_jobs(root), plan_run.capacity(machines)

    @staticmethod
    def _plan_payload(run: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        return {"id": run["id"], "auto_approve": run.get("auto_approve", False), "paused": run.get("paused", False),
                "finished": decision["finished"], "rows": decision["rows"]}

    def _planning_alive(self, feature_ids: Any) -> dict[str, bool]:
        """For features whose job is still being planned: is that planning run still going?"""
        return {fid: getattr(self._plan_sessions.get(fid), "running", False) for fid in feature_ids}

    def plan_view(self, root: Path) -> dict[str, Any] | None:
        """The plan run for the Features page: its settings and what each feature is doing. Changes nothing."""
        run = plan_run.load(runtime_dir(root))
        if not run:
            return None
        features, jobs, slots = self._plan_inputs(root)
        decision = plan_run.decide(run, features, jobs, slots, self._planning_alive(run.get("starting") or {}), can_start=False)
        return self._plan_payload(run, decision)

    def plan_tick(self, jobs: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """Advance "Build the plan": start ready features, approve plans when asked to, and record what finished.
        Returns the run as the page shows it, or None when no plan is being built."""
        root = self.root
        if root is None:
            return None
        runtime = runtime_dir(root)
        with self._plan_lock:
            run = plan_run.load(runtime)
            if not run or run.get("finished"):
                return None
            features, jobs, slots = self._plan_inputs(root, jobs)
            starting = run.setdefault("starting", {})
            for fid in list(starting):  # the planning run made its job: from now on the job says how it's going
                job = plan_run.job_for(fid, run, jobs)
                if job:
                    starting.pop(fid)
                    self._plan_sessions.pop(fid, None)
            decision = plan_run.decide(run, features, jobs, slots, self._planning_alive(starting))
            by_id = {f["id"]: f for f in features}
            for fid in decision["start"]:
                feature = by_id[fid]
                params = {"type": "feature", "summary": f"{feature['name']}: {feature.get('summary') or feature['name']}"[:500],
                          "feature": fid, "branch_mode": "new", "no_dispatch": True}
                argv = build_new_job(params, root) + ["--plan-run", run["id"]]
                session = self.sessions.start("new_job", f"Build the plan · {feature['name']}", argv, root, self.child_env(),
                                              runtime / "logs" / "ui")
                self._plan_sessions[fid] = session
                starting[fid] = {"at": time.time()}
            running = self.sessions.running_job_ids()
            for job_id in decision["approve"]:
                if job_id in running:
                    continue
                session = self.sessions.start("approve", "Approve · plan run", ACTIONS["approve"].build({"job": job_id}, root),
                                              root, self.child_env(), runtime / "logs" / "ui")
                session.job_id = job_id
            run["finished"] = decision["finished"]
            plan_run.save(runtime, run)
            return self._plan_payload(run, decision)

    def notify_once(self, tracker: "notifier.Tracker", jobs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        root = self.root
        if root is None:
            return []
        events = tracker.update(inbox_overview(root, self.sessions, with_others=False, jobs=jobs)["here"], self.sessions.list())
        machine = account.load_machine()
        if machine and events:  # phones and browsers on the account, even with no tab open
            try:
                account.send_events(machine, events, project_display_name(root))
            except account.AccountError as exc:
                print(f"  Push notifications: {exc}", flush=True)
        url = read_settings(root).get("notification_webhook")
        if url:
            for event in events:
                try:
                    notifier.post_webhook(url, notifier.payload(event, project_display_name(root), PUBLIC_URL.get("url", "")))
                except notifier.NotifyError as exc:
                    print(f"  Notifications: {exc}", flush=True)
        return events

    def child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["ORCHESTRATOR_PROJECT_ROOT"] = str(self.root)
        env.pop("ORCHESTRATOR_CONFIG", None)
        # Actions run with the project as cwd, where `python -m orchestrator` would pick up the
        # project's own copy of the package (older, or missing newer scripts) instead of the one
        # serving this UI. `-P` (see orchestrator_argv) keeps cwd off the import path and this puts
        # the UI's package first; unlike PYTHONSAFEPATH it isn't inherited by the scripts we start.
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PACKAGE_PARENT), env.get("PYTHONPATH")]))
        return env


class UIHandler(BaseHTTPRequestHandler):
    server: UIServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the terminal quiet
        return

    # -- plumbing

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self._cors_headers()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = HTTPStatus.OK, extra: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json", extra)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _host_ok(self) -> bool:
        allowed = self.server.allowed_hosts
        if allowed is None:
            return True
        h = (self.headers.get("Host") or "").lower()
        if h in allowed:
            return True
        if h.endswith(".trycloudflare.com") or ".trycloudflare.com:" in h:
            return True
        return False

    def _origin_allowed(self, origin: str) -> bool:
        """The hosted app, the server's own pages (whatever name they were reached by), and pages on this computer."""
        if origin.rstrip("/") in self.server.allowed_origins:
            return True
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https"):
            return False
        if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
            return True
        return parsed.netloc.lower() == (self.headers.get("Host") or "").lower()

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        self.send_header("Vary", "Origin")
        if origin and self._origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")

    def _authenticate(self) -> dict[str, Any] | None:
        """Who is asking: the owner (the server's access token) or someone signed in, or None.
        A sign-in stops working as soon as its email is no longer allowed, wherever the allowance came from."""
        supplied = self._supplied_token()
        if not supplied:
            return None
        if hmac.compare_digest(supplied, self.server.token):
            return {"kind": "owner", "role": "owner", "token": supplied}
        sign_in = self.server.sign_ins.lookup(supplied)
        if sign_in is None or sign_in["email"] not in self.server.allowed_emails():
            return None
        return {"kind": "sign_in", "role": self.server.role_of(sign_in["email"]), "token": supplied, **sign_in}

    def _is_owner(self) -> bool:
        return self._principal["role"] == "owner"

    def _client_ip(self) -> str:
        # Behind a tunnel the connection comes from the tunnel, so prefer what it says it saw. This is a note for the
        # owner, not something anything relies on, so a forged header only misleads the log.
        forwarded = self.headers.get("CF-Connecting-IP") or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        return forwarded or self.client_address[0]

    def _audit(self, event: str, who: str | None = None, **detail: Any) -> None:
        if who is None:
            who = (self._principal or {}).get("email") or "access token"
        self.server.audit.record(event, who, ip=self._client_ip(), **detail)

    def _require_owner(self, what: str) -> None:
        if not self._is_owner():
            self._audit("denied", what=what)
            raise UIError(f"Only the owner of this computer can {what}.", HTTPStatus.FORBIDDEN)

    def _sign_ins_view(self) -> list[dict[str, Any]]:
        mine = self._principal.get("id") if self._principal.get("kind") == "sign_in" else None
        # The owner sees everyone; a member sees only their own sign-ins.
        return [{"id": s["id"], "email": s["email"], "created": s["created"], "last_seen": s["last_seen"],
                 "expires": s["expires"], "current": s["id"] == mine}
                for s in self.server.sign_ins.list() if self._is_owner() or s["email"] == self._principal.get("email")]

    def _supplied_token(self) -> str:
        supplied = ""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[7:]
        else:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if COOKIE_NAME in cookie:
                supplied = cookie[COOKIE_NAME].value
        if not supplied:
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if "token" in query and query["token"]:
                supplied = query["token"][0]
        return supplied

    def _body(self) -> dict[str, Any]:
        if self.headers.get("X-Orchestrator-UI") != "1" or "application/json" not in self.headers.get("Content-Type", ""):
            raise UIError("Missing UI headers", HTTPStatus.FORBIDDEN)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise UIError("Request too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self._body_read = True
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise UIError("Invalid JSON")
        if not isinstance(data, dict):
            raise UIError("Invalid JSON")
        return data

    # -- routing

    def do_OPTIONS(self) -> None:
        origin = self.headers.get("Origin", "")
        if not origin or not self._origin_allowed(origin):
            self.send_response(HTTPStatus.FORBIDDEN)
            self.send_header("Vary", "Origin")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Orchestrator-UI")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        self._body_read = False
        self._principal: dict[str, Any] | None = None  # set per request: a kept-alive connection reuses this handler
        try:
            self._dispatch_inner(method)
        finally:
            self._drain_body()

    def _drain_body(self) -> None:
        """Read and discard a request body no route consumed (a DELETE with a JSON body, a rejected POST).
        Left unread it would be parsed as the start of the next request on a kept-alive connection."""
        if getattr(self, "_body_read", True):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return
        if length > 1_000_000:
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except OSError:
            self.close_connection = True
        self._body_read = True

    def _product_route(self, root: Path, method: str, parts: list[str], query: dict[str, list[str]]) -> None:
        """/api/product...: the one product requirements document, its history, settings, designs and AI help."""
        doc = prd_doc.Prd(root, runtime_dir(root))
        doc.migrate_legacy()
        try:
            if method == "GET" and parts == ["product"]:
                self._json(doc.overview())
            elif method == "GET" and len(parts) == 3 and parts[1] == "design":
                name = parts[2]
                folder = root / prd_doc.DESIGNS_DIR
                target = folder / name
                if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or not target.is_file():
                    raise UIError("That design isn't there.", HTTPStatus.NOT_FOUND)
                inline = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(target.suffix.lower())
                # Only real images are shown in place. Anything else (HTML, SVG, PDF) is a download, so an uploaded file can never run in the app's origin.
                headers: dict[str, str] = {}
                if not inline:
                    headers["Content-Disposition"] = f'attachment; filename="{name}"'
                self._send(HTTPStatus.OK, target.read_bytes(), inline or "application/octet-stream", headers)
            elif method == "GET" and len(parts) == 3 and parts[1] == "history":
                v = doc.version(parts[2])
                older = [x for x in doc._versions() if x["at"] < v["at"]]
                self._json({"id": v["id"], "at": v["at"], "source": v["source"], "summary": v["summary"], "job": v.get("job", ""), "content": v["content"],
                            "diff": prd_doc.unified_diff(older[-1]["content"] if older else "", v["content"], "before", "this version")})
            elif method == "POST" and parts == ["product"]:
                body = self._body()
                source = _choice(body, "source", ["you", "import", "draft"]) if body.get("source") else "you"
                summary = _text(body, "summary", limit=300)
                if "section" in body:
                    doc.set_section(str(body["section"]), str(body.get("body") or ""), source, summary)
                else:
                    doc.write(str(body.get("text") or ""), source, summary)
                self._json(doc.overview())
            elif method == "POST" and parts == ["product", "settings"]:
                doc.set_auto_update(bool(self._body().get("auto_update")))
                self._json(doc.overview())
            elif method == "POST" and parts == ["product", "dismiss"]:
                doc.dismiss_notice()
                self._json({"ok": True})
            elif method == "POST" and parts == ["product", "revert"]:
                doc.revert(_text(self._body(), "id", required=True))
                self._json(doc.overview())
            elif method == "POST" and parts == ["product", "reference"]:
                body = self._body()
                picked = _clean_links(body.get("links"))
                if picked:  # items chosen from a connected app (a Figma frame, say): the link, plus its image when there is one
                    try:
                        contexts = integrations.build_context(picked, saved_integrations(root))
                    except integrations.IntegrationError as exc:
                        raise integration_error(exc)
                    for ctx in contexts:
                        doc.add_reference(f"{PROVIDER_NAMES.get(ctx.item.provider, ctx.item.provider)}: {ctx.item.title}", ctx.item.url or ctx.item.ref)
                        img = _save_image(root, root / prd_doc.DESIGNS_DIR, ctx)
                        if img:
                            doc.add_reference(f"{ctx.item.title} (image)", f"designs/{Path(img).name}")
                else:
                    url = _text(body, "url", required=True, limit=2000)
                    if not re.match(r"https?://", url):
                        raise UIError("Use a link that starts with http:// or https://")
                    doc.add_reference(_text(body, "label", limit=120) or url, url)
                self._json(doc.overview())
            elif method == "POST" and parts == ["product", "design"]:
                data = self._read_upload()
                saved = save_upload(root, (query.get("name") or [""])[0], data, folder=root / prd_doc.DESIGNS_DIR)
                if saved["kind"] == "log":
                    raise UIError("Add an image, PDF, Figma export or HTML file. Logs belong on a job.")
                doc.add_reference(saved["name"], f"designs/{Path(saved['path']).name}")
                self._json(doc.overview())
            elif method == "GET" and len(parts) == 3 and parts[1] == "task":
                task = self.server.tasks.get(parts[2])
                if task is None:
                    raise UIError("That request is no longer around. Start it again.", HTTPStatus.NOT_FOUND)
                self._json(task)
            elif method == "POST" and parts == ["product", "draft"]:
                if not prd_doc.can_draft(root):
                    raise UIError("There's nothing in this project to read yet (no README, notes or code). Describe it in your own words instead.")
                commits = git(root, "log", "--format=%s", "-n", "30").splitlines()
                current = doc.read() or prd_doc.template()
                digest = prd_doc.project_digest(root, commits)
                self._start_proposal(root, current, prd_doc.draft_prompt(digest, current), lambda reply: prd_doc.parse_draft(reply, current), 270)
            elif method == "POST" and parts == ["product", "import"]:
                if (self.headers.get("Content-Type") or "").startswith("application/json"):
                    body = self._body()
                    source, name = _text(body, "text", required=True, limit=prd_doc.MAX_IMPORT_CHARS * 2), "pasted text"
                else:
                    name = (query.get("name") or [""])[0]
                    source = prd_doc.extract_text(name, self._read_upload())
                if not source.strip():
                    raise UIError("There was no text in that.")
                current = doc.read() or prd_doc.template()
                self._start_proposal(root, current, prd_doc.import_prompt(source, name), prd_doc.parse_proposal, 240)
            else:
                raise UIError("Not found", HTTPStatus.NOT_FOUND)
        except prd_doc.PrdError as exc:
            raise UIError(str(exc), HTTPStatus.BAD_GATEWAY if "model" in str(exc).lower() else HTTPStatus.BAD_REQUEST)

    def _start_proposal(self, root: Path, current: str, prompt: str, parse: Callable[[str], dict[str, str]], timeout: int) -> None:
        """Ask the model for a proposed document in the background; the page polls /api/product/task/<id> for the diff."""
        def work() -> dict[str, Any]:
            try:
                proposal = parse(self._model_call(root, prompt, timeout=timeout))
            except prd_doc.PrdError:
                # Models sometimes answer in prose or break the JSON. Say what format is needed and ask once more before giving up.
                proposal = parse(self._model_call(root, prompt + prd_doc.FORMAT_REMINDER, timeout=timeout))
            return {**proposal, "diff": prd_doc.unified_diff(current, proposal["markdown"])}

        self._json({"task": self.server.tasks.start(work)}, HTTPStatus.ACCEPTED)

    def _read_upload(self) -> bytes:
        """The raw request body as a file upload, refused (without reading it) when it is over the limit."""
        if self.headers.get("X-Orchestrator-UI") != "1":
            raise UIError("Missing UI headers", HTTPStatus.FORBIDDEN)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > new_job_form.UPLOAD_LIMIT:
            self.close_connection = True  # the body is not read, so end this connection rather than leave it in the stream
            raise UIError(f"That file is over {new_job_form.UPLOAD_LIMIT // (1024 * 1024)} MB", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self._body_read = True
        return self.rfile.read(length) if length > 0 else b""

    _attempted = ""
    _attempted_how = ""

    def _sign_in(self, body: dict[str, Any]) -> tuple[str, str | None, str]:
        """Who is signing in, how, and the token they get: (token, email, how). Raises UIError when they may not."""
        self._attempted, self._attempted_how = "", ""
        token = str(body.get("token") or "").strip()
        id_token = str(body.get("id_token") or body.get("idToken") or "").strip()
        ticket = str(body.get("ticket") or "").strip()
        if ticket:
            # From the hosted app: the control plane vouches for who this is, for this computer only, once.
            self._attempted_how = "hosted app"
            machine = account.load_machine()
            if machine is None:
                raise UIError("This computer isn't connected to an account. Run `orchestrator connect` on it.", HTTPStatus.CONFLICT)
            try:
                claims = account.verify_ticket(ticket, machine)
            except account.AccountError as exc:
                raise UIError(str(exc), HTTPStatus.UNAUTHORIZED)
            email = str(claims["email"]).lower()
            self._attempted = email
            if not self.server.use_ticket_nonce(claims["nonce"], float(claims["exp"])):
                raise UIError("That sign-in link was already used. Sign in again.", HTTPStatus.UNAUTHORIZED)
            self.server.forget_allowed_emails()
            if email not in self.server.allowed_emails():
                raise UIError(f"Email {email} is not authorized for this computer.", HTTPStatus.FORBIDDEN)
            return self.server.sign_ins.create(email), email, "hosted app"
        if id_token:
            self._attempted_how = "Google, Apple or GitHub"
            user_info = verify_firebase_id_token(id_token)
            email = (user_info.get("email") or "").strip().lower()
            self._attempted = email
            if not email:
                raise UIError("This account didn't share an email address. With GitHub, add a verified "
                              "primary email to your profile (it can stay private).", HTTPStatus.FORBIDDEN)
            if user_info.get("emailVerified") is False:
                raise UIError(f"The email {email} isn't verified with that provider.", HTTPStatus.FORBIDDEN)
            self.server.forget_allowed_emails()  # just added under Configuration? Count it now.
            allowed = self.server.allowed_emails()
            if not allowed:
                raise UIError(
                    "No sign-in emails are allowed yet, so sign-in is closed. On the computer running Orchestrator, "
                    "set ORCHESTRATOR_ALLOWED_EMAILS, set git config user.email, or add your email under "
                    "Configuration (the access-token sign-in still works).",
                    HTTPStatus.FORBIDDEN)
            if email not in allowed:
                # Not the list of who is allowed: anyone with a Google account can reach this far.
                raise UIError(f"Email {email} is not authorized for this computer. Ask its owner to add it "
                              "under Configuration, Who can sign in.", HTTPStatus.FORBIDDEN)
            # Their own token, not the server's: it expires, and it can be revoked without locking anyone else out.
            return self.server.sign_ins.create(email), email, "Google, Apple or GitHub"
        if token:
            self._attempted_how = "access token"
            if not hmac.compare_digest(token, self.server.token):
                raise UIError("Invalid access token", HTTPStatus.UNAUTHORIZED)
            return token, None, "access token"
        raise UIError("Access token or ID token required", HTTPStatus.BAD_REQUEST)

    def _dispatch_inner(self, method: str) -> None:
        if not self._host_ok():
            self._error(HTTPStatus.FORBIDDEN, "Unexpected Host header")
            return
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if not self.server.accepting_requests:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Remote access is turned off.")
                return
            if url.path == "/desktop/open":
                if not self.server.desktop_listener or method != "GET":
                    self._error(HTTPStatus.NOT_FOUND, "Not found")
                    return
                route = self.server.browser_grants.consume((query.get("grant") or [""])[0])
                if route is None:
                    self._error(HTTPStatus.UNAUTHORIZED, "Open Orchestrator again from the Mac menu.")
                    return
                self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {
                    "Location": "/" + route,
                    "Set-Cookie": f"{COOKIE_NAME}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"})
                return
            origin = self.headers.get("Origin")
            if method in ("POST", "DELETE") and origin and not self._origin_allowed(origin):
                raise UIError("Unexpected request origin", HTTPStatus.FORBIDDEN)
            if method == "GET" and not url.path.startswith("/api/"):
                self._static(url.path, query)
                return
            if method == "POST" and url.path == "/api/auth":
                body = self._body()
                try:
                    issued, authed_email, how = self._sign_in(body)
                except UIError as exc:
                    if exc.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.CONFLICT):
                        self._audit("sign_in_refused", who=self._attempted or "unknown", how=self._attempted_how, reason=str(exc))
                    raise
                self._audit("sign_in", who=authed_email or "access token", how=how)
                self._json({"ok": True, "token": issued, "email": authed_email}, extra={
                    "Set-Cookie": f"{COOKIE_NAME}={issued}; HttpOnly; SameSite=Strict; Path=/"
                })
                return
            self._principal = self._authenticate()
            if self._principal is None:
                self._error(HTTPStatus.UNAUTHORIZED, "Sign in again, or open the URL printed by 'orchestrator ui' (it carries the access token).")
                return
            if method in ("POST", "DELETE"):
                # Every change needs the page's own header, checked once here rather than wherever a body happens to be
                # read: another site can't set it, so it can't make a signed-in browser change things or start (and bill)
                # model runs. Requests with no body are covered too.
                if self.headers.get("X-Orchestrator-UI") != "1":
                    raise UIError("Missing UI headers", HTTPStatus.FORBIDDEN)
                with self.server.gate.admit():
                    self._api(method, url.path, query)
            else:
                self._api(method, url.path, query)
        except BusyError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except UIError as exc:
            self._error(exc.status, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # surface, don't crash the handler thread
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    def _model_call(self, root: Path, prompt: str, model: str = "", timeout: int = 150) -> str:
        """One read-only model turn (prompt in, answer out) in a child process, so a slow or failing model can't hang the server."""
        # The model works in an empty scratch folder, not the project: an agentic model (one with file tools) asked for an answer
        # would otherwise be free to write files into the repository. Everything it needs is in the prompt.
        with tempfile.TemporaryDirectory(prefix="orchestrator-model-") as scratch:
            argv = orchestrator_argv("script", "job_chat_run.py", *(["--model", model] if model else []), "--timeout", str(max(30, timeout - 15)), "--cwd", scratch)
            try:
                res = subprocess.run(argv, input=prompt, cwd=root, env=self.server.child_env(), capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise UIError("The model took too long. Try a shorter request.", HTTPStatus.GATEWAY_TIMEOUT)
        marker = "<<<ORCHESTRATOR-REPLY>>>"
        if res.returncode != 0 or marker not in res.stdout:
            raise UIError((res.stderr.strip().splitlines() or ["The model couldn't answer."])[-1], HTTPStatus.BAD_GATEWAY)
        return res.stdout.split(marker, 1)[1]

    def _static(self, path: str, query: dict[str, list[str]]) -> None:
        token = (query.get("token") or [""])[0]
        if path in ("/", "/index.html") and token:
            if not hmac.compare_digest(token, self.server.token):
                self._error(HTTPStatus.UNAUTHORIZED, "Invalid token")
                return
            # Trade the URL token for a cookie, then drop it from the address bar.
            self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {
                "Location": "/",
                "Set-Cookie": f"{COOKIE_NAME}={self.server.token}; HttpOnly; SameSite=Strict; Path=/",
            })
            return
        rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        target = safe_resolve(STATIC_DIR / rel)
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "Not found")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type, {
            "Content-Security-Policy": (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net https://www.gstatic.com https://apis.google.com; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "img-src 'self' data: blob: https://*.googleusercontent.com https://lh3.googleusercontent.com; "
                "connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://*.googleapis.com https://*.firebaseapp.com; "
                "frame-src 'self' https://swift-orch-web-20260923.firebaseapp.com https://*.firebaseapp.com; "
                "frame-ancestors 'none'"
            ),
        })

    def _api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        parts = [p for p in path.split("/") if p][1:]  # drop "api"
        from orchestrator.web.bootstrap import dispatch_bootstrap
        if dispatch_bootstrap(self, method, parts):
            return
        root = self.server.root

        if method == "GET" and parts == ["state"]:
            visible_runs = self._sessions_view()
            self._json({"project": project_state(root), "runs": visible_runs,
                        **inbox_state(root, self.server.sessions, runs=visible_runs),
                        "product_notice": prd_doc.Prd(root, runtime_dir(root)).notice(),
                        "alerts": {"webhook": bool(read_settings(root).get("notification_webhook"))},
                        "actions": {k: {"title": a.title, "confirm": a.confirm, "fields": a.fields,
                                        "owner_only": k in OWNER_ONLY_ACTIONS}
                                    for k, a in ACTIONS.items()},
                        # The caller's own credential (so a cookie sign-in can also authorize event streams), never the server's.
                        "token": self._principal["token"],
                        "runner": {"version": account.package_version(), "api_version": account.API_VERSION},
                        "you": {"kind": self._principal["kind"], "email": self._principal.get("email"), "role": self._principal["role"]}})
        elif method == "POST" and parts == ["auth", "logout"]:
            if self._principal["kind"] == "sign_in":
                self.server.sign_ins.revoke_token(self._principal["token"])
                self._audit("sign_out")
            self._json({"ok": True}, extra={
                "Set-Cookie": f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
            })
        elif method == "GET" and parts == ["projects"]:
            self._json({"projects": all_projects_info(root), "active": str(safe_resolve(root))})
        elif method == "POST" and parts == ["projects", "scan"]:
            self._require_owner("look for projects on this computer")
            body = self._body()
            paths_input = body.get("paths")
            custom_paths = [Path(p).expanduser() for p in paths_input] if isinstance(paths_input, list) else None
            self._json({"discovered": scan_for_projects(custom_paths)})
        elif method == "POST" and parts == ["projects", "add"]:
            self._require_owner("add a project folder")
            body = self._body()
            target_str = str(body.get("root") or "").strip()
            if not target_str:
                raise UIError("Project root path is required")
            p = safe_resolve(Path(target_str).expanduser())
            if not p.is_dir():
                raise UIError(f"Directory not found: {p}")
            name = str(body.get("name") or "").strip() or project_display_name(p)
            remember_project(p, name, active=bool(body.get("active", False)))
            self._audit("project_added", project=name, path=str(p))
            if body.get("active"):
                self.server.set_root(p)
            self._json({"ok": True, "project": {"name": name, "root": str(p), "active": bool(body.get("active", False))}})
        elif method == "DELETE" and parts == ["projects"]:
            self._require_owner("remove a project")
            body = self._body()
            target_str = str(body.get("root") or "").strip()
            if not target_str:
                raise UIError("Project root path is required")
            p = safe_resolve(Path(target_str).expanduser())
            if p == safe_resolve(root):
                raise UIError("Cannot remove the active project")
            forget_project(p)
            self._audit("project_removed", path=str(p))
            self._json({"ok": True, "projects": all_projects_info(root)})
        elif method == "GET" and parts == ["jobs"]:
            running = self.server.sessions.running_job_ids()
            jobs = list_jobs(root)
            for job in jobs:
                job["active_run"] = job["id"] in running
            self._json({"jobs": jobs})
        elif method == "GET" and parts == ["analytics"]:
            self._json(analytics_overview(root))
        elif method == "POST" and parts == ["analytics", "plan"]:
            plan = analytics.tracking_plan(feature_store.load(runtime_dir(root)))
            target = root / "docs" / "analytics" / "tracking-plan.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(plan, encoding="utf-8")
            self._json({"ok": True, "path": str(target.relative_to(root))})
        elif method == "POST" and len(parts) == 3 and parts[0] == "features" and parts[2] == "kpis":
            body = self._body()
            op = _choice(body, "op", ["add", "update", "delete", "measure", "restore"])
            rt = runtime_dir(root)
            try:
                if op == "add":
                    feature_store.kpi_add(rt, parts[1], body)
                elif op == "update":
                    feature_store.kpi_update(rt, parts[1], str(body.get("kpi") or ""), body)
                elif op == "delete":
                    gone = next((k for f in feature_store.load(rt) if f["id"] == parts[1] for k in f.get("kpis", []) if k["id"] == str(body.get("kpi") or "")), None)
                    feature_store.kpi_delete(rt, parts[1], str(body.get("kpi") or ""))
                    self._json({**analytics_overview(root), "undo": {"kpi": gone}})
                    return
                elif op == "restore":
                    feature_store.kpi_restore(rt, parts[1], body.get("kpi") if isinstance(body.get("kpi"), dict) else {})
                else:
                    feature_store.kpi_measure(rt, parts[1], str(body.get("kpi") or ""), body.get("value"), str(body.get("note") or ""), str(body.get("decision") or ""))
            except feature_store.FeatureError as exc:
                raise UIError(str(exc), HTTPStatus.NOT_FOUND if "not found" in str(exc) else HTTPStatus.BAD_REQUEST)
            self._json(analytics_overview(root))
        elif method == "GET" and parts == ["health"]:
            self._json({**project_health.evaluate(project_facts(root)), "name": project_display_name(root)})
        elif method == "GET" and parts == ["recent-logs"]:
            self._json({"logs": recent_logs(root)})
        elif method == "POST" and parts == ["uploads"]:
            self._json(save_upload(root, (query.get("name") or [""])[0], self._read_upload()))
        elif parts and parts[0] == "product":
            self._product_route(root, method, parts, query)
        elif method == "GET" and parts and parts[0] == "docs":
            try:
                if parts == ["docs"]:
                    self._json(docs_index(root))
                elif len(parts) == 3 and parts[1] == "job":
                    title, md = docs_job_markdown(root, parts[2])
                    self._json({"title": title, "markdown": md})
                elif len(parts) == 3 and parts[1] == "feature":
                    title, md = docs_feature_markdown(root, parts[2])
                    self._json({"title": title, "markdown": md})
                elif parts == ["docs", "file"]:
                    rel = (query.get("path") or [""])[0]
                    md = project_docs.read_project_file(root, rel)
                    self._json({"path": rel, "title": next((f["title"] for f in project_docs.project_files(root) if f["path"] == rel), rel), "markdown": md})
                elif parts == ["docs", "export"]:
                    self._json({"markdown": docs_export(root), "name": f"{project_display_name(root)}-documentation.md"})
                else:
                    raise UIError("Not found", HTTPStatus.NOT_FOUND)
            except project_docs.DocsError as exc:
                raise UIError(str(exc), HTTPStatus.NOT_FOUND)
        elif method == "GET" and parts == ["preflight"]:
            self._json({"items": preflight_overview(root, refresh=bool(query.get("refresh")))})
        elif method == "GET" and parts == ["delivery"]:
            self._json(delivery_overview(root))
        elif method == "POST" and parts == ["delivery", "rerun"]:
            run_id = self._body().get("run_id")
            if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
                raise UIError("Which run?")
            try:
                res = subprocess.run(["gh", "run", "rerun", str(run_id), "--failed"], cwd=root, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise UIError(f"Couldn't reach GitHub: {exc}", HTTPStatus.BAD_GATEWAY)
            if res.returncode != 0:
                raise UIError((res.stderr or res.stdout).strip().splitlines()[-1][:200] if (res.stderr or res.stdout).strip() else "GitHub refused the re-run.", HTTPStatus.BAD_GATEWAY)
            _GH_CACHE.clear()  # so the list shows the run starting
            self._json({"ok": True})
        elif method == "GET" and parts == ["test-cases"]:
            self._json(test_case_view(root, test_case_lib.load_library(root)))
        elif method == "POST" and parts == ["test-cases"]:
            body = self._body()
            op = _choice(body, "op", ["create", "edit", "delete"])
            try:
                if op == "delete":
                    case_id = str(body.get("id") or "").strip()
                    if not case_id:
                        raise UIError("Which test case?")
                    test_case_lib.delete_library_case(root, case_id)
                else:
                    test_case_lib.create_or_update_library_case(root, body.get("case") if isinstance(body.get("case"), dict) else body, is_new=(op == "create"))
            except (test_case_lib.TestCaseError, ValueError) as exc:
                raise UIError(str(exc))
            self._json({"ok": True, **test_case_view(root, test_case_lib.load_library(root))})
        elif method == "GET" and parts == ["inbox"]:
            self._json(inbox_overview(root, self.server.sessions, runs=self._sessions_view()))
        elif method == "GET" and parts == ["features"]:
            self._json({**features_overview(root), "plan": self.server.plan_view(root)})
        elif method == "GET" and parts == ["plan-run"]:  # the Features page's light poll while a plan builds
            self._json({"plan": self.server.plan_view(root)})
        elif method == "POST" and parts == ["plan-run"]:
            body = self._body()
            chosen = body.get("features")
            if not isinstance(chosen, list):  # by default: every planned feature that has no jobs yet, in build order
                rolled = feature_store.rollup(feature_store.load(runtime_dir(root)), list_jobs(root) + archived_feature_jobs(root))
                chosen = [f["id"] for f in sorted(rolled, key=lambda f: f["layer"]) if f["status"] == "planned" and not f["jobs_total"]]
            try:
                with self.server._plan_lock:
                    plan_run.start(runtime_dir(root), [str(c) for c in chosen], feature_store.load(runtime_dir(root)),
                                   body.get("auto_approve") is True)
            except plan_run.PlanRunError as exc:
                raise UIError(str(exc))
            self._audit("plan_run_started", features=len(chosen))
            self._json({"plan": self.server.plan_tick() or self.server.plan_view(root)}, HTTPStatus.CREATED)  # first layer now, not in 15 s
        elif method == "POST" and parts in (["plan-run", "pause"], ["plan-run", "stop"]):
            body = self._body()
            with self.server._plan_lock:
                run = plan_run.load(runtime_dir(root))
                if not run:
                    raise UIError("No plan is being built.", HTTPStatus.NOT_FOUND)
                if parts[1] == "stop":
                    plan_run.clear(runtime_dir(root))  # nothing new starts; jobs already running carry on
                else:
                    run["paused"] = body.get("paused") is True
                    if not run["paused"]:  # resuming retries features whose planning didn't finish
                        alive = self.server._planning_alive(run.get("starting") or {})
                        run["starting"] = {fid: entry for fid, entry in (run.get("starting") or {}).items()
                                           if alive[fid] and time.time() - float(entry.get("at", 0)) < plan_run.STARTING_TIMEOUT}
                    plan_run.save(runtime_dir(root), run)
            stopped = parts[1] == "stop"
            self._json({"plan": None if stopped else (self.server.plan_tick() if not run["paused"] else None) or self.server.plan_view(root)})
        elif method == "POST" and parts == ["features", "propose"]:
            # Draft a feature map from the product requirements in the background; the page polls /api/product/task/<id>.
            prd_text = prd_doc.Prd(root, runtime_dir(root)).read() or ""
            if not prd_doc.is_filled_doc(prd_text):
                raise UIError("Write the product requirements first (Product), so there's something to plan features from.")
            existing = feature_store.load(runtime_dir(root))
            request = feature_map.prompt(prd_text, existing, prd_doc.project_digest(root))

            def work() -> dict[str, Any]:
                try:
                    return feature_map.parse(self._model_call(root, request, timeout=240), existing)
                except feature_map.FeatureMapError:
                    return feature_map.parse(self._model_call(root, request + prd_doc.FORMAT_REMINDER, timeout=240), existing)

            self._json({"task": self.server.tasks.start(work)}, HTTPStatus.ACCEPTED)
        elif method == "POST" and parts == ["features", "accept"]:
            body = self._body()
            existing = feature_store.load(runtime_dir(root))
            try:  # what comes back from the page is checked again, never trusted
                proposal = feature_map.validate(body.get("features"), existing)
                feature_map.accept(runtime_dir(root), proposal["features"], [str(c) for c in body.get("chosen") or [] if isinstance(c, str)])
            except (feature_map.FeatureMapError, feature_store.FeatureError) as exc:
                raise UIError(str(exc))
            self._json(features_overview(root))
        elif method == "POST" and parts == ["features"]:
            body = self._body()
            try:
                feature_store.create(runtime_dir(root), str(body.get("name") or ""), str(body.get("summary") or ""), body.get("paths"), body.get("depends_on"), str(body.get("serves") or ""))
            except feature_store.FeatureError as exc:
                raise UIError(str(exc))
            self._json(features_overview(root))
        elif method == "POST" and parts == ["features", "restore"]:
            body = self._body()
            try:
                feature = feature_store.restore(runtime_dir(root), body.get("feature"))
            except feature_store.FeatureError as exc:
                raise UIError(str(exc))
            for job_id in [j for j in (body.get("jobs") or []) if isinstance(j, str)][:500]:
                try:
                    job_path = resolve_job_path(root, job_id)
                except UIError:
                    continue
                job = read_json_file(job_path)
                if job and not job.get("feature"):
                    job["feature"] = feature["id"]
                    write_json_file(job_path, job)
            self._json(features_overview(root))
        elif method == "POST" and len(parts) == 2 and parts[0] == "features":
            body = self._body()
            try:
                if "status" in body:
                    feature_store.set_status(runtime_dir(root), parts[1], str(body["status"]))
                fields = {k: body[k] for k in ("name", "summary", "paths", "depends_on", "serves") if k in body}
                if fields:
                    feature_store.update(runtime_dir(root), parts[1], **fields)
            except feature_store.FeatureError as exc:
                raise UIError(str(exc), HTTPStatus.NOT_FOUND if "not found" in str(exc) else HTTPStatus.BAD_REQUEST)
            self._json(features_overview(root))
        elif method == "DELETE" and len(parts) == 2 and parts[0] == "features":
            record = next((f for f in feature_store.load(runtime_dir(root)) if f["id"] == parts[1]), None)
            try:
                feature_store.delete(runtime_dir(root), parts[1])
            except feature_store.FeatureError as exc:
                raise UIError(str(exc), HTTPStatus.NOT_FOUND)
            moved = []
            for path in jobs_dir(root).glob("*.json"):  # jobs on a deleted feature become unassigned
                job = read_json_file(path)
                if job.get("feature") == parts[1]:
                    job.pop("feature")
                    write_json_file(path, job)
                    moved.append(path.stem)
            self._json({**features_overview(root), "undo": {"feature": record, "jobs": moved}})
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "scope":
            job_path = resolve_job_path(root, parts[1])
            job = read_json_file(job_path)
            body = self._body()
            op = _choice(body, "op", ["accept", "reset", "adopt"])
            if op == "accept":
                raw = body.get("paths")
                if not isinstance(raw, list) or not raw or len(raw) > 200:
                    raise UIError("Choose the files to accept")
                job["scope_accepted"] = sorted(set(job.get("scope_accepted") or []) | {str(p)[:300] for p in raw})
            elif op == "adopt":
                raw = body.get("paths")
                if not isinstance(raw, list) or len(raw) > 200:
                    raise UIError("Choose the files to adopt")
                if "plan" not in job or not isinstance(job["plan"], dict):
                    job["plan"] = {}
                existing = set(job["plan"].get("likely_files") or [])
                job["plan"]["likely_files"] = sorted(existing | {str(p)[:300] for p in raw if str(p).strip()})
            else:
                job.pop("scope_accepted", None)
            write_json_file(job_path, job)
            self._json({"ok": True, "scope": job_scope(root, job)})
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "plan-tasks":
            job_path = resolve_job_path(root, parts[1])
            if parts[1] in self.server.sessions.running_job_ids():
                raise UIError("A worker is running this job. Pause it before changing its plan.", HTTPStatus.CONFLICT)
            job = read_json_file(job_path)
            body = self._body()
            op = _choice(body, "op", ["edit", "add", "remove", "move"])
            try:
                if op == "edit":
                    plan_edit.edit(job, body.get("index"), body)
                elif op == "add":
                    plan_edit.add(job, body)
                elif op == "remove":
                    plan_edit.remove(job, body.get("index"))
                else:
                    plan_edit.move(job, body.get("index"), str(body.get("direction") or ""))
            except plan_edit.PlanEditError as exc:
                raise UIError(str(exc))
            write_json_file(job_path, job)
            self._json({"ok": True, "tasks": job["plan"]["tasks"], "completed": job.get("completed_task_indices", [])})
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "plan-test-cases":
            job_path = resolve_job_path(root, parts[1])
            if parts[1] in self.server.sessions.running_job_ids():
                raise UIError("A worker is running this job. Pause it before changing its test cases.", HTTPStatus.CONFLICT)
            job = read_json_file(job_path)
            body = self._body()
            op = _choice(body, "op", ["add", "edit", "remove"])
            issue_num = job.get("issue_number")
            if not issue_num:
                m = re.search(r"issue-(\d+)", str(job.get("branch") or ""))
                if m:
                    issue_num = int(m.group(1))
                else:
                    m2 = re.search(r"-(\d+)$", parts[1])
                    issue_num = int(m2.group(1)) if m2 else parts[1]
            try:
                if op == "add":
                    plan_edit.add_case(job, body, issue_number=issue_num)
                elif op == "edit":
                    plan_edit.edit_case(job, str(body.get("id") or ""), body)
                else:
                    plan_edit.remove_case(job, str(body.get("id") or ""))
            except plan_edit.PlanEditError as exc:
                raise UIError(str(exc))
            write_json_file(job_path, job)
            self._json({"ok": True, "test_cases": test_case_view(root, test_case_lib.job_cases(job), {c["id"] for c in test_case_lib.due_cases(job)})})
        elif method in ("GET", "POST") and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "brief":
            job_path = resolve_job_path(root, parts[1])
            job = read_json_file(job_path)
            out_dir = runtime_dir(root) / "output" / parts[1]
            brief_file = out_dir / "brief.md"
            if method == "POST":
                if parts[1] in self.server.sessions.running_job_ids():
                    raise UIError("A worker is running this job. Pause it before editing its brief.", HTTPStatus.CONFLICT)
                body = self._body()
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise UIError("Brief text cannot be empty.")
                out_dir.mkdir(parents=True, exist_ok=True)
                brief_file.write_text(text, encoding="utf-8")
                job["updated_at"] = datetime.now().isoformat()
                write_json_file(job_path, job)
            else:
                text = read_limited(brief_file) if brief_file.is_file() else None
                if text is None:
                    text = generate_job_brief(job)
            try:
                rel_path = str(brief_file.relative_to(root))
            except ValueError:
                rel_path = f".orchestrator/output/{parts[1]}/brief.md"
            try:
                runtime_path = str(brief_file.relative_to(runtime_dir(root)))
            except ValueError:
                runtime_path = f"output/{parts[1]}/brief.md"
            self._json({"ok": True, "text": text, "path": rel_path, "runtime_path": runtime_path})
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "revert-task":
            job_path = resolve_job_path(root, parts[1])
            if parts[1] in self.server.sessions.running_job_ids():
                raise UIError("A worker is running this job. Pause it first.", HTTPStatus.CONFLICT)
            job = read_json_file(job_path)
            index = self._body().get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                raise UIError("Which task?")
            try:
                commit = task_revert.revert_latest(root, job, index)
            except task_revert.RevertError as exc:
                raise UIError(str(exc), HTTPStatus.CONFLICT)
            job["updated_at"] = datetime.now().isoformat()
            write_json_file(job_path, job)
            self._json({"ok": True, "commit": commit, "completed": job.get("completed_task_indices", [])})
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "feature":
            job_path = resolve_job_path(root, parts[1])
            job = read_json_file(job_path)
            target = str(self._body().get("feature") or "").strip()
            try:
                if target:
                    feature_store.note_work_attached(runtime_dir(root), target, job_state(job)["group"])
                    job["feature"] = target
                else:
                    job.pop("feature", None)
            except feature_store.FeatureError as exc:
                raise UIError(str(exc), HTTPStatus.NOT_FOUND)
            write_json_file(job_path, job)
            self._json({"ok": True, "feature": job.get("feature")})
        elif method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "diff":
            job = read_json_file(resolve_job_path(root, parts[1]))
            self._json(job_file_diff(root, job, (query.get("path") or [""])[0]))
        elif method == "GET" and len(parts) == 2 and parts[0] == "jobs":
            detail = job_detail(root, parts[1])
            detail["runs"] = self._sessions_view(job_id=parts[1])
            self._json(detail)
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "reference":
            job_path = resolve_job_path(root, parts[1])
            job = read_json_file(job_path)
            body = self._body()
            url = str(body.get("url") or "").strip()
            note = str(body.get("note") or "").strip()
            if not url:
                raise UIError("URL or file path is required")
            refs = job.get("reference_artifacts") or []
            refs.append({"url": url, "note": note, "added_at": datetime.now().isoformat()})
            job["reference_artifacts"] = refs
            write_json_file(job_path, job)
            self._json({"ok": True, "reference_artifacts": refs})
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "models":
            job_path = resolve_job_path(root, parts[1])
            job = read_json_file(job_path)
            body = self._body()
            planner = str(body.get("planner") or "").strip()
            builder = str(body.get("builder") or "").strip()
            reviewer = str(body.get("reviewer") or "").strip()
            if planner: job["planner"] = planner
            if builder: job["builder"] = builder
            if reviewer: job["reviewer"] = reviewer
            write_json_file(job_path, job)
            self._json({"ok": True, "planner": job.get("planner"), "builder": job.get("builder"), "reviewer": job.get("reviewer")})
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "chat":
            job_path = resolve_job_path(root, parts[1])

            def llm(prompt: str, model: str) -> str:
                return self._model_call(root, prompt, model)

            try:
                thread = job_chat.ask(job_path, str(self._body().get("message") or ""), llm, root, read_json_file, write_json_file)
            except job_chat.ChatError as exc:
                raise UIError(str(exc))
            self._json({"ok": True, "conversation": thread})
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "close_issue":
            job_path = resolve_job_path(root, parts[1])
            job = read_json_file(job_path)
            issue_num = job.get("issue_number")
            if not issue_num:
                raise UIError("Job has no issue number")
            try:
                subprocess.run(["gh", "issue", "close", str(issue_num)], cwd=str(root), check=True)
            except Exception as e:
                raise UIError(f"Failed to close issue #{issue_num}: {e}")
            job["status"] = "completed"
            write_json_file(job_path, job)
            self._json({"ok": True, "closed": issue_num})
        elif ((method == "DELETE" and len(parts) == 2 and parts[0] == "jobs") or
              (method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "delete")):
            job_id = parts[1]
            if job_id in self.server.sessions.running_job_ids():
                raise UIError("This job is running right now. Stop it before deleting.", HTTPStatus.CONFLICT)
            body = self._body() if (method == "POST" or self.headers.get("Content-Length")) else {}
            revert = bool(body.get("revert", False))
            delete_job(root, job_id, revert=revert)
            self._json({"ok": True, "deleted": job_id, "reverted": revert})
        elif method == "GET" and parts == ["file"]:
            target = resolve_runtime_file(root, (query.get("path") or [""])[0])
            relative = target.relative_to(safe_resolve(runtime_dir(root)))
            if not self._is_owner() and (not relative.parts or relative.parts[0] not in {"jobs", "output"}):
                self._require_owner("read private Orchestrator files")
            if target.stat().st_size > MAX_FILE_BYTES:
                with target.open("rb") as fh:
                    fh.seek(-MAX_FILE_BYTES, os.SEEK_END)
                    text = "[showing the last 2 MB]\n" + fh.read().decode("utf-8", "replace")
            else:
                text = target.read_text(encoding="utf-8", errors="replace")
            self._json({"path": str(target.relative_to(safe_resolve(runtime_dir(root)))), "text": text})
        elif method == "GET" and parts == ["new-project"]:
            self._require_owner("create a project")
            self._json(new_project_state())
        elif method == "POST" and parts == ["new-project", "draft"]:
            self._require_owner("create a project")
            self._json({"draft": new_project.save_draft(self._body())})
        elif method == "POST" and parts == ["new-project", "discard"]:
            self._require_owner("create a project")
            self._body()
            new_project.clear_draft()
            self._json({"ok": True})
        elif method == "POST" and parts == ["new-project", "create"]:
            self._require_owner("create a project")
            self._body()
            self._json(create_new_project(self.server))
        elif method == "POST" and parts == ["new-project", "publish"]:
            self._require_owner("publish a project")
            body = self._body()
            self._json({"step": publish_known_project(str(body.get("root") or ""), str(body.get("visibility") or "private"))})
        elif method == "GET" and parts == ["integrations"]:
            self._json({"integrations": integrations.public_catalog(saved_integrations(root), read_settings(root).get("integration_options"))})
        elif method == "POST" and len(parts) == 3 and parts[0] == "integrations" and parts[2] == "options":
            self._require_owner("change connected app settings")
            self._json({"options": save_integration_options(root, parts[1], self._body().get("options"))})
            self._audit("settings_changed", part="integrations")
        elif method == "POST" and len(parts) == 3 and parts[0] == "integrations" and parts[2] == "connect":
            self._require_owner("change connected app credentials")
            self._json(connect_integration(root, parts[1], self._body().get("values") or {}))
            self._audit("settings_changed", part="integrations")
        elif method == "POST" and len(parts) == 3 and parts[0] == "integrations" and parts[2] == "disconnect":
            self._require_owner("change connected app credentials")
            self._body()
            disconnect_integration(root, parts[1])
            self._audit("settings_changed", part="integrations")
            self._json({"ok": True})
        elif method == "GET" and len(parts) == 3 and parts[0] == "integrations" and parts[2] == "search":
            self._json({"items": search_integration(root, parts[1], (query.get("q") or [""])[0])})
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "links":
            if parts[1] in self.server.sessions.running_job_ids():
                raise UIError("This job is running right now. Attach context once it stops.", HTTPStatus.CONFLICT)
            self._json({"links": attach_links_to_job(root, parts[1], _clean_links(self._body().get("links")))})
        elif method == "GET" and parts == ["setup"]:
            self._json(setup_checklist(root, runtime_dir(root)))
        elif method == "GET" and parts == ["ai-providers"]:
            self._json(ai_providers_state(root))
        elif method == "GET" and parts == ["config"]:
            state = {**config_state(root), "sign_ins": self._sign_ins_view()}
            if self._is_owner():
                state["audit"] = self.server.audit.recent(30)
            else:  # who else can sign in is the owner's business
                state["allowed_emails"] = [e for e in state["allowed_emails"] if e["email"] == self._principal.get("email")]
                for owner_field in ("keys", "ollama_host", "email", "analytics", "webhook", "instructions",
                                    "machines", "firebase", "role_prompts", "menus"):
                    state.pop(owner_field, None)
            state["viewer"] = {"role": self._principal["role"], "email": self._principal.get("email")}
            self._json(state)
        elif method == "POST" and parts == ["sign-ins", "revoke"]:
            target = _text(self._body(), "id", required=True, limit=40)
            if not self._is_owner() and not any(x["id"] == target for x in self._sign_ins_view()):
                self._require_owner("end someone else's sign-in")
            if not self.server.sign_ins.revoke(target):
                raise UIError("That sign-in has already ended.", HTTPStatus.NOT_FOUND)
            self._audit("sign_in_ended", sign_in=target)
            self._json({"sign_ins": self._sign_ins_view()})
        elif method == "POST" and parts == ["config", "allowed-email"]:
            self._require_owner("change who can sign in")
            body = self._body()
            result = config_update(root, "allowed-email", body)
            self.server.forget_allowed_emails()
            if body.get("op") == "remove":
                self.server.sign_ins.revoke_email(str(body.get("email") or ""))
            self._audit("email_allowed" if body.get("op") == "add" else "email_removed", email=str(body.get("email") or "").lower())
            self._json(result)
        elif method == "GET" and parts == ["config", "doc"]:
            wanted = (query.get("id") or [""])[0]
            entry = next((d for d in doc_entries(root) if d["id"] == wanted), None)
            if not entry:
                raise UIError("Document not found", HTTPStatus.NOT_FOUND)
            self._json({"name": entry["name"], "text": Path(entry["path"]).read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]})
        elif method == "POST" and len(parts) == 2 and parts[0] == "config":
            if parts[1] in OWNER_ONLY_CONFIG:
                self._require_owner(f"change {parts[1].replace('-', ' ')} settings")
            self._json(config_update(root, parts[1], self._body()))
            self._audit("settings_changed", part=parts[1])  # the section only, never what it was set to
        elif method == "GET" and parts == ["git"]:
            self._json(git_state(root))
        elif method == "GET" and parts == ["tests"]:
            self._json(self._tests(root, refresh=bool(query.get("refresh"))))
        elif method == "GET" and parts == ["devlogs"]:
            self._json({"pulls": device_log_pulls(root), "sessions": self._device_sessions(root)})
        elif method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "export-zip":
            job_id = parts[1]
            try:
                job_path = resolve_job_path(root, job_id)
            except UIError:
                self._error(HTTPStatus.NOT_FOUND, "Job not found")
                return
            if not job_path.is_file():
                self._error(HTTPStatus.NOT_FOUND, "Job not found")
                return
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(job_path, arcname=f"{job_id}/job.json")
                for p, arc in [
                    (runtime_dir(root) / "jobs" / f"{job_id}-brief.md", f"{job_id}/brief.md"),
                    (runtime_dir(root) / "jobs" / f"{job_id}-plan.md", f"{job_id}/plan.md"),
                    (runtime_dir(root) / "jobs" / f"{job_id}-diff.patch", f"{job_id}/diff.patch"),
                    (runtime_dir(root) / "logs" / f"{job_id}.log", f"{job_id}/execution.log"),
                    (runtime_dir(root) / "output" / f"{job_id}-verification.md", f"{job_id}/verification.md"),
                ]:
                    if p.is_file():
                        zf.write(p, arcname=arc)
            data = zip_buffer.getvalue()
            self._send(HTTPStatus.OK, data, "application/zip", {
                "Content-Disposition": f'attachment; filename="{job_id}-export.zip"'
            })
        elif method == "GET" and parts == ["visual-checks"]:
            self._json({"checks": visual_checks_inventory(root)})
        elif method == "GET" and len(parts) >= 4 and parts[0] == "visual-checks" and parts[2] == "screenshots":
            target = visual_check_image(root, parts[1], parts[3])
            if not target:
                self._error(HTTPStatus.NOT_FOUND, "Screenshot not found")
                return
            self._send(HTTPStatus.OK, target.read_bytes(), "image/png", {})
        elif method == "POST" and parts == ["project"]:
            self._switch_project(self._body())
        elif method == "GET" and parts == ["runs"]:
            self._json({"runs": self._sessions_view()})
        elif method == "POST" and parts == ["runs"]:
            self._start_run(self._body(), root)
        elif len(parts) == 3 and parts[0] == "runs":
            self._run_op(method, parts[1], parts[2], query)
        else:
            self._error(HTTPStatus.NOT_FOUND, "Not found")

    def _tests(self, root: Path, refresh: bool = False) -> dict[str, Any]:
        """Suites/plans/coverage via the console's own discovery, cached briefly
        (it walks the repo)."""
        cache = getattr(self.server, "tests_cache", None)
        if cache and cache[0] == root and not refresh and time.time() - cache[1] < 120:
            return cache[2]
        try:
            result = subprocess.run(orchestrator_argv("script", "job_actions.py", "tests", "--json"),
                                    cwd=root, env=self.server.child_env(), capture_output=True, text=True, timeout=90)
            data = json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
            data = None
        if data is None:
            return {"suites": [], "plans": [], "coverage": None, "error": "Couldn't list tests; try the console's Test menu."}
        self.server.tests_cache = (root, time.time(), data)
        return data

    def _device_sessions(self, root: Path) -> dict[str, Any]:
        if not read_json_file(runtime_dir(root) / "project.json").get("remote_logs"):
            return {"configured": False, "items": [], "error": None}
        try:
            result = subprocess.run(orchestrator_argv("logs", "sessions", "--json", "--limit", "15"),
                                    cwd=root, env=self.server.child_env(), capture_output=True, text=True, timeout=45)
        except subprocess.TimeoutExpired:
            return {"configured": True, "items": [], "error": "Timed out talking to the log store."}
        if result.returncode != 0:
            message = re.sub(r"\x1b\[[0-9;]*m", "", (result.stderr or result.stdout).strip())
            return {"configured": True, "items": [], "error": message or "orchestrator logs sessions failed"}
        try:
            return {"configured": True, "items": json.loads(result.stdout or "[]"), "error": None}
        except json.JSONDecodeError:
            return {"configured": True, "items": [], "error": result.stdout.strip()[:500]}

    def _switch_project(self, body: dict[str, Any]) -> None:
        wanted = str(body.get("root") or "").strip()
        if not wanted:
            raise UIError("Project root path is required")
        candidate = safe_resolve(Path(wanted).expanduser())
        if not candidate.is_dir():
            raise UIError("Directory not found")
        known = {str(safe_resolve(Path(p["root"]).expanduser()))
                 for p in load_recent_projects().get("projects", []) if p.get("root")}
        if str(candidate) not in known and not (runtime_dir(candidate) / "project.json").is_file():
            raise UIError("Not a known orchestrator project")
        self.server.set_root(candidate)
        remember_project(candidate, active=True)
        self._json({"project": project_state(candidate)})

    def _start_run(self, body: dict[str, Any], root: Path) -> None:
        key = str(body.get("action") or "")
        action = ACTIONS.get(key)
        if not action:
            raise UIError(f"Unknown action '{key}'")
        if key in OWNER_ONLY_ACTIONS:
            self._require_owner(f"use {action.title.lower()}")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise UIError("params must be an object")
        argv = action.build(params, root)
        title = action.title
        job_id = str(params.get("job") or "") or None
        if job_id and job_id in self.server.sessions.running_job_ids():
            raise UIError("This job already has a run in progress. Open it from Activity instead.", HTTPStatus.CONFLICT)
        if job_id:
            job_title = job_summary(resolve_job_path(root, job_id), read_json_file(resolve_job_path(root, job_id)))["title"]
            title = f"{action.title} · {job_title}"
        try:
            cols, rows = int(body.get("cols") or 110), int(body.get("rows") or 32)
        except (TypeError, ValueError):
            raise UIError("cols/rows must be numbers")
        session = self.server.sessions.start(key, title, argv, root, self.server.child_env(),
                                             runtime_dir(root) / "logs" / "ui", cols=cols, rows=rows)
        session.job_id = job_id
        self._audit("run_started", action=key, job=job_id)
        if key in ("new_job", "fix"):
            self._watch_for_created_job(session, root, params.get("_linked") if key == "new_job" else None,
                                        params.get("_attachments") if key == "new_job" else None)
        self._json({"run": session.summary()}, HTTPStatus.CREATED)

    def _watch_for_created_job(self, session: PtySession, root: Path, linked: list[dict[str, str]] | None = None,
                               attachments: dict[str, Any] | None = None) -> None:
        """Links the job a new-job/fix run creates, so its run can offer "Open job". (Its feature is recorded by new_job.py.)"""
        before = {p.name for p in jobs_dir(root).glob("*.json")} if jobs_dir(root).exists() else set()

        def watch() -> None:
            while True:
                if jobs_dir(root).exists():
                    created = [p for p in jobs_dir(root).glob("*.json") if p.name not in before]
                    if created:
                        newest = max(created, key=lambda p: p.stat().st_mtime)
                        session.result_job = newest.stem
                        if linked:
                            self._record_links(newest, linked)
                        if attachments and (attachments.get("logs") or attachments.get("refs")):
                            self._record_attachments(newest, attachments)
                        return
                if not session.running:
                    return
                time.sleep(1)

        threading.Thread(target=watch, daemon=True).start()

    @staticmethod
    def _record_attachments(job_path: Path, attachments: dict[str, Any]) -> None:
        """Attach the chosen logs and reference files to a just-created job, the way Link logs / Attach mockup do later."""
        for _ in range(10):  # the job file may still be mid-write
            job = read_json_file(job_path)
            if job:
                if attachments.get("logs"):
                    job["last_manual_log_paths"] = list(dict.fromkeys(list(job.get("last_manual_log_paths") or []) + attachments["logs"]))
                if attachments.get("refs"):
                    job["reference_artifacts"] = list(job.get("reference_artifacts") or []) + [
                        {"type": r["type"], "note": r["name"], **{k: r[k] for k in ("path", "url") if r.get(k)}} for r in attachments["refs"]]
                write_json_file(job_path, job)
                return
            time.sleep(0.5)

    @staticmethod
    def _record_links(job_path: Path, linked: list[dict[str, str]]) -> None:
        """Note which tickets/issues/designs a new job was created from (shown on its page)."""
        for _ in range(10):  # the job file may still be mid-write
            job = read_json_file(job_path)
            if job:
                job["external_links"] = linked
                tmp = job_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
                os.replace(tmp, job_path)
                return
            time.sleep(0.5)

    def _run_op(self, method: str, sid: str, op: str, query: dict[str, list[str]]) -> None:
        session = self.server.sessions.get(sid)
        if session.action in OWNER_ONLY_ACTIONS:
            self._require_owner(f"access {ACTIONS[session.action].title.lower()} output")
        if method == "GET" and op == "stream":
            self._stream(session, int((query.get("offset") or ["0"])[0] or 0))
        elif method == "GET" and op == "output":
            # The same output as the stream, as one ordinary request: it answers when there is something new (or the wait
            # is up), so it works through proxies and tunnels that hold back an open event stream.
            try:
                offset = max(0, int((query.get("offset") or ["0"])[0] or 0))
                wait = min(max(float((query.get("wait") or ["0"])[0] or 0), 0.0), OUTPUT_MAX_WAIT_SECONDS)
            except ValueError:
                raise UIError("offset and wait must be numbers")
            end, data, finished = session.read(offset, timeout=wait)
            self._json({"offset": end, "data": base64.b64encode(data).decode("ascii"), "finished": finished,
                        "exit_code": session.exit_code if finished else None})
        elif method == "POST" and op == "input":
            data = str(self._body().get("data") or "")
            session.write(data.encode("utf-8"))
            self._json({"ok": True})
        elif method == "POST" and op == "resize":
            body = self._body()
            session.resize(int(body.get("cols") or 100), int(body.get("rows") or 30))
            self._json({"ok": True})
        elif method == "POST" and op == "stop":
            self._body()
            session.stop()
            self._json({"ok": True})
        else:
            self._error(HTTPStatus.NOT_FOUND, "Not found")

    def _sessions_view(self, job_id: str | None = None) -> list[dict[str, Any]]:
        sessions = self.server.sessions.list(job_id=job_id)
        if self._is_owner():
            return sessions
        return [session for session in sessions if session.get("action") not in OWNER_ONLY_ACTIONS]

    def _stream(self, session: PtySession, offset: int) -> None:
        """Server-sent events: `data` carries base64 terminal bytes, `end` the exit code."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self._cors_headers()
        self.end_headers()
        self.close_connection = True
        while True:
            if not self.server.accepting_requests:
                return
            offset, data, finished = session.read(offset, timeout=15)
            if data:
                payload = json.dumps({"offset": offset, "data": base64.b64encode(data).decode("ascii")})
                self.wfile.write(f"event: data\ndata: {payload}\n\n".encode())
            if finished:
                self.wfile.write(f"event: end\ndata: {json.dumps({'exit_code': session.exit_code})}\n\n".encode())
                self.wfile.flush()
                return
            if not data:
                self.wfile.write(b": keepalive\n\n")
            self.wfile.flush()


# --------------------------------------------------------------------------- entry point


def open_browser(url: str) -> None:
    opener = shutil.which("open") or shutil.which("xdg-open")
    if opener:
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def get_tailscale_info() -> tuple[str | None, str | None]:
    """Return (ip, dns_name) for local machine if Tailscale is running."""
    tailscale = shutil.which("tailscale")
    if not tailscale:
        return None, None
    try:
        raw = subprocess.check_output([tailscale, "status", "--json"], timeout=3, stderr=subprocess.DEVNULL).decode("utf-8")
        data = json.loads(raw)
        self_info = data.get("Self", {})
        dns_name = self_info.get("DNSName", "").rstrip(".")
        tailscale_ips = self_info.get("TailscaleIPs", [])
        ip = tailscale_ips[0] if tailscale_ips else None
        return ip, dns_name
    except Exception:
        return None, None


def _drain(proc: subprocess.Popen) -> None:
    """cloudflared logs to the pipe we gave it. Once nobody reads it the pipe fills (about 64 KB) and cloudflared blocks,
    so a tunnel left running for hours would stop carrying traffic. Read and discard it."""
    def pump() -> None:
        try:
            for _ in proc.stdout or ():
                pass
        except (OSError, ValueError):
            pass
    threading.Thread(target=pump, daemon=True, name="cloudflared-output").start()


def start_tunnel(port: int, token: str | None = None, discovery_timeout: float = 15.0) -> tuple[subprocess.Popen | None, str | None]:
    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        return None, None
    try:
        if token:
            proc = subprocess.Popen(
                [cloudflared, "tunnel", "run", "--token", token],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            _drain(proc)
            return proc, None
        proc = subprocess.Popen(
            [cloudflared, "tunnel", "--url", f"http://127.0.0.1:{port}", "--metrics", "127.0.0.1:0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception:
        return None, None
    found: queue.Queue[str] = queue.Queue(maxsize=1)
    def discover_and_drain() -> None:
        try:
            while proc.stdout:
                line = proc.stdout.readline()
                if not line:
                    break
                match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if match:
                    try:
                        found.put_nowait(match.group(0))
                    except queue.Full:
                        pass
        except (OSError, ValueError, StopIteration):
            pass
    threading.Thread(target=discover_and_drain, daemon=True, name="cloudflared-output").start()
    deadline = time.monotonic() + discovery_timeout
    while time.monotonic() < deadline:
        try:
            return proc, found.get(timeout=min(.1, max(0, deadline - time.monotonic())))
        except queue.Empty:
            pass
        if proc.poll() is not None:
            break
    return proc, None


class TunnelKeeper:
    """Keeps a tunnel up for as long as the server runs: if cloudflared exits (a dropped connection, a laptop that slept)
    it is started again, and the address it comes back with is reported. A quick tunnel gets a new address each time."""

    def __init__(self, port: int, token: str | None = None, fixed_url: str = "",
                 on_url: Callable[[str], None] = lambda url: None,
                 starter: Callable[..., tuple[Any, str | None]] = start_tunnel, interval: float = 10.0, max_wait: float = 300.0):
        self.port, self.token, self.fixed_url = port, token, fixed_url.rstrip("/")
        self.on_url, self.starter, self.interval, self.max_wait = on_url, starter, interval, max_wait
        self.proc: Any = None
        self.url = ""
        self._stop = threading.Event()

    def _launch(self) -> bool:
        self._end_process()
        self.proc, found = self.starter(self.port, token=self.token) if self.token else self.starter(self.port)
        self.url = self.fixed_url or (found or "")
        return self._healthy()

    def _healthy(self) -> bool:
        # A quick tunnel that never printed an address isn't usable, even though cloudflared is running.
        return self.proc is not None and self.proc.poll() is None and bool(self.url or self.token)

    def start(self) -> str:
        self._launch()
        threading.Thread(target=self._watch, daemon=True, name="tunnel-keeper").start()
        return self.url

    def _watch(self) -> None:
        failures = 0
        while not self._stop.wait(min(self.interval * (2 ** failures), self.max_wait)):
            if self._healthy():
                failures = 0
                continue
            before = self.url
            print("  The tunnel stopped. Starting it again…", flush=True)
            if self._launch():
                failures = 0
                if self.url != before:
                    self.on_url(self.url)
            else:
                failures = min(failures + 1, 6)  # back off: cloudflared missing, or no network

    def _end_process(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def stop(self) -> None:
        self._stop.set()
        self._end_process()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator ui", description="Local web interface for the orchestrator.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to bind (default 127.0.0.1). Use a Tailscale IP to reach it from your phone.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="Don't open a browser")
    parser.add_argument("--token", default=None, help="Fixed access token for this session")
    parser.add_argument("--tunnel", action="store_true", help="Start a Cloudflare tunnel for remote access from phone")
    parser.add_argument("--tunnel-token", default=None, help="Cloudflare Named Tunnel token for a permanent stable domain")
    parser.add_argument("--public-url", default=None, help="Stable public or tunnel URL for remote access")
    parser.add_argument("--tailscale", action="store_true", help="Bind to Tailscale interface with stable MagicDNS URL")
    args = parser.parse_args(argv)

    root = find_project_root()
    if not (runtime_dir(root) / "project.json").is_file():
        print(f"No orchestrator project at {root}. Run 'orchestrator wizard' there first, "
              "or pass --project to 'orchestrator ui'.")
        return 1
    # One Orchestrator per Mac user: the desktop app and this command share state and the account's address for it.
    try:
        lease = InstanceLease.acquire(user_state_dir() / "ui-instance.lock", kind="cli")
    except BusyError:
        print("Orchestrator is already running on this computer (the Orchestrator app, the background service, "
              "or another `orchestrator ui`). Open that one, or stop it first.")
        return 1
    try:
        return _serve(args, root)
    finally:
        lease.close()


def _serve(args: argparse.Namespace, root: Path) -> int:
    remember_project(root)

    settings_file = runtime_dir(root) / "config" / "settings.json"
    settings = {}
    if settings_file.is_file():
        try:
            settings = read_json_file(settings_file)
        except Exception:
            pass

    tunnel_token = args.tunnel_token or os.environ.get("CLOUDFLARE_TUNNEL_TOKEN") or settings.get("tunnel_token")
    public_url = args.public_url or os.environ.get("ORCHESTRATOR_PUBLIC_URL") or settings.get("public_url")

    if getattr(args, "tailscale", False):
        ts_ip, ts_dns = get_tailscale_info()
        if ts_ip:
            if args.host == "127.0.0.1":
                args.host = "0.0.0.0"
            if not public_url:
                public_url = f"http://{ts_dns or ts_ip}:{args.port}"
        else:
            print("  \033[93mWarning: Tailscale not active or not installed.\033[0m")

    try:
        server = UIServer((args.host, args.port), root, token=args.token)
    except OSError as exc:
        print(f"Could not listen on {args.host}:{args.port}: {exc.strerror}. Try --port.")
        return 1

    if public_url:
        from urllib.parse import urlparse
        parsed = urlparse(public_url)
        if parsed.netloc and server.allowed_hosts is not None:
            server.allowed_hosts.add(parsed.netloc)
            if parsed.hostname:
                server.allowed_hosts.add(parsed.hostname)

    url = f"{server.base_url}?token={server.token}"
    print(f"Orchestrator UI for {project_display_name(root)} ({root})")
    print(f"  Open: {url}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"\033[93m  Listening on {args.host}: anyone who can reach it still needs the token above. "
              "Only bind to a private network (e.g. Tailscale).\033[0m")
    keeper: TunnelKeeper | None = None
    reach = {"url": ""}  # the origin browsers reach this computer at, reported to the account; changes if a quick tunnel restarts

    def reachable_url() -> str:
        return reach["url"].rstrip("/") if reach["url"].startswith("https://") else ""

    def announce(label: str, address: str) -> None:
        reach["url"] = address
        hosted_url = f"{HOSTED_APP_URL}/?backend={address}&token={server.token}"
        PUBLIC_URL["url"] = hosted_url
        print(f"  \033[92m✓ {label}:\033[0m   {address}/?token={server.token}")
        print(f"  \033[92m✓ Phone Web UI:\033[0m {hosted_url}")

    if tunnel_token or getattr(args, "tunnel", False):
        print("  Starting Cloudflare Named Tunnel (stable persistent token)..." if tunnel_token
              else "  Starting Cloudflare tunnel for remote/phone access...")
        keeper = TunnelKeeper(args.port, token=tunnel_token, fixed_url=public_url or "",
                              on_url=lambda address: announce("Tunnel restarted at", address))
        address = keeper.start()
        if address:
            announce("Stable Tunnel URL" if tunnel_token else "Tunnel URL", address)
        elif tunnel_token:
            print(f"  \033[92m✓ Cloudflare Named Tunnel started.\033[0m Specify --public-url to display your phone link.")
        else:
            print("  \033[93mWarning: Could not establish Cloudflare tunnel (cloudflared missing or timed out). Trying again in the background.\033[0m")
    elif public_url:
        announce("Stable Remote URL", public_url)
    machine = account.load_machine()
    if machine:
        if reachable_url():
            print(f"  \033[92m✓ Account:\033[0m {machine.get('owner_email')} can sign in at {HOSTED_APP_URL}")
        else:
            print(f"  Account: connected to {machine.get('owner_email')}, but not reachable from {HOSTED_APP_URL} "
                  "until it has an https address (--tunnel, or --public-url https://…).")
        server.start_heartbeat(reachable_url)
    else:
        print("  Tip: run `orchestrator connect` to sign in to this computer from anywhere with Google.")
    print("  Ctrl-C to stop (running commands are stopped too).", flush=True)
    if not args.no_open:
        open_browser(url)

    # A service manager or `kill` stops with SIGTERM, a closed terminal with SIGHUP: clean up as Ctrl-C does, so the
    # tunnel and running commands don't outlive the server. Hangups stay ignored under nohup, which asks for that.
    def stop(signum, frame):
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) is not signal.SIG_IGN:
            signal.signal(sig, stop)
    try:
        server.start_notifier()
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        if keeper:
            keeper.stop()
        server.sessions.stop_all()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
