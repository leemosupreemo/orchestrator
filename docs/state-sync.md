# Sharing project state between computers

Goal: every computer working on a project sees the same jobs, product requirements and key project state, **without
Orchestrator's servers storing project content**. Project content travels where the code already does, the project's own
repository. The control plane (Firestore, `docs/control-plane.md`) keeps only what it takes to find and reach computers.

Status: **phase 1 is built** (the product requirements document, its design and source files). **Phase 2 is built**, with the fast
nudge (jobs written to the repository as well as the account, read from the repository first); the nudge needs the control-plane
function deployed. Phases 3 and 4 are planned.

## What lives where

| State | Where it belongs | Today |
|---|---|---|
| Product requirements, their designs and source documents (`.orchestrator/product/`) | The repository's state ref | **Phase 1, built** |
| Jobs (`.orchestrator/jobs/`): records, plans, answers, fix history, archive and ownership events | The repository's state ref | **Phase 2, built:** written to both; phase 3 stops the Firestore copy (`account_jobs`, `job_owners`, `job_archives`, `machine_snapshots`) |
| Setup profile: the portable parts of `project.json` (build and test commands, prompts), not machine paths | The repository's state ref | Firestore (`master_configs`, `machine_setups`); phase 4 |
| PRD history, epics, features, project chat | Optional, later | Local |
| Logs, run output, coverage caches, locks, `config/` | Stay on each computer: per machine or regenerable | Local |
| Pairing, machine secrets, sign-in tickets, online status, update commands, push tokens, rate limits, which computer is primary | The control plane: identity and rendezvous, no project content | Firestore |

## How it travels: `refs/orchestrator/state`

`orchestrator/state_ref.py`. One hidden ref per repository, on `origin`. It points at a commit whose tree mirrors the shared files
(`product/prd.md`, `product/designs/...`, `product/source/...`; later `jobs/<id>.json` and the setup file).

- **Never in the way.** It is not a branch: it is never checked out, branch lists and host web pages don't show it, and an
  ordinary `git clone` or `git fetch` doesn't download it. Commits are built in a scratch index, so the working tree, the index
  and branches are never touched.
- **No overwrites.** A computer fetches the ref, merges, commits on top and pushes **without `--force`**. If another computer
  pushed in between, git rejects the push as non-fast-forward and nothing is written; the computer fetches again, merges with
  what won and retries. That is the compare-and-swap that the control plane's version numbers would have given, done by git.
- **No hooks.** Pushes use `--no-verify`: a repository's pre-push hook (a test run) is for code, not for this. The unfinished-work
  backups (`wip_backup.py`) now push the same way.
- **No personal identity.** State commits are authored by "Orchestrator <orchestrator@localhost>"; the computer's name is in a
  `Orchestrator-Computer:` trailer so a change can say where it came from.
- **Any host.** Plain `git fetch` and `git push` to `origin`: GitHub, GitLab, Bitbucket, Gitea, Azure DevOps, a self-hosted server
  or an SSH or file remote. It fails, and says so on Home under sync problems, when the computer can't push (no rights, a read-only
  clone) or the host refuses custom refs (Gerrit, some locked-down servers).
- **No remote, no sharing.** A project without `origin` stays on its computer, as its job branches already do. Two computers whose
  `origin` points at different repositories (a fork) don't share, which is correct.

## On by default, per project and per kind

A project with a remote shares its product requirements and its jobs **by default** (the person's decision, 2026-11-20): every
computer working on it, paired to the same account or not, reads and writes the same state through the repository. **A public
repository asks first** (`state_ref.SHARE_PUBLIC_BY_DEFAULT` is off): anyone can read its state ref, so its requirements, plans and
your answers would be public. Whether a repository is public is asked of GitHub with `gh`; other hosts can't say, and are
treated as private.

The choice is part of setup: **Set up an existing project** has a "Your other computers" section ("Share its requirements and
jobs with your other computers through ...", ticked for a private repository, unticked with a warning for a public one, or a
note when there is no remote), and **Start a new project** puts the same box under "Who can see the repository?", following
that choice until it is ticked by hand. After setup, each kind can be stopped or started per project on the Product page, under
**Keeping it up to date** (**Stop sharing**, **Stop sharing jobs**, **Share ... again**). An explicit answer always wins over the
default and is kept in `.orchestrator/state/repo_sync.json` (`product`, `jobs`); setup records the same answer for both.

**Teams.** Everyone with access to a repository who runs Orchestrator on it shares into the same state. A permission and admin
system for team projects (who may read and write which state) is planned; until then the repository's own access is the only
control, so on a shared team repository, decide per project whether to stop sharing.

## Merging

Each kind of state has its own rule, so a merge never silently drops someone's work:

| State | Rule |
|---|---|
| Product requirements text | Section by section against the text both last agreed on: a section only one side changed takes that side; a section both changed differently keeps both, the other computer's under "Also written on <name>:" |
| Its "Source documents" list | The union |
| Design and source files | The union: a file one computer has is copied to the others; a file is never overwritten or deleted |
| Job records (phase 2) | The copy with the higher ownership number (`owner_epoch`, raised by Continue here) wins, the fencing rule the control plane uses today |
| Archive and restore events (phase 2) | The newest event per job |

Every change that arrives is written as a version in that computer's own history and announced; see
[Product requirements](product-requirements.md#across-your-computers).

## Phases

1. **Product requirements (built).** `state_ref.py`, `product_sync.py` on it, consent on the Product page, design and source files,
   `--no-verify` for every Orchestrator push. The control-plane endpoints built for an earlier, Firestore-based version were never
   deployed and have been removed.
2. **Jobs, written to both (built).** `orchestrator/job_sync.py`, on the same thread as the product round: each computer writes its
   jobs to `jobs/<id>.json` (the entry the account receives, `account._job_entry`, plus the computer's name) and the newest archive
   event per job to `job-events.json`, while the account keeps receiving them as before, so mixed versions keep working and no
   `breaking` release is needed. A copy is replaced only by a higher ownership number, or by its own computer; a computer whose job
   was continued elsewhere marks its copy moved (`apply_moved_jobs`) and applies archive events (`apply_job_archives`), as the
   heartbeat's replies do. Changes that say nothing new (only `updated_at` or the live step) aren't pushed. Home reads other
   computers' jobs from `GET /api/shared-jobs` first and fills gaps from the account's snapshots; a shared job names its computer,
   matched to the account's list when the computer is paired. Not yet: the hosted app's **Your computers** page, which still
   lists an offline computer's jobs from the account.
3. **Jobs off Firestore.** Computers stop writing job content to the control plane. The stored content (`account_jobs`,
   `machine_snapshots` and the job titles in the others) is deleted, and the control plane gains a delete path for what an
   account still holds. When every one of an account's computers is offline, the hosted app shows the computers but no job list.
4. **Setup profiles.** The portable setup moves to the state ref; `master_configs` and `machine_setups` are retired the same way.

## What changes for speed

The control plane told other computers about a change within seconds. Fetching the state ref every minute alone would be slower,
so there is a content-free nudge (built; it works once the control-plane function is deployed, and until then computers simply
fetch every minute):

- A computer that pushed shared state reports, with its heartbeat, the commit it last pushed for each shared project
  (`state_heads`: `{project key: {sha, at}}`). The key is a hash of the repository's name (`state_ref.project_key`), the same on
  every computer with that remote. The account keeps the newest report per key (`state_heads` collection, one document per
  account: key, commit hash, time, computer; nothing of the project itself) and returns `{key: sha}` in every heartbeat reply.
- A computer whose fetched copy and last push are both different from that commit is behind (`state_ref.behind`): the heartbeat
  wakes the sync thread, which fetches and merges at once.
- After a round that pushed, the computer sends a heartbeat straight away rather than at the next minute.

So a change reaches the others within a few seconds of the pushing computer's round. A computer on an older version ignores the
reply's `state_heads`, and an older control plane ignores the report: both fall back to the minute's fetch.

## Risks and limits

- **Readable by repository readers.** That is the reason for the per-project question. Encrypting the shared files would need a key
  exchanged directly between computers at pairing (a key held by the control plane would defeat the purpose); not planned yet.
- **Growth.** Every change is a commit on the ref. Jobs change often, so phase 2 commits only meaningful changes (status, plan,
  answers), not every step. A few kilobytes per commit lasts a long time.
- **Credentials.** Pushing needs the computer's non-interactive git credentials, the same ones job branches and backups already
  use. A push that keeps failing shows on Home, with what to do.
- **Assets are never deleted or replaced.** A design changed under the same name on two computers stays different on each until
  one is renamed; deleting a file on one computer doesn't remove it elsewhere.

## Code and tests

`orchestrator/state_ref.py` (transport, consent per kind, public-repository check), `orchestrator/product_sync.py` (phase 1),
`orchestrator/job_sync.py` (phase 2), `UIServer.start_state_sync` (the thread, paired or not), `GET /api/product` (`sharing`),
`POST /api/product/sharing` (`enabled`, `scope`: product or jobs) and `GET /api/shared-jobs`.
Tests: `tests/test_product_sync.py` and `tests/test_job_sync.py` run two computers against a real bare remote (merges, races,
ownership hand-over, archives, hooks, clones, consent, files), and `tests/test_wip_backup.py` checks backups ignore pre-push hooks,
skips identical local files during restore, and enforces explicit `overwrite=True` confirmation (`WipConflictError` / 409 `conflicts`) when local changes differ.
Sync problems show on Home under the `product` and `shared_jobs` channels, naming the computer (`Sync problems on <Computer Name>`).

### Sync reliability & filtering details

- **Task scratch files excluded:** Internal per-task worker scratch files (`*_task_<N>.json`) are excluded from `job_sync.py`, `account.py` snapshots/updates, and the Home remote-jobs merger so only real jobs are shared.
- **Live state transitions pushed:** `job_sync.py` includes `state` in its change comparison so a worker exit that flips a job from **Building** to **Paused** (while raw `status` remains `executing`) pushes immediately to `refs/orchestrator/state`.
- **Full job context, brief, and run history via state ref:** `job_sync.py` shares rich job context (`raw_input`, `spec`, `risk_assessment`, full `verification` including `suggested_additions`, `clarification_*` metadata, `blocker`, `brief.md`) and compact finished-run sidecars (`runs`, merged by run ID across computers, without log bytes) strictly in `refs/orchestrator/state` (never Firestore). `import_remote_job()` restores them, `run_history()` and the Run page display synced remote runs cleanly, `pages["remote-job"]` and `findJobElsewhere()` prefer `GET /api/shared-jobs` over snapshots, multi-hop moves (`A -> B -> C`) update `moved_to`, and job/run mutations wake `start_state_sync()` immediately.
- **Stale pre-sleep failures suppressed:** `sync_health.problems()` ignores failures whose `failed_at` is older than 15 minutes (`STALE_FAILURE_SECONDS = 900`, e.g. from before a Mac slept), `sync_health.record()` starts a fresh grace window if a failure arrives after a >15-minute gap (instead of reviving the pre-sleep `failing_since`), `start_state_sync()` clears pre-sleep failures on wake (including mid-round sleep) and shares its `wake_state_sync` signal across `local`/`remote` `UIServer` instances, and Home auto-rechecks `GET /api/sync-health` after nudging a fresh round so cleared banners disappear without a reload.
