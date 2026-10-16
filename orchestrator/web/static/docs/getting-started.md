# Getting Started 🚀

Orchestrator coordinates AI coding workflows from a terminal console. It turns feature requests and bug reports into tracked jobs that move through planning, verification, test-driven implementation, and review. Swift has the deepest support; Rust, Python, JavaScript/TypeScript, and Go support is in beta.

## Set Up a Project ⚙️

From the project root, run:

```bash
orchestrator wizard
orchestrator check
orchestrator console
```

The wizard detects the project stack, creates `.orchestrator/project.json` and missing starter docs, and configures at least one AI model. GitHub, remote machines, and Apple delivery are optional. `orchestrator check` validates prerequisites and AI provider access; `orchestrator console` opens the interactive console.

## Run Your First Job 🧠

In the console:

1. Select **New Job**.
2. Choose a feature, iteration, bug fix, or design workflow.
3. Describe the work in plain language.
4. Choose a branch and whether to continue automatically after planning.
5. Follow progress from the job history.

Feature jobs use a test-driven workflow: plan the tests, verify the plan, write a failing test, implement the change, and review the result.

## Key Project Files 📂

```text
.orchestrator/project.json         Project and build/test configuration
.orchestrator/config/settings.json AI provider and console settings
.orchestrator/config/machines.json Remote machine definitions
.orchestrator/jobs/                Job records
.orchestrator/logs/                Runtime logs
AGENTS.md                          Instructions for coding agents
docs/build-test-commands.md        Canonical build and test commands
docs/ai-workflow.md                Project workflow notes
```

Generated jobs, logs, output, and state under `.orchestrator/` are excluded by its `.gitignore`.

## Common Commands 💻

```bash
orchestrator wizard               # Configure a project
orchestrator check                # Check prerequisites and providers
orchestrator check-config         # Validate project configuration
orchestrator console              # Open the console
orchestrator projects             # List remembered projects
orchestrator use <name-or-path>   # Select the active project
orchestrator update               # Update Orchestrator
```

For complete setup options and troubleshooting, see the [User Guide](user-guide.md).
