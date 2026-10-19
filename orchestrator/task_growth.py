"""Did the task that just finished stay inside its plan?

The worker asks this after each task is committed, while there are still tasks to go. A task that added far more than
planned, touched files the plan never mentioned, or changed dependencies is where a small job starts to grow; it is
better to stop there and let a person decide than to build three more tasks on top. scope_check judges the whole job
from its diff at review; this judges one task, with a wider margin, because a task is allowed to run a little over.
"""
from __future__ import annotations

from typing import Any

from orchestrator import scope_check

GROWTH_FACTOR = 2   # lines added, as a multiple of what is typical for one task
MAX_UNPLANNED = 3   # files the task's plan never mentioned


def check(task: dict[str, Any], changed: list[dict[str, Any]]) -> dict[str, Any] | None:
    """None when the task stayed inside its plan, else {"reasons", "added", "files"} (the files that were not planned).

    `changed` is what the task committed, in scope_check.parse_numstat's shape."""
    plan_files = [str(f) for f in (task.get("likely_files") or []) if str(f).strip()] if isinstance(task, dict) else []
    result = scope_check.evaluate(plan_files, 1, changed)
    unplanned = sorted({p for f in result["findings"] if f["kind"] in ("manifest", "new_files", "outside_plan") for p in f["files"]})
    manifests = sorted({p for f in result["findings"] if f["kind"] == "manifest" for p in f["files"]})
    reasons = []
    if result["added"] > GROWTH_FACTOR * scope_check.LINES_PER_TASK:
        reasons.append(f"added {result['added']} lines (about {scope_check.LINES_PER_TASK} is typical)")
    if len(unplanned) > MAX_UNPLANNED:
        reasons.append(f"touched {len(unplanned)} files the plan didn't mention")
    if manifests:
        reasons.append(f"changed dependencies or build settings ({', '.join(manifests[:2])})")
    return {"reasons": reasons, "added": result["added"], "files": unplanned} if reasons else None


def sentence(index: int, reasons: list[str]) -> str:
    """'Task 2 added 950 lines (about 400 is typical) and touched 5 files the plan didn't mention.'"""
    joined = reasons[0] if len(reasons) == 1 else ", ".join(reasons[:-1]) + " and " + reasons[-1]
    return f"Task {index + 1} {joined}."
