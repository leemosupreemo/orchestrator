# Orchestrator (Beta)

**Build product features and fix bugs by coordinating multiple machines and LLMs through one end-to-end development workflow.**

Orchestrator brings planning, implementation, review, testing, and delivery into a single multi-agent process. It distributes work across local and remote machines, assigns different LLMs to specialized roles, and carries each change from an idea or bug report through verification.

Orchestrator runs on a Mac and fully supports **iOS, Android and web** projects (see [Supported platforms](#-supported-platforms)). Other languages and stacks (Rust, Python, Go, Swift Package Manager) are detected in beta.

---

## 🚀 Key Features

*   **Web app, from anywhere**: Sign in at one fixed address and reach the computer that has your projects, from any device. The work runs on your own computer, and your code and assets stay on it. It is phone-friendly and covers new projects, jobs, device logs, builds and distribution; each action runs in a live in-browser terminal, and the full console is one click away. `orchestrator ui` runs the same interface locally. See [Web UI](docs/user-guide.md#web-ui).
*   **Product requirements**: One short document per project that every planner, builder and reviewer reads first. Write it, import one you have (summarised into its sections, the full file kept as a reference for jobs), or draft it from existing code, then break it down into epics (a story map or a top-down work breakdown, as detailed as what you wrote). Approved epics become jobs marked Epic that wait until you plan them, and can be sent to Jira or imported from it. See [Product requirements](docs/product-requirements.md).
*   **Interactive Dev Console**: A terminal-based UI designed for speed. Single-key shortcuts (`y/n`, `A`/`F`/`Q`) and real-time status bars make orchestration feel like a native tool.
*   **Structured Multi-Agent Workflow**: Jobs pass through specialized agents, with feature work guided by a test-driven development (TDD) philosophy:
    *   **Planner**: Analyzes requirements and drafts a multi-step plan that defines tests before production code.
    *   **Verifier**: Checks the plan against the codebase for feasibility, architectural fit, regression risks, and appropriate test coverage.
    *   **Builder**: Follows the Red–Green–Refactor cycle: write a failing test, implement the minimum code needed to pass, then improve the code while keeping tests green.
    *   **Reviewer**: Audits the implementation, test coverage, and regression risks before changes are finalized.
*   **Fleet Orchestration**: Dispatch heavy builds or exhaustive test suites to remote machines via SSH. The Orchestrator handles branch synchronization, worker package installation, remote execution, and job output automatically.
*   **Interactive Account Setup**: Detect installed AI and GitHub CLIs, then sign in or add API keys from the wizard without restarting.
*   **GitHub, GitLab, Bitbucket or plain git**: With GitHub, jobs become issues and finished work a pull request (through the `gh` CLI). With any other host, or none, jobs run on their own branches, which are pushed for a merge request or merged locally. See [Code hosts](docs/code-hosts.md).
*   **Project setup**: Detect Xcode, Android (Gradle) and Node.js/TypeScript projects and configure their build and test commands. Rust, Python, Go and Swift Package Manager projects are detected in beta.
*   **Signing keys, checked for you**: App delivery shows whether this computer has the key that signs your builds (the App Store Connect key for iOS, the release keystore for Android), walks you through setting it up, and can copy it from another of your computers after a security warning.

---

## 🛠️ Install

On a Mac, one command installs what's missing, installs Orchestrator, adds the computer to your account and starts it at login:

```bash
curl -fsSL https://swift-orch-web-20260923.web.app/install.sh | sh
```

Then open https://swift-orch-web-20260923.web.app and sign in. To install by hand, Orchestrator requires Python 3.11 or newer. With `pipx` installed:

```bash
pipx ensurepath
pipx install "git+https://github.com/leemosupreemo/orchestrator.git"
orchestrator --help
```

### Quick Start

1.  **Import** (for an existing project): Import an existing project, detect its stack, generate config and starter docs, and draft a PRD from the code.
    ```bash
    cd /path/to/MyProject
    orchestrator import
    ```
2.  **Configure**: Alternatively, run the wizard for full interactive setup (AI models, tools, prompts, etc.).
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

The Dev Console is the terminal interface to the Orchestrator (the web app covers the same ground in a browser). It features:
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

## 📱 Supported platforms

Orchestrator runs on **macOS**. From that Mac it works on three kinds of project:

| Project | Build, test and coverage | Send to testers |
| --- | --- | --- |
| **iOS** | Xcode builds and tests, simulator screenshots, line coverage | Archive, sign, and upload the IPA to Firebase App Distribution |
| **Android** | Gradle builds and unit tests (instrumented tests by hand), emulator screenshots, line coverage when the module writes a coverage report | Build the APK or AAB and upload it to Firebase App Distribution |
| **Web** | `npm`, `pnpm`, `yarn` or `bun` builds and tests (Jest, Vitest), screenshots, line coverage | A preview link on Firebase Hosting (it expires after 7 days) |

Not supported yet: running Orchestrator on Windows or Linux, macOS, tvOS, watchOS or visionOS apps, publishing to Google Play, and deploying a web app to production (previews only). Android needs the Android SDK and Gradle on the Mac that builds it.

---

## 📱 Mobile Delivery

For iOS projects, Orchestrator can archive and sign an app, upload the IPA to Firebase App Distribution, and release it to configured tester emails or groups for installation on multiple devices. Successful deliveries include a receipt with the version, build number, artifact checksum, and recipients.

For a phone-operated workflow, use [Secure ShellFish](https://secureshellfish.app/) on iPhone or iPad to connect over SSH to the machine running Orchestrator. From there, you can coordinate the job, run builds and tests, and distribute the resulting beta build through Firebase.

Runtime logs from those device builds come back the same way: the app ships them to Sentry Logs, and `orchestrator logs pull --latest` (or **Link Logs → Pull Device Logs** in the console) downloads the newest launch and links it to the job. Run `orchestrator logs setup` once per project. See [Device Logs](docs/user-guide.md#device-logs).

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
- [Code hosts](docs/code-hosts.md): GitHub, GitLab, Bitbucket, other hosts, or no remote.
- [Product requirements](docs/product-requirements.md): The one living document every AI reads first: how to start it (write, draft from a project, import), how it keeps itself true, and how to undo it.

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
