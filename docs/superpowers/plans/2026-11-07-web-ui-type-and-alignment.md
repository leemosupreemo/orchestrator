# Web UI Type and Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the web UI's loose text sizes and weights with one enforced set of text roles and one card alignment rule, keeping today's look.

**Architecture:** Role tokens in `:root` of `style.css` replace `--text-*`; existing semantic selectors read them; markup built in JS uses role and utility classes instead of inline styles. A stricter `test_design_tokens_hold` keeps it that way.

**Tech Stack:** Vanilla CSS and JS in `orchestrator/web/static/`, Python `unittest` in `tests/test_web_ui.py`, Playwright Chromium for screenshots.

**Spec:** `docs/superpowers/specs/2026-11-07-web-ui-type-and-alignment-design.md`

## Global Constraints

- Sizes: `--type-display: 28px; --type-page: 22px; --type-section: 18px; --type-body: 15px; --type-secondary: 13px; --type-tag: 11px;`
- Weights: `--weight-regular: 400; --weight-medium: 550; --weight-bold: 650;` Leading: `--leading-tight: 1.2; --leading-body: 1.45;`
- Mapping: 11→tag; 12→secondary (upper-case badges/tags→tag); 13→secondary; 14→body if main text of its container, else secondary; 15/16→body; 18→section; 20–22→page; 28+→display. Weight 500–600→medium; 620–800→bold.
- Field labels: body size, medium, never upper-case. Upper-case only on Tag.
- Code: `0.9em` of surrounding text. Terminal/log: mono at secondary. Phones: same sizes; only the touch-input 16px rule is exempt.
- No colour, layout, radius, shadow or control-height changes. No commits unless the user asks.

## Review Focus

- Text that was 12px in dense lists (pills, row meta) grows to 13 and may wrap or truncate on a 360px phone: check list rows and pills at 360px.
- The page subtitle shrinks 14→13: check it is still readable in dark mode.
- Removing `--text-*` breaks any token used from JS (`style="font-size: var(--text-xs)"`): the defined-var check covers `app.js` and `configuration.js` only, so extend it to every `static/*.js`.
- Defining previously undefined utility classes (`mt-4`, `gap-6`) changes spacing where they were silently ignored: check those spots in screenshots.
- Card alignment: a title-bar control that wraps on phones must start at the title's left edge (App delivery Branch picker).

---

### Task 1: Tokens, role classes and the enforcing test

**Files:**
- Modify: `orchestrator/web/static/style.css` (`:root` tokens, base `body`/`strong` rules, new role classes)
- Test: `tests/test_web_ui.py` (`test_design_tokens_hold`)

**Interfaces:**
- Produces: tokens above; classes `.t-display .t-page .t-section .t-subsection .t-body .t-label .t-secondary .t-tag`; utility classes on the spacing scale `gap-{4,8,12,16}`, `mt-{4,8,12,16}`, `mb-{4,8,12,16}`, `pt-{8,12}`, `ml-8`, `flex-wrap`, `cursor-pointer`, `text-muted`, `text-danger`.

- [ ] **Step 1:** Extend `test_design_tokens_hold` to read every `static/*.js`, and assert:
  - every `font-size:` in CSS is `var(--type-*)`, `inherit`, an `em` value, or the touch-input `16px !important` rule;
  - every `font-weight:` in CSS is `var(--weight-*)`;
  - no `style="…"` in JS contains `font-size`, `font-weight`, `margin`, `padding` or `gap`;
  - every class in JS markup matching `^(gap|mt|mb|pt|pb|ml|mr|text|font|t)-` or in `{flex-wrap, cursor-pointer}` has a `.class` rule in `style.css`;
  - inline-style ratchet lowered to the count after Task 3.
- [ ] **Step 2:** Run `python3 -m pytest -q tests/test_web_ui.py -k design_tokens` → FAIL (the list of violations is the to-do list).
- [ ] **Step 3:** Add the tokens, role classes and utilities; delete `--text-*`.

### Task 2: Migrate `style.css`

- [ ] **Step 1:** Replace every `--text-*`, raw px/rem font size and raw weight with role tokens per the mapping. Semantic selectors: `.topbar h1`→page; `.sub`/`#page-sub`→secondary; `.card-h h2`→section; `.card-h h2 .count`→secondary; `.field > span`/`legend`→label; `.label`, `.branch-meta-tag`, `.more-menu-header`→tag; `.meta`, `.hint`, `.hint-text`, `.field small`→secondary; `.pill`→tag only if upper-case, else secondary.
- [ ] **Step 2:** Fix the "Send to" clash: `.field > span.label` renders as a field label (not upper-case).
- [ ] **Step 3:** Card alignment: `.branch-meta-box` content starts at the card's content edge; the title-bar control row starts at the title edge when wrapped.
- [ ] **Step 4:** Run the CSS part of the test → PASS for CSS assertions.

### Task 3: Inline styles and undefined classes in JS

- [ ] **Step 1:** In every `static/*.js`, replace inline styles that set size, weight, margin, padding or gap with classes; replace `font-13` with `t-secondary`, `text-xs` with `t-secondary` (or `t-tag` when upper-case).
- [ ] **Step 2:** Run `python3 -m pytest -q tests/test_web_ui.py` → all PASS, with the ratchet set to the new count.

### Task 4: Visual pass and docs

- [ ] **Step 1:** Screenshot every main route at 390 and 1440, light and dark, before (git HEAD copy of `static/`) and after; compare and fix unintended changes. App delivery is also checked with a real git project and Firebase configured.
- [ ] **Step 2:** Update the token table and hero rules in `docs/web-ui-conventions.md`.
- [ ] **Step 3:** Full suite: `python3 -m pytest -q tests/` → only the 3 known pre-existing failures.
