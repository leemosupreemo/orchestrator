"""Project-local evidence about the models that ran job stages."""
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

FIELD = "model_outcomes"
RESULTS = {
    "planner": {"plan_gate_passed"},
    "builder": {"build_checks_passed"},
    "reviewer": {"review_generated"},
}
TEXT_FIELDS = ("id", "at", "role", "selected_model", "actual_model", "job_type", "result")
MAX_OUTCOMES = 100


def merge(*histories: Any) -> list[dict[str, Any]]:
    """Keep bounded, deduplicated metadata from local and shared job copies."""
    entries: dict[str, dict[str, Any]] = {}
    for history in histories:
        for entry in history if isinstance(history, list) else []:
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(key), str) and 0 < len(entry[key]) <= 160 for key in TEXT_FIELDS
            ):
                continue
            if entry["result"] not in RESULTS.get(entry["role"], ()):
                continue
            item = {key: entry[key] for key in TEXT_FIELDS}
            tasks = entry.get("tasks")
            if isinstance(tasks, list) and all(type(task) is int and task > 0 for task in tasks):
                item["tasks"] = tasks[:100]
            entries[item["id"]] = item
    return sorted(entries.values(), key=lambda item: (item["at"], item["id"]))[-MAX_OUTCOMES:]


def record(job: dict[str, Any], role: str, selected: str | None, actual: str | None, result: str,
           *, tasks: list[int] | None = None) -> None:
    if not selected or not actual:
        return
    entry = {
        "id": uuid4().hex,
        "at": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "selected_model": selected,
        "actual_model": actual,
        "job_type": job.get("type") or "generic",
        "result": result,
        "tasks": tasks,
    }
    job[FIELD] = merge(job.get(FIELD), [entry])
