# Final Job Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement the tasks. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make manual review the final job step, with verified app delivery when configured.

**Architecture:** A shared Python review module produces final_review for the API and validates final actions. Delivery records identify the built commit. The frontend displays that state and gathers explicit acknowledgment.

**Tech Stack:** Existing Python, unittest, plain JavaScript, Node rendering checks.

**Spec:** `docs/superpowers/specs/2026-11-17-final-job-review-design.md`

## Global Constraints

- No real delivery, merge, or completion during development.
- Keep the existing merge/complete operations and five-stage lifecycle.
- Missing configuration permits manual approval; configured but unsuccessful delivery blocks it.
- Manual approval and delivery must match the current revision.
- Preserve existing unrelated uncommitted edits.

## Review Focus

- Approval through menus/API must enforce the same checks as the final panel.
- Old receipts, different branches/jobs, changed commits, and retries must not look delivered.
- Tests/design-only work must remain completable without an app build.
- Finishing the last task in YOLO must still wait on a person.
- Failed merge/complete must not leave a successful sign-off on disk.

### Task 1: Review state, receipt revision, and final-action guards

Files: new `orchestrator/job_review.py`, new `tests/test_job_review.py`; modify `orchestrator/web/server.py`, `orchestrator/scripts/job_actions.py`, `orchestrator/scripts/deliver_build.py`, relevant existing tests.

Interface: job detail adds `final_review` with `ready`, `can_approve`, `reason`, `revision`, `approval`, and `delivery`. Delivery has `status` (unavailable/pending/delivered/failed/stale), `required`, `reason`, `url`, `version`, `build`, `built_at`. Final Web UI actions require `reviewed: true` and `review_revision` matching state.

- [ ] Prove validation/receipt failures in tests before implementation.
- [ ] Implement shared state and guard Web UI actions and CLI --reviewed attestation.
- [ ] Record delivery attempts and built commit, rejecting stale successes.
- [ ] Verify focused Python tests and offline workflow.

### Task 2: Final review UI

Files: `orchestrator/web/static/app.js`, new `tests/test_job_review_ui.py`.

- [ ] Test final panel rendering, optional delivery, failed delivery, and review acknowledgment.
- [ ] Rename final step; show delivery/retry/link/details and block approval appropriately.
- [ ] Route every merge/complete button through a fresh review dialog and send explicit attestation/revision.
- [ ] Verify rendering tests and existing job-page checks.

### Task 3: Review and validation

- [ ] Run canonical compile and full test commands, report pre-existing failures separately.
- [ ] Perform independent code review, fix material findings, and recheck affected tests.
- [ ] Reload the idle local UI safely and verify the new response; leave changes uncommitted.

Execution proceeds under the user's existing approval; backend and frontend work have separate file ownership.
