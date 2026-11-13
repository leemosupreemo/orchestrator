# Web UI type and alignment system

Date: 2026-11-07. Branch: `web-ui` (the vanilla JS app in `orchestrator/web/static/`).

## Why

Titles, subtitles, labels and secondary text look different from page to page, and content inside a card does not
always line up. The App delivery page shows all of it: "Send to" is large and upper-case while other field labels are
not, the "Latest commit" label and the text around it are too close in size to read as levels, and help text that was
meant to be 13px renders at 15px.

The app already has tokens (`--space-*`, `--text-*`, `--control-*`), but they allow too much and nothing checks how
text is styled:

- 9 text sizes, six of them between 11 and 16px, so neighbouring levels look the same.
- 11 font weights (400 to 800).
- 76 inline `style="…"` attributes in the page scripts, 26 of them setting a text size.
- 15 utility class names used in markup that `style.css` never defines (about 60 uses), so they silently do nothing.
  Examples: `text-xs` (14 uses), `font-13`, `mt-4`, `gap-6`, `flex-wrap`, `pt-12`.
- Headings get their size from one-off rules per page (about 25 `… h2`/`… h3` rules).

## Goal

One small, named set of text styles and one alignment rule, used everywhere, with a test that stops drift.
The app should look like today's app, only tidy and consistent. **Not a visual refresh**: colours, layout, card style,
radius, shadows and control heights stay as they are.

### Non-goals
- No new colours, fonts, components or page layouts.
- The React app on `main` (`web/src`) is not part of this.
- No content or wording changes, except where a label is restyled.

### Failure, even if the code works
- A page looks noticeably different from today (a "redesign").
- Text gets smaller and harder to read on a phone.
- A rule exists only in the doc and nothing enforces it.

## Text styles

Six sizes and three weights. Every piece of text uses one of these roles.

| Role | Size | Weight | Colour | Used for |
|---|---|---|---|---|
| Display | 28 | bold | text | Sign-in and empty-state heroes only |
| Page title | 22 | bold | text | The page `h1` in the top bar |
| Section title | 18 | bold | text | Card titles (`.card-h h2`), page parts |
| Subsection title | 15 | bold | text | `h3` inside a card, hero titles, list row titles |
| Body | 15 | regular | text | Running text, inputs, buttons, notices |
| Field label | 15 | medium | text | The label above or beside a form field ("Send to", "Release notes") |
| Secondary | 13 | regular | muted | Page subtitle under the title, row meta, hints, explanations, timestamps |
| Tag | 11 | bold, upper-case, letter-spaced | muted | Small overline labels ("LATEST COMMIT"), pill and badge text |

Notes:
- The page subtitle moves from 14 to 13 muted. It sits under a 22px title, so it does not need to be larger.
- Code (`code`, `.mono`) uses the size of the text around it at `0.9em`. Relative sizes are allowed only for code.
- Terminal and log output stays monospace at the Secondary size (13).
- Phones use the same sizes as desktop. Phone-only rules may change layout and spacing, not text size.
- Field labels are never upper-case. The upper-case style belongs to Tag only.
- On touch screens inputs stay at 16px (the iPhone zoom rule already in `style.css`). That rule is the one
  exception to the scale.

Weights: regular 400, medium 550, bold 650. Bare `strong`/`b` render at bold 650.

### Tokens

Replace the size tokens with role tokens, and add weight tokens:

```
--type-display: 28px;   --type-page: 22px;   --type-section: 18px;
--type-body: 15px;      --type-secondary: 13px;   --type-tag: 11px;
--weight-regular: 400;  --weight-medium: 550;  --weight-bold: 650;
--leading-tight: 1.2;   --leading-body: 1.45;
```

Remove `--text-2xs` … `--text-3xl`. The existing check "every `var(--x)` must be defined" then finds every place still
using the old tokens, so none are missed.

Add role classes for markup built in JS: `.t-display`, `.t-page`, `.t-section`, `.t-subsection`, `.t-body`,
`.t-label`, `.t-secondary`, `.t-tag`. Existing semantic selectors (`.topbar h1`, `#page-sub`, `.card-h h2`,
`.field > span`, `.label`, `.meta`, `.hint-text`, `.pill`) keep their names but take their size and weight from the
role tokens.

### Mapping today's values

| Today | Becomes |
|---|---|
| 11px | Tag |
| 12px | Secondary 13. Upper-case badges and tags go to Tag 11 instead |
| 13px | Secondary |
| 14px | Body 15 when it is the main text of its container (notice body, hero description); otherwise Secondary 13 |
| 15px, 16px | Body (or Subsection title when bold and acting as a heading) |
| 18px | Section title |
| 20–22px | Page title |
| 28px and up | Display |
| weight 500–600 | medium 550 |
| weight 620–800 | bold 650 |

## Alignment and spacing

- **One left edge per card.** A card's title bar, its controls and its content all start at `--container-pad-x`.
  A nested box with its own border or tint (for example `.branch-meta-box`) spans the content width, and its text
  starts at the same inset as the controls next to it. Nested boxes do not add a second horizontal inset.
- **Controls in a title bar** (like the Branch picker) sit right on wide screens. When they wrap on a phone, they
  start at the title's left edge.
- **Label and control:** a field label sits directly above its control, `--space-2` apart, and both start on the same
  edge. Radio groups are one field: the label, then the options.
- **Vertical rhythm:** inside a card, sibling blocks are `--space-4` apart, grouped lines (a title and its secondary
  line) are `--space-1` apart, and cards on a page are `--space-4` apart.
- Spacing uses only the existing `--space-*` tokens.

## Cleanup

- Define the missing utility classes that match the spacing scale, or replace their uses with existing classes. Unify
  near-duplicates (`gap-6` vs `gap-8`) onto the scale. `cursor-pointer`, `text-danger` and `text-muted` map to
  existing styles.
- Move inline styles that set text size, weight, margin, padding or gap into classes. Inline styles that are truly
  per-element and dynamic (a progress bar width, a computed colour) may stay.
- Replace the per-page heading rules with the role tokens. A page keeps a one-off rule only for layout (margin,
  wrapping), never for size or weight.

## Enforcement

Extend `test_design_tokens_hold` in `tests/test_web_ui.py` so that, across `style.css` and every `static/*.js` file:

1. `font-size` uses only `var(--type-*)`, `inherit` or an `em` value inside a code rule, plus the one touch-input rule.
2. `font-weight` uses only `var(--weight-*)`.
3. No inline `style="…"` sets `font-size`, `font-weight`, `margin`, `padding` or `gap`. The overall inline-style
   ratchet number goes down to whatever remains.
4. Every utility-pattern class used in markup (`gap-*`, `mt-*`, `mb-*`, `pt-*`, `pb-*`, `ml-*`, `mr-*`, `text-*`,
   `font-*`, `t-*`) is defined in `style.css`.

Update the token table in `docs/web-ui-conventions.md` to the new roles, and the hero rules in that file that name
14px and 16px.

## Order of work

1. Tokens, role classes and the stronger test (the test fails at first; that is the to-do list).
2. Mechanical migration of `style.css` to the role tokens.
3. Inline styles and undefined classes, file by file.
4. A visual pass, page by page, starting with App delivery. Compare screenshots before and after at 390px and 1440px,
   light and dark, and fix anything that changed beyond the intended tidying.

## Verification

- The full web UI test suite passes, including the stronger token test.
- Before and after screenshots for every main route (Home, App delivery, Product, Packages, Connections, Tests,
  New job, Activity, Configuration pages, a job page, Git, Checkup) at 390px and 1440px.
- App delivery is checked against a project that has git branches and Firebase set up, because the demo project used in
  tests shows neither the branch picker's real contents nor the "Send to" form.

## Left to the builder

- Exact class names for the role classes, if the `t-` prefix clashes with something.
- Which existing one-off heading rules can be deleted outright and which still need a layout-only rule.
- Whether a near-duplicate utility class is defined or its uses are rewritten.
- How the screenshots are produced (the existing Playwright Chromium install works).
