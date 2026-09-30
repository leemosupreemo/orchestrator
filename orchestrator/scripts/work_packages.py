#!/usr/bin/env python3
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


DEFAULT_BUDGETS = {"iterations": 4, "minutes": 20}
ACCEPTED_STATUSES = {"accepted", "completed"}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "work"


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _active_role_ids(job: dict[str, Any]) -> set[str]:
    return {str(role.get("id")) for role in (job.get("team") or {}).get("roles", []) if role.get("id")}


def _package_from_task(job: dict[str, Any], task: dict[str, Any], index: int) -> dict[str, Any]:
    title = str(task.get("title") or f"Task {index + 1}")
    package_id = str(task.get("id") or f"task-{index + 1}-{_slug(title)}")
    requested_role = str(task.get("role") or "implementation_engineer")
    active_roles = _active_role_ids(job)
    if active_roles and requested_role not in active_roles:
        requested_role = "implementation_engineer"
    return {
        "id": package_id,
        "role": requested_role,
        "objective": str(task.get("description") or title),
        "requested_mode": str(task.get("execution_mode") or "agentic"),
        "required_tools": _string_list(task.get("required_tools")) or ["read", "edit", "shell", "tests"],
        "dependencies": _string_list(task.get("depends_on")),
        "scopes": _string_list(task.get("likely_files")),
        "acceptance_criteria": _string_list(task.get("acceptance_criteria")),
        "tests": _string_list(task.get("tests")),
        "budgets": deepcopy(task.get("budgets") or DEFAULT_BUDGETS),
        "status": "pending",
        "attempts": [],
        "handoff": None,
    }


def build_work_packages(job: dict[str, Any]) -> list[dict[str, Any]]:
    plan = job.get("plan") or {}
    tasks = [task for task in plan.get("tasks", []) if isinstance(task, dict)]
    if tasks:
        packages = [_package_from_task(job, task, index) for index, task in enumerate(tasks)]
    else:
        packages = [{
            "id": "main",
            "role": "implementation_engineer",
            "objective": str(plan.get("summary") or job.get("title") or "Complete the job"),
            "requested_mode": "agentic",
            "required_tools": ["read", "edit", "shell", "tests"],
            "dependencies": [],
            "scopes": _string_list(plan.get("likely_files")),
            "acceptance_criteria": _string_list(plan.get("acceptance_criteria")),
            "tests": _string_list(plan.get("test_recommendations")),
            "budgets": deepcopy(DEFAULT_BUDGETS),
            "status": "pending",
            "attempts": [],
            "handoff": None,
        }]

    previous = {str(item.get("id")): item for item in job.get("work_packages", []) if isinstance(item, dict)}
    for package in packages:
        old = previous.get(package["id"])
        if not old:
            continue
        for field in ("status", "attempts", "handoff"):
            if field in old:
                package[field] = deepcopy(old[field])
    return packages


def ready_work_packages(packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    statuses = {str(package.get("id")): package.get("status") for package in packages}
    return [
        package for package in packages
        if package.get("status") in {None, "pending", "scheduled"}
        and all(statuses.get(dependency) in ACCEPTED_STATUSES for dependency in package.get("dependencies", []))
    ]


def _normalized_scope(scope: str) -> str:
    return scope.strip().removeprefix("./").rstrip("/")


def scopes_overlap(left: list[str], right: list[str]) -> bool:
    if not left or not right:
        return True
    for left_scope in map(_normalized_scope, left):
        for right_scope in map(_normalized_scope, right):
            if not left_scope or not right_scope:
                return True
            if left_scope == right_scope:
                return True
            if left_scope.startswith(f"{right_scope}/") or right_scope.startswith(f"{left_scope}/"):
                return True
    return False


def non_conflicting_batch(packages: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for candidate in ready_work_packages(packages):
        if len(selected) >= max(1, limit):
            break
        if any(scopes_overlap(candidate.get("scopes", []), item.get("scopes", [])) for item in selected):
            continue
        selected.append(candidate)
    return selected
