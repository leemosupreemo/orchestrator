# macOS Menu-Bar App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a simple visible macOS app that clients install, pair and start without installing Orchestrator through Terminal.

**Architecture:** A native menu app controls a separate per-user Python agent through a private Unix socket. The agent serves the existing browser workspace, including a new projectless setup mode. The signed application contains its runtime and tunnel binary and updates as one unit after work finishes.

**Tech Stack:** Python 3.12, stdlib HTTP/socket/threading/locking, Swift/AppKit/SwiftUI/ServiceManagement, Sparkle 2, Swift Package Manager, macOS signing/notarization tools.

**Spec:** `docs/superpowers/specs/2026-10-03-macos-menu-bar-design.md`

## Global Constraints

- macOS 13 or newer; separate Apple Silicon and Intel downloads.
- Bundle Python 3.12, Orchestrator source/package data, cloudflared and CA certificates; no client Python, pipx or Homebrew requirement.
- Preserve `~/.orchestrator` state and existing project locations. Honour `ORCHESTRATOR_USER_STATE_DIR` in tests.
- Browser grants last at most 60 seconds and are consumed once. Never expose pairing/machine secrets to the native app.
- Quitting the menu, toggling remote access, changing login preferences and updating must not stop active work.
- Keep the application bundle immutable; packaged updates do not invoke pip, pipx or git pull.
- Never register services, stop the user's live server/tunnel, or publish a release as a test side effect.
- GitHub/AI CLIs, Git, Xcode and project toolchains remain explicit prerequisites.
- Use `docs/build-test-commands.md` for Python validation. Run `orchestrator check-config` after changing the repository's `.orchestrator/project.json`; temporary project configs are validated through their isolated fixtures.

## Review Focus

- A project disappears or loses its config between selection and restart: return to setup without inventing a project or corrupting the registry (Tasks 2/4).
- A socket path is stale, too long, a symlink or occupied by another UID: refuse unsafe cleanup and report an actionable error (Task 4).
- Stop/update races with a new run or an existing background operation: serialize admission and refuse replacement until all work is idle (Tasks 1/8).
- Finder/launchd supplies a minimal environment and an app path with spaces: locate the bundled runtime/tools and trust store without using a developer PATH (Tasks 6/7).
- A remote tunnel forwards a request from loopback: it must not redeem a desktop browser grant or reach native controls (Tasks 3/4).

## Structure and checkpoints

Use one integrated plan because setup, the agent, the native menu and packaging share the same lifecycle/protocol. Checkpoints produce independently testable results: Tasks 1–4 deliver the controllable agent; Tasks 5–7 deliver an installable development app; Tasks 8–10 make updates, migration and public distribution ready.

New Python modules: `runtime_control.py` (locking/admission), `project_setup.py` (setup business logic), `web/bootstrap.py` (projectless routes), `web/browser_grants.py` (browser handoff), `desktop_protocol.py` (wire contract), `desktop_agent.py` (coordination), `desktop_runtime.py` (bundle environment), `desktop_diagnostics.py` (allowlisted export), `desktop_migration.py` (legacy detection).

Native source is a Swift package under `desktop/macos/`, with `DesktopCore`, `Orchestrator` and `OrchestratorAgentLauncher` targets. `DesktopCore` owns testable state/protocol/service/update adapters; AppKit/SwiftUI adapters stay in the executable. Packaging scripts live in `packaging/macos/` and assemble the `.app` from Swift build products and the pinned runtime rather than adding XcodeGen as a build dependency.

## Task 1: Serialize lifecycle operations with work admission

**Files:** Create `orchestrator/runtime_control.py`, `tests/test_runtime_control.py`; modify `orchestrator/web/server.py`.

**Interfaces:** Produce `InstanceLease.acquire(path: Path) -> InstanceLease`, `InstanceLease.close() -> None`, `ActivityGate.admit() -> ContextManager[None]`, `ActivityGate.prepare(reason: str) -> bool`, `ActivityGate.cancel() -> None`, `ActivityGate.snapshot() -> dict[str, Any]`. `prepare` atomically closes admissions only if admitted work is zero; return false and leave admissions open when busy. Lease metadata identifies PID/install kind without carrying secrets.

- [ ] **1. Write lifecycle regression tests.** `test_prepare_refuses_active_work`: inside `admit()`, assert `prepare("update") is False`; admission remains usable. `test_prepare_closes_admission_atomically`: coordinate competing threads with barriers, assert exactly one of preparation or new admission succeeds. `test_second_instance_refused_and_crashed_holder_recoverable`: use separate subprocesses, assert a live lock cannot be stolen and kernel release permits recovery.
- [ ] **2. Run** `python3 -m unittest tests.test_runtime_control -v`; confirm the new contracts fail before implementing them.
- [ ] **3. Implement the contracts.** Use OS advisory locks on a stable file, never PID existence alone or deleting the locked file. Wire `SessionManager.start` to retain admission until its subprocess exits; wire `BackgroundTasks.start` to retain admission until its work completes. Mutation handlers retain short admissions through completion. Add gate status to server instance state; do not change role authorization.
- [ ] **4. Run** the focused module and `python3 -m unittest tests.test_web_ui -v`. Test that running task retention does not expire merely because its result-retention window elapses. Count command sessions and background work, not just job IDs.
- [ ] **5. Commit** only these files: `feat(runtime): gate agent shutdown and updates on active work`.

## Task 2: Serve first-run setup without a configured project

**Files:** Create `orchestrator/project_setup.py`, `orchestrator/web/bootstrap.py`, `orchestrator/web/static/setup.js`, `tests/test_project_setup.py`, `tests/test_web_bootstrap.py`; modify `orchestrator/web/server.py`, `orchestrator/project_config.py`, `orchestrator/web/static/app.js`, `orchestrator/web/static/index.html`, `orchestrator/web/static/style.css`; reuse `orchestrator/new_project.py`, `orchestrator/cli.py`, `orchestrator/config_validation.py`.

**Interfaces:** Produce `inspect_project(root: Path) -> dict[str, Any]` (keys `root`, `configured`, `inferred`, `errors`), `apply_project_setup(root: Path, values: dict[str, Any]) -> dict[str, Any]` (keys `ok`, `root`, `errors`), and `dispatch_bootstrap(handler: UIHandler, method: str, parts: list[str]) -> bool`. Extend `load_project_config(root: Path | None = None) -> ProjectConfig` without changing no-argument CLI behavior; never change global cwd/environment to load an HTTP-selected project. `UIServer` accepts `root: Path | None`; non-null callers retain their existing behavior. `GET /api/bootstrap` returns `needs_project`, `selected_root`, `projects`, `runner`, `you`. `POST /api/setup/inspect` and `/api/setup/apply` consume explicit folder/settings selections.

- [ ] **1. Write HTTP tests using an isolated `UIServer(root=None)`.** `test_fresh_install_serves_setup_without_dummy_project`: assert HTTP 200 and no generated `project.json`. `test_project_routes_need_project_before_dereference`: authenticated `/api/jobs` returns HTTP 409 with `code == "project_required"`. `test_unconfigured_folder_requires_apply`: inspection writes nothing; explicit apply validates and saves. `test_removed_active_project_returns_to_setup`: no 500 or fabricated registry entry. Include unauthenticated/member/hostile-origin requests and require refusal.
- [ ] **2. Run** `python3 -m unittest tests.test_project_setup tests.test_web_bootstrap -v`; confirm the missing bootstrap/setup contracts fail.
- [ ] **3. Implement rootless dispatch before project-specific code.** Allow only the bootstrap/setup endpoints, existing project registry and new-project draft/create/publish endpoints in rootless state. Use `new_project.py` for project creation, then show config setup because `create_project` alone does not supply a valid Orchestrator config. Extract only the noninteractive setup helpers needed from the CLI; never call `run_wizard` in HTTP. Use existing validation and protect existing config from implicit overwrite. Rootless auth accepts the local owner credential only; project-backed sign-ins keep existing roles. Advance to project mode only when the resulting config validates.
- [ ] **4. Run** the new tests, `python3 -m unittest tests.test_new_project tests.test_config_validation tests.test_web_ui -v`, and `node --check orchestrator/web/static/setup.js`. Exercise the browser route from projectless startup through explicit apply to Home; prerequisite errors retain inputs and explain the missing tool.
- [ ] **5. Commit:** `feat(setup): open the browser workspace before the first project exists`.

## Task 3: Open the browser without sharing the permanent owner token

**Files:** Create `orchestrator/web/browser_grants.py`, `tests/test_browser_grants.py`; modify `orchestrator/web/server.py`.

**Interfaces:** Produce `BrowserGrantStore.issue(route: str) -> str`, `BrowserGrantStore.consume(grant: str) -> str | None`, with an injectable monotonic clock and 60-second maximum lifetime. The private desktop listener redeems `GET /desktop/open?grant=...`, establishes the normal owner cookie and redirects to the validated local route. No mint/redeem API is available on the remotely tunneled listener.

- [ ] **1. Write tests.** `test_grant_expires_at_sixty_seconds`: assert consume returns `None` at the deadline. `test_one_consumer_wins_concurrent_replay`: exactly one thread gets the route. `test_tunnel_cannot_redeem_grant`: requests on the tunnel-target listener get 404 even with forged Host/forwarded headers and loopback source. `test_clean_redirect_and_no_grant_logging`: cookie is HttpOnly/SameSite and Location/access output contain no secret. Reject external, protocol-relative and control-character routes.
- [ ] **2. Run** `python3 -m unittest tests.test_browser_grants -v`; confirm failures.
- [ ] **3. Implement two listeners sharing one server context.** One loopback desktop listener permits redemption; a separate loopback listener is the cloudflared target and categorically disables it. Do not use peer IP, Host or `X-Forwarded-For` alone: cloudflared connects locally. Native grants open the desktop listener, which serves the same workspace and authorization state. Normalize the UI token storage to `user_state_dir()` with private atomic writes. Keep legacy CLI startup unchanged when desktop mode is absent.
- [ ] **4. Run** the grant and existing auth/role HTTP tests. Assert the remote listener cannot mint grants, access private desktop controls or weaken single-use remote account tickets.
- [ ] **5. Commit:** `feat(desktop): add one-time local browser handoff`.

## Task 4: Build the controllable background agent

**Files:** Create `orchestrator/desktop_protocol.py`, `orchestrator/desktop_agent.py`, `tests/test_desktop_protocol.py`, `tests/test_desktop_agent.py`; modify `orchestrator/account.py`, `tests/test_control_plane.py`, `orchestrator/web/server.py` as needed for reusable lifecycle hooks.

**Interfaces:** Produce `DesktopAgent(state_dir: Path, control_dir: Path, runtime: dict[str, str], dependencies: AgentDependencies)`, `DesktopAgent.run() -> int`, `DesktopAgent.handle(command: str, params: dict[str, Any]) -> dict[str, Any]`, `validate_request(raw: bytes) -> dict[str, Any]`, `control_socket_path(control_dir: Path) -> Path`. Use newline-framed UTF-8 JSON, protocol version `1`, max message `65536` bytes, socket I/O timeout `5` seconds. Request: `{version, request_id, command, params}`. Reply: `{version, request_id, ok, result}` or `{version, request_id, ok:false, error:{code,message}}`.

`AgentDependencies` is a dataclass whose injectable `pair_start`, `pair_poll`, `heartbeat`, `tunnel_start` and `tunnel_stop` callables wrap the existing account/tunnel implementations. Account contracts are `start_pairing(name: str | None = None) -> dict`, `poll_pairing(pairing: dict) -> dict`, `finish_pairing(pairing: dict, claimed: dict) -> dict`; `connect()` reuses these and retains existing CLI output. Status result keys are `agent`, `setup`, `local_interface`, `remote_access`, `activity:{runs,tasks}`, `local_origin`, `project:{name,root}|null`, `pairing:{state,code,url,owner_email}`, `runner:{version,api_version}`, `update`, `remote_enabled`. Pairing secrets never appear in these results. `browser_grant` accepts an allowlisted `route` and returns `{url}`. `remote_access` consumes `{enabled:bool}`. Lifecycle commands return `{accepted:bool,reason:string}`. Native-only background permission is merged into display state by Task 7.

- [ ] **1. Write real socket and fake-dependency lifecycle tests.** Malformed JSON, unknown command/version, excess frame size and peer UID mismatch return typed refusals. A cancelled/expired pairing never persists credentials; successful pairing updates status without restarting. `test_local_ready_before_slow_tunnel`, `test_remote_off_leaves_running_session_alive`, `test_unrelated_port_listener_uses_available_port`, `test_busy_stop_refused`, `test_stale_socket_cleanup_is_owned_only` assert those named outcomes. Long/symlink socket paths must fail safely.
- [ ] **2. Run** `python3 -m unittest tests.test_desktop_protocol tests.test_desktop_agent -v`; confirm failures.
- [ ] **3. Implement allowlisted commands** `status`, `pair_start`, `pair_cancel`, `browser_grant`, `remote_access`, `diagnostics`, `stop_if_idle`, `prepare_update`, `cancel_update`. Place the socket under `~/Library/Application Support/Orchestrator/control/agent.sock` by default, with an injected short temp path in tests; directory 0700/socket 0600, verify both peer UIDs, reject symlinks and unowned stale paths. Status includes all spec dimensions and `local_origin` but no credentials/transcripts. Refactor pairing into reusable start/poll/cancel functions so cancelling interrupts between bounded polls. Reconcile project, pairing and persisted `desktop.json` preferences; start local listeners before asynchronously attempting a tunnel. Persist remote-off and report an empty endpoint to the account. Suppress remote sign-in admission while off. Acquire Task 1's shared state lease before serving; detect legacy state ownership separately from an unrelated occupied port.
- [ ] **4. Run** the focused tests and `python3 -m unittest tests.test_control_plane tests.test_web_bootstrap tests.test_browser_grants -v`. Start one isolated agent subprocess without a project, request status/open setup, then stop it through the socket; use fake tunnel/control-plane services only.
- [ ] **5. Commit:** `feat(desktop): coordinate pairing and the background agent`.

## Task 5: Deliver the visible menu and first-launch window

**Files:** Create `desktop/macos/Package.swift`, `desktop/macos/Sources/DesktopCore/{ProtocolModels,AgentClient,DesktopState,BrowserOpener}.swift`, `desktop/macos/Sources/Orchestrator/{AppMain,MenuController,WelcomeView,SettingsView,DiagnosticsView}.swift`, `desktop/macos/Sources/OrchestratorAgentLauncher/main.swift`, `desktop/macos/Tests/DesktopCoreTests/{AgentClientTests,DesktopStateTests,BrowserOpenerTests}.swift`.

**Interfaces:** `AgentClient.request(command: String, params: [String: JSONValue]) async throws -> AgentReply`; `DesktopState.refresh() async`; `BrowserOpener.openGrant(_ result: BrowserGrantResult, trustedOrigin: URL) throws`; use Codable models matching Task 4. Define `JSONValue` as a recursive Codable JSON enum, `AgentReply` as Task 4's envelope and `BrowserGrantResult` as its `{url:String}` result. The launcher accepts only its bundle-relative runtime and explicit test configuration, never a remote-provided executable.

- [ ] **1. Write native contract/state tests.** Assert typed protocol errors and stale-response rejection, local-ready/remote-off text, unknown activity disabling Stop, failed-grant origin validation, and socket disconnect leaving an actionable unavailable state. A malicious URL or pairing URL outside the configured hosted origin is never opened.
- [ ] **2. Run** `swift test --package-path desktop/macos`; confirm the missing implementation fails.
- [ ] **3. Implement an accessory-policy AppKit menu app with SwiftUI windows.** Use a template SF Symbol through `NSStatusItem`, match the approved menu labels, and keep network/socket work off the main thread. Welcome connects/polls pairing, uses `NSOpenPanel` for existing folders and opens the browser setup/create flow. Settings exposes login/remote/account/idle-stop controls; diagnostics exports Task 9's safe data once available. Clicking Projects opens `#/projects`, or setup when no valid project exists. Acquire a separate menu-instance lease and activate the existing instance on relaunch. The launcher resolves containing-app paths and `exec`s the private Python module; development tests inject a fixture runtime. Quit Menu never sends stop.
- [ ] **4. Run** Swift tests/build and a development menu smoke test against an isolated Task 4 agent. Verify reopening setup, folder cancellation, keyboard/accessibility labels, the menu remaining responsive during slow pairing, and a run surviving menu quit.
- [ ] **5. Commit:** `feat(macos): add the Orchestrator menu and welcome window`.

## Task 6: Assemble an immutable app with its private runtime

**Files:** Create `orchestrator/desktop_runtime.py`, `packaging/macos/{build.py,dependencies.json,Info.plist,THIRD_PARTY_NOTICES.md}`, `tests/test_desktop_runtime.py`, `tests/test_macos_packaging.py`; modify `orchestrator/cli.py`, `orchestrator/web/server.py`, `pyproject.toml` only for packaging/version/update dispatch.

**Interfaces:** `bundle_environment(bundle: Path, user_paths: list[Path]) -> dict[str, str]`; `is_packaged_install() -> bool`; `build_bundle(architecture: str, output_dir: Path, manifest: Path, version: str, build_number: int) -> Path`. Developer command: `python3 packaging/macos/build.py --arch arm64 --output dist/macos --build-number 1 --development`; equivalent `x86_64` output is separate.

- [ ] **1. Write packaging/runtime tests.** Assert paths with spaces work; system/Homebrew/user-selected search directories are deterministic; malformed architecture/digest/archive traversal is refused; installed package data and metadata are present; packaged Update returns an app-update instruction instead of invoking pipx. Missing release version/build number fails before signing. Assert no source secrets/runtime state enter the assembled app.
- [ ] **2. Run** `python3 -m unittest tests.test_desktop_runtime tests.test_macos_packaging -v`; confirm failures.
- [ ] **3. Implement reproducible assembly.** Select a stable Python 3.12 standalone release, cloudflared release and CA bundle from their official publishers; check in their exact HTTPS URLs, versions, SHA-256 digests and licences for each architecture. No `latest` URLs or zero/sample digests. Build the wheel, install it and certificates into a staged private runtime, build the Swift executables and assemble `Contents/{MacOS,Resources,Library}`. Set macOS minimum `13.0`, native/package release versions equal, and increasing build number. Export `PYTHONDONTWRITEBYTECODE=1`, `PYTHONNOUSERSITE=1`, verified CA path, explicit packaged-install marker, and shared executable search paths. Never source shell init files or write to the installed app. Package assets/prompts/templates/config and preserve required symlinks/permissions. Validate all Mach-O architectures and linked runtime dependencies.
- [ ] **4. Run** focused tests, build both architecture bundles, and invoke each executable on a matching host or architecture emulator where supported. On the local architecture, relocate the app into a temp directory with spaces, remove developer PATH/PYTHONPATH, and verify `sys.executable` subprocesses, package version/resources and HTTPS with normal certificate validation. Foreign build success is not claimed as a clean-machine runtime pass.
- [ ] **5. Commit:** `feat(packaging): bundle the macOS app runtime and tunnel`.

## Task 7: Add login startup, idle stop and removal

**Files:** Create `desktop/macos/Sources/DesktopCore/{ServiceController,LifecycleCoordinator}.swift`, `desktop/macos/Tests/DesktopCoreTests/ServiceControllerTests.swift`, `packaging/macos/agent.plist`; modify native Settings/AppMain, bundle assembly and agent lifecycle tests.

**Interfaces:** `ServiceController.registrationState() -> RegistrationState`, `setStartAtLogin(_ enabled: Bool) async throws`, `removeBackgroundComponents() async throws`; `LifecycleCoordinator.ensureRunning() async throws`. Define `RegistrationState` with `enabled`, `requiresApproval`, `notRegistered`, `notFound` and retain desired-vs-actual mode separately. Injectable adapters wrap `SMAppService.mainApp`, `SMAppService.agent(plistName:)` and on-demand launcher start; test doubles never call real registrations.

- [ ] **1. Write service transition tests.** Enabled/requiresApproval/notFound states are distinct from live agent health; disabling login while busy remains pending; idle mode changes replace exactly one agent; Quit Menu leaves it alive; Stop idle prevents KeepAlive restarting it; Remove retains state/project files. A running app on a DMG refuses registration and displays installation instructions.
- [ ] **2. Run** `swift test --package-path desktop/macos`; confirm new service tests fail.
- [ ] **3. Implement app-bundled LaunchAgent registration** using a unique desktop label `com.orchestrator.desktop.agent` and `BundleProgram` resolving the native launcher. Put its plist in `Contents/Library/LaunchAgents`. Require explicit setup choice before registration and offer `SMAppService.openSystemSettingsLoginItems()` for approval. In on-demand mode launch the separate process without kill-on-parent-exit behavior. Transition/unregister only after acquiring the agent idle gate; restore prior registration on failure. Persist desired mode plus whether an idle stop intentionally suspended the agent, so normal relaunch can resume. Private bounded service logs belong outside the bundle.
- [ ] **4. Run** Swift and Python lifecycle tests. Provide an opt-in integration fixture with a distinct bundle/service identifier and isolated state; test login approval and actual launchd restart on a disposable profile. Record unrun real-login checks instead of using the developer's existing LaunchAgent.
- [ ] **5. Commit:** `feat(macos): manage login startup and background lifecycle`.

## Task 8: Update the whole application after work is idle

**Files:** Create `desktop/macos/Sources/DesktopCore/UpdateCoordinator.swift`, `desktop/macos/Sources/Orchestrator/SparkleAdapter.swift`, `desktop/macos/Tests/DesktopCoreTests/UpdateCoordinatorTests.swift`; modify `Package.swift`, `Info.plist`, assembly, agent lifecycle tests.

**Interfaces:** `UpdateCoordinator.prepareInstallation() async throws`, `cancelInstallation() async`, `recoverAfterRelaunch() async`; use `prepare_update`/`cancel_update` from Task 4 and services from Task 7. Persist only a nonsecret recovery journal containing previous desired registrations, pending transition and update identity.

- [ ] **1. Write tests.** A new HTTP mutation racing prepare cannot enter after the gate is acquired; background project creation prevents update; busy updates display waiting and release admissions; an unknown/disconnected agent never counts as idle; installation failure restores the original mode. Assert a pending Sparkle install plus Quit Menu during active work does not replace the bundle or stop its agent.
- [ ] **2. Run** Swift update tests and Python activity/lifecycle tests; confirm the new orchestration fails before implementation.
- [ ] **3. Integrate a pinned Sparkle 2 release** and its matching API. Verify HTTPS appcast/archive signatures through Sparkle, copy/sign all its frameworks/helpers, and embed only the public update key. Permit checks/downloads while busy but gate final replacement. Coordinate app termination, automatic installation and install-on-quit paths, not only `shouldPostponeRelaunchForUpdate` (that hook is not guaranteed on every path). Disable unattended installation until each path is covered. Idle installation stops/unregisters the old agent, then hands control to Sparkle; relaunched app reconciles journal/registrations and starts the new runtime. Failure/cancellation releases the gate and recovers the old app. Missing feed/key configuration produces a clear development-mode status, not a fake successful update.
- [ ] **4. Run** native/Python tests and a two-version signed development-feed fixture when credentials exist. Exercise bad signatures, unreachable feed, busy worker, menu quit, registration disabled and cancelled installation. Keep downloaded fixtures and keys outside git.
- [ ] **5. Commit:** `feat(macos): coordinate signed updates with active work`.

## Task 9: Migrate legacy installs and expose safe diagnostics

**Files:** Create `orchestrator/desktop_migration.py`, `orchestrator/desktop_diagnostics.py`, `tests/test_desktop_migration.py`, `tests/test_desktop_diagnostics.py`; modify `orchestrator/service.py`, native welcome/diagnostics and desktop agent integration.

**Interfaces:** `inspect_legacy(state_dir: Path, runner: Callable) -> dict[str, Any]`; `migrate_legacy(state_dir: Path, consent: bool, runner: Callable) -> dict[str, Any]`; `diagnostics_snapshot(status: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]`. Migration operates only on verified legacy label `com.orchestrator.ui`; diagnostics serializes an allowlist, not arbitrary log lines followed by regex cleanup.

- [ ] **1. Write tests.** Non-consent, foreign plist/UID, manual CLI server, busy legacy agent and stale PID never trigger a kill/unregister; accepted idle migration creates a recoverable service-definition backup and retains credentials/projects. A legacy server without the new idle-gate capability requires the user to stop it manually. Diagnostics excludes maliciously injected secret/error/transcript fields while retaining actionable typed states.
- [ ] **2. Run** `python3 -m unittest tests.test_desktop_migration tests.test_desktop_diagnostics -v`; confirm failures.
- [ ] **3. Implement detection and native migration UI.** Authenticate instance identity, inspect only exact known service paths and prefer kernel lease ownership over port/name matching. Teach CLI UI/service startup to participate in the shared state lease; do not retrofit unsupported migration commands onto older running servers. Reuse existing state rather than copying credentials. Backup only the identified service definition to private state storage, and report how to recover it. Keep orphan/manual instances visible with a browser-open action. Export only version/state/tool presence/result codes and sanitized summaries; exclude command arguments, file content, project paths and raw exception text.
- [ ] **4. Run** migration/diagnostics plus `tests.test_service` and `tests.test_web_ui`; exercise Settings export to a user-chosen file and assert no known fixture secrets occur in output. Verify uninstall does not erase pairing/history or unregister unrelated services.
- [ ] **5. Commit:** `feat(desktop): migrate legacy services and show safe diagnostics`.

## Task 10: Produce release artifacts and verify installation

**Files:** Create `packaging/macos/{release.py,smoke.py}`, `.github/workflows/macos-release.yml`, `docs/macos-desktop.md`, `tests/test_macos_release.py`; modify `docs/build-test-commands.md`, `packaging/macos/build.py`, hosted download UI in `orchestrator/web/static/account.js` when verified release URLs exist.

**Interfaces:** `release.py --bundle PATH --output DIR --signing-identity ID --notary-profile PROFILE`; `smoke.py --bundle PATH --state-dir PATH --control-dir PATH`. DMGs contain the app and an Applications symlink. Manual workflow inputs: fixed ref, release version, increasing build number, and development/signed mode; development artifacts are never public client downloads.

- [ ] **1. Write release tests.** Reject missing signatures/notary inputs in release mode, mismatched native/Python versions, wrong architecture, unsupported deployment target and unverified dependency digests. Assert workflow artifacts exclude credentials/logs and hosted links stay disabled until both verified architecture artifacts are available. Require a matching executable—not just a file named Orchestrator—in smoke checks.
- [ ] **2. Run** `python3 -m unittest tests.test_macos_release -v`; confirm failures.
- [ ] **3. Implement artifact construction and protected manual release workflow.** Sign nested code/frameworks with Developer ID Application and hardened runtime, sign app/DMG, submit with notarytool, staple and verify with codesign/spctl before uploading artifacts. Use protected secrets/Keychain inputs, Sparkle signing and GitHub release assets; publish the appcast only after verified artifacts exist. Default workflow performs builds/artifact upload without publishing a public release. Public release creation and hosting deployment remain an explicit rollout action. Add architecture download labels and update the hosted page's CLI-only onboarding when URLs are real. Document setup, prerequisite limits, menu/agent quit differences, migration and removal.
- [ ] **4. Run canonical validation:** `python3 -m compileall -q orchestrator tests`, `python3 -m unittest discover -s tests`, `swift test --package-path desktop/macos`, both native builds and `git diff --check`. Build/inspect development DMGs. Run `smoke.py` with isolated state on the local architecture. Complete clean-machine acceptance on Apple Silicon and Intel, macOS 13 and a current OS, covering the spec's login/pairing/setup/tunnel/port/quit/update/migration scenarios. Record unavailable signing or test machines as release blockers; do not claim distribution readiness from developer-only checks.
- [ ] **5. Commit:** `feat(release): package and validate macOS desktop distribution`; request a whole-branch review and hand off the actual app/DMG paths plus validation evidence. Do not merge, install live background services, or publish during validation.

## Execution and completion record

Recommended execution: native, in the current session, because tasks share bootstrap, control and lifecycle contracts. Establish an isolated worktree at execution start and keep each commit reviewable. A final independent review checks authorization, admission races, bundle mutability, migration targets and update/quit interactions.

Product completion requires Tasks 1–10 and their relevant checks. Signed public artifacts additionally require the owner's signing/notarization/update-key configuration and clean-machine evidence. Missing release credentials should not prevent building and validating the development app, but must keep public download publication disabled.

## Update API reference

Use the pinned version's [Sparkle updater delegate API](https://sparkle-project.org/documentation/api-reference/Protocols/SPUUpdaterDelegate.html). Its relaunch deferral and install-on-quit hooks have different conditions; a single relaunch callback is insufficient evidence that active work is protected.
