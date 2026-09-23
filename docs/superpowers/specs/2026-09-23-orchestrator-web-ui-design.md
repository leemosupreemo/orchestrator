# Orchestrator Web UI Design

## Purpose and success criteria

Build a custom browser interface for Swift Orchestrator that follows the current CLI's menus and runs real jobs. It should make the choices, job state, and next actions easier to understand without changing the meaning of an operation. Publish the interface to a dedicated Firebase Hosting preview URL so it can be tested from a browser. The first worker runs on the current Mac, which must remain online while jobs execute.

The release is successful when an authorized user can sign in, select a configured project, create a job using every CLI job type and option, run it through the applicable state transitions, inspect live progress and outputs, and reach every current top-level, configuration, and job-detail menu action through the web interface. A job started in either interface must remain visible and usable in the other. The Firebase URL must load over HTTPS and show a clear offline state when the Mac worker is unavailable.

## Selected architecture

The browser app is a TypeScript web client served by Firebase Hosting. Firebase Authentication identifies the operator. Cloud Firestore carries typed commands, sanitized job projections, progress events, worker heartbeat, and command results. Cloud Storage carries uploaded reference files and redacted output artifacts. A Python worker running on the Mac receives authorized commands and invokes shared Orchestrator operations against the local project. Local `.orchestrator/jobs/*.json` files remain the canonical job records. The worker publishes updated projections after each change, including changes made through the CLI.

The Firebase project is dedicated to Orchestrator testing; the existing Firebase projects for Thirteen, Themis, and Diff Hunter are not reused. Hosting uses a preview channel first. Its URL can be shared, but authorization is enforced by Firebase Authentication and Firestore rules because a preview URL itself is public. The Firebase project ID, web app configuration, and preview URL are recorded in setup documentation. Local credentials and service account material are excluded from Git.

The worker uses an outbound connection to Firebase, so the Mac needs no public inbound port. Only the worker invokes Git, Xcode, AI provider CLIs, Firebase App Distribution, and local scripts. Commands execute serially per project to avoid simultaneous operations on one worktree. Read-only requests may run concurrently when they cannot change project state.

## UI and menu parity

The home screen shows the active project, worker availability, model and machine readiness, and the CLI job history. The main navigation maps the CLI's New Job, Run Unit Tests, Configuration & Tools, and Refresh & Sync actions to clearly named pages or buttons. Each operation retains the same prerequisites and result semantics as the CLI.

| CLI area | Browser presentation |
| --- | --- |
| New Job | Guided form for feature with design, feature iteration, bug fix, design prototype, and coverage audit. It includes quick mode, external spec, automation mode, branch mode, model preset or overrides, and machine filtering where the CLI offers them. A review screen shows the exact chosen settings before submission. |
| Job history | Searchable job list with type, title, status, progress, project, time, and linked logs. Archived jobs are separate. |
| Job detail | Plan, brief, tasks, current phase, debug attempts, changed files, linked context, logs, AI sessions, branch and PR details, and status-specific actions. |
| Configuration & Tools | Models and live discovery, API key status and setup guidance, machine fleet, prerequisite audit, base branch, target project, archived jobs, instruction files, email settings, documentation, Firebase App Distribution, Orchestrator self-tests, and update controls. |
| Run Unit Tests and Refresh & Sync | Explicit actions with progress, final result, and captured output. |

Job detail must derive action availability from the same state rules as the CLI, including plan approval and revision, scheduling, execution, debugging, resume, merge, device delivery, iteration, re-opening a bug, full re-run, linked logs and reference artifacts, Ask AI, model overrides, context export, GitHub navigation, issue close, and discard. The UI uses descriptive labels instead of single-key shortcuts. It explains why a status-dependent action is unavailable.

The CLI remains usable. Business rules and state transitions are extracted into shared Python operations rather than reimplemented independently in JavaScript. Terminal prompts become typed inputs in the web app; the worker validates them again before invoking the operation. Features that launch provider sign-in in a local browser remain guided local setup steps, with status visible in the web UI. API keys are entered and stored locally on the Mac, never submitted to Firestore or copied into browser-readable job data.

## Data and command flow

1. The operator signs in. Firestore rules permit only the explicitly provisioned operator account to read projections and create typed commands. The initial account is provisioned during Firebase setup; an arbitrary signed-in account gets no access.
2. The UI reads the project list, worker heartbeat, job projections, and recent command results. It does not read local files or raw Firebase service credentials.
3. For a mutation, the UI submits a command with a unique ID, project ID, operation name, validated input, and expected job revision when applicable. Browser clients cannot set execution results or job state. The command schema is an allowlist, not a shell command string.
4. The worker claims the command transactionally, checks operator authorization and the current local state, and invokes the corresponding shared operation. Duplicate command IDs do not repeat side effects. A conflicting or stale revision is rejected with an actionable message.
5. The worker records phase events and a bounded, redacted output stream, then writes the command result and refreshed job projection. The UI subscribes to these documents and renders progress without requiring a page reload.
6. Periodic reconciliation republishes jobs changed through the terminal CLI. Local files remain authoritative if a projection is missing or out of date.

The command record has explicit queued, running, succeeded, failed, and cancelled states. Cancellation applies to commands that have not started; running operations retain their existing CLI interruption semantics. If the Mac stops responding, the UI shows the last heartbeat and leaves queued commands visible. On restart, the worker reconciles claims with local job state before retrying; operations with uncertain external side effects require operator review rather than blind replay. Full redacted logs and uploaded reference files use Cloud Storage with owner-only access and size limits; Firestore receives only bounded excerpts, status, and artifact references. Sensitive values are redacted before publication.

## Access and safety

Firebase Authentication uses a verified operator identity. The first release has one provisioned operator account. Firestore rules restrict reads and command creation to that identity and deny browser writes to jobs, events, results, or worker state. Storage rules restrict upload and download to the same identity, path scope, and size/type limits. The worker authenticates with its own local service identity and receives only the permissions needed for the Orchestrator Firebase project. Rules and the worker both validate command shape and allowed operations. The browser cannot send arbitrary process arguments, paths outside a selected project, or shell text for execution.

The UI shows the concrete effect and target project for merge, discard, full re-run, issue close, updates, fleet purge, and delivery before executing them. It requires an explicit confirmation and records the command ID and outcome. Existing CLI safeguards remain in the shared operation. A preview deployment must not expose job data, logs, API keys, or executable commands to an unsigned user.

## Delivery and verification

Implement in end-to-end slices: first authentication and worker connectivity; then shared job read/create/execute operations; then every remaining job action; then configuration and tools; then visual polish and deployment. Each slice remains usable from both the CLI and web app. A menu parity checklist tied to the current CLI is maintained with the implementation so added CLI actions are not silently omitted.

Verification covers shared-operation tests, command authorization and idempotency tests, status-to-action parity tests, Firestore and Storage rules tests, web build, and browser checks of the main flows. Run the repository's canonical validation commands where applicable and the Python test suite for the extracted operations. Exercise at least one harmless real operation on a configured project through the deployed preview URL, confirm the Mac worker processes it, and confirm the CLI sees the resulting state. Check signed-out access, worker-offline behavior, a stale action, a failed command, and sensitive-output redaction. Record the deployed preview URL and worker start instructions in the project documentation.

The Firebase setup and preview deployment follow the official [Hosting preview documentation](https://firebase.google.com/docs/hosting/test-preview-deploy). Firestore authorization follows the official [Security Rules documentation](https://firebase.google.com/docs/firestore/security/get-started).
