from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contract import Operation


@dataclass(frozen=True)
class MenuItem:
    label: str
    operation: Operation | None = None


def main_menu() -> list[MenuItem]:
    return [
        MenuItem("New Job", Operation.JOB_CREATE),
        MenuItem("Run Unit Tests", Operation.TESTS_RUN),
        MenuItem("Configuration & Tools"),
        MenuItem("Refresh & Sync", Operation.PROJECT_REFRESH),
    ]


def configuration_menu() -> list[MenuItem]:
    return [
        MenuItem("LLM Models", Operation.TOOL_MODELS),
        MenuItem("Manage LLM API Keys", Operation.TOOL_KEYS),
        MenuItem("Manage Machine Fleet", Operation.TOOL_FLEET),
        MenuItem("Run Prerequisite Audit", Operation.TOOL_AUDIT),
        MenuItem("Select Base Branch", Operation.TOOL_BASE_BRANCH),
        MenuItem("Change Target Project", Operation.TOOL_PROJECT),
        MenuItem("Manage Archived Jobs", Operation.TOOL_ARCHIVE),
        MenuItem("AI Instruction Settings", Operation.TOOL_INSTRUCTIONS),
        MenuItem("Email Notification Settings", Operation.TOOL_EMAIL),
        MenuItem("Documentation & Architecture Guides", Operation.TOOL_DOCS),
        MenuItem("Firebase App Distro", Operation.TOOL_DISTRIBUTION),
        MenuItem("Orchestrator Self-Tests", Operation.TOOL_SELF_TESTS),
        MenuItem("Update Orchestrator", Operation.TOOL_UPDATE),
    ]


_COMMON = [
    MenuItem("Link Logs", Operation.JOB_LINK_LOGS),
    MenuItem("Attach UI Mockup / Reference", Operation.JOB_ATTACH_REFERENCE),
    MenuItem("Ask AI", Operation.JOB_ASK_AI),
    MenuItem("Select LLM Models", Operation.JOB_MODELS),
    MenuItem("Export Context", Operation.JOB_EXPORT),
    MenuItem("View Brief / Summary", Operation.JOB_BRIEF),
    MenuItem("View in GitHub", Operation.JOB_GITHUB),
    MenuItem("Close Issue in GitHub", Operation.JOB_CLOSE_ISSUE),
    MenuItem("Discard & Revert Changes", Operation.JOB_DISCARD),
]


def available_actions(job: dict[str, Any]) -> list[MenuItem]:
    status = job.get("status")
    job_type = job.get("type")
    phase = job.get("debug_phase", "propose")
    actions: list[MenuItem] = []
    if status == "designing":
        actions.append(MenuItem("Approve Design & Plan Implementation", Operation.JOB_APPROVE_SUBTASKS))
        actions.append(MenuItem("Tweak / Revise Design", Operation.JOB_REVISE))
    elif status == "planned":
        actions.append(MenuItem("Approve / Create Sub-tasks" if job_type == "feature-plan" else "Schedule & Dispatch",
                                Operation.JOB_APPROVE_SUBTASKS if job_type == "feature-plan" else Operation.JOB_SCHEDULE))
        actions.append(MenuItem("Tweak / Revise Plan", Operation.JOB_REVISE))
    elif status == "scheduled":
        actions.append(MenuItem("Execute", Operation.JOB_EXECUTE))
    elif status == "debugging":
        if phase in {"propose", "paused", "verify"}:
            actions.append(MenuItem("Fix / Verify Failing Tests", Operation.JOB_DEBUG))
        actions.extend([MenuItem("Resume Job", Operation.JOB_RESUME), MenuItem("Tweak / Give Hint", Operation.JOB_REVISE)])
    elif status == "review-needed":
        actions.extend([MenuItem("Merge & Mark Completed", Operation.JOB_MERGE),
                        MenuItem("Deliver to Device", Operation.JOB_DELIVER),
                        MenuItem("Tweak / Iterate Further", Operation.JOB_ITERATE)])
        if job_type in {"bug-fix", "bug-investigate"}:
            actions.append(MenuItem("Bug Still Happening?", Operation.JOB_REOPEN_BUG))
        tasks = job.get("plan", {}).get("tasks", [])
        if tasks and len(job.get("completed_task_indices", [])) < len(tasks):
            actions.append(MenuItem("Resume Job", Operation.JOB_RESUME))
    elif status == "human-needed":
        if job.get("branch"):
            actions.append(MenuItem("Deliver to Device", Operation.JOB_DELIVER))
        actions.extend([MenuItem("Tweak / Revise", Operation.JOB_ITERATE), MenuItem("Resume Job", Operation.JOB_RESUME)])
    elif status == "executing":
        actions.append(MenuItem("Resume Job", Operation.JOB_RESUME))
    if status not in {"completed", "discarded", "planned"}:
        actions.extend([MenuItem("Auto-Fix / Iterate", Operation.JOB_DEBUG), MenuItem("Full Re-run", Operation.JOB_RERUN)])
    if job.get("ai_modified_files") or job.get("ai_untracked_files"):
        actions.append(MenuItem("View AI Changes", Operation.JOB_FILES))
    return actions + _COMMON
