# Orchestrator User Guide

This guide covers using Orchestrator with supported project stacks. Swift and Xcode support is the most mature; support for additional languages is in beta.

If you are new to Orchestrator, start with [Getting Started](getting-started.md) for the short overview and first-run path.

## Install

Recommended CLI install:

```bash
brew install pipx
pipx ensurepath
pipx install "git+https://github.com/leemosupreemo/orchestrator.git"
```

Upgrade later with:

```bash
pipx upgrade orchestrator
```

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
- GitHub issue and pull request workflow: `gh`
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

Project-local prompt files with matching names take precedence over package defaults.

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

## Visual Simulator Checks

Visual checks require an app bundle and bundle identifier. Configure:

```json
{
  "app_bundle_id": "com.example.MyApp",
  "visual_app_path": "/absolute/path/to/MyApp.app"
}
```

If `visual_app_path` is omitted, the visual check flow searches the configured derived data path for a built `.app`.

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

`orchestrator ui` starts a local web interface for the current project and opens it in your browser:

```bash
orchestrator ui                  # http://127.0.0.1:8765/?token=...
orchestrator ui --project Thirteen --port 9000 --no-open
```

- **Home** shows what **needs you**: a question from the planner, a plan to approve, failing tests to fix, or a PR ready to merge. Each item has the one button that moves it forward. Below that are jobs in progress and recently finished ones. **New job** and **Fix something** are always one tap away, and less frequent actions (pull device logs, build, test, distribute the current branch, setup check, full console) sit under **More**.
- **Jobs** filters by Needs you / In progress / Done. A job page states what it's waiting on, shows one primary action for its status (Start, Run now, Answer, Run fix, Merge & complete), and puts everything else under More. **Deliver to testers** builds *that job's* branch. **Merge & complete** merges the PR, deletes the AI branch and archives the job, the same as in the console.
- **Device logs** lists recent app launches from the log store, with Pull and Follow buttons (see [Device Logs](#device-logs)).
- **Activity** lists every command started from the UI. Runs started from a job are named after it and appear on that job's page. When a run finishes, it offers the next step, such as **Open job** after creating one.

Pages refresh themselves every few seconds, but never while you're typing, have a menu open, or have a dialog up. Leaving a page closes any dialog that was open on it.

Every action runs the same CLI command you would type, inside a real terminal that is shown in the page. Prompts that need you (y/n, menus, pasted logs, passwords) appear there, and you answer them by typing into the terminal. On a phone, use the key bar under it (Enter, Esc, arrows, y/n/q, Ctrl-C, Ctrl-D). **Open full console** runs the regular `orchestrator console` in the browser, so every console feature stays available. Each run's output is also saved to `.orchestrator/logs/ui/`.

Access and security:

- The server listens on `127.0.0.1` only. The printed URL carries a random per-start token. Opening it once stores the token in a cookie, and every API call needs it.
- The browser can only start a fixed set of actions. It never sends a command line, and file reads are limited to `.orchestrator/`.
- From a phone, either tunnel over SSH: `ssh -L 8765:127.0.0.1:8765 <mac>`, then open the printed URL on the phone. Or bind to a private network address such as Tailscale: `orchestrator ui --host 100.x.y.z`. Don't bind to a public interface.
- Stopping the server (Ctrl-C) also stops the commands it started.

## Device Logs

Builds running on a phone (Firebase App Distribution, TestFlight) can't be read over a cable from an SSH session, so the app ships its logs to a central store and Orchestrator pulls them back. Sentry Logs is the supported store: the app sends each log entry with a per-launch `app_session` attribute, plus one `remote_log.session_start` entry per launch carrying its build channel, version and build.

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
- GitHub actions fail: install `gh` and run `gh auth login`.
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
