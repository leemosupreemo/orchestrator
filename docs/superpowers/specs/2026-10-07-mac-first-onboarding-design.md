# Mac-first onboarding for nontechnical users

## Outcome and scope

A person with a fresh Mac can install Orchestrator, connect it to their website account, create or select a project, and reach a ready workspace without using Terminal to install Orchestrator or establish remote access. The user selected Mac first. Windows and Linux installation are outside this release.

Extend the existing menu-bar app, bundled runtime, pairing protocol, browser workspace, and release tooling described in `2026-10-03-macos-menu-bar-design.md`. Do not introduce another installer or account system. Project build tools and AI providers may require separate installation or authentication; explain and guide these explicitly rather than promising that all dependencies are bundled.

## Implementation order

1. Replace the prerequisite-heavy website installation path with guided Mac installation, backed by real release availability.
2. Make connection and remote access understandable and recoverable through the app and browser.
3. Validate and publish the signed Mac installer and update artifacts; enable downloads only after release acceptance.
4. Guide project and AI setup with actionable prerequisite checks.
5. Align website, installer, and supported runner versions around an explicit release commit.

Work on the first installation screen can precede release publication, but completion of a nontechnical installation path depends on step 3. Until then, describe downloads as unavailable and keep command-line installation in a clearly labeled advanced section.

## Installation experience

After sign-in, preserve the existing idea capture and existing-project choice. The next screen explains that Orchestrator runs on the user's Mac and that the website lets them access it remotely.

Show three steps: download the app, move it to Applications and open it, then choose Connect this Mac. Explain that Python, pipx, Homebrew, and tunnel software are not required for the app installation.

Offer Apple Silicon and Intel downloads only when a published release record provides validated artifacts. Include plain instructions for finding the chip under Apple menu > About This Mac; browser detection is only a suggestion. State the supported macOS version, currently 13 or later. On a phone, explain that installation must be completed on the Mac and that the phone can be used afterward.

Missing, malformed, unsupported, or unavailable release metadata must result in an honest unavailable state, not a fabricated download URL. Keep an existing-computer pairing-code option and an advanced command-line path accessible.

## Connecting the Mac

The native welcome window makes Connect this Mac the clear next action for an unpaired computer. It opens the existing one-time pairing URL in the browser. The browser names the computer and asks the signed-in user to confirm ownership; signing in must preserve the pairing intent.

Distinguish account connection, local workspace readiness, and remote reachability. Show Connecting, Connected, and actionable error states. Expired codes offer a fresh connection attempt. A Mac already connected to another account requires an explicit account-change flow; never silently reassign it.

Use the bundled cloudflared and existing remote-access preference. The normal path must not ask users to type a tunnel command or paste a backend URL. Explain sleep and lost-network states in plain language and offer retry actions. Local setup remains available when remote connectivity fails.

## Installer and release acceptance

Use the existing build and release scripts to produce Apple Silicon and Intel DMGs from one fixed commit. Sign and notarize them, verify their signatures, and validate the bundled Python runtime and tunnel binary. Configure the signed update feed and matching public key before claiming automatic updates are available.

Publish a versioned release record containing version, build, source commit, supported macOS version, and each architecture's HTTPS artifact URL and SHA-256 digest. Adapt existing consumers together to preserve compatibility with their required fields. Publish the installer, update artifacts, and record before enabling website downloads. Preserve the last known working release if publication fails.

Before public availability, exercise installation, first launch, pairing, projectless startup, permissions, remote access, restart, and update on clean supported Macs. Both architectures need runtime evidence; cross-compilation alone is insufficient. Missing signing inputs or clean-machine access must be reported as release blockers.

## Project and AI setup

After pairing, resume the saved idea or existing-code choice. Use a native folder picker for existing code and a guided project form for new work. Infer project settings where possible and explain remaining choices in user-facing terms.

Show only prerequisites needed for the chosen project and provider. Each missing item receives a plain explanation, a supported installation or sign-in action, and a Check again action. Installing tools or changing system settings requires a visible user action. Keep optional integrations separate from requirements to start the first job.

Never show Ready solely because account pairing succeeded. Readiness requires a usable project configuration, a working chosen AI provider, and required project tools. Failed checks retain user input and give a concrete next action without exposing credentials.

## Version consistency

Build the desktop app and hosted static assets from the same selected release commit. Record that commit and version in release metadata. Publish app artifacts before the website offers them. Do not make nontechnical users install from a development branch. The advanced CLI path should use an intentional release tag when available.

Retain the existing runner API compatibility check. A website that needs a newer runner explains the required update and offers the supported app update path. Version equality does not replace API compatibility checks.

## Validation and completion

Follow `docs/build-test-commands.md` for changed Python and Swift components. Add behavior-focused coverage for release availability, invalid metadata, preserved pairing intent, prerequisite failures, and version compatibility where those behaviors change. Inspect the onboarding on desktop and mobile widths, including signed-out, signed-in with no computer, pairing, and offline states.

Complete a real clean-Mac walkthrough from a new website account to the first successful job. Record which provider and project stack were exercised, and any steps that still require manual installation. Never claim clean-machine completion from source inspection or developer-machine builds.

## Current evidence and unresolved release prerequisites

The repository contains the native app, private Python runtime packaging, bundled cloudflared, pairing, setup screens, and development/signed build workflow. The hosted website currently uses pipx installation instructions and disables the Mac release feature.

At design time, GitHub listed no releases. Requests for the macos-release environment's secret and variable names returned HTTP 404, so environment access and signing configuration remain unconfirmed. Resolve these prerequisites during release preparation without recording secret values in the repository.
