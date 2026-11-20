# Close release gaps implementation plan

Goal: make the existing hosted release usable on connected computers, broaden safely scoped native package updates, and replace authentication test assumptions with executable verification.

Design: keep the current web-ui release and native manager architecture. Update the local editable runner from the web-ui worktree only after checking it is idle. Give old runners an accurate compatibility notice and install command for the matching release channel. Fail an update when git or pip fails, and explain when a restart is needed. Package capabilities are computed from the actual selected manifest and shown before launching; expand supported managers only where a selected package and local environment can be preserved. Verify auth using an isolated emulator and runtime mobile redirect tests; do not mutate production users or send real email without a named recipient/send request. Sentry requires a supplied public registration ID.

- [x] Prove and fix false-success update behavior; align hosted runner compatibility and install channel.
- [x] Add bounded native package support and capability explanations; test manager scope and environment isolation.
- [x] Exercise reset request/code/new-password/reuse with Firebase Auth emulator; test mobile redirect return completion.
- [x] Install and restart idle local runner; verify new actions and account heartbeat. Inspect reachable machines without interrupting work.
- [x] Run focused/full validation and review; prepare the verified tree for push/publication. Verify live assets after deployment and report remaining external inputs honestly.

Verification: unit tests reproduced false success for failed git/pip stages, no-op plain-pip updates and old release instructions before fixes. The selected package capability/UI path and manifest-specific AI fallback have runtime tests. Firebase Auth emulator verified request/code/confirm/new-password sign-in and rejected reused codes. Bun 1.4.4 and Poetry 2.5.1 were provisioned temporarily and updated only selected dependencies under their constraints, ran configured checks, and left host/external environments unchanged. Plain pip was smoke-tested in a temporary .venv with hostile inherited environment targets and also stayed local. Code review found no material issues.

Local rollout: changed the editable installation from the main checkout to the web-ui worktree; checked the runner idle, stopped it gracefully, restarted with the same project/tunnel and verified API 3 plus native package actions. Cloud metadata independently confirms API 3 heartbeats. The other registered Mac remains API 2; its Tailscale, .local and saved LAN SSH routes all time out, so neither its runner update nor its previously reported stuck job can be verified here. Its cached snapshot still includes an executing job and is not live process evidence.

External inputs still required: public Sentry OAuth client ID; a named recipient and send authorization for real password-reset delivery; restored SSH access for the other Mac. Actual provider login on a physical mobile device remains separate from runtime handler tests. Modern Yarn workspaces, Gradle, Unity and complex pip requirements offer an explicit AI plan with a support reason instead of an unsupported native action.

Final staged-tree validation: 2,593 tests passed with nine optional tests skipped; 63 release-focused tests passed. The opt-in Auth emulator test passed separately, and both opt-in Bun/Poetry integration tests passed separately. Python compilation, JavaScript/shell syntax, and staged diff checks passed.
