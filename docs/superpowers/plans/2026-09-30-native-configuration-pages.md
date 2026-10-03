# Native Configuration Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat Configuration screen with a Configuration context menu and dedicated native pages for API Keys, Base Branch, Projects, Archived Jobs, Email Notifications, and Documentation.

**Architecture:** Add a focused browser helper that owns configuration metadata, route lookup, safe menu markup, and pure page rendering. Keep network requests and DOM event binding in `app.js`, reusing the existing structured `/api/config` endpoints. Disabled future entries stay visible as `Coming next` and have no terminal action.

**Tech Stack:** Vanilla JavaScript, HTML `<dialog>`, CSS, Python `ThreadingHTTPServer`, Python `unittest`, Node.js for pure browser-helper tests.

**Spec:** `docs/superpowers/specs/2026-09-30-native-configuration-pages-design.md`

## Global Constraints

- Clicking the primary Configuration control opens its context menu; there is no separate submenu-arrow control.
- Native configuration routes use `#/config/<section>` and remain bookmarkable and reload-safe.
- Phase one enables only API Keys, Base Branch, Projects, Archived Jobs, Email Notifications, and Documentation.
- Unimplemented configuration entries are disabled, visibly labeled `Coming next`, and never launch terminal actions.
- Secret values are never returned, redisplayed, placed in URLs, or written into page markup after submission.
- Mutations disable their initiating controls, preserve user input on failure, and refresh only the active configuration page after success.
- No `.orchestrator/project.json` changes are planned; `orchestrator check-config` is required only if implementation unexpectedly changes that file.
- Preserve all unrelated dirty-worktree changes. Because the target JavaScript, HTML, CSS, and test files already contain overlapping uncommitted user work, do not stage or commit implementation tasks unless the user first authorizes including those pre-existing changes.

## Review Focus

- Direct navigation to an unknown or disabled `#/config/<section>` must show the configuration chooser, not a blank page or terminal action; covered in Task 2 route tests.
- A delayed configuration response must not replace a different page after navigation; covered in Task 3 stale-page test.
- Repeated mutation clicks must issue one request while pending and restore controls after failure; covered in Task 3 mutation-guard tests and reused by Tasks 4-6.
- Secret-bearing API and email forms must clear only after success and never render saved values; covered in Tasks 3 and 5.
- Configuration menu keyboard behavior must support open, Escape close, outside-click close, and focus return; covered by the Task 2 menu-controller tests.

---

### Task 1: Configuration Registry and Safe Rendering

**Files:**
- Create: `orchestrator/web/static/configuration.js`
- Modify: `orchestrator/web/static/index.html`
- Test: `tests/test_web_ui.py`

**Interfaces:**
- Produces: `globalThis.ConfigurationPages.groups() -> Array<Group>`.
- Produces: `globalThis.ConfigurationPages.find(id: string) -> Entry | null`.
- Produces: `globalThis.ConfigurationPages.renderMenu() -> string`.
- `Entry` fields: `{id, label, description, route, enabled, status}`; disabled entries have `route: null`, `enabled: false`, and `status: "Coming next"`.

- [ ] **Step 1: Write failing Node-backed tests for the registry**

Add `ConfigurationPagesUiTests` in `tests/test_web_ui.py`. Load `configuration.js` with Node and assert literal phase-one entries:

- `api-keys -> #/config/api-keys`
- `base-branch -> #/config/base-branch`
- `projects -> #/projects`
- `archived-jobs -> #/config/archived-jobs`
- `email -> #/config/email`
- `documentation -> #/config/documentation`

Assert Models, AI Instructions, Fleet, Firebase, Xcode Cloud, Setup Wizard, Audit, Self-Tests, and Updates are disabled with `Coming next` and no route.

- [ ] **Step 2: Run the registry tests and verify RED**

Run: `python3 -m unittest tests.test_web_ui.ConfigurationPagesUiTests`

Expected: FAIL because `configuration.js` and `ConfigurationPages` do not exist.

- [ ] **Step 3: Implement the registry and menu renderer**

Create `configuration.js` as a browser/Node-compatible IIFE. Keep the registry private; return copies from `groups()`. Render enabled entries as buttons carrying `data-config-route`, and disabled entries as native disabled buttons with visible `Coming next` text. Escape all interpolated text and attributes internally.

- [ ] **Step 4: Load the helper before `app.js`**

Add `<script src="configuration.js"></script>` to `index.html` beside `project-picker.js`, before `app.js`.

- [ ] **Step 5: Run focused tests and syntax checks**

Run: `node --check orchestrator/web/static/configuration.js && python3 -m unittest tests.test_web_ui.ConfigurationPagesUiTests`

Expected: syntax exit 0 and all registry tests PASS.

- [ ] **Step 6: Record the Task 1 diff checkpoint without staging**

```bash
git status --short
git diff --check
```

### Task 2: Configuration Trigger, Context Menu, and Routes

**Files:**
- Modify: `orchestrator/web/static/configuration.js`
- Modify: `orchestrator/web/static/index.html`
- Modify: `orchestrator/web/static/app.js`
- Modify: `orchestrator/web/static/style.css`
- Test: `tests/test_web_ui.py`

**Interfaces:**
- Consumes: Task 1 registry and `renderMenu()`.
- Produces: `ConfigurationPages.resolve(section: string | undefined) -> Entry | null`, returning only enabled native configuration entries.
- Produces: sidebar trigger `#configuration-menu-trigger` with `aria-haspopup="menu"`, `aria-controls="configuration-context-menu"`, and synchronized `aria-expanded`.
- Produces: `ConfigurationPages.createMenuController({trigger, menu, document, navigate}) -> {open, close, destroy}`; `close({restoreFocus: true})` returns focus to the trigger.

- [ ] **Step 1: Write failing route and menu-state tests**

Extend `ConfigurationPagesUiTests` to assert `resolve("api-keys")` returns the enabled entry while `resolve("models")` and `resolve("unknown")` return `null`. Test `createMenuController` with minimal DOM fakes: `open()` sets `aria-expanded=true`; Escape and outside pointerdown close it; Escape restores trigger focus; an enabled `data-config-route` calls `navigate(route)` once; disabled controls never navigate. Add an HTTP static-index assertion that the single Configuration trigger has the three ARIA attributes and `config-submenus-btn` is absent.

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_web_ui.ConfigurationPagesUiTests tests.test_web_ui.AuthTests.test_static_index_served_with_csp`

Expected: FAIL on the missing resolver and old two-control navigation markup.

- [ ] **Step 3: Replace the two-part navigation control**

In `index.html`, replace the Configuration anchor plus arrow button with one button, `#configuration-menu-trigger`, styled as a secondary navigation item. It opens the context menu and does not change the hash itself.

- [ ] **Step 4: Implement context-menu lifecycle**

In `app.js`, replace `configSubmenusList()` and the configuration-specific branches of generic `openContextMenu()` with the Task 2 controller and registry-rendered menu. Supply `navigate(route)` as `location.hash = route`. Keep disabled buttons out of the navigation path.

- [ ] **Step 5: Route dedicated sections**

Change `resolveRoute()` so `#/config/<section>` resolves to `{page: "config", args: [section], nav: "config", query}`. `#/config`, disabled sections, and unknown sections resolve to `{page: "config", args: [], nav: "config", query}` for the chooser.

- [ ] **Step 6: Add focused responsive/accessibility styles**

Style the single trigger consistently with `.nav-secondary a`, add menu roles/focus states, and retain phone-width usability without restoring the removed arrow.

- [ ] **Step 7: Verify Task 2**

Run: `node --check orchestrator/web/static/configuration.js && node --check orchestrator/web/static/app.js && python3 -m unittest tests.test_web_ui.ConfigurationPagesUiTests tests.test_web_ui.AuthTests.test_static_index_served_with_csp`

Expected: all PASS.

- [ ] **Step 8: Record the Task 2 diff checkpoint without staging**

```bash
git status --short
git diff --check
```

### Task 3: Shared Native Page Shell and API Keys

**Files:**
- Modify: `orchestrator/web/static/configuration.js`
- Modify: `orchestrator/web/static/app.js`
- Modify: `orchestrator/web/static/style.css`
- Test: `tests/test_web_ui.py`

**Interfaces:**
- Produces: `ConfigurationPages.render(section: string | undefined, state: object) -> {title, sub, html}`.
- Produces: chooser rendering when `section` is missing or unavailable.
- Produces: `runConfigMutation(button: HTMLElement, request: {part, body}, successMessage: string) -> Promise<boolean>` in `app.js`; it guards duplicate submission and returns `true` only after a successful API mutation and active-page refresh.
- Produces: `ConfigurationPages.createMutationGuard(send: function) -> run(request: object) -> Promise<any | null>`; `run` returns `null` without calling `send` when a request is already pending and restores availability after resolve or reject.
- Produces: `ConfigurationPages.routeMatches(current: {page, args}, section: string | undefined) -> boolean` for stale-response protection.

- [ ] **Step 1: Write failing chooser and API-key render tests**

Using literal configuration fixtures in Node, assert:

- the chooser contains every group, enabled routes, disabled `Coming next` controls, and no `data-action="config_menu"`;
- API Keys renders provider labels and the exact states `Saved`, `From environment`, and `Not set`;
- API-key HTML contains no supplied secret fixture value;
- Ollama includes host help while other providers do not.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_web_ui.ConfigurationPagesUiTests`

Expected: FAIL because `ConfigurationPages.render` is missing.

- [ ] **Step 3: Implement the chooser and API-key renderer**

Implement `render(undefined, state)` and `render("api-keys", state)` in `configuration.js`. Use a common page header/status/card pattern and existing API-key identifiers from `GET /api/config`.

- [ ] **Step 4: Replace the flat `pages.config` loader**

In `app.js`, make `pages.config(args)` fetch `GET /api/config`, render only `args[0]`, and show the existing update-required message on `404`. Before applying async results, verify the current route still matches the requested section.

- [ ] **Step 5: Implement guarded API-key actions**

Retain password dialogs for set/replace and confirmation for clear, but bind them only on the API Keys page. Use `runConfigMutation` with `POST /api/config/keys`; preserve dialog fields on failure, clear secrets through dialog disposal on success, and refresh `#/config/api-keys` in place.

- [ ] **Step 6: Add stale-response and duplicate-submit tests**

Test `createMutationGuard` with a controllable promise: a second call returns `null` and invokes `send` once, then calls work again after both resolve and reject. Test `routeMatches` with matching and non-matching page/argument fixtures, and use it in `pages.config` before returning fetched content.

- [ ] **Step 7: Verify Task 3**

Run: `node --check orchestrator/web/static/configuration.js && node --check orchestrator/web/static/app.js && python3 -m unittest tests.test_web_ui.ConfigurationPagesUiTests tests.test_web_ui.ActionTests.test_config_settings_roundtrip_and_validation`

Expected: all PASS.

- [ ] **Step 8: Record the Task 3 diff checkpoint without staging**

```bash
git status --short
git diff --check
```

### Task 4: Base Branch and Archived Jobs Pages

**Files:**
- Modify: `orchestrator/web/static/configuration.js`
- Modify: `orchestrator/web/static/app.js`
- Test: `tests/test_web_ui.py`

**Interfaces:**
- Consumes: `ConfigurationPages.render` and `runConfigMutation` from Task 3.
- Produces: renderers for `base-branch` and `archived-jobs`.
- Produces: page-scoped submit/click handlers using `POST /api/config/base-branch` and `POST /api/config/archived-restore`.

- [ ] **Step 1: Write failing renderer tests**

Assert Base Branch renders only literal backend-supplied branches, selects `base_branch`, and escapes hostile branch text. Assert Archived Jobs renders identifier/title/status, gives valid entries Restore buttons, disables corrupt entries, and shows `No archived jobs` for an empty list.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_web_ui.ConfigurationPagesUiTests`

Expected: FAIL because both renderers are missing.

- [ ] **Step 3: Implement both renderers and scoped handlers**

Use one focused card per page. Base Branch submits `{branch}`. Archived restore submits `{id}` and disables only the selected row while pending. Successful operations refresh the current page.

- [ ] **Step 4: Add backend boundary tests**

Extend the existing configuration round-trip test to assert an unknown branch is rejected, path-like archive identifiers are rejected, and a valid archived file moves back to the active jobs directory.

- [ ] **Step 5: Verify Task 4**

Run: `python3 -m unittest tests.test_web_ui.ConfigurationPagesUiTests tests.test_web_ui.ActionTests.test_config_settings_roundtrip_and_validation`

Expected: all PASS.

- [ ] **Step 6: Record the Task 4 diff checkpoint without staging**

```bash
git status --short
git diff --check
```

### Task 5: Email Notifications Page

**Files:**
- Modify: `orchestrator/web/static/configuration.js`
- Modify: `orchestrator/web/static/app.js`
- Test: `tests/test_web_ui.py`

**Interfaces:**
- Consumes: Task 3 mutation helper.
- Produces: `email` renderer and page-scoped actions for recipient add/remove, provider configuration, and existing `test_email` run action.

- [ ] **Step 1: Write failing email renderer tests**

Assert Gmail and Resend fixtures produce plain-language sender status, recipients are escaped, test email is unavailable without recipients, saved-secret indicators appear without secret values, and an empty recipient list shows `No recipients yet`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_web_ui.ConfigurationPagesUiTests`

Expected: FAIL because the email renderer is missing.

- [ ] **Step 3: Implement native email rendering and actions**

Reuse the existing add-recipient and sender dialogs, scoped to `#/config/email`. Use `POST /api/config/email` for `add`, `remove`, and `provider`; retain blank-secret-means-keep behavior; expose Send Test only when recipients exist.

- [ ] **Step 4: Add validation and secret-retention tests**

Extend backend tests for malformed email rejection, duplicate recipient idempotence, unknown provider rejection, blank secret retention, and absence of saved secret contents in `config_state()`.

- [ ] **Step 5: Verify Task 5**

Run: `python3 -m unittest tests.test_web_ui.ConfigurationPagesUiTests tests.test_web_ui.ActionTests.test_config_settings_roundtrip_and_validation`

Expected: all PASS.

- [ ] **Step 6: Record the Task 5 diff checkpoint without staging**

```bash
git status --short
git diff --check
```

### Task 6: Documentation Page and Terminal-Handoff Removal

**Files:**
- Modify: `orchestrator/web/static/configuration.js`
- Modify: `orchestrator/web/static/app.js`
- Modify: `orchestrator/web/static/style.css`
- Test: `tests/test_web_ui.py`

**Interfaces:**
- Consumes: Task 1 registry and Task 3 page shell.
- Produces: `documentation` renderer and document-read handler using `GET /api/config/doc?id=<encoded-id>`.
- Produces: phase-one Configuration UI with no `config_menu`, `openMenu`, scroll chips, or flat section markup.

- [ ] **Step 1: Write failing documentation and no-terminal tests**

Assert documentation fixtures are grouped into `Orchestrator docs` and `Project docs`, names are escaped, empty groups are omitted, and each Read control carries only the opaque document id. Assert rendered chooser/pages contain no `data-action="config_menu"`, `data-scroll-to`, or `Open Full CLI Menu`.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_web_ui.ConfigurationPagesUiTests`

Expected: FAIL because the documentation renderer and cleanup are incomplete.

- [ ] **Step 3: Implement documentation rendering and reading**

Render the two groups as accessible lists. Bind Read to the existing allowlisted document endpoint and show escaped content in the existing dialog with `Close` as the primary action.

- [ ] **Step 4: Remove obsolete flat configuration code**

Delete the old `cfgItem`, `cfgSection`, submenu chips, configuration terminal-button builders, old configuration scroll handling, and configuration-specific terminal menu entries from the Web UI. Do not remove backend `config_menu` compatibility code in this phase.

- [ ] **Step 5: Finish responsive and focus styling**

Add page-shell, status-summary, chooser-grid, disabled-entry, and narrow-screen styles. Ensure context-menu focus indicators and native disabled appearance remain visible in light and dark themes.

- [ ] **Step 6: Add document confinement regression coverage**

Verify unknown and path-like document ids return an error and allowed ids return only configured document content.

- [ ] **Step 7: Run complete Web UI verification**

Run:

```bash
node --check orchestrator/web/static/configuration.js
node --check orchestrator/web/static/project-picker.js
node --check orchestrator/web/static/app.js
python3 -m unittest tests.test_web_ui
git diff --check
```

Expected: all commands exit 0 and the Web UI module reports 0 failures.

- [ ] **Step 8: Record the Task 6 diff checkpoint without staging**

```bash
git status --short
git diff --check
```

### Task 7: Repository Verification and Review

**Files:**
- Verify only; modify scoped files only if a failure is caused by this implementation.

**Interfaces:**
- Consumes: all prior task outputs.
- Produces: a verified phase-one native configuration experience ready for handoff.

- [ ] **Step 1: Run canonical package validation**

Run: `python3 -m compileall -q orchestrator tests`

Expected: exit 0.

- [ ] **Step 2: Run the canonical full suite**

Run: `python3 -m unittest discover -s tests`

Expected: report the exact result. The worktree baseline currently has two unrelated failures—`test_malformed_verifier_output_is_ignored` and `test_visual_check_auto_resolves_bundle_id`; confirm no additional failures are introduced rather than claiming a fully green suite.

- [ ] **Step 3: Run configuration and JavaScript checks**

Run:

```bash
node --check orchestrator/web/static/configuration.js
node --check orchestrator/web/static/app.js
python3 -m unittest tests.test_web_ui.ConfigurationPagesUiTests
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 4: Check configuration-file scope**

Run: `git diff --name-only -- .orchestrator/project.json`

Expected: no output. If it changed, run `orchestrator check-config` and report why the unplanned change was necessary.

- [ ] **Step 5: Request a read-only whole-change review**

Review against the design spec with emphasis on native-only navigation, disabled future entries, keyboard/focus behavior, stale async responses, duplicate submissions, secret handling, and preservation of unrelated worktree changes. Fix Critical and Important findings, then rerun Steps 1-4.

- [ ] **Step 6: Record review-fix status without staging**

```bash
git status --short
git diff --check
```
