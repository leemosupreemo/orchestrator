"""A conversation with an LLM that lives on the job.

Messages are stored in the job file under `conversation`, so the discussion
travels with the job (export, resume, handoff). The model gets the job's plan,
progress and a bounded diff stat, never write access: this is for questions and
course-correction advice. Changes still go through Revise plan or Run fix.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Callable

MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_MESSAGES = 20
MAX_DIFF_CHARS = 12000
ROLE = "reviewer"

LlmFn = Callable[[str, str], str]


class ChatError(ValueError):
    pass


def _tasks_text(job: dict[str, Any]) -> str:
    tasks = job.get("tasks") if isinstance(job.get("tasks"), list) else (job.get("plan") or {}).get("tasks") or []
    done = {str(i) for i in job.get("completed_tasks") or []}
    lines = []
    for i, t in enumerate(tasks):
        title = t if isinstance(t, str) else (t.get("title") or t.get("name") or t.get("description") or f"Task {i + 1}")
        lines.append(f"- [{'x' if str(i) in done else ' '}] {title}")
    return "\n".join(lines) or "(no tasks)"


def diff_stat(job: dict[str, Any], root: Path) -> str:
    branch, base = job.get("branch"), job.get("base_branch") or "main"
    if not branch:
        return ""
    try:
        res = subprocess.run(["git", "diff", "--stat", f"{base}...{branch}"], cwd=root, capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return res.stdout[:MAX_DIFF_CHARS] if res.returncode == 0 else ""


def build_prompt(job: dict[str, Any], history: list[dict[str, Any]], question: str, diff: str = "") -> str:
    plan = job.get("plan") or {}
    transcript = "\n".join(f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('text', '')}"
                           for m in history[-MAX_HISTORY_MESSAGES:])
    return "\n\n".join(part for part in [
        "You are helping a developer understand and steer one job in an AI coding orchestrator. "
        "Answer concisely and concretely. You cannot change code; if a change is needed, say whether to use "
        "'Revise plan' (scope or approach is wrong) or 'Run fix' (the implementation is wrong).",
        f"## Job\nTitle: {job.get('title', '')}\nType: {job.get('type', '')}\nStatus: {job.get('status', '')}\n"
        f"Branch: {job.get('branch') or '(none)'}",
        f"## Plan summary\n{plan.get('summary') or job.get('summary') or '(none)'}",
        f"## Tasks\n{_tasks_text(job)}",
        f"## Changes (diff stat)\n{diff}" if diff else "",
        f"## Conversation so far\n{transcript}" if transcript else "",
        f"## New question\n{question}",
    ] if part)


def messages(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [m for m in job.get("conversation") or [] if isinstance(m, dict)]


def ask(job_path: Path, question: str, llm: LlmFn, root: Path,
        read: Callable[[Path], dict[str, Any]], write: Callable[[Path, dict[str, Any]], None]) -> list[dict[str, Any]]:
    """Answer `question` and persist both messages. The job file is re-read after the
    (slow) model call so a worker that updated it meanwhile isn't overwritten."""
    question = (question or "").strip()
    if not question:
        raise ChatError("Type a question first")
    if len(question) > MAX_MESSAGE_CHARS:
        raise ChatError(f"Keep it under {MAX_MESSAGE_CHARS} characters")
    job = read(job_path)
    asked_at = time.time()
    reply = llm(build_prompt(job, messages(job), question, diff_stat(job, root)), job.get("reviewer") or job.get("planner") or "")
    reply = (reply or "").strip()
    if not reply:
        raise ChatError("The model returned an empty answer. Try again.")
    job = read(job_path)
    thread = messages(job) + [{"role": "user", "text": question, "t": asked_at},
                              {"role": "assistant", "text": reply, "t": time.time()}]
    job["conversation"] = thread
    write(job_path, job)
    return thread
