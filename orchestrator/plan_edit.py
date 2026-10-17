"""Edit a job's plan tasks in place, safely.

A plan's tasks are what the worker executes in order; finished ones are recorded
by index in `completed_task_indices`, and test cases point at tasks by their
1-based number. These helpers change the unfinished part of the plan while
keeping both of those consistent, so editing never marks the wrong task done or
orphans a test case. Finished tasks can't be changed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orchestrator.scripts import test_cases as tc

MAX_TITLE = 200
MAX_TEXT = 2000
MAX_LIST = 20
MAX_ITEM = 300
LOCKED_STATUSES = ("completed", "archived", "discarded")


class PlanEditError(ValueError):
    pass


def _tasks(job: dict[str, Any]) -> list[dict[str, Any]]:
    plan = job.setdefault("plan", {})
    if not isinstance(plan.get("tasks"), list):
        plan["tasks"] = []
    # Older plans store tasks as plain strings; editing needs the object form.
    plan["tasks"] = [t if isinstance(t, dict) else {"title": str(t)} for t in plan["tasks"]]
    return plan["tasks"]


COMPLETED_KEYS = ("completed_task_indices", "completed_tasks")  # the worker writes the first; older jobs and the UI summary also read the second


def _as_index(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _done(job: dict[str, Any]) -> set[int]:
    return {i for key in COMPLETED_KEYS for i in map(_as_index, job.get(key) or []) if i is not None}


def _lines(value: Any) -> list[str]:
    items = value.splitlines() if isinstance(value, str) else value if isinstance(value, list) else []
    out = [str(i).strip()[:MAX_ITEM] for i in items if str(i).strip()]
    return out[:MAX_LIST]


def _check_open(job: dict[str, Any]) -> None:
    if job.get("status") in LOCKED_STATUSES:
        raise PlanEditError("This job is finished; its plan can't be changed.")


def _check_index(job: dict[str, Any], index: Any) -> int:
    tasks = _tasks(job)
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(tasks):
        raise PlanEditError("That task doesn't exist.")
    if index in _done(job):
        raise PlanEditError("That task is already done and can't be changed.")
    return index


def _clean(fields: dict[str, Any], partial: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "title" in fields or not partial:
        title = str(fields.get("title") or "").strip()
        if not title:
            raise PlanEditError("A task needs a title.")
        if len(title) > MAX_TITLE:
            raise PlanEditError(f"Keep the title under {MAX_TITLE} characters.")
        out["title"] = title
    if "description" in fields:
        text = str(fields["description"] or "").strip()
        if len(text) > MAX_TEXT:
            raise PlanEditError(f"Keep the description under {MAX_TEXT} characters.")
        out["description"] = text
    for key in ("acceptance_criteria", "likely_files", "tests"):
        if key in fields:
            out[key] = _lines(fields[key])
    return out


def edit(job: dict[str, Any], index: Any, fields: dict[str, Any]) -> dict[str, Any]:
    _check_open(job)
    i = _check_index(job, index)
    task = _tasks(job)[i]
    task.update(_clean(fields, partial=True))
    return task


def add(job: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    _check_open(job)
    task = {"description": "", "acceptance_criteria": [], "likely_files": [], "tests": [], "complexity": "low", **_clean(fields, partial=False)}
    _tasks(job).append(task)
    return task


def _renumber_cases(job: dict[str, Any], mapping) -> None:
    for case in (job.get("plan") or {}).get("test_cases") or []:
        if isinstance(case, dict) and isinstance(case.get("task"), int):
            case["task"] = mapping(case["task"])


def remove(job: dict[str, Any], index: Any) -> dict[str, Any]:
    _check_open(job)
    i = _check_index(job, index)
    removed = _tasks(job).pop(i)
    for key in COMPLETED_KEYS:  # keep each list in its own form (ints or digit strings) but shifted
        if isinstance(job.get(key), list):
            shifted = [(_as_index(v) - 1 if _as_index(v) > i else _as_index(v)) for v in job[key] if _as_index(v) is not None]
            job[key] = [str(v) for v in shifted] if job[key] and isinstance(job[key][0], str) else shifted
    number = i + 1
    _renumber_cases(job, lambda t: None if t == number else (t - 1 if t > number else t))
    return removed


def move(job: dict[str, Any], index: Any, direction: str) -> None:
    _check_open(job)
    if direction not in ("up", "down"):
        raise PlanEditError("Move a task up or down.")
    i = _check_index(job, index)
    j = i - 1 if direction == "up" else i + 1
    tasks = _tasks(job)
    if not 0 <= j < len(tasks):
        raise PlanEditError("It's already at the end.")
    if j in _done(job):
        raise PlanEditError("A task can't move past finished work.")
    tasks[i], tasks[j] = tasks[j], tasks[i]
    a, b = i + 1, j + 1
    _renumber_cases(job, lambda t: b if t == a else (a if t == b else t))


def _cases(job: dict[str, Any]) -> list[dict[str, Any]]:
    plan = job.setdefault("plan", {})
    if not isinstance(plan.get("test_cases"), list):
        plan["test_cases"] = []
    return plan["test_cases"]


def add_case(job: dict[str, Any], fields: dict[str, Any], issue_number: Any = 0) -> dict[str, Any]:
    _check_open(job)
    cases = _cases(job)
    title = str(fields.get("title") or "").strip()
    expected = str(fields.get("expected") or "").strip()
    if not title or not expected:
        raise PlanEditError("Each test case needs a title and an expected result.")
    case_type = tc.normalize_type(fields.get("type"))
    priority = str(fields.get("priority") or "medium").lower()
    if priority not in ("high", "medium", "low"):
        priority = "medium"
    prefix = f"TC-{issue_number}-"
    taken = [str(c.get("id", "")) for c in cases if str(c.get("id", "")).startswith(prefix)]
    if not taken and cases:
        first_id = str(cases[0].get("id", ""))
        if first_id.startswith("TC-") and first_id.count("-") >= 2:
            prefix = first_id.rsplit("-", 1)[0] + "-"
            taken = [str(c.get("id", "")) for c in cases if str(c.get("id", "")).startswith(prefix)]
    nums = [int(i.rsplit("-", 1)[1]) for i in taken if i.rsplit("-", 1)[1].isdigit()]
    counter = (max(nums, default=0) + 1)
    case_id = f"{prefix}{counter:02d}"

    task = fields.get("task")
    if isinstance(task, str) and task.isdigit():
        task = int(task)
    elif not isinstance(task, int) or task <= 0:
        task = None

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    case = {
        "id": case_id,
        "area": str(fields.get("area") or "General").strip() or "General",
        "title": title,
        "type": case_type,
        "priority": priority,
        "preconditions": _lines(fields.get("preconditions")),
        "steps": _lines(fields.get("steps")),
        "expected": expected,
        "covers": _lines(fields.get("covers")),
        "tests": _lines(fields.get("tests")) if case_type not in tc.MANUAL_TYPES else [],
        "task": task,
        "created": now,
        "updated": now,
    }
    cases.append(case)
    return case


def edit_case(job: dict[str, Any], case_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    _check_open(job)
    cases = _cases(job)
    case = next((c for c in cases if c.get("id") == case_id), None)
    if not case:
        raise PlanEditError(f"Test case '{case_id}' doesn't exist.")
    if "title" in fields:
        title = str(fields["title"] or "").strip()
        if not title:
            raise PlanEditError("A test case needs a title.")
        case["title"] = title
    if "expected" in fields:
        exp = str(fields["expected"] or "").strip()
        if not exp:
            raise PlanEditError("A test case needs an expected result.")
        case["expected"] = exp
    if "area" in fields:
        case["area"] = str(fields["area"] or "General").strip() or "General"
    if "type" in fields:
        case["type"] = tc.normalize_type(fields["type"])
    if "priority" in fields:
        p = str(fields["priority"]).lower()
        if p in ("high", "medium", "low"):
            case["priority"] = p
    if "task" in fields:
        task = fields["task"]
        if isinstance(task, str) and task.isdigit():
            case["task"] = int(task)
        elif isinstance(task, int) and task > 0:
            case["task"] = task
        else:
            case["task"] = None
    for key in ("preconditions", "steps", "covers", "tests"):
        if key in fields:
            case[key] = _lines(fields[key])
    if tc.is_manual(case):
        case["tests"] = []
    case["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return case


def remove_case(job: dict[str, Any], case_id: str) -> dict[str, Any]:
    _check_open(job)
    cases = _cases(job)
    for i, c in enumerate(cases):
        if c.get("id") == case_id:
            return cases.pop(i)
    raise PlanEditError(f"Test case '{case_id}' doesn't exist.")

