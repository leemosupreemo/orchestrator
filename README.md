# Orchestrator (Beta)

**Build product features and fix bugs by coordinating multiple machines and LLMs through one end-to-end development workflow.**

Orchestrator brings planning, implementation, review, testing, and delivery into a single multi-agent process. It distributes work across local and remote machines, assigns different LLMs to specialized roles, and carries each change from an idea or bug report through verification.

Orchestrator began with deep support for Swift and Xcode. It is now expanding across languages, with beta support for Rust, Python, Node.js/TypeScript, Go, Swift Package Manager, and Xcode projects.

---

## 🚀 Key Features

*   **Interactive Dev Console**: A terminal-based UI designed for speed. Single-key shortcuts (`y/n`, `A`/`F`/`Q`) and real-time status bars make orchestration feel like a native tool.
*   **Structured Multi-Agent Workflow**: Jobs pass through specialized agents, with feature work guided by a test-driven development (TDD) philosophy:
    *   **Planner**: Analyzes requirements and drafts a multi-step plan that defines tests before production code.
    *   **Verifier**: Checks the plan against the codebase for feasibility, architectural fit, regression risks, and appropriate test coverage.
    *   **Builder**: Follows the Red–Green–Refactor cycle: write a failing test, implement the minimum code needed to pass, then improve the code while keeping tests green.
    *   **Reviewer**: Audits the implementation, test coverage, and regression risks before changes are finalized.
*   **Fleet Orchestration**: Dispatch heavy builds or exhaustive test suites to remote machines via SSH. The Orchestrator handles branch synchronization, worker package installation, remote execution, and job output automatically.
*   **Interactive Account Setup**: Detect installed AI and GitHub CLIs, then sign in or add API keys from the wizard without restarting.
*   **Automated GitHub PR & Issue Workflow**: Integrates with GitHub through the `gh` CLI to create issues, open pull requests, and post automated status updates.
*   **Multi-Language Project Setup (Beta)**: Detect Rust, Python, Node.js/TypeScript, Go, Swift Package Manager, and Xcode projects, then configure the appropriate build and test commands.

---

## 🛠️ Install

Orchestrator requires Python 3.11 or newer. With `pipx` installed:

```bash
pipx ensurepath
pipx install "git+https://github.com/leemosupreemo/orchestrator.git"
orchestrator --help
```

### Quick Start

1.  **Configure**: Run the wizard from the project root.
    ```bash
    cd /path/to/MyProject
    orchestrator wizard
    ```
2.  **Verify**: Check prerequisites, AI provider access, and machine configuration.
    ```bash
    orchestrator check
    ```
3.  **Launch Console**: Start the interactive dev console to create your first job.
    ```bash
    orchestrator console
    ```

### Setup Wizard

The wizard detects your project stack and guides you through five steps:

1. **Project**: Confirm the project and its build/test settings.
2. **AI**: Choose at least one model and sign in if needed.
3. **Tools**: Optionally configure GitHub, custom prompts, SSH workers, and Apple delivery.
4. **Review**: Edit, apply, or cancel before files are written.
5. **Verify**: Validate the configuration and index the project.

You need a project name, an AI model, and valid build/test settings for your stack. Worker and delivery fields are required only when enabled. Prompts clearly label how to skip optional fields or keep defaults; use **Ctrl-S** to skip an optional section and **Ctrl-Q** to quit.

See the [User Guide](docs/user-guide.md#first-run-wizard) for every option, non-interactive setup, and generated file.

### Multi-Language Support (Beta)

- Swift
- Rust
- Python
- JavaScript and TypeScript
- Go

---

## 🧩 Agent Hierarchy

| Agent | Role | Output |
| :--- | :--- | :--- |
| **Planner** | Codebase research, scoping, and test planning | Grounded implementation plan |
| **Verifier** | Plan feasibility and architecture validation | Approval status, corrections, and risks |
| **Builder** | Test-driven implementation and validation | Tested code changes |
| **Reviewer** | Diff review against the brief and acceptance criteria | Risk-ranked findings and verdict |
| **Debug Agent** | Evidence-based root-cause investigation | Hypothesis and targeted action plan |
| **Build Checker** | Build and test log triage | Ranked blockers and recommended fix order |

---

## 💻 Dev Console UI

The Dev Console is the primary way to interact with the Orchestrator. It features:
- **Single-Key Control**: Navigate menus and approve actions with instant key presses (no `Enter` required for `y/n` or menu choices).
- **Interactive AI Discovery**: Automatically detects installed CLIs and guides you through authentication if needed.
- **Real-time Status Bar**: Tracks machine availability, active models, and job progress.
- **Context Linking**: Easily attach logs, UI mockups, or previous job context to new requests.

---

## 📡 Remote Workers (Fleet)

Scale your workflow by adding remote machines as workers. Remote workers can handle:
- **Builds**: Offload resource-intensive build jobs.
- **Tests**: Run exhaustive UI or unit test suites in parallel.
- **Execution**: The Orchestrator syncs your repository and its own runtime to the worker automatically.

Add workers in `.orchestrator/config/machines.json` and prepare them with:
```bash
orchestrator worker-install --machine worker1
orchestrator worker-check --machine worker1
```

---

## 📱 Mobile Delivery

For iOS projects, Orchestrator can archive and sign an app, upload the IPA to Firebase App Distribution, and release it to configured tester emails or groups for installation on multiple devices.

For a phone-operated workflow, use [Secure ShellFish](https://secureshellfish.app/) on iPhone or iPad to connect over SSH to the machine running Orchestrator. From there, you can coordinate the job, run builds and tests, and distribute the resulting beta build through Firebase.

---

## ⚙️ Project Configuration

The wizard writes stack-specific project and build/test settings to `.orchestrator/project.json`.

Validate your configuration any time:
```bash
orchestrator check-config
```

### 🆙 Keeping Up to Date

Use the command that matches your installation:
```bash
pipx upgrade orchestrator     # pipx installation
orchestrator update           # Editable Git checkout
orchestrator update --fleet   # Enabled remote machines
```

---

## 📚 Documentation

- [Getting Started](docs/getting-started.md): What Orchestrator does and the first commands to run.
- [User Guide](docs/user-guide.md): Comprehensive setup, commands, and troubleshooting.

---

## 🛠️ Local Development

For developers contributing to the Orchestrator itself:

```bash
git clone https://github.com/leemosupreemo/orchestrator.git
cd orchestrator
python3 -m pip install -e .
python3 -m unittest discover -s tests
```

Package install smoke test:
```bash
python3 tests/smoke_package_install.py
```

---

## 📄 License

Copyright © 2026 The Jaunt Company. All rights reserved.

This project is source-available for viewing and evaluation, but is
not released under an open-source license. See [LICENSE](LICENSE) for details.
