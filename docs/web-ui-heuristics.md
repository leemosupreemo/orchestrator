# Web UI heuristic evaluation

Method: Nielsen's ten usability heuristics applied by hand to the web UI, using rendered
pages (headless Chrome at 390px and 1300px, light and dark) and the code. This is a
single-reviewer expert evaluation, not user testing, and it did not use a design tool
(the Figma connection links designs to jobs; it does not analyse screens). Severity:
0 cosmetic, 1 minor, 2 moderate, 3 major, 4 blocker. Branch: `web-ui-revamp`.

## Fixed in this pass

| # | Heuristic | Finding | Sev | Fix |
|---|-----------|---------|-----|-----|
| 1 | Visibility of system status | Home showed "0 machines · 0 models" as neutral text; nothing said jobs can't run without them | 3 | Replaced with a warning link: "No machine set up: jobs can't run yet" (or no model) |
| 2 | Visibility of system status | Losing the connection (tunnel drop, laptop asleep) failed silently; the page kept showing stale data | 3 | Banner after two failed polls: "Can't reach Orchestrator right now… retrying" |
| 3 | Match with the real world | A job waiting for you to start a fix was labelled "Debugging", which reads as if something is running | 2 | Now "Needs a fix" |
| 4 | Aesthetic and minimalist design | The "Setup n/7" button floated over content on every page, covering text | 2 | Shown only on Home and Check-up |
| 5 | Aesthetic and minimalist design | Tests page led with Swift-only frameworks on a Python project | 2 | Shown only for Swift or unknown-language projects |
| 6 | Consistency | "Available" and "Installed" were both amber (the warning colour) | 2 | Installed is green; Available is neutral |
| 7 | Consistency | Page action buttons wrapped onto two lines (Tests: "More" under "Run all tests") | 1 | One row |
| 8 | Recognition rather than recall | On phones six pages had no way in | 3 | "More" sheet in the bottom bar (see the accessibility pass) |
| 9 | Help and documentation | No help in the app; docs buried under Configuration | 3 | Help page (sidebar, and the phone More sheet): the flow from idea to learning, where things are, a glossary, common questions |
| 10 | User control and freedom | Deleting a feature or KPI, and Mark complete, could not be undone | 2 | Deleting a feature or KPI shows an Undo for 10 seconds (no confirm needed); undoing restores jobs and KPI results. Mark complete can be restored from Configuration > Archived jobs and returns to its earlier status; the confirm now says so |
| 11 | Flexibility and efficiency | No search or shortcuts after the hotkeys were removed | 2 | Command palette: Cmd/Ctrl+K or `/` jumps to any page, job or feature and runs common actions; keyboard-operable and announced as a combobox |
| 12 | Recognition rather than recall | 13 sidebar items; Home's "Action required" filter duplicated the Inbox | 2 | Tools grouped as Build & ship / Learn & improve / Set up; the separate Inbox page was folded into the top of Home ("Waiting on you"), so there is one place to look |
| 13 | Match with the real world | Jargon in Configuration | 1 | Machines, Tool check, Orchestrator health check, Tester builds (Firebase), Instructions for AI helpers, with the old term in each description |
| 14 | Consistency | Primary action placement and job/run/task vocabulary varied | 2 | Written down in `docs/web-ui-conventions.md` and the Help glossary; a few checks are automated |
| 15 | Error prevention | Webhook and analytics keys were only checked when you pressed "test" | 1 | Both are tested on save and the result is shown right away |
| 16 | Help users recover from errors | Raw server messages in toasts | 2 | Known failures now say what to do next (`errors.js`); unknown ones pass through unchanged |
| 17 | Visibility of system status | Alert status only visible in config | 1 | The "Waiting on you" section on Home shows "Alerts: browser on/off, Slack on/off" |
| 18 | Aesthetic and minimalist design | The job page is one long scroll on a phone | 2 | Tasks, runs, logs and output are folded on phones (open on desktop) |

## Open

None from this evaluation. A single reviewer finds only part of the problems; real users will find
the rest, so treat this list as a starting point, not a clearance.

## Found while testing the fixes

Undo exposed a server bug: a request body that no route read (a DELETE with a JSON body, or a
rejected POST) was left in the connection, so the next request on it failed with
`501 Unsupported method ('{}GET')` or `400`. The server now drains unread bodies after every
request. Three regression tests fail without the fix. Discard job still cannot be undone
(it deletes the branch); its confirm says so.

Checking the grouped sidebar at a 640px-high window showed a second bug: the sidebar did not scroll,
so on short screens "Open full console", "Notify me" and "Lock session" were off-screen and
unreachable (Lock sat 275px below the visible area). The sidebar now scrolls, tightens its spacing on
short screens, and the Configuration menu is positioned so it is not clipped.

## Not evaluated

Real users, assistive technology, the terminal and run pages, the sign-in screen, tablet
widths and landscape, and copy tone across every message.
