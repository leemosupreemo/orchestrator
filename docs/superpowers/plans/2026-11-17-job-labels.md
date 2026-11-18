# Concise Job Labels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Apply the approved twelve Web UI labels without changing workflow behavior.

**Architecture:** Update `job_state` and remote fallback labels. Keep reasons and actions intact; use explicit progress metadata for filtering and raw status/action for epic readiness.

**Tech Stack:** Existing Python server and plain JavaScript frontend.

**Spec:** `docs/superpowers/specs/2026-11-17-job-labels-design.md`

## Global Constraints

- Use exactly the twelve labels in the spec for recognized job states.
- Preserve internal statuses, actions, tones, groups, recovery steps, and descriptions.
- Preserve specific blocker and task-growth context in the reason.
- Do not change activity/run statuses or product-completeness labels.

## Review Focus

- Partially complete jobs must not newly enter bulk YOLO actions.
- Interrupted builds must remain in Home's In progress list.
- An epic requiring a design decision must not become ready to build accidentally.
- Blocker details and commit-only recovery must remain visible.
- Older remote snapshots without progress metadata must still render sensibly.

### Task 1: Consolidate job labels and preserve behavior

**Files:** Modify `orchestrator/web/server.py`, `orchestrator/web/static/app.js`, `orchestrator/account.py`, `orchestrator/plan_run.py`, `docs/user-guide.md`, and existing status-related tests.

**Interfaces:** `job_state(job, root=None)` retains its fields and adds `in_progress: bool`; Home consumes it with a legacy fallback.

- [x] Update existing status assertions for the approved wording and verify they fail before implementation.
- [x] Update labels, move variable heading context into reasons, and preserve progress/epic behavior independently of labels.
- [x] Update remote fallback labels and Home's progress predicate.
- [x] Verify focused status tests, then `python3 -m unittest discover -s tests` and `python3 -m compileall -q orchestrator tests`.
- [x] Review the final diff and leave the result uncommitted for the user to review.

Execution: inline, as authorized by the user's “continue”; no additional approval or execution-method question is needed.
