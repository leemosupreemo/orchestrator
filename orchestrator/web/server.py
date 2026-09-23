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
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
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
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from orchestrator.project_config import (
    DEFAULT_RUNTIME_DIRNAME,
    find_project_root,
    load_recent_projects,
    project_display_name,
    remember_project,
    safe_resolve,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "orchestrator_ui"
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
        }

    def _looks_like_prompt(self) -> bool:
        with self._cond:
            tail = bytes(self._buffer[-400:])
        text = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", tail).decode("utf-8", "replace").rstrip(" \t")
        last = text.splitlines()[-1].strip() if text.strip() else ""
        return bool(last) and (last.endswith((":", "?", ">", ")", "]")) or "Enter" in last or "(y/n" in last.lower())


def _display_argv(argv: list[str]) -> list[str]:
    """`python -m orchestrator ...` reads better as `orchestrator ...`."""
    if len(argv) >= 3 and argv[1:3] == ["-m", "orchestrator"]:
        return ["orchestrator", *argv[3:]]
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


KIND_LABELS = {
    "bug": "Bug fix", "bug-fix": "Bug fix", "bug-investigate": "Bug fix", "quick": "Quick change",
    "quick-fix": "Quick change", "feature": "Feature", "feature-plan": "Feature", "feature-task": "Feature task",
    "feature-design": "Design", "design": "Design", "coverage": "Tests", "test-audit": "Tests",
}
FAILING_TEST_STATUSES = {"tests-failed", "build-failed", "failed"}


def job_state(job: dict[str, Any]) -> dict[str, Any]:
    """What the job is waiting on, in plain words, and the one action that moves
    it forward. `group` is needs_you | working | done; `tone` drives colour:
    attention (amber) = your move, working (blue), done (green), failed (red)."""
    status = job.get("status") or "unknown"
    tests = job.get("test_status")
    pr = job.get("pr_number")
    tasks = job.get("tasks") if isinstance(job.get("tasks"), list) else []
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
        return state("needs_you", "attention", "Needs you", "Waiting on a decision in the console.", "console", "Open in console")
    if status == "designing":
        return state("needs_you", "attention", "Design ready", "Approve the design to plan its implementation, or ask for changes.", "approve", "Approve design")
    if status == "planned":
        if job.get("type") == "feature-plan" and not job.get("approved"):
            return state("needs_you", "attention", "Plan ready", "Approve the plan to start building its tasks.", "approve", "Approve plan")
        return state("needs_you", "attention", "Plan ready", "Start building when you're happy with the plan.", "schedule", "Start")
    if status == "review-needed":
        if pr:
            return state("needs_you", "attention", "Ready to merge", f"PR #{pr} is ready for your review.", "merge", "Merge & complete")
        return state("needs_you", "attention", "Ready to review", "Changes are ready; finish up in the console.", "console", "Open in console")
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
        return state("done", "done", "Done", "Merged and archived." if pr else "Finished.")
    if status == "failed":
        return state("needs_you", "failed", "Failed", "Last run failed; try a fix.", "debug", "Run fix")
    if remaining:
        return state("working", "working", status.replace("-", " ").capitalize(), f"{remaining} task(s) left.", "resume", "Resume")
    return state("working", "working", status.replace("-", " ").capitalize(), "")


def job_summary(path: Path, job: dict[str, Any]) -> dict[str, Any]:
    tasks = job.get("tasks") or []
    completed = job.get("completed_tasks") or []
    kind = job.get("type") or job.get("job_type")
    return {
        "id": path.stem,
        "title": job.get("title") or job.get("summary") or path.stem,
        "type": kind,
        "kind": KIND_LABELS.get(str(kind), str(kind or "Job").replace("-", " ").capitalize()),
        "status": job.get("status") or "unknown",
        "state": job_state(job),
        "branch": job.get("branch"),
        "issue_number": job.get("issue_number"),
        "pr_number": job.get("pr_number"),
        "question": job.get("human_clarification_question") if job.get("status") == "human-needed" else None,
        "tasks_total": len(tasks) if isinstance(tasks, list) else 0,
        "tasks_done": len(completed) if isinstance(completed, list) else 0,
        "updated": path.stat().st_mtime,
    }


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
    return {"summary": job_summary(path, job), "job": job, "outputs": outputs,
            "logs": [resolve_linked_log(root, ref) for ref in linked_logs(job)],
            "docs": job_documents(out_dir), "changes": job_changes(root, job),
            "links": github_links(root, job)}


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
    if branch and re.fullmatch(r"[A-Za-z0-9._/-]+", branch) and re.fullmatch(r"[A-Za-z0-9._/-]+", base):
        diffstat = git(root, "diff", "--stat", "--stat-width=100", f"{base}...{branch}", "--")
    return {"files": [str(f) for f in files][:200], "diffstat": diffstat[-20_000:], "base": base,
            "hypothesis": job.get("builder_hypothesis") or ""}


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


def project_state(root: Path) -> dict[str, Any]:
    config = read_json_file(runtime_dir(root) / "project.json")
    status = git(root, "status", "--porcelain")
    recent = load_recent_projects().get("projects", [])
    return {
        "name": project_display_name(root),
        "root": str(root),
        "branch": git(root, "branch", "--show-current"),
        "dirty_files": len([l for l in status.splitlines() if l.strip()]),
        "scheme": config.get("scheme"),
        "firebase_distribution": bool(config.get("firebase_distribution")),
        "remote_logs": bool(config.get("remote_logs")),
        "configured": bool(config),
        "recent": [{"name": p.get("name"), "root": p.get("root")} for p in recent],
    }


# --------------------------------------------------------------------------- actions


@dataclass
class Action:
    title: str
    build: Callable[[dict[str, Any], Path], list[str]]
    # Shown in a confirm dialog before running: set for anything outward-facing.
    confirm: str | None = None
    fields: list[str] = field(default_factory=list)


def orchestrator_argv(*args: str) -> list[str]:
    return [sys.executable, "-m", "orchestrator", *args]


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


def build_new_job(params: dict[str, Any], root: Path) -> list[str]:
    job_type = _choice(params, "type", JOB_TYPES, "bug")
    summary = _text(params, "summary", required=True, limit=500)
    argv = orchestrator_argv("script", "new_job.py", job_type, "--summary", summary)
    spec = _text(params, "spec", limit=200_000)
    if spec:
        spec_dir = runtime_dir(root) / "ui" / "specs"
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_file = spec_dir / f"{datetime.now():%Y%m%d-%H%M%S}-{job_type}.md"
        spec_file.write_text(spec + "\n", encoding="utf-8")
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


def build_manual(mode: str) -> Callable[[dict[str, Any], Path], list[str]]:
    return lambda params, root: orchestrator_argv("script", "manual_run.py", mode)


ACTIONS: dict[str, Action] = {
    "console": Action("Interactive console", lambda p, r: orchestrator_argv("console")),
    "check": Action("Setup check", lambda p, r: orchestrator_argv("check")),
    "check_config": Action("Config check", lambda p, r: orchestrator_argv("check-config")),
    "wizard": Action("Setup wizard", lambda p, r: orchestrator_argv("wizard")),
    "worker_check": Action("Worker check", lambda p, r: orchestrator_argv("worker-check")),
    "new_job": Action("New job", build_new_job, fields=["type", "summary", "spec", "branch_mode", "no_dispatch", "yolo", "free"]),
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
    "git_new_branch": Action("New branch", build_git_new_branch, fields=["name"]),
    "merge": Action("Merge & complete", lambda p, r: orchestrator_argv("script", "job_actions.py", "merge", _job_path(p, r)),
                    confirm="Merges the job's PR on GitHub, deletes its AI branch and archives the job.", fields=["job"]),
    "deliver": Action("Deliver to testers", lambda p, r: orchestrator_argv("script", "deliver_build.py", _job_path(p, r)),
                      confirm="Builds this job's branch and sends a real Firebase release to your testers.", fields=["job"]),
    "build": Action("Build", build_manual("build")),
    "test": Action("Run all tests", build_manual("test")),
    "distribute": Action("Distribute current branch", build_distribute,
                         confirm="Builds whatever branch is checked out now and sends a real Firebase release to your testers.",
                         fields=["notes"]),
    "logs_setup": Action("Device logs setup", lambda p, r: orchestrator_argv("logs", "setup")),
    "logs_pull": Action("Pull device logs", build_logs_pull, fields=["session", "level", "query"]),
    "logs_tail": Action("Follow device logs", build_logs_tail, fields=["session"]),
}


# --------------------------------------------------------------------------- HTTP


class UIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], root: Path, token: str | None = None):
        super().__init__(address, UIHandler)
        self.root = root
        self.token = token or secrets.token_urlsafe(24)
        self.sessions = SessionManager()
        self.allowed_hosts = self._allowed_hosts()

    def _allowed_hosts(self) -> set[str] | None:
        """Host headers accepted, or None when bound to every interface: the
        name a remote browser uses then can't be known, and the token remains
        the actual gate."""
        host, port = self.server_address[:2]
        if host in ("0.0.0.0", "::"):
            return None
        names = {host, "localhost", "127.0.0.1", "[::1]"}
        return {f"{n}:{port}" for n in names} | names

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        return f"http://{shown}:{port}/"

    def set_root(self, root: Path) -> None:
        self.root = root

    def child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["ORCHESTRATOR_PROJECT_ROOT"] = str(self.root)
        env.pop("ORCHESTRATOR_CONFIG", None)
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
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _host_ok(self) -> bool:
        allowed = self.server.allowed_hosts
        return allowed is None or (self.headers.get("Host") or "") in allowed

    def _authed(self) -> bool:
        supplied = ""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            supplied = auth[7:]
        else:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if COOKIE_NAME in cookie:
                supplied = cookie[COOKIE_NAME].value
        return bool(supplied) and hmac.compare_digest(supplied, self.server.token)

    def _body(self) -> dict[str, Any]:
        if self.headers.get("X-Orchestrator-UI") != "1" or "application/json" not in self.headers.get("Content-Type", ""):
            raise UIError("Missing UI headers", HTTPStatus.FORBIDDEN)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise UIError("Request too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise UIError("Invalid JSON")
        if not isinstance(data, dict):
            raise UIError("Invalid JSON")
        return data

    # -- routing

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        if not self._host_ok():
            self._error(HTTPStatus.FORBIDDEN, "Unexpected Host header")
            return
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if method == "GET" and not url.path.startswith("/api/"):
                self._static(url.path, query)
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
                "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'"
            ),
        })

    def _api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
        root = self.server.root
        parts = [p for p in path.split("/") if p][1:]  # drop "api"

        if method == "GET" and parts == ["state"]:
            self._json({"project": project_state(root), "runs": self.server.sessions.list(),
                        "actions": {k: {"title": a.title, "confirm": a.confirm, "fields": a.fields}
                                    for k, a in ACTIONS.items()}})
        elif method == "GET" and parts == ["jobs"]:
            running = self.server.sessions.running_job_ids()
            jobs = list_jobs(root)
            for job in jobs:
                job["active_run"] = job["id"] in running
            self._json({"jobs": jobs})
        elif method == "GET" and len(parts) == 2 and parts[0] == "jobs":
            detail = job_detail(root, parts[1])
            detail["runs"] = self.server.sessions.list(job_id=parts[1])
            self._json(detail)
        elif method == "GET" and parts == ["file"]:
            target = resolve_runtime_file(root, (query.get("path") or [""])[0])
            if target.stat().st_size > MAX_FILE_BYTES:
                with target.open("rb") as fh:
                    fh.seek(-MAX_FILE_BYTES, os.SEEK_END)
                    text = "[showing the last 2 MB]\n" + fh.read().decode("utf-8", "replace")
            else:
                text = target.read_text(encoding="utf-8", errors="replace")
            self._json({"path": str(target.relative_to(safe_resolve(runtime_dir(root)))), "text": text})
        elif method == "GET" and parts == ["git"]:
            self._json(git_state(root))
        elif method == "GET" and parts == ["tests"]:
            self._json(self._tests(root, refresh=bool(query.get("refresh"))))
        elif method == "GET" and parts == ["devlogs"]:
            self._json({"pulls": device_log_pulls(root), "sessions": self._device_sessions(root)})
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
        wanted = str(body.get("root") or "")
        known = {str(safe_resolve(Path(p["root"]).expanduser()))
                 for p in load_recent_projects().get("projects", []) if p.get("root")}
        candidate = safe_resolve(Path(wanted).expanduser()) if wanted else None
        if not candidate or (str(candidate) not in known
                             and not (runtime_dir(candidate) / "project.json").is_file()):
            raise UIError("Not a known orchestrator project")
        self.server.set_root(candidate)
        remember_project(candidate)
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
            self._watch_for_created_job(session, root)
        self._json({"run": session.summary()}, HTTPStatus.CREATED)

    def _watch_for_created_job(self, session: PtySession, root: Path) -> None:
        """Links the job a new-job/fix run creates, so its run can offer "Open job"."""
        before = {p.name for p in jobs_dir(root).glob("*.json")} if jobs_dir(root).exists() else set()

        def watch() -> None:
            while True:
                if jobs_dir(root).exists():
                    created = [p for p in jobs_dir(root).glob("*.json") if p.name not in before]
                    if created:
                        session.result_job = max(created, key=lambda p: p.stat().st_mtime).stem
                        return
                if not session.running:
                    return
                time.sleep(1)

        threading.Thread(target=watch, daemon=True).start()

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator ui", description="Local web interface for the orchestrator.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interface to bind (default 127.0.0.1). Use a Tailscale IP to reach it from your phone.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="Don't open a browser")
    args = parser.parse_args(argv)

    root = find_project_root()
    if not (runtime_dir(root) / "project.json").is_file():
        print(f"No orchestrator project at {root}. Run 'orchestrator wizard' there first, "
              "or pass --project to 'orchestrator ui'.")
        return 1
    remember_project(root)

    try:
        server = UIServer((args.host, args.port), root)
    except OSError as exc:
        print(f"Could not listen on {args.host}:{args.port}: {exc.strerror}. Try --port.")
        return 1

    url = f"{server.base_url}?token={server.token}"
    print(f"Orchestrator UI for {project_display_name(root)} ({root})")
    print(f"  Open: {url}")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"\033[93m  Listening on {args.host}: anyone who can reach it still needs the token above. "
              "Only bind to a private network (e.g. Tailscale).\033[0m")
    print("  Ctrl-C to stop (running commands are stopped too).", flush=True)
    if not args.no_open:
        open_browser(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.sessions.stop_all()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
