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
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import zipfile
import signal
import ssl
import struct
import subprocess
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
from urllib.parse import parse_qs, urlparse
import urllib.error
import urllib.request

from orchestrator import integrations
from orchestrator import analytics
from orchestrator import delivery as delivery_view
from orchestrator import features as feature_store
from orchestrator import inbox as inbox_view
from orchestrator import new_job_form
from orchestrator import notifier
from orchestrator import plan_edit
from orchestrator import project_health
from orchestrator import scope_check
from orchestrator.stack_detection import detect_project_stack
from orchestrator.scripts import test_cases as test_case_lib
from orchestrator import job_chat
from orchestrator import new_project
from orchestrator.setup_checklist import setup_checklist
from orchestrator.project_config import (
    DEFAULT_RUNTIME_DIRNAME,
    find_project_root,
    forget_project,
    load_recent_projects,
    project_display_name,
    remember_project,
    safe_resolve,
)

PUBLIC_URL: dict[str, str] = {}  # {"url": ...} once the server knows where people reach it
STATIC_DIR = Path(__file__).resolve().parent / "static"
PACKAGE_PARENT = Path(__file__).resolve().parents[2]  # folder holding the `orchestrator` package serving this UI
COOKIE_NAME = "orchestrator_ui"
FIREBASE_API_KEY = "AIzaSyBg8h8yiC8OCoezFLEq6mQLhlc260b8CcI"
FIREBASE_PROJECT_ID = "swift-orch-web-20260923"
MAX_BUFFER_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SESSIONS_KEPT = 50
IDLE_PROMPT_SECONDS = 20
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


# --------------------------------------------------------------------------- terminal sessions


class PtySession:
    """A command running in a pseudo-terminal, with its output buffered so any
    number of browser tabs can attach, detach and catch up from an offset."""

    def __init__(self, sid: str, action: str, title: str, argv: list[str], cwd: Path,
                 env: dict[str, str], transcript: Path | None = None, cols: int = 110, rows: int = 32):
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
        with self._cond:
            self.exit_code = code
            self.ended = time.time()
            self._cond.notify_all()
        if self._transcript:
            self._transcript.close()

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
    def __init__(self) -> None:
        self._sessions: dict[str, PtySession] = {}
        self._lock = threading.Lock()

    def start(self, action: str, title: str, argv: list[str], cwd: Path, env: dict[str, str],
              transcript_dir: Path | None, cols: int = 110, rows: int = 32) -> PtySession:
        sid = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        transcript = None
        if transcript_dir:
            transcript_dir.mkdir(parents=True, exist_ok=True)
            transcript = transcript_dir / f"{sid}-{action}.log"
        session = PtySession(sid, action, title, argv, cwd, env, transcript, cols=cols, rows=rows)
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


def inbox_overview(root: Path, sessions: Any, with_others: bool = True) -> dict[str, Any]:
    running = sessions.running_job_ids()
    jobs = list_jobs(root)
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
    return inbox_view.build(active, jobs, sessions.list(), others)


def inbox_state(root: Path, sessions: Any) -> dict[str, Any]:
    """What the browser polls: the count for the badge and the items it can notify about."""
    here = inbox_overview(root, sessions, with_others=False)["here"]
    return {"inbox_count": len(here),
            "inbox": [{"id": i["id"], "title": i["title"], "label": i["label"], "reason": i["reason"],
                       "hash": f"#/runs/{i['run_id']}" if i["kind"] == "run" else f"#/jobs/{i['job_id']}"} for i in here]}


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
    brief_file = root / "docs" / "product-brief.md"
    brief_text = brief_file.read_text(encoding="utf-8", errors="ignore") if brief_file.is_file() else ""
    platforms, recommend_pending = project_health.brief_platforms(brief_text)
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
        "brief": bool(brief_text.strip()), "agents": any((root / n).is_file() for n in ("AGENTS.md", "CLAUDE.md", "GEMINI.md")),
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
    return {"features": feature_store.rollup(features, jobs + archived), "archived_jobs": archived, "overlaps": feature_store.overlaps(features, jobs, files),
            "unassigned": [j["id"] for j in jobs if not j.get("feature") and j["state"]["group"] != "done"]}


def list_jobs(root: Path) -> list[dict[str, Any]]:
    directory = jobs_dir(root)
    if not directory.exists():
        return []
    jobs = []
    for path in directory.glob("*.json"):
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
        "docs": job_documents(out_dir),
        "changes": job_changes(root, job),
        "links": github_links(root, job),
        "test_summary": test_summary,
        "scope": job_scope(root, job),
        "test_cases": test_case_view(root, test_case_lib.job_cases(job), {c["id"] for c in test_case_lib.due_cases(job)}),
        "pipeline": pipeline,
        "tasks": tasks,
        "completed_tasks": completed,
        "next_task": next_task,
        "approach": job.get("approach") or job.get("execution_strategy") or "Standard workflow",
    }


def read_limited(path: Path, limit: int = 200_000) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= limit else text[:limit] + "\n…[truncated]"


def job_documents(out_dir: Path) -> list[dict[str, str]]:
    """The console's "View Brief / Summary": brief, builder summary, investigations."""
    docs = []
    for name, title in (("brief.md", "Brief"), ("builder_summary.md", "Builder summary"), ("investigations.md", "Investigations")):
        text = read_limited(out_dir / name)
        if text and text.strip():
            docs.append({"title": title, "text": text})
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
    return links


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
        "dirty_files": len([l for l in status.splitlines() if l.strip()]),
        "scheme": config.get("scheme"),
        "firebase_distribution": bool(config.get("firebase_distribution")),
        "remote_logs": bool(config.get("remote_logs")),
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


def save_upload(root: Path, name: str, data: bytes) -> dict[str, Any]:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", PurePosixPath(name or "").name).strip(".-")[:80]
    if not clean:
        raise UIError("Give the file a name")
    try:
        kind = new_job_form.upload_kind(clean)
    except new_job_form.FormError as exc:
        raise UIError(str(exc))
    if not data:
        raise UIError("That file is empty")
    folder = runtime_dir(root) / "ui" / "uploads"
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


def build_git_checkout(params: dict[str, Any], root: Path) -> list[str]:
    branch = _text(params, "branch", required=True, limit=250)
    if branch not in git(root, "branch", "--format=%(refname:short)").splitlines():
        raise UIError("Not a local branch")
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

API_KEYS = [
    ("gemini_api_key", "Antigravity / Gemini", "GEMINI_API_KEY"),
    ("anthropic_api_key", "Claude", "ANTHROPIC_API_KEY"),
    ("openai_api_key", "Codex / OpenAI", "OPENAI_API_KEY"),
    ("ollama_api_key", "Ollama Cloud", "OLLAMA_API_KEY"),
]
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
            "github": github_cli_state(fresh=True), "default_parent": new_project.default_parent()}


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
    "new_job": Action("New job", build_new_job, fields=["type", "summary", "spec", "repro", "expected", "vibe", "subsystems", "branch_mode", "no_dispatch", "yolo", "free", "links", "feature", "logs", "files"]),
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
    "sync_fleet": Action("Sync fleet code", lambda p, r: orchestrator_argv("script", "sync_fleet.py")),
    "fleet_llm_check": Action("Fleet LLM latency & quota check", lambda p, r: orchestrator_argv("script", "fleet_llm_check.py")),
    "update_local": Action("Update Orchestrator (local)", lambda p, r: orchestrator_argv("update")),
    "update_fleet": Action("Update Orchestrator (fleet-wide)", lambda p, r: orchestrator_argv("update", "--fleet")),
    "scaffold_canary": Action("Scaffold canary test suite", lambda p, r: orchestrator_argv("script", "job_actions.py", "scaffold-canary")),
}


# --------------------------------------------------------------------------- HTTP


def get_or_create_ui_token(supplied: str | None = None) -> str:
    """Return the supplied token or read/persist a stable token in ~/.orchestrator/ui_token."""
    if supplied and supplied.strip():
        return supplied.strip()
    token_file = Path.home() / ".orchestrator" / "ui_token"
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
        token_file.write_text(token)
        token_file.chmod(0o600)
    except Exception:
        pass
    return token


class UIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], root: Path, token: str | None = None):
        super().__init__(address, UIHandler)
        self.root = root
        self.token = get_or_create_ui_token(token)
        self.sessions = SessionManager()
        self.allowed_hosts = self._allowed_hosts()
        self._stopping = threading.Event()

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

    def start_notifier(self, interval: float = 15.0) -> threading.Thread:
        """Push new inbox items and finished runs to the configured webhook, tab open or not."""
        tracker = notifier.Tracker()

        def loop() -> None:
            while not self._stopping.wait(interval):
                try:
                    self.notify_once(tracker)
                except Exception as exc:  # never let a bad webhook or odd job file kill the watcher
                    print(f"  Notifications: {exc}", flush=True)

        thread = threading.Thread(target=loop, daemon=True, name="notifier")
        self._tracker = tracker
        self.notify_once(tracker)  # seed so existing items aren't announced
        thread.start()
        return thread

    def notify_once(self, tracker: "notifier.Tracker") -> list[dict[str, Any]]:
        root = self.root
        events = tracker.update(inbox_overview(root, self.sessions, with_others=False)["here"], self.sessions.list())
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
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
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

    def _authed(self) -> bool:
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
        return bool(supplied) and hmac.compare_digest(supplied, self.server.token)

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
        origin = self.headers.get("Origin", "*")
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD, DELETE")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Orchestrator-UI")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Max-Age", "86400")
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

    def _dispatch_inner(self, method: str) -> None:
        if not self._host_ok():
            self._error(HTTPStatus.FORBIDDEN, "Unexpected Host header")
            return
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if method == "GET" and not url.path.startswith("/api/"):
                self._static(url.path, query)
                return
            if method == "POST" and url.path == "/api/auth":
                body = self._body()
                token = str(body.get("token") or "").strip()
                id_token = str(body.get("id_token") or body.get("idToken") or "").strip()

                authed_email = None
                if id_token:
                    user_info = verify_firebase_id_token(id_token)
                    user_email = (user_info.get("email") or "").strip().lower()
                    if not user_email:
                        raise UIError("This account didn't share an email address. With GitHub, add a verified "
                                      "primary email to your profile (it can stay private).", HTTPStatus.FORBIDDEN)
                    if user_info.get("emailVerified") is False:
                        raise UIError(f"The email {user_email} isn't verified with that provider.", HTTPStatus.FORBIDDEN)
                    allowed = allowed_auth_emails(self.server.root)
                    if not allowed:
                        raise UIError(
                            "No sign-in emails are allowed yet, so sign-in is closed. On the computer running Orchestrator, "
                            "set ORCHESTRATOR_ALLOWED_EMAILS, set git config user.email, or add your email under "
                            "Configuration (the access-token sign-in still works).",
                            HTTPStatus.FORBIDDEN)
                    if user_email not in allowed:
                        raise UIError(
                            f"Email {user_email} is not authorized for this computer. "
                            f"Authorized: {', '.join(sorted(allowed))}",
                            HTTPStatus.FORBIDDEN
                        )
                    authed_email = user_email
                elif token:
                    if not hmac.compare_digest(token, self.server.token):
                        raise UIError("Invalid access token", HTTPStatus.UNAUTHORIZED)
                else:
                    raise UIError("Access token or ID token required", HTTPStatus.BAD_REQUEST)

                self._json({"ok": True, "token": self.server.token, "email": authed_email}, extra={
                    "Set-Cookie": f"{COOKIE_NAME}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"
                })
                return
            if not self._authed():
                self._error(HTTPStatus.UNAUTHORIZED, "Open the URL printed by 'orchestrator ui' (it carries the access token).")
                return
            self._api(method, url.path, query)
        except UIError as exc:
            self._error(exc.status, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # surface, don't crash the handler thread
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

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
                "img-src 'self' data: https://*.googleusercontent.com https://lh3.googleusercontent.com; "
                "connect-src 'self' https://identitytoolkit.googleapis.com https://securetoken.googleapis.com https://*.googleapis.com https://*.firebaseapp.com; "
                "frame-src 'self' https://swift-orch-web-20260923.firebaseapp.com https://*.firebaseapp.com; "
                "frame-ancestors 'none'"
            ),
        })

    def _api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        root = self.server.root
        parts = [p for p in path.split("/") if p][1:]  # drop "api"

        if method == "GET" and parts == ["state"]:
            self._json({"project": project_state(root), "runs": self.server.sessions.list(),
                        **inbox_state(root, self.server.sessions),
                        "alerts": {"webhook": bool(read_settings(root).get("notification_webhook"))},
                        "actions": {k: {"title": a.title, "confirm": a.confirm, "fields": a.fields}
                                    for k, a in ACTIONS.items()},
                        "token": self.server.token})
        elif method == "POST" and parts == ["auth", "logout"]:
            self._json({"ok": True}, extra={
                "Set-Cookie": f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
            })
        elif method == "GET" and parts == ["projects"]:
            self._json({"projects": all_projects_info(root), "active": str(safe_resolve(root))})
        elif method == "POST" and parts == ["projects", "scan"]:
            body = self._body()
            paths_input = body.get("paths")
            custom_paths = [Path(p).expanduser() for p in paths_input] if isinstance(paths_input, list) else None
            self._json({"discovered": scan_for_projects(custom_paths)})
        elif method == "POST" and parts == ["projects", "add"]:
            body = self._body()
            target_str = str(body.get("root") or "").strip()
            if not target_str:
                raise UIError("Project root path is required")
            p = safe_resolve(Path(target_str).expanduser())
            if not p.is_dir():
                raise UIError(f"Directory not found: {p}")
            name = str(body.get("name") or "").strip() or project_display_name(p)
            remember_project(p, name, active=bool(body.get("active", False)))
            if body.get("active"):
                self.server.set_root(p)
            self._json({"ok": True, "project": {"name": name, "root": str(p), "active": bool(body.get("active", False))}})
        elif method == "DELETE" and parts == ["projects"]:
            body = self._body()
            target_str = str(body.get("root") or "").strip()
            if not target_str:
                raise UIError("Project root path is required")
            p = safe_resolve(Path(target_str).expanduser())
            if p == safe_resolve(root):
                raise UIError("Cannot remove the active project")
            forget_project(p)
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
            data = self.rfile.read(length) if length > 0 else b""
            self._json(save_upload(root, (query.get("name") or [""])[0], data))
        elif method == "GET" and parts == ["delivery"]:
            self._json(delivery_overview(root))
        elif method == "GET" and parts == ["test-cases"]:
            self._json(test_case_view(root, test_case_lib.load_library(root)))
        elif method == "GET" and parts == ["inbox"]:
            self._json(inbox_overview(root, self.server.sessions))
        elif method == "GET" and parts == ["features"]:
            self._json(features_overview(root))
        elif method == "POST" and parts == ["features"]:
            body = self._body()
            try:
                feature_store.create(runtime_dir(root), str(body.get("name") or ""), str(body.get("summary") or ""), body.get("paths"), body.get("depends_on"))
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
                fields = {k: body[k] for k in ("name", "summary", "paths", "depends_on") if k in body}
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
            op = _choice(body, "op", ["accept", "reset"])
            if op == "accept":
                raw = body.get("paths")
                if not isinstance(raw, list) or not raw or len(raw) > 200:
                    raise UIError("Choose the files to accept")
                job["scope_accepted"] = sorted(set(job.get("scope_accepted") or []) | {str(p)[:300] for p in raw})
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
            detail["runs"] = self.server.sessions.list(job_id=parts[1])
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
            env = self.server.child_env()

            def llm(prompt: str, model: str) -> str:
                argv = orchestrator_argv("script", "job_chat_run.py", *(["--model", model] if model else []))
                try:
                    res = subprocess.run(argv, input=prompt, cwd=root, env=env, capture_output=True, text=True, timeout=150)
                except subprocess.TimeoutExpired:
                    raise UIError("The model took too long. Try a shorter question.", HTTPStatus.GATEWAY_TIMEOUT)
                marker = "<<<ORCHESTRATOR-REPLY>>>"
                if res.returncode != 0 or marker not in res.stdout:
                    raise UIError((res.stderr.strip().splitlines() or ["The model couldn't answer."])[-1], HTTPStatus.BAD_GATEWAY)
                return res.stdout.split(marker, 1)[1]

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
        elif method == "GET" and parts == ["file"]:
            target = resolve_runtime_file(root, (query.get("path") or [""])[0])
            if target.stat().st_size > MAX_FILE_BYTES:
                with target.open("rb") as fh:
                    fh.seek(-MAX_FILE_BYTES, os.SEEK_END)
                    text = "[showing the last 2 MB]\n" + fh.read().decode("utf-8", "replace")
            else:
                text = target.read_text(encoding="utf-8", errors="replace")
            self._json({"path": str(target.relative_to(safe_resolve(runtime_dir(root)))), "text": text})
        elif method == "GET" and parts == ["new-project"]:
            self._json(new_project_state())
        elif method == "POST" and parts == ["new-project", "draft"]:
            self._json({"draft": new_project.save_draft(self._body())})
        elif method == "POST" and parts == ["new-project", "discard"]:
            self._body()
            new_project.clear_draft()
            self._json({"ok": True})
        elif method == "POST" and parts == ["new-project", "create"]:
            self._body()
            self._json(create_new_project(self.server))
        elif method == "POST" and parts == ["new-project", "publish"]:
            body = self._body()
            self._json({"step": publish_known_project(str(body.get("root") or ""), str(body.get("visibility") or "private"))})
        elif method == "GET" and parts == ["integrations"]:
            self._json({"integrations": integrations.public_catalog(saved_integrations(root), read_settings(root).get("integration_options"))})
        elif method == "POST" and len(parts) == 3 and parts[0] == "integrations" and parts[2] == "options":
            self._json({"options": save_integration_options(root, parts[1], self._body().get("options"))})
        elif method == "POST" and len(parts) == 3 and parts[0] == "integrations" and parts[2] == "connect":
            self._json(connect_integration(root, parts[1], self._body().get("values") or {}))
        elif method == "POST" and len(parts) == 3 and parts[0] == "integrations" and parts[2] == "disconnect":
            self._body()
            disconnect_integration(root, parts[1])
            self._json({"ok": True})
        elif method == "GET" and len(parts) == 3 and parts[0] == "integrations" and parts[2] == "search":
            self._json({"items": search_integration(root, parts[1], (query.get("q") or [""])[0])})
        elif method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "links":
            if parts[1] in self.server.sessions.running_job_ids():
                raise UIError("This job is running right now. Attach context once it stops.", HTTPStatus.CONFLICT)
            self._json({"links": attach_links_to_job(root, parts[1], _clean_links(self._body().get("links")))})
        elif method == "GET" and parts == ["setup"]:
            self._json(setup_checklist(root, runtime_dir(root)))
        elif method == "GET" and parts == ["config"]:
            self._json(config_state(root))
        elif method == "GET" and parts == ["config", "doc"]:
            wanted = (query.get("id") or [""])[0]
            entry = next((d for d in doc_entries(root) if d["id"] == wanted), None)
            if not entry:
                raise UIError("Document not found", HTTPStatus.NOT_FOUND)
            self._json({"name": entry["name"], "text": Path(entry["path"]).read_text(encoding="utf-8", errors="replace")[:MAX_FILE_BYTES]})
        elif method == "POST" and len(parts) == 2 and parts[0] == "config":
            self._json(config_update(root, parts[1], self._body()))
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
            self._json({"runs": self.server.sessions.list()})
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
        if key in ("new_job", "fix"):
            self._watch_for_created_job(session, root, params.get("_linked") if key == "new_job" else None,
                                        params.get("_feature") if key == "new_job" else None,
                                        params.get("_attachments") if key == "new_job" else None)
        self._json({"run": session.summary()}, HTTPStatus.CREATED)

    def _watch_for_created_job(self, session: PtySession, root: Path, linked: list[dict[str, str]] | None = None,
                               feature: str | None = None, attachments: dict[str, Any] | None = None) -> None:
        """Links the job a new-job/fix run creates, so its run can offer "Open job"."""
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
                        if feature:
                            self._record_feature(newest, root, feature)
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
                        {"type": r["type"], "path": r["path"], "note": r["name"]} for r in attachments["refs"]]
                write_json_file(job_path, job)
                return
            time.sleep(0.5)

    @staticmethod
    def _record_feature(job_path: Path, root: Path, feature: str) -> None:
        """Put a just-created job under the feature it was created for."""
        for _ in range(10):  # the job file may still be mid-write
            job = read_json_file(job_path)
            if job:
                job["feature"] = feature
                try:
                    feature_store.note_work_attached(runtime_dir(root), feature, "working")
                except feature_store.FeatureError:
                    return
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
        if method == "GET" and op == "stream":
            self._stream(session, int((query.get("offset") or ["0"])[0] or 0))
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

    def _stream(self, session: PtySession, offset: int) -> None:
        """Server-sent events: `data` carries base64 terminal bytes, `end` the exit code."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
        self.end_headers()
        self.close_connection = True
        while True:
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


def start_tunnel(port: int) -> tuple[subprocess.Popen | None, str | None]:
    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        return None, None
    try:
        proc = subprocess.Popen(
            [cloudflared, "tunnel", "--url", f"http://127.0.0.1:{port}", "--metrics", "127.0.0.1:0"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception:
        return None, None
    tunnel_url = None
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            time.sleep(0.1)
            continue
        m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
        if m:
            tunnel_url = m.group(0)
            break
    return proc, tunnel_url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator ui", description="Local web interface for the orchestrator.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to bind (default 127.0.0.1). Use a Tailscale IP to reach it from your phone.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="Don't open a browser")
    parser.add_argument("--token", default=None, help="Fixed access token for this session")
    parser.add_argument("--tunnel", action="store_true", help="Start a Cloudflare tunnel for remote access from phone")
    args = parser.parse_args(argv)

    root = find_project_root()
    if not (runtime_dir(root) / "project.json").is_file():
        print(f"No orchestrator project at {root}. Run 'orchestrator wizard' there first, "
              "or pass --project to 'orchestrator ui'.")
        return 1
    remember_project(root)

    try:
        server = UIServer((args.host, args.port), root, token=args.token)
    except OSError as exc:
        print(f"Could not listen on {args.host}:{args.port}: {exc.strerror}. Try --port.")
        return 1

    url = f"{server.base_url}?token={server.token}"
    print(f"Orchestrator UI for {project_display_name(root)} ({root})")
    print(f"  Open: {url}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"\033[93m  Listening on {args.host}: anyone who can reach it still needs the token above. "
              "Only bind to a private network (e.g. Tailscale).\033[0m")
    tunnel_proc = None
    if getattr(args, "tunnel", False):
        print("  Starting Cloudflare tunnel for remote/phone access...")
        tunnel_proc, tunnel_url = start_tunnel(args.port)
        if tunnel_url:
            hosted_url = f"https://swift-orch-web-20260923.web.app/?backend={tunnel_url}"
            PUBLIC_URL["url"] = hosted_url
            print(f"  \033[92m✓ Tunnel URL:\033[0m   {tunnel_url}/?token={server.token}")
            print(f"  \033[92m✓ Phone Web UI:\033[0m {hosted_url}")
        else:
            print("  \033[93mWarning: Could not establish Cloudflare tunnel (cloudflared missing or timed out).\033[0m")
    print("  Ctrl-C to stop (running commands are stopped too).", flush=True)
    if not args.no_open:
        open_browser(url)
    try:
        server.start_notifier()
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        if tunnel_proc:
            try:
                tunnel_proc.terminate()
                tunnel_proc.wait(timeout=2)
            except Exception:
                try:
                    tunnel_proc.kill()
                except Exception:
                    pass
        server.sessions.stop_all()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
