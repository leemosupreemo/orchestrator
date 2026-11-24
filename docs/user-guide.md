# Orchestrator User Guide

This guide covers using Orchestrator with supported project stacks. Swift and Xcode support is the most mature; support for additional languages is in beta.

If you are new to Orchestrator, start with [Getting Started](getting-started.md) for the short overview and first-run path.

## Install

Quickest, on your Mac: one command installs what's missing, installs Orchestrator, adds the computer to your account and starts it at login (see [Web UI](#web-ui) for what that gives you):

```bash
curl -fsSL https://swift-orch-web-20260923.web.app/install.sh | sh
```

It needs Python 3.11+, Git, pipx and cloudflared, and installs the ones that are missing (on a Mac through Homebrew). Add `--no-service` to skip the start at login, or `--dry-run` to only print what it would do. On Windows, run it from a WSL terminal.

To install by hand instead, the CLI install is:

```bash
brew install pipx
pipx ensurepath
pipx install "git+https://github.com/leemosupreemo/orchestrator.git@web-ui"
```

Upgrade later with:

```bash
pipx install --force "git+https://github.com/leemosupreemo/orchestrator.git@web-ui"
```

Restart Orchestrator after the update, once its jobs finish. The hosted site currently uses the `web-ui` branch; installing the default branch does not provide the same features.

For local development from this repository:

```bash
cd /path/to/swift_orchestrator
python3 -m pip install -e .
```

Verify the command is available:

```bash
orchestrator --help
```

The package has no Python runtime dependencies outside the standard library. Some workflows require external command-line tools:

- Xcode project build/test: `xcodebuild`, `xcrun`
- GitHub issue and pull request workflow: `gh` (not needed for GitLab, Bitbucket or plain git; see [Code hosts](code-hosts.md))
- Remote workers: `ssh`, `scp`, `rsync`
- AI providers: at least one of `codex`, `antigravity`, `claude`, `opencode`, `ollama`, or matching API key configuration
- Firebase delivery: `firebase` plus a project-local distribution script

Optional Codex-side MCP servers and plugins can improve repo, GitHub, iOS Simulator, Sentry, and browser inspection workflows. See [Recommended MCP Servers, Plugins, and Extensions](recommended-mcp-plugins.md).

## First-Run Wizard

For a new project, prefer the wizard:

```bash
orchestrator wizard
```

The interactive wizard has five stages:

1. **Project:** review detected settings together. Press Enter to accept them, or choose a field number to edit.
2. **AI setup:** keep available models, select models by number, log in to a provider, or enter an API key. At least one model is required.
3. **Optional tools:** select GitHub integration, SSH workers, custom role prompts, or (for Apple projects) Firebase delivery and signing. Enter skips optional setup; existing settings are retained.
4. **Review and apply:** review the proposed settings and actions. Edit project fields or models, cancel without writing project files, or press Enter to save. Project configuration, API keys, prompt copies, and generated files are written only after applying. CLI logins/account switches happen immediately when selected; keychain setup and worker installation run after saving.
5. **Verify and finish:** validate the saved configuration and index the project. Xcode projects can also check build settings; this does not compile the app. Completion appears only after all requested checks pass, with `orchestrator console` as the next step.

Text fields accept ordinary letters and spaces, including `s` and `q`. The prompt labels optional fields and explains whether Enter keeps a default, skips that field, or continues. Use Ctrl-S to **skip the current section** where offered and Ctrl-Q to quit. Yes/no menus also accept S/Q; arrow-key selectors use B to return. Enter keeps the displayed default. Failed checks leave the applied configuration in place and show a command for retrying.

Applying initializes the project if needed and creates missing starter docs and the helper script. `--force` replaces generated configuration and starter docs; the review screen calls this out before applying. Non-interactive runs keep their flag-driven flow without review prompts.

Scriptable example:

```bash
orchestrator wizard \
  --models codex,antigravity \
  --copy-prompt-overrides \
  --ssh-machine mac2=mac2:/Users/me/Documents/MyApp \
  --firebase \
  --distribution-script-path scripts/distribute_ios.sh \
  --firebase-plist-path MyApp/GoogleService-Info.plist \
  --team-id YOUR_TEAM_ID \
  --method ad-hoc \
  --verify \
  --non-interactive
```

After the wizard, review:

```text
AGENTS.md
docs/build-test-commands.md
docs/ai-workflow.md
.orchestrator/project.json
.orchestrator/config/machines.json
.orchestrator/config/settings.json
.orchestrator/prompts/*.md
```

The files in `.orchestrator/prompts/` are project-local role prompt overrides. They are copied only when requested, and they should be checked for project-specific assumptions before jobs are created.

Use `--verify` to run setup/config checks before the wizard exits. Use `--install-workers` when the wizard adds SSH machines and should immediately run package install/check for those workers.

## Trying Setup From Scratch

To go through first-time setup as a new user would, as often as you like, without touching your own setup:

```bash
orchestrator sandbox            # opens the web app as on a brand-new Mac
orchestrator sandbox --sample   # ...with a small sample git project to try "Add existing project"
orchestrator sandbox --reset    # start over from a blank slate
orchestrator sandbox --wipe     # delete it
```

It runs the web app the way the Mac app does on a first launch: no project yet, so setup asks for one. It uses a throwaway home folder at `~/.orchestrator-sandbox/<name>/home`, so projects, settings, sign-ins and anything setup installs (OpenCode, for example) stay there. It also can't see the AI and developer tools you've installed, so setup installs what it needs from scratch, as on a new Mac. `--keep-tools` lets it see them. Use `--name` to keep several sandboxes, for example one per scenario. It runs on port 8799, so the browser's sign-in for it is separate from your real one. Stop it with Ctrl+C; running the same command again carries on where you left off.

Not isolated: the macOS Keychain, where Claude Code and GitHub's `gh` keep their sign-ins, so those may already show as signed in. An installer that starts a login shell can also see `/usr/local/bin`.

## Project Selection

The easiest path is to run commands from the project root:

```bash
cd /path/to/MyApp
orchestrator wizard
orchestrator console
```

You can also point commands at a project explicitly:

```bash
orchestrator wizard --project /path/to/MyApp
orchestrator check-config --project /path/to/MyApp
orchestrator console --project /path/to/MyApp
```

Initialized projects are remembered in:

```text
~/.orchestrator/projects.json
```

List recent projects:

```bash
orchestrator projects
```

Set the active project:

```bash
orchestrator use MyApp
```

Then commands can run from outside the repo and use the active project when no project is found from the current directory:

```bash
orchestrator console
```

Explicit project selection still wins:

```bash
orchestrator console --project MyApp
```

## Manual Initialization

From the target Swift repository:

```bash
orchestrator init
orchestrator check
orchestrator check-config
```

Or initialize a specific path:

```bash
orchestrator init --root /path/to/MyApp --project-name MyApp
orchestrator init --project /path/to/MyApp --project-name MyApp
```

### Importing an Existing Project

To bring an existing project under Orchestrator in one step — detect the stack, write config, generate starter docs, and optionally draft a PRD from the code — use `import`:

```bash
cd /path/to/YourProject
orchestrator import              # full flow with PRD drafting
orchestrator import --no-prd     # config + docs only, no PRD
orchestrator import --check      # also run orchestrator check afterward
orchestrator import --force      # overwrite existing config and docs
```

`import` supports the same stack detection as `init` (Swift, Xcode, SPM, Python, Rust, Node, Go, C#/Unity, etc.) and passes the detected build/test commands through. When a PRD is drafted, the model analysis runs as a background task that you can review and accept from the Product page or via the CLI diff.

Optional starter files:

```bash
orchestrator init --with-starter-docs --with-helper-script
```

`init` creates `.orchestrator/` in the target repository. It detects the first `.xcworkspace` or `.xcodeproj`, then asks `xcodebuild -list -json` for schemes and targets when possible.

Generated files:

```text
.orchestrator/
  .gitignore
  project.json
  config/
    machines.json
    settings.json
  jobs/
  logs/
  output/
  state/
```

Commit `.orchestrator/.gitignore`, `.orchestrator/project.json`, and `.orchestrator/config/*.json` if the team should share the same orchestrator setup. The generated `.orchestrator/.gitignore` excludes runtime `jobs/`, `logs/`, `output/`, and `state/` contents by default.

With `--with-starter-docs`, `init` also creates:

```text
AGENTS.md
docs/build-test-commands.md
docs/ai-workflow.md
```

With `--with-helper-script`, `init` creates:

```text
scripts/orchestrator
```

The helper script runs `orchestrator "$@"`, which gives the project a stable repo-local command wrapper.

## Configure Project Behavior

Edit `.orchestrator/project.json` after initialization. Common fields:

```json
{
  "project_name": "MyApp",
  "base_branch": "main",
  "pr_base_branch": "main",
  "branch_prefix": "ai/issue",
  "xcode_project": "MyApp.xcodeproj",
  "xcode_workspace": null,
  "scheme": "MyApp",
  "test_target": "MyAppTests",
  "derived_data_path": "/tmp/myapp_orchestrator_dd",
  "build_command": null,
  "test_command": null,
  "backend_test_command": null,
  "app_bundle_id": null,
  "visual_app_path": null,
  "delivery_provider": null,
  "distribution_script_path": null,
  "firebase_plist_path": null,
  "remote_package_install_path": "~/.orchestrator/package",
  "firebase_distribution": false
}
```

Use `build_command` and `test_command` when a project needs custom build/test commands instead of the generated `xcodebuild` defaults or auto-detected stack commands (`detect_project_stack`: SwiftPM, npm/Node, Python, Go, Rust, Gradle, Unity, .NET). If neither `.orchestrator/project.json`, `docs/build-test-commands.md`, nor stack auto-detection provides both commands, the setup checklist and job preflight checks direct you to Onboarding checklist (`#/checkup`).

Swift Package example:

```json
{
  "xcode_project": null,
  "xcode_workspace": null,
  "scheme": "MyPackage",
  "test_target": "MyPackageTests",
  "build_command": "swift build",
  "test_command": "swift test"
}
```

Run validation after edits:

```bash
orchestrator check-config
```

### Measure verification time on one computer

Set `verification_mode` to `observe` in `.orchestrator/project.json` and add `"parallel_verify": true` to exactly two adjacent per-job entries in `test_commands`. Choose checks that can each run from a clean checkout and depend only on the candidate source and their declared prerequisites. Keep coverage in one place if a check is already part of the main test command.

Run `orchestrator check-config` and then run a job. After the main build and tests pass, the extra checks run in their configured order on the job's computer. Each invocation writes a local JSON record at `.orchestrator/output/<job-id>/verification/<invocation-id>.json` under the default runtime directory. The record gives the pair's elapsed time and each check's duration, status, retry information, and any missing prerequisite. A job that skips testing produces no record.

The default mode is `off`. Observation keeps the existing test verdict rules, including how blocked extra checks are reported.

### Try assisted verification

Set `verification_mode` to `assist` to make both opted-in checks required. They run even if the builder skips its main test command. If a required check cannot run on either computer, the job reports it in the test log and remains unverified.

In the owner's `.orchestrator/config/machines.json`, opt an enabled SSH worker in with `"verification_helper": true`. The runner considers the first opted-in helper allowed by the job. The helper needs Orchestrator on its SSH shell's Python path, access to the same Git origin, at least 1 GiB free on the repository and temporary checkout volumes, and matching opted-in command definitions in its local project configuration. A helper can run checks without any enabled AI models. Run `orchestrator check-config` after changing project settings.

For speed-based placement, put measured samples in the owner's `.orchestrator/config/verification-timings.json`. Use each machine's configured name and replace the example digest keys below with the two digests from the observation records. Values are lists of elapsed seconds. Include a helper `setup` list for the cost of preparing one candidate on that machine.

```json
{
  "local": {
    "first-check-digest": [58, 60, 61],
    "second-check-digest": [58, 59, 60]
  },
  "air": {
    "first-check-digest": [62, 65, 67],
    "second-check-digest": [47, 49, 50],
    "setup": [5, 6, 7]
  }
}
```

Each estimate needs at least three samples. The owner dispatches for speed when the predicted saving exceeds both 10 seconds and 10% of the local pair time. A uniquely capable helper can run a required check without calibration. The run log records the placement and any fallback. A dirty shared checkout uses local checks. Job-owned worktrees can send their uncommitted candidate when they started clean.

To return to observation, set `verification_mode` to `observe` before the next job starts. Use `off` to disable measurements as well.

The Pro-to-Air pilot still needs live validation. See the [two-Mac verification implementation plan](superpowers/plans/2026-11-21-two-mac-verification.md).

## Configure AI Providers

On **Manage AI tools**, **Connect** shows a guided checklist for Claude, Codex, Poolside and Antigravity. It installs the tool if needed, prepares user-local Node.js/npm for Codex when missing, and shows the browser approval link and any code. Installer terms require your explicit agreement. When it is time to sign in, the checklist steps aside and the dialog shows only what to do: **Open sign-in** (or **Copy link** to sign in on another device), then either enter the approval code it shows, or, when the tool gives you a code back (Claude's does), paste it into the box and press **Submit code**. No terminal is involved. When the installer asks you to agree to terms, the question is shown above **Agree and continue**. Closing the dialog keeps the sign-in running under Running now; its terminal there answers anything else a tool asks.

**Keeping the AI going.** Two things stop an AI partway, and Orchestrator handles both:

- **It stops to ask.** In a YOLO job nobody is waiting to answer, so when the planner or builder asks a question, Orchestrator answers for you: decide from the product requirements, the architecture and the code, prefer the smallest change, note the assumption, and carry on (a "should I continue?" is answered "yes, carry on"). It does this up to 3 times a job; after that the job waits for you, so an AI that keeps asking can't loop. You can always tell: the job says **🤖 Answered for you** with how many, and its questions list each one as **Answered for you (YOLO)**, never as yours. The run log shows it as it happens. Jobs that aren't YOLO still wait for your answer.
- **It runs out of usage.** When a tool says its usage limit, quota or credits ran out (or it's rate limited), Orchestrator sets that tool aside for every job until the reset time it gave (an hour if it didn't say, a few minutes for a rate limit), and a Claude or Codex limit covers all of that tool's models, since it's the account's. The job carries on with the next allowed model, which is told another model started the work and to pick up from the files with `git diff` rather than start over. If every available model hits a usage or rate limit, Orchestrator pauses the job with a usage-limit blocker showing when it resets (and why each tried model failed) and **automatically resumes the job** once the reset time passes. Once the reset time passes the tool is back in rotation by itself. **Manage AI tools** says which tools are out of usage and until when.

**What your AI tools can do.** Manage AI tools lists each installed tool's own slash commands, skills, plugins and commands, as the tool reports them on this computer (Claude Code reports everything it has loaded in the project; the others list their commands and the folders they keep custom commands and skills in). It rescans when you open the page and the last scan is over 15 minutes old, or on **Rescan**. A few of these are a button on a job, in its More menu, once the job has changes: **Code review by Claude** (`/code-review`), **Security review by Claude** (`/security-review`), **Code review by Codex** (`codex review`), and **Deep cloud review (Claude ultra)** (`/code-review ultra`, billed to your Claude account, so only the owner can start it, after a confirmation, and it opens in the run's terminal because Claude asks before it starts). A review runs on the job's branch in a scratch checkout, so your folder is never touched, and its text is saved with the job's documents. Each appears only when the scan found it and the tool is signed in.

**When a job stops because an AI tool's sign-in expired**, its failure card (titled *AI sign-in expired*) and job hero offer a **Sign in to Claude** button (or Codex, Antigravity or Pool, whichever tool the job was using). It opens the same guided sign-in dialog over the page without starting a separate job. As soon as sign-in succeeds, Orchestrator closes the dialog and **automatically retries the job** (or resumes any jobs waiting on that sign-in when signed in from Home's **Blocked** card or **Manage AI tools**).

The orchestrator can use model provider CLIs or API keys. The setup check looks for common CLIs and environment keys:

```bash
orchestrator check
```

Model definitions are bundled with the installed package. Registry sync is optional and only refreshes those defaults when a remote registry is available.
Live discovery can also query provider APIs from environment keys and can query Antigravity through the signed-in `agy` CLI; no `GEMINI_API_KEY` is required for that Antigravity path.

Practical options:

- Install and authenticate a CLI, such as `codex login`, `claude auth login`, or the equivalent command for your provider.
- Export API keys in the shell where the console runs, such as `OPENAI_API_KEY`, `GEMINI_API_KEY`, or `ANTHROPIC_API_KEY`.
- Store non-secret project settings in `.orchestrator/config/settings.json`.

Do not commit secrets.

## Configure Workers

Local worker config is generated automatically in `.orchestrator/config/machines.json`.

Example local machine:

```json
{
  "name": "local",
  "enabled": true,
  "execution_mode": "local",
  "ssh_target": null,
  "repo_path": "/Users/me/Documents/MyApp",
  "roles": ["planner", "reviewer", "worker", "build", "test"],
  "models": ["antigravity", "codex", "claude"],
  "priority": 100,
  "max_concurrent_jobs": 1,
  "max_heavy_jobs": 1,
  "supports_xcode": true,
  "supports_simulator": true,
  "supports_backend_tests": false,
  "interactive_reserved": true,
  "tags": ["interactive", "primary"]
}
```

Example SSH worker:

```json
{
  "name": "mac2",
  "enabled": true,
  "execution_mode": "ssh",
  "ssh_target": "mac2",
  "repo_path": "/Users/me/Documents/MyApp",
  "orchestrator_package_path": "~/.orchestrator/package",
  "orchestrator_runtime_dir": ".orchestrator",
  "roles": ["worker", "build", "test"],
  "models": ["codex", "antigravity"],
  "priority": 90,
  "max_concurrent_jobs": 1,
  "max_heavy_jobs": 1,
  "supports_xcode": true,
  "supports_simulator": true,
  "supports_backend_tests": false,
  "interactive_reserved": false,
  "tags": ["remote"]
}
```

Check worker readiness:

```bash
orchestrator worker-check
orchestrator worker-check --machine mac2
```

Install or refresh the package on an SSH worker:

```bash
orchestrator worker-install --machine mac2
```

`worker-install` copies the installed package source to the worker's `orchestrator_package_path` and verifies that the remote machine can import `orchestrator.scripts.worker_run`. Remote dispatch stops before syncing jobs if the package is missing.

## Prompt Instructions

Default role prompts live in the package:

```text
orchestrator/prompts/
```

The prompt files are role-specific rather than model-specific:

```text
planner_bug.md
planner_feature.md
planner_coverage.md
builder_bug.md
builder_feature_task.md
builder_infra.md
debug_agent.md
reviewer.md
verifier.md
build_checker.md
designer.md
```

To customize them per project, run the wizard with `--copy-prompt-overrides` or manually create:

```text
.orchestrator/prompts/
```

Project-local prompt files with matching names take precedence over package defaults, one file at a time: a prompt you haven't customised keeps using the built-in one.

In the Web UI, **Configuration → Instructions for AI helpers** lists the four roles you are most likely to tune. **Customize** opens the built-in instructions as the starting point, so you edit rather than write from nothing; **Reset** goes back to them, and saving the built-in text unchanged doesn't count as a customisation.

| Role | File | What it does |
|---|---|---|
| Architect | `verifier.md` | Reviews each plan before it is built (the "Senior Architect Review" on a job). |
| Planner | `planner_feature.md` | Researches the project and turns a request into tasks with acceptance criteria. |
| Builder | `builder_feature_task.md` | Writes the failing test, then the code, and reports what changed. |
| Reviewer | `reviewer.md` | Reviews the diff against the brief: blockers, medium-risk concerns, minor suggestions, verdict. |

Jobs read the Architect's, Planner's and Builder's replies as JSON, so their instructions must keep asking for those fields (`status` and `comments`; `tasks` and `acceptance_criteria`; `files_changed`, `test_command` and `summary`). Saving instructions that drop them is refused, with the missing fields named. The Reviewer replies in free text and has no such rule. The same instructions apply to every job in the project.

## Run The Console

From the Swift project root:

```bash
orchestrator console
```

The console stores runtime data under `.orchestrator/` by default. To run from another directory, set the project root:

```bash
SWIFT_ORCHESTRATOR_PROJECT_ROOT=/path/to/MyApp orchestrator console
```

## Create And Run Jobs

Use the console for normal job creation. For direct script access:

```bash
orchestrator script new_job.py bug
orchestrator script new_job.py feature
orchestrator script schedule_job.py .orchestrator/jobs/JOB.json
```

Generated job files are written under `.orchestrator/jobs/`. Logs and review output are written under `.orchestrator/logs/` and `.orchestrator/output/`.

### Jobs and runs

A **job** is a whole piece of work and its five stages: Plan, Build, Verify, Document and Finalize. A **run** is one go at part of it, started from the app: planning, a build, one fix attempt, a merge. A small job is often a single run from start to finish; a larger one has a run per stage and perhaps several fix attempts.

- **On the job page**, the status card (`#job-hero`) surfaces the live run directly with **View run** and **Stop** actions, the current `Now:` phase, the latest meaningful AI/tool activity line, and remaining disk space on the connected computer. The **Runs** card lists every run of the job, newest first, with what it did, how it ended and how long it took.
- **The job's stages show its runs.** The stage a run is working on says so ("Fix attempt 2 running"), a stage that took several runs shows how many, and the hero says which step the run is on. If a run fails before it can update the job, the job page says so and links to that run's log. When a job stops automatically (e.g. disk space, sign-in, or blocker), the status card focuses strictly on the stop reason and recovery action, while unanswered questions auto-expand in the **Questions** card with `Blocking` / `Non-blocking` labels and header flags (`blocking`, `non-blocking`, `auto-answered`).
- **On a run's page**, the line under the title says which job it belongs to and where it sits ("Run 3 of 5 of the job …", or "The only run of the job …"), with links to the runs before and after it. Back returns to the job.
- Runs are remembered after they end and after Orchestrator restarts, so a job keeps its full list.

### The job page

The status box at the top says where the job is and holds its one next step (Approve, Merge & complete, Run fix…). The
stage pills above it scroll to the card for that stage; Finalize scrolls to the status box itself.

- **Tell the AI what to change.** Below the status, one box for asking the AI for more, whatever the stage. Its button
  says what will happen:

  | When the job is | Button | What it does |
  |---|---|---|
  | Planned, not built yet | **Re-plan** | Re-plans with your note; you approve the new plan before anything is built |
  | Ready to merge | **Request changes** | Builds the change on the job's branch, runs the tests and updates the pull request; you review again before merging |
  | Failed or needing a fix | **Run fix** | Starts a fix attempt. Add any feedback in the job page's **Tell the AI what to change** field when it appears. |
  | Merged | **Reopen and change** | Reopens the job with your note and builds it |

  Enter sends, Shift+Enter starts a new line, and a half-written note survives the page refreshing itself. The box is
  hidden while a run works, while the AI waits for an answer (the question card takes it), and while you review a plan
  (the plan card has Revise).
- **Changes.** The card starts with **What changed**, the builder's own summary in full, then the changed files and their
  diffs. The status box keeps only the file count and the pull request link.
- **Documentation.** Lists the project's docs that concern this job: the ones it changed, then those that mention the
  files it changed, each with why. All project docs are a click away.
- **Screenshots.** When the job changed visual files and screens can be captured (see
  [UX And Design Review](#ux-and-design-review)), the UX and design check's screenshots are shown under Changes.

### Quick changes

A **Quick** job skips the full planning stage, but still gets a short **fast-tracked plan**: one model call writes one to
three tasks, the acceptance criteria, what it assumed, the files it will likely touch, and test cases, from your
description plus the project's PRD, architecture and docs. It never stops to ask questions. The Plan card marks it
**Fast-tracked**, and the build pass gets the plan. If that call fails, the job carries on as before, with your
description as the brief.

### When the build changes nothing

The Build step only counts as done when the builder changed files. If it finishes without changing any, the job stops as **Nothing was built**: it doesn't go on to test unchanged code or start fix attempts. The timeline marks Build with "No files changed", and **Run the build again** retries it. The usual cause is an AI tool that refused to edit files; the message says so when the builder reported it. Claude, when building or fixing, is allowed to edit files in the project (its other roles stay read-only). A fix attempt that changes nothing stops the same way, and an investigation may change nothing.

### Running a fix

The job page shows what failed and any available context. **Run fix** starts a repair directly. If **Tell the AI what to change** appears on the job page, you can add reproduction steps, what you saw, or what you expect. Leave it blank to use the recorded failure. If device logging is configured, the direct button requests the latest device log. The inline form lets you choose whether to include it.

The debugger reviews the brief, acceptance criteria, logs, and previous attempts. It proposes a change and the builder applies it on the job's existing branch. If the debugger hits something that genuinely requires a human decision, missing hardware/device captures, or subjective visual confirmation, it pauses with **Question for you** in the **Questions** card and raises an in-app notification banner (even in YOLO mode); replying there resumes the fix attempt directly. Orchestrator runs the build and tests again, then follows the job's normal review flow when they pass. An initial build or test failure can start one repair automatically. If that repair still fails, use **Run another fix** or add new evidence on the job page. Repeated hypotheses or unchanged failures can pause automatic fixing for review; running a manual **Run fix** (with or without new guidance) resets the pause gate so your next attempt runs fresh.

Before creating a new job, you can toggle **Use free AI only** directly on the **New job** form; for an existing job, open **⋯ (More) → Settings → Choose models…** to toggle **Use free AI only** (or pick individual role models) before the next run. On **Configuration → AI tools**, any provider whose API key was rejected shows **Check again** and **Dismiss** (hiding the notice while keeping that provider's broken models out of Ask AI).

For machine problems, follow the action shown on the job. Use **Free up space** for storage and **Sign in** when the AI tool's session expired. A rejected CLI flag such as `unexpected argument '--full-auto'` is an AI tool error. Check the Orchestrator backend and Codex CLI on the computer running the job before retrying. On a job that already passed its checks, use **Request changes** to refine the result or **Revise plan** to change the agreed scope.

See [the full fix workflow](job-recovery.md#how-a-fix-runs) for how attempts, blockers, and recovery are recorded.

## Running Several Jobs

You can start as many jobs as you like. This section explains what happens when they would get in each other's way.

### One job at a time in a project folder

Each job builds on its own branch, but on any one computer all jobs share the project folder. Before a job starts, it checks out its branch in that folder. If two jobs ran at once, each would switch the branch under the other and throw away the other's unsaved work. So **one job works in a project folder at a time**, and the others wait their turn:

- A job that has to wait shows **Waiting** on Home and on its page, with which job it's waiting for. Its log says the same: `Waiting for “Add login page” to finish`.
- It starts on its own when the job ahead finishes, stops or fails. You don't need to start it again.
- This covers every way of starting work: **Start**, **Run now**, **Resume**, **Run fix**, **YOLO all**, **Build the plan**, and the console. Jobs on different computers each have their own folder, so they run at the same time.
- While a job is working, **Switch branch**, **New branch** and **Pull** on Home and the Git page are refused, with the name of the job that's working. Use them once it finishes.
- The same job can't run twice at once. Starting it again says it already has a run in progress.

If a job crashes, the folder is free again at once. The computer releases the lock when the job's process ends, however it ends.

### Jobs at once

On the Git page, **Jobs at once** (1 to 4, default 1) lets more than one job build in a project together. It's the same number **Build the plan** uses, so raising it also builds independent features of a plan at the same time. Above 1, a job that would wait builds alongside in its own checkout instead: a separate copy of its branch, kept outside the project (in `~/.orchestrator/worktrees`), so your folder and its branch are never touched. The job page says so. Its checkout is removed when the job is merged, completed, discarded or deleted. One with uncommitted changes is only kept when you finish the job; discarding removes it.

Some jobs still wait their turn:

- **Jobs on the current branch** (or with branch switching off): git can't check out one branch in two places.
- **Jobs that change the same files as a running job**: building them side by side would end in conflicting branches.
- **A job whose project folder holds another job's uncommitted work.** It pauses as **Blocked** ("“…” has uncommitted work here") rather than take that work as its own, then starts by itself once that work is committed.

The job page also says when an open job **overlaps** another by files, with one plain next step for the closest one:

- **The same request twice** (the titles match, typos and all): "This looks like the same request as …". **Keep this one**
  archives the other (its branch and files stay; restore it from Home), and **Open the other** compares them first.
- **The other job is building now**: **Merge** waits, greyed out, until it finishes.
- **This job is ready to merge**: merge it first, then finish the other; its pull request may need updating.
- **The other job is ready to merge**: merge that one first, then come back.

Which files overlap is behind **Which files?**. A new job that looks like one still open asks before it's created, even with a typo in it.

### Jobs that look like the same work

Before a job is created, its description is compared with the jobs still open. If it looks like one of them, Orchestrator asks first and lists the matching jobs. *Add a login page* and *Login page with Google sign-in* match; *Fix the login bug* and *Add a login page* don't.

- Open a listed job to carry on with it, or choose **Don't create**.
- Choose **Create anyway** when it really is separate work.

The check compares the meaningful words of the descriptions, so it won't catch the same work described in completely different words. Jobs created automatically (from a plan run or a defect found by tests) aren't asked about.

### Jobs that change the same code

Two different jobs can still need to change the same files. The **Features** page flags features that claim the same paths, and jobs in progress that change the same file. Each job's branch is merged on its own, so the second one to merge may have a merge conflict to resolve first.

## Visual Simulator Checks

Visual checks require an app bundle and bundle identifier. Configure:

```json
{
  "app_bundle_id": "com.example.MyApp",
  "visual_app_path": "/absolute/path/to/MyApp.app"
}
```

If `visual_app_path` is omitted, the visual check flow searches the configured derived data path for a built `.app`.

## Unneeded Code Check

AI coding agents tend to add more than a change needs: a second copy of a helper the project already has, a function
nothing calls, the same block pasted twice, debug leftovers. Each one is small; together they are how a codebase gets
harder to change. The job page's **Unneeded code** card looks for them in what the job changed. It sits after the Scope
check, which asks a different question: whether the job stayed inside its plan.

The card appears once the job has a branch to compare with its base branch, and it reads only the lines the job
**added**, so debt that was already in the project is never counted against the job. It flags:

| Finding | Severity | What it means |
|---|---|---|
| **`name` already exists** | medium | A new top-level function, class or constant has the same name as one the project already defines elsewhere (the file is named). Call the existing one instead. Only definitions that start at the beginning of a line in source files outside test folders count, so a method of the same name, or code quoted inside a test, is not a duplicate. |
| **The same code was added twice** | medium | A run of five or more lines was added in two places, in one file or across files. Move it into one function. |
| **`name` is never used** | low | Nothing else in the project mentions a new top-level definition. Remove it, or wire it in if it was meant to be called. Functions with a decorator are skipped, since the decorator registers them. |
| **Debug leftovers** | low | `console.log`, `debugger`, `breakpoint()`, `pdb.set_trace()`, or a `FIXME`, `XXX` or `HACK` marker was added. |
| **Commented-out code** | low | Two or more added lines are code in comments. Git keeps the history, so delete them. |

Each finding gives the file and line. **Keep it** accepts a finding as intended; it is remembered on the job
(`cruft_accepted` in the job file) and counted in the card. **Create a job to remove it** opens a pre-filled quick job
that lists every open finding and asks for them to be fixed by deleting, reusing or merging, without adding anything.
**Reset kept** brings accepted findings back. The reviewer's instructions (`reviewer.md`) also ask it to search the
project for each new helper or dependency before accepting it, so the model review and this check back each other up.

### Telling the builder what already exists

The cheapest unneeded code to remove is the code never written. Before the builder starts a task, Orchestrator lists
the project's own top-level definitions (functions, classes, constants) whose names share words with the task's title,
description and acceptance criteria, and adds the closest eight to the builder's prompt under **EXISTING CODE TO
REUSE**. It also names any file the plan expects to change that already exists, so the builder extends it instead of
adding a parallel one. The prompt says the list is a guess to ignore where it doesn't apply, asks the builder to read
the closest entry and call or extend it before writing a new helper, class or dependency, and asks it to say why in its
summary if it adds one anyway.

It is plain name matching over a single `git grep`, so there is nothing to set up or keep up to date and no extra model
call (about 0.2 s on a project with two thousand definitions). It sees files from earlier tasks in the same job, even
before they are committed. Tests, lockfiles and non-source files are left out, as are bug fixes and investigations,
which edit existing code by nature. If the search fails the build carries on without the section.

The job records what it suggested (`reuse_suggested` in the job file), and the Unneeded code card reports it: "The
builder was pointed at 8 existing definitions; the change uses 3 (write_json_file, ...)". A name counts as used when the
added lines mention it other than by defining it, so a builder that writes its own second copy is not credited. Compare
the "uses" count across jobs to see whether the brief is helping.

How well it works, measured once: run over 40 recent commits of this project (stand-ins for jobs, 20 to 800 added
source lines each) with every finding checked by hand, the first version reported 11 findings, 8 of them wrong (it
matched JavaScript quoted inside Python tests, and methods with the same name). After the fixes above it reports 3: a
pasted block, an identical `read_cache` and a similar `write_cache`, which are the ones worth acting on. That is
high precision on a small sample from one project, and quiet by design: it examined 169 definitions across those
commits and found no unused ones and no leftovers. It has not yet been measured on your jobs or on a project with
more cruft. For the reuse list, the names it suggested for a commit's title were mentioned in that commit's changes
51% of the time, against 10% for names suggested for a different commit. That shows the suggestions are relevant, not
yet that they prevent duplicates.

What it deliberately does not do:

- **It is a prompt to look, never a gate.** Nothing blocks a merge, and unlike the Scope check the hero does not yet warn
  about it. Every check is a heuristic that errs toward staying quiet.
- **It judges a narrow slice.** Python, JavaScript and TypeScript, Swift, Go, Rust and Kotlin source files only; tests,
  lockfiles and Orchestrator's own files are skipped. Only top-level definitions with specific names (eight or more
  characters, with an underscore or inner capital, like `format_date` or `ScoreService`) are looked up, so ordinary
  method names that every class has are never flagged.
- **It matches names and text, not meaning.** A new function that does the same job under a different name is not
  found, and a repeated block inside a string (such as prompt text) can be flagged. On a very large change it reports
  more pasted blocks than a normal job would.
- **It does not remove or refactor anything itself.** Fixing a finding is a normal job you choose to create.

Lookups cost a Git call each (at most 40 per job), so the answer is kept until the job's branch moves. The code is
`orchestrator/cruft_check.py` (pure over the diff, with tests in `tests/test_cruft_check.py`), `job_cruft` in
`orchestrator/web/server.py`, and `orchestrator/reuse_brief.py` for the builder prompt (tests in
`tests/test_reuse_brief.py`).

## UX And Design Review

A checklist review of your product's screens, in two parts: **UX** (how it works: status, wording, one primary action,
progressive disclosure, navigation and a way back from every detail page, empty states, errors, forms, short action
labels, long content behind "Show more") and **Design** (how it looks: hierarchy, tokens, spacing, alignment, sizes,
section headers that read as titles, one header layout, consistent field labels, no unstyled browser controls,
overlap, responsiveness, contrast, dark mode, focus). The checklist is
`orchestrator/prompts/ux_reviewer.md`; your own UI conventions file, if you have one, takes precedence over it.

- **Product pass.** Web UI > UX review > Run a pass, or `orchestrator script ux_review_run.py pass`. It captures every
  configured screen, runs a UX pass and a design pass (two model calls), and writes one report with findings ranked by
  severity, the checklist, and the screenshots, under `.orchestrator/output/ux-pass/<when>/`. Each finding leads with the
  UX shortcoming it breaks (the checklist item, by name), then where it is, what's wrong and the fix, each in a sentence;
  **Fix top issues** turns the major ones into a job. A pass that couldn't capture screenshots finishes as "Completed with notes" (yellow)
  and says only the code was read; in a project with no interface files it skips the model calls and reports
  "Nothing to review".
- **Change check.** After the code review, every job whose diff touches interface files (HTML, CSS, JSX/TSX, Vue,
  Svelte, SwiftUI views, storyboards, scripts in UI folders, or anything matching `ui_review.paths`) gets the same
  checklist on just what it changed. The job page shows a "UX and design check" card, and the hero warns before you
  merge when something major was found. It is a prompt to look, never a gate.

Screens come from `ui_review` in `.orchestrator/project.json` (or Web UI > UX review > Screens). Until you save them, the
Screens form starts from what the project says it runs on: the product requirements' "Built for" line and its files (a
`package.json` with Vite, Next and the like gives the web address and start command; an Xcode project, iOS; an Android
`build.gradle`, the app id and build command), ticked and filled in for you to check and Save. Its options: a **design
rules file** (your spacing, type, colour and wording rules, which screens are checked against), **Check dark mode too**
(each screen again in dark mode, for contrast) and **Check each job's UI changes** (the change check above).

```json
"ui_review": {
  "url": "http://localhost:3000",
  "routes": ["/", "/settings"],
  "widths": [390, 1440],
  "dark_mode": true,
  "start_command": "npm run dev",
  "simulator": false,
  "review_changes": true,
  "conventions": "docs/ui-conventions.md",
  "paths": ["lib/theme/**"]
}
```

Web screens are captured with a headless Chrome, Chromium or Edge (set `ORCHESTRATOR_BROWSER` to choose one) using
device emulation, so 390 px is a real phone viewport, and dark mode through `prefers-color-scheme`. `$VARIABLES` in the
URL are read from the environment, so a sign-in token needn't be written into the file. `start_command` runs only if
the URL isn't answering, and is stopped afterwards. `simulator: true` adds the iOS simulator check's screenshots.
Without screens, the review works from the code and says what it couldn't judge. Screens are first-load views: no
scrolling, signing in or interaction.

Native apps need no URL. Set up any of these (together with, or instead of, `url`):

```json
"ui_review": {
  "simulator": true,
  "simulator_screens": ["myapp://settings", "myapp://profile"],
  "android": {
    "package": "com.example.app",
    "apk": "app/build/outputs/apk/debug/app-debug.apk",
    "build_command": "./gradlew assembleDebug",
    "avd": "Pixel_8_API_34",
    "screens": ["myapp://settings"]
  },
  "macos": { "app": "build/Debug/MyApp.app", "build_command": "xcodebuild -scheme MyApp build" }
}
```

- **iOS:** the simulator check launches the app, then opens each `simulator_screens` link (a URL scheme or universal
  link your app handles) and captures it, in light and dark.
- **Android:** needs `adb` (Android platform-tools, found on your PATH, in `ANDROID_HOME`, or in
  `~/Library/Android/sdk`). It uses a running emulator or device, or starts `avd`; installs `apk` (after
  `build_command`, if set), launches `package`, opens each link in `screens`, and captures light and dark.
- **Mac:** launches the built `.app` and captures its window in light mode. The app running Orchestrator needs Screen
  Recording permission (System Settings > Privacy & Security), or the capture is refused and the report says so.

Anything that fails is listed in the review's capture limits, and the review carries on from the code.

**Your own capture command.** For anything Orchestrator can't screenshot itself (a game engine, a desktop toolkit, a
hardware rig), give it a command that writes the pictures:

```json
"ui_review": {
  "capture": {
    "command": "python3 tools/capture_review.py --output \"$ORCHESTRATOR_CAPTURE_DIR\"",
    "timeout": 900
  }
}
```

The command runs through `bash` in the project folder, after any other targets, and must exit 0. Orchestrator sets:

| Variable | Value |
|---|---|
| `ORCHESTRATOR_CAPTURE_DIR` | an empty folder to write into (removed afterwards) |
| `ORCHESTRATOR_CAPTURE_JOB` | the job being checked, empty for a product pass |
| `ORCHESTRATOR_CAPTURE_DARK` | `1` when dark screens were asked for, else `0` |

Every `.png`, `.jpg` or `.webp` it writes there (subfolders included) becomes a screen: up to 20 images, 15 MB each.
Screens are named from the file name, or from a `captions.json` beside them (`{"eye-level.png": "Eye level, forest"}`); a
file ending in `-dark` or `_dark` counts as a dark screen. Files that aren't images are ignored. `timeout` is seconds
(10 to 3600, default 900); a command that runs over is stopped together with everything it started. If it fails, runs
out of time or writes nothing, the review's capture limits say so (with the command's last line of output) and
the review carries on from the code. Its images are not used if it exits with an error. The command comes from your own
`project.json`, like `build_command`, and runs with your permissions. Setting it turns off the devices found for you
(below). Saving the **Screens** form in the Web UI leaves `capture` and `paths` alone, so they live in the file.

**Devices found for you.** For a job's change check, when `ui_review` names no native target, the iOS simulator is used
if the project is an Xcode app and Xcode's tools are installed, and Android if the project has an app id and `adb` is
found. The capture limits say a device was picked this way. Setting `"simulator": false` (or `"android": false`) in
`ui_review` turns that off.

Models see the screenshots as attachments (Codex, opencode) or through their file tools (Claude, Gemini, agy); a model
that can't open images says so in the report's limits.

## Source control

The **Git setup** page (under *Build & ship*) starts with a **Source control** card. It opens with one line on why source control matters, then a dropdown of where your code can be kept:
GitHub, GitLab, Bitbucket, Azure DevOps, another Git host, or on this computer only.

- **Nothing chosen yet:** the dropdown says "Choose one…". Pick an option to see what it's good at, what to watch out for, how Orchestrator works with it, and the
  steps to set it up, with the buttons for those steps right there (Initialize repository, Sign in to GitHub, Create a GitHub repository, Connect a repository,
  Use this computer only).
- **Already connected:** the dropdown shows the current one. Pick another to see the steps to switch. Switching changes where jobs push; your history comes with you.
- **Help me set this up with AI** is optional. A model reads where the project is now (a repository or not, commits, remote, GitHub sign-in) and writes steps from there.
- GitHub gets the fullest workflow (an issue and a draft pull request per job). The others use plain Git: jobs work on branches and merge on this computer, with a link
  to open a review request.

## Architecture

Every project has one approved architecture: the language, frameworks, structure and packages it is built with, and why. It lives in a section of the **Product** page, above History, and it is saved in the project as `docs/architecture.json` (the record) and `docs/architecture.md` (the same decision written out, with a diagram). Every plan, plan review and build reads it as a constraint, so jobs stop re-guessing the stack.

**New projects.** After **Start a new project**, the next step is **Choose the architecture**. A model reads the product requirements, the platforms you picked and the build tools on this Mac, then suggests two or three options in plain words. Each option shows:

- **Best for:** the kind of product it suits, in one sentence.
- **Fit for your product:** a score out of 100 against your requirements.
- **A simple diagram:** who uses it, what they open, what runs behind it, where data lives, and outside services.
- **Good and Watch out:** everyday pros and cons.
- **Style mix:** weighted percentages in three fixed categories (system shape, code organisation and UI pattern), because real architectures are mixtures.

The recommended option is selected. Pick another if you prefer, adjust the language, framework or build and test commands under **Stack and commands**, or **Ask for changes** with a note. **Approve architecture** saves it and commits just those two files, leaving any other changes alone. None of this is required: every step can be skipped.

**Imported projects.** For a project that already has code, **Describe the architecture** reads a digest of the repository (languages, folders, manifests, packages, entry points and the README) and describes the architecture as it is now, with an approximate diagram and style mix. Nothing in the code changes. Confirm the description, and jobs follow it from then on.

**Tied to the product requirements.** Each decision lists the requirements it serves, and **How it serves your product requirements** maps each core need to the part of the architecture that meets it. **Not covered yet** lists needs it doesn't meet. When the requirements change after approval, the section and the Home card say so. **Check it against the requirements** then proposes only the adjustments the new requirements need.

**Skipping it.** **Skip for now** (or **Decide later** on a proposal) never leaves the project without an architecture. Orchestrator keeps a *working assumption*. With a proposal, that is the recommended option. Without one, it is read from the code, with no model involved. Working assumptions are versioned 0.1, 0.2 and so on, are saved and committed like an approved decision, and jobs follow them unless a request clearly needs otherwise. **Approve as it is** makes the assumption the chosen architecture, version 1.0, and so does approving any proposal. **Refresh from the code** updates an inferred assumption after the code changes, and Home flags when that has happened. A new project with nothing recorded at all only gets a note in its plans not to pick a stack silently.

**Changing it later.** Most changes are small and frequent; a few are large and rare:

- **Add or adjust (tweak):** a package, a module, a storage choice. The language, framework and overall shape stay. The version goes from 1.3 to 1.4.
- **Rethink the architecture (overhaul):** new options are weighed against staying with what you have. The version goes from 1.3 to 2.0, and the page warns that existing code may need rebuilding.

Orchestrator decides which kind a proposal is from what it changes, not from what the model calls it. A change to the primary language, the framework, the chosen option or the dominant system shape is an overhaul. Before you approve, **What would change** lists every difference, and **History** keeps each version with its changes.

The complete reference, including the style catalog, file formats and API, is `docs/architecture-step.md` in the Orchestrator repository.

## Packages

The **Packages** page (under *Build & ship*) lists the libraries and plugins this project uses, and ones worth adding. It's per project.

- **In this project** is read from the project's manifests: `package.json`, `requirements.txt`, `pyproject.toml`, `Package.swift`, `Cargo.toml`,
  `go.mod`, `Gemfile`, `Podfile`, `build.gradle`, Unity's `Packages/manifest.json` and `pubspec.yaml`. Nothing is run, and vendored folders are skipped.
- **Suggested packages appear on their own.** When your product requirements are set or change in a meaningful way (about a paragraph of new content, left unchanged for a minute or so; small tweaks and one-line additions don't count by themselves, but add up), a model reads them
  and your features, searches the web where it can, and suggests packages the project doesn't have yet, each tied to the feature it helps. Each is one line with a
  link: **Accept** it or **Reject** it. A rejected package isn't suggested again. The sidebar shows how many are waiting. The first time a project is looked at it only
  notes the requirements it has, so existing projects aren't reviewed until they change. Turn the automatic review off with the checkbox on the page, or run one any
  time with **Recommend packages**. It uses Claude Code or the Gemini CLI for the search; with any other model it answers from what it knows, and the page says
  versions may be out of date.
- **Accepted** packages are yours to manage: add your own with **Add package** (paste a link from npm, PyPI, crates.io, pub.dev, RubyGems, CocoaPods, pkg.go.dev, Maven or GitHub, and the name and ecosystem are filled in), edit, reject or remove. These are notes about the
  project; they never change your code.
- **Update** on an installed package uses its native package manager, then runs the project's configured test command. It makes no AI call. If either step fails, **Fix with AI** opens a repair job with the failed command and error filled in. Changes are kept for review.
- Direct updates support npm, pnpm, Bun, standalone Yarn, SwiftPM, Cargo, Go, uv, Poetry, simple pip requirements, CocoaPods, Bundler, Dart and Flutter. Poetry and pip require an existing isolated project-local `.venv`; pip requirements with includes, options, extras, markers or URLs need a planned update. Updates respect declared constraints and may adjust necessary transitive lock entries. Modern Yarn workspaces, Gradle and Unity offer **Update with AI** in the menu before any change. A missing test command prevents native updates until it is configured.
- **Add to project** and **Remove** open a prefilled New job because they can require code changes. Once a job installs a package, it appears under **In this project**.

## Firebase Delivery

Firebase delivery is opt-in:

```json
{
  "delivery_provider": "firebase",
  "firebase_distribution": true,
  "distribution_script_path": "scripts/distribute_ios.sh",
  "firebase_plist_path": "MyApp/GoogleService-Info.plist",
  "firebase_groups": "internal-testers"
}
```

`check-config` fails early if Firebase delivery is enabled but the distribution script or plist path is missing.

The delivery workflow archives and signs the iOS app, uploads the IPA to Firebase App Distribution, and releases it to configured tester emails or groups. If no recipients are configured, Orchestrator uses the `internal-testers` group. Each successful delivery writes a receipt under `.orchestrator/output/delivery/` with the app version, build number, IPA path, recipients, and SHA-256 checksum.

In the console, use **Quick Build & Distribution** to deliver the current branch. The live delivery test under **Firebase App Distro** also publishes a real release; it is not a dry run. A delivery job targeting another branch stops and asks you to switch explicitly so local changes are never discarded.

For an end-to-end workflow from iPhone or iPad, use [Secure ShellFish](https://secureshellfish.app/) to connect over SSH to the machine running Orchestrator. You can manage the coding workflow remotely, run builds and tests, and trigger Firebase delivery from the same terminal session.

## Web UI

The Web UI is how most people use Orchestrator. The work runs on your own computer; you reach it from a browser at **https://swift-orch-web-20260923.web.app**, which never changes. Your code, files and assets stay on your computer, never on our servers: the hosted site only knows your account, which computers are on it, and where to reach them (see [Control plane](control-plane.md)). Home starts with a short setup checklist until the basics are done, then shows what needs you next.

> **Setup checklist.** Five things make a computer ready: a project, build & test commands (configured, documented, or auto-detected), Git, an AI model (a provider you can use, with a model selected), and an enabled machine. Each row has one button that opens the right place, and the rest, such as role instructions and screens to review, is optional.

> **Copy setup from another computer.** When another computer on your account has already set this project up further, the checklist offers **Copy setup** with a list of those computers (the furthest along first). It copies build and test commands, the code host, delivery and Firebase choices, notification emails, model choices and custom role prompts. It never copies API keys, passwords, signing keys or folder paths, and sign-ins (GitHub CLI, an AI tool, Xcode's licence) are per computer. After a copy those steps stay to do on this computer, marked "Done on <computer>", so the shield only turns green when this computer can really run a job. Computers share this through your account (see [Control plane](control-plane.md)); a computer that isn't signed in to an account shares nothing.

> **Master config sync.** For computers signed into the same account, the first configured computer (or the most recent one where settings are saved or **Set as master** is clicked in the Setup panel) becomes the project's shared master config. Other computers automatically pull newer portable settings (`project.json`, `settings.json`, `prompts/*.md`) when checking setup or clicking **Configure** / **Sync master config**. While syncing or pushing, the Setup panel shows a live progress status and surfaces any sync error inline and as an error toast.

> **Product requirements.** Each project has one short document, `docs/product/prd.md`, that every planner, builder and reviewer reads first. You can write it, import one you already have (it is summarised into the sections, and the full file stays attached for jobs to read), or have it drafted from an existing project, and it is kept up to date as jobs finish, with a history you can undo. Every section is optional. Once there is something in it, **Break down** splits it into epics to approve or decline. Your computers share the document by default, through the project's own repository (a hidden ref, not Orchestrator's servers), so anyone who can read that repository can read it. You choose when you set the project up (a public repository asks first), and can change it on the Product page (**Stop sharing**). An edit on one computer appears on the others within seconds, as a version in each one's history, announced like an automatic update and undoable; designs and source documents travel too, and so do jobs (with their own **Stop sharing jobs**). If you changed the same section on two computers, both texts are kept (the other computer's marked **Also written on** its name) for you to tidy. A project with no Git remote stays on its computer. See [Sharing project state between computers](state-sync.md). See [Epics](#epics) and [Product requirements](product-requirements.md).

> **New project.** The wizard asks for a name and platforms, then one question for each optional section of the product requirements, including project success measures. Everything after the name and platforms can be skipped. Its last step offers **Break it down** into epics, and can be skipped.

> **Documentation.** There is no Docs page. Orchestrator's own guides are on the **Help** page. A job's page, written from the live project (what it is, what was decided, what was built, how it was tested, what the review said), is under **Documentation** in the job's More menu, and the PRD's Markdown file is linked from the Product page. See [Product requirements](product-requirements.md) for the PRD.

`orchestrator ui` is the part that runs on your computer. With the one-command install it starts at login and keeps a tunnel open, so you rarely run it yourself. To run it by hand, or to use it only on this computer, start it for the current project:

```bash
orchestrator ui                  # http://127.0.0.1:8765/?token=...
orchestrator ui --project Thirteen --port 9000 --no-open
```

- **Home** is the project at a glance. See [Home](#home) below for each part.
- **Jobs** filters by Needs you / In progress / Done. A job page states what it's waiting on, shows one primary action for its status (Start, Run now, Answer, Run fix, Merge & complete), and puts everything else under More. **Deliver to testers** builds *that job's* branch. **Merge & complete** merges the PR, deletes the AI branch and archives the job, the same as in the console.
- **Device logs** lists recent app launches from the log store, with Pull and Follow buttons (see [Device Logs](#device-logs)).
- **Analytics** shows this project's connected analytics platform, feature KPIs and manually logged results, plus project success measures from the PRD. Link a feature to a job to pass its KPIs into planning; a job report then names the event tests and their results. Orchestrator does not import KPI values from the analytics provider.
- A job page also has the approvals the planning loop needs (**Approve plan**, **Approve design**, **Accept suggestions** from the architect review), **Revise plan** to re-plan with requested changes, the job's **Changes** (why, the files it touched, and a diffstat against the base branch), its **Brief** / builder summary / investigations, and links to its issue and PR on GitHub.
- **Tests** (under Tools, or Home → More) lists test plans and suites with a Run button each, runs everything, and measures coverage. A coverage number is marked *Estimate* when measuring failed and the console fell back to an estimate from the test count.
- **Git** shows the branch, what there is to push and pull, and uncommitted files, with Pull, Push (with confirmation), Switch branch and New branch. Home's branch name links here.
- **Home → More** also has Setup check, Config check, Worker check and the Setup wizard.
- **Activity** lists every command started from the UI. Runs started from a job are named after it and appear on that job's page. When a run finishes, it offers the next step, such as **Open job** after creating one.
- **A run's page** keeps the status box at the top that says what the run is doing now (including remaining disk space on the current computer), or what to do next once it ends. Task progress advances monotonically as real AI/tool activity is observed within a step rather than waiting at 0% until the build phase. Below it, **Timeline** shows the run as steps, such as *Preparing git branch*, each task of the plan, *Building project* and *Running tests*:
  - each step shows how long it took and, for a task, which one it is ("2 of 3");
  - the AI model calls made during a step are listed under it ("Asked claude-sonnet-4-6 · 48s"), including a model that timed out and was replaced;
  - while a step is running, up to 3 recent meaningful AI/tool activity updates (MCP calls, shell commands, build/import steps) appear under the active model call, with spinner and hook noise filtered out;
  - error lines in a step's output are counted beside it;
  - open a step to see just its own output;
  - a failed run opens the step it failed in.

  **Terminal** shows the full output as it was printed. A run that asks something switches to the Terminal by itself, so you can answer it there.

Pages refresh themselves every few seconds, but never while you're typing, have a menu open, or have a dialog up. Leaving a page closes any dialog that was open on it.

### Home

From the top:

- **Top bar.** The project's name, which is also a project switcher. On the right are the **setup health** button and **New job**. The health button is a round button with a coloured light: green when everything required is set up, red when something required is missing, and half green, half amber when only optional items are left. Tap it for the checklist, with a button beside each item that isn't done. On a phone it sits at the right of the header bar, and New job is in the tab bar.
- **Branch.** The branch picker, with the **Ask AI** chat button opposite it (see [Ask AI](#ask-ai)).
- **Project status.** Three short lines:
  - **Last commit:** its message and when it was made. Hover over it for the commit's hash and author.
  - **Last job run:** the job that ran most recently, what state it's in now, and how long ago.
  - **Last success:** the most recent job that built successfully (ready to review or merge, or finished). It's only shown when it isn't the same job as the last run.

  When jobs are waiting on you, the card says how many, for example **2 jobs need you**. Tap that to filter the list to them and scroll down to it, without reloading the page. While runs are going, it says how many are in progress.
- **Preview** (web projects). The app as it is on the current branch. See [Live preview](#live-preview).
- **Jobs.** Everything not finished, filtered by **All**, **Needs you**, **In progress** and **Backlog**. Each row shows the one thing that moves it forward. Finished jobs are listed below, and archived ones under that. **Archive** (swipe a row, or the job's menu) puts a job away with its branch and files kept; a job with a run going can't be archived, and Home says so. Archiving or restoring a job does the same on all your computers.

The sidebar's **Primary machine** dropdown switches between your computers. The one marked **Primary** starts this project's jobs. Another computer can look at the project, but it can't start work until it's made primary.

**Jobs on your other computers.** Home also lists the jobs your other computers run in this project, with the same state their own job page shows ("Blocked: Git is locked", "Tests passed, not committed", the run working now and its step), usually within 10 seconds of a change. The computer running a job is its one source of truth; your account only holds a read-only copy for the others. While a job runs, each finished task is pushed to the job's own branch (never the base branch), so if that computer fails, at most the task in progress is lost. Opening an idle job from another online computer automatically brings the job's latest record, branch, and unfinished work here and opens it locally (toast: *Latest job info synced*). When a run is actively executing on another online computer, the job offers **Switch to `<Machine>`** (connects directly to that computer and opens the live job) or **Stop run & take over here** (stops the remote run after confirmation, waits for it to exit, pulls the latest post-stop record, and opens it locally).

### When a computer is offline

Your other computers never need the one that has a job to be online: what a job needs is on your account or on GitHub
before that computer goes away.

- **One owner per job.** Each job has an ownership number; picking a job up on another computer raises it. The computer holding the
  highest number owns the job, and any other copy steps aside (**Last worked on <computer>**), even if its computer was offline during
  the move and syncs late. Copies brought over by an old project download never win.
- **Machine-agnostic switching.** Opening an online, idle job from another computer automatically brings it to the machine
  you're using (latest record, branch and unfinished work) and opens it locally.
  When the other computer is offline, its page pauses to warn about any unsynced work before you click **Work on it here** (or its
  next step). When another online computer is actively running the job, you can either **Switch to `<Machine>`** to follow it live or **Stop run & take over here** to stop the remote run and continue locally.
- **Every job is reachable.** Each computer sends your account a record of every job that changed, not only the newest
  ones, so you can continue any unfinished job from another computer.
- **Unfinished work.** About once a minute, a paused or failed job's uncommitted files are backed up to GitHub, on a
  hidden ref of the job's own (your files, what's staged and the branch are never touched). Picking the job up elsewhere
  brings them back too, unless that would overwrite files changed there; then the job page offers **Bring the unfinished
  work here** once those are dealt with.
- **Freshness.** A job on another computer says how fresh it is ("on MacBook Pro (offline, synced 3h ago)"), and its
  page warns that an offline computer may have newer work it hasn't uploaded, and whether its unfinished files came
  along.
- **Nothing is lost on reconnect.** A computer that comes back with commits or files it never uploaded, for a job that
  moved on without it, saves them to a side branch, `<branch>-from-<computer>`, and the job's page there says where.
- **Archive and Restore** apply on every computer.

**When syncing fails.** If this computer can't check in with your account, send its job updates, or back up a job's
branch or unfinished work for more than 10 minutes, Home shows a warning: since when, what your other computers see
instead, and what to do (back online, sign in again, or fix the GitHub sign-in). **Sync details** lists each kind of
sync and when it last worked. A job whose latest commits or unfinished files couldn't be backed up says so on its page.
Brief blips (a laptop waking, a Wi-Fi hop) aren't shown, and everything retries by itself.

The one thing no design can fix: work done while a computer was cut off reaches the others only once it reconnects.

### Job page

Each job page is focused on getting the feature realized and reviewed, with plumbing tucked out of the way:

- **Agile iterative lifecycle stepper.** Shows the current iteration badge (`↻ Iter N`) alongside **Plan → Build → Verify → Document → Decide** (automatically omitting **Document** on quick, bug, and tests-only jobs when no docs are affected). **Plan** stays approved across fix and change cycles; when a new iteration loops back into **Build** or **Verify**, downstream stages partially reset (`Pending re-verify` / `Pending re-check`) until the new iteration passes. In **Decide**, the hero shows the recommended next action (release/merge) alongside direct routing controls: **Build again** (fix an implementation issue while keeping the goal), **Frame again** (revise the goal/plan before editing code), **Try again** (re-verify without code changes when evidence is inconclusive), or **Pause / Close** (archive and move on).
- **Hero status & review summary.** Shows the job's current state and primary action. Paused (dead-worker) and failed jobs surface the actual error (`last_error`, unreported run failure headline, or structured problem summary) while skipping already-cleared blockers (such as resolved disk-space failures), and starting a new run clears stale `last_error`. If the last run failed before updating the job, a warning banner shows its headline and links straight to **Details**. When ready for review, a structured summary separates the file count and `+add/-del` diffstat, compact file chips, and **Inspect diff ↓** / **Open PR ↗** actions.
- **Tasks & Runs.** **Tasks** appear above Plan with completion, details, editing controls, and how many runs worked on each task (unattributed work runs attach to the job's current task). Feature-task runs also show each task's outcome and the checks or changed files behind it. Directly below Tasks, the **Runs** table uses the same layout as the Jobs table, showing the latest 5 runs with a **Show more** button up to 25 runs.
- **Plan.** The opening bullets summarize the job's scope, acceptance criteria, and constraints. **Full brief** opens the complete Markdown and lets you download it. **Edit brief** updates the saved brief the reviewer reads. When the job is ready for a revision, select **Revise the plan using this brief** to send your edits to the planner. Approve the revised plan before the builder runs it.
- **Categorized More (`⋯`) menu.** Actions are grouped under **Work on this job**, **Share & export**, **Settings**, and **Finish or remove** (only showing groups with available actions). **Download ZIP** fetches the job bundle with authentication whether you're using localhost or the hosted web app.
- **Lean behind-the-scenes execution.** Non-overlapping jobs run up to 2 at once in isolated git worktrees by default; internal `.orchestrator/` and AI-tool dotfiles (`.claude/`, `.codex/`, `.gemini/`, `.poolside/`, `.cursor/`) are filtered out of review diffs and unsaved-file counts; fix loops pass a compact **Iteration Ledger** (ruled-out hypotheses, failing tests, modified files) so models don't re-explore dead ends; and planners follow YAGNI minimal vertical-slice scoping.

### Updates, local Ollama models, disk cleanup & navigation

- **Updates & machine failover (`Settings → Updates` & `Settings → Machines`).** `Settings → Updates` shows all paired computers in a **Machines** table (`Machine`, `Version`, and right-hand action slot with a disabled **Up to date** button or active **Update** button, plus a **Local** badge on the connected computer). Opening the page automatically checks every online computer for updates. Clicking **Update** or **Update all** displays an inline spinner and progress banner, then polls for the restarted server and refreshes automatically. Auto-update toggles (**Auto-install minor updates**, checked every 5 minutes when idle, and **Auto-install breaking updates**) sync across all online paired computers. When a job stops on a machine blocker (low disk space — whether from a structured blocker, `last_error`, or a failed run headline — expired sign-in, missing tool, or simulator) and another online paired Mac has the same project with ≥15 GB free, the job hero offers **Free up space** and a 1-click **`Run on <Mac>`** failover button; enabling **Auto-handoff jobs on low disk space** in **Settings → Machines** automatically moves and runs those jobs on the ready peer.
- **Local Ollama models (`Settings → AI & models` & Home).** When Ollama is installed, **Download & manage local models** opens a modal listing installed models (via Ollama's `GET /api/tags`) with one-click **Remove** and a curated list of downloadable coding/general models with live download sizes fetched from `registry.ollama.ai` manifests, a size filter, a free-space check, and a custom tag download input. On **Home**, Orchestrator also recommends up to 2 free local models sized to fit the connected computer's RAM while preserving at least 15 GiB of free disk space for builds. Pulled Ollama models (including `ollama/`-prefixed IDs) work directly in planner, builder, reviewer, and Ask AI runs.
- **Disk cleanup (`Free up disk space`).** Recognizes and safely clears Xcode `DerivedData` (including `CompilationCache.noindex` and `Index.noindex`), iOS Simulator caches, VS Code caches, per-project Unity/Gradle/SwiftPM caches, and global developer caches (`npm`/`npx`, `pip`, `uv`, `Homebrew`, `CocoaPods`, `SwiftPM`, `Hugging Face`).
- **Sidebar & connectors.** **Xcode Cloud** lives under **Build & ship**; **Git setup** lives under **Configuration → This project** (with simplified 3-step source control instructions, collapsed pros/trade-offs, and a yellow warning with explanation for missing GitHub job labels); **Tester build setup** is linked directly from **App delivery**. Connector modals show a primary blue **Login ↗** button with divider spacing and a **`Get token ↗`** link next to every token field, and **Sentry** auto-detects your organization slug from your auth token (`GET /api/0/organizations/`) so you don't need to enter it manually.

### Ask AI

The chat button on Home opens a conversation about the project. Ask anything: what the product is, what changed last week, what's left in the requirements, or why something was built the way it was. It can't change code or files. When something should change, it says what, and you start a job for it.

Choose the kind of AI at the top of the panel. Each option shows the model it will use on this computer:

| Option | Which models | Good for |
|---|---|---|
| **Free** | Models that cost nothing to run: local ones (Ollama), free hosted ones (OpenCode), or ones a subscription already includes | Quick questions. Answers may be simpler |
| **Balanced** | Good paid models that aren't the most expensive, such as Claude Sonnet | Most questions |
| **Complex** | The strongest models, such as Claude Opus | Hard questions. Costs the most |

Only models that are ready on this computer count. An option with nothing set up is greyed out and says what to set up. Your choice is remembered. If the chosen option's model doesn't answer, up to two others **of the same kind** are tried. A Free question never ends up answered by a paid model: if no free model answers, you get an error, not a more expensive answer.

Every question goes to the model with the project as it is right now, so whichever model answers knows the same things:

- the product requirements and the documents attached to them;
- the README, AGENTS.md and the files under `docs/`;
- the current branch, recent commits, uncommitted changes and branches;
- the jobs;
- the conversation so far.

Free models get a smaller share of all this, because they handle less text. The conversation is saved with the project, so it's there on any device. **Clear** removes it for everyone on the project, so it takes a second tap. While an answer is coming, it shows which model is working and for how long, and **Stop** cancels it.

### Live preview

For a web project, choose **Preview** beside the chat icon in the upper right of Home. The preview opens in its own full-screen view and shows the app running from the branch that's checked out now, including unsaved work. It starts by itself when you open it. Choose **Close** or use your browser's Back button to return. The frame says what's going on:

- **Starting live preview:** the dev server is starting. **Show log** shows its output.
- **Live preview couldn't start:** **Try again**, or **Show log** to see why.
- **Live preview stopped responding:** **Try again** restarts the dev server, or **Dismiss**.
- **The preview isn't running:** **Start live preview**.

The globe icon above the frame opens **Public link**, where you can publish or update a shared link, copy its address, see its expiry date, and choose whether to publish automatically when a job is ready for review. A public link uses the last commit; uncommitted work stays in your private live preview.

To report a problem, choose **Point out a problem**, then click the part of the page that's wrong and **Add to message**. It becomes a chip, with a small screenshot, above the text box. Remove a chip with ×. Describe what's wrong and **Send**. It becomes a new job, planned straight away, and you stay in the preview. **Follow it** in the message opens the job. Problems the page detects by itself, such as errors in the browser console, are listed under the frame with **Fix with AI**.

The live preview isn't a job: it isn't counted among running work or listed on Activity.

If Preview needs a newer Orchestrator, a supported Mac app can update automatically. A modal shows the computer's reported progress, waits for running jobs, and reconnects before trying Preview again. **Cancel update** cancels the request on the computer; the modal stays on **Cancelling…** until the computer confirms. During final installation, cancellation is disabled so the app can finish safely.

Older CLI installations and Mac apps without the cancellable updater need a one-time upgrade first. The modal directs you to **Computers** rather than claiming an update has started. A failed, cancelled, or unavailable update does not start over by itself; **Try update again** explicitly starts another attempt when supported.

### Adding an existing project

**Add existing project**, on the Projects page, has three tabs:

- **Discovered:** projects already on this computer. It looks up to three folders deep in `~/Developer`, `~/Projects`, `~/Documents`, `~/Code`, `~/src`, `~/Desktop` and your home folder. It finds Git repositories, Xcode and Swift projects, and folders already set up for Orchestrator. Several folders of one project, such as its worktrees, are listed by folder name, and a worktree says which checkout it belongs to. Projects you've already added aren't listed. Looking usually takes a few seconds. If it takes more than a minute it stops and offers **Try again**.
- **GitHub:** the repositories your GitHub account and its organisations can open, newest first, private ones included. Ones already on this computer are marked **On this computer**. **Clone & add** downloads one into `~/Projects`, or `~/Developer` if you use that, and adds it. It never downloads over an existing folder. This needs the GitHub CLI (`gh`) signed in on the computer.
- **Custom Path:** type the path of any folder.

A project found in a folder that isn't set up for Orchestrator yet goes through the setup steps first.

Something not listed? Check whether it's deeper than three folders, inside another Git repository, or in a folder macOS hasn't let Orchestrator see (Documents or Desktop). Add it under **Custom Path**.

### Messages

What the app has to tell you appears in the message bar at the top of the page: something you did worked, something failed, or a note. Errors are written in plain words with what to do next. Sign-in errors are reworded too: for example, "This sign-in method isn't turned on yet" instead of Firebase's own text. Each message has a close button, and hovering over one keeps it open.

Every action runs the same CLI command you would type, inside a real terminal that is shown in the page. Prompts that need you (y/n, menus, pasted logs, passwords) appear there, and you answer them by typing into the terminal. On a phone, use the key bar under it (Enter, Esc, arrows, y/n/q, Ctrl-C, Ctrl-D). **Open full console** runs the regular `orchestrator console` in the browser for what has no page of its own: Ask AI about changes, reset & rerun, discard & revert, export context, model and API-key settings, the machine fleet, email recipients, Xcode Cloud and archived jobs. Each run's output is also saved to `.orchestrator/logs/ui/`.

Use it from anywhere:

Quickest on your Mac: run `curl -fsSL https://swift-orch-web-20260923.web.app/install.sh | sh`. It installs what's missing (Python 3.11+, Git, pipx, cloudflared), installs Orchestrator, shows the code to confirm, and starts Orchestrator at login. Add `--no-service` to skip the login start, or `--dry-run` to only print what it would do. The steps below do the same by hand.

1. On the computer with your projects, run `orchestrator connect`. It shows a code and a link.
2. Open the link (or go to https://swift-orch-web-20260923.web.app and enter the code), sign in with Google, and confirm. The computer is now on your account.
3. Start `orchestrator ui --tunnel` on it. Signing in to the hosted app from any browser opens it; with more than one computer you choose which. **Configuration → Your computers** switches between them.

`orchestrator connect --status` shows which account the computer is on, and `orchestrator disconnect` removes it. See [Control plane](control-plane.md) for how it works.

Access and security:

- The server listens on `127.0.0.1` only. The printed URL carries the access token, kept in `~/.orchestrator/ui_token` (delete the file and restart to change it). Opening the URL once stores the token in a cookie, and every API call needs it. The access token is the owner's key: anyone holding it can do everything, so don't share it.
- On the hosted app, a link can never supply a backend address or an access token: it only talks to computers your account vouches for, and the sign-in token for your account goes only to those. A link with `?backend=` or `?token=` in it is ignored there. (On a computer's own address, `?token=` still works, as above.)
- Signing in with Google, Apple, GitHub or email works for the emails listed under **Configuration → Who can sign in**: your git `user.email`, `ORCHESTRATOR_ALLOWED_EMAILS`, `allowed_emails` in `project.json`, and emails added on that page. Each sign-in gets its own token, never the access token. It lasts 30 days, is listed on that page, and stops working as soon as its email is no longer allowed. Only a hash of each is stored, in `~/.orchestrator/ui_sign_ins.json`.
- Browsers can call the server only from its own pages, pages on this computer, and the hosted app. Add other origins (a custom domain, say) with `ORCHESTRATOR_ALLOWED_ORIGINS`, comma-separated.
- The browser can only start a fixed set of actions. It never sends a command line, and file reads are limited to `.orchestrator/`.
- **One address from anywhere:** connect this computer to your account once (`orchestrator connect`, or the install
  command above) and run `orchestrator ui --tunnel`. Then https://swift-orch-web-20260923.web.app is the address to use
  on any device: it never changes, and after you sign in it finds this computer, because the computer reports its
  current tunnel address every 60 seconds. The tunnel's own `trycloudflare.com` link changes on every restart. When the
  page loses the computer it keeps retrying quietly and looks the computer up again by account, so a changed tunnel
  address needs nothing from you; a notice appears only if the computer stays unreachable. The startup output no longer
  prints a link with your token in it.
- **Starting at login:** `orchestrator service install` runs `orchestrator ui --tunnel` as a background service, and
  `orchestrator service uninstall` removes it. `orchestrator service status` shows whether it is running.
- From a phone without an account, either tunnel over SSH: `ssh -L 8765:127.0.0.1:8765 <mac>`, then open the printed URL on the phone. Or bind to a private network address such as Tailscale: `orchestrator ui --host 100.x.y.z`. Don't bind to a public interface.
- Stopping the server (Ctrl-C) also stops the commands it started.

## Epics

**Epics** (the second half of the Product page, below the requirements) breaks the product requirements down into epics, the big pieces of the product, for you to approve or decline. It is offered once, the first time the requirements have something in them, as the last step of the new project wizard, and is always there at the bottom of the Product page.

- **Break down from the PRD** uses the technique that fits what you've written (a story map for the flows people follow, a top-down work breakdown for technical products, a short list when there's little to go on), or the one you pick in the Technique menu. A short PRD gets a few broad epics and questions; a detailed one gets a finer breakdown with requirements.
- **Approve** the ones you want (ticking one ticks what it builds on). Each approved epic becomes a feature and a **job marked Epic** that waits in the Backlog filter on Home. **Make a plan** runs the usual planning for it; nothing runs, and nothing is asked of you, until you do. Once the plan is written the epic says **Plan ready**, and **Review plan** opens it for your approval. Declined epics are kept and can be brought back.
- **Build the plan** (on the Approved card) plans and builds several approved epics for you, in the order they depend on each other, with the MVP ones ticked to start. Epics that don't depend on each other are built at the same time, up to **Jobs at once**. Tick **Approve plans automatically** to skip approving each plan (a plan with a question or the architect's concerns still waits). Its progress shows above the Approved card, with **Pause** and **Stop**; stopping starts nothing new and leaves running jobs to finish.
- **Check together** (on the plan's progress card) merges all of the plan's job branches in a scratch checkout (yours isn't touched) and runs the build and tests on the result, since each job only passed on its own. It names any branches that conflict, and says if the combination fails. When they work together, the combination is kept as the branch `integration/plan-<id>` and **Send this build to testers** sends it to your device through Firebase App Distribution, the same as **Distribute**. A build that is out of date (a branch or the base moved since the check) or didn't pass is never offered. With only one branch so far, that branch is the build. Sending a build switches the project folder to its branch while it builds, then back; it waits while a job is working in the folder.
- **Jira.** With Jira connected and a project key set, **Send approved epics to Jira** creates them there (a Jira icon and key then appear on the epic and its job), and **Import epics from Jira** brings the project's open epics in as approved epics with waiting jobs. Only the owner of the computer can do either, and nothing is sent until you press the button. See [Product requirements](product-requirements.md#jira).

## Device Logs

Builds running on a phone (Firebase App Distribution, TestFlight) can't be read over a cable from an SSH session, so the app ships its logs to a central store and Orchestrator pulls them back. Sentry Logs is the supported store: the app sends each log entry with a per-launch `app_session` attribute, plus one `remote_log.session_start` entry per launch carrying its build channel, version and build.

### Connecting Sentry from the web app

Under **Connections → Sentry**, **Sign in with Sentry** opens Sentry with a code filled in. Check that the code matches the one Orchestrator shows, pick your organization, and approve. Orchestrator then saves the connection itself: there's no token to create, and the organization comes with the sign-in. It asks for `org:read`, `project:read`, `event:read` and `event:write` (`event:write` lets jobs comment on and resolve issues). Sentry's sign-in tokens last 30 days; the usual reminder appears before it runs out, and you sign in again. Device Logs uses the same connection.

Self-hosted Sentry, or a computer where sign-in isn't set up, uses **Advanced: use a token instead**: a personal token with those scopes, plus the organization slug.

Sign-in needs Orchestrator registered once as a public OAuth application on sentry.io (in Sentry's developer or API application settings). It needs no redirect URL, because the device flow doesn't use one. Put its client ID in `ORCHESTRATOR_SENTRY_CLIENT_ID`, in the `sentry_client_id` setting, or in `CLIENT_ID` in `orchestrator/sentry_device.py` to turn it on for everyone. Orchestrator never needs the client secret.

### From the command line

One-time setup, from the project root:

```bash
orchestrator logs setup
```

This finds the Sentry DSN in the repo, writes a `remote_logs` block to `.orchestrator/project.json`, asks for a read token and saves it to `.orchestrator/.env` (chmod 600), then makes a live query to confirm access. Create the token under Sentry **User Settings → Personal Tokens** with scopes `org:read`, `project:read` and `event:read`. It is stored as `SENTRY_LOGS_TOKEN` because `SENTRY_AUTH_TOKEN` is usually an upload-only token for dSYMs.

```json
{
  "remote_logs": {
    "provider": "sentry",
    "api_base": "https://us.sentry.io",
    "org": "4510342215434240",
    "project": "4510342216941568",
    "token_env": "SENTRY_LOGS_TOKEN",
    "session_attribute": "app_session"
  }
}
```

Day to day:

```bash
orchestrator logs sessions                        # recent app launches: id, time, channel, version, device
orchestrator logs pull --latest                   # newest launch -> .orchestrator/output/cloud_logs/<ts>-<session>/cloud.log
orchestrator logs pull --session 1a2b3c4d --level warn --query 'category:LobbyViewModel'
orchestrator logs tail                            # follow the newest launch live (polls every 5s)
```

In the console, **Link Logs → Pull Device Logs (cloud)...** lists recent launches and links the pulled logs to the job. Choosing **Always newest launch** links `cloud:latest` instead, which re-pulls the newest launch on every debug iteration — reproduce on the phone, then rerun the debug loop without relinking. `cloud:latest` and `cloud:<session>` also work anywhere `debug_job.py --logs` takes a path.

Logs reach Sentry within about 5 seconds, and immediately when the app is backgrounded. Sentry keeps them for 30 days.

## Adaptive Planning & Prototype-First Experience Loop (`Build → Experience → Refine`)

Orchestrator combines an **Adaptive Specification & Planning Pipeline** with a **Prototype-First Experience Loop**:

1. **Say It Your Way (Default Entry Point)**:
   - The **New job** page defaults to **Freestyle ("Say it your way")**, accepting anything from a one-sentence idea to a complete PRD with attachments, code examples, and design links.
2. **Adaptive Rolling-Wave Planning (Single-Pass Fast-Track + Two-Pass Epics)**:
   - **Pass A (Living Feature Spec & Ambiguity Assessment)**: Synthesizes a versioned `LivingFeatureSpec` (`.orchestrator/output/<job_id>/spec.json` and `spec.md`) with provenance tags (`[explicit]`, `[code_derived]`, `[inferred]`), checks for outdated/renamed repository paths, and evaluates uncertain decisions across **Confidence**, **Impact**, and **Reversibility**. Following a strict **Hierarchy of Truth (`Code & Manifests > README / Comments`)**, discrepancies between `README.md` or doc labels and verified repository code, manifests, or asset files (`zone-manifest.json`, `.asset`, source files) are automatically resolved in favor of the verified repository file and recorded in `spec.assumptions` (`spec.resolved_conflicts`) rather than pausing to ask the user. For single-slice requests ($\le 3$ tasks, no deferred stories, no blocking ambiguity), Pass A collapses into a **single-pass fast-track** (`"pass": "combined"`) and generates tasks and test cases directly without a second Pass B LLM call.
   - **Time to First Experience (TTFE) Optimization**: Unsafe or irreversible choices (privacy/PII, security/auth, billing, destructive data loss, or costly schema changes) and authoritative product/architecture contradictions (`prd.md#Not this`, `docs/architecture.json`) pause at the **Intent Gate**, whereas reversible product-behavior defaults (such as initial scoring or tie-breaker rules) are recorded as v0.1 **Product Hypotheses** (`spec.product_hypotheses`) so the first playable slice builds immediately.
   - **Pass B (Repository-Aware Execution Planning & Test Case Compendium Sync)**: Runs for multi-story epics or post-clarification plans to plan concise, plain-English tasks (with optional `depends_on` task dependencies) and automated test cases for only the first ready vertical story slice, while turning remaining `deferred` stories into waiting `status: "backlog"` jobs linked to the same feature. Normalized `plan.test_cases` are automatically synced into the repository's **Test case library** compendium (`docs/test-cases/<area>/TC-<ref>-<nn>.json`) when a job is planned (`new_job.py`), edited in the Web UI (`POST /api/jobs/<id>/plan-test-cases`), or built and committed on the job's branch (`worker_run.py` / `test_cases.sync_job_cases`).
   - **Auto-Passing Lightweight Gates**: Small, low-risk, reversible slices with clean test cases and no manifest changes automatically pass the Intent and Plan gates (`auto_passed_gates: true`).
3. **Success Criteria, KPIs & Guardrail / Counter-KPI Metrics**:
   - Every feature spec includes explicit or implied `success_criteria`.
   - When an analytics or monitoring app (Mixpanel, Amplitude, PostHog, Sentry, Datadog, New Relic) is connected, `spec.kpi_metrics` ties those success criteria to a **Primary KPI** and at least one **Guardrail / Counter-KPI** metric (e.g., error rate, latency, or abandonment counter-KPI).
   - When no analytics app is connected, Orchestrator recommends setting up analytics/KPIs **once** and then stays silent on KPIs until analytics is connected in setup.
4. **Experience, Decide & Refine (`v0.1 → v0.2 → v0.3`)**:
   - When a build reaches `review-needed` (step `5. Decide` in the lifecycle stepper), the Job page folds **Version history** (with clickable **Assumed defaults** chips and **Undo last iteration**) into the top stepper, paired with a **Decide** routing strip (`.job-decide-routes`) in the hero card offering the recommended primary release action (**Merge & complete** / **Mark complete**) alongside explicit routes:
     - **Build again** — focuses the targeted implementation fix prompt (`#job-steer-text`).
     - **Frame again** — opens the revise-plan dialog before editing code when intended behavior or rules change.
     - **Try again** — re-runs verification (`test`) on the current version without code changes when evidence is inconclusive.
     - **Pause / Close** — archives the job to record the stop and move on.
   - Every reaction runs through a 5-way **Change-Impact Classifier** (`visual_or_copy`, `behavior_change`, `new_capability`, `fundamental_rethink`, `undo`), locks previously accepted criteria against regression (`locked_behaviors`), and triggers an **Architectural Refactor Guardrail** when the same files are patched $\ge 3$ times across iterations.
   - The **Analytics** page tracks the 5 **Intent-Convergence Metrics**: **Time to first experience (TTFE)**, **Time per feedback iteration**, **Iterations to acceptance**, **Rework & regression rate**, and **User interaction burden**.
5. **Lean LLM Execution & Prompt Guardrails**:
   - Plan quality checks (test cases, size budget, and vertical slicing) run together with at most **1 combined self-correction retry**.
   - Grounded, single-slice plans from high-tier planners that auto-pass the deterministic Plan Gate skip the redundant Senior Architect verification LLM call.
   - Each job dynamically breaks down into runs (`job_runs.breakdown`): taskless jobs (`bug-fix`, `bug-investigate`, `test-audit`, `feature-design`), `quick-fix`, 1-task feature plans, and 2–3 small cohesive tasks touching overlapping files ($\le 4$ unique files) run as a single pass (`1 run` / `1 batched run`); multi-task feature plans with overlapping files or sequential dependencies run as `N sequential runs`; and multi-task feature plans with non-overlapping `likely_files` in the same dependency wave run concurrently in isolated git worktrees (`N runs · M waves (parallel)`), recording each task run and merging checkpoints back onto the job's branch. Failed builds capture output on the first run and extract compiler/test errors deterministically (including MSBuild `error :` diagnostics, ignoring `Passed! - Failed: 0` lines); UI screenshot instructions only appear for UI-touching tasks or configured `ui_review` projects; and visual/copy or undo refinements skip redundant post-job PRD update LLM calls.
6. **Resilient AI Sign-In Fallback & Streamlined Workspace**:
   - When a job's configured AI CLI rejects authentication (e.g., expired OAuth token) and the job is not restricted to free-only models, Orchestrator automatically falls back to ready signed-in models on this computer — including Antigravity-hosted Claude models (`claude-sonnet-*`, `claude-opus-*` routed via `agy`) — and marks the expired CLI rejected until re-authenticated. The order is **cheapest adequate first**, not strongest first: a model is adequate when it is no weaker than the one that failed and meets the role's own minimum tier (planner, builder and debugger need a high-tier model; verifier and architect need the top tier). Among those, hosted Claude comes first when Claude was the tool that failed, then the lowest cost, so a Sonnet that loses its sign-in falls back to a hosted Sonnet before any Opus. For builder and debugger roles, text-only local CLIs (`ollama` / `qwen`, which lack file-editing tools) are excluded from sign-in fallbacks, and free `opencode` models are demoted behind standard agentic providers when falling back from a paid primary model. Weaker models are tried only after every adequate one has failed, and the tool whose sign-in failed is never a fallback.
   - When a build or task finishes with **0 files changed**, Orchestrator checks whether another completed job already modified the same target files (or whether the builder reported the work is already in place). If so, it pauses with a **stopping clarification question** naming the overlapping completed job and shared files (never auto-answered by YOLO) so you can either close the job or clarify what still needs to be built. Otherwise, the no-change stop message names the actual model that ran and includes the builder's concise summary.
   - Automatic minor updates (`auto_minor`) re-check for active runs right before restarting the local UI server so a run started while an update is downloading is never interrupted with `SIGTERM`. The server also records its boot commit (`_boot_commit`) and checks every 30 seconds whether `HEAD` on disk moved past it (from a deferred restart, a local commit, or a manual `git pull`), automatically restarting once idle and `orchestrator/` has no uncommitted edits.
   - `.NET` test auto-detection (`dotnet_test_projects`) skips projects that enforce external MSBuild `-p:` properties so bare `dotnet test` commands stay green.
   - Failure heroes include a one-click top-right log-copy icon (`#i-log`) and a **Run on `<Peer Name>`** failover button when a machine-scoped blocker (such as low disk space) has an online peer with enough free space (plus an opt-in **Auto-handoff jobs on low disk space** toggle in **Settings → Machines**). The **Questions** fold (`#clarify-history`) places its `#i-download` chat export menu (**Copy chat** / **Export chat**) right-aligned in the title row, Run pages place rerun (`#i-refresh`) and share-log (`#i-share`) icons in the upper-right header, Documentation **Gaps** offer a **Fix** modal (Use AI, Write manually, or Import file), and AI Tools starts fully collapsed with background-only capability scanning.

## Updating Orchestrator

Each computer keeps itself up to date. About every half hour it checks the branch it was installed from (`web-ui`), and
sorts what's new into one of two kinds:

- **Minor updates**: fixes and improvements. They install by themselves once no run is going, then Orchestrator restarts.
  Jobs carry on where they were.
- **Breaking updates**: changes your other computers or saved data depend on, such as a new way jobs sync. They wait for
  you, so you know when it happens and can update your other computers too. A breaking update says what changed.

**Configuration > Updates** shows this computer's version and what's waiting, with **Check now**, **Update now**, what's
new in the update, and recent updates. Two settings: **Install minor updates by themselves** (on) and **Install breaking
updates by themselves too** (off). An update never installs while a run is going, and a copy of Orchestrator with edits of
its own (a development checkout) isn't updated over them; the page says so instead.

The Orchestrator Mac app updates itself through its own menu (Check for Updates). A computer too old to update itself
shows the command to run in **Your computers**.

For developers: a change is breaking when it raises `breaking` in `orchestrator/release.json` (see AGENTS.md).

## Troubleshooting

Run both checks first:

```bash
orchestrator check
orchestrator check-config
```

Common issues:

- `orchestrator: command not found`: install the package in the active Python environment or use the virtualenv's `bin/orchestrator`.
- `orchestrator: command not found` after `pipx install`: run `pipx ensurepath`, open a new terminal, then retry.
- `Configure xcode_project, xcode_workspace, or build_command`: run `init` from the Swift project root or set `build_command`.
- `scheme is required`: set `scheme` in `.orchestrator/project.json`.
- `No AI providers found`: authenticate a provider CLI or export a supported API key.
- SSH worker is `NOT READY`: run `orchestrator worker-install --machine NAME`, then rerun `worker-check`.
- Remote worker imports fail after package changes: rerun `worker-install` to refresh the source copy.
- GitHub actions fail: install `gh` and run `gh auth login`, or set `"code_host": "git"` to work without GitHub ([Code hosts](code-hosts.md)).
- `orchestrator logs` reports `Sentry refused the token (403)`: the token lacks `org:read`, `project:read` or `event:read`. Create a new one and rerun `orchestrator logs setup`.
- `No app sessions found`: the build predates remote logging, the app was never opened, or it is an App Store build (errors only). Open the app, wait a few seconds, and retry.
- `Stale Git index lock` (`.git/index.lock`): job pull recovery automatically removes lock files older than 10s when `lsof` confirms no process holds them. If a lock is newer or blocked, use **Remove lock & Rerun** in the web UI (or delete `.git/index.lock` manually once no Git process is running).
- `Untracked local files would be overwritten`: pull recovery automatically cleans up colliding untracked files whose contents already match `origin/<branch>`. Files with different local contents are preserved and can be stashed cleanly via **Stash colliding files & Rerun** in the web UI without disturbing unrelated work.
- `Branch out of sync with remote`: use **Sync with remote & Rerun** in the web UI (after confirming) to abort any stuck rebase and reset the local branch to `origin/<branch>`.
- `<machine> wasn't set up with the Orchestrator app`: remote UI updates apply only to packaged Orchestrator `.app` installs. On CLI or LaunchAgent Macs, run `orchestrator update` locally (or `launchctl kickstart -k gui/$(id -u)/com.orchestrator.ui` to reload local edits).

## Generated Ignore Rules

`orchestrator init` writes `.orchestrator/.gitignore`:

```gitignore
jobs/
logs/
output/
state/
*.pyc
__pycache__/
```

This lets the project commit orchestrator config while keeping runtime output local.

## Suggested Project Docs

The setup checker looks for grounding docs that help AI agents work consistently:

```text
AGENTS.md
docs/architecture.md
docs/coding-standards.md
docs/build-test-commands.md
```

You don't write `docs/architecture.md` by hand: the Architecture section of the Product page writes it when you approve or skip. The setup checklist's recommended **Choose or describe the architecture** item takes you there, and counts as done once the project has an approved architecture or a working assumption.

Minimal `AGENTS.md` snippet:

```md
# AGENTS.md

Use `docs/build-test-commands.md` for canonical validation commands.
Prefer minimal, reviewable diffs.
Do not commit secrets, generated runtime logs, or unrelated changes.
Run `orchestrator check-config` after changing `.orchestrator/project.json`.
```
