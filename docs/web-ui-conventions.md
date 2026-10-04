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

## Cards

- The header is a title bar (tinted, `--text-lg` heading): the title on the left, a count may sit beside it, actions on
  the right.
- At most two visible actions, as small buttons, the secondary one first and the main one last; anything else goes in a
  More menu.
- Labels are one verb when the card names the object: **Edit**, **Raw**, **Add**, **Attach…**, **Run**. Name the object
  only when it would be unclear next to the title ("Edit" on the Brief card, not "Edit brief"). The button's tooltip
  can say it in full.
- Facts about the content (where a file lives, when it changed) go in a `.card-caption` strip under the header, never
  loose in the header.
- In a folding card the header is the toggle, so its actions go at the bottom inside the fold.
- Long content shows up to a height and then **Show more**, centred under it, grows the card to fit
  (`data-expandable="<px>"`, wired by `wireExpandables`). No inner scroll boxes; diffs, logs and terminals are the
  exception.

- Group by what things are to the person, not where they came from. A job's context is its **ticket** (a chip in the
  header), its **designs** and **what went wrong**, whatever app or upload each came from. Empty groups don't show. A
  title joined with "&" usually means two groups.
- One way to attach: **Attach…** asks what it is, then where it comes from (a connected app, a link, a file, pasted
  text). Attaching never starts a run.

- A decision shows what it decides. While a plan waits for approval (or to start), it sits right under the job's
  hero as "The plan to approve": summary, assumptions and risks, and each task with what "done" means and the files it
  touches, editable in place, with **Revise…** beside it. Once approved it folds back to Tasks.

- A More menu groups its items under headings (on a job: This job, Share, Open, Settings), one line each with the
  description in the tooltip (`moreMenu(items, { compact: true })`), only what fits the current stage, and ending
  actions (close, archive, delete) last below a divider. It fits without scrolling; on phones it opens as a sheet over
  a dimmed page.

## Navigation and fields

- A page one level down (a job, a run, a document, a setting, a form) shows **Back** above its title. It returns to
  the previous page in the app, or to the page's parent when opened directly (`routeParent` in `app.js`). Pages don't
  add their own back links.
- A field's label sits above its control. Paired fields (Home's project and branch) are the same size and sit side by
  side, with `--space-3` between them.

## Words

- **Job**: one piece of work with a plan, a branch and a status. **Run**: one command that was
  executed. **Task**: one step inside a job's plan. A job has tasks and runs; a run is never "a job".
- **Feature**: something a user would name. Jobs belong to features.
- Plain words a casual user knows: "Needs you", "Planning", "Split into jobs", never "execution", "decomposed" or
  upper-case type ids. Sentence case for every label and title. Counts are words: `plural(n, "job")`, never "job(s)".
- Ask before something can't be undone with `formDialog`, and name the action on its button ("Remove machine"), never
  `confirm()`. `test_copy_stays_plain_and_consistent` checks these.
- Status labels come from the server's job state (`state.label`, `state.reason`, `state.next`).
  Pages never invent their own status words.
- Plain names first, the older technical term in the description ("Machines", "fleet").

## Colour and status

- Status colours are tokens: `ok` (done, on track), `warn` (needs you), `bad` (failed, behind),
  accent (in progress). Text on a coloured background uses the matching `-ink` token, never `#fff`.
- Amber means "needs you" only. Neutral information uses the muted colour.

## Every page

- A change to the interface is checked by the UX and design review (`orchestrator/prompts/ux_reviewer.md`) after the
  code review. Its checklist ids (`ux.one-primary`, `design.overlap`, …) are the vocabulary for UI findings.

- One `h1` (the header). It takes focus on navigation so screen readers announce the page.
- Interactive targets are at least about 32px tall (44px on phones for primary controls).
- It works at 390px: nothing overflows, and anything reachable on desktop is reachable from the
  phone tab bar (Home, Product, New job, Activity, Projects) or the drawer behind the menu button, which holds
  everything else. New job is the tab bar's centre button on phones and Home's header button on wider screens; there
  is no floating button.
- Errors show what happened and what to do next (`errors.js`).
- It is reachable from the command palette (`Cmd/Ctrl+K`): add it to `PALETTE_PAGES`.

## Design tokens: spacing, type and radius

`style.css` defines three scales next to the colour tokens. Use them; do not write raw `px` for padding, margin, gap, font-size or border-radius.

| Scale | Tokens |
|---|---|
| Spacing | `--space-0` 2, `--space-1` 4, `--space-2` 8, `--space-3` 12, `--space-4` 16, `--space-5` 20, `--space-6` 24, `--space-7` 32, `--space-8` 40 |
| Type | `--text-2xs` 11, `--text-xs` 12, `--text-sm` 13, `--text-md` 14, `--text-base` 15, `--text-lg` 16, `--text-xl` 18, `--text-2xl` 22, `--text-3xl` 28 |
| Radius | `--r-xs` 4, `--r-sm` 6, `--r-md` 8, `--r-lg` 12 (cards; `--radius`), `--r-pill` |
| Control height | `--control-xs` 28 (inline), `--control-sm` 32 (small buttons), `--control-md` 38 (default), `--control-lg` 44 (touch), `--control-xl` 48 |
| Shadow | `--shadow` (cards), `--shadow-2` (menus, popovers), `--shadow-3` (dialogs, drawers) |
| Layer | `--z-raised`, `--z-inner`, `--z-sticky`, `--z-float`, `--z-header`, `--z-palette`, `--z-drawer`, `--z-menu`, `--z-toast`; a backdrop is its layer minus one |

Every `var(--x)` must be defined; `test_design_tokens_hold` checks this, the layers, the control heights and the shadows,
and ratchets the number of inline styles in `app.js` down.

Rules checked on every page at 360, 430 and 1440 px, light and dark:
- No horizontal scroll. A control that sizes itself to its content (a select, a long path) needs `max-width` and its parents `min-width: 0`.
- Stand-alone controls and links are at least 28 px tall on desktop and 36 px on phones; inline links inside a sentence are exempt.
- Long model-written text (an architect's concerns, a failure reason) is shown with `clamped()` ("Show more"); rows clamp it to two lines. The same text is not shown twice on one page.
- A list that can grow past about five items is capped with "Show all N".

## Containers, heroes and callouts

Callout and hero containers (`.job-hero`, `.banner`, `.notice`) frame actionable status, warnings and next steps:
- **Tone pairing**: Tinted containers pair tone borders with matching soft backgrounds (`--warn` + `--warn-soft`, `--accent` + `--accent-soft`, `--ok` + `--ok-soft`, `--bad` + `--bad-soft`). Text uses `--text` or high-contrast ink tokens.
- **Visual hierarchy**:
  - The hero title (`.job-hero-title`) is an `h2` styled with `--text-lg` (16px), `font-weight: 650`, and line-height 1.3. It never uses a tiny pill badge for the container's title.
  - The description (`.job-hero-reason` or banner body) uses `--text-md` (14px). Spacing between title and description is `--space-1` (4px) to keep them grouped.
- **Button sizing & proportions**:
  - Hero action buttons use standard `.btn.primary` (`min-height: 38px`, padding `--space-2` `--space-4`), not bloated 48px `.big` buttons. On phones (`<= 760px`), primary actions stretch to full width with `min-height: 44px`.
  - Banners use `.btn.small` (`min-height: 32px`).
- **Container geometry**:
  - Border radius uses `--radius` (`--r-lg`, 12px).
  - Padding uses `--space-4` `--space-5` on desktop for heroes, `--space-4` on phones, and `--space-3` `--space-4` for banners/notices.
- **Job lifecycle stepper & next step clarity**:
  - Every job detail page presents a 4-phase lifecycle stepper (`.job-stepper`): `1. Plan` → `2. Build` → `3. Verify` → `4. Review`.
  - Active steps use `var(--accent)` (or `var(--warn)`/`var(--bad)` when attention/fix is needed), completed steps use `var(--ok)` with `✓`, upcoming steps are muted.
  - The hero card clarifies **who has the ball** (`.job-hero-badge-row`: e.g. "Your action needed" vs "AI Worker active") and previews the exact automated builder task that will run upon taking the primary action (`.job-hero-next-preview`).
  - The **Progress** card clearly separates task completion count, active/next builder task, remaining tasks, and verification status.
- **Job Brief**:
  - Positioned near the top of the Job Detail page immediately following Progress (before Test Cases and technical changes).
  - Displays the canonical relative file location (`.brief-path-chip`: `.orchestrator/output/<job_id>/brief.md`) with click-to-select and a direct link to the raw file viewer (`Raw file ↗`).
  - Directly editable via "Edit brief", which opens an in-page modal dialog with full markdown editing and saves to the canonical `.orchestrator/output/<job_id>/brief.md` file.

## Home

- One list of jobs. A job that needs you is a job in an earlier status: it sorts first and its status says what it needs ("Approve plan"). The whole row is one link to the job, where the next step is the primary action; rows carry no buttons. There is no second "waiting" list; "Needs you" is a filter.
- Actions belong to the page for what they act on (Tests: run, build, coverage; Delivery: send a build; Device logs: pull; Check-up: environment checks and the setup wizard). Home has no menu of them.
- Things that are not jobs go where they belong: the Product card (what the product is, and a notice when the AI updated it), other projects' waiting jobs as one line under the list.

## Sign-in

- Provider sign-in uses the pop-up. A blocked pop-up is explained ("Allow pop-ups for this site"), never silently turned into a full-page redirect: that flow fails with "Unable to process request due to missing initial state" in browsers that partition storage between the site (`swift-orch-web-20260923.web.app`) and Firebase's auth domain (`swift-orch-web-20260923.firebaseapp.com`). Full-page sign-in is offered as an opt-in link, with a warning.
- To make redirects work everywhere, set `authDomain` in `app.js` to the site's own domain, after adding `https://<site>/__/auth/handler` as an authorized redirect URI in Google Cloud (Credentials > the Web client), as the callback URL of the GitHub OAuth app, and as an Apple Services ID return URL. Setting it first makes Google answer `redirect_uri_mismatch` (checked 2026-10-02).

## Product requirements

- One document, five sections, shown in full on the Product page; Home shows a short card. Guidance text is written for people who are not technical and may not know yet: "say so" is always an acceptable answer.
- Anything the AI changes on its own is announced, shown as a diff in the history, and undoable. Never edit the document silently; never take a section's text away.

## Messages

Everything the app tells you appears the same way: a short message over the top of the page (`messages.js`; `toast(...)` and `notify(...)` in `app.js`). It sits 16 px from the top, centred, at most 560 px wide, with 8 px between messages, above open dialogs, and never blocks the page behind it.

| Kind | Use for | Stays |
|---|---|---|
| success | something you did worked ("Saved", "Copied") | 4 s |
| info | something you should know ("Product requirements updated…") | 6 s |
| warning | needs your attention but nothing failed ("Pick at least one platform") | 10 s |
| error | something failed | 10 s |

- `sticky` for something that lasts as long as its cause (can't reach Orchestrator; an update waiting for you). Give it an `id` so it updates or clears instead of repeating.
- A message with actions (Undo, See what changed) stays at least 10 s. Hovering or focusing a message holds it open. Every message has a close button. At most three show at once.
- Wording: one or two short sentences, in plain words. Say what happened, then what to do if there is something to do. No raw browser or server text ("Failed to fetch"): add it to `errors.js`. Success is past tense and tiny ("Saved"). Use "…" only for something still in progress ("Retrying…").
- Not messages: what describes the page you are looking at (a job that can't start yet, features that overlap, a page that couldn't load) stays on the page next to its content.
