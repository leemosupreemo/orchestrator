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
  "firebase_plist_path": "MyApp/GoogleService-Info.plist"
}
```

`check-config` fails early if Firebase delivery is enabled but the distribution script or plist path is missing.

The delivery workflow archives and signs the iOS app, uploads the IPA to Firebase App Distribution, and can release it to configured tester emails or groups. This lets multiple testers install the beta build on their devices.

For an end-to-end workflow from iPhone or iPad, use [Secure ShellFish](https://secureshellfish.app/) to connect over SSH to the machine running Orchestrator. You can manage the coding workflow remotely, run builds and tests, and trigger Firebase delivery from the same terminal session.

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
