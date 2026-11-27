"""What is wrong with a job that needs a fix, in plain words, for the person who has to decide whether to run another attempt.

The job page and the "Run fix" dialog used to say only "Run a fix attempt" and leave out the problem. This reads what the job already
recorded (its last error, the test and build logs and their AI analysis, the builder's report, the last diagnosis) and returns the
one thing that is wrong, most important first, with a few lines of context:

    {"summary": "...", "lines": ["...", ...], "sign_in": {"provider": "claude", "name": "Claude"} | None}

Nothing here calls a model or changes anything.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator import builder_check, run_result

MAX_LINE = 1200
_AUTH = re.compile(r"oauth|\b401\b|authenticate|unauthori[sz]ed|sign[- ]?in|session expired|token (?:has )?expired|api key|credentials", re.I)
_TOOLS = (("claude", "Claude", re.compile(r"claude|anthropic", re.I)), ("codex", "Codex", re.compile(r"codex|openai", re.I)),
          ("agy", "Antigravity", re.compile(r"antigravity|\bagy\b", re.I)), ("pool", "Pool", re.compile(r"\bpool\b|poolside", re.I)))


def _signins_file() -> Path:
    from orchestrator.project_config import user_state_dir
    return user_state_dir() / "ai_signins.json"  # sign-ins belong to the computer, not to one project


def record_sign_in(provider: str, at: float | None = None) -> None:
    """Connect finished signing in to `provider`: a sign-in error from before now is no longer the problem."""
    path = _signins_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    data[provider] = time.time() if at is None else at
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def signed_in_at(provider: str) -> float:
    try:
        value = json.loads(_signins_file().read_text(encoding="utf-8")).get(provider)
    except (OSError, ValueError, AttributeError):
        return 0.0
    return float(value) if isinstance(value, (int, float)) else 0.0


def _job_time(job: dict[str, Any]) -> float:
    """When the job last changed, which is when its last error was recorded."""
    try:
        return datetime.fromisoformat(str(job.get("updated_at") or "")).timestamp()
    except ValueError:
        return 0.0


def _tool(text: str) -> tuple[str, str] | None:
    return next(((i, n) for i, n, pattern in _TOOLS if pattern.search(text)), None)


def _read(path: Path, limit: int = 120_000) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-limit:]


def _latest(folder: Path, pattern: str) -> Path | None:
    found = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime) if folder.is_dir() else []
    return found[-1] if found else None


def _clip(text: str, n: int = MAX_LINE) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= n:
        return text
    cut = text[: n - 1].rsplit(" ", 1)[0].rstrip(" ,;:-") or text[: n - 1].rstrip()
    return cut + "…"


def _top_blocker(analysis: str) -> str:
    """The first bullet under the analysis's "Top blocker" heading, without markdown."""
    match = re.search(r"##\s*Top blocker\s*\n+(?:[-*]\s*)?(.+)", analysis)
    if not match:
        return ""
    return _clip(re.sub(r"[*`_]", "", match.group(1)))


def _test_problem(job: dict[str, Any], out_dir: Path) -> str:
    build_failed = job.get("test_status") == "build-failed"
    if build_failed:
        top = _top_blocker(_read(_latest(out_dir, "build_analysis_*.md") or out_dir / "-"))
        return "The project doesn't build." + (f" {top}" if top else " The build log in the job's output has the details.")
    log = _read(out_dir / "test.log")
    if builder_check.no_tests_ran(log):
        return "No tests ran: the test command found nothing to run. That is a test setup problem, not a code failure."
    failed = run_result.failed_tests(run_result.clean_lines(log), 3)
    if failed:
        return "These tests fail: " + "; ".join(failed)
    top = _top_blocker(_read(_latest(out_dir, "test_analysis_*.md") or out_dir / "-"))
    return top or ("The tests didn't pass. The test log in the job's output has the details." if log or job.get("test_status") else "")


def last_debug_entry(job: dict[str, Any]) -> dict[str, Any]:
    history = job.get("debug_history") if isinstance(job.get("debug_history"), list) else []
    return history[-1] if history and isinstance(history[-1], dict) else {}


def last_fix_passed(job: dict[str, Any]) -> bool:
    """True when a debugging job's latest fix attempt passed build & tests and is ready for review."""
    if job.get("status") != "debugging" or job.get("debug_phase") == "paused":
        return False
    if job.get("test_status") in ("tests-failed", "build-failed", "failed") or str(job.get("last_error") or "").strip():
        return False
    res = last_debug_entry(job).get("result")
    return isinstance(res, dict) and res.get("build_ok") is True and res.get("tests_ok") is True


def describe(job: dict[str, Any], out_dir: Path) -> dict[str, Any] | None:
    """The problem behind a job in "debugging" or "failed", or None for a job in any other state."""
    if job.get("status") not in ("debugging", "failed") or last_fix_passed(job):
        return None
    out_dir = Path(out_dir)
    summary, lines, sign_in = "", [], None

    def add(text: str) -> None:
        nonlocal summary
        if not text:
            return
        if not summary:
            summary = text
        elif text not in lines:
            lines.append(text)

    error = str(job.get("last_error") or "").strip()
    if error and _AUTH.search(error) and (tool := _tool(error)) and 0 < _job_time(job) < signed_in_at(tool[0]):
        error = ""  # signed in again since: the sign-in is fixed; what's left is the rest of the problem
    if error and _AUTH.search(error):
        tool = _tool(error)
        name = f"{tool[1]} " if tool else "AI "
        sign_in = {"provider": tool[0], "name": tool[1]} if tool else None
        add(f"The last fix attempt couldn't run: your {name}sign-in expired. Sign in again, then run the fix.")
    if builder_check.writes_blocked(_read(out_dir / "builder_summary.md")):
        add("The AI's file edits were blocked (by its tool's permission mode), so the code never changed. Check that tool's permissions.")
    add(_test_problem(job, out_dir))
    if error and not (sign_in or _AUTH.search(error)):
        add(f"The last attempt stopped with an error: {_clip(error)}")
    if job.get("debug_phase") == "paused" and job.get("debug_pause_reason"):
        add(f"Automatic fixing paused: {_clip(str(job['debug_pause_reason']))}")
    last = last_debug_entry(job)
    last_result = last.get("result")
    if not summary:
        if last_result == "pending":
            add("The last fix attempt stopped before finishing.")
        elif isinstance(last_result, dict):
            if last_result.get("aborted"):
                notes = _clip(str(last_result.get("notes") or ""))
                add(f"The last fix attempt stopped early: {notes}" if notes else "The last fix attempt stopped before finishing.")
            elif last_result.get("build_ok") is False:
                add("The last fix attempt didn't build.")
            elif last_result.get("tests_ok") is False:
                add("The last fix attempt's tests didn't pass.")
    diagnosis = _clip(str(last.get("hypothesis") or ""))
    if diagnosis:
        if summary:
            add(f"Last attempt's diagnosis: {diagnosis}")
        else:
            add(f"The last fix attempt didn't finish. Its diagnosis: {diagnosis}")
    return {"summary": summary or "The build or tests didn't pass.", "lines": lines[:4], "sign_in": sign_in}
