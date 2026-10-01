# Swift Orchestrator: Comprehensive Test Matrix & Quality Assurance Guide

This document catalogs the complete test matrix for Swift Orchestrator and its Web UI. It establishes acceptance criteria, covers edge cases and boundary conditions, outlines failure recovery modes, and evaluates end-to-end workflows from distinct user perspectives (POV).

---

## 1. System Architecture & Testing Taxonomy

```
+----------------------------------------------------------------------------------------------------+
|                                         USER INTERFACE (WEB UI)                                    |
|   +-------------------+  +-----------------------+  +--------------------+  +------------------+   |
|   | Project Explorer  |  | Language Pill & Cards |  | Jobs & Quick Fix   |  | Interactive TTY  |   |
|   +-------------------+  +-----------------------+  +--------------------+  +------------------+   |
+----------------------------------------------------------------------------------------------------+
                                             │ HTTP / SSE / Token / Cookies
+--------------------------------------------▼-------------------------------------------------------+
|                                  LOCAL WEB SERVER (Python 3.14)                                    |
|   +-------------------+  +-----------------------+  +--------------------+  +------------------+   |
|   | Project Switcher  |  | GitHub / Local Scanner|  | Job State Engine   |  | PTY Process Mgr  |   |
|   +-------------------+  +-----------------------+  +--------------------+  +------------------+   |
+----------------------------------------------------------------------------------------------------+
                                             │ CLI Subprocesses & Filesystem
+--------------------------------------------▼-------------------------------------------------------+
|   Git Repositories    │    Xcode / SPM / Generic    │    Firebase & CI    │    Fleet Machines      |
+----------------------------------------------------------------------------------------------------+
```

### Test Case Classification
- **BFT (Basic Functionality Test)**: Happy path, canonical behavior, standard workflows.
- **EC (Edge Case & Boundary)**: Null states, unicode paths, giant buffers, empty lists, rate limits.
- **FM (Failure Mode & Recovery)**: Missing tools, network outages, interrupted processes, unhandled exits.
- **POV (User Perspective & UX)**: Ergonomics, responsive design, keyboard flow, focus retention, latency.
- **SEC (Security & Threat Mitigation)**: Token leak prevention, CSRF headers, CSP directives, traversal defense.

---

## 2. Test Matrix by Functional Area

### Category A: Project Management & Source Tracking

| ID | Title | Type | Preconditions | Test Procedure | Expected Result | Automation |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **TC-PROJ-01** | Active Project Detection & Display | BFT | Orchestrator initialized on target repo | Open Web UI at root (`#/`) | Sidebar dropdown and topbar header display the active project name. | Automated (`test_state`) |
| **TC-PROJ-02** | GitHub vs. Local Source Classification | BFT | Repository with git remote vs. local repo without remote | Navigate to `#/projects` | Projects with GitHub remotes display `GitHub Tracked` badge with `owner/repo`. Local-only repos display `Local` badge. | Automated (`test_detect_project_source_github_and_local`) |
| **TC-PROJ-03** | Source Filter Switching | BFT | Multiple tracked projects (mix of GitHub and Local) | Click "GitHub (N)" and "Local (N)" filter chips on Projects page | Project grid instantly filters cards without reloading; counters match source counts. | Manual / E2E |
| **TC-PROJ-04** | Active Project Switching via Sidebar | BFT | At least 2 projects in recent history | Select alternate project in sidebar dropdown | UI sends `POST /api/project`, triggers state refresh, updates active project name, and reloads current view. | Automated (`test_projects_endpoint_lists_all_projects_and_active`) |
| **TC-PROJ-05** | Manage Projects Option in Dropdown | BFT | UI loaded | Select "📁 Manage all projects..." at the bottom of sidebar dropdown | Dropdown resets to active project; view navigates directly to `#/projects`. Option has distinct muted background. | Manual / E2E |
| **TC-PROJ-06** | Filesystem Project Discovery Scanner | BFT | Adjacent directories contain Xcode, Swift, or Git projects | Open "Add Project" dialog; allow scan to run | Discovered codebases appear in list; already-tracked projects are excluded; projects show type badges. | Automated (`test_projects_scan_finds_projects`, `test_project_picker_excludes_tracked_projects_from_discovery_rows`) |
| **TC-PROJ-07** | Manual Path Project Ingestion | BFT | Valid directory on host with git or Xcode project | Enter path in "Project Directory Path" input and submit | Project is verified, appended to tracked list, and made available in dropdown. | Automated (`test_projects_add_and_forget`) |
| **TC-PROJ-08** | Untracking / Forgetting Project | BFT | Project is currently tracked (not active) | Click "Forget" on project card in `#/projects` | Project is removed from tracked list; filesystem remains intact. | Automated (`test_projects_add_and_forget`) |
| **TC-PROJ-09** | Missing / Deleted Project Folder on Disk | EC / FM | Tracked project directory deleted from filesystem | Restart server or switch to project in UI | UI displays graceful fallback ("Cannot find directory on disk"); does not crash; allows user to Forget it. | Manual |
| **TC-PROJ-10** | Path Traversal Prevention in Project Switch | SEC | Attacker supplies `../../../../etc` in project root | Issue `POST /api/project` with `{"root": "/etc"}` | Server validates path, verifies orchestrator configuration, and returns 400 Bad Request. | Automated (`test_switch_project_rejects_unknown_dirs`) |
| **TC-PROJ-11** | Detached HEAD & Dirty File Indicator | EC | Project in detached HEAD state with 5 unstaged files | View project card and Home status subline | Displays commit SHA instead of branch name; shows `5 uncommitted` files indicator with warning tone. | Automated (`test_git_state`) |

---

### Category B: Codebase Language Analytics & Visualizations

| ID | Title | Type | Preconditions | Test Procedure | Expected Result | Automation |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **TC-LANG-01** | GitHub API Language Fetching | BFT | GitHub tracked project with `gh` CLI authenticated | Load project state in UI | Backend queries `gh api repos/{repo}/languages`, computes byte percentages, and returns structured language array. | Automated (`test_get_project_languages_github_api`) |
| **TC-LANG-02** | Local Filesystem Language Scanner Fallback | BFT | Project without GitHub remote or when offline | Load project state | Scanner walks directory, tallies bytes by extension, skips `.git`/`Pods`/`DerivedData`, and computes accurate percentages. | Automated (`test_scan_local_languages`) |
| **TC-LANG-03** | Topbar Language Breakdown Pill | BFT | Project loaded with languages on Home screen | Inspect topbar title row next to `#page-title` | Multi-segment colored bar and top language pills appear adjacent to project name with matching Linguist colors. | Manual / E2E |
| **TC-LANG-04** | Topbar Tooltip Completeness | BFT | Multi-language project (e.g., Swift 86.1%, Python 12.0%, Shell 1.0%) | Hover over topbar language pill | Tooltip displays full list of languages and percentages (`Swift: 86.1% · Python: 12.0% · Shell: 1.0%`). | Manual / E2E |
| **TC-LANG-05** | Topbar Language Hiding on Other Pages | BFT | Topbar language pill active on Home | Navigate to `#/activity` or `#/git` | Language pill is hidden (`hidden=true`) so it does not falsely label other page titles. | Manual / E2E |
| **TC-LANG-06** | Project Cards Language Visualization | BFT | Multiple projects with diverse languages | Open `#/projects` | Every card displays a 100% width proportional segmented bar and top colored dot tags above git metadata. | Manual / E2E |
| **TC-LANG-07** | In-Memory Cache & TTL | BFT | Project languages calculated once | Trigger 10 rapid state refreshes within 1 hour | Subsequent queries return cached languages without spawning subprocesses or re-scanning disks. | Automated (`test_get_project_languages_github_api`) |
| **TC-LANG-08** | 100% Single-Language Codebase | EC | Project contains only Python files | View language pill and card bar | Bar renders as a single uninterrupted solid segment (100.0%) with no visual seams. | Manual |
| **TC-LANG-09** | Empty or Non-Code Directory | EC | Project with only README.md or no recognized extensions | Load project | Language pill gracefully returns empty string; `#topbar-languages` remains hidden without errors. | Automated (`test_scan_local_languages`) |
| **TC-LANG-10** | GitHub API Rate Limit or Network Timeout | FM | Block network or exhaust GitHub API token | Load GitHub project | Backend catches timeout (2.0s), falls back silently to local scanner; user experiences no hanging. | Manual |

---

### Category C: Job Management & Task Execution

| ID | Title | Type | Preconditions | Test Procedure | Expected Result | Automation |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **TC-JOB-01** | Canonical Job Status Labels | BFT | Jobs in various states (`completed`, `review-needed`, `debugging`, `planned`) | Open Home jobs table | Badges display canonical terminology (`Completed`, `Review needed`, `Debugging`, `Planned`) with correct color tones. | Automated (`test_kind_labels_are_plain_language`) |
| **TC-JOB-02** | Filter Empty State Clarity | BFT | Filter with 0 jobs vs. brand new project with 0 jobs | Switch filter to "Action required" when none waiting | Table displays `No jobs match this filter.` when filtered; displays onboarding description when completely empty. | Manual / E2E |
| **TC-JOB-03** | Sorting by Last Modified and Status | BFT | Multiple jobs in list | Click `Status` and `Last modified` column headers | Table sorts in ascending/descending order; active sort direction arrow reflects state. | Manual / E2E |
| **TC-JOB-04** | One-Click "Fix Something" / Quick Fix | BFT | Active project loaded | Click "Fix something" hero button, enter issue description, submit | Creates run with action `fix`, launches live terminal stream, opens run view immediately. | Automated (`test_new_job_validates_input`) |
| **TC-JOB-05** | New Job Creation (`#/new`) | BFT | Active project loaded | Navigate to `#/new`, enter title, prompt, select model, submit | Backend writes job spec to `.orchestrator/jobs/`, launches planner run, links run to job ID. | Automated (`test_new_job_writes_spec_file_and_passes_flags`) |
| **TC-JOB-06** | Active Run Indicator on Job Row | BFT | A job has an active running process | View Home jobs list | Job row displays flashing green dot `<span class="dot"></span>Working` badge. | Automated (`test_jobs_list_marks_jobs_with_a_running_run`) |
| **TC-JOB-07** | Human Clarification Prompt | BFT / POV | Job enters `human-needed` with clarification question | View job in list and job detail view | Job grouped under "Action required" with primary "Answer" button; clicking opens dialog displaying question. | Automated (`test_question_needs_an_answer`) |
| **TC-JOB-08** | Review-Needed Merge Guard | BFT / EC | Job in `review-needed` status | Check available action buttons | Offers "Merge PR" only if a PR URL is recorded; otherwise offers "Review changes" and "Approve". | Automated (`test_review_needed_offers_merge_only_with_a_pr`) |
| **TC-JOB-09** | Malformed Job JSON Resilience | FM | Corrupted JSON file in `.orchestrator/jobs/` | Load jobs list | Server skips or flags invalid job file; valid jobs render normally without 500 Internal Server Error. | Manual |
| **TC-JOB-10** | Job Archival and Restore | BFT | Completed job | Open job menu, click "Archive job"; navigate to archived menu | Job disappears from active table; appears in archived list; can be restored to active list. | Manual |

---

### Category B: Interactive Terminal (PTY) & Streaming Runs

| ID | Title | Type | Preconditions | Test Procedure | Expected Result | Automation |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **TC-TERM-01** | Real PTY Spawning & TTY Verification | BFT | Valid action invoked (e.g. `check` or `test`) | Start run via `POST /api/runs` | Subprocess executes in genuine pseudo-terminal (`isatty() == True`), preserving color and cursor control. | Automated (`test_runs_in_a_real_tty_and_takes_input`) |
| **TC-TERM-02** | SSE Real-Time Terminal Streaming | BFT | Running process emitting stdout | Client opens `/api/runs/{id}/stream` | Server sends Server-Sent Events (`data: <base64>`); xterm.js decodes and renders in real time. | Automated (`test_start_stream_and_input`) |
| **TC-TERM-03** | Terminal Keybar Input | BFT | Interactive run prompting for input (e.g. `[y/N]`) | Click `y`, `Enter`, or `Ctrl-C` in terminal keybar | Input bytes are posted to `/api/runs/{id}/input` and received by running process stdin immediately. | Automated (`test_start_stream_and_input`) |
| **TC-TERM-04** | Dynamic Terminal Resizing | BFT | Window size changed on desktop or mobile | Resize browser viewport | FitAddon calculates new column/row dimensions; client debounces and sends `POST /api/runs/{id}/resize`. | Automated (`test_run_starts_at_requested_terminal_size`) |
| **TC-TERM-05** | Clean Process Group Termination | BFT | Running long build or script | Click "Stop" button on run page | Server sends `SIGTERM` / `SIGKILL` to entire process group (`os.killpg`); status updates to failed/stopped. | Automated (`test_stop_terminates_process_group`) |
| **TC-TERM-06** | Late Reader Stream Catch-Up | EC | Process started 10 seconds ago | Open run page midway through execution | Client requests `/stream?offset=0`; server replays all historical output from offset 0 before streaming live. | Automated (`test_late_reader_catches_up_from_offset_zero`) |
| **TC-TERM-07** | Fallback Terminal for Unsupported Browsers | EC | Browser where `window.Terminal` fails to load | Navigate to active run | Page falls back to preformatted text block (`<pre class="term-fallback">`) without throwing unhandled exceptions. | Automated (client logic) |
| **TC-TERM-08** | High-Volume Output Flooding | EC / Stress | Script emits 100,000 lines of logs | Monitor UI memory and terminal responsiveness | Terminal remains stable; memory does not crash browser tab; transcript persists to `.orchestrator/logs/ui/`. | Manual / Stress |

---

### Category E: Security, Authentication & Session Integrity

| ID | Title | Type | Preconditions | Test Procedure | Expected Result | Automation |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **TC-SEC-01** | Unauthorized Request Rejection | SEC | No token or cookie provided | Issue `GET /api/state` | Server responds with `HTTP 401 Unauthorized`. | Automated (`test_api_requires_token`) |
| **TC-SEC-02** | URL Token Exchange for HttpOnly Cookie | SEC | Valid access token in URL query parameter | Navigate to `http://localhost:8765/?token=<TOKEN>` | Server returns `Set-Cookie` with `HttpOnly; SameSite=Strict`; client strips token from address bar. | Automated (`test_url_token_is_exchanged_for_http_only_cookie`) |
| **TC-SEC-03** | Invalid / Expired Token Rejection | SEC | Forged or invalid token | Navigate with `?token=invalid-secret` | Server returns 401; user redirected to Sign In gate. | Automated (`test_wrong_url_token_rejected`) |
| **TC-SEC-04** | CSRF Header Requirement on Mutations | SEC | Request with valid token but missing custom header | Issue `POST /api/runs` without `X-Orchestrator-UI: 1` | Server rejects request with `HTTP 403 Forbidden`. | Automated (`test_post_requires_ui_header`) |
| **TC-SEC-05** | Content Security Policy & Clickjacking Guard | SEC | Load `/` HTML | Inspect response headers | Header contains `frame-ancestors 'none'`, restrictive script-src, and secure connect-src directives. | Automated (`test_static_index_served_with_csp`) |
| **TC-SEC-06** | Static File Path Traversal Defense | SEC | Malicious URL path requested | Issue `GET /../server.py` or `GET /%2e%2e/server.py` | Server rejects path and returns `HTTP 404 Not Found`. | Automated (`test_static_traversal_blocked`) |
| **TC-SEC-07** | Firebase ID Token Authentication | SEC | Firebase configured; valid user ID token | Post ID token to `/api/auth` | Server validates token against Google Identity Toolkit; checks email against allowed list; logs in. | Automated (`test_auth_endpoint_valid_id_token`) |
| **TC-SEC-08** | Unverified Email Address Rejection | SEC | Firebase account with unverified email | Attempt login with unverified token | Server refuses login with `email isn't verified` error (HTTP 403). | Automated (`test_auth_refuses_accounts_without_a_verified_email`) |
| **TC-SEC-09** | Session Lock Button | BFT / SEC | User logged in | Click "Lock session" in sidebar | Client clears local token; state resets; sign-in gate modal immediately blocks screen. | Manual |
| **TC-SEC-10** | Non-Localhost Binding Warning | SEC | Server started with `--host 0.0.0.0` | Observe terminal output on startup | Server displays high-visibility warning advising token requirement and private network (Tailscale) usage. | Manual |

---

### Category F: Developer Tools & Platform Views

| ID | Title | Type | Preconditions | Test Procedure | Expected Result | Automation |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **TC-TOOL-01** | Git Status & Branch Switcher View | BFT | Git repository with multiple branches | Open `#/git` | Lists local branches, current commit hash, dirty files diff preview, and branch checkout dropdown. | Automated (`test_git_state`) |
| **TC-TOOL-02** | Safe Branch Checkout Parameterization | SEC | Branch name with shell metacharacters (`main; rm -rf /`) | Attempt checkout of branch | Server passes branch as structured argument to git without shell interpolation; invalid names rejected. | Automated (`test_git_actions_validate_branches`, `test_stash_checkout_keeps_branch_out_of_the_shell_string`) |
| **TC-TOOL-03** | Test Suite Runner View | BFT | Project with tests configured | Open `#/tests`, click "Run Tests" | Spawns test action; terminal streams live test execution; shows pass/fail summary upon completion. | Automated (`test_actions_run_the_package_that_serves_the_ui_not_the_projects_copy`) |
| **TC-TOOL-04** | Device Logs Viewer | BFT | Project configured for device logging | Open `#/devlogs` | Displays recent device log sessions, crash reports, and pull trigger button. | Automated (`test_directories_expand_to_log_files_and_cloud_refs_are_described`) |
| **TC-TOOL-05** | Unconfigured Device Logs Guidance | EC | Project without remote logs enabled | Open `#/devlogs` | Renders clean explanatory card detailing setup steps rather than throwing errors. | Automated (`test_devlogs_reports_unconfigured`) |
| **TC-TOOL-06** | Configuration Submenus Navigation | BFT | Active project | Click configuration submenus trigger | Context menu opens with all configuration areas (API keys, fleet, Firebase, audit, update). | Manual / E2E |
| **TC-TOOL-07** | Onboarding Setup Wizard Checklist | BFT | New project without full configuration | Open Home view | Setup banner / wizard appears with actionable steps (Machines, AI Keys, Project Settings). | Automated (`test_config_settings_roundtrip_and_validation`) |

---

## 3. User Perspective (POV) Scenarios & Walkthroughs

### Scenario 1: First-Time Developer Onboarding (Zero Config)
- **Persona**: Alex, a new iOS engineer opening Orchestrator for the first time.
- **Journey**:
  1. Alex runs `orchestrator ui` in their terminal.
  2. Browser opens automatically to `http://127.0.0.1:8765/?token=...`.
  3. Token exchanges silently for an HttpOnly session cookie; URL cleanses.
  4. Setup Checklist appears indicating missing machine worker or API keys.
  5. Alex runs Setup Wizard, configures Gemini/Claude keys, and returns to Home.
- **Success Criteria**: Zero manual terminal file edits needed; clear wizard instructions; no ambiguous error codes.

### Scenario 2: Multi-Project Lead Developer
- **Persona**: Sarah, managing 3 customer projects and 1 internal tool.
- **Journey**:
  1. Sarah navigates to `#/projects`.
  2. She filters by "GitHub" to check remote repo synchronization.
  3. She observes language breakdown pills (e.g. Thirteen: Swift 86.1%, Python 12.0%).
  4. She checks uncommitted dirty file indicators on cards before switching.
  5. She switches to `Themis` with one click; topbar header and state instantly update.
- **Success Criteria**: Rapid context switching (< 200ms); zero project cross-contamination; dirty states immediately apparent.

### Scenario 3: Remote Mobile Operator over Cloudflare Tunnel
- **Persona**: Dave, monitoring automated test runs from his iPhone while away from his desk.
- **Journey**:
  1. Dave opens Cloudflare tunnel URL `https://...trycloudflare.com` on Safari iOS.
  2. Authenticates via Firebase Google sign-in with company email.
  3. Top bar displays project name and language pills formatted for compact mobile viewport.
  4. Terminal displays with touch-friendly keybar buttons (`Ctrl-C`, `Enter`, `Esc`).
  5. Dave taps "Lock session" when done to invalidate cached tokens on his phone.
- **Success Criteria**: Mobile-responsive layout (< 400px width); keyboard doesn't obscure terminal; session locks securely.

### Scenario 4: Fast Bug Triage & Quick Fix
- **Persona**: Morgan, investigating a test failure reported on main branch.
- **Journey**:
  1. Morgan lands on Home dashboard; sees job marked `Debugging` in red.
  2. Clicks job row; reads test audit failure output and error trace.
  3. Clicks "Fix something" hero button; types `Fix lobby seat index out of bounds`.
  4. Watches real-time streaming PTY output as the agent applies fix and verifies tests.
  5. Job status updates to `Completed` with green badge.
- **Success Criteria**: Entire cycle completed from web UI; live feedback during execution; clear next-action buttons.

---

## 4. Edge Cases, Failure Modes & Stress Matrix

| Failure / Edge Condition | Vulnerability or Symptom | Expected System Behavior & Mitigation |
| :--- | :--- | :--- |
| **Network Partition during GitHub API call** | Request hangs or UI freezes. | 2.0s strict timeout on `subprocess.run(["gh", ...])`; catches exception and falls back to local scanner. |
| **Corrupted `project.json`** | Python `json.JSONDecodeError` crashing server. | `read_json_file` catches `JSONDecodeError`, logs error, and returns empty dict default safely. |
| **Stale Git lock file (`.git/index.lock`)** | Branch checkout or commit commands fail. | Command runner reports actionable git error; run page banner displays failed exit code and log link. |
| **Rapid Polling while Typing** | User input in text field overwritten by background tick. | `tick()` explicitly checks `document.activeElement.matches("input, textarea, select")` and skips reload if active. |
| **Process Stalled in Infinite Loop** | Zombie process consuming CPU. | User clicks "Stop" button; server signals process group via `os.killpg(session.pgid, signal.SIGTERM)`. |
| **Ultra-narrow Mobile Viewport (< 340px)** | Elements overlap or break container width. | Responsive flexbox with `flex-wrap: wrap`, collapsible sidebar, and minified dot labels. |

---

## 5. Verification Commands & Test Execution

Run the canonical validation suite in terminal:

```bash
# 1. Syntax & compilation audit
python3 -m compileall -q orchestrator tests

# 2. Complete unit & integration test suite
python3 -m unittest discover -s tests -p "test_*.py"

# 3. Direct execution of web UI test suite
python3 -m unittest tests/test_web_ui.py

# 4. Project configuration schema verification
orchestrator check-config
```
