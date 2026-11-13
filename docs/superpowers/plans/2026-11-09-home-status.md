# Home Status Implementation Plan

> Implement inline using executing-plans and test-driven-development.

**Goal:** Deliver the approved Home status container and three prefilled job suggestions.
**Spec:** docs/superpowers/specs/2026-11-09-home-status-design.md
**Architecture:** Python owns validated, cached suggestions. JS renders existing source-backed facts and suggestion links; Home requests generation asynchronously through authenticated endpoints.
**Tech stack:** Python standard library, existing BackgroundTasks and model-call boundary, browser JavaScript and CSS.

## Constraints and review focus

- Owner-only generation; GET never invokes a model.
- Requirements/job revisions invalidate suggestions; unchanged page reloads do not repeat model calls.
- Read-only model work, bounded response fields, escaped HTML, no automatic job creation.
- Empty/non-Git projects, model failures, old backends and mobile layouts remain usable.
- Captured project root prevents a background result from being saved to a newly selected project.

## Tasks

1. Add tests for fingerprint stability/invalidation, validation, persistence and fallback, then implement `orchestrator/home_status.py`.
2. Test authenticated endpoints and owner-only deduplicated background generation, then wire them in `server.py`.
3. Test rendered facts, escaping, three prefilled New job URLs and fallback, then add `home-status.js`, index script ordering and responsive CSS. Integrate at the top of Home and remove the previous dirty-files line.
4. Run focused tests, compile/syntax checks, full suite and a final independent review. Commit, push and publish under the session's existing authorization; verify served assets.

## Validation

`python3 -m unittest tests.test_home_status`
`python3 -m compileall -q orchestrator tests`
`node --check orchestrator/web/static/home-status.js`
`node --check orchestrator/web/static/app.js`
`python3 -m unittest discover -s tests`
