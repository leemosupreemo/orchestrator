# Orchestrator Web UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish an authenticated Firebase preview URL for a custom web UI that mirrors every CLI menu and runs real Orchestrator jobs on a Mac.

**Architecture:** A TypeScript web client sends typed commands through Firestore. A local Python worker validates and executes them against shared Orchestrator operations and publishes safe projections. Firebase Authentication and rules restrict access; Firebase Storage carries uploaded references and redacted artifacts.

**Tech Stack:** Python 3.11+, TypeScript, React, Vite, Firebase Authentication, Cloud Firestore, Cloud Storage, Firebase Hosting.

**Spec:** `docs/superpowers/specs/2026-09-23-orchestrator-web-ui-design.md`

## Global Constraints

- Keep `.orchestrator/jobs/*.json` as canonical job records and preserve CLI behavior.
- Keep API keys, service credentials, and raw secrets off Firestore, Storage, and Git.
- Run commands serially per project; only typed allowlisted operations are executable.
- The first operator is one explicitly provisioned account; unsigned users cannot access jobs or commands.
- Use a dedicated Firebase project and a Hosting preview channel.
- Keep the Mac worker online for real job execution; show the offline state in the UI.

## Review Focus

- A stale job revision submitted from an old browser tab must fail without changing the local job.
- A command retried after a worker crash must not repeat a merge, discard, distribution, or other external side effect.
- A new CLI menu action must appear in the parity audit until web support is added.
- Logs and job fields containing credentials must be redacted before Firebase publication.
- A signed-in but unprovisioned user must not read jobs or enqueue operations.

---

### Task 1: Menu and command contract

**Files:** Create `orchestrator/web/contract.py`, `orchestrator/web/menu.py`, `tests/test_web_contract.py`.

**Interfaces:** `Operation` is an enum of typed actions. `available_actions(job)` returns the same enabled job actions as the CLI. `validate_command(data)` returns a parsed command or raises `ValueError`.

- [ ] Write tests for every CLI top-level and configuration menu label, all job status/type action sets, invalid operation names, unknown fields, and stale revision inputs.
- [ ] Run `python3 -m unittest tests.test_web_contract -v`; expected failure is missing web contract module.
- [ ] Add the operation registry and input validation, with no arbitrary command or path argument.
- [ ] Re-run the focused tests; expect zero failures. Commit contract and tests.

### Task 2: Shared local job operations

**Files:** Create `orchestrator/web/jobs.py`, `tests/test_web_jobs.py`; modify `orchestrator/scripts/dev_console.py` and selected CLI scripts only where logic must be shared.

**Interfaces:** `list_jobs(root)`, `get_job(root, id)`, `create_job(root, options)`, and `run_job_action(root, id, operation, inputs, expected_revision)` use the CLI's job files and scripts.

- [ ] Write focused tests with temporary project roots for read, create, legal action, illegal action, stale revision, and CLI visibility.
- [ ] Run `python3 -m unittest tests.test_web_jobs -v`; expect import or behavior failures.
- [ ] Extract callable operations from interactive prompts and wire both CLI and web callers to them. Keep existing CLI prompts and subprocess semantics.
- [ ] Re-run focused tests and `python3 -m unittest discover tests`; expect zero failures. Commit the job adapter.

### Task 3: Configuration and tools operations

**Files:** Create `orchestrator/web/tools.py`, `tests/test_web_tools.py`; modify CLI implementation where shared operations are necessary.

**Interfaces:** `run_tool(root, operation, inputs)` covers every configuration and advanced-tools item in the menu manifest, with typed inputs and explicit result objects.

- [ ] Write tests for each menu item's handler, project path confinement, local-only provider sign-in guidance, and refusal to transmit API keys.
- [ ] Run `python3 -m unittest tests.test_web_tools -v`; expect missing handlers.
- [ ] Implement handlers using existing Orchestrator functions and scripts, including archived jobs, model and fleet controls, audits, docs, distribution, tests, and updates.
- [ ] Re-run focused and full Python tests; expect zero failures. Commit the tools adapter.

### Task 4: Firebase worker and safe projections

**Files:** Create `orchestrator/web/worker.py`, `orchestrator/web/projection.py`, `tests/test_web_worker.py`; update `pyproject.toml` for worker dependencies.

**Interfaces:** `project_job(job)` returns a redacted browser-safe record. `Worker.process(command_id)` claims once, checks owner and revision, executes the operation, and publishes status and output. `Worker.reconcile()` republishes CLI changes.

- [ ] Write fake-transport tests for claim idempotency, serial execution, crash recovery, worker heartbeat, CLI reconciliation, stale revisions, and redaction.
- [ ] Run `python3 -m unittest tests.test_web_worker -v`; expect missing worker behavior.
- [ ] Implement the worker with Firebase Admin credentials loaded from the Mac environment and bounded output. Add a CLI entry point and local startup documentation.
- [ ] Re-run focused and full Python tests; expect zero failures. Commit the worker.

### Task 5: Firebase access and deployment configuration

**Files:** Create `firebase.json`, `firestore.rules`, `storage.rules`, `web/.env.example`, `docs/web-ui-firebase.md`, and rules tests.

**Interfaces:** Browser can create only valid queued command documents and upload scoped references; it cannot write job projections or worker results. Only the provisioned operator can read/write allowed resources.

- [ ] Write emulator rules tests for signed-out denial, unprovisioned denial, operator command creation, forbidden result writes, and Storage size/type limits.
- [ ] Run rules tests; expect denial and test failures before rules are implemented.
- [ ] Create the dedicated Firebase project, configure Authentication, Firestore, Storage, and Hosting, and deploy rules. Record setup steps without secrets.
- [ ] Re-run rules tests; expect zero failures. Commit config and documentation.

### Task 6: Web shell and job pages

**Files:** Create `web/package.json`, `web/src/*`, `web/index.html`, `web/tsconfig.json`, `web/vite.config.ts`, and web tests.

**Interfaces:** `FirebaseRepository` subscribes to authorized projects, jobs, worker state, and commands; pages render Jobs, New Job, Job Detail, Setup & Tools, and sign-in.

- [ ] Write component tests for signed-out, worker-offline, job list, job detail, and new-job options.
- [ ] Run web tests; expect missing components.
- [ ] Implement responsive navigation and accessible forms, preserving CLI labels and explaining status-specific actions.
- [ ] Run `npm --prefix web test` and `npm --prefix web run build`; expect zero failures. Commit web shell.

### Task 7: All menu actions and real command feedback

**Files:** Modify `web/src/*`; add browser workflow tests and parity tests.

**Interfaces:** Each menu item maps to a typed command or local-only setup instruction. The UI displays queued, running, succeeded, failed, stale, and offline states.

- [ ] Write workflow tests for every menu item, action confirmation, command payload, progress, result, and stale-action error.
- [ ] Run focused web tests; expect failures for unimplemented actions.
- [ ] Implement all remaining job and Setup & Tools screens and command forms. Add a parity test that compares web actions against the Python menu manifest.
- [ ] Re-run web tests, build, and Python suite; expect zero failures. Commit action coverage.

### Task 8: Preview deployment and end-to-end verification

**Files:** Update `docs/web-ui-firebase.md` and any defects found during testing.

**Interfaces:** A Firebase Hosting preview URL serves the authenticated UI and a connected Mac worker executes commands.

- [ ] Run all canonical applicable checks, Python tests, web tests, build, and Firebase rules tests; fix any failures.
- [ ] Start the worker, deploy the Hosting preview channel, and open the URL in a browser. Verify signed-out denial, operator sign-in, worker heartbeat, one harmless real command, CLI state sync, offline behavior, and one failed command.
- [ ] Record the URL, deployment command, project ID, and worker startup procedure in documentation. Commit the verified deployment metadata.

## Completion gate

Audit the full CLI menu and job action matrix against the web UI. Verify no button is a mock action. Review the entire branch, fix important findings, and report any environment limitation that prevents a real authenticated preview URL or worker test.
