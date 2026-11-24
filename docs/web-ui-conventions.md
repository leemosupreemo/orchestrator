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

- The header is a title bar (tinted, section-title heading): the title on the left, a count may sit beside it, actions on
  the right.
- At most two visible actions, as small buttons, the secondary one first and the main one last; anything else goes in a
  More menu.
- Labels are one verb when the card names the object: **Edit**, **Raw**, **Add**, **Attach…**, **Run**. Name the object
  only when it would be unclear next to the title ("Edit" on the Brief card, not "Edit brief"). The button's tooltip
  can say it in full.
- Facts about the content (where a file lives, when it changed) go in a `.card-caption` strip under the header, never
  loose in the header.
- In a folding card the header is the toggle, so its actions go at the bottom inside the fold.
  The exception is a plain icon link that only navigates (Home's Product card: `#i-external` at the header's right edge, `margin-left: auto`); it is its own click target and never toggles the fold.
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
- **Inline fix routing:** Starting a fix (`action="debug"` or `action="fix"` with a job) from anywhere—Home's job
  list, a job page, or a run page—always opens or stays on `#/jobs/<id>` instead of navigating to a dedicated fix run
  page. When the hero's next action is **Run fix**, the hero renders a single primary **Run fix** button directly under
  the optional note textarea (suppressing the duplicate top-right **Run fix** button), and the failure copy-log control
  (`#copy-job-failure-log`) uses the standard `.btn.ghost.icon-only` size with inline loading spinner and temporary
  green check + **Copied** confirmation (`.is-copied`).
- **Titles & in-app question alerts:** Job table titles (`.job-table-row .job-title`) and list item titles (`.item .title`) wrap up to 2 lines (`-webkit-line-clamp: 2` / `line-clamp: 2`) on desktop and mobile before truncating. Newly arriving **Question for you** inbox items show a sticky warning notification in `#messages` with an **Answer question** link (and clear automatically once answered).

- A More menu groups its items under headings (on a job: **Work on this job**, **Links**, **Settings**, **Finish or remove**; sharing and exporting live behind a single **Share & export…** modal), one line each with the
  description in the tooltip (`moreMenu(items, { compact: true })`), only what fits the current stage, and ending
  actions (close, archive, delete) last below a divider. It fits without scrolling; on phones it opens as a sheet over
  a dimmed page.
- **Icon-only & dismissal controls:** Compact add controls use the `+` SVG (`#i-plus`) and overflow menus use the `⋯` SVG (`#i-more`) inside `.btn.icon-only` / `.btn.icon-only.small` (uniform `--control-md` `40px` square hit target on desktop, `--control-lg` `44px` on mobile, `padding: 0`, `.icon` `20px` / `.small .icon` `18px`, with `aria-label` and `title`). Dismissing a modal, dialog, or cancellable loading card uses a top-right `✕` (`.dialog-close-btn`) rather than an extra bottom `Cancel` text button.

- Nothing the app starts waits at a terminal. Runs from the app carry `ORCHESTRATOR_NONINTERACTIVE=1`
  (`child_env()`); in them, menus take their defaults, "press Enter" pauses don't wait, and a question that needs
  typed input ends that step with a clear line instead (`scripts/common.py`: `web_run()`, `interactive()`). Questions
  for you go to the job's page (Answer). Each action passes everything its script needs (Revise passes the job's
  models, machines and branch mode). The only terminal tools are the console, wizard and configuration menus
  (`TERMINAL_ACTIONS`), and the app reaches only the console, from the sidebar; setup, GitHub sign-in and device
  logs have app pages.

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
| Spacing | `--space-0` 2, `--space-1` 4, `--space-1-5` 6, `--space-2` 8, `--space-2-5` 10, `--space-3` 12, `--space-4` 16, `--space-5` 20, `--space-6` 24, `--space-7` 32, `--space-8` 40 |
| Type size | `--type-display` 24, `--type-page` 18, `--type-section` 16, `--type-body` 14, `--type-secondary` 12, `--type-tag` 11 |
| Type weight | `--weight-regular` 400, `--weight-medium` 550, `--weight-bold` 650 |
| Radius | `--r-xs` 4, `--r-sm` 6, `--r-md` 8, `--r-lg` 12 (cards; `--radius`), `--r-pill` |
| Control height | `--control-xs` 28 (inline), `--control-sm` 32 (small buttons), `--control-md` 38 (default), `--control-lg` 44 (touch), `--control-xl` 48 |
| Shadow | `--shadow` (cards), `--shadow-2` (menus, popovers), `--shadow-3` (dialogs, drawers) |
| Layer | `--z-raised`, `--z-inner`, `--z-sticky`, `--z-float`, `--z-header`, `--z-palette`, `--z-drawer`, `--z-menu`, `--z-toast`; a backdrop is its layer minus one |

Every `var(--x)` must be defined; `test_design_tokens_hold` checks this, the layers, the control heights and the shadows,
and ratchets the number of inline styles in `app.js` down. `test_text_uses_the_type_roles` checks that every `font-size`
and `font-weight` comes from the type tokens, that no inline style sets text size, weight or spacing, and that every
utility class used in markup is defined.

### Type roles

Every piece of text is one of these. Markup built in scripts uses the `t-*` class; existing selectors read the tokens.

| Role | Class | Size / weight | Used for |
|---|---|---|---|
| Display | `.t-display` | 28 bold | Sign-in and empty-state heroes |
| Page title | `.t-page` | 22 bold | The page `h1` |
| Section title | `.t-section` | 18 bold | Card titles |
| Subsection title | `.t-subsection` | 15 bold | `h3` in a card, hero titles, row titles |
| Body | `.t-body` | 15 regular | Running text, inputs, buttons, notices |
| Field label | `.t-label` | 15 medium | The label of a form field; never upper-case |
| Secondary | `.t-secondary` | 13 regular, muted | Page subtitle, row meta, hints, timestamps |
| Tag | `.t-tag` | 11 bold, upper-case | Small overline labels ("LATEST COMMIT") |

Code uses `0.9em` of the text around it. Phones use the same sizes; only text fields on touch screens are 16px, so
iPhone Safari does not zoom in. A card's title, its controls and its content start on one left edge
(`--container-pad-x`); a row inside a card body does not add a second inset.

Rules checked on every page at 360, 430 and 1440 px, light and dark:
- No horizontal scroll. A control that sizes itself to its content (a select, a long path) needs `max-width` and its parents `min-width: 0`.
- Stand-alone controls and links are at least 28 px tall on desktop and 36 px on phones; inline links inside a sentence are exempt.
- Long model-written text (an architect's concerns, a failure reason) is shown with `clamped()` ("Show more"); rows clamp it to two lines. The same text is not shown twice on one page.
- A list that can grow past about five items is capped with "Show all N".

## Containers, heroes and callouts

Callout and hero containers (`.job-hero`, `.banner`, `.notice`) frame actionable status, warnings and next steps:
- **Tone pairing**: Tinted containers pair tone borders with matching soft backgrounds (`--warn` + `--warn-soft`, `--accent` + `--accent-soft`, `--ok` + `--ok-soft`, `--bad` + `--bad-soft`). Text uses `--text` or high-contrast ink tokens.
- **Visual hierarchy**:
  - The hero title (`.job-hero-title`) is an `h2` in the subsection role (`--type-body`, `--weight-bold`), line-height 1.3. It never uses a tiny pill badge for the container's title.
  - The description (`.job-hero-reason` or banner body) uses `--type-body`. Spacing between title and description is `--space-1` (4px) to keep them grouped.
- **Button sizing & proportions**:
  - Hero action buttons use standard `.btn.primary` (`min-height: 38px`, padding `--space-2` `--space-4`), not bloated 48px `.big` buttons. On phones (`<= 760px`), primary actions stretch to full width with `min-height: 44px`.
  - Banners use `.btn.small` (`min-height: 32px`).
- **Container geometry**:
  - Border radius uses `--radius` (`--r-lg`, 12px).
  - Padding uses `--space-4` `--space-5` on desktop for heroes, `--space-4` on phones, and `--space-3` `--space-4` for banners/notices.
- **Job lifecycle stepper & next step clarity**:
  - Every job detail page presents a 5-phase agile lifecycle stepper (`.job-stepper`): `1. Plan` → `2. Build` → `3. Verify` → `4. Document` → `5. Decide`.
  - Active steps use `var(--accent)` (or `var(--warn)`/`var(--bad)` when attention/fix is needed), completed steps use `var(--ok)` with `✓`, upcoming steps are muted. Subsequent fix/re-plan cycles partially reset downstream steps (`Pending re-verify` / `Pending re-check`), and iteration **Version history** (with `Undo last iteration` and assumed-default chips) folds along the bottom of `.job-stepper`. `Document` is auto-skipped on `quick-fix`, `bug-fix`, `bug-investigate`, `coverage`, `test-audit`, and `tests-only` jobs unless docs changed.
  - The hero card leads with its title (what's needed, e.g. "Approve plan"), says why and what to look at, and shows the active-run `Now:` line (`.job-hero-next-preview`), latest meaningful AI/tool activity (`.job-hero-activity`), **View run** and **Stop** actions while running, and remaining machine disk space (`.job-hero-disk`, from `GET /api/state` `disk_free_bytes`) along the bottom (no separate `activeRunCard` above the stepper). When automatically stopped/blocked, the hero surfaces only the stop reason and recovery actions (hiding unrelated feedback/YOLO banners). On failures/blockers, a `#i-copy` icon button sits top-right on the title row to copy failure details and the latest run log in one click, and machine-scoped blockers (including low disk space from structured blockers, `last_error`, unreported run failures, or live `state.disk_low` taking priority before showing **Run fix** / **Resume** / **Start**) offer **Free up space** and a 1-click **Run on `<Peer Name>`** failover button when an eligible online peer is available; sign-in blockers hide the fix input and **Run fix** button. On review-ready / decide jobs, `.job-decide-routes` provides explicit **Build again**, **Frame again**, **Try again**, and **Pause / Close** redirect actions alongside the primary release button, and `.job-review-summary` sits as a full-width band inside `.job-hero`. No badge row above the title.
  - The **Questions** card (`#clarify` when active, `#clarify-history` fold when answered) auto-expands when unanswered questions exist, shows only unanswered questions labeled `Blocking` / `Non-blocking` (folding previously answered items), places a right-aligned `#i-download` dropdown menu (**Copy chat** / **Export chat**) on the title row, and puts status flags (`blocking`, `non-blocking`, `auto-answered`) on a row below the title.
  - On Run pages, the status banner includes remaining machine disk space (`Disk space`) and monotonic activity-driven task progress (`step_fraction` advancing on observed AI/tool activity before `BUILDING`); the live Timeline filters spinner/hook noise to display up to 3 recent meaningful activity updates under the active model call; finished runs place a `#i-refresh` rerun icon and `#i-share` copy/export log menu in the upper-right header.
  - On Documentation, **Gaps** rows use uniform right-aligned primary **Fix** buttons opening a chooser modal (Use AI, Write manually, or Import file); **Repository docs** places `#i-download` export in its card header; job and feature snapshots live on their respective pages.
  - On AI Tools, provider rows and supported-model trees start collapsed by default, tool abilities scan silently in the background (showing only usage-limit holds), and the "AI models updated" toast advances its baseline on first display so it does not repeat across reloads.
  - The **Progress** card clearly separates task completion count, active/next builder task, remaining tasks, and verification status.
- **Job Tasks and Plan**:
  - Tasks appear above Plan once. The Tasks header displays a dynamic run-breakdown pill (`1 run`, `1 batched run`, `N sequential runs`, or `N runs · M waves (parallel)`) explaining how the job executes. Multi-wave and parallel tasks display `Wave K · Parallel` or `Wave K · After T1` badges, and all active tasks in a running parallel wave display `In progress` simultaneously. Long task descriptions (>140 chars) format into scannable sentence bullets with a `More (N)` fold and basename file chips. Each task shows completion and a run count that expands to the associated runs.
  - The **Runs** table sits directly above Tasks in `.job-table` format with a 5-run cutoff and **Show more** up to 25 runs (without a stage/model summary footer).
  - Plan shows a short bullet summary, with **Full brief** for viewing or downloading the Markdown and **Edit brief** for changing it.
- **Modals & dialogs**:
  - All modal titles (`dialog h2` and `.feedback-title`) share one consistent heading style: `var(--type-page)`, `var(--weight-semibold)`, `line-height: 1.3`, `letter-spacing: -0.01em`, `color: var(--text)`.
  - Connector modals place a primary blue `Login ↗` button in `.conn-login-top` (bottom padding/margin `--space-4` + `1px solid var(--border)` divider) above the form fields, and an inline `Get token ↗` link next to every secret token field label.

## Home

- One list of jobs. A job that needs you is a job in an earlier status: it sorts first and its status says what it needs ("Approve plan"). The whole row is one link to the job, where the next step is the primary action; rows carry no buttons. There is no second "waiting" list; "Needs you" is a filter.
- Actions belong to the page for what they act on (Tests: run, build, coverage; Delivery: send a build; Device logs: pull; Readiness: environment checks and the setup checklist). Home has no menu of them.
- The sidebar leads with what people open most (Home, Activity, Product), then Build & ship (Tests, Delivery, Git, Device logs) and Review (Check-up, UX review, Analytics, Docs). Settings is one entry: Configuration, whose "Set up" group holds Readiness, Connections and Projects. "Finish setup" sits at the top of the sidebar only until the required setup is done. The phone tab bar is Home, Product, New job, Activity, Tests.
- One home per setting. Check-up is about the product; Readiness is about whether jobs can run here (setup checklist, build tools, tool and health checks); Slack alerts are on Connections; Orchestrator's guides are in Docs; archived jobs are a filter on Home; the test case library is under Tests (`#/tests/cases`). The terminal console's Configuration menu uses the same groups and names (checked by `MenuParityTests`).
- A route that moves keeps its old address working: add it to `routes.js`, which sends old links to the new place with their query.
- Things that are not jobs go where they belong: blockers fold concisely into the top **Project status** card, and the **Product** card moves to the bottom of the Home stack collapsed once all sections are completed. Home stays focused on the active project without cross-project waiting lines.

## Sign-in

- Provider sign-in uses the pop-up. A blocked pop-up is explained ("Allow pop-ups for this site"), never silently turned into a full-page redirect: that flow fails with "Unable to process request due to missing initial state" in browsers that partition storage between the site (`swift-orch-web-20260923.web.app`) and Firebase's auth domain (`swift-orch-web-20260923.firebaseapp.com`). Full-page sign-in is offered as an opt-in link, with a warning.
- To make redirects work everywhere, set `authDomain` in `app.js` to the site's own domain, after adding `https://<site>/__/auth/handler` as an authorized redirect URI in Google Cloud (Credentials > the Web client), as the callback URL of the GitHub OAuth app, and as an Apple Services ID return URL. Setting it first makes Google answer `redirect_uri_mismatch` (checked 2026-10-02).

## Product requirements

- One document, six optional sections, shown in full on the Product page; Home shows a short card. Guidance text is written for people who are not technical and may not know yet: "say so" is always an acceptable answer.
- Anything the AI changes on its own is announced, shown as a diff in the history, and undoable. Never edit the document silently; never take a section's text away.
- A change that arrives from another computer (source `sync`, "synced from another computer" on the Product card) follows the same rule: a history version, an announcement, undoable. When two computers changed a section, both texts are kept.
- Sharing with other computers is on by default, said plainly ("on by default") with **Stop sharing** beside it per kind; a public repository gets a warning saying who can read it. If a policy holds a kind back, the question says where it would go and who could read it, and the safe choice leads.

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
