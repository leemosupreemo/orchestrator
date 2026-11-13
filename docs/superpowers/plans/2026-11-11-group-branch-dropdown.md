# Grouped branch dropdown implementation plan

**Goal:** Show “Installed locally” above “Download” in the Home and Git branch selectors.

**Architecture:** Share native optgroup markup between selectors. Keep local branches unchanged; list cached remote-only branches separately. Selecting a remote entry fetches its exact configured remote ref and creates a local tracking branch. Reuse the existing stash-and-switch flow.

**Constraints:** Reuse design tokens and native controls. Never overwrite a local branch, force a fetch, interpolate branch names into shell code, or fetch as part of polling. Keep remote identity when branch names exist on multiple remotes.

**Files:** `orchestrator/web/server.py` supplies choices and validates/downloads branches; `orchestrator/web/static/app.js` shares grouped markup; `tests/test_branch_dropdown.py` tests real Git operations and rendered options.

- [x] Add failing tests for ordering, duplicate filtering, selected/last-used labels, remote identity, fresh downloads, stashing and hostile branch names.
- [x] Implement branch choices and safe fetch/tracking checkout, preserving ordinary checkout behavior.
- [x] Use shared grouped options in Home and Git, including when only remote branches exist.
- [x] Run focused branch, Git, worktree and UI convention checks, syntax/compilation checks and diff checks.
- [x] Review the complete change and address findings.

**Validation limits:** The earlier full suite has unrelated fresh-install GitHub-stub and product-task cancellation failures; report focused results explicitly.

**Review ruling:** Resolve remote source branches through configured fetch mappings, including exclusions; never infer a source branch from the tracking-ref suffix. Existing Git-page extraction tests load the real shared option renderer.

**Evidence:** 187 focused tests pass. JavaScript syntax, Python compilation and diff checks pass. Final independent review reports no remaining actionable findings.
