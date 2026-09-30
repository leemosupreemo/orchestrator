# Capability-Aware Agentic Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route bounded role-tagged work packages through agentic execution when the active model/adapter/machine supports it, with recorded automatic fallback to guided execution.

**Architecture:** A focused execution-capability module describes complete model/adapter routes and supplies role-aware routing signals. A work-package module converts planned feature tasks into bounded assignments and computes conservative non-conflicting batches. Existing planner, scheduler, builder, and worker paths persist the selected mode, attempts, role context, handoffs, and fallback without replacing legacy single-builder jobs.

**Tech Stack:** Python 3.11+, dataclasses, enums, JSON job state, unittest, existing CLI adapters

**Spec:** `docs/superpowers/specs/2026-09-15-capability-aware-agentic-execution-design.md`

## Global Constraints

- Agentic capability belongs to the model, adapter, machine, tools, authentication, and permission combination.
- Orchestrator retains scope, permissions, budgets, scheduling, handoff validation, QA, and final acceptance.
- Sub-agents are short-lived and receive one bounded role-tagged work package.
- Fallback is automatic and recorded; it pauses only when every safe route is exhausted or new authority is needed.
- Parallel readiness requires satisfied dependencies and non-overlapping declared scopes; unknown scope conflicts.
- Existing jobs and model metadata remain compatible.
- No new third-party dependencies.

---

### Task 1: Execution Profiles and Role-Aware Routing

**Files:**
- Create: `orchestrator/scripts/execution_capabilities.py`
- Modify: `orchestrator/scripts/model_router.py`
- Create: `tests/test_execution_capabilities.py`

**Interfaces:**
- Produces: `ExecutionMode`, `ExecutionProfile`, `profile_for_model(model_name, machine=None, probe=None)`, `mode_for_role(profile, role)`, and `supports_mode(profile, mode)`.
- Extends: `score_model(..., required_execution_mode=None)` and `get_prioritized_models(..., required_execution_mode=None)`.

- [x] **Step 1: Write failing tests** proving Codex/Claude/Gemini/OpenCode builder routes are agentic, API-like/local text routes default conservatively, missing runtime CLIs remove agentic availability, and builder routing prefers an allowed agentic route.
- [x] **Step 2: Run `python3 -m unittest tests.test_execution_capabilities -v`** and confirm the module/interface failures.
- [x] **Step 3: Implement immutable adapter capability definitions and runtime profile resolution.** Profiles must serialize to JSON-safe dictionaries and unknown adapters must never gain edit or shell tools.
- [x] **Step 4: Add execution-mode scoring to the router** without changing ordering when no mode is requested.
- [x] **Step 5: Run `python3 -m unittest tests.test_execution_capabilities -v`** and confirm all routing tests pass.

### Task 2: Bounded Work Packages and Guarded Scheduling

**Files:**
- Create: `orchestrator/scripts/work_packages.py`
- Modify: `orchestrator/prompts/planner_feature.md`
- Modify: `orchestrator/scripts/new_job.py`
- Modify: `orchestrator/scripts/schedule_job.py`
- Create: `tests/test_work_packages.py`

**Interfaces:**
- Produces: `build_work_packages(job) -> list[dict]`, `ready_work_packages(packages) -> list[dict]`, `scopes_overlap(left, right) -> bool`, and `non_conflicting_batch(packages, limit) -> list[dict]`.
- Persists: `job["work_packages"]`, including role, objective, requested mode, tools, dependencies, scopes, acceptance criteria, budgets, state, and attempts.

- [x] **Step 1: Write failing tests** for stable package IDs, role attachment, explicit dependencies, unknown-scope conflict, file/directory overlap, independent batching, and legacy plans.
- [x] **Step 2: Run `python3 -m unittest tests.test_work_packages -v`** and confirm failures.
- [x] **Step 3: Extend the feature-plan schema** with optional `role`, `depends_on`, and `execution_mode` task fields while keeping older planner responses valid.
- [x] **Step 4: Implement work-package normalization and conservative batching.** Missing roles default to `implementation_engineer`; missing scopes conflict; explicit dependencies gate readiness.
- [x] **Step 5: Persist packages after planning and expose package requirements to scheduling.** Scheduler model choice must request the package execution mode and record the resolved profile.
- [x] **Step 6: Run focused package, scheduler, and E2E tests.**

### Task 3: Agentic Attempts, Automatic Fallback, and Handoffs

**Files:**
- Modify: `orchestrator/scripts/llm.py`
- Modify: `orchestrator/scripts/run_builder.py`
- Modify: `orchestrator/scripts/worker_run.py`
- Create: `tests/test_agentic_execution.py`
- Modify: `tests/test_e2e_workflow.py`

**Interfaces:**
- Extends: `run_llm(..., attempt_log: list[dict] | None = None, required_execution_mode: str | None = None)`.
- Persists: package/job `execution_attempts` with requested/actual mode, model, adapter, outcome, downgrade reason, timestamps, and session ID.
- Produces: validated `execution_handoff` with work performed, evidence, files, tests, criteria status, risks, and next action.

- [ ] **Step 1: Write failing tests** for agentic-first attempts, fallback to another agentic model, downgrade to guided mode, attempt persistence, work-package prompt context, and invalid handoff rejection.
- [ ] **Step 2: Run the focused tests** and confirm missing trace/fallback behavior.
- [ ] **Step 3: Add optional attempt tracing to `run_llm`.** Each failed and successful model attempt records its actual profile; fallback order prefers the requested execution mode and then safely downgrades.
- [ ] **Step 4: Pass bounded package context through the builder** and persist attempts without changing the existing return tuple.
- [ ] **Step 5: Validate structured handoffs against actual builder output and workspace/test evidence.** Missing optional handoff data is synthesized from verified state; contradictory success claims remain failures.
- [ ] **Step 6: Run focused execution and worker tests.**

### Task 4: User-Facing Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/user-guide.md`
- Modify: `docs/superpowers/specs/2026-09-15-role-based-team-design.md`

**Interfaces:**
- Documents: Role-based team selection and capability-aware agentic execution as complementary features, including automatic route choice, bounded authority, guarded parallelism, and fallback cost/behavior.

- [ ] **Step 1: Update README and user guide** with concise normal-user explanations and a short advanced execution-profile section.
- [ ] **Step 2: Run `python3 -m compileall -q orchestrator tests`.**
- [ ] **Step 3: Run `python3 -m unittest discover -s tests`.**
- [ ] **Step 4: Run `git diff --check` and inspect `git diff --stat`.**
