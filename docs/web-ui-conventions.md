# Web UI conventions

Short rules so new pages feel like the existing ones. They describe what the UI already does;
the checks that can be automated live in `tests/test_web_ui.py`.

## One primary action per screen

- A page has at most one filled (primary) button, and it is the thing most people come to do:
  **New feature** on Features, **Run all tests** on Tests, **Send current branch** on Delivery.
- A job's primary action is the hero button ("Run fix", "Mark complete", "Answer"). It is never
  repeated in the job's More menu, a tile, or a banner.
- Everything else goes in one **More** menu, in the page header or on the item it affects.
- Rows may carry one small button for that row's next step (Inbox, Ready to ship).

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
