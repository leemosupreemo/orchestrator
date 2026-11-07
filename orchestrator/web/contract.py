from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Operation(StrEnum):
    JOB_CREATE = "job.create"
    JOB_APPROVE_SUBTASKS = "job.approve_subtasks"
    JOB_SCHEDULE = "job.schedule"
    JOB_EXECUTE = "job.execute"
    JOB_REVISE = "job.revise"
    JOB_DEBUG = "job.debug"
    JOB_RESUME = "job.resume"
    JOB_MERGE = "job.merge"
    JOB_DELIVER = "job.deliver"
    JOB_ITERATE = "job.iterate"
    JOB_REOPEN_BUG = "job.reopen_bug"
    JOB_RERUN = "job.rerun"
    JOB_LINK_LOGS = "job.link_logs"
    JOB_ATTACH_REFERENCE = "job.attach_reference"
    JOB_ASK_AI = "job.ask_ai"
    JOB_MODELS = "job.models"
    JOB_EXPORT = "job.export"
    JOB_BRIEF = "job.brief"
    JOB_GITHUB = "job.github"
    JOB_CLOSE_ISSUE = "job.close_issue"
    JOB_DISCARD = "job.discard"
    JOB_FILES = "job.files"
    TESTS_RUN = "tests.run"
    PROJECT_REFRESH = "project.refresh"
    TOOL_MODELS = "tool.models"
    TOOL_KEYS = "tool.keys"
    TOOL_FLEET = "tool.fleet"
    TOOL_AUDIT = "tool.audit"
    TOOL_BASE_BRANCH = "tool.base_branch"
    TOOL_PROJECT = "tool.project"
    TOOL_ARCHIVE = "tool.archive"
    TOOL_INSTRUCTIONS = "tool.instructions"
    TOOL_EMAIL = "tool.email"
    TOOL_DOCS = "tool.docs"
    TOOL_DISTRIBUTION = "tool.distribution"
    TOOL_SELF_TESTS = "tool.self_tests"
    TOOL_UPDATE = "tool.update"


JOB_MUTATIONS = frozenset({
    Operation.JOB_APPROVE_SUBTASKS, Operation.JOB_SCHEDULE, Operation.JOB_EXECUTE,
    Operation.JOB_REVISE, Operation.JOB_DEBUG, Operation.JOB_RESUME, Operation.JOB_MERGE,
    Operation.JOB_DELIVER, Operation.JOB_ITERATE, Operation.JOB_REOPEN_BUG,
    Operation.JOB_RERUN, Operation.JOB_LINK_LOGS, Operation.JOB_ATTACH_REFERENCE,
    Operation.JOB_MODELS, Operation.JOB_CLOSE_ISSUE, Operation.JOB_DISCARD,
})

INPUT_FIELDS: dict[Operation, frozenset[str]] = {
    Operation.TESTS_RUN: frozenset({"action", "plan"}),
    Operation.JOB_CREATE: frozenset({"job_type", "prompt", "repro", "expected", "subsystems", "vibe", "quick", "spec_url", "artifact_id", "spec_file_name", "yolo", "branch_mode", "preset", "planner", "builder", "reviewer", "allowed_models", "allowed_machines", "stitch", "free"}),
    Operation.JOB_REVISE: frozenset({"feedback"}),
    Operation.JOB_ITERATE: frozenset({"feedback"}),
    Operation.JOB_REOPEN_BUG: frozenset({"feedback", "log_paths"}),
    Operation.JOB_LINK_LOGS: frozenset({"log_paths"}),
    Operation.JOB_ATTACH_REFERENCE: frozenset({"artifact_id", "source_url", "description"}),
    Operation.JOB_ASK_AI: frozenset({"question", "model"}),
    Operation.JOB_MODELS: frozenset({"allowed_models"}),
    Operation.JOB_DEBUG: frozenset({"feedback", "max_iterations", "notes", "bug_reproduced"}),
    Operation.JOB_RERUN: frozenset({"branch_mode", "log_paths"}),
    Operation.TOOL_MODELS: frozenset({"allowed_models", "refresh", "live_discovery", "yolo"}),
    Operation.TOOL_KEYS: frozenset({"action"}),
    Operation.TOOL_FLEET: frozenset({"action", "machine_ids", "name", "new_name", "ssh_target", "repo_path"}),
    Operation.TOOL_BASE_BRANCH: frozenset({"branch"}),
    Operation.TOOL_PROJECT: frozenset({"project_id", "path", "action"}),
    Operation.TOOL_ARCHIVE: frozenset({"action", "job_id"}),
    Operation.TOOL_INSTRUCTIONS: frozenset({"action", "name", "content"}),
    Operation.TOOL_EMAIL: frozenset({"action", "email", "provider", "content", "sender_email", "display_name"}),
    Operation.TOOL_DOCS: frozenset({"name"}),
    Operation.TOOL_DISTRIBUTION: frozenset({"action", "job_id"}),
    Operation.TOOL_SELF_TESTS: frozenset({"suite"}),
    Operation.TOOL_UPDATE: frozenset({"action", "fleet"}),
}

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class Command:
    project_id: str
    operation: Operation
    inputs: dict[str, Any]
    job_id: str | None = None
    expected_revision: int | None = None


def validate_command(data: dict[str, Any]) -> Command:
    if not isinstance(data, dict):
        raise ValueError("Command must be an object")
    unknown = set(data) - {"project_id", "operation", "inputs", "job_id", "expected_revision"}
    if unknown:
        raise ValueError(f"Unknown command fields: {', '.join(sorted(unknown))}")
    project_id = data.get("project_id")
    if not isinstance(project_id, str) or not _IDENTIFIER.fullmatch(project_id):
        raise ValueError("Invalid project ID")
    try:
        operation = Operation(data["operation"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Unknown operation") from exc
    job_id = data.get("job_id")
    if job_id is not None and (not isinstance(job_id, str) or not _IDENTIFIER.fullmatch(job_id)):
        raise ValueError("Invalid job ID")
    if operation.value.startswith("job.") and operation != Operation.JOB_CREATE and job_id is None:
        raise ValueError("Job ID required")
    revision = data.get("expected_revision")
    if operation in JOB_MUTATIONS and (type(revision) is not int or revision < 0):
        raise ValueError("Expected job revision required")
    if revision is not None and (type(revision) is not int or revision < 0):
        raise ValueError("Invalid job revision")
    inputs = data.get("inputs", {})
    if not isinstance(inputs, dict):
        raise ValueError("Inputs must be an object")
    unknown_inputs = set(inputs) - INPUT_FIELDS.get(operation, frozenset())
    if unknown_inputs:
        raise ValueError(f"Unknown inputs for {operation}: {', '.join(sorted(unknown_inputs))}")
    return Command(project_id, operation, inputs, job_id, revision)
