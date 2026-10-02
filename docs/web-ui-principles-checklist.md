# Web UI revamp checklist

Tracks the original Orchestrator Principles list, grouped. Baseline findings are in
[web-ui-audit.md](web-ui-audit.md). Status: `[x]` done, `[~]` partly done, `[ ]` not started.
"Done" means implemented and covered by a test or verified in code; nothing here has been
checked in a browser yet.

## A. Getting started

- [~] **On-ramp / wizard / checklist, minimum steps to the aha moment.** Setup checklist, FAB and panel exist (required vs optional). Time to first job is not measured or shortened.
- [~] **Describe the app in my words, then answer clarifying questions.** New-project flow asks short questions and writes a brief. Job planner questions show only after planning starts.
- [x] **Choose platforms early on a new project.** The new-project questions now ask what you're building for as a multi-select (iOS, Android, macOS, Windows/Linux, web, backend, CLI/library) or "Not sure: recommend for me". The choice goes into the product brief (an undecided choice tells the AI to recommend with reasons), and the finish page lists what each platform needs next. Works in the terminal flow too.
- [x] **Wizard examines current project state and says what's missing.** Tools > Check-up: stage (getting started / building / with testers / released), "n of 10 in place", one highlighted next step, and every item with why and a fix: brief, platforms, AI instructions, git + remote, features, tests, CI, tester delivery, releases, KPIs. Fixes are a pre-filled new job, a link to the right page, or a command to copy. It checks product state; tools and keys stay in the setup checklist.
- [x] **Easy login.** Google, Apple, GitHub or access token.
- [~] **Set up machines and LLMs easily.** Config pages and counts exist. No add-machine flow from the job view.
- [~] **Any main coding language.** Stack detection and language bar. Several actions are still iOS/Firebase specific.

## B. Describing and steering work

- [x] **Feature/area of focus is clear and prominent on top.** Job page leads with a status hero: state, reason, one primary action.
- [x] **Info goes general -> detailed, actionable first.** Hero, then Progress, Changes, then docs, links, tasks, runs, logs, raw JSON.
- [x] **No redundant CTAs on one screen.** Job page: one primary action, one menu, each action once. Home: one New job per viewport. Guarded by tests.
- [x] **Statuses made clear (one vocabulary).** Job page, home and jobs lists all use the server's `state` (label, reason, tone, next action). Duplicate emoji vocabularies and banners removed.
- [x] **Express intent; if no strong opinion, LLM recommends.** New-job form has a "You decide the details" option (on by default). The planner is told to pick conventional defaults and record each as an assumption; the job page lists them under "What the AI assumed".
- [~] **Easy to tweak / course-correct.** Revise plan, Run fix, Still broken?, model override. No inline edit of tasks.
- [x] **Talk to the LLM directly, tied to the job.** "Ask about this job" thread on the job page; messages are stored in the job file. Read-only: it advises, and Revise plan / Run fix act. (The old Ask AI action started a fix run; removed.)
- [x] **Organize features with no overlap (discrete elements).** Features page: create/edit/delete, group jobs under a feature (job menu: Move to feature), roll-up progress. Overlap warnings when two features claim the same code paths, or two in-flight jobs on different features change the same file. New job has a Feature field, and the planner is told the feature's purpose, owned paths and dependencies, and which paths other features own. Not yet: auto-suggesting a feature.
- [x] **See how pieces fit the overall picture.** Features > Map: features laid out in layers (foundations on the left, what builds on them to the right) with dependency lines, status, job progress, "waiting on" for unfinished dependencies and overlap flags. Dependencies are set by hand in the feature form; circular ones are rejected. Not yet: dependencies inferred from code, or a view of jobs inside the map.
- [x] **Mark features complete (knowing they're never fully done).** "Mark complete" records when; attaching new open work reopens the feature and says so.

## C. Running jobs and control

- [x] **Start / stop / pause.** Start and Run now exist. The hero's Stop is now "Pause" (stops the worker; Resume continues from the next task). Other run views still say Stop.
- [x] **Know job states; resume or delete.** Reviewed jobs with no PR now end with "Mark complete" in the web UI (before, the only option was "Open in console"). Resume from the primary action or menu. Delete is now a real server action (`discard`): reverts the job's changes, deletes its AI branch, archives it. It previously only opened the console.
- [~] **Easy to revert.** Whole-job discard works. No checkpoint or per-task revert.
- [~] **Changes made clear.** Changes card shows file chips, diffstat and reasoning. No inline diff or per-task attribution.
- [x] **Easy to fix bugs I find.** "Fix something" on Home, "Still broken?" on a job.
- [x] **Handles everything; comes to me only with critical questions.** Inbox (first item in the sidebar, with a count): every job waiting on you (questions, plan/design approvals, reviews, failures), plus runs stopped at a prompt, ranked failures first. Each row has its one next action inline. Other projects' waiting jobs are listed below with "Switch & open". The tab title shows the same count. Not yet: notifying when a job moves into the inbox without a run (only run changes notify).
- [x] **Notifications when done or problem.** Three channels: (1) browser alerts (sidebar button) for anything new in the inbox and for runs that finish or fail, firing even in a background tab; (2) Slack-compatible webhook (Configuration > Slack & chat alerts) sent by the web server for the same events, so it reaches your phone with the tab closed, with a link back to the job; (3) the worker's existing email and macOS alerts. Limits: the webhook watcher runs only while the web server runs, and covers the active project only; no native mobile push.

## D. Testing and quality

- [x] **TDD and thorough testing (unit, integration, test cases).** Job page has a Test cases card: covered / planned / no test / manual, split by unit, integration and UI, with steps, expected result and where each test lives, and a warning when automated cases that are due have no test. The Tests page lists the whole project's case library by area with status filters. Coverage is read from the code (an assigned test exists, or a test carries the case id), not from what the model says. Not yet: running a single case from the UI, or a test-first gate that blocks building until tests exist.
- [ ] **Anti-gold-plating (refactor pass).** No scope-creep check.
- [~] **UX heuristics analysis (use ui builder connection).** Expert evaluation against Nielsen's ten heuristics, written up in `docs/web-ui-heuristics.md`: 8 findings fixed, 10 open with severity and a suggestion each. It was done by reviewing rendered pages and code, not through the Figma connection (which links designs to jobs but can't analyse screens), and not with real users. Not yet: an analysis built into the product that runs on a design or on the running app.

## E. Delivery and environments

- [~] **Easy distribution for manual testing and device management.** Delivery page: latest build with testers (version, build, groups, when, which job), "Send current branch", per-job "Send to testers" for reviewed work, earlier builds, tester groups, and a warning if the Firebase CLI is missing. Testers and devices themselves are managed in the Firebase console (linked); the UI can't add or remove them.
- [~] **Handoff once built to try with a group.** Send to a Firebase tester group from the Delivery page or a job, and see what they have. No shareable install page or invite link of its own.
- [x] **Versions, branches, what's live/prod.** Delivery > Live treats the base branch as production: its newest commit, the last release tag, and how many changes are not yet released. Next to it: what testers have, and what is ready to ship. Not yet: named environments beyond "live" and "testers".
- [~] **Good CI/CD.** Delivery > Pipeline shows recent GitHub Actions runs (status, branch, link) and whether the base branch build is passing, next to the Live card. Needs `gh` signed in; Xcode Cloud is only detected, not read. No re-run or trigger from the UI.
- [~] **Use on mobile and distribute to mobile.** Responsive layout; job page is a long scroll on a phone.

## F. Integrations and measurement

- [x] **Integrate with Jira and similar.** Connections page, link picker, update log.
- [~] **Connections to other product-creation tools.** Figma, Trello, Sentry. Nothing further.
- [~] **KPIs and analytics setup (2-3 platforms).** Tools > Measure: connect Mixpanel, Amplitude or PostHog (US/EU, key stored but never shown) and verify with a test event; define KPIs per feature (event, target, direction, unit); export a tracking plan to `docs/analytics/tracking-plan.md`. Jobs created under a feature are told to emit its KPI events through the project's analytics layer and add a test for each. Caveats: the test-event calls follow each provider's documented ingestion API but were only checked against fakes, not a live account; it does not read numbers back from the provider.
- [~] **Build-measure-learn.** Each KPI has a learning log: log a result by hand with a decision (keep / iterate / drop) and a note; the page shows latest vs target, trend and on-track / behind, and feature cards summarise their KPIs. Not yet: results pulled automatically from the provider, or a job suggested from a "behind" KPI.
- [ ] **Experimentation / A/B (future).** Deferred by design.

## Housekeeping

- [x] Debug "Original Home/Jobs" toggles and their duplicate layouts removed.
- [x] Fixed the carried-over failing test (its fixture lacked the test cases plans now require).
- [x] Inline `style=` attributes in `app.js` cut from 97 to 50 by replacing repeated spacing with utility classes. The rest are dynamic (progress widths) or one-offs.
- [ ] Per-job visual QA (the screenshots card shows the newest check from the whole project).
- [x] Accessibility pass, measured with a Chrome DevTools audit over 12 pages x phone/desktop x light/dark: fixed contrast failures (light-theme accent, warn, ok, bad and muted colours; white text on amber badges), a duplicate icon id, an unlabelled chat box, small tap targets, and no visible focus ring on links. Added a skip link, a page title that updates per page (with the inbox count), focus moved to the page heading on navigation, and removed `aria-live` from the whole page. Audit result: 0 issues in overflow, names, labels, contrast and ids. Not covered: a real screen reader, keyboard-only walkthrough of every dialog, and the terminal pages.
- [x] Verified in a browser (headless Chrome via DevTools) at 390px and 1300px, light and dark, for every main page. Found and fixed: on phones the Lock session and Notify buttons were off-screen, and Tests, Git, Delivery, Measure, Check-up and Connections had no way in (now a "More" sheet in the bottom bar). Not tested: a real phone, tablet widths, landscape, the terminal/run pages, and the sign-in screen.
