from __future__ import annotations

import re
from typing import Any
from .menu import available_actions


_SECRETS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b"),
]


def redact_text(value: str) -> str:
    result = value
    for pattern in _SECRETS:
        result = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", result)
    return result


def redact_data(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items() if not any(
            word in key.lower() for word in ("api_key", "password", "secret", "credential", "raw_input"))}
    return value


def project_job(job: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "job_id", "title", "type", "status", "created_at", "updated_at", "revision",
        "branch", "issue_number", "pr_number", "planner", "builder", "reviewer",
        "allowed_models", "allowed_machines", "completed_task_indices", "debug_phase",
        "iteration", "max_iterations", "ai_modified_files", "ai_untracked_files",
        "last_manual_log_paths", "reference_artifacts", "llm_sessions",
    )
    projection = {key: job[key] for key in keys if key in job}
    projection["actions"] = [{"label": item.label, "operation": item.operation.value}
                             for item in available_actions(job) if item.operation]
    plan = job.get("plan")
    if isinstance(plan, dict):
        projection["plan"] = {key: plan[key] for key in ("title", "summary", "tasks", "acceptance_criteria") if key in plan}
    history = job.get("debug_history")
    if isinstance(history, list):
        projection["debug_history"] = history[-10:]
    return redact_data(projection)
