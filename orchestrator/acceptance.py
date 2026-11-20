"""The acceptance criteria a job's work must meet, in one place for every step that judges the work.

A job's criteria live in its plan (`plan.acceptance_criteria`, plain text) and in its living spec (`spec.acceptance_criteria`
with who asked for each, plus requirements that came from the person's feedback). The builder was shown the plan's list, but the
debugger and the reviewer only got the original brief. So a requirement added later ("Increase foliage density 2x") never reached
the fix loop, which drifted to making tests pass, and the reviewer had nothing to check the diff against. These helpers are pure:

    criteria(job)        the distinct criteria, the person's own first
    block(job)           them as a prompt section ("" when there are none)
    diff_warnings(job)   red flags from what the job changed, for the reviewer (a product job that only touched tests)
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from orchestrator import builder_check
from orchestrator.job_scope import TEST_TYPES, is_test_file

MAX_CRITERIA = 12
MAX_TEXT = 300
# Said by every plan; it asks for nothing a reviewer could check.
GENERIC = {"implementation matches provided instructions."}
# Where a requirement came from the person themselves, not from the planner's inference.
PERSON_SOURCES = {"user_experience_feedback", "user_prompt", "human_feedback"}


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:MAX_TEXT]


def criteria(job: dict[str, Any]) -> list[dict[str, Any]]:
    """[{"text", "person"}] for the job: spec criteria, requirements that came from the person's feedback, and the plan's list.
    `person` is true when the person asked for it. Distinct by wording, the person's own first, capped."""
    found: dict[str, dict[str, Any]] = {}

    def add(text: Any, person: bool) -> None:
        clean = _text(text)
        key = clean.lower()
        if not clean or key in GENERIC:
            return
        if key in found:
            found[key]["person"] = found[key]["person"] or person
        else:
            found[key] = {"text": clean, "person": person}

    spec = job.get("spec") if isinstance(job.get("spec"), dict) else {}
    for item in spec.get("acceptance_criteria") or []:
        if isinstance(item, dict):
            add(item.get("text"), item.get("provenance") == "explicit" or item.get("source") in PERSON_SOURCES)
        else:
            add(item, False)
    for item in spec.get("functional_requirements") or []:
        if isinstance(item, dict) and item.get("source") == "user_experience_feedback":
            add(item.get("text"), True)
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    for item in plan.get("acceptance_criteria") or []:
        add(item.get("text") if isinstance(item, dict) else item, False)
    ordered = sorted(found.values(), key=lambda c: not c["person"])  # stable: the person's own first
    return ordered[:MAX_CRITERIA]


def block(job: dict[str, Any]) -> str:
    """The criteria as a section for a model's prompt, or "" when the job has none."""
    items = criteria(job)
    if not items:
        return ""
    lines = [f"- {'[asked for by the person] ' if c['person'] else ''}{c['text']}" for c in items]
    return ("Acceptance criteria for this job. The work is only done when the change itself meets each one; passing tests "
            "does not satisfy them:\n" + "\n".join(lines) + "\n")


def changed_files(job: dict[str, Any]) -> list[str]:
    """Files the job changed or added, without local tool state."""
    files = [*(job.get("ai_modified_files") or []), *(job.get("ai_untracked_files") or [])]
    return sorted({f for f in files if isinstance(f, str) and f and not builder_check.is_ignored_tool_path(f)})


def diff_warnings(job: dict[str, Any]) -> list[str]:
    """Red flags from what the job changed, for the reviewer: a job that asks for product behavior but only changed tests."""
    if not job or job.get("type") in TEST_TYPES or job.get("output_scope") == "tests-only":
        return []
    files = changed_files(job)
    if not files or not all(is_test_file(PurePosixPath(f.replace("\\", "/"))) for f in files):
        return []
    items = criteria(job)
    asked = items[0]["text"] if items else str(job.get("title") or "the brief")
    shown = ", ".join(files[:4]) + (f" and {len(files) - 4} more" if len(files) > 4 else "")
    return [f"Every file this job changed is a test file ({shown}), but it asks for: {asked}. "
            "Check that the product itself changed to meet it, not only the tests."]
