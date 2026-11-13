# Automatic recovery when Preview needs a newer runner

## Requested behavior

When opening Preview requires a newer Orchestrator on the selected computer, start an update automatically and show a modal with progress and Cancel. Keep running jobs safe. Reconnect to that same computer and retry Preview after the update succeeds.

## Approach

Reuse the existing account control plane, desktop agent, signed Sparkle updater, and idle work gate. The preceding conversation selected automatic recovery with a progress modal. Alternatives considered: sending users to the computer list to update manually (does not meet the requested recovery), or remotely executing installation shell commands (bypasses the established signed updater and cannot reliably recover a partly updated installation).

This is a coordinated frontend, control-plane, Python agent, and native updater change. It is not a frontend-only error-message change.

## Detection

- Version the remote-preview capability and the cancellable update protocol explicitly. The package version currently stays at 0.1.0 across changes and cannot establish compatibility.
- Confirm that the selected computer lacks the required preview capability before starting recovery. A generic 404, missing branch, failed tunnel, permission denial, or offline computer must not trigger an update.
- Use the account's authenticated machine identity, never a stale tunnel URL or another available computer. Recheck identity after asynchronous requests.
- Only the account owner can request or cancel an update. Report unsupported permissions without retry loops.

## Modal and lifecycle

Show "Updating Orchestrator on <computer>" with the reason: "Preview needs a newer version. Your running jobs will finish first."

Show truthful stages from the computer: Requested, Checking for an update, Downloading, Waiting for current jobs, Installing, Reconnecting. Do not invent percentage progress. The modal remains visible while the computer restarts, and state can be recovered after a page reload.

Use one expiring update operation per machine with a stable operation ID, acknowledgement, progress, and terminal result. Repeated preview attempts join the operation instead of requesting more updates. A cancelled, failed, timed-out, or insufficient update must not automatically start again while the user remains in that attempt.

After the computer returns, renew the authenticated connection, verify the required preview capability, and retry the preview exactly once. "Up to date" from the updater alone does not establish that Preview can work. If no compatible release exists, show an actionable failure rather than repeat the update.

Bind the saved preview attempt to its original machine and project. Restore update status after navigation or reload, but retry Preview only while that same context remains selected. Expire undelivered requests and stalled operations; an operation already installing must be reconciled after reconnect, never reported as safely cancelled merely because its timer expired.

## Cancel semantics (implementation proposal)

- Cancel while requested, checking, downloading, or waiting for jobs cancels the update operation on the computer; it does not merely dismiss the modal or stop browser polling.
- Keep showing "Cancelling…" until the computer acknowledges cancellation. If it is offline, show cancellation as pending and retain the operation identity.
- Suppress further automatic recovery for that cancelled attempt. Preview remains available for an explicit later retry.
- Once the updater has entered the final replacement/relaunch step, disable Cancel and show "Finishing installation—this step cannot be cancelled safely." Continue to show progress and reconnect. Never claim cancellation succeeded if installation already won the race.
- Coordinate cancellation with the updater's actual cancellation hooks and installation gate, including late download callbacks and cancel/install races. Clearing the scheduler's stored closure alone is insufficient: Sparkle can already have an installer armed for app termination.
- Keep requested-update cancellation separate from unrelated scheduled automatic updates.

## Job safety and recovery

Continue using the existing work gate. New work and the final install decision must be serialized: an update cannot observe idle and then restart a newly running job. Preserve the existing update journal and restore background services after failure or relaunch. Unknown activity is not idle. Do not stop preview or job processes merely to make the computer appear idle.

Control-plane commands are owner-only, bounded, expiring, idempotent, and acknowledged. Ignore stale progress/cancellation from an earlier operation. Publish only safe status metadata, with no project files or credentials in progress messages.

## Bootstrap and rollout limitation

Existing CLI-managed installations do not consume remote update requests. The existing native updater also has no remote cancellation protocol. Neither can acquire this new behavior solely from a website deployment.

Advertise recovery support only after the computer has the new cancellable update implementation. For older or unsupported installations, show an honest one-time upgrade instruction instead of "Updating" with a nonfunctional Cancel button. Do not remotely replace an unmanaged CLI installation.

Roll out the control-plane protocol and a compatible signed desktop release before enabling automatic recovery in the hosted UI. Upgrade the currently affected computer once as part of initial setup. Keep existing manual native update requests backward compatible.

## Validation

- Control-plane tests: account ownership, capability gating, deduplication, expiry, cancellation before delivery, delivery/ack retries, stale operation IDs, late progress, and concurrent cancel/install claims.
- Agent/protocol tests: update and cancel delivery, progress sanitization, stale commands, launch of the menu app if absent, restart reconciliation, and preserved running jobs.
- Native updater tests: cancellation during checking/download/waiting, late download completion after cancellation, install/cancel races, idle/unknown/busy activity, and recovery after failed installation.
- Frontend behavior tests: missing capability vs ordinary preview failures; one modal/request; all progress stages; real cancel acknowledgement; pending cancellation while offline; project/machine changes; timeout; update unavailable; reconnect; capability verification; single preview retry.
- Run canonical Python validation and the macOS package tests. Check the integrated flow against a controlled outdated packaged runner before enabling the hosted behavior. Do not claim an end-to-end fix from static asset verification alone.

## Scope

Preserve the full-screen Preview page and the worktree's unfinished managed-connection changes. Avoid changes to unrelated job, connection, or authentication behavior. This design does not introduce general remote shell execution or replace the release/signing pipeline.
