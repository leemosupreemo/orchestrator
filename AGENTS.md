# AGENTS.md

Repository guidance for coding agents working on swift-orchestrator.

- Use `docs/build-test-commands.md` for canonical validation commands.
- Follow `docs/web-ui-conventions.md` for any Web UI (`orchestrator/web/static/`) changes.
- Prefer minimal, reviewable diffs.
- Do not modify unrelated files.
- Do not commit secrets, generated runtime logs, or unrelated environment changes.
- Run `orchestrator check-config` after changing `.orchestrator/project.json`.
- Project content shared between computers travels through the project's repository (`refs/orchestrator/state`, `docs/state-sync.md`), not the control plane: don't add project content to Firestore.
- Republish the hosted web app with `python3 scripts/republish.py` (it checks committed static files and new test failures), never a bare `firebase deploy --only hosting`.
- Raise `breaking` in `orchestrator/release.json` (and add a `breaking_changes` line saying why) when a change needs every computer to update before they work together again: a new sync or job-file format, a data migration, or a raised `account.API_VERSION`. Everything else is a minor update, which computers install by themselves.
