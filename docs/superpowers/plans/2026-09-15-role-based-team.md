# Role-Based Team Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional, recommended Role-based team development approach that automatically assembles a right-sized engineering roster, preserves a shared user-goal brief, and exposes team status and controls throughout a job.

**Architecture:** A focused `team_roles` module owns the role catalog, deterministic selection, manifest updates, Intent Brief creation, and prompt formatting. Job creation persists the selected approach and manifest; the existing planner, builder, and reviewer consume the shared context without replacing the current workflow engine. The dev console adds approach selection, compact roster visibility, and in-progress roster editing.

**Tech Stack:** Python 3.11+, argparse, JSON job state, unittest, terminal console helpers

**Spec:** `docs/superpowers/specs/2026-09-15-role-based-team-design.md`

## Global Constraints

- Role-based team is a development approach alongside Standard workflow.
- Role-based team is recommended and preselected during interactive job creation.
- The automatic roster must remain small and add situational roles only from explicit evidence.
- All active roles share the persisted Intent Brief and structured handoff contract.
- Legacy jobs without team metadata keep existing behavior.
- No new third-party dependency is introduced.

---

### Task 1: Team Selection Domain

**Files:**
- Create: `orchestrator/scripts/team_roles.py`
- Create: `tests/test_team_roles.py`

**Interfaces:**
- Produces: `available_role_ids() -> list[str]`, `assemble_team(text: str, job_type: str, requested_roles: list[str] | None = None) -> dict`, `refine_team(manifest: dict, plan: dict) -> dict`, `build_intent_brief(raw_input: str, plan: dict) -> dict`, `format_team_context(team: dict) -> str`, and `replace_team_roles(team: dict, role_ids: list[str]) -> dict`.

- [x] **Step 1: Write failing unit tests** for foundation roles, UI/database/security inference, deterministic capping, custom pinned roles, intent extraction, context formatting, and safe replacement.
- [x] **Step 2: Run `python3 -m unittest tests.test_team_roles -v`** and confirm import/test failures.
- [x] **Step 3: Implement the immutable role catalog and pure selection/formatting functions** with no filesystem or LLM dependencies.
- [x] **Step 4: Run `python3 -m unittest tests.test_team_roles -v`** and confirm all domain tests pass.

### Task 2: Job Creation and Agent Context

**Files:**
- Modify: `orchestrator/scripts/new_job.py`
- Modify: `orchestrator/scripts/run_builder.py`
- Modify: `orchestrator/scripts/review_ready.py`
- Modify: `tests/test_e2e_workflow.py`
- Create: `tests/test_team_prompt_context.py`

**Interfaces:**
- Consumes: Task 1 team functions.
- Produces: CLI flags `--development-approach {standard,role-based}` and `--team-roles`, persisted `development_approach` and `team` job fields, and shared team context in planner/builder/reviewer prompts.

- [x] **Step 1: Add failing CLI persistence and prompt-context tests** using mocked model and GitHub calls.
- [x] **Step 2: Run the focused tests** and confirm the missing flags and context assertions fail.
- [x] **Step 3: Parse the approach and optional custom roles, assemble before planning, refine after planning, build the Intent Brief, and persist the manifest.**
- [x] **Step 4: Append formatted team context to planner, builder, and reviewer prompts only for role-based jobs.**
- [x] **Step 5: Run the focused tests** and confirm role metadata and shared context survive the full flow.

### Task 3: Console Choice, Visibility, and Live Editing

**Files:**
- Modify: `orchestrator/scripts/dev_console.py`
- Modify: `tests/test_dev_console.py`

**Interfaces:**
- Consumes: Task 1 role catalog and `replace_team_roles`.
- Produces: recommended approach choice during job creation, compact job roster display, and `handle_change_team(job: dict) -> None`.

- [x] **Step 1: Add failing console tests** for the preselected approach, generated CLI flags, roster rendering, and persisted user-edited roles.
- [x] **Step 2: Run the focused console tests** and confirm they fail for the missing interaction.
- [x] **Step 3: Add the two-option approach prompt and an advanced custom-role selector.**
- [x] **Step 4: Render the approach and compact roster in the job header, then add a non-conflicting Change Team action for role-based jobs.**
- [x] **Step 5: Implement roster editing with the existing checkbox helper, preserving foundation roles and recording a handoff entry.**
- [x] **Step 6: Run focused console tests** and confirm interaction compatibility.

### Task 4: Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/user-guide.md`

**Interfaces:**
- Consumes: completed CLI and console behavior from Tasks 1–3.
- Produces: user-facing explanation of Standard versus Role-based team, benefits, cost, automatic staffing, and controls.

- [x] **Step 1: Document the new approach concisely** in the feature overview and job-creation guide.
- [x] **Step 2: Run `python3 -m compileall -q orchestrator tests`.**
- [x] **Step 3: Run `python3 -m unittest discover -s tests`.**
- [x] **Step 4: Review `git diff --check` and `git diff --stat`** to ensure the change is focused and clean.
