# Web UI audit against Orchestrator Principles

Scope: `orchestrator/web/static/{index.html,app.js,configuration.js,style.css}` and `orchestrator/web/server.py`,
read from the `principles-revamp` worktree (includes its uncommitted edits). Static code review only; the deployed
Firebase app was not exercised. Line numbers refer to `app.js` unless noted.

Legend: **Met** / **Partial** / **Missing**

## Scorecard

| # | Principle | Verdict | Evidence / gap |
|---|-----------|---------|----------------|
| 1 | Idea -> app on web/mobile/desktop easily | Partial | `#/new-project` wizard (2713-2810) is describe -> where -> create, GitHub-centric. No platform choice. |
| 2 | Describe in my words, then clarifying questions | Partial | Planner question surfaces as a card (1704) once the job runs. No up-front clarification before creating a job. |
| 3 | Job focus very clear and prominent on top | Partial | Title appears 3 times (page title 1699, sub 1700, card 1710). Status is in the 2nd card, below a plain attribute grid. |
| 4 | General -> detailed, actionable first | Partial | Banner and next-action sit mid-page (1744), after an attributes card. Then 4 action grids with 15 tiles (1783-1876) push logs, tasks and docs far down. Tasks/docs/logs order is not by relevance. |
| 5 | Handles everything, comes to me with critical questions | Partial | "Action required" filter (1359) and "Answer" action exist. No inbox of questions across jobs. |
| 6 | Set up machines and LLMs easily | Partial | Config pages and setup checklist exist (`setup_checklist.py`). Machine/model counts link to config. No one-click add-machine flow visible from the job view. |
| 7 | Integrate with JIRA etc. | Met | `#/connections`, link picker (2620-2680), Jira/Trello/Sentry/Figma, integration log (1922). |
| 8 | TDD and thorough testing | Partial | Tests page, coverage, suites, plans (2100-2131); job shows pass/fail (1509). No test-first gate, no per-job test cases or unit/integration split. |
| 9 | Easy to tweak | Partial | Revise plan, model override, advanced options. "Revise Plan" is listed 3 times on one screen (header menu 1497, tile 1794, banner). |
| 10 | Organize features, no overlap | Missing | No feature/area model. Jobs are a flat list. Sub-jobs exist only via "Splinter" (1799). |
| 11 | Understand status clearly | Partial | Canonical `state` group/tone (1022). But 3 status vocabularies coexist: `formatJobStatus` (1522), banner titles (1539), `state.label`. Emoji-only meaning. |
| 12 | Any main coding language | Partial | Language bar (147) and stack detection. Several actions are iOS-specific (Simulator Visual Check, Firebase). |
| 13 | Easy login | Met | Google/Apple/GitHub SSO plus access token (822-858). |
| 14 | Clear on-ramp, wizard, checklist, minimum aha | Met/Partial | Setup checklist + FAB + panel (1255-1291) separates required from optional. Time to first "aha" is not measured or shortened. |
| 15 | Easy distribution for manual testing, device mgmt | Partial | Distribute/Deliver actions (1330, 1495). No device or tester list, no per-build status. Firebase only. |
| 16 | Connections to favorite creation tools | Partial | Figma/Jira/etc. only. |
| 17 | Talk to LLM directly, tied to job | Partial | "Ask AI [Q]" tile (1803) runs an action. No persistent conversation thread visible on the job. |
| 18 | Easy to course-correct | Met | Revise, Still broken?, Run fix, Resume. |
| 19 | Easy to revert | Partial | One "Discard & Revert" with `confirm()` (553). No revert to a checkpoint or a specific commit. Not undoable. |
| 20 | Changes made clear | Partial | DELTA card shows file chips and diffstat (1764). No inline diff or per-task change attribution. |
| 21 | Easy to start/stop/pause | Partial | Start, Stop (`data-stop`), Resume exist. **No pause**, and no stop for non-running states. |
| 22 | Know job states, resume or delete | Partial | Resume yes. Delete is "discard" and is buried in the Safety grid. Archive state exists but has no action. |
| 23 | Easy to fix bugs I find | Met | "Fix something" hero button (1382), "Still broken?". |
| 24 | Express intent, LLM recommends if no opinion | Missing | New-job form has no "you decide" affordance. Model and approach are defaults, not recommendations with rationale. |
| 25 | See how pieces fit the overall picture | Missing | No architecture/feature map or roadmap view. |
| 26 | Handoff to try with group | Partial | Deliver to testers, ZIP export (1837). No shareable build page or invite flow. |
| 27 | UX heuristics analysis via UI builder | Missing | Figma attach only. No heuristic review step. |
| 28 | Notifications when done/problem | Partial | Email settings and test (server.py 1410-1620). Browser/push/Slack not in the web UI. In-app toast only. |
| 29 | Wizard examines project state, says what's missing | Partial | Setup checklist covers tooling, not product state (features done, tests missing). |
| 30 | Mobile use and distribute to mobile | Partial | Responsive layout (9 `@media` blocks), mobile project select, floating New button. Job detail is a long scroll on mobile. |
| 31 | Choose platforms early on new project | Missing | Not asked in the new-project flow. |
| 32 | KPIs/analytics setup (Mixpanel etc.) | Missing | No references anywhere in UI or server. |
| 33 | Mark features complete (never truly complete) | Missing | No feature entity. Job "completed" only. |
| 34 | Versions/branches, what's live/prod | Partial | Git page and branch in header. No environment or "what's live" indicator. |
| 35 | Good CI/CD | Partial | Server detects `ci_scripts`/Xcode Cloud (server.py 1462). No pipeline status in UI. |
| 36 | Anti-gold-plating (refactor pass) | Missing | "Quick change" can be a refactor but there is no review for scope creep. |
| 37 | **No redundant CTAs on the same screen** | **Missing (worst)** | See below. |
| 38 | Build-measure-learn | Missing | No metrics loop. |
| 39 | Experimentation / A/B (future) | Missing | Fine as a deferral. |

## Principle 37 findings: redundant CTAs (highest priority)

Job detail page shows the same action in several places:

- **Revise plan**: header menu (1497), tile `[F]` (1794).
- **Select/override models**: pipeline "Override Models [O]" (1758), tile `[O]` (1833).
- **Primary next action**: header primary button (1490), banner button (1546-1617), tile `[A]` (1790). All three do the same thing.
- **Open in console / Back**: header "Open in console" (1500), tile `[B]` Back to Main Menu (1870) plus sidebar "Open full console".
- **View in GitHub**: header menu (1498) and tile `[G]` (1849).
- **Attach context**: tiles `[L]`/`[K]` (1816-1820) and the "Attach..." button (1917).
- **Run fix**: header (1492), banner (1582, 1600).

Home page: "New job" appears in the sidebar, in the hero button (1381), in the floating button (index.html 76), and in the original layout again. "Fix something" is on Home and per job.

Terminal-isms leak into the web UI: `[S]` hotkeys in button labels, "Press 'S' to…" hints (1545-1608), `└─` tree glyphs (1738, 1771), and ALL-CAPS banner titles. Keyboard shortcuts are keyed `S` for three different actions (Stop, Schedule, Splinter), and `data-key="S"` is duplicated on one page, so the first match wins.

## Other heuristic findings

1. **Debug toggles shipped in production UI**: "Original Home (debug)" and "Original Jobs (debug)" buttons (1377, 1470) plus two legacy render paths (~150 lines duplicated). Remove or gate behind a flag.
2. **Inline styles**: 75 `style="…"` attributes in `app.js` block theming and consistency.
3. **Destructive action uses `confirm()`** with all-caps text (553). There is no undo, no showing of what will be lost.
4. **Accessibility**: emoji-only status (`✅`, `❌`) with `aria-label` only in some lists. `data-key` shortcuts can fire while focus is on a link. Dialog focus handling not verified.
5. **Hardcoded model names** in the pipeline fallback (1629-1631: `gemini-3.1-pro-preview`, `gpt-5.4`) display as if real when the job has none, which is misleading.
6. **`approach` fallback** shows "Standard workflow" when unknown, which looks authoritative.
7. **`visualChecks[0]`** shows the newest check for the whole project on every job (1894), not that job's.
8. **Job detail does 2 API calls serially** (`jobs/<id>` then `visual-checks`), and failure is swallowed (1649).
9. **Docs card** opens only the first doc; the Brief is not distinguished from investigations.
10. **Security note**: `data-params` JSON is built with `esc(JSON.stringify(...))` in many places. Check `esc` escapes quotes. `data-open` URLs are not validated for scheme (`javascript:` is blocked only by `window.open` semantics, but should be allowlisted).

## Recommended revamp order

1. **Job detail redesign** (principles 3, 4, 37): one header with title, state pill, and a single primary action. Then "Needs you" (question, review, failure). Then progress (tasks + next). Then Changes. Then Details (docs, logs, outputs, JSON) collapsed. One "More" menu replaces the 15-tile grid. Drop hotkey labels from buttons.
2. **One status vocabulary** (11): derive label, icon, tone and next action from `state` only.
3. **Add Pause and a real Revert/Checkpoint** (19, 21, 22).
4. **Job conversation thread** (17) and a "you decide" option in New job with a recommendation (24).
5. **Cross-job inbox** for questions and failures plus browser/Slack notifications (5, 28).
6. **Feature/area model and map** (10, 25, 33). This is the largest new surface and underpins KPIs, versions, and environments.
7. Remove debug toggles and inline styles (cleanup).
