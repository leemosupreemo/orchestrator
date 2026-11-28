# Headless Mac Enrollment and Unattended Updates Implementation Plan

**Goal:** Set up `Orchestrator.app` on a Mac nobody sits at, over SSH, with one command from the hosted app, and keep it updated without anyone clicking.

**Architecture:** One-time enrollment tokens in the control plane; an install script that verifies and installs the same signed app as the DMG; an `enroll` command in the bundled runtime; a background handoff in the menu app that registers login items; Sparkle checks and installs automatically or on request, always through the existing idle gate.

**Spec:** `docs/superpowers/specs/2026-10-03-headless-mac-enrollment-design.md`

## Global Constraints

- Builds on the `macos-menu-bar` work. No separate app, build or agent.
- Tokens: ≥128 bits, stored only as SHA-256, 15-minute lifetime, single use, owner-only creation.
- Never print or log machine secrets, tokens after redemption, or grants.
- Never replace the bundle while work is admitted; unknown activity is never idle; never block logout or shutdown.
- Never change power, login or FileVault settings; only report them.
- Tests never register login items, call real `launchctl`/`pmset`/`open`, contact the real control plane or download releases.
- **Add a Mac** and `install-mac.sh` stay unpublished until signed releases and the update feed exist.
- Validate with `docs/build-test-commands.md`, including `swift test --package-path desktop/macos`.

## Task 1: Enrollment tokens in the control plane

**Files:** `cloud/functions/control_plane.py`, `tests/test_control_plane.py`.

- [ ] Tests: create requires a signed-in owner and returns a token once; redeem creates a machine identical in shape to a claimed pairing; reuse, expiry, an unknown token and the per-account limit are refused; only the hash is stored; the owner receives a "New Mac added" push; the new machine is marked as added with a command.
- [ ] Implement `create_enrollment`, `redeem_enrollment` and routes `POST /enroll/create` (person) and `POST /enroll/redeem` (computer). Sweep expired enrollments with pairings.
- [ ] Commit: `feat(control-plane): one-time enrollment tokens for headless Macs`.

## Task 2: `enroll` in the bundled runtime

**Files:** create `orchestrator/enroll.py`, `tests/test_enroll.py`; modify `orchestrator/account.py`, `orchestrator/cli.py`.

- [ ] Tests (injected runner, console user, control plane and clock): no console session or root is refused with automatic-login guidance; an app on a mounted image is refused; a token redeems once and saves the machine; an already-enrolled Mac reuses its identity for the same account and refuses a different account; a token on stdin works; a local path project is used, a git URL is cloned into `~/Projects/<name>`, and an existing folder is never overwritten; missing inferred settings stop with a list and keep the clone; output contains no secrets.
- [ ] Implement `orchestrator enroll --token|--token-stdin --project PATH_OR_URL [--name NAME]`, reusing `account.save_machine`, `project_setup.inspect_project/apply_project_setup` and `remember_project`. Hand off with `open -g -j -a <bundle> --args --register-background` and wait (60 s) for the agent socket to report `setup: ready`.
- [ ] Commit: `feat(enroll): set up a headless Mac from one command`.

## Task 3: Background registration handoff in the menu app

**Files:** `desktop/macos/Sources/Orchestrator/AppMain.swift`, `MenuController.swift`, `DesktopCore/ServiceController.swift`, `Tests/DesktopCoreTests/ServiceControllerTests.swift`.

- [ ] Tests: `--register-background` registers the menu and agent login items with no window, starts the agent, records `requiresApproval` distinctly, and leaves an already-running menu instance to do it instead of starting a second.
- [ ] Implement the launch argument and a hidden path through `ServiceController.setStartAtLogin(true)`.
- [ ] Commit: `feat(macos): register background items without a window`.

## Task 4: Install script

**Files:** create `packaging/macos/install-mac.sh`, `tests/test_install_script.py`.

- [ ] Tests run the script against fake `curl`, `hdiutil`, `codesign`, `spctl`, `ditto`, `stat` and `uname` on `PATH`: root and no-console are refused; the architecture picks the right record; a digest or signature mismatch stops before anything is copied or run; `/Applications` falls back to `~/Applications`; arguments pass through to `enroll`; `sh -n` and, if installed, `shellcheck` pass.
- [ ] Implement with POSIX `sh`, `set -eu`, a temp folder cleaned on exit, and the release record URL as the only input from hosting.
- [ ] Commit: `feat(packaging): verified install script for headless Macs`.

## Task 5: Unattended updates

**Files:** `desktop/macos/Sources/DesktopCore/UpdateCoordinator.swift`, new `DesktopCore/UpdateScheduler.swift`, `Sources/Orchestrator/SparkleAdapter.swift`, `SettingsView.swift`, `packaging/macos/Info.plist`, tests.

- [ ] Tests (injected clock and agent status): with the setting off, nothing is checked automatically; on, a check runs every 6 hours; a downloaded update installs only after 10 idle minutes; work starting before install defers it; an unavailable agent defers it; a failed install restores the previous mode; the setting persists.
- [ ] Implement `UpdateScheduler`, a headless `SPUUserDriver` for scheduled checks, and allow scheduled checks in `mayPerform` only when the setting is on. Add **Install updates automatically** to Settings, defaulting on.
- [ ] Commit: `feat(macos): install updates automatically once idle`.

## Task 6: Update this Mac, from the hosted app

**Files:** `cloud/functions/control_plane.py`, `orchestrator/account.py`, `orchestrator/desktop_agent.py`, `orchestrator/desktop_protocol.py`, `orchestrator/web/static/account.js`, menu app, tests.

- [ ] Tests: only the owner can request; the heartbeat reply carries the request and later heartbeats report `updated`/`waiting_for_work`/`failed` and clear it; the agent exposes `update_requested` in `status`; the menu runs the gated install without the idle wait; when no menu has checked in for 60 s the agent starts it hidden (injected `open`); a request never bypasses the work gate.
- [ ] Implement `POST /machines/update` (person), the heartbeat field, a `menu_hello` control command, and the hosted button with its result text.
- [ ] Commit: `feat(updates): ask a Mac to update from the hosted app`.

## Task 7: Closet readiness checks

**Files:** create `orchestrator/mac_readiness.py`, `tests/test_mac_readiness.py`; modify `desktop_agent.py`, `desktop_diagnostics.py`, `account.py` heartbeat, `cloud/functions/control_plane.py`, `account.js`.

- [ ] Tests with injected command output: automatic login, sleep, `autorestart`, Xcode license and background-item approval map to typed states; unreadable settings become `unknown`, not a warning; nothing writes settings; the control plane keeps only known check names and states; the hosted list shows each warning with its fix.
- [ ] Implement hourly checks in the agent, reuse the diagnostics allowlist, and add the states to the heartbeat.
- [ ] Commit: `feat(desktop): report whether a Mac is ready to run unattended`.

## Task 8: Add a Mac in the hosted app, and the guide

**Files:** `orchestrator/web/static/account.js`, `docs/macos-desktop.md`, new `docs/mac-mini.md`, `tests/test_macos_release.py`.

- [ ] Tests: **Add a Mac** and the script are absent until the release record exists (extend the existing no-download guard); the shown command expires visibly and can't be shown twice.
- [ ] Implement the dialog (copy button, 15-minute countdown, both project forms) and write the Mac mini guide: automatic login (FileVault off), `pmset -a sleep 0 disksleep 0 autorestart 1`, one-time Screen Sharing steps, macOS updates.
- [ ] Commit: `feat(web-ui): add a headless Mac from the hosted app`.

## Completion

Canonical validation plus Swift tests after each task. Deploying the function and hosting, and publishing the script, happen only after signed releases exist. Final acceptance is the Mac mini checklist in the spec, run on real hardware; anything not run there is recorded as unverified, not assumed.
