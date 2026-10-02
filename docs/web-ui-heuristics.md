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
| 4 | Aesthetic and minimalist design | The "Setup n/7" button floated over content on every page, covering text | 2 | Shown only on Home, Inbox and Check-up |
| 5 | Aesthetic and minimalist design | Tests page led with Swift-only frameworks on a Python project | 2 | Shown only for Swift or unknown-language projects |
| 6 | Consistency | "Available" and "Installed" were both amber (the warning colour) | 2 | Installed is green; Available is neutral |
| 7 | Consistency | Page action buttons wrapped onto two lines (Tests: "More" under "Run all tests") | 1 | One row |
| 8 | Recognition rather than recall | On phones six pages had no way in | 3 | "More" sheet in the bottom bar (see the accessibility pass) |

## Open

| # | Heuristic | Finding | Sev | Suggestion |
|---|-----------|---------|-----|------------|
| A | Help and documentation | No help in the app. Docs are buried under Configuration > Documentation; forms have little hint text | 3 | A Help link in the sidebar and More sheet; short "why" text under each setup step |
| B | User control and freedom | Delete feature, delete KPI, and Mark complete have a confirm but no undo (archived jobs can be restored from Configuration, but nothing says so) | 2 | A toast with Undo for 10 seconds; mention restore on the confirm |
| C | Flexibility and efficiency | The terminal-style hotkeys on the job page were removed, so power users lost shortcuts and there is no search across jobs or features | 2 | A command palette (jump to job, page or action) |
| D | Recognition rather than recall | 13 sidebar items; Home's "Action required" filter duplicates the Inbox | 2 | Group Tools into Build / Ship / Learn; drop the filter or link it to the Inbox |
| E | Match with the real world | Jargon survives in Configuration: "Prerequisite Audit", "Self-Tests", "Machine Fleet", "Xcode Cloud" | 1 | Plain names with the old term in the description |
| F | Consistency | Primary actions sit in different places: top right, a hero, inline in rows. "Job", "run" and "task" are used loosely | 2 | One rule for primary action placement; a short glossary |
| G | Error prevention | The webhook and analytics keys are checked only when you press "test" | 1 | Test on save and show the result inline |
| H | Help users recover from errors | Server errors reach the user as a toast with the server's raw message | 2 | Pair each common failure with what to do next |
| I | Visibility of system status | Whether browser alerts and the Slack webhook are on is visible only in the sidebar button and one config page | 1 | A line in the Inbox header: "Alerts: browser on, Slack off" |
| J | Aesthetic and minimalist design | The job page is a long scroll of cards on a phone | 2 | Collapse Changes, Logs and Technical details by default on small screens |

## Not evaluated

Real users, assistive technology, the terminal and run pages, the sign-in screen, tablet
widths and landscape, and copy tone across every message.
