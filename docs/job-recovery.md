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
| Build or tests fail (the code) | `status: debugging` | Needs a fix | AI fix attempt (`debug_job.py`), up to 8, paused when stalled |
| Something outside the code: a git lock, an expired sign-in, a missing tool, a full disk, Xcode or the simulator | `status: human-needed`, `blocker: {...}` (`orchestrator/blockers.py`) | The blocker's title, checked now | Steps to clear it, then **Try again**; resumes by itself once cleared |
| The AI asks a question | `human_clarification_question` | Question for you | **Answer** |
| The AI's "question" is really a blocker ("rerun with write access") | `human_clarification_question`, classified by `classify_clarification` | The AI couldn't continue (`orchestrator/blocker_fix.py`) | The one fix that applies, e.g. **Plan again** telling the planner it plans only |
| The work passed its tests but the commit failed | `passed_uncommitted: {files}` | Tests passed, not committed | **Commit the work**: Resume commits those files without building again, and without checking anything out (a forced checkout would throw the uncommitted work away) |
| Another job is using the project folder | `waiting_for` (`folder_lock`) | Waiting | Starts by itself |
| Continued on another computer | `moved_to` | Moved to <computer> | None here; Continue here on its copy brings it back |

### Blockers

`blockers.detect(text)` recognises a blocker in an error or a build or test log. Only high-confidence signatures count,
so a failure in the code is never taken for one. Each blocker has a `scope` (`project` or `machine`) and a `key`, so
Home's **Blocked** card shows it once with every job it holds up.

- **Where they are recorded.** `worker_run.py` checks the run's `build.log`, `test.log` and `environment_failure.md` before
  it would start a fix attempt (`blocker_in_logs`, `pause_on_blocker`), and checks the exception in its catch-all.
- **Cleared or not.** `is_cleared` checks the blocker now rather than trusting when it was recorded: the lock file is
  gone, the person signed in since (`job_problem.signed_in_at`), the tool is on `PATH`, 2 GB is free, the folder is
  writable. Xcode and simulator problems can't be checked, so they wait for **Try again**.
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
| A running job changes the same files | `parallel_jobs.decide` | The job waits instead of building alongside |
| A job on the current branch, or with branch switching off | `parallel_jobs.decide` | Waits for the project folder (git can't check out one branch twice) |
| Another open job's uncommitted work is in the folder | `blockers.other_jobs_work`, checked before the worker touches git | Pauses as blocked, "“…” has uncommitted work here"; starts by itself once that work is committed |

## Keeping the AI going

- **YOLO autoresponses** (`common.autorespond`, `MAX_AUTO_RESPONSES` = 3). When a YOLO job's planner (`new_job.py`) or
  builder (`worker_run.py`, for one task and for each task of a plan) asks a question, Orchestrator answers it
  (`AUTO_ANSWER`, or `AUTO_CONTINUE` for a should-I-continue) and carries on. Each answer goes into
  `clarification_history` with `auto: true` and who asked; past the limit the job pauses as before. The job page labels
  them "Answered for you (YOLO)" and says how many in the hero.
- **Out of usage** (`usage_limits.py`, `project_chat.record_out_of_usage`). A usage-limit, quota, credits or rate-limit
  error sets the model's whole family aside (`limit:<family>` in `model_checks.json`) until the reset time read from the
  message, or an hour (five minutes for a rate limit). `is_model_blocked` leaves it out of every job's choices until then,
  so it returns to rotation by itself. Within the same call `run_llm` skips that family's other models and gives the next
  builder or fix attempt a `HANDOFF` note: the work is started, read `git status`/`git diff` and finish it. Manage AI
  tools lists the holds (`usage_holds`).

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
3. A moved job reads **Moved to <computer>** (group `done`, no action). `_launch_run` refuses every run on it except
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
| Uncommitted work | `refs/orchestrator/wip/<job id>` on origin | Each heartbeat, a paused or failed job's uncommitted files are committed in a scratch index and pushed there (`wip_backup.backup_job`; never touches the working tree, the index or the branch). The record (`wip_backup`) travels with the job; Continue here offers **Bring the unfinished work here** (`wip_backup.restore`, `/api/jobs/<id>/restore-wip`), which never overwrites files with changes. |
| Archive and Restore | `job_archives` on the account | The newest one per job wins on every computer (`apply_job_archives`). |
| Work a computer never uploaded | side branch `<branch>-from-<computer>`, ref `<job id>-from-<computer>` | When a job is marked moved, unpushed commits and uncommitted files go there (`wip_backup.keep_stranded`), not onto the job's branch; the moved job's page says where. |
| History when a job comes back | the job | Taking a job back keeps both copies' answers and fix attempts, without repeats. |

The remote job page says when the other computer last synced and, offline, that it may have newer work not uploaded yet
(`remoteFreshnessHtml`). Home's row says "offline, synced 3h ago".

**The one limit:** work a computer did after its last upload, while it was cut off, can't reach another computer until it
reconnects. That's why everything uploads early and often; and when it reconnects, that work is kept on a side branch.

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
| Out-of-usage holds, switching with a handoff, YOLO autoresponses | `tests/test_autorespond_and_usage.py` |
| A YOLO job whose planner and builder ask, finishing by itself | `tests/e2e/test_pipeline_offline.py` (`YoloAutorespondTests`) |
| A job blocked mid-run by a held git lock, cleared, resumed by the server, committed and opened as a PR, inside the full offline pipeline | `tests/e2e/test_pipeline_offline.py` (`BlockedJobRecoveryTests`) |
| Moves recorded by the control plane | `tests/test_control_plane.py` (`MovedJobTests`) |
| Jobs at once: deciding, worktrees, branch setup, removal, the setting | `tests/test_parallel_jobs.py` |
| Worktrees, made in one place for the integration check, both previews and jobs at once | `tests/test_worktrees.py` |
| Overlap guards: duplicates with typos, shared files, another job's uncommitted work | `tests/test_overlap_guards.py` |
| A job building alongside another in its own worktree, end to end | `tests/e2e/test_pipeline_offline.py` (`ParallelJobTests`) |

Git-touching tests run only in temporary repositories with a local bare remote, never in this checkout.
