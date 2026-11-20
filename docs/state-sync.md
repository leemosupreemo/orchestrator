# Sharing project state between computers

Goal: every computer working on a project sees the same jobs, product requirements and key project state, **without
Orchestrator's servers storing project content**. Project content travels where the code already does, the project's own
repository. The control plane (Firestore, `docs/control-plane.md`) keeps only what it takes to find and reach computers.

Status: **phase 1 is built** (the product requirements document, its design and source files). Phases 2 to 4 are planned.

## What lives where

| State | Where it belongs | Today |
|---|---|---|
| Product requirements, their designs and source documents (`.orchestrator/product/`) | The repository's state ref | **Phase 1, built** |
| Jobs (`.orchestrator/jobs/`): records, plans, answers, fix history, archive and ownership events | The repository's state ref | Firestore (`account_jobs`, `job_owners`, `job_archives`, `machine_snapshots`); phase 2-3 moves them |
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

## Asked once, per project

Anyone who can read the repository can read the state ref; on a public repository that is everyone. So nothing is shared until
the person says yes for that project. The Product page asks once ("Share with your other computers?"), says where it would go and
who could read it, and on a repository GitHub reports as public (checked with `gh`) warns and makes **Keep on this computer** the
main button. The answer is kept per project in `.orchestrator/state/repo_sync.json` and can be changed any time (**Stop sharing**,
**Share through ...**). Hosts other than GitHub can't be asked whether a repository is public, so the prompt says who can read it
and leaves the choice to the person.

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
2. **Jobs, written to both.** Each computer writes its jobs to the state ref and still to Firestore, and reads the state ref first.
   Mixed versions keep working, so no `breaking` release is needed. The hosted app gets a computer's jobs from that computer, or,
   when it is offline, from another of the account's computers that is online (each has the shared state).
3. **Jobs off Firestore.** Computers stop writing job content to the control plane. The stored content (`account_jobs`,
   `machine_snapshots` and the job titles in the others) is deleted, and the control plane gains a delete path for what an
   account still holds. When every one of an account's computers is offline, the hosted app shows the computers but no job list.
4. **Setup profiles.** The portable setup moves to the state ref; `master_configs` and `machine_setups` are retired the same way.

## What changes for speed

The control plane told other computers about a change within seconds. Fetching the state ref every minute is slower. Phase 2 adds a
content-free nudge: a computer that pushed a new state sends the commit's hash (40 characters, no content) with its heartbeat, and
the reply tells the account's other computers to fetch now.

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

`orchestrator/state_ref.py` (transport, consent, public-repository check), `orchestrator/product_sync.py` (phase 1),
`UIServer.start_state_sync` (the thread, paired or not), `GET /api/product` (`sharing`) and `POST /api/product/sharing`.
Tests: `tests/test_product_sync.py` runs two computers against a real bare remote (merges, races, hooks, clones, consent,
files), and `tests/test_wip_backup.py` checks backups ignore pre-push hooks.
