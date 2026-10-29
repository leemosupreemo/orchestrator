# Getting Started 🚀

Orchestrator coordinates AI coding workflows from a terminal console. It turns feature requests and bug reports into tracked jobs that move through planning, verification, test-driven implementation, and review. Swift has the deepest support; Rust, Python, JavaScript/TypeScript, and Go support is in beta.

## Use the Web App 🌐

The quickest way in, on a Mac or Linux computer:

```bash
curl -fsSL https://swift-orch-web-20260923.web.app/install.sh | sh
```

It installs what's missing, adds the computer to your account (you confirm a code in the browser) and starts Orchestrator at login. Then open **https://swift-orch-web-20260923.web.app** from any device and sign in. Your code and files stay on your computer; the site only knows which computers are on your account. Home starts with a four-step checklist until you're set up: a project, Git, an AI model, and an enabled machine. Start a new project there to answer a few questions about what you're building; each answer goes into the product requirements, and **Break it down** turns them into epics to approve (each becomes a job that waits until you plan it, and can be sent to Jira).

Prefer the terminal? Everything below works there too.

## Set Up a Project ⚙️

From the project root, run:

```bash
orchestrator import             # detect stack, create config, generate docs, draft PRD
orchestrator wizard            # full interactive setup
orchestrator check
orchestrator console
```

`orchestrator import` is the quickest way to add an existing project: it detects the stack (Swift, Python, Rust, Node, Go, C#/Unity, etc.), writes `.orchestrator/project.json` with the right build/test commands, generates starter docs, and can draft a product requirements document from your existing code. The wizard detects the project stack, creates `.orchestrator/project.json` and missing starter docs, and configures at least one AI model. GitHub, remote machines, and Apple delivery are optional. `orchestrator check` validates prerequisites and AI provider access; `orchestrator console` opens the interactive console.

Setup checks confirm that configuration and required tools are present; they do not run your project’s build or test commands. Run both once yourself before starting the first job, and correct the commands in `.orchestrator/project.json` if they do not exercise the project you intend to build.

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
orchestrator import [--project <path>]   # Import an existing project (detect stack, create config, draft PRD)
orchestrator wizard                      # Interactively configure a project
orchestrator check                       # Check prerequisites and providers
orchestrator check-config                # Validate project configuration
orchestrator console                     # Open the console
orchestrator projects                    # List remembered projects
orchestrator use <name-or-path>          # Select the active project
orchestrator update                      # Update Orchestrator
```

For complete setup options and troubleshooting, see the [User Guide](user-guide.md).
