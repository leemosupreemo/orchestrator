# Mac-first onboarding implementation plan

**Goal:** Guide a new Mac user from website sign-in to a working local workspace.
**Architecture:** Extend Account rendering and the native welcome window; load validated release metadata asynchronously. Preserve existing pairing and readiness services.
**Spec:** `docs/superpowers/specs/2026-10-07-mac-first-onboarding-design.md`
**Execution:** Inline; user requested autonomous execution without further checkpoints.

## Constraints and review focus

Downloads require a real signed release for both architectures. Reject invalid or unsafe metadata and retain pairing when metadata is unavailable. Do not replace the active user's agent or tunnel during validation. Preserve idea input, avoid stale async renders, and keep advanced CLI setup available. Public release acceptance requires actual clean-machine evidence.

## Tasks

- [x] Website installation: add strict `Account.macRelease(value)` validation and `renderMacSetup(release)` rendering in account.js; use the guide in renderMachines. Fetch `/releases/macos.json` when showing the account screen; malformed/absent metadata yields unavailable copy. Test invalid URLs, missing architectures, absent releases, escaping and pairing availability in AccountUiTests.
- [x] Connection: use app-oriented instructions for desktop Macs; keep terminal instructions for CLI users. Improve post-pairing and connection-error copy in app.js and make the native welcome window offer a clear numbered path. Validate account tests and Swift build/tests.
- [ ] Release preparation: include required static image/JSON assets in packaged source. Add a local manifest preparation command consuming signed release receipts, matching versions/builds, fixed source commit, and clean-machine acceptance evidence. Test refusal of development receipts and mismatched artifacts. Inspect signing availability, build an isolated development app, and record blockers to public release.
- [x] Guided setup: link project setup completion to existing tools/models checks and make their purpose clear before starting work; preserve the native folder picker and saved idea. Validate changed setup behavior and existing readiness tests.
- [ ] Release consistency: document an explicit same-commit build/publication sequence and tag-based advanced installs when release metadata is available. Run canonical Python validation, focused web/packaging tests, Swift checks, and diff checks. Record remaining external blockers without enabling public download links.

## Execution evidence

Initial inspection: no published GitHub releases. GitHub macos-release environment metadata returned 404. Local signing identities include Apple Development and Apple Distribution only; neither is Developer ID Application, so public notarized distribution cannot be completed with these identities.


### Implementation results

- Website setup now prioritizes the Mac app; pairing and advanced CLI setup remain accessible when downloads are unavailable. Release metadata loads without replacing typed connection codes or focused inputs.
- Packaged-Mac status and connection failures use app-oriented recovery instructions. The native welcome screen explains account connection and project setup.
- Project setup points to the readiness checklist; Check again bypasses cached AI/tool login results. Existing provider configuration remains responsible for provider installation/sign-in; no new automatic third-party installer was introduced.
- Packaging includes logo images, JSON metadata, and web manifests. Source identity is captured before the build and checked afterward; public signing refuses dirty or unidentified builds.
- prepare_manifest.py validates both architecture receipts, matching commit/version/build, artifact digests, and human clean-machine acceptance evidence. The release runbook documents same-commit publication and CLI release tags.
- Full Python suite: 1,465 tests passed, 6 skipped. Swift suite: 42 tests passed. Final focused Python regression suite: 50 tests passed. JS syntax checks and git diff checks passed.
- Headless Chrome: guide renders without horizontal overflow at 390 and 1280 px; delayed metadata after a machine-list render preserves the typed code and focus while adding both download links.
- An isolated Apple Silicon development app built and its bundled agent smoke test passed. This used the developer Mac, not a clean consumer Mac.

### Remaining external release work

Public installation is not complete. This machine has no Developer ID Application signing identity; GitHub did not expose the macos-release environment configuration; no public release exists. A signed update feed and private update-signing configuration remain unconfirmed. Clean-machine acceptance on Apple Silicon and Intel (macOS 13 and current macOS), and a fresh-account first-job walkthrough, remain required. Download metadata has deliberately not been published. These changes have not been deployed to Firebase Hosting.
