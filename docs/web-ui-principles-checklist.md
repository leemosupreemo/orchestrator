# Web UI revamp checklist

Tracks the original Orchestrator Principles list, grouped. Baseline findings are in
[web-ui-audit.md](web-ui-audit.md). Status: `[x]` done, `[~]` partly done, `[ ]` not started.
"Done" means implemented and covered by a test or verified in code; nothing here has been
checked in a browser yet.

## A. Getting started

- [~] **On-ramp / wizard / checklist, minimum steps to the aha moment.** Setup checklist, FAB and panel exist (required vs optional). Time to first job is not measured or shortened.
- [~] **Describe the app in my words, then answer clarifying questions.** New-project flow asks short questions and writes a brief. Job planner questions show only after planning starts.
- [ ] **Choose platforms early on a new project.** Not asked in the new-project flow.
- [ ] **Wizard examines current project state and says what's missing.** Checklist covers tooling, not product state.
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
- [ ] **Organize features with no overlap (discrete elements).** No feature/area model; jobs are a flat list.
- [ ] **See how pieces fit the overall picture.** No map or roadmap view.
- [ ] **Mark features complete (knowing they're never fully done).** Depends on the feature model.

## C. Running jobs and control

- [x] **Start / stop / pause.** Start and Run now exist. The hero's Stop is now "Pause" (stops the worker; Resume continues from the next task). Other run views still say Stop.
- [x] **Know job states; resume or delete.** Resume from the primary action or menu. Delete is now a real server action (`discard`): reverts the job's changes, deletes its AI branch, archives it. It previously only opened the console.
- [~] **Easy to revert.** Whole-job discard works. No checkpoint or per-task revert.
- [~] **Changes made clear.** Changes card shows file chips, diffstat and reasoning. No inline diff or per-task attribution.
- [x] **Easy to fix bugs I find.** "Fix something" on Home, "Still broken?" on a job.
- [ ] **Handles everything; comes to me only with critical questions.** "Action required" filter exists. No cross-job inbox of questions.
- [~] **Notifications when done or problem.** Browser notifications (opt-in, sidebar button) when a run finishes, fails, or waits for input, plus a count in the tab title. Works while the tab is in the background, not when it is closed. Email settings exist separately. No Slack or mobile push.

## D. Testing and quality

- [~] **TDD and thorough testing (unit, integration, test cases).** Plans must now carry test cases (server side). Tests page and coverage exist. Web UI does not show a job's test cases.
- [ ] **Anti-gold-plating (refactor pass).** No scope-creep check.
- [ ] **UX heuristics analysis via UI builder.** Figma attach only.

## E. Delivery and environments

- [~] **Easy distribution for manual testing and device management.** Deliver and Distribute actions (Firebase). No device or tester list.
- [~] **Handoff once built to try with a group.** Deliver to testers and export. No shareable build page.
- [~] **Versions, branches, what's live/prod.** Git page and branch in header. No environment indicator.
- [~] **Good CI/CD.** CI config detected. No pipeline status in UI.
- [~] **Use on mobile and distribute to mobile.** Responsive layout; job page is a long scroll on a phone.

## F. Integrations and measurement

- [x] **Integrate with Jira and similar.** Connections page, link picker, update log.
- [~] **Connections to other product-creation tools.** Figma, Trello, Sentry. Nothing further.
- [ ] **KPIs and analytics setup (2-3 platforms).**
- [ ] **Build-measure-learn.**
- [ ] **Experimentation / A/B (future).** Deferred by design.

## Housekeeping

- [x] Debug "Original Home/Jobs" toggles and their duplicate layouts removed.
- [x] Fixed the carried-over failing test (its fixture lacked the test cases plans now require).
- [ ] Remove the 75 inline `style=` attributes in `app.js`.
- [ ] Per-job visual QA (the screenshots card shows the newest check from the whole project).
- [ ] Accessibility pass (emoji-only status in a few lists, dialog focus).
- [ ] Verify in a browser (desktop and phone width).
