# Headless Mac enrollment and unattended updates

## Purpose and decisions

Some people run Orchestrator on a Mac nobody sits at, typically a Mac mini in a closet reached over SSH. The DMG flow in `2026-10-03-macos-menu-bar-design.md` assumes someone at the screen: they drag the app, read a pairing code, pick a folder and approve a login item. This design adds a second way into the **same** signed `Orchestrator.app`. It doesn't add a separate product, build or agent.

Decisions:

- One app, two entry points. The DMG remains the default. Headless enrollment installs the same app, uses the same agent, login item, state (`~/.orchestrator`) and updates, so a Mac set up either way behaves the same afterwards.
- The agent runs as the logged-in user, with automatic login on the Mac. iOS builds, simulators and keychain signing need a user session, so a pre-login system daemon wouldn't be able to do the work.
- Enrollment uses a one-time token from the hosted app instead of a pairing code, because nobody can read a code off the closet Mac's screen.
- The first project is chosen on the command line (`--project` path or git URL). Remote setup of a first project stays out of scope: remote access starts only once a project exists, and projectless setup is limited to the local owner credential. Settings can be refined from the browser afterwards.
- Updates install automatically once the agent is idle, and the owner can also ask for one from the hosted app. Without this, a closet Mac would never update, because updating currently needs someone to click **Check for Updates**.
- Automatic updates are a setting, on by default for new installs from either path. This is a default the owner can change in Settings, not a lock.

## Client experience

1. In the hosted app, choose **Add a Mac** on the computers page. It shows a command valid for 15 minutes and for one Mac:

   ```text
   curl -fsSL https://<hosted>/install-mac.sh | sh -s -- --token ENROLL-XXXX --project git@github.com:me/app.git
   ```

2. Paste it into an SSH session on the Mac mini. The script picks the Apple Silicon or Intel build, downloads the signed DMG, verifies its digest and Apple signature, installs `Orchestrator.app` in Applications and runs the app's `enroll` command.
3. `enroll` redeems the token, sets up the project, then starts the app in the background to register the login items and the agent. It prints what it did and any closet-readiness warnings.
4. The Mac appears in the hosted app's computer list as online. From then on it is used exactly like any other computer.

Running the command again is safe: an already-enrolled Mac keeps its identity, and an installed app of the same or newer version isn't replaced.

## Enrollment tokens (control plane)

New person endpoint `POST /enroll/create`: signed-in owner only. It creates `enrollments/<sha256(token)>` with `owner_uid`, `owner_email`, `created` and `expires` (15 minutes), and returns the token once. The token has at least 128 bits of entropy and is prefixed `enroll_` so it is recognisable in logs and leaks.

New computer endpoint `POST /enroll/redeem` with `{token, name, os, version}`: looks the enrollment up by hash, refuses it if expired or used, applies the existing per-account computer limit, then creates the machine and its secret exactly as `claim_pairing` does. It deletes the enrollment in the same step, so the token works once, and returns `{machine_id, machine_secret, owner_email}`. Only the hash is stored, as with pairing poll secrets.

The owner gets a push notification and the computer list marks the Mac "Added with a command" for its first day. If someone else uses a leaked token in those 15 minutes, the owner sees it immediately. The token appears in the closet Mac's shell history; it is single-use and expires, so that is acceptable, and `enroll` also accepts it on standard input for people who prefer that.

## The install script

`install-mac.sh` is served from hosting and is short enough to read before running. It:

- refuses to run as root or without a logged-in console session for the current user (`stat -f %Su /dev/console`), and explains automatic login if missing;
- chooses `arm64` or `x86_64` from `uname -m`, reads the published release record (`version`, `sha256`, file name), downloads the DMG over HTTPS and checks the digest;
- mounts it read-only, checks `codesign --verify --strict` and `spctl --assess` on the app, and copies it with `ditto` into `/Applications` (or `~/Applications` if `/Applications` isn't writable);
- runs `Orchestrator.app/Contents/Resources/runtime/bin/orchestrator enroll` with the remaining arguments and returns its exit code.

Until signed releases exist, the hosted page doesn't show **Add a Mac** and hosting doesn't serve the script, consistent with the DMG links.

## The `enroll` command

Bundled in the app's runtime, so it needs no Python on the Mac:

1. Checks preconditions: a console session for this user, a supported macOS version, the app running from Applications and not a mounted image.
2. Redeems the token (or reuses the existing pairing if this Mac is already enrolled to the same account; refuses a token for a different account rather than silently switching).
3. Sets up the project: a local path is used as is; a git URL is cloned into `~/Projects/<name>` using the person's existing git credentials (SSH keys or `gh`). If the project has no Orchestrator config, the inferred settings from `project_setup.inspect_project` are applied and validated; if inference isn't enough, it stops with the missing items listed and leaves the clone in place.
4. Hands off to the app: `open -g -j -a <app> --args --register-background`. The menu app registers the menu and agent login items without showing windows, starts the agent and exits its handoff mode. `enroll` waits for the agent's control socket to report `setup: ready`.
5. Runs the closet checks (below) and prints the results, then exits 0. Secrets are never printed.

If macOS reports the background item as needing approval, `enroll` says so and points to the one-time Screen Sharing step. Whether a Developer ID app registered over SSH needs that click is verified on a real Mac before the guide promises either way.

## Unattended updates

The updater stays in the menu app (Sparkle needs an app process to replace and relaunch). Two triggers, one gate:

- **Automatic.** When **Install updates automatically** is on, the menu app checks the feed every 6 hours and downloads in the background. A custom Sparkle user driver with no UI handles the result. Installation waits until the agent has had no runs or tasks for 10 minutes, then calls the existing `UpdateCoordinator.prepareInstallation()`. If work starts in the meantime, the gate refuses and it tries again later. The menu app relaunches after the update, and `recoverAfterRelaunch()` restores the agent and its login mode.
- **On request.** **Update this Mac** in the hosted app (owner only) sets `update_requested` on the machine. The next heartbeat reply carries it. The agent shows it in `status`, and the menu app starts the same gated install, ignoring the 10-minute idle wait but never the work gate. The result (`updated`, `waiting_for_work`, `failed`) goes back in later heartbeats and the flag is cleared.

If the menu app isn't running when an update is due, for example because someone chose **Quit Orchestrator Menu**, the agent starts it hidden with `open -g -j`. The agent knows its own bundle path, and the menu checks in over the control socket, so the agent can tell whether one is running.

The safeguards from the menu-bar work all stay: the bundle is never replaced while work is running, a stuck or unknown agent never counts as idle, and logout and shutdown are never blocked. Automatic checks are permitted in `SPUUpdaterDelegate.mayPerform` only when the setting is on.

## Closet readiness checks

The agent checks a few Mac settings at startup and every hour, and reports them as typed states in `status`, diagnostics and the heartbeat:

| Check | Warning when |
| --- | --- |
| Automatic login | not set to this user |
| Sleep | system sleep isn't disabled (`pmset`) |
| Power failure | `autorestart` is off |
| Xcode | installed but the license isn't accepted |
| Background item | requires approval |

The hosted computer list shows these next to the Mac with the fix for each. They are warnings, not errors: a Mac on a desk doesn't need them. None of them changes a setting; changing power or login settings needs administrator rights and is the owner's decision.

## Security notes

- The enrollment token is a bearer credential for 15 minutes. It only creates a computer under the owner's account; it can't open or control existing ones. The push notification and the "Added with a command" mark make misuse visible.
- The install script verifies the digest from the release record and Apple's signature before running anything from the download.
- `update_requested` only asks the Mac to look for an update from its own configured feed, which Sparkle verifies with the embedded public key. The control plane can't supply what gets installed.
- Nothing here weakens the owner/member roles: creating enrollments and requesting updates are owner actions.

## Delivery and verification

Release prerequisites are the same as for the DMG: Developer ID signing, notarization, the Sparkle feed and keys. Unattended updates can't be shipped before the feed exists. Verify on a Mac mini with automatic login: enrollment over SSH with both path and git URL projects, a re-run, an expired and a reused token, login-item registration from SSH, an automatic update while idle, an update requested during a running job (it waits), a requested update while the menu app was quit, power loss and restart, and the readiness warnings appearing and clearing.
