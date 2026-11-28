# Finish Remaining Session Items Implementation Plan

**Goal:** Implement the four remaining approved features without unrelated changes.
**Spec:** docs/superpowers/specs/2026-11-09-finish-session-items.md
**Execution:** Parallel independent backend tasks, parent-owned web integration, then combined review and canonical full validation.

- [x] Shared requirements: tests for legacy migration, branch switching, worktree sharing, history and all context readers; implement canonical shared source and preserve tracked legacy copies.
- [x] Package updates: validate actual manifest entries, plan fixed argv for native managers, run update then configured checks, record errors and support AI repair handoff. Test success/failure/unsupported manager/argument injection.
- [x] AI prerequisites: user-local Node LTS bootstrap where npm is missing; verify downloads, report check/prerequisites/install/sign_in/ready progress, test failures and model readiness. No sudo or automatic license consent.
- [x] Parent integration: authenticated owner-only package actions, guided Connect dialog with progress and interactive controls, Product picker only in AI steps, shared requirements consumers in server. Test UI behavior and access controls.
- [x] Inspect Sentry/password-reset configuration without sending messages; document any external prerequisites.
- [x] Focused tests, syntax/compilation checks, combined code review, full suite; commit/push/publish under the existing session authorization and verify served assets (release verification recorded below).

Validation: 2,563 full-suite tests passed (six skipped), 122 focused tests passed, Python compilation and JavaScript syntax checks passed. Browser smoke covered guided sign-in and mobile layout. A real official Node LTS bootstrap in a temporary HOME successfully ran node, npm, and npx. Combined code review fixes include rejecting modern Yarn workspace updates and disabling user curl configuration for verified downloads.

External prerequisites: Sentry OAuth client ID is absent. No real password-reset email was sent because no explicit test recipient/send request was supplied. Connected computers need the updated runner for backend features.
