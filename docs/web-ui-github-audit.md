# Web UI audit: GitHub comparison, NN/g heuristics, layout and copy

Method: rendered pages in headless Chrome at 390 px and 1440 px (light), 19 pages including Home, a planned job,
a design job awaiting review, New job, New project, Product, Tests, Check-up, Configuration, Projects and Activity,
then checked the code behind each finding. Compared against GitHub's information architecture (web and iOS app) and
Primer guidance, and Nielsen's heuristics and NN/g articles. Single reviewer, no user testing. Branch: `web-ui-audit-fixes`.
Earlier passes: [web-ui-audit.md](web-ui-audit.md), [web-ui-heuristics.md](web-ui-heuristics.md).

Severity (NN/g): 0 cosmetic, 1 minor, 2 moderate, 3 major, 4 blocker. Status: `[x]` fixed, `[~]` partly, `[ ]` open.

References:
- Primer: [navigation](https://primer.style/product/ui-patterns/navigation), [progressive disclosure](https://primer.style/product/ui-patterns/progressive-disclosure), [button](https://primer.style/product/components/button) ("rarely use more than one [primary] per page"), [empty states](https://primer.style/product/ui-patterns/empty-states) (one primary action)
- NN/g: [progressive disclosure](https://www.nngroup.com/articles/progressive-disclosure/) (more than 2 levels "typically have low usability"), [hidden navigation](https://www.nngroup.com/articles/find-navigation-mobile-even-hamburger/) (hiding navigation degrades discoverability on mobile and desktop), [10 heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/)
- GitHub Mobile: bottom tab bar for top destinations; a repository is a list of rows (Issues, Pull requests, Actions, Releases…) that each open a page. Reddit's app pairs a bottom tab bar with a side drawer for everything else.

## The structural gap

GitHub has three levels, each with one home: **global** (dashboard, inbox, search, profile and settings), **repository**
(header with tabs and counts, Settings with a Danger zone) and **item** (a pull request: title, number, state, one merge
box, tabs). Orchestrator has a global sidebar and items, but no project level, so the same thing is reachable in several
places:

- Projects: sidebar select, Projects page, Configuration > Projects.
- Setup and health: Check-up, Configuration > Tool check, the Setup wizard, the Setup button.
- Docs: Docs page, Configuration > Documentation, Help.
- Configuration is three levels deep (sidebar dropdown, 19-tile page, the setting).

Target structure (simple first, advanced one step away, never more than two levels):

```
Global (avatar/settings)  Account and computers: machines, models, API keys, sign-ins, updates, alerts, lock, console
Project header            name ▾ · branch ▾
  Tabs: Jobs · Product · Tests · Delivery · Insights · Settings
        Jobs:     Needs you | Running | Done, with Done/Archive in bulk
        Product:  PRD + Features (+ Map)
        Tests:    runs with status, then suites, then test cases
        Delivery: live, testers, pipeline
        Insights: Measure, Check-up, Docs
        Settings: base branch, AI instructions, connections, Firebase/CI, archived jobs, Danger zone
Job (like a pull request) Title · state · one primary action
  Tabs: Overview (question or hero, tasks, conversation) · Changes (this job's diff only) · Checks (tests, cases, scope, runs, logs)
  Desktop right rail: type, feature, models, branch, linked tickets
Phone                     bottom tab bar for top destinations + side drawer for everything else
```

## Checklist

### Navigation and information architecture

- [x] **1. Phone navigation is hidden behind a hamburger (sev 3).** The bottom bar was replaced by a drawer, so every
  destination takes two taps and is out of sight. Keep the drawer for the full list, and bring back a bottom tab bar for
  the top destinations (Reddit pattern), with New job in its centre.
  Fixed: a phone tab bar (Home, Product, New job, Activity, Projects) sits beside the drawer, which keeps the full list.
- [x] **2. New job is on screen twice (sev 3).** Header button plus a floating + on every page, including the New job page
  itself, Configuration and Tests. The + covers content: a Tests row's Run button, a New job field on phones, the job
  hero's title. Breaks "one New job per viewport" in [web-ui-conventions.md](web-ui-conventions.md).
  Fixed: the floating + is gone. Phones use the tab bar's New job (Home's header button is hidden there); desktop keeps Home's button.
- [x] **9. Product has no navigation entry, and its page highlights Home (sev 2).** `resolveRoute` maps product to `home`.
  Fixed: Product is in the sidebar and the tab bar, and its page highlights it.
- [x] **13. Configuration mixes three scopes in 19 tiles (sev 2).** Account/computer settings (who can sign in, updates,
  machines, API keys) beside project settings (base branch, archived jobs, Firebase, Xcode Cloud), plus duplicates of
  Projects, Docs and the Setup wizard. The sidebar item is both a link and a dropdown.
  Fixed: grouped by scope (This project, AI models, Computers & access, Alerts, Troubleshooting) in sentence case. Projects, Documentation and the Setup wizard are no longer listed (they're in the sidebar, Help and Check-up); their routes still work. The sidebar flyout stays: it goes straight to a setting, so it's two levels.
- [x] **16. Activity only shows what was started since the page was opened (sev 2).** Empty after a reload, so it
  misleads as a destination.
  Fixed, and corrected: runs live in the server's memory, so the list survived a browser reload but emptied on a server restart. Activity now also lists earlier runs from their saved logs (owners only), and the subtitle says what the page is.
- [x] **19. Rarely used actions are heavy sidebar buttons (sev 1).** Help, Open full console, Notify me and Lock session
  sit as bordered buttons under the navigation; GitHub keeps these in the avatar menu.
  Fixed: Help, Open full console, Notify and Lock are quiet rows in a sidebar footer. Not yet: an avatar/account menu.

### Home and lists

- [~] **5. Home is alarm-fatigued (sev 3).** All 16 jobs are amber "Needs you", most from June. "Needs you (16)" equals
  "All (16)" and the badge never drops. No Done/Archive, no select-many. GitHub's inbox has Done, bulk select and swipe.
  Partly fixed: jobs waiting more than 14 days fold into one line with Show and Archive them (with Undo); a job's menu has Archive. Archive keeps the branch and files and restores exactly. Not yet: select-many, and the Home badge still counts folded jobs until they're archived.
- [x] **6. Job rows say everything twice (sev 2).** "Approve plan" pill plus "Approve the plan to start building its
  tasks."; the date twice ("Jun 12" and "Jun 12, 7:22 PM"); a Type column reading "Feature" on every row.
  Fixed: rows drop the reason sentence and the second date (exact time on hover). The Type column stays (sortable).
- [x] **10. The phone Home spends about 40% of the first screen before content (sev 2).** Project, branch, uncommitted
  count, machine and model counts and a language bar. The language bar belongs on Projects, not the dashboard.
  Fixed: the language bar is gone from Home (it's on each Projects card); the status line shows only what needs a glance (uncommitted work, running jobs, a missing machine or model).
- [x] **15. Tests is an unsorted ~6,000 px list (sev 2).** 85 suites, a Run button on every row, no last result, no
  grouping by folder.
  Fixed: suites are grouped by folder (closed when there are several; open when filtering), with suite and test counts per folder. Discovery no longer lists tests shipped inside installed libraries (`venv`, `site-packages`), which had added third-party suites. Not yet: a last result per suite.
- [x] **18. Project cards are noisy (sev 1).** "Orchestrated" on every card; the only button on inactive cards is "Forget".
  Fixed: "Not set up" shows only where it applies; other projects' names read as links (the card switches project); Open is secondary; real plurals; "Your projects".

### Job page

- [x] **3. Changes shows your uncommitted working tree, not the job's work (sev 3).** A plan awaiting approval shows
  "10 files unsaved (local)". `app.js` delta block.
  Fixed: uncommitted edits count only while the job's own branch is checked out. Needs a server restart to take effect.
- [x] **4. Sub-job links are broken (sev 3).** They point at `#/job/<id>`; the route is `#/jobs/<id>`, so "Job not found".
  Fixed.
- [x] **7. Two filled primary buttons on phones, and Delete beside More (sev 2).** "Answer" on the planner question and
  "Accept suggestions" in the hero; the question sits above the stepper. Delete is a red top-level button; GitHub keeps
  deletion last in a menu or in a Danger zone.
  Fixed: the question sits below the hero with a plain Answer button (none when the hero's action is Answer); Archive and Delete are last in More.
- [x] **8. The brief shows raw markdown; the header shows internal ids (sev 2).** `## Summary` as text;
  `FEATURE-PLAN · #124 (20260612-165816-feature-124)`.
  Fixed: the brief renders as markdown; the header reads "Design · #5". The full id stays in Technical details.
- [x] **17. Collapsible cards use a tiny "•" as their toggle on desktop (sev 1).** Primer: a chevron with text.
  Fixed: a chevron before the title and the count beside it. "Tasks Checklist" is now "Tasks".

### Product, Check-up and New project

- [x] **11. Product's empty state is shown five times (sev 2).** "Nothing here, and that's fine." on every section;
  "Draft it from my project" offered in three places (Home card, Product header, "Start here").
  Fixed: one "Start here" card when the document is empty, and the header offers Draft and Import only after that; the per-section "Nothing here" lines are gone; Home's Product card buttons are secondary so New job is the one primary.
- [x] **12. Check-up contradicts itself and Tests (sev 2).** Four filled primary buttons; the Features fix is labelled
  "Product requirements"; commands show raw backticks; "310 suite(s)" here against "85 suites" on Tests.
  Fixed: only the highlighted next step has a filled button; Features says "Add features" and opens Core features; commands render as code; counts read "3 test files", not "suite(s)" (Check-up counts files, Tests counts suites).
- [x] **14. New project step 1 asks for everything at once (sev 2).** 11 fields and 8 checkboxes, headed "PRD · Pitch";
  "Not sure: recommend for me" can be ticked alongside iOS. Ask the name, what it is and platforms; fold the rest under
  "Add more detail (optional)".
  Fixed: "The basics" shows the six questions the server requires; the four optional ones fold under "Add more detail (optional)"; no "PRD ·" labels; "Not sure" and a platform clear each other. Open question: should who-it's-for, problem and day-one features stay required when the Product page calls everything optional?

### 20. Layout, spacing and the design system (sev 2)

Done in this pass: tokens for control heights (`--control-xs…xl`: 28, 32, 38, 44, 48), shadows (`--shadow`, `--shadow-2`,
`--shadow-3`), layers (`--z-raised…--z-toast`) and the backdrop; every undefined variable replaced (they had silently
done nothing: no hover background, no radius, no "good" colour); three utility classes that markup used but CSS never
defined (`gap-8`, `gap-12`, `align-center`); stray colours moved to tokens, which fixed an invisible language-bar track
in light mode and accent glows still using the old accent colour; one card header height; phone filter chips scroll
instead of wrapping; a tablet step (761 to 1100 px) with a narrower sidebar. `test_design_tokens_hold` fails on an
undefined variable, a raw z-index or control height, more than ten distinct shadows, or more inline styles than today (62,
from 79; lower it as you go). Open: the remaining one-off inline styles, raw px in a few places, an icon-only sidebar for
tablets.

Elements come in many sizes and some float over content or sit out of line. Measured in `style.css` and `app.js`:

- [x] **Control heights:** seven different values (28, 32, 36, 38, 40, 44, 48 px). Define three: small 28, default 36,
  large/touch 44, as tokens (`--control-sm/md/lg`), and use them for buttons, selects and inputs alike.
- [x] **Shadows:** 19 distinct `box-shadow` values. Define `--shadow-1` (cards), `--shadow-2` (menus, popovers),
  `--shadow-3` (dialogs, drawers).
- [x] **Colours:** 45 hard-coded colours outside the token blocks. Move them to tokens so dark mode can't drift.
- [x] **Layering:** 13 unrelated `z-index` values (2 to 1000). Define a scale: base, sticky, overlay, drawer, dialog, toast.
- [~] **Inline styles:** 79 `style="…"` in `app.js` (the earlier pass reported 50). Replace repeated ones with classes.
- [x] **Undefined tokens:** ten variables are used but never defined (`--bg-card`, `--bg-hover`, `--border-hover`, `--good`,
  `--hover`, `--radius-sm` in CSS; `--bg-subtle`, `--err`, `--font-mono`, `--good` in JS), so those styles silently do nothing.
- [ ] **Raw px:** 13 spacing, type or radius values bypass the scale.
- [~] **Breakpoints:** one (760 px). Tablets get the phone layout or a cramped desktop. Add a tablet step (about 1024 px)
  where the sidebar narrows to icons.
- [x] **Floating elements:** the + and Setup buttons overlap content with no reserved space. Anything fixed must reserve
  its height (the tab bar does this with bottom padding on `.main`) or not exist.
- [~] **Card anatomy:** card header heights, title sizes and action placement vary (Product "Write" buttons small and
  right; Check-up actions in rows; job cards with a centred count). One card header: title left, count beside it,
  actions right, same height everywhere.
- [~] **Alignment:** buttons in a row with different heights (Product: "Draft it from my project" vs "Write it"); stat
  lines wrapping mid-group on phones; filter chips wrapping to two lines on phones (use a horizontal scroller).
- [x] **Guard it:** extend the checks in `tests/test_web_ui.py` to fail on new raw colours, shadows, z-indexes and control
  heights outside the tokens.

### 21. Words, copy and feedback (sev 2)

Done in this pass: the job hero speaks plainly ("Needs you", "Planning", "Then: the AI starts on Task 1"); "Split into
jobs"; sentence case across Configuration and its pages; role names "Architect", "Planner", "Builder", "Reviewer";
real plurals everywhere a count is shown (a `plural()` helper in `app.js`, `_n()` in `project_health.py`); the six native
`confirm()` boxes are in-app dialogs whose button names the action ("Remove machine", "Close issue"); a page that fails to
load says why (through `errors.js`) and offers Go to Home or Try again. Correction: toasts already passed errors through
`errors.js` (`toast(message, true)` calls `Errors.explain`), so that finding was wrong. `test_copy_stays_plain_and_consistent`
guards the jargon, the plurals, `confirm()` and sentence case. Open: Delete / Discard / Forget / Remove still overlap in
places (Discard is the server action behind Delete), and Stop vs Pause on run views.

Casual users should understand every label without knowing git or the pipeline; the same thing should always have the
same name; every error should say what to do next.

- [x] **Internal terms on screen:** "FEATURE-PLAN", "PHASE: PLANNING & SPEC", "Next execution: Approving will queue
  builder on Task 1", "Pending execution", "Decomposed Sub-Task Jobs", "Parallel subtask job", "Orchestrated",
  "Tracked Projects", "Validate .orchestrator/project.json", "(prerequisite audit)", "PRD" without explanation.
  Plain versions: "Feature", "Planning", "When you approve, the AI starts on Task 1", "Not started", "Split into jobs".
- [~] **One name per thing:** Delete, Discard, Forget and Remove are all used for "make this go away"; Stop and Pause both
  appear for a running job. Pick one per meaning: Delete (gone, can't undo), Archive (hidden, can restore), Remove (take
  out of a list), Pause.
- [x] **One case style:** Configuration mixes Title Case ("Base Branch", "Archived Jobs", "Email Notifications",
  "Setup Wizard", "API Keys") with sentence case ("Who can sign in", "Slack & chat alerts"). Use sentence case.
- [~] **Say it once:** the status pill and the reason sentence repeat each other; "Nothing here, and that's fine." five
  times on one page; dates twice per row; the job id twice in the header.
- [~] **Short and specific:** page subtitles describe the mechanism rather than the use ("Everything started from this
  page since the UI was opened"; "Tell it what you need. It's planned, then built on a worker, and anything that needs
  you shows up on Home."). Lead with what the person can do here.
- [x] **Actionable errors:** "Something went wrong" + "Job not found" offers no way on; give a link back to Jobs and say
  why (archived, other project). 55 places pass `e.message` straight into a toast; route them through `errors.js`
  so unknown failures still say what to try. 6 `confirm()` dialogs: name what will be lost and the button's verb
  ("Delete job"), not OK/Cancel.
- [x] **Counts and plurals:** "suite(s)", "job(s)": write the plural properly.
- [x] **Guard it:** a test that fails on banned terms in rendered labels (FEATURE-PLAN, execution, decomposed, `(s)`),
  and on Title Case in Configuration labels.

## Not evaluated

Dark mode (captured, not reviewed in detail), tablet widths, landscape, the terminal and run pages, the sign-in screen,
a real phone, assistive technology, and real users.

## Keeping it true: the review is built in

The method used here is now part of the product. UX review (sidebar, under Learn & improve) runs a product pass:
screenshots at phone and desktop widths, light and dark, then a UX pass and a design pass against the checklist in
`orchestrator/prompts/ux_reviewer.md`, which encodes this audit's principles. Every job that changes interface files
gets the same checklist on its own changes after the code review, shown on the job page. See the user guide.

## Follow-ups from review (2026-10-04)

- [x] Detail pages had no way back: a shared **Back** above the title (previous page, or the parent when opened
  directly); the 17 "← All configuration" links, Docs' "← All docs" and the file page's Back button are gone.
- [x] Project and branch pickers were placed inconsistently (one label above, one beside, cramped): both label-above,
  same size, side by side on phones.
- [x] Card titles didn't read as titles: card headers are tinted title bars with a larger heading.
- [x] Brief header: "Raw file ↗" and "Edit brief" stacked awkwardly with the path floating beside the title. Now
  **Raw** and **Edit** as a pair on the right, the path in a caption under the header, and the card-header rules in
  `docs/web-ui-conventions.md` (Cards). Test cases' Add moved to its header as **Add**.
- [x] The brief scrolled inside a fixed box: long content now fades into a centred **Show more** that grows the card
  (brief, other job documents, Product sections).

The UX and design review checks for these too: `ux.back`, `ux.action-labels`, `ux.long-content`,
`design.section-headers`, `design.header-anatomy`, `design.fields` and `design.native-controls` are on the checklist in
`orchestrator/prompts/ux_reviewer.md`, and the rules themselves are in `docs/web-ui-conventions.md`, which the review
reads as this project's conventions.

- [x] The job page grouped tickets, errors and designs in one card by source ("Linked tickets, errors & designs"), while
  uploaded mockups and logs sat in two other cards, with three ways to attach (one of which started a fix run). Now:
  the ticket is a chip in the header; **Designs** and **What went wrong** gather items whatever their source (Figma or
  upload; Sentry, logs or screenshots on a bug job), in a context rail beside the job on wide screens and under its
  top section on narrow ones; empty groups don't show; one **Attach…** asks what it is, then where from, and only
  attaches. The update log to linked apps folds at the bottom. The review checks for this as `ux.grouping`.
- [x] "Approve plan" didn't show what was being approved: the tasks were folded near the bottom and the plan's summary
  and assumptions were elsewhere. The plan now sits right under the decision while it waits (summary, assumptions and
  risks, each task with what "done" means and its files, editable), the hero points to it, and Revise sits beside it.
  The review checks for this as `ux.decision-context`.
- [x] The job's More menu had up to 16 two-line items in one scrolling list, mixing workflow steps, links, settings,
  tools and deletion, with two items for "fix it" and steps that didn't fit the stage. Now grouped (This job, Share,
  Open, Settings), one line each with descriptions in tooltips, stage-aware, one "Run a fix…", ending actions last and
  apart, a sheet with a dimmed backdrop on phones. The review checks for this as `ux.menus`.
