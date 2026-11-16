"""What to do about a paused job whose "question" is really a blocker: no answer can fix it, so the person is never asked one.

Each blocker is checked before anything is offered: when it is already fixed (the folder is writable, the tool is installed,
the person signed in since) or never needed fixing (a planner asking for write access: planning doesn't write files), the one
button retries with what's needed. Otherwise the steps to fix it are given, then the same button.

    {"kind": ..., "reason": "...", "steps": ["..."], "action": "revise" | "resume", "label": "...",
     "params": {...}, "sign_in": {"provider": "claude", "name": "Claude"} | None}

Nothing here calls a model or changes anything.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from orchestrator import job_problem

_WRITE = re.compile(r"write access|read-only|read only|sandbox|cannot write|can't write|permission denied", re.I)
_TOOL = re.compile(r"(?:^|[\s`'\"])([\w.+-]+)(?:`|'|\")?:? (?:command not found|is not installed|not installed)", re.I)
_TOOL_AFTER = re.compile(r"(?:command not found|not installed):?\s*`?([\w.+-]+)", re.I)

PLAN_ONLY = ("Plan only: you don't need write access and shouldn't ask for it. Write the plan (tasks, the failing tests to add, "
             "docs and screenshots to produce) as JSON; the builder creates those files when the plan runs.")


def _missing_tool(text: str) -> str:
    match = _TOOL.search(text) or _TOOL_AFTER.search(text)
    name = match.group(1) if match else ""
    return "" if name.lower() in ("is", "not", "the", "a", "it", "tool") else name


def diagnose(question: str, job: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    planned = bool((job.get("plan") or {}).get("tasks"))
    retry = ("resume", "Try again", {}) if planned else ("revise", "Plan again", {"change": "Plan again. The last attempt stopped before planning."})

    def result(kind, reason, steps=(), action=None, sign_in=None):
        verb, label, params = action or retry
        return {"kind": kind, "reason": reason, "steps": list(steps), "action": verb, "label": label, "params": params, "sign_in": sign_in}

    if _WRITE.search(question) and not planned:
        # Planning never writes files, so this was never a real limit: tell the planner so and plan again.
        return result("plan_only", "The planner asked for write access, but planning doesn't need it: it only writes the plan, "
                      "and the builder creates the files. Plan again tells it that.", action=("revise", "Plan again", {"change": PLAN_ONLY}))
    if _WRITE.search(question):
        writable = root is None or os.access(root, os.W_OK)
        if writable:
            return result("write", "The builder said it couldn't write files, but the project folder is writable now. Try again.")
        return result("write", f"The builder can't write to the project folder ({root}).",
                      [f"In Finder, select the folder {root.name}, choose File › Get Info, and under Sharing & Permissions give your "
                       "account Read & Write.", "Then press Try again."])
    if job_problem._AUTH.search(question):
        tool = job_problem._tool(question)
        name = tool[1] if tool else "the AI tool"
        if tool and 0 < job_problem._job_time(job) < job_problem.signed_in_at(tool[0]):
            return result("sign_in", f"You've signed in to {name} since this stopped. Try again.")
        return result("sign_in", f"Your {name} sign-in expired, so the AI couldn't run.", [f"Sign in to {name}.", "Then press Try again."],
                      sign_in={"provider": tool[0], "name": tool[1]} if tool else None)
    if re.search(r"command not found|not installed", question, re.I):
        tool = _missing_tool(question)
        if tool and shutil.which(tool):
            return result("tool", f"{tool} is installed now. Try again.")
        what = tool or "the tool it names"
        return result("tool", f"The AI needs {what}, which isn't installed on this Mac.",
                      [f"Install {what}.", "Then press Try again."])
    return result("other", f"The AI stopped: {question}", ["If it stops the same way again, use Run a fix… to give it more to go on."])
