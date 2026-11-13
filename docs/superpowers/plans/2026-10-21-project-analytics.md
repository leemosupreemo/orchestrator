# Project Analytics and KPI Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make optional success measures available to all project jobs, make feature KPIs visible during feature job creation, and promote the existing Measure view into a project Analytics page.

**Architecture:** Extend the existing PRD section model and prompt context, reuse feature-owned KPI records and provider settings, and enrich the existing analytics overview/page. Feature job instructions will require event test evidence in the job's plan and completion summary without claiming test success equals product success.

**Tech Stack:** Python 3.11 standard library, existing Python web server, vanilla JavaScript and CSS.

**Spec:** `docs/superpowers/specs/2026-10-21-project-analytics-design.md`

## Global Constraints

- A project may have no KPIs or analytics provider configured; these remain optional.
- The first version uses results entered by the project owner and does not query analytics vendors for product usage data.
- Keep feature KPI definitions and results in the existing feature store; do not create a second KPI store.
- The optional Success measures PRD section records project-level outcomes and targets in the person's own words.
- Keep credentials private and expose only provider identity, region, whether a key is configured, and public dashboard links.
- Do not treat build status, test pass status, or provider connection tests as evidence that a product outcome was achieved.

## Review Focus

- Old PRDs without Success measures must remain readable and gain an empty optional section when normalized.
- Draft/import/automatic update prompts must preserve the user's Success measures without inventing metrics.
- A project with no PRD or analytics provider must still allow job creation and manual KPI work.
- A KPI with no result must remain distinct from a measured zero.
- Provider secrets must never be returned in the analytics overview.

---

### Task 1: Add optional PRD Success measures

**Files:**
- Modify: `orchestrator/prd.py`
- Modify: `orchestrator/new_project.py`
- Modify: `docs/product-requirements.md`

**Interfaces:**
- Preserve the existing `SECTIONS`, `split`, `normalise`, `context_block`, and prompt APIs; add section id `success` with title `Success measures`.

- [x] Add the optional section with clear guidance for project-level outcomes and targets.
- [x] Update audience length limits and all fixed-section prompt wording and examples to include Success measures.
- [x] Ensure import, draft, update, and unsafe-update behavior retains filled Success measures while keeping the section optional.
- [x] Include the optional field in new-project onboarding and update the user-facing PRD documentation.
- [x] Review every `SECTIONS` consumer and run `python3 -m compileall -q orchestrator`.

### Task 2: Make KPI expectations visible in feature jobs

**Files:**
- Modify: `orchestrator/analytics.py`
- Modify: `orchestrator/web/server.py`
- Modify: `orchestrator/web/static/app.js`
- Modify: `orchestrator/prompts/planner_feature.md`
- Modify: `orchestrator/prompts/builder_feature_task.md`
- Modify: `orchestrator/prompts/reviewer.md`

**Interfaces:**
- Continue using `analytics.instrumentation_context(feature, provider_name)` as the planner/job measurement block.
- Continue using the existing `features` and `analytics` API responses; do not add a new store.

- [x] For a selected feature with no KPI, show a non-blocking prompt on New job with a direct link to add or suggest a KPI on Analytics.
- [x] On Analytics, focus the selected feature when the link includes its id; retain normal page behavior without an id.
- [x] Strengthen the measurement context so plans name event tests and the job's completion summary reports the event, test, and outcome.
- [x] Tell builders and reviewers to verify event instrumentation and its test evidence while keeping outcome claims separate from code/test evidence.
- [x] Run `python3 -m compileall -q orchestrator`.

### Task 3: Promote Measure to project Analytics

**Files:**
- Modify: `orchestrator/web/static/index.html`
- Modify: `orchestrator/web/static/app.js`
- Modify: `docs/web-ui-conventions.md`
- Modify: `docs/web-ui-principles-checklist.md`
- Modify: `docs/user-guide.md`

**Interfaces:**
- Reuse `GET /api/analytics`, the existing feature rollup, provider metadata and KPI operations.
- Keep `#/measure` as the stable route while presenting the navigation and page title as Analytics.

- [x] Present project-level totals for tracked KPIs and measured KPIs, provider connection/region and dashboard link, grouped KPI results, and an optional-setup empty state.
- [x] Preserve manual result entry, learning history, add/edit/delete/undo, provider setup, and tracking-plan export.
- [x] Update first-party page labels, help text, and documentation from Measure to Analytics where they are user-facing.
- [x] Keep the UI usable when the provider is disconnected or has no key.
- [x] Run `python3 -m compileall -q orchestrator`, `node --check orchestrator/web/static/app.js`, and `git diff --check`.

### Task 4: Final consistency pass

**Files:**
- Review all files changed in Tasks 1–3.

- [x] Confirm all success-measure and KPI claims match what the system actually stores or verifies.
- [x] Confirm existing uncommitted user edits remain intact and only intended files changed for this feature.
- [x] Run `python3 -m compileall -q orchestrator`, `node --check orchestrator/web/static/app.js`, and `git diff --check`.
