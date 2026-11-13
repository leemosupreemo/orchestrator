# Orchestrator User Guide

This guide covers using Orchestrator with supported project stacks. Swift and Xcode support is the most mature; support for additional languages is in beta.

If you are new to Orchestrator, start with [Getting Started](getting-started.md) for the short overview and first-run path.

## Install

Quickest, on a Mac or Linux computer: one command installs what's missing, installs Orchestrator, adds the computer to your account and starts it at login (see [Web UI](#web-ui) for what that gives you):

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

Use `build_command` and `test_command` when a project needs custom build/test commands instead of the generated `xcodebuild` defaults.

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

## Configure AI Providers

On **Manage AI tools**, **Connect** shows a guided checklist for Claude, Codex, Poolside and Antigravity. It installs the tool if needed, prepares user-local Node.js/npm for Codex when missing, and shows the browser approval link and any code. Installer terms require your explicit agreement. **Sign-in details and advanced input** is available when a provider asks an additional question. Closing the dialog keeps the sign-in running under Running now.

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

## UX And Design Review

A checklist review of your product's screens, in two parts: **UX** (how it works: status, wording, one primary action,
progressive disclosure, navigation and a way back from every detail page, empty states, errors, forms, short action
labels, long content behind "Show more") and **Design** (how it looks: hierarchy, tokens, spacing, alignment, sizes,
section headers that read as titles, one header layout, consistent field labels, no unstyled browser controls,
overlap, responsiveness, contrast, dark mode, focus). The checklist is
`orchestrator/prompts/ux_reviewer.md`; your own UI conventions file, if you have one, takes precedence over it.

- **Product pass.** Web UI > UX review > Run a pass, or `orchestrator script ux_review_run.py pass`. It captures every
  configured screen, runs a UX pass and a design pass (two model calls), and writes one report with findings ranked by
  severity, the checklist, and the screenshots, under `.orchestrator/output/ux-pass/<when>/`. "Create a job to fix"
  turns the findings into a job. A pass that couldn't capture screenshots finishes as "Completed with notes" (yellow)
  and says only the code was read; in a project with no interface files it skips the model calls and reports
  "Nothing to review".
- **Change check.** After the code review, every job whose diff touches interface files (HTML, CSS, JSX/TSX, Vue,
  Svelte, SwiftUI views, storyboards, scripts in UI folders, or anything matching `ui_review.paths`) gets the same
  checklist on just what it changed. The job page shows a "UX and design check" card, and the hero warns before you
  merge when something major was found. It is a prompt to look, never a gate.

Screens come from `ui_review` in `.orchestrator/project.json` (or Web UI > UX review > Screens):

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
- Direct updates support npm, pnpm, Bun, standalone Yarn, SwiftPM, Cargo, Go, uv, Poetry, simple pip requirements, CocoaPods, Bundler, Dart and Flutter. Poetry and pip require an existing isolated project-local `.venv`; pip requirements with includes, options, extras, markers or URLs need a planned update. Updates respect declared constraints and may adjust necessary transitive lock entries. Modern Yarn workspaces, Gradle and Unity show an explanation and **Plan update with AI** before any change. A missing test command prevents native updates until it is configured.
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

> **Setup checklist.** Four things make a computer ready: a project, Git, an AI model (a provider you can use, with a model selected), and an enabled machine. Each row has one button that opens the right place, and the rest, such as role instructions and screens to review, is optional.

> **Product requirements.** Each project has one short document, `docs/product/prd.md`, that every planner, builder and reviewer reads first. You can write it, import one you already have (it is summarised into the sections, and the full file stays attached for jobs to read), or have it drafted from an existing project, and it is kept up to date as jobs finish, with a history you can undo. Every section is optional. Once there is something in it, **Break down** splits it into epics to approve or decline. See [Epics](#epics) and [Product requirements](product-requirements.md).

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
- **A run's page** keeps the box at the top that says what the run is doing now, or what to do next once it ends. Below it, **Timeline** shows the run as steps, such as *Preparing git branch*, each task of the plan, *Building project* and *Running tests*:
  - each step shows how long it took and, for a task, which one it is ("2 of 3");
  - the AI model calls made during a step are listed under it ("Asked claude-sonnet-4-6 · 48s"), including a model that timed out and was replaced;
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

  When jobs are waiting on you, the card says how many, for example **2 jobs need you**. Tap that to see them. While runs are going, it says how many are in progress.
- **Preview** (web projects). The app as it is on the current branch. See [Live preview](#live-preview).
- **Jobs.** Everything not finished, filtered by **All**, **Needs you**, **In progress** and **Backlog**. Each row shows the one thing that moves it forward. Finished jobs are listed below, and archived ones under that.

The sidebar's **Primary machine** dropdown switches between your computers. The one marked **Primary** starts this project's jobs. Another computer can look at the project, but it can't start work until it's made primary.

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

Quickest on a Mac or Linux computer: run `curl -fsSL https://swift-orch-web-20260923.web.app/install.sh | sh`. It installs what's missing (Python 3.11+, Git, pipx, cloudflared), installs Orchestrator, shows the code to confirm, and starts Orchestrator at login. Add `--no-service` to skip the login start, or `--dry-run` to only print what it would do. The steps below do the same by hand.

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

Minimal `AGENTS.md` snippet:

```md
# AGENTS.md

Use `docs/build-test-commands.md` for canonical validation commands.
Prefer minimal, reviewable diffs.
Do not commit secrets, generated runtime logs, or unrelated changes.
Run `orchestrator check-config` after changing `.orchestrator/project.json`.
```
