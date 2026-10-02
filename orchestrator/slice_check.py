"""Is the plan a set of working slices, or a stack of layers?

A plan that builds the data layer, then the API, then the screen leaves nothing to
run until the last task, so every mistake is found late. A good feature plan makes
the first task produce something that works end to end, however small, and each
later task adds one capability on top. These checks read a plan's tasks and flag
the common layered shapes. They are heuristics over wording: they can be wrong,
so a flagged plan is sent back once and, if it still looks layered, kept with the
warning visible instead of being blocked.
"""
from __future__ import annotations

import re
from typing import Any

# Work that only prepares the ground: nothing a person could try exists after it.
FOUNDATION = ("schema", "migration", "database", "data model", "data layer", "models", "entities", "persistence", "repository", "types",
              "protocol", "interfaces", "scaffold", "boilerplate", "skeleton", "project setup", "infrastructure", "dependencies", "plumbing")
# Anything a person can see or do.
USER_FACING = ("screen", "view", "page", "button", "form", "command", "cli", "ui", "ux", "flow", "user can", "users can", "can now", "end-to-end",
               "end to end", "displays", "shows", "visible", "launch", "runs", "menu", "tab", "dialog", "output")
LAYERS = {
    "data": ("schema", "migration", "database", "data model", "data layer", "persistence", "storage", "entities", "orm"),
    "service": ("api", "endpoint", "backend", "service layer", "server", "repository", "controller", "business logic"),
    "ui": ("ui", "screen", "view", "page", "component", "frontend", "interface for", "layout"),
}


def _text(task: Any) -> str:
    if isinstance(task, str):
        return task.lower()
    if not isinstance(task, dict):
        return ""
    parts = [str(task.get("title", "")), str(task.get("description", ""))] + [str(a) for a in task.get("acceptance_criteria") or []]
    return " ".join(parts).lower()


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) for w in words)


def _layer(task: Any) -> str | None:
    title = (task.get("title", "") if isinstance(task, dict) else str(task)).lower()
    found = [name for name, words in LAYERS.items() if _has(title, words)]
    return found[0] if len(found) == 1 else None  # a task that spans layers is exactly what we want


def problems(plan: dict[str, Any]) -> list[str]:
    tasks = [t for t in (plan.get("tasks") or []) if t]
    if len(tasks) < 2:
        return []  # a single task is its own slice
    out = []
    first = tasks[0]
    text = _text(first)
    if _has(text, FOUNDATION) and not _has(text, USER_FACING):
        out.append("The first task only prepares foundations (data, types or setup). Nothing can be run or tried after it, "
                   "so mistakes won't show until much later.")
    if isinstance(first, dict) and not [a for a in first.get("acceptance_criteria") or [] if str(a).strip()]:
        out.append("The first task has no acceptance criteria, so nobody can tell whether it works.")
    layers = [_layer(t) for t in tasks]
    order = [l for l in layers if l]
    if len(tasks) >= 3 and "data" in order and "service" in order and "ui" in order \
            and order.index("data") < order.index("service") < order.index("ui") and len(order) >= 3 and order.count(None) == 0:
        out.append("The tasks are split by layer (data, then service, then screen). Each task should add one capability across the layers, "
                   "so something works after every task.")
    return out


def replan_request(found: list[str]) -> str:
    bullets = "\n".join(f"- {p}" for p in found)
    return ("### PLAN SHAPE (re-plan requested) ###\n"
            "Re-order and re-cut the tasks into vertical slices. After the FIRST task something real must run end to end, even the simplest "
            "version of the feature. Each later task adds one capability on top of something that already works and ends with the build and "
            "tests passing. Keep every other part of the plan.\n" + bullets + "\n\nReturn the full corrected plan JSON.")
