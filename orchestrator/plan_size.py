"""Is the plan small enough to be one job?

A job someone approves, builds and reviews in one sitting has to stay small: a few tasks that touch a handful of
files. A plan past that is split into jobs before anyone is asked to approve it, not trimmed after the work is done
(scope_check does that, from the diff). The numbers are a budget the planner is told about and a limit the page
enforces, so changing them here changes both. Pure over the plan; nothing is stored, so editing the plan re-measures it.
"""
from __future__ import annotations

from typing import Any

MAX_TASKS = 5
MAX_FILES = 12
SMALL_TASKS = 3
SMALL_FILES = 6


def _files(tasks: list[Any]) -> set[str]:
    found: set[str] = set()
    for task in tasks:
        if isinstance(task, dict) and isinstance(task.get("likely_files"), list):
            found.update(str(f).strip() for f in task["likely_files"] if str(f).strip())
    return found


def measure(plan: dict[str, Any] | None) -> dict[str, Any]:
    """{"label": Small | Medium | Too large, "tasks", "files", "too_large", "reasons", "deferred"} for a plan."""
    plan = plan if isinstance(plan, dict) else {}
    tasks = [t for t in (plan.get("tasks") or []) if t] if isinstance(plan.get("tasks"), list) else []
    files = len(_files(tasks))
    reasons = []
    if len(tasks) > MAX_TASKS:
        reasons.append(f"It has {len(tasks)} tasks; one job should have at most {MAX_TASKS}.")
    if files > MAX_FILES:
        reasons.append(f"It touches {files} files; one job should touch at most {MAX_FILES}.")
    small = len(tasks) <= SMALL_TASKS and files <= SMALL_FILES
    deferred = [str(d).strip() for d in (plan.get("deferred") or []) if str(d).strip()] if isinstance(plan.get("deferred"), list) else []
    return {"label": "Too large" if reasons else "Small" if small else "Medium", "tasks": len(tasks), "files": files,
            "too_large": bool(reasons), "reasons": reasons, "deferred": deferred}


def replan_request(reasons: list[str]) -> str:
    bullets = "\n".join(f"- {r}" for r in reasons)
    return ("### PLAN SIZE (re-plan requested) ###\n"
            f"This plan is too big for one job: at most {MAX_TASKS} tasks and about {MAX_FILES} files. Keep only what the request needs "
            "to work end to end, as working slices. Whatever you leave out goes in a top-level `deferred` array, one short line each, "
            "as follow-up jobs; do not plan them.\n" + bullets + "\n\nReturn the full corrected plan JSON.")
