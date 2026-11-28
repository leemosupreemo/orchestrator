# Preview Update Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Recover Preview from a confirmed missing capability using a cancellable signed native update.
**Architecture:** A per-machine atomic update operation in the account control plane arbitrates cancellation against installation. The agent persists and delivers it to the native updater; the hosted preview modal follows it across reconnects.
**Tech Stack:** Python, Firestore transactions, vanilla JavaScript, Swift, Sparkle 2.7.3.
**Spec:** docs/superpowers/specs/2026-11-11-preview-update-recovery.md

## Global Constraints

- Preserve running work and the existing update journal/work gate.
- Preserve all existing managed-connection and full-screen-preview edits.
- Only signed native installations advertising `update_control: 1` support automatic recovery; other installs require a one-time upgrade.
- `preview_remote` is the explicit capability in runner state and machine heartbeat, independent of general API version. Required API metadata is informational; completion also verifies the actual preview capability.
- An update operation is `{id, action, state, created, expires}`; action is `update` or `cancel`; expiry is 1800 seconds. Terminal states: `current`, `cancelled`, `failed`, `unavailable`, `expired`. Installing state is `updating`. Other states: `requested`, `checking`, `downloading`, `waiting_for_work`, `cancelling`.
- Only matching operation IDs may report progress. Installing is claimed atomically before service shutdown. An installing operation never expires to cancellation.

## Review Focus

- Cancellation between downloading and install claim: exactly one wins, with honest UI.
- Expired/stale/replayed commands and late native callbacks cannot install another operation.
- Navigation or switching machines/projects must not retry Preview against stale context.
- A generic 404 or tunnel failure must never trigger an update.
- An unavailable updater/release or offline machine must not leave an infinite spinner or automatic update loop.

### Task 1: Atomic control-plane operations

Files: `cloud/functions/update_recovery.py` (new), `control_plane.py`, `main.py`, `tests/test_update_recovery.py` (new).
Interfaces: owner POST `machines/update-recovery` `{machine_id}` starts/joins; owner POST `machines/cancel-update` `{machine_id, operation_id}`; machine-auth POST `machine/update-progress` `{operation_id,state}` returns `{accepted, update_operation}`. GET machines adds `update_control`, `capabilities`, `update_operation`. Heartbeat accepts sanitized capability fields and returns `update_operation` until terminal acknowledgement. Use separate `machine_updates/<machine_id>` transaction to prevent races with regular heartbeat writes. `state=updating` is an atomic install claim; reject after cancel/expiry. Cancel after install returns 409.
- [x] Write tests for ownership, unsupported installers, expiry/dedup, cancellation vs claim, stale IDs and terminal progress.
- [x] Observe failures, implement pure state machine and atomic store mutation, then pass tests with existing control-plane tests.

### Task 2: Agent delivery, persistence, install gate

Files: `orchestrator/update_recovery.py` (new), `desktop_agent.py`, `desktop_protocol.py`, `account.py`, `web/server.py`, focused tests.
Interfaces: heartbeat capability `update_control=1` only from packaged native agent, capabilities `{preview_remote:true}`. `account.report_update(machine, operation_id, state)` calls machine/update-progress. Agent status adds `update_operation`. `update_progress` local command accepts optional `operation_id`; stale IDs rejected. `prepare_update` accepts optional `operation_id`; operation-specific installation claims server before gate preparation and reports true only if both accepted. Persist active operation in private state, resume/restore terminal state after relaunch, launch menu when update/cancel needs handling. Existing manual update commands remain backward compatible.
- [x] Write tests for replay, cancel delivery, persisted status, stale progress, busy gate and failed claim.
- [x] Observe failures, implement, run agent/protocol/account tests.

### Task 3: Native cancellation and progress

Files: `desktop/macos/Sources/Orchestrator/SparkleAdapter.swift`, `MenuController.swift`, `DesktopCore/UpdateScheduler.swift`, native tests.
Interfaces: read agent `update_operation`; send `update_progress` and `prepare_update` with operation_id for recovery sessions. Honor cancel before scheduler.observe. Native cancellation cancels Sparkle download/ready replies, not merely scheduler callbacks. Operation IDs bind progress; don't cancel unrelated scheduled updates. Use user-driver download cancellation and deferred ready reply for remote sessions, preserve existing local update UI. Report checking/downloading/waiting_for_work/current/failed/unavailable/cancelled; agent claims updating during prepare.
- [x] Write tests for cancelled late download and scheduler prepare races; observe failure.
- [x] Implement actual updater callbacks plus operation-bound MenuController delivery; run Swift package tests and build.

### Task 4: Hosted modal and preview retry

Files: `orchestrator/web/static/update-recovery.js` (new), `index.html`, `app.js`, focused JS-through-Node tests and docs.
Interfaces: module accepts authenticated cpApi, selected machine/project context, reconnect, preview probe and rendering callbacks. Confirm explicit missing capability before update; track one persisted attempt per context. Modal shows reported state, Cancel sends matching operation ID, acknowledges cancelled only after machine result, disables cancellation when updating, has bounded polling. Reload resumes operation. Success requires reconnect to same machine and verified `preview_remote`, then one preview retry.
- [x] Test missing capability vs ordinary errors, dedup, reload, stale context, cancel pending/offline, install conflict, timeout and successful one-shot retry.
- [x] Implement and integrate through preview/remote error handler, run Node behavior and Python UI tests.

### Task 5: Review and validation

- [x] Run compileall, full Python suite, Swift package tests/build, JS syntax, diff checks.
- [x] Independent review of the complete implementation and race/unsupported-install behaviors.
- [x] Record release/bootstrap limitations; do not publish UI ahead of compatible native and control-plane rollout.

## Execution record

Implemented and independently reviewed. The five review findings were fixed with regression tests. Whole Python suite: 2778 tests, 12 skipped; native: 72 tests and successful build; frontend: 32 behavior tests; real Firestore emulator: 3 tests. New Python state machines: 100% line/branch; four native recovery helpers: 100% measured coverage; frontend controller: 100% line/function, 98.87% V8 branches. See docs/testing/preview-update-recovery.md for commands and explicit signed-runtime/visual coverage gaps. No publication or changes to installed services were made.
