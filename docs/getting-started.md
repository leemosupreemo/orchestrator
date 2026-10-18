# Getting Started 🚀

Orchestrator coordinates AI coding workflows from a terminal console. It turns feature requests and bug reports into tracked jobs that move through planning, verification, test-driven implementation, and review. Swift has the deepest support; Rust, Python, JavaScript/TypeScript, and Go support is in beta.

## Use the Web App 🌐

The quickest way in, on a Mac or Linux computer:

```bash
curl -fsSL https://swift-orch-web-20260923.web.app/install.sh | sh
```

It installs what's missing, adds the computer to your account (you confirm a code in the browser) and starts Orchestrator at login. Then open **https://swift-orch-web-20260923.web.app** from any device and sign in. Your code and files stay on your computer; the site only knows which computers are on your account. Home starts with a four-step checklist until you're set up: a project, Git, an AI model, and an enabled machine. Start a new project there to answer a few questions about what you're building; each answer goes into the product requirements, and **Suggest features** turns them into features to approve.

Prefer the terminal? Everything below works there too.

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
