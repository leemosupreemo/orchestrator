# macOS app

`Orchestrator.app` is a small menu-bar app for people who shouldn't need Terminal. It carries its own Python runtime and `cloudflared`, starts a background agent that serves the usual browser workspace, and shows the computer's status in the menu bar. The design is in `docs/superpowers/specs/2026-10-03-macos-menu-bar-design.md`.

## What a client does

1. Download the DMG for their Mac (Apple Silicon or Intel), drag Orchestrator to Applications and open it.
2. **Connect this Mac** opens the hosted pairing page with the code filled in.
3. Choose a project folder, or create one in the browser.
4. Choose whether Orchestrator starts at login. macOS may ask them to approve it in System Settings › Login Items.

Git, the GitHub CLI, Xcode and the AI tools' CLIs are still installed separately. The setup screens say which are missing.

## Menu versus agent

- **Quit Orchestrator Menu** closes only the menu. The agent keeps running, along with any work in progress.
- **Stop Orchestrator on this Mac** (Settings) stops the agent. It's unavailable while work is running.
- **Remote access** turns the tunnel and remote sign-ins on or off. Local use and running work are unaffected.
- **Remove background components** (Settings) unregisters the agent once it's idle. Projects, settings, history and the account connection stay in `~/.orchestrator`. Then move the app to the Trash.

## Updates

Sparkle verifies each update's signature. The app is replaced only after the agent confirms that no work is running; if work is running, the update waits.

- **Install updates automatically** (Settings, on by default) checks every six hours, downloads in the background and installs after ten minutes with no work running. The app relaunches by itself.
- **Check for Updates** in the menu checks right away.
- **Update** next to a Mac in the hosted app asks that Mac to update as soon as no work is running, even with automatic updates off. Only Macs running the app show it.

If an update is ready to install when the menu quits, quitting is refused until the work finishes. Logout and shutdown are never blocked. An update keeps the menu's login item, so Orchestrator comes back at the next login even if it installed while quitting.

## Macs nobody sits at

A Mac mini in a closet is set up over SSH with a one-time command from **Add a Mac nobody sits at** in the hosted app. See `docs/mac-mini.md`.

## Moving from `orchestrator service install`

The app shares `~/.orchestrator` with the command-line install, so the pairing and projects carry over. Settings shows an older background service when it finds one:

- **Running:** remote access in the app is paused, so the two don't keep overwriting this computer's address on the account. When its work finishes, run `orchestrator service uninstall` and choose **Check Again**. The app never stops it, because older versions can't say whether work is in progress.
- **Installed but not running:** **Move to the app** removes it after confirmation. A copy of its definition is kept in `~/.orchestrator/backups/`. To restore it, copy that file back to `~/Library/LaunchAgents/com.orchestrator.ui.plist` and run `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.orchestrator.ui.plist`.
- Only that exact service is ever touched. A file with the same name that doesn't run Orchestrator is reported and left alone.

`orchestrator ui` and the app's agent can't run at the same time for one Mac user, so whichever starts second says so and exits.

## Building

```bash
python3 packaging/macos/build.py --arch arm64 --output dist/macos --build-number 1 --development
python3 packaging/macos/smoke.py --bundle dist/macos/arm64/Orchestrator.app --state-dir "$(mktemp -d)" --control-dir "$(mktemp -d)"
python3 packaging/macos/release.py --bundle dist/macos/arm64/Orchestrator.app --output dist/release --development
```

Use `--arch x86_64` for the Intel build. An Intel build assembled on Apple Silicon has been checked only as a build. It still has to be run on an Intel Mac. Development images are ad-hoc signed and are for testing only, never for clients.

The smoke test uses its own state and control folders, picks another port if 8765 is taken, and never registers a login item.

## Releasing

Run the **macOS app** workflow (`.github/workflows/macos-release.yml`) by hand with a ref, version, build number and mode. It builds, tests and uploads the DMGs as workflow artifacts. It doesn't publish anything.

Signed mode runs in the protected `macos-release` environment and needs:

| Name | Kind | What |
| --- | --- | --- |
| `MACOS_DEVELOPER_ID_P12`, `MACOS_DEVELOPER_ID_PASSWORD` | secret | Developer ID Application certificate (base64 .p12) and its password |
| `MACOS_SIGNING_IDENTITY` | secret | e.g. `Developer ID Application: Name (TEAMID)` |
| `MACOS_NOTARY_APPLE_ID`, `MACOS_NOTARY_TEAM_ID`, `MACOS_NOTARY_PASSWORD` | secret | Apple ID, team and app-specific password for notarytool |
| `MACOS_UPDATE_FEED_URL`, `MACOS_UPDATE_PUBLIC_ED_KEY` | variable | Sparkle appcast URL (https) and EdDSA public key |

`release.py` refuses to start without each of these, and refuses Apple Development and Apple Distribution certificates, because only Developer ID can be notarized for direct download.

Publishing comes after that and is deliberate. For **Add a Mac**, also copy `packaging/macos/install-mac.sh` and a release record to the hosted folder (`orchestrator/web/static/install-mac.sh` and `releases/macos.json`) and set `MAC_APP_RELEASED = true` in `account.js`; a test requires all three together. The record is:

```json
{"version": "0.2.0",
 "arm64": {"url": "https://…/Orchestrator-0.2.0-arm64.dmg", "sha256": "…"},
 "x86_64": {"url": "https://…/Orchestrator-0.2.0-x86_64.dmg", "sha256": "…"}}
```

The digests come from the `.json` file `release.py` writes beside each image. Then publish a GitHub release with both images, the Sparkle appcast (signed with the private EdDSA key, which never goes into the repository), and **Download for Mac** links on the hosted page. Keep the hosted links off until both architectures have passed clean-machine checks on macOS 13 and on a current macOS: install, pairing, setup, tunnel, port conflict, menu quit, update while busy, and migration.

## Preparing website downloads

The signed-in website now shows a Mac installation guide. Download buttons appear only when
`orchestrator/web/static/releases/macos.json` contains a complete valid release record. A missing
record leaves account pairing available and explains that the download is not yet available.
The old `MAC_APP_RELEASED` switch still controls unattended SSH enrollment separately.

Use one clean source commit for both app builds and website assets. The builder records the
commit and whether tracked source files were modified. Public signing refuses dirty or
unidentified source builds. Build outputs must be outside tracked source files. Tag the
selected commit `v<version>` before offering the release; advanced CLI installation uses that tag.

After clean-machine acceptance, create an acceptance JSON outside the repository:

```json
{"commit": "FULL_40_CHARACTER_SOURCE_COMMIT",
 "arm64": {"passed": true, "notes": "Record the Macs/OS versions and actual acceptance results here"},
 "x86_64": {"passed": true, "notes": "Record the Macs/OS versions and actual acceptance results here"}}
```

The notes must document the required checklist on macOS 13 and a current macOS for each architecture.
Only write passing results after actually running those checks. This file is a human acceptance
record; the metadata tool validates its presence and matching commit, not the truth of the observations.

Put both signed DMGs and their release receipts in one artifacts directory, then run:

```bash
python3 packaging/macos/prepare_manifest.py \
  --artifacts /path/to/signed-artifacts \
  --base-url https://github.com/leemosupreemo/orchestrator/releases/download/v0.1.0 \
  --commit FULL_40_CHARACTER_SOURCE_COMMIT \
  --acceptance /path/to/acceptance.json \
  --output /path/to/staged-hosting/releases/macos.json
```

The command refuses development receipts, mismatched versions/builds/commits, missing
architectures, digest mismatches, and missing acceptance evidence. It never overwrites a manifest.

Publication order:

1. Build, sign, notarize, and validate app images from the selected commit.
2. Finish clean-machine acceptance and generate the matching release manifest.
3. Publish the versioned GitHub release, both images, and the signed Sparkle update feed.
4. Stage static website files from that same commit with the generated manifest. Verify both
   artifact URLs are reachable before deploying Firebase Hosting from that staging directory.
5. Walk through installation and pairing from a fresh browser account. Keep the previous
   release artifacts available for rollback.

Until signed artifacts and acceptance evidence exist, do not place a fabricated release record
in static hosting or expose development DMGs as client downloads.
