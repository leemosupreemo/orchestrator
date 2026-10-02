# Web UI conventions

Short rules so new pages feel like the existing ones. They describe what the UI already does;
the checks that can be automated live in `tests/test_web_ui.py`.

## One primary action per screen

- A page has at most one filled (primary) button, and it is the thing most people come to do:
  **New feature** on Features, **Run all tests** on Tests, **Send current branch** on Delivery.
- A job's primary action is the hero button ("Run fix", "Mark complete", "Answer"). It is never
  repeated in the job's More menu, a tile, or a banner.
- Everything else goes in one **More** menu, in the page header or on the item it affects.
- Rows may carry one small button for that row's next step (Waiting on you, Ready to ship).

## Where actions live

- Page-level actions: top right of the page header.
- Item-level actions: on the item (card header or row), not in a separate toolbar.
- Destructive actions are last in a menu, styled as dangerous. Prefer **Undo** (10 seconds) over a
  confirmation dialog when the action can be reversed; keep the confirmation when it can't
  (Discard job deletes a branch).

## Words

- **Job**: one piece of work with a plan, a branch and a status. **Run**: one command that was
  executed. **Task**: one step inside a job's plan. A job has tasks and runs; a run is never "a job".
- **Feature**: something a user would name. Jobs belong to features.
- Status labels come from the server's job state (`state.label`, `state.reason`, `state.next`).
  Pages never invent their own status words.
- Plain names first, the older technical term in the description ("Machines", "fleet").

## Colour and status

- Status colours are tokens: `ok` (done, on track), `warn` (needs you), `bad` (failed, behind),
  accent (in progress). Text on a coloured background uses the matching `-ink` token, never `#fff`.
- Amber means "needs you" only. Neutral information uses the muted colour.

## Every page

- One `h1` (the header). It takes focus on navigation so screen readers announce the page.
- Interactive targets are at least about 32px tall (44px on phones for primary controls).
- It works at 390px: nothing overflows, and anything reachable on desktop is reachable from the
  bottom bar or the More sheet.
- Errors show what happened and what to do next (`errors.js`).
- It is reachable from the command palette (`Cmd/Ctrl+K`): add it to `PALETTE_PAGES`.

## Design tokens: spacing, type and radius

`style.css` defines three scales next to the colour tokens. Use them; do not write raw `px` for padding, margin, gap, font-size or border-radius.

| Scale | Tokens |
|---|---|
| Spacing | `--space-0` 2, `--space-1` 4, `--space-2` 8, `--space-3` 12, `--space-4` 16, `--space-5` 20, `--space-6` 24, `--space-7` 32, `--space-8` 40 |
| Type | `--text-2xs` 11, `--text-xs` 12, `--text-sm` 13, `--text-md` 14, `--text-base` 15, `--text-lg` 16, `--text-xl` 18, `--text-2xl` 22, `--text-3xl` 28 |
| Radius | `--r-xs` 4, `--r-sm` 6, `--r-md` 8, `--r-lg` 12 (cards; `--radius`), `--r-pill` |

Rules checked on every page at 360, 430 and 1440 px, light and dark:
- No horizontal scroll. A control that sizes itself to its content (a select, a long path) needs `max-width` and its parents `min-width: 0`.
- Stand-alone controls and links are at least 28 px tall on desktop and 36 px on phones; inline links inside a sentence are exempt.
- Long model-written text (an architect's concerns, a failure reason) is shown with `clamped()` ("Show more"); rows clamp it to two lines. The same text is not shown twice on one page.
- A list that can grow past about five items is capped with "Show all N".
