# Job recovery and ownership

How a job stops, what it says when it does, how it gets going again, and how its state is kept safe and shared across your
computers. The user-facing view is in the [user guide](user-guide.md); this page is for working on Orchestrator itself.

## Where the truth lives

| What | Where | Authoritative |
|---|---|---|
| The job record: status, plan, progress, errors, blocker | `.orchestrator/jobs/<id>.json` in the project, on the computer running it | **Yes, only there** |
| The code | That computer's checkout, then the job's branch on the git host | The branch, once pushed |
| What other computers see | The account's snapshot of each computer (`account.build_snapshot`) | No: a read-only copy |

The control plane never decides anything about a job. Each job has one owner (the computer that runs it) and one writer.

## How a run stops

When the build or tests fail, or the worker raises, the job ends up in one of these states. `job_state` in
`orchestrator/web/server.py` turns each into the label, reason and single next action the job page shows.

| Cause | Recorded as | Job page | Next |
|---|---|---|---|
| Build or tests fail (the code) | `status: debugging` | **Build failing**, **Tests failing**, **Auto-fix paused**, **Fix stopped**, **Ready to merge / review** (when the latest fix passed checks), or **Needs a fix** | An initial repair may run automatically. Further repairs wait for **Run fix** (with optional guidance). Long diagnoses expand inline via **Show more**. |
| Something outside the code: a git lock, an expired sign-in, missing toolchains or project packages, a full disk, an AI usage/rate limit, Xcode or the simulator | `status: human-needed`, `blocker: {...}` (`orchestrator/blockers.py`) | The blocker's title, checked now (or consolidated **Set up environment…** on missing local dependencies) | 1-click **Set up environment…** / **Install `<Tool>`…**, **Run on `<Peer>`** (verified against peer preflight), or manual steps + **Try again**; resumes automatically once cleared |
| The AI asks a question | `human_clarification_question` | Question for you | **Answer** |
| The AI's "question" is really a blocker ("rerun with write access") | `human_clarification_question`, classified by `classify_clarification` | The AI couldn't continue (`orchestrator/blocker_fix.py`) | The one fix that applies, e.g. **Plan again** telling the planner it plans only |
| The work passed its tests but the commit failed | `passed_uncommitted: {files}` | Tests passed, not committed | **Commit the work**: Resume commits those files without building again, and without checking anything out (a forced checkout would throw the uncommitted work away) |
| Another job is using the project folder | `waiting_for` (`folder_lock`) | Waiting | Starts by itself |
| Continued on another computer | `moved_to` | Last worked on <computer> | Opening it automatically brings it to the local machine when the other computer is online and idle; offline copies warn before continuing here |

### How a fix runs

1. **Start.** The web app's **Run fix** button posts the existing `debug` action directly. `build_debug` starts `debug_job.py --manual` with the job file and any feedback or log references. Explicitly resuming a paused job or supplying new feedback/logs clears `debug_pause_reason` and records `debug_convergence_reset_at` so `analyze_debug_loop_convergence` starts a fresh convergence window instead of immediately re-pausing on prior history. The status box shows the recorded failure, and the inline **Tell the AI what to change** form can add feedback. With device logging configured, a direct click requests `cloud:latest`. The inline form can include or omit it. Design and Tests jobs hand feedback to their specialized worker and skip the separate diagnosis pass.
2. **Diagnose.** `run_propose` gives the debugger the saved brief, current acceptance criteria, linked references, project requirements, builder summary, failing build or test logs, and the iteration ledger. Log references pull device logs when available. The debugger uses the job's reviewer model as its preferred model and supplies the job's allowed models to the router. Its proposal records a hypothesis, action (`patch`, `add_logging`, `investigate`, or `ask_human`), and any targeted tests. The prompt and raw response are saved under the job's `output/<job-id>/iter_<number>/` directory.
   - **`ask_human` action:** When the debugger determines it cannot proceed without human input (missing hardware/device captures, subjective visual confirmation, or an ambiguous requirement), returning `"action": "ask_human"` with `clarification_question` pauses the job as `status: human-needed` with `clarification_asker: "debugger"`, skips `run_implement`, sends a desktop/webhook notification, and surfaces a sticky in-app **Question for you** banner in the web app (even in YOLO mode). Answering in the **Questions** card (`job_actions.answer`) records the Q&A and resumes `debug_job.py --manual --feedback ...` rather than re-planning from scratch.
3. **Implement and verify.** `run_implement` launches `worker_run.py` with `AI_DEBUG_CHILD=1`. If `worker_run.py` crashes early (e.g. `DiskFullError` or quota error), `run_implement` marks the trailing `"pending"` entry in `debug_history` with `aborted: true` so it does not linger or trigger false loop detections. The builder receives the proposed fix. The worker retains untracked files previously attributed to the job (`ai_untracked_files`), runs the normal build and tests on the job's branch (including configured extra checks when applicable), and only commits/pushes changes when `build_ok` is true so non-compiling partial work stays uncommitted for the next fix attempt. Build and test results are recorded in `debug_history`. When checks pass, the worker saves the change and follows the job's normal review flow. A passing build alone still leaves the acceptance criteria to verify.
4. **Stop or continue.** The initial worker can start a repair after build or test failure (`debug_job.py` owns incrementing `iteration`, avoiding double increments). If the repair's own worker fails the checks, it keeps the job on its branch and waits for the next request. A repeated hypothesis or unchanged failure signature can pause unattended automatic retries before the next proposal; `analyze_debug_loop_convergence` ignores `aborted` entries, collapses duplicate same-iteration entries, and respects `debug_convergence_reset_at`. Cross-machine handoffs (`import_remote_job` with `taking_back=True`) merge `debug_history` and `clarification_history` by stable key (`_merge_job_history`) so evolved `pending → resolved` entries or truncated strings are never duplicated. The debugger also applies a default safety check at 30 iterations when the history is no longer converging. A model or CLI failure before a proposal restores the iteration count and records the error. A failed commit after passing checks can offer **Commit the work**, which retries saving the files.

The job's **Runs** section shows each attempt's logs and result. The job record keeps `debug_phase`, `debug_history`, `debug_proposal`, `debug_pause_reason`, and the latest error. Use the run log for tool failures and the iteration ledger for repeated repair hypotheses.

### Blockers

`blockers.detect(text)` recognises a blocker in an error or a build or test log. Only high-confidence signatures count,
so a failure in the code is never taken for one. Each blocker has a `scope` (`project` or `machine`) and a `key`, so
Home's **Blocked** card shows it once with every job it holds up.

- **Where they are recorded.** Both `worker_run.py` and `debug_job.py` run a startup dependency gate (`missing_build_tool_blocker`: missing `build_command`/`test_command` executables in `build_tools.py` and unhydrated lockfiles in `manifest_deps.py`) **before** calling any LLM, pausing immediately with a structured `tool` blocker instead of burning tokens on an unconfigured host. During and after execution, `worker_run.py` checks the run's `build.log`, `test.log` and `environment_failure.md` before it would start a fix attempt (`blocker_in_logs`, `pause_on_blocker`), and checks the exception in its catch-all. `debug_job.py`
  also catches LLM failures in `run_propose`, rolls back the unused iteration increment, and records any detected blocker so
  expired sign-ins and environment issues trigger 1-click sign-in + auto-retry and automatic server resumption. Prior fix
  attempts, ruled-out hypotheses, failing tests, and touched feature files are passed forward in a compact **Iteration Ledger**
  (`iteration_ledger`) so fix loops and fallback models don't re-explore dead ends. Non-overlapping jobs default to 2 concurrent
  worktrees (`parallel_jobs.DEFAULT_AT_ONCE = 2`) behind the scenes unless `max_concurrent_jobs` is set explicitly.
- **Cleared or not.** `is_cleared` checks the blocker now rather than trusting when it was recorded: the lock file is
  gone, the person signed in since (`job_problem.signed_in_at`), the tool is on `PATH`, the build's disk floor is met, the folder is
  writable, or an AI usage/rate limit's reset time (`until`) has passed. Xcode and simulator problems can't be checked, so they wait for **Try again**.
- **Resuming by itself.** The server's notifier loop (every 15 seconds, with or without a browser open) resumes each job
  whose blocker is cleared (`UIServer.resume_cleared_jobs`). Home's refresh calls the same method. Each job is resumed at
  most once an hour per blocker (`auto_resumed_at`, carried over in `last_blocker` when the run starts), so a blocker that
  comes straight back pauses the job again instead of looping.
  **Clear the lock** (`/api/blockers/clear-lock`) removes a git lock only when it is over 10 seconds old and `lsof` shows
  nothing holds it, then resumes the jobs it held without waiting for the hour.

### Git locks

A git command that is killed (often a tool's status check that timed out in a large repository) leaves
`.git/index.lock` behind, and every git command after it fails with "exit status 128". The worker clears a stale lock and
retries during checkout, pull, and the commit at the end (`_git_commit_step`). If the lock is still held after about
30 seconds, the job stops with a plain explanation, not the raw git error.

### After a success

Reaching the success point clears `last_error`, so a finished job doesn't keep offering **Run a fix…** for an earlier
attempt's error. The job page only shows that button while something is actually wrong.

## Jobs at once and overlapping work

By default one job builds in a project at a time: every local start (the worker, and `schedule_job` dispatching to this
computer) goes through `worker_run.run_local`, which waits for the project folder (`folder_lock`). Before this, approving a
plan called `execute_job` directly and skipped the wait, so two jobs could build in one folder at once.

**Jobs at once** (set on the Git page, 1 to 4) lets more build together (`orchestrator/parallel_jobs.py`). It is stored as
this computer's `max_concurrent_jobs` in the project's `config/machines.json`, the same number **Build the plan** and the
scheduler use, so raising it also lets Build the plan run independent features at the same time. A job that would wait builds in its own git worktree instead, kept outside the project in
`~/.orchestrator/worktrees/<project>-<hash>/<job>` so test discovery, Xcode and Unity never see the copies. The worker runs
there with the worktree as its project folder and the project's `.orchestrator` for its records. A new branch starts from
`origin/<base>` (or the local base) without checking the base out, since the project folder has it. The job keeps its
worktree until it's merged, completed, discarded or deleted. Merging and completing only remove a clean worktree; discarding
removes it anyway.

Guards, so jobs never step on each other's work:

| Guard | Where | What happens |
|---|---|---|
| The same request twice, typos and all | `job_overlap.similarity` (near matches for long words) | New job asks: open that one, or create anyway |
| Two open jobs changing the same files | `job_overlap.file_overlaps`, on the job page (`overlapAdviceHtml`) | One next step: the same request twice → Keep this one (archives the other); the other building → Merge waits; one ready → merge it first. Files behind "Which files?" |
| A running job changes the same files, or an unplanned job collides with a blind/related peer | `parallel_jobs.decide` + `parallel_mece.classify_launch_overlap` | Hard overlap auto-waits; soft overlap (same directory, sibling job, or related topic on disjoint files) runs in parallel with lane awareness |
| Active parallel peer lanes (loose ME) | `parallel_mece.load_active_peers` / `lane_context` in `new_job.py` and `run_builder.py` | Planners and builders automatically see active peers' claimed files so they stay out of each other's lane and keep shared integration edits strictly additive |
| Mid-run drift into another active job's files | `scope_check.evaluate(..., peer_files=...)` via `server.job_scope` | Surfaces a soft `peer_overlap` scope finding advising which job to merge first rather than failing the build |
| Sibling requirement gaps or duplicate claims (loose CE) | `parallel_mece.evaluate_sibling_coverage` in `splinter_job.py`, `server.job_detail`, `server.features_overview`, and `meceCoverageHtml` | For splintered parent jobs and Features with requirements, checks that sibling jobs (plus `deferred`) cover every parent requirement without silent gaps or duplicate ownership; surfaces a quiet notice only when a gap or overlap exists |
| A job on the current branch, or with branch switching off | `parallel_jobs.decide` | Waits for the project folder (git can't check out one branch twice) |
| Another open job's uncommitted work is in the folder | `blockers.other_jobs_work`, checked before the worker touches git | Pauses as blocked, "“…” has uncommitted work here"; starts by itself once that work is committed |

## Keeping the AI going

- **State sync startup refresh** (`sync_health.clear_failures`, `server.UIServer.start_state_sync`). On launch,
  `start_state_sync` clears prior-session failure markers from `sync_health.json` (preserving `ok_at`) and runs its
  first `product_sync` / `job_sync` round immediately instead of waiting `CHECK_SECONDS`, so stale errors from an earlier
  session are never shown on startup.
- **End-of-job review gate** (`review_ready.parse_review_outcome`, `worker_run.execute_job`). After build and tests pass,
  `review_ready.py` compares the diff against `brief.md` and `acceptance.block(job)` and records `review_outcome` on the
  job. When the reviewer reports `## Blockers`, `Not met` acceptance criteria, or `Request changes`, `worker_run.py`
  triggers one automatic `trigger_followup_iteration` gap-closure pass instead of marking the job complete; if gaps
  still remain after that pass, the job pauses as `human-needed`.
- **YOLO autoresponses** (`common.autorespond`, `MAX_AUTO_RESPONSES` = 3). When a YOLO job's planner (`new_job.py`) or
  builder (`worker_run.py`, for one task and for each task of a plan) asks a question, Orchestrator answers it
  (`AUTO_ANSWER`, or `AUTO_CONTINUE` for a should-I-continue) and carries on. Each answer goes into
  `clarification_history` with `auto: true` and who asked; past the limit the job pauses as before. The job page labels
  them "Answered for you (YOLO)" and says how many in the hero. When a YOLO job hits a blocker in build/test logs,
  `worker_run.py` goes straight into one automated fix attempt (recording `yolo_blocker_fix_attempts` and passing the
  blocker detail as `debug_feedback`) before falling back to `pause_on_blocker` if it persists.
- **Out of usage** (`usage_limits.py`, `project_chat.record_out_of_usage`, `blockers.py`). A usage-limit, quota, credits or rate-limit
  error is checked before auth/unavailability markers so tool output mentioning `"forbidden"` doesn't misclassify a quota
  error as a rejected sign-in. It sets the model's whole family aside (`limit:<family>` in `model_checks.json`) until the
  reset time read from the message (supporting hours, minutes, and seconds such as `Resets in 18m3s`), or an hour (five
  minutes for a rate limit). `is_model_blocked` leaves it out of every job's choices until then, so it returns to rotation
  by itself. Within the same call `run_llm` skips that family's other models and gives the next builder or fix attempt
  the original task and a `HANDOFF` note to continue from the files already changed. The run timeline shows the quota
  and its reset time when known. If every tried model fails, `run_llm`
  includes each model's short failure reason in `No model could answer (tried: ...)`, and `blockers.detect` records a
  `usage_limit` blocker with the parsed `until` timestamp so `UIServer.resume_cleared_jobs` automatically resumes the job
  once the limit resets. Manage AI tools lists active holds (`usage_holds`).
- **Deactivated models and cross-provider rescue** (`llm.is_unusable_model_error`, `llm._auth_fallback_models`,
  `server._probe_pool_health`). Deactivated or retired model responses (`"is deactivated"`, `"model_deactivated"`) are
  recorded as unavailable (`record_model_failure`) and quarantined for 24 hours (`KEY_HOLD` in `project_chat.py`) rather
  than treated as generic 30-minute blips, so subsequent jobs skip them. During model refresh (`server.refresh_models`),
  opaque CLIs without a model-list command like `pool` are probed once per `CHECK_FRESH` window when signed in so a
  deactivated remote model is excluded ahead of time before a job tries it. When every configured model in a non-free-only
  job fails and at least one hit a quota or all hit provider/account limits, `run_llm` automatically expands to other
  ready, signed-in models on the computer (cheapest adequate first) with `HANDOFF` preserved. If no model can answer
  and a task aborts with an automation error, `worker_run.py` marks both the parent job
  and the active `_task_N.json` file `human-needed` so the task does not remain stuck in `executing`.
- **A Claude call that never answers (macOS privacy prompt)** (`claude_plugins.py`, `llm._claude_plugin_warnings`,
  `preflight.checks`). On macOS, a Claude Code plugin installed for one project (`scope: project` in
  `~/.claude/plugins/installed_plugins.json`) whose folder is inside Documents, Desktop or Downloads is looked up whenever
  `claude` starts in *any* git repository. If the app that started `claude` has no permission for that folder, macOS
  holds the open until someone answers a privacy question, so a headless `claude -p` prints nothing until the stall limit,
  and only inside a git repository (it answers in seconds in a plain folder). Orchestrator reads the plugin file and
  compares paths only: touching the folder to test it would hang the same way. `llm._run_llm_single` prints a
  `[warning]` line naming the plugin, the folder and the fix before a Claude call starts, and Preflight shows a
  `claude-plugins` warning (never a blocker) on the project's checklist. To fix it, allow Documents (or the named
  folder) for the app that runs Orchestrator in System Settings > Privacy & Security > Files and Folders (or add that
  app under Full Disk Access), then restart Orchestrator; or remove the entry from `installed_plugins.json` (keep a
  backup) and re-add the plugin from the project with `/plugin`. A warning does not stop the call: a sign-in or
  permission that is already in place still works.

## Durability

`backup_push` in `worker_run.py` pushes the job's own branch to `origin` after each finished task (`checkpoint_task`) and
after the final commit. It never pushes the base branch, `main`, `master`, `develop` or `trunk`, or a job in manual branch
mode. It's best effort: a failure is recorded as `backup_error` and tried again after the next task, and the job carries
on. A computer that fails mid-job loses at most the task in progress.

The job record is backed up in the snapshot below. For an unfinished job, the snapshot carries `record`
(`account.job_record`): the answers to its questions, its fix attempts, its models, task commits and the rest of its plan,
trimmed so one job stays under 30 KB. **Continue here** restores those fields, so a job picked up elsewhere keeps its
history. What belongs to one computer (its process, its blockers, uncommitted files) never travels.

## Sharing state across computers

- **What's sent.** `account.build_snapshot` lists each computer's recent jobs (20 per project, 10 projects). Each job carries
  the job page's own `state` (label, reason, next step), its `active_run` (label and current step, from
  `UIServer.live_job_runs`), its `blocker`, and where it was imported from.
- **When.** A `snapshot-sync` thread rebuilds the snapshot every 10 seconds (`SNAPSHOT_CHECK_SECONDS`) and sends it when its
  hash changed. The heartbeat still sends one every 5 minutes as a fallback, and archiving or importing sends one at once.
- **How it's shown.** Home on another computer uses that `state` as is, with no action button, since a job can only be
  acted on by the computer that owns it. The remote job page shows the state, the run's step, and the next step to take on
  that computer.

## One owner per job

**Continue here** moves a job to the computer you're on:

1. The new computer imports the job (`import_remote_job`) with `imported_from_machine` and `imported_at`, and fetches its
   branch.
2. The computer it left learns this two ways, whichever comes first:
   - **From the account.** When the new computer's snapshot arrives, the control plane records the move on the computer the
     job left (`_record_moves`, `moved_jobs` on its machine record) and returns it in that computer's next heartbeat reply.
     `apply_moved_jobs` then marks every copy older than the one that moved. No browser is needed.
   - **From Home.** Home there sees the newer copy in the other computer's snapshot and posts `/api/jobs/<id>/moved`.

   Either way, `mark_job_moved` records `moved_to`.
3. A moved copy reads **Last worked on <computer>** (group `done`, no action, no error details), and opening it goes straight to the latest copy's page (`findJobElsewhere`), where the job's own next step (Merge & complete, Resume, Approve…) takes it over and runs (`takeOverJob`, `TAKE_OVER_THEN_RUN`): the import brings its latest record, fetches its branch and restores its unfinished work automatically when local files are clean or already match the backup (skipping identical files; differing local edits are never overwritten automatically). **Work on it here** does the same without running a step. Only a run going on it there must finish first. Home doesn't file it under Completed: it isn't finished, it lives elsewhere. The owner's row (from its snapshot, with the job's real state) stands in for it among the jobs; when there's no such row, the moved copy itself stays there. `_launch_run` refuses every run on it except
   delete and discard, and `execute_job` returns before touching git or a model.
4. Continuing it back imports the other copy over the moved one, since that copy is newer. Comparing `imported_at` stops the
   two computers marking each other as moved.

Continue here is refused while the snapshot shows a run working on the job on the other computer.

**Limits.** The account route needs the control plane deployed with `_record_moves`. Until it is, only Home marks a
moved job. A computer that's offline learns of a move at its next heartbeat.

## When the computer that has the job is offline

Nothing has to be fetched from another computer: what a job needs is on the account or on origin before that computer goes
away. The rules, from local-first sync practice:

| What | Where it lives | How |
|---|---|---|
| Who owns a job | `job_owners` on the account | An ownership number, `owner_epoch` (`account.job_epoch`), is the fencing token. Continue here raises it. The computer holding the highest number owns the job (`ControlPlane._record_owners`); a copy elsewhere at or below it hears it moved through its heartbeat (`moved_jobs` with `epoch`; `apply_moved_jobs`), even if it syncs late. Copies from old project downloads count as 0 and never win. |
| Every job's record | `account_jobs`, one document per job | Each send carries the jobs that changed since the account last took them (`account.job_updates`, up to 60; remembered in `account_jobs_sent.json` once accepted). A record is replaced only by the same computer or a higher ownership number (`_record_jobs`). The remote job page and Continue here see every unfinished job a computer holds, not only the 20 newest in its snapshot (`get_snapshot`). |
| Committed work | the job's branch on origin | `backup_push`, as above. |
| Uncommitted work | `refs/orchestrator/wip/<job id>` on origin | Each heartbeat, a paused or failed job's uncommitted files are committed in a scratch index and pushed there (`wip_backup.backup_job`; never touches the working tree, the index or the branch). The record (`wip_backup`) travels with the job; Continue here offers **Bring the unfinished work here** (`wip_backup.restore`, `POST /api/jobs/<id>/restore-wip`). Files whose local bytes already match the backup commit are skipped instead of blocking; if any local files truly differ, the endpoint returns HTTP 409 with `conflicts` (`WipConflictError`) and the UI asks for explicit confirmation (**Replace local files**, `overwrite: true`) before overwriting them. |
| Archive and Restore | `job_archives` on the account | The newest one per job wins on every computer (`apply_job_archives`). |
| Work a computer never uploaded | side branch `<branch>-from-<computer>`, ref `<job id>-from-<computer>` | When a job is marked moved, unpushed commits and uncommitted files go there (`wip_backup.keep_stranded`), not onto the job's branch; the moved job's page says where. |
| History when a job comes back | the job | Taking a job back keeps both copies' answers and fix attempts, without repeats. |

Moving: once the person agrees for a project, job records and archive events are also written to the project's repository
(`refs/orchestrator/state`) with the same ownership rule, and Home reads them from there first (phase 2, built). Phase 3 stops
the account's copy. See [Sharing project state between computers](state-sync.md).

The remote job page says when the other computer last synced and, offline, that it may have newer work not uploaded yet
(`remoteFreshnessHtml`). Home's row says "offline, synced 3h ago".

**When sync fails** (`orchestrator/sync_health.py`). Each channel records its last success and failure in
`~/.orchestrator/sync_health.json`: `account` (the heartbeat), `jobs` (snapshot and job record sends,
`send_snapshot_if_changed`), `branches` (any open job's `backup_error` from `backup_push`), `unfinished`
(`wip_backup.backup_job`), `product` (the shared product requirements, `product_sync`) and `shared_jobs`
(jobs shared through the repository, `job_sync`). A channel failing for longer than `GRACE_SECONDS` (10 minutes) is a problem:
`/api/sync-health` returns it with `advice` for the usual causes (offline, signed out of the account, GitHub refusing the
push, a rejected push), and Home shows it (`syncHealthBanner`) with each channel's state under **Sync details**. A
success clears it. The job page says when that job's commits or unfinished files aren't backed up (`backupNote`). Errors
are stored without credentials from remote URLs.

**The one limit:** work a computer did after its last upload, while it was cut off, can't reach another computer until it
reconnects. That's why everything uploads early and often; and when it reconnects, that work is kept on a side branch.

## Low disk space

New Job warns before planning if this computer is below the build's disk-space target. On the job page, live `state.disk_low` (`GET /api/state` `disk_free_bytes`, `disk_required_bytes`, `disk_needed_bytes`) takes priority before showing **Run fix**, **Resume**, or **Start** on local jobs (remote SSH-assigned jobs and completed/review-ready jobs are unaffected). The worker checks again before implementation and before running the build, using the tightest of the project, Orchestrator data, and temporary-file volumes. The build floor defaults to 15 GB and uses the existing `ORCHESTRATOR_MIN_DISK_GB` setting.

Open **Free up space** from a startup warning, low-disk job hero, or failed run. The dialog shows how much space is available and how much the build needs, and syncs live free-space state immediately after cleaning caches so the job hero flips right back to its next action once the floor is met. Select the caches to clear after closing the tools that use them and letting active jobs finish. Use **Check again** after clearing other files in macOS **System Settings → General → Storage**. An SSH job's dialog names the computer that needs cleanup. Local jobs paused by the disk blocker are eligible to resume when they meet the build floor.

The dialog recognizes these caches on the project volume:

- **Xcode.** DerivedData entries and shared build caches.
- **Unity.** The ignored `Library` directory in a recognized Unity project (plus `~/Library/Unity/cache` and `~/Library/Caches/com.unity3d.UnityEditor`). Opening the project again reimports its assets. Active-process checks look for `Unity`, `UnityShaderCompiler`, and `UnityPackageManager`, and intentionally ignore the persistent background `Unity.Licensing.Client` IPC daemon. [Unity asset import guide](https://docs.unity3d.com/cn/2022.3/Manual/ImportingAssets.html)
- **Android Studio and IntelliJ.** The ignored project-local `.gradle` cache. If the daemon is still running after closing the IDE, stop it with `./gradlew --stop`. [Gradle directory layout](https://docs.gradle.org/current/userguide/directory_layout.html)
- **Swift Package Manager.** Compiled debug and release products in an ignored `.build` directory. Dependency checkouts are preserved. [Swift build artifact locations](https://docs.swift.org/latest/documentation/swiftlinux/building/)
- **VS Code and VS Code Insiders.** Browser and compiled-code caches. Settings, local history and unsaved-file backups are preserved. [VS Code profile cache definitions](https://github.com/microsoft/vscode/blob/main/.agents/skills/launch/SKILL.md)

Workspace cache discovery looks at the project root and one level of subprojects. Orchestrator checks Git before offering a project cache and skips directories containing tracked files.

## Verification helper recovery

Assisted verification is opt-in. The job runner uses an eligible helper for a configured pair of required checks and falls back to the owner when helper infrastructure fails. The [two-Mac implementation plan](superpowers/plans/2026-11-21-two-mac-verification.md) tracks the remaining recovery work and live pilot.

- **Preflight unavailable.** Check the project's explicit SSH helper opt-in, the job's allowed computers, and the helper's project checkout and Git access. The helper matches the requested check against its own project configuration and checks prerequisites in a detached checkout of the exact candidate. A toolchain mismatch or unavailable candidate makes that helper ineligible for the check.
- **Start reply lost.** Query the existing attempt or repeat start with the same identity. The identity binds the attempt to the project, candidate commit, check definition and owner generation. A completed attempt returns its saved result. A network error leaves the outcome unknown until the helper responds.
- **Lease expires or the attempt is cancelled.** The helper's lease lasts 60 seconds after the last successful renewal. The supervisor stops the running process group when it detects expiry or cancellation. A late result cannot update a terminal attempt. The execution lock is held while the previous check process is still running.
- **Test fails or the helper cannot start.** A completed failure stays a test failure. If the helper cannot supply required evidence, the job tries the check locally. If that check is also blocked, the job records the missing evidence in its test log.
- **Owner job resumes.** The builder opens a new verification invocation on a whole-job resume. Within an invocation, the coordinator reconciles a lost reply using the existing attempt. If the candidate changes after a helper returns evidence, the builder discards that evidence and requires verification again.

The helper keeps its reservation journal under `~/.orchestrator/verification-worker` by default. The owner keeps per-check attempt records under the job's `verification/<invocation-id>/` output directory and accepted check results in the job's `test_commands.json`. Candidate commits travel through temporary refs in the project's Git repository.

## Tests

| Area | Tests |
|---|---|
| Blockers: detection, clearing, grouping, auto-resume, job page | `tests/test_blockers.py` |
| Question blockers and their fixes | `tests/test_question_blockers.py` |
| Tests passed, not committed; no fix offered for an old error | `tests/test_passed_uncommitted.py` |
| Git lock recovery in the commit step | `tests/test_worker_run.py` (`test_committing_finished_work_clears_a_leftover_git_lock_and_retries`) |
| Snapshot contents and sending on change | `tests/test_snapshot_sync.py` |
| Backup push (in throwaway repositories), job ownership, moves from the heartbeat, record restore | `tests/test_job_ownership.py` |
| Commit the work, end to end in a throwaway repository | `tests/test_passed_uncommitted.py` (`CommitOnlyResumeTests`) |
| Out-of-usage holds, deactivated-model fallback with handoff, YOLO autoresponses | `tests/test_autorespond_and_usage.py` |
| Claude plugins that can hang `claude -p` (macOS-protected folders): finding them, the Preflight warning, the pre-call warning | `tests/test_claude_plugins.py`, `tests/test_preflight.py` (`ClaudePluginChecks`), `tests/test_llm_claude_plugin_warning.py` |
| Deactivated-model 24h quarantine and pre-flight `pool` health probe | `tests/test_project_chat.py` (`test_probe_pool_health_blocks_deactivated_pool_model_ahead_of_time`) |
| Task `_task_N.json` status sync on automation error | `tests/test_worker_run.py` (`test_task_automation_error_marks_both_parent_and_task_job_human_needed`) |
| End-of-job review gate: parsing blockers/unmet criteria, auto-fix pass, and `human-needed` pause | `tests/test_acceptance.py` (`ReviewOutcomeGateTests`), `tests/test_worker_run.py` (`test_review_gate_triggers_autofix_once_then_pauses_human_needed`) |
| Inline fix routing, hero controls, low-disk priority over Run fix, and task-title splitting | `tests/test_web_preview.py` (`JobTitleSplitTests`) |
| Disk space floor, cache discovery, Unity active-process check (ignoring `Unity.Licensing.Client`), and cache cleanup | `tests/test_disk_space.py` |
| Unfinished-work backup (`refs/orchestrator/wip/<job>`), identical-file restore skip, 409 conflicts, and opt-in overwrite | `tests/test_wip_backup.py` |
| Automatic minor updates, deferred restart safety, and boot-commit auto-restart when `HEAD` advances on disk | `tests/test_self_update.py` |
| A YOLO job whose planner and builder ask, finishing by itself | `tests/e2e/test_pipeline_offline.py` (`YoloAutorespondTests`) |
| A job blocked mid-run by a held git lock, cleared, resumed by the server, committed and opened as a PR, inside the full offline pipeline | `tests/e2e/test_pipeline_offline.py` (`BlockedJobRecoveryTests`) |
| Moves recorded by the control plane | `tests/test_control_plane.py` (`MovedJobTests`) |
| Jobs at once: deciding, worktrees, branch setup, removal, the setting | `tests/test_parallel_jobs.py` |
| Verification helper leases, reconnect, cancellation and exact checkout | `tests/test_verification_attempts.py`, `tests/test_verification_worker.py`, `tests/test_verification_candidate.py`, `tests/test_verification_coordinator.py` |
| Worktrees, made in one place for the integration check, both previews and jobs at once | `tests/test_worktrees.py` |
| Overlap guards: duplicates with typos, shared files, another job's uncommitted work | `tests/test_overlap_guards.py` |
| A job building alongside another in its own worktree, end to end | `tests/e2e/test_pipeline_offline.py` (`ParallelJobTests`) |

Git-touching tests run only in temporary repositories with a local bare remote, never in this checkout.
