#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import os
os.environ["AI_REQUEST_SOURCE"] = "orchestrator"
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Force line-buffering for stdout/stderr to ensure logs stream immediately
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
except:
    pass

SCRIPTS_DIR = Path(__file__).resolve().parent

from common import (
    LOGS_DIR,
    OUTPUT_DIR,
    ROOT,
    StatusBar,
    find_latest_runtime_log,
    format_job_id,
    get_repo_state,
    gh_comment,
    is_firebase_configured,
    now_iso,
    print_phase,
    read_json,
    record_clarification,
    run,
    run_shell,
    slugify,
    update_issue_status,
    write_json,
)
from open_or_update_pr import open_or_update_pr
from run_builder import BuilderClarificationNeeded, run_builder
from orchestrator.project_config import PROJECT_CONFIG


from typing import List, Optional, Tuple

def is_process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def normalize_branch_mode(branch_mode: Optional[str]) -> str:
    if branch_mode in {"manual", "manual (no git actions)"}:
        return "manual"
    if branch_mode in {"current", "current-branch (git pull)"}:
        return "current"
    return "new"


def current_git_branch() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(ROOT),
    ).decode("utf-8").strip()


def prepare_git_branch(job: dict, issue_number: int) -> Tuple[str, str]:
    base_branch = job.get("base_branch") or os.environ.get("BASE_BRANCH", PROJECT_CONFIG.base_branch)
    generated_branch = f'ai/issue-{issue_number}-{slugify(job["title"])}'
    branch_mode = normalize_branch_mode(job.get("branch_mode"))

    if branch_mode == "manual":
        branch = job.get("branch") or current_git_branch()
        print(f"\n[1/4] Manual branch mode. Using current branch metadata: {branch}")
        print("      - Branch mode is 'manual'. Skipping git operations.")
        return branch, base_branch

    if branch_mode == "current":
        branch = job.get("branch") or current_git_branch()
        print(f"\n[1/4] Preparing current branch: {branch}...")
        print(f"      - Pulling latest for current branch {branch}...")
        if PROJECT_CONFIG.git_remote:
            run(["git", "remote", "set-url", "origin", PROJECT_CONFIG.git_remote], cwd=ROOT)
        run(["git", "fetch", "origin"], cwd=ROOT)
        run(["git", "checkout", "-f", branch], cwd=ROOT)
        run(["git", "pull", "--ff-only", "origin", branch], cwd=ROOT)
        return branch, base_branch

    branch = job.get("branch") or generated_branch
    print(f"\n[1/4] Preparing git branch: {branch}...")
    print(f"      - Fetching origin and updating {base_branch}...")
    if PROJECT_CONFIG.git_remote:
        run(["git", "remote", "set-url", "origin", PROJECT_CONFIG.git_remote], cwd=ROOT)
    run(["git", "fetch", "origin"], cwd=ROOT)
    run(["git", "checkout", "-f", base_branch], cwd=ROOT)
    run(["git", "pull", "--ff-only", "origin", base_branch], cwd=ROOT)

    checkout_res = subprocess.run(["git", "checkout", "-f", branch], cwd=str(ROOT), capture_output=True)
    if checkout_res.returncode == 0:
        print(f"      - Switched to existing branch: {branch}")
        subprocess.run(["git", "pull", "origin", branch], cwd=str(ROOT), capture_output=True)
    else:
        print(f"      - Creating new branch from {base_branch}: {branch}")
        run(["git", "checkout", "-f", "-b", branch], cwd=ROOT)

    return branch, base_branch


def mark_human_needed(job_path: Path, job: dict, question: str, task_index: Optional[int] = None) -> None:
    job["status"] = "human-needed"
    job["human_clarification_question"] = question
    job["last_error"] = f"Builder clarification needed: {question}"
    if task_index is not None:
        job["paused_at_task_index"] = task_index
    job["updated_at"] = now_iso()
    write_json(job_path, job)


def format_distributed_status(status_str: Optional[str]) -> str:
    if not status_str:
        return ""
    if status_str.startswith("SUCCESS"):
        rest = status_str[len("SUCCESS"):]
        return f"\033[1;92mSUCCESS\033[0m{rest}"
    elif status_str.startswith("FAILED"):
        rest = status_str[len("FAILED"):]
        return f"\033[1;91mFAILED\033[0m{rest}"
    elif status_str.startswith("Skipped") or status_str.startswith("Not Triggered"):
        return f"\033[90m{status_str}\033[0m"
    return status_str


def print_status_report(job: dict, build_ok: bool, test_ok: bool, pr_number: Optional[int] = None, pr_url: Optional[str] = None, distributed_status: Optional[str] = None) -> None:
    is_success = build_ok and test_ok
    status = job.get("status", "unknown")
    
    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = 80
    box_width = max(20, min(60, cols - 2))
    
    if is_success:
        print("\n\033[92m" + "=" * box_width)
        print("  🎉 SUCCESS: Implementation Verified  🎉")
        print("=" * box_width)
        
        accomplishment = job.get("builder_summary")
        if not accomplishment and job.get("debug_history"):
            last_debug = job["debug_history"][-1]
            accomplishment = last_debug.get("summary") or last_debug.get("action")
        if not accomplishment:
            brief_dir = OUTPUT_DIR / job.get("job_id", "")
            summary_file = brief_dir / "builder_summary.md"
            if summary_file.exists():
                try:
                    import json
                    content = summary_file.read_text(encoding="utf-8")
                    parsed = json.loads(extract_json_block(content))
                    accomplishment = parsed.get("summary")
                except Exception:
                    pass

        summary = accomplishment or job.get("plan", {}).get("summary")
        summary_label = "Accomplishment:" if accomplishment else "Plan Summary:"
        if summary:
            import textwrap
            wrap_width = max(20, min(80, cols - 4))
            wrapped = textwrap.fill(
                summary,
                width=wrap_width,
                initial_indent="  ",
                subsequent_indent="  ",
                break_long_words=False,
                break_on_hyphens=False
            )
            print(f"\n  \033[1;97m{summary_label}\033[0m")
            print(f"\033[90m{wrapped}\033[0m")

        print("""
     _      _      _
    ( )    ( )    ( )
     X      X      X
    / \\    / \\    / \\ 
        """)
        print(f"  Job ID:      {job['job_id']}")
        print(f"  Status:      {status}")
        if pr_number:
            print(f"  PR:          #{pr_number}")
        if distributed_status:
            print(f"  Distributed: {format_distributed_status(distributed_status)}")
        print("\033[0m")
        
        print("\n\033[1;97mNEXT STEPS:\033[0m")
        if pr_url:
            clickable_pr = f"\033]8;;{pr_url}\033\\{pr_url}\033]8;;\033\\"
            print(f"  1. Review PR changes on GitHub:\n     \033[4;96m{clickable_pr}\033[0m")
        elif pr_number:
            print(f"  1. Review PR changes on GitHub:\n     \033[1;96mgh pr view {pr_number} --web\033[0m")
        else:
            print("  1. Review changes on GitHub.")
        
        print("  2. Test and verify manually on device (via Firebase App Distribution) or simulator.")
        print("  3. Follow up based on your verification:")
        print("     • \033[1;92mIf verified & working:\033[0m Merge PR on GitHub or run \033[1;96morchestrator console\033[0m -> \033[92m[M] Merge\033[0m.")
        job_type = job.get("type", "")
        if job_type in {"feature-plan", "feature", "feature-design"}:
            print("     • \033[1;93mIf bug / missing functionality:\033[0m Enter feedback below or run \033[1;96morchestrator console\033[0m -> \033[1;93m[H] Bug/Missing Functionality\033[0m.")
        else:
            print("     • \033[1;93mIf issues persist / iterate:\033[0m Enter feedback below or run \033[1;96morchestrator console\033[0m -> \033[1;91m[H] Bug Still Happening?\033[0m.")
        print("       \033[90m💡 Tip: Runtime logs are automatically detected and attached for the next AI pass.\033[0m")
    else:
        print("\n\033[1;91m" + "!" * box_width)
        print("  ⚠️  FAILURE: Human Intervention Needed  ⚠️")
        print("!" * box_width)
        print("""
     _______
    |  ___  |
    | |   | |
    | |___| |
    |  ___  |
    | |   | |
    |_|   |_|
        """)
        print(f"  Job ID:      {job['job_id']}")
        print(f"  Status:      {status}")
        print(f"  Build:       {'✅ OK' if build_ok else '❌ FAILED'}")
        print(f"  Tests:       {'✅ OK' if test_ok else '❌ FAILED'}")
        if distributed_status:
            print(f"  Distributed: {format_distributed_status(distributed_status)}")
        print("\033[0m")
        
        print("\n\033[1;97mNEXT STEPS:\033[0m")
        print(f"  1. Inspect build/test failure logs in: \033[1;96m.orchestrator/output/{job['job_id']}/\033[0m")
        print(f"  2. Open \033[1;96morchestrator console\033[0m to link reproduction logs (\033[1;93m[L] Link Logs\033[0m) or provide hints (\033[1;93m[F] Tweak\033[0m).")
        print(f"  3. Resume or auto-fix (\033[1;92m[D]\033[0m Auto-Fix / \033[1;93m[U]\033[0m Resume) once feedback or logs are linked.")


def print_clarification_report(job: dict, question: Optional[str] = None) -> None:
    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = 80
    box_width = max(20, min(60, cols - 2))
    
    print("\n\033[1;93m" + "!" * box_width)
    print("  ⏸️  PAUSED: Builder Needs Clarification  ")
    print("!" * box_width + "\033[0m")
    print(f"Job ID:      {job['job_id']}")
    print(f"Status:      {job.get('status', 'human-needed')}")
    print("\nQuestion:")
    q_text = question or job.get("human_clarification_question", "No clarification question recorded.")
    import textwrap
    wrap_width = max(20, min(80, cols - 4))
    wrapped_q = textwrap.fill(
        q_text,
        width=wrap_width,
        initial_indent="  ",
        subsequent_indent="  ",
        break_long_words=False,
        break_on_hyphens=False
    )
    print(wrapped_q)
    print("\nNEXT STEPS:")
    print("  1. Open \033[1;96morchestrator console\033[0m to answer the question (\033[1;96m[A] Answer Question\033[0m).")
    print("  2. The answer will be supplied to the AI builder and execution will resume automatically.")
    print("=" * box_width + "\n")


def trigger_followup_iteration(job_path: Path, job: dict, feedback: str) -> None:
    recent_log = find_latest_runtime_log(job.get("job_id"))
    log_paths = job.get("last_manual_log_paths", [])
    if not isinstance(log_paths, list):
        log_paths = []
    if recent_log and recent_log not in log_paths:
        log_paths.append(recent_log)
        print(f"\n  ✅ Attached latest log file: \033[97m{recent_log}\033[0m")
        
    job["status"] = "debugging"
    job["debug_phase"] = "propose"
    job["last_manual_log_paths"] = log_paths
    
    if "debug_history" not in job:
        job["debug_history"] = []
        
    iter_num = job.get("iteration", 0) + 1
    job_type = job.get("type", "")
    if job_type in ["feature-plan", "feature", "feature-design"]:
        hypothesis = "User reported bug or missing functionality in new feature."
    else:
        hypothesis = "User follow-up / observation on latest run."

    job["debug_history"].append({
        "iteration": iter_num,
        "hypothesis": hypothesis,
        "action": "Iterate fix based on user feedback.",
        "implementation_plan": feedback,
        "expected_signal": "Validation successful",
        "result": "pending"
    })
    job["iteration"] = iter_num
    job["updated_at"] = now_iso()
    write_json(job_path, job)
    
    print("\n🚀 Starting automated debugging iteration based on your follow-up...")
    from debug_job import run_debug_iteration
    run_debug_iteration(job_path)


def send_notifications(job: dict, title: str, message: str, summary: str | None = None) -> None:
    job_id = job.get("job_id")
    try:
        args = [sys.executable, str(SCRIPTS_DIR / "notify.py"), title, message, job_id or ""]
        if summary:
            args.append(summary)
        subprocess.run(args, cwd=str(ROOT), check=False)
    except:
        pass


def unpack_builder_result(result):
    if len(result) == 3:
        build_ok, test_ok, summary_path = result
        return build_ok, test_ok, summary_path, {}
    return result


def execute_job(job_path: Path, resume: bool = False) -> None:
    job = read_json(job_path)

    if resume and job.get("worker_pid"):
        old_pid = job["worker_pid"]
        if old_pid != os.getpid() and is_process_running(old_pid):
            print(f"      - Worker already running (PID: {old_pid}). Tailing logs...")
            try:
                # Tail the runtime log path so resume works outside the repo-root ai/ directory too.
                subprocess.run(["tail", "-f", str(LOGS_DIR / "batch_test_results.log")])
            except KeyboardInterrupt:
                print("\n      - Detached from logs.")
            return

    # Task 1: Update Job Schema and PID Tracking
    job["worker_pid"] = os.getpid()
    job["updated_at"] = now_iso()
    if "completed_task_indices" not in job:
        job["completed_task_indices"] = []
    write_json(job_path, job)

    status_bar = StatusBar(job, is_processing=True)

    print_phase("execution")
    if resume:
        print("      - RESUME MODE ENABLED")
    status_bar.render()

    issue_number = job["issue_number"]

    try:
        print_phase("git_prep")
        status_bar.render()
        branch, base_branch = prepare_git_branch(job, issue_number)

        print_phase("status_update")
        status_bar.render()
        print(f"[2/4] Updating issue #{issue_number} status to 'executing'...")
        is_debug = job.get("status") == "debugging"

        job["branch"] = branch
        job["base_branch"] = base_branch

        if not is_debug:
            job["status"] = "executing"

        job["updated_at"] = now_iso()
        write_json(job_path, job)

        update_issue_status(
            issue_number,
            "status:executing",
            ["status:planned", "status:fix-requested", "status:debugging"]
        )
        
        # Add comment about worker start
        machine_name = os.environ.get("MACHINE_NAME", "local")
        start_msg = f"🚀 **Worker Started** on `{machine_name}`\n\n- **Job ID**: `{job['job_id']}`\n- **Branch**: `{branch}`"
        gh_comment(issue_number, start_msg)

        # Capture state before builder
        pre_state = get_repo_state()

        if job.get("type") == "feature-plan" and job.get("approved"):
            tasks = job.get("plan", {}).get("tasks", [])
            completed = job.get("completed_task_indices", [])
            all_ok = True
            for i, task in enumerate(tasks):
                if i in completed:
                    print(f"      - Task {i+1} already completed. Skipping.")
                    continue

                print_phase(f"sub-task {i+1}/{len(tasks)}", subtext=task['title'])
                
                # Create a temporary, task-specific job object for the builder
                task_job = copy.deepcopy(job)
                task_job['job_id'] = f"{job['job_id']}_task_{i+1}" # Unique ID per task
                task_job['title'] = f"{job['title']} (Task {i+1}: {task['title']})"
                
                # CLEAN SLATE: Don't inherit parent errors or history that might distract the worker
                task_job['last_error'] = None
                task_job['debug_history'] = []
                task_job['ai_modified_files'] = []
                task_job['ai_untracked_files'] = []
                task_job['iteration'] = 0
                
                task_job['plan']['summary'] = task['description'] # Use task description as summary
                task_job['plan']['acceptance_criteria'] = task['acceptance_criteria']
                task_job['plan']['likely_files'] = task['likely_files']
                
                # Write this temp view to a temporary path to pass to run_builder
                temp_job_path = job_path.parent / f"{job_path.stem}_task_{i+1}.json"
                write_json(temp_job_path, task_job)
                
                while True:
                    try:
                        # In YOLO mode sub-tasks, we disable the 'resume' forensics because it often 
                        # hits false positives from previous tasks.
                        build_ok, test_ok, _summary_path, task_output = unpack_builder_result(run_builder(temp_job_path, resume=False))
                        break
                    except BuilderClarificationNeeded as clarification:
                        mark_human_needed(job_path, job, clarification.question, task_index=i)
                        update_issue_status(issue_number, "status:human-needed", ["status:executing"])
                        if sys.stdin.isatty() and not job.get("headless", False) and not is_yolo:
                            print_clarification_report(job, question=clarification.question)
                            print("\033[97m💡 Answer now to resume the builder immediately, or press Enter to pause.\033[0m")
                            try:
                                user_ans = input("\033[1;96mYour Answer (or Enter to pause):\033[0m ").strip()
                            except (EOFError, KeyboardInterrupt):
                                user_ans = ""
                                print(f"\n\033[90m  💾 Progress saved: Job paused at Task {i+1} ({task.get('name', f'Task {i+1}')}).\033[0m")
                                print(f"\033[90m  💡 Resume anytime by running: orchestrator console\033[0m\n")
                            if user_ans:
                                record_clarification(job, clarification.question, user_ans)
                                record_clarification(task_job, clarification.question, user_ans)
                                write_json(job_path, job)
                                write_json(temp_job_path, task_job)
                                print(f"\n      - Resuming Task {i+1} with your clarification...", flush=True)
                                continue
                        send_notifications(
                            job,
                            "Job Paused: Clarification Needed",
                            f"Builder needs clarification for Issue #{issue_number}.\nQuestion: {clarification.question}\nTitle: {job['title']}"
                        )
                        return
                    except Exception as e:
                        # SYSTEM/LLM ERROR: Do not trigger automated debugging
                        print(f"\n❌ Automation error during Task {i+1}: {e}")
                        job["last_error"] = str(e)
                        job["status"] = "human-needed"
                        job["worker_pid"] = None
                        job["updated_at"] = now_iso()
                        write_json(job_path, job)
                        return
                
                # IMPORTANT: Only mark task done if code was actually changed (patch)
                # and validation passed. If AI only 'investigated', it's not done yet.
                was_implemented = task_output.get("action") == "patch" or task_output.get("action") == "add_logging"
                
                # VERIFY: Did the AI actually touch the files it said it would?
                # This prevents "phantom completion" where the AI runs old passing tests 
                # but doesn't implement the current task.
                task_post_state = get_repo_state()
                task_modified = [f for f in task_post_state["modified"] if f not in pre_state["modified"]]
                task_untracked = [f for f in task_post_state["untracked"] if f not in pre_state["untracked"]]
                files_touched = task_modified + task_untracked
                
                likely_files = task.get("likely_files", [])
                actual_likely_touched = any(any(f.endswith(lf) for lf in likely_files) for f in files_touched)
                
                # VERIFY: Did the AI run the right tests?
                # If the builder overrode the test command to something unrelated to the task, flag it.
                test_command_used = task_output.get("test_command", "")
                expected_tests = task.get("tests", [])
                test_command_ok = not expected_tests or any(et in test_command_used for et in expected_tests)
                
                if build_ok and test_ok and was_implemented:
                    if likely_files and not actual_likely_touched:
                         print(f"      - ⚠️  Task {i+1} CLAIMED success, but none of the likely files were touched.")
                         print(f"      - Files touched: {files_touched}")
                         print(f"      - Likely files: {likely_files}")
                         job["status"] = "human-needed"
                         job["last_error"] = f"Task {i+1} phantom completion: AI claimed success but didn't modify expected files."
                         write_json(job_path, job)
                         all_ok = False
                         break

                    if not test_command_ok:
                         print(f"      - ⚠️  Task {i+1} passed tests, but the test command was downgraded.")
                         print(f"      - Test command used: {test_command_used}")
                         print(f"      - Expected tests: {expected_tests}")
                         job["status"] = "human-needed"
                         job["last_error"] = f"Task {i+1} phantom completion: AI ran unrelated tests to get a green signal."
                         write_json(job_path, job)
                         all_ok = False
                         break

                    job["completed_task_indices"].append(i)
                    write_json(job_path, job)
                elif build_ok and test_ok and not was_implemented:
                    print(f"      - Task {i+1} investigation successful, but no code changed. Halting to prevent phantom completion.")
                    # We mark it as human-needed to break the loop and avoid infinite recursion
                    job["status"] = "human-needed"
                    job["last_error"] = f"Task {i+1} was not implemented (AI did not write code)."
                    write_json(job_path, job)
                    all_ok = False 
                    break 
                else:
                    print(f"!!! Sub-task {i+1} failed validation. Halting feature implementation.")
                    all_ok = False
                    break # Exit the loop on first failure
            
            build_ok = all_ok
            test_ok = all_ok
        else:
            # Original single-task execution
            while True:
                try:
                    build_ok, test_ok, _summary_path, task_output = unpack_builder_result(run_builder(job_path, resume=resume))
                    break
                except BuilderClarificationNeeded as clarification:
                    mark_human_needed(job_path, job, clarification.question)
                    update_issue_status(issue_number, "status:human-needed", ["status:executing"])
                    if sys.stdin.isatty() and not job.get("headless", False) and not is_yolo:
                        print_clarification_report(job, question=clarification.question)
                        print("\033[97m💡 Answer now to resume the builder immediately, or press Enter to pause.\033[0m")
                        try:
                            user_ans = input("\033[1;96mYour Answer (or Enter to pause):\033[0m ").strip()
                        except (EOFError, KeyboardInterrupt):
                            user_ans = ""
                            print(f"\n\033[90m  💾 Progress saved: Job paused in 'human-needed' state.\033[0m")
                            print(f"\033[90m  💡 Resume anytime by running: orchestrator console\033[0m\n")
                        if user_ans:
                            record_clarification(job, clarification.question, user_ans)
                            write_json(job_path, job)
                            print(f"\n      - Resuming builder with your clarification...", flush=True)
                            resume = False
                            continue
                    send_notifications(
                        job,
                        "Job Paused: Clarification Needed",
                        f"Builder needs clarification for Issue #{issue_number}.\nQuestion: {clarification.question}\nTitle: {job['title']}"
                    )
                    return

        # Capture state after builder
        post_state = get_repo_state()

        # Identify files changed/added by AI
        ai_modified = [f for f in post_state["modified"] if f not in pre_state["modified"]]
        ai_untracked = [f for f in post_state["untracked"] if f not in pre_state["untracked"]]

        # Re-read job to get updates from run_builder (like test_command_override)
        job = read_json(job_path)

        if task_output and isinstance(task_output, dict):
            if task_output.get("summary"):
                job["builder_summary"] = task_output["summary"]
            if task_output.get("hypothesis"):
                job["builder_hypothesis"] = task_output["hypothesis"]

        job["ai_modified_files"] = ai_modified
        job["ai_untracked_files"] = ai_untracked
        write_json(job_path, job)

        # Commit changes if any
        if ai_modified or ai_untracked:
            print(f"      - Committing {len(ai_modified)} modified and {len(ai_untracked)} untracked files...")
            for f in ai_modified + ai_untracked:
                run_shell(f"git add {shlex.quote(f)}", cwd=ROOT)
            
            commit_msg = f"feat: {job['title']} (AI generated)"
            if job.get("type") == "bug-fix":
                commit_msg = f"fix: {job['title']} (AI generated)"
            
            run_shell(f"git commit -m '{commit_msg}'", cwd=ROOT)

        tasks = job.get("plan", {}).get("tasks", [])
        completed = job.get("completed_task_indices", [])
        has_remaining_feature_tasks = (
            job.get("type") == "feature-plan"
            and bool(tasks)
            and len(completed) < len(tasks)
        )

        if has_remaining_feature_tasks and build_ok and test_ok:
            job["status"] = "executing" if job.get("is_yolo") else "review-needed"
            job["updated_at"] = now_iso()
            write_json(job_path, job)
            if job.get("is_yolo"):
                print(
                    f"\n\033[1;93m🚀 YOLO MODE: {len(completed)}/{len(tasks)} tasks complete; continuing automatically...\033[0m"
                )
                time.sleep(2)
                return execute_job(job_path, resume=True)

        write_json(job_path, job)

        if not build_ok or not test_ok:
            # Set job status to debugging to trigger an automatic iteration
            job["status"] = "debugging"
            job["debug_phase"] = "propose"
            job["iteration"] = job.get("iteration", 0) + 1
            if "max_iterations" not in job:
                job["max_iterations"] = 8
            
            # Record failure in history if this was a debug iteration
            if job.get("debug_history"):
                last = job["debug_history"][-1]
                if last.get("result") == "pending":
                    last["result"] = {
                        "build_ok": build_ok,
                        "tests_ok": test_ok,
                        "notes": "Validation failed during worker run"
                    }
                
            job["updated_at"] = now_iso()
            write_json(job_path, job)

            if os.environ.get("AI_DEBUG_CHILD") == "1":
                print("\n⚠️ Tests failed during debug implementation. Leaving job ready for the next debug iteration.")
                return

            # Trigger debug_job.py automatically
            print("\n⚠️ Tests failed. Triggering automated debug iteration...")
            from debug_job import run_debug_iteration
            run_debug_iteration(job_path)

            return

        # SUCCESS POINT
        likely_files = job.get("plan", {}).get("likely_files", [])
        is_ios = any(f.endswith(".swift") or f.endswith(".storyboard") or f.endswith(".plist") for f in likely_files)

        is_yolo = job.get("yolo", False)
        tasks = job.get("plan", {}).get("tasks", [])
        completed = job.get("completed_task_indices", [])
        all_tasks_done = len(completed) >= len(tasks)

        distributed_status = "Not Triggered"
        if PROJECT_CONFIG.firebase_distribution and is_ios and (not is_yolo or all_tasks_done):
            print("\n🚀 Automated build delivery triggered after verification...")
            try:
                res = subprocess.run([sys.executable, str(SCRIPTS_DIR / "deliver_build.py"), str(job_path)], cwd=str(ROOT))
                if res.returncode == 0:
                    formatted_time = now_iso().replace("T", " ")[:19]
                    method = PROJECT_CONFIG.delivery_method or "ad-hoc"
                    distributed_status = f"SUCCESS via Firebase App Distribution ({method}) at {formatted_time}"
                else:
                    distributed_status = f"FAILED (exit code {res.returncode})"
            except Exception as e:
                print(f"\n⚠️ Build automated delivery failed: {e}")
                distributed_status = f"FAILED ({e})"
        else:
            if not PROJECT_CONFIG.firebase_distribution:
                distributed_status = "Skipped (Firebase distribution disabled)"
            elif not is_ios:
                distributed_status = "Skipped (Non-iOS task)"
            else:
                distributed_status = "Skipped (YOLO mode - awaiting final task)"

        print_phase("pull_request")
        status_bar.render()
        print(f"[4/4] Implementation successful. Opening/updating Pull Request...")
        pr_number, pr_url = open_or_update_pr(job_path)

        if is_debug:
            # Update history with success
            history = job.get("debug_history")
            if history:
                history[-1]["result"] = {
                    "build_ok": True,
                    "tests_ok": True,
                    "notes": "Implementation succeeded and PR updated"
                }
            job["debug_phase"] = "propose" # Ready for next iteration or verification

            # Add a commit with iteration info
            iter_num = job["iteration"]
            prop = job.get("debug_proposal", {})
            commit_msg = f"debug(iter {iter_num}): {prop.get('action', 'update')}"
            run_shell(f"git commit --allow-empty -m '{commit_msg}'", cwd=ROOT, check=False)
            run_shell(f"git push origin {branch}", cwd=ROOT, check=False)

        job["pr_number"] = pr_number
        job["pr_url"] = pr_url
        if not is_debug:
            job["status"] = "review-needed"

        job["updated_at"] = now_iso()
        write_json(job_path, job)

        if not is_debug:
            update_issue_status(
                issue_number,
                "status:review-needed",
                ["status:executing"]
            )
            
            # Post success comment
            success_msg = f"✅ **Implementation Complete**\n\n- **PR**: #{pr_number}\n- **Status**: Ready for review/merge."
            gh_comment(issue_number, success_msg)

        print_phase("review")
        status_bar.render()
        print(f"\n[5/5] Triggering local AI review for PR #{pr_number}...")
        brief_file = OUTPUT_DIR / job["job_id"] / "brief.md"
        run_shell(
            f'{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPTS_DIR / "review_ready.py"))} {pr_number} --reviewer {shlex.quote(job["reviewer"])} --brief-file {shlex.quote(str(brief_file))} --job-file {shlex.quote(str(job_path))}',
            cwd=ROOT,
            check=True,
            capture=False,
        )

        print_status_report(job, True, True, pr_number=pr_number, pr_url=pr_url, distributed_status=distributed_status)
        
        # Determine if we should distribute
        is_yolo = job.get("is_yolo", False)
        tasks = job.get("plan", {}).get("tasks", [])
        completed = job.get("completed_task_indices", [])
        all_tasks_done = len(completed) >= len(tasks)

        # Send completion notification
        job_summary = job.get("plan", {}).get("summary")
        send_notifications(
            job, 
            "Job Complete: SUCCESS", 
            f"Implementation successful for Issue #{issue_number}.\nPR: #{pr_number}\nTitle: {job['title']}",
            summary=job_summary
        )

        # YOLO MODE: Automatically continue to next task
        if job.get("is_yolo", False):
            tasks = job.get("plan", {}).get("tasks", [])
            completed = job.get("completed_task_indices", [])
            if len(completed) < len(tasks):
                print("\n\033[1;93m🚀 YOLO MODE: Proceeding to next task automatically...\033[0m")
                time.sleep(2)
                # Re-run execute_job (recursive continuation)
                return execute_job(job_path, resume=True)
            else:
                print("\n\033[1;92m🏁 YOLO MODE: All tasks completed successfully!\033[0m")

        # Interactive Follow-up / Bug / Missing Functionality prompt
        if sys.stdin.isatty() and not job.get("headless", False) and not job.get("is_yolo", False):
            job_type = job.get("type", "")
            is_feature = job_type in ["feature-plan", "feature", "feature-design"]

            print("\n" + "─" * 60)
            if is_feature:
                print("\033[1;97m💬 New Feature Verification (Optional):\033[0m")
                print("\033[90mIf you tested this feature and noticed any bug, missing functionality, or UI adjustments\n(e.g., 'Button added but tap handler doesn\\'t submit' or 'Add empty state message'),\nenter it below to immediately trigger an automated iteration with your latest runtime logs attached.\nPress Enter to finish and exit.\033[0m\n")
                prompt_label = "\033[1;96mBug / Missing Functionality (or Enter to finish):\033[0m "
            else:
                print("\033[1;97m💬 Follow-up / Bug Still Happening? (Optional):\033[0m")
                print("\033[90mIf you tested this build and noticed remaining issues or have follow-up feedback\n(e.g., 'Risk tab is visible now but not populated'), enter it below to immediately\ntrigger an automated fix iteration with your latest runtime logs automatically attached.\nPress Enter to finish and exit.\033[0m\n")
                prompt_label = "\033[1;96mFollow-up / Feedback (or Enter to finish):\033[0m "

            try:
                followup_ans = input(prompt_label).strip()
            except (EOFError, KeyboardInterrupt):
                followup_ans = ""
                print("")

            if followup_ans:
                trigger_followup_iteration(job_path, job, followup_ans)
                return

    except BuilderClarificationNeeded as e:
        print("\n" + "!"*60)
        print(f"⏸️  EXECUTION PAUSED: {e}")
        print("!"*60 + "\n")

        mark_human_needed(job_path, job, e.question)
        print_clarification_report(job)
        update_issue_status(issue_number, "status:human-needed", ["status:executing"])
        
        # Post pause comment
        pause_msg = f"⏸️ **Execution Paused**\n\nThe Builder requires human clarification to proceed:\n> {e.question}"
        gh_comment(issue_number, pause_msg)
        
        job_summary = job.get("plan", {}).get("summary")
        send_notifications(
            job,
            "Job Paused: Clarification Needed",
            f"Builder needs clarification for Issue #{issue_number}.\nQuestion: {e.question}\nTitle: {job['title']}",
            summary=job_summary
        )
        return

    except Exception as e:
        print(f"\n" + "!"*60)
        print(f"❌ CRITICAL ERROR during execution: {e}")
        print("!"*60 + "\n")

        # Mark as human-needed so it's obvious in the console
        job["status"] = "human-needed"
        job["updated_at"] = now_iso()
        job["last_error"] = str(e)
        job["worker_pid"] = None
        write_json(job_path, job)

        # Update GitHub too
        try:
            update_issue_status(issue_number, "status:human-needed", ["status:executing"])
            
            # Post error comment
            err_msg = f"❌ **Execution Failed**\n\nA critical error occurred during implementation:\n```\n{e}\n```"
            gh_comment(issue_number, err_msg)
        except:
            pass

        # Send failure notification
        job_summary = job.get("plan", {}).get("summary")
        send_notifications(
            job, 
            "Job Complete: FAILED", 
            f"Critical error during execution for Issue #{issue_number}.\nError: {e}\nTitle: {job['title']}",
            summary=job_summary
        )

        # Re-raise so the user still gets the full traceback for debugging
        raise



def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_file")
    parser.add_argument("--resume", action="store_true", help="Resume from existing artifacts")
    args = parser.parse_args()

    execute_job(Path(args.job_file), resume=args.resume)


if __name__ == "__main__":
    main()
