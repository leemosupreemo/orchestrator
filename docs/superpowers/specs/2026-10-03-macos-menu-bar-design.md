# Orchestrator macOS menu-bar app

## Purpose and decisions

Clients should install and use Orchestrator without opening Terminal. They want a visible, simple desktop presence, with Tailscale as the interaction reference. The user approved the direction of a small native menu-bar app with a separate background agent and browser-based workspace.

Ship `Orchestrator.app` in a downloadable, signed and notarized DMG. The native app shows computer status and basic controls; the existing web interface handles projects, configuration and jobs. Package the Python runtime and cloudflared inside the app. Installation does not depend on Python, pipx or Homebrew on the client's Mac.

Proposed defaults for this first release: macOS 13 or newer; separate Apple Silicon and Intel downloads; SwiftUI for the setup/settings windows and AppKit for the menu. Architecture-specific packages avoid constructing universal binaries from unrelated Python distributions. The download page offers both with clear labels.

Compared with a standalone PKG, a menu-bar app provides the requested visible state and controls. A full native workspace would duplicate the existing browser interface. The small app is the selected approach.

## Client experience

1. Click **Download for Mac** in the hosted app, open the DMG, drag Orchestrator to Applications, and launch it.
2. A compact welcome window offers **Connect this Mac**. It opens the existing hosted pairing page with the one-time code populated. Browser authentication remains in the user's browser.
3. Choose an existing project folder using a native folder picker, or choose **Create a project** to open the browser creation flow. An unconfigured existing folder receives a browser setup flow; it never launches the interactive CLI wizard as the only way forward.
4. The existing setup screens check required tools and account connections. Missing optional tools do not prevent pairing or opening the interface. A job cannot start until its actual prerequisites are satisfied.
5. Enable **Start at login** during setup, with a visible choice. If macOS requires approval for background operation, show **Open Login Items Settings** and explain what to enable.
6. Finish with **Open Orchestrator**. Subsequent launches show the menu-bar icon and reuse the active project.

Launching from the DMG shows an **Install in Applications** instruction before registering background components. Reopening the app must bring back the existing menu instance rather than start a second agent.

## Menu and settings

The icon uses a monochrome template image; status is readable as text and is not conveyed by colour alone. The menu remains short:

```text
Orchestrator
Available locally · Remote access connected
Project: My App
2 runs in progress
─────────────────────────────────────────
Open Orchestrator…
Projects…
Remote access                     ✓
─────────────────────────────────────────
Settings…
Check for Updates…
Diagnostics…
Quit Orchestrator Menu
```

**Open Orchestrator** opens the local browser workspace through a short-lived sign-in grant. **Projects** opens the existing Projects page; the menu need not replicate project editing. When no project exists, both actions open setup instead.

**Remote access** enables or disables the tunnel and remote sign-ins while keeping local work and running jobs alive. Disabling it closes remote connections, invalidates the remote reachability status in the account, and leaves local access available. This replaces an ambiguous Pause control: it does not claim to pause builds or queue jobs. Turning it off persists across restarts.

**Settings** contains account/pairing state, Start at login, remote access, version, and **Stop Orchestrator on this Mac**. Stop is disabled while runs or background tasks are active and explains why. **Quit Orchestrator Menu** only exits the visible app; the separate agent continues, and the label makes that explicit. Stopping the agent also closes its tunnel and stops heartbeats. Launching the app again restarts it.

**Diagnostics** shows service state, local connectivity, tunnel state, package/API versions, prerequisite results and recent redacted errors. Its copy/export action excludes credentials, sign-in grants, command output, user code and pairing secrets. No separate log-upload service is introduced.

## Status model

Report independent dimensions rather than marking everything Online when only a process is running:

| Dimension | Values and meaning |
| --- | --- |
| Agent | Starting, running, stopped, failed |
| Setup | Needs pairing, needs project, ready |
| Local interface | Ready, starting, unavailable |
| Remote access | Off, connecting, connected, reconnecting, unavailable |
| Background permission | Enabled, requires approval, disabled |
| Activity | Number of active runs and background tasks |
| Update | Current, available, waiting for work to finish, installing, failed |

An offline network can coexist with a working local interface. An unpaired Mac can still complete local setup. The local interface starts before a tunnel connection attempt, so network delays do not freeze onboarding.

## Components and lifetime

- `desktop/macos/`: native menu-bar target, settings/welcome windows, a small bundled agent launcher, service registration and Sparkle integration.
- `orchestrator/desktop_agent.py`: entry point for the packaged agent. Owns desktop lifecycle state, the private control channel and coordinated startup/shutdown of the existing UI server, tunnel and account reporting.
- `orchestrator/desktop_protocol.py`: narrow, versioned control messages and validation, independent of project/job business logic.
- Existing `orchestrator/web/`: workspace and setup interface, with explicit support for startup before any project exists.
- `packaging/macos/`: reproducible bundle/DMG assembly, dependency manifest, signing and release verification commands.

The menu app and agent run as the signed-in user. Use Apple's `SMAppService.agent(plistName:)` for the app-bundled LaunchAgent and `SMAppService.mainApp` for the visible menu app's login preference. The native launcher resolves the runtime relative to the containing app and executes `python -P -m orchestrator.desktop_agent`. It does not embed developer-machine absolute paths. With Start at login enabled, launchd independently manages the agent, so quitting the menu cannot interrupt a run.

Start at login controls both login registrations. Changes that require unregistering/restarting the agent wait for its idle gate and display a pending state while work is active. With login startup disabled, launching the menu starts the bundled launcher as a separate, on-demand process that survives menu exit; launchd crash recovery is not promised in this mode. Switching between these modes stops the old instance while idle before starting the replacement. Registration status and a live agent response are checked separately: service registration alone does not prove the agent is healthy.

The CLI service workflow remains supported for CLI installs. The packaged app does not run `orchestrator service install` or replace a user's current service during design or testing.

## Native app / agent boundary

Use a Unix-domain socket in a short per-user application-support path, with a private parent directory and socket permissions. Both sides verify the peer UID. It is separate from the browser HTTP listener and is never routed through cloudflared. The app receives only status and structured results; it does not read the stored machine secret.

Requests are bounded JSON messages with protocol version, request ID, command and parameters. Allow only status, pairing start/cancel, browser grant, remote-access preference, diagnostics, stop-if-idle and prepare/cancel-update. No arbitrary shell commands, executable paths or file reads. Pairing is asynchronous; its result exposes the code/URL and claim state but not the poll or machine secret. Timeouts, unsupported versions and failed commands have typed, user-displayable errors.

Browser grants last at most 60 seconds and are consumed once to establish the normal owner session cookie, then redirect to a clean local URL. Only loopback requests can redeem them. Access logging and diagnostics redact the grant. The app validates a returned browser URL against the agent's known loopback origin before opening it. Existing remote ticket and owner/member checks continue to protect remote access; the new grant does not replace those.

Use an atomic instance lock to ensure one desktop agent per user. Inspect an occupied web port by authenticated identity rather than assuming any process on port 8765 is Orchestrator. Select an available loopback port for the packaged agent when an unrelated listener is present; tunnel configuration uses the actual bound port. Do not kill a process based on its name or port alone.

## First launch without a project

Currently `web.server.main()` and `service.install()` require an existing `project.json`. The packaged entry point must support a real bootstrap state instead of making a dummy project in the user's home directory.

In bootstrap state, serve a small local setup page and an explicit allowlist of authenticated, owner-only project-independent routes: pairing state, project registry, creation draft/create/publish and prerequisite detection. Project-specific routes return a structured `project_required` response before dereferencing a root. The UI opens setup rather than rendering empty job pages. No tunnel or remote workspace access starts until a valid project has been selected. The desktop agent reconciles pairing/project/preference changes and starts or stops the tunnel and heartbeat accordingly; it must not depend on pairing already existing at process startup.

Reuse `new_project.py` and project configuration/setup functions for browser setup. Selecting an existing folder requires explicit selection and config validation; a folder with no config opens setup, and save occurs only after the user applies the settings. The transition from bootstrap to a valid project occurs without changing the agent's control socket identity. Native calls remain available if the web workspace fails to start.

## Bundle, state and executable discovery

The app includes a pinned, architecture-specific Python 3.12 standalone installation, the built Orchestrator wheel and package data, cloudflared, and a CA certificate bundle for TLS. Record exact upstream versions, download digests and licences in a checked-in build manifest during implementation. Verify all inputs before bundling. Validate the distribution's minimum macOS version against the declared macOS 13 floor.

Install package source and resources in the private runtime so existing subprocess calls using `sys.executable`, `-m orchestrator`, package prompts/templates/static files and worker copying remain usable. Do not freeze the CLI entry point into a binary that changes these assumptions. Package metadata must report the release version rather than `dev`. Use the same release version across Python metadata and the native app, plus an increasing native build number.

Treat the installed bundle as immutable. Disable bytecode writes into it and never run pip, git pull or pipx reinstall against it. Packaged Update controls use the app updater. User state stays in `~/.orchestrator` and projects retain their current locations. Tokens use `user_state_dir()` consistently; logs are private, bounded and excluded from distribution.

Finder does not inherit a terminal PATH. Resolve bundled tools explicitly and use a deterministic search path covering system tools, `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin` and recorded user-selected tool directories. Do not source shell startup files. Missing tool screens offer native executable selection or provider download/sign-in links. Launch environment discovery must be shared by the UI's subprocesses and the service.

GitHub, AI-provider CLIs, Git, Xcode and project-specific toolchains remain explicit prerequisites. This first release removes Terminal from installing and starting Orchestrator; it does not promise that every optional third-party tool can be installed or authenticated without its own setup. The prerequisite UI must state this clearly before a user starts dependent work.

## Updates, stopping and removal

Use Sparkle 2 for app updates, with HTTPS feeds and signed archives. Update the native shell, private Python package/runtime and tunnel binary as one release. Verify Apple signatures and Sparkle signatures through the framework, rather than treating a download checksum as publisher authentication.

Before replacement, the agent atomically enters an update-preparation state: reject new runs, project mutations and background tasks, then check all existing work. If busy, cancel preparation and show **Update when current work finishes**. Recheck and acquire the gate again once idle. An accepted update unregisters/stops the agent before replacing the bundle; launchd must not repeatedly restart an old interpreter against new files. After relaunch, reconcile registration and start the new agent. If replacement fails, restore normal admissions and the original registration. Download failure leaves the current installation usable.

Stop-if-idle uses the same admission gate and live activity checks. Active work is not force-stopped by closing the menu, changing login preferences, updating or toggling remote access.

Settings provides **Remove background components**, which unregisters this app's services after the idle check, and explains how to move the app to Trash. User settings, projects, pairing and audit history are retained by default. Account disconnection is a separate explicit action using the existing machine-removal flow.

## Existing installations

Detect the legacy `com.orchestrator.ui` service and a running authenticated CLI server. Offer a migration explanation during setup. Import the existing per-user settings/projects/pairing by reusing their current locations. Stop and remove only the identified legacy service after the user accepts migration and it is idle; preserve a recoverable copy of its service definition. Never uninstall Python, pipx or third-party tools.

A manually started CLI server is left running, with a message explaining how to stop it or use the existing browser interface. The desktop app must not silently start two independently active orchestrators for the same user state. CLI and desktop runs use an agreed state lock during migration/coexistence. Updating fleet workers remains separate from updating the desktop bundle.

## Delivery and verification

Build locally first. Produce separate unsigned development DMGs for both architectures; clearly distinguish these from client releases. Add a manual macOS release workflow to build from a fixed commit, sign nested binaries/frameworks, sign the app and DMG, notarize and staple, and verify the final artifact. Signing credentials and Sparkle private keys come from protected build inputs, never the repository. Public download links/appcast publication follow successful artifact validation.

Validate Python changes with `docs/build-test-commands.md`, add meaningful protocol/bootstrap/lifecycle regression tests, and build/test the native targets. Desktop smoke tests must use isolated user state and test service identifiers, never the developer's live tunnel or LaunchAgent.

Release acceptance requires an installation test on a clean Mac without developer Python, pipx, Homebrew or cloudflared. Exercise both CPU architectures and the oldest supported OS; these can be separate Macs or appropriate VMs. Check pairing, projectless setup, grant replay/expiry, browser access, restart/login startup, background approval disabled, port conflicts, missing tools, tunnel loss/recovery, menu quit during a run, remote-access off during a run, idle stop, update blocked by active work, update/relaunch and legacy migration. Verify TLS normally; never disable certificate checks to make the test pass.

The design is complete when approved. Implementation is complete when the native app and bundled runtime pass these checks. Distribution-ready additionally requires signed/notarized artifacts and clean-machine results; a successful build on the developer's machine alone is insufficient.

## References

- [Apple SMAppService](https://developer.apple.com/documentation/servicemanagement/smappservice): registration and approval of app-bundled background services on macOS 13 and later.
- [Apple agent(plistName:)](https://developer.apple.com/documentation/servicemanagement/smappservice/agent(plistname:)): LaunchAgent plist location in the app bundle.
- [Apple notarization](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution): outside-App-Store distribution.
- [Sparkle documentation](https://sparkle-project.org/documentation/): updater integration, signing and appcast requirements.
- [Python standalone archives](https://github.com/astral-sh/python-build-standalone/blob/main/docs/distributions.rst): self-contained installation archives.
