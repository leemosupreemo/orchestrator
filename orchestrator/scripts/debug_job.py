#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from common import ROOT, OUTPUT_DIR, PROMPTS_DIR, now_iso, read_json, write_json, write_text, run_shell, print_phase, StatusBar, analyze_debug_loop_convergence, get_job_test_summary
from llm import run_llm, extract_json_block
from model_router import ModelRole
from reference_artifacts import reference_context

SCRIPTS_DIR = Path(__file__).resolve().parent

MAX_LOG_SOURCE_PATH_CHARS = 512
MAX_LOG_LINE_CHARS = 2_000
MAX_LOG_ENTRY_CHARS = 15_000
MAX_RUNTIME_LOG_CHARS = 100_000


def split_log_sources(logs_path: str) -> list[str]:
    """Split comma-separated log paths without exploding pasted raw log text."""
    raw_log_markers = [
        "\n",
        "com.apple.dt.ActivityLogSectionAttachment",
        "ENAMETOOLONG",
        "Traceback (most recent call last)",
    ]
    if any(marker in logs_path for marker in raw_log_markers):
        return [logs_path]
    return [part.strip() for part in logs_path.split(",") if part.strip()]


def looks_like_path(text: str) -> bool:
    if not text or len(text) > MAX_LOG_SOURCE_PATH_CHARS or "\n" in text or "\0" in text:
        return False
    if "com.apple.dt." in text or "{" in text or "}" in text:
        return False
    return True


def sanitize_runtime_log(text: str) -> str:
    dropped_activity_lines = 0
    dropped_tool_path_lines = 0
    sanitized_lines: list[str] = []

    for line in text.splitlines():
        if line.startswith("Error stating path ") and (
            "ENAMETOOLONG" in line or "com.apple.dt." in line or len(line) > MAX_LOG_LINE_CHARS
        ):
            dropped_tool_path_lines += 1
            continue

        if "com.apple.dt.ActivityLogSectionAttachment.TaskMetrics" in line:
            dropped_activity_lines += 1
            continue

        if re.match(r'^\d+"(?:Target|com\.apple\.dt\.)', line):
            dropped_activity_lines += 1
            continue

        if len(line) > MAX_LOG_LINE_CHARS:
            head = line[:1_200]
            tail = line[-400:]
            line = f"{head}\n...[truncated long log line: {len(line)} chars]...\n{tail}"

        sanitized_lines.append(line)

    notes = []
    if dropped_activity_lines:
        notes.append(f"[debug_job] Omitted {dropped_activity_lines} serialized Xcode activity-log line(s).")
    if dropped_tool_path_lines:
        notes.append(f"[debug_job] Omitted {dropped_tool_path_lines} tool path-stat error line(s) caused by serialized logs.")

    sanitized = "\n".join(notes + sanitized_lines)
    if len(sanitized) > MAX_LOG_ENTRY_CHARS:
        head_size = int(MAX_LOG_ENTRY_CHARS * 0.2)
        tail_size = int(MAX_LOG_ENTRY_CHARS * 0.8)
        omitted = len(sanitized) - (head_size + tail_size)
        sanitized = (
            sanitized[:head_size]
            + f"\n...[debug_job truncated {omitted} chars]...\n"
            + sanitized[-tail_size:]
        )
    return sanitized


def append_runtime_log(current: str, header: str, content: str) -> str:
    cleaned = content.strip()
    if not cleaned:
        return current

    entry = f"{header}\n{cleaned}\n"
    if len(entry) > MAX_LOG_ENTRY_CHARS:
        entry = (
            entry[: MAX_LOG_ENTRY_CHARS // 2]
            + f"\n...[debug_job truncated large log: {len(entry) - MAX_LOG_ENTRY_CHARS} chars omitted]...\n"
            + entry[-MAX_LOG_ENTRY_CHARS // 2 :]
        )

    combined = current + entry
    if len(combined) > MAX_RUNTIME_LOG_CHARS:
        overflow = len(combined) - MAX_RUNTIME_LOG_CHARS
        head_size = MAX_RUNTIME_LOG_CHARS // 4
        tail_size = MAX_RUNTIME_LOG_CHARS - head_size
        return (
            combined[:head_size]
            + f"\n...[debug_job truncated older runtime log context: {overflow} chars omitted]...\n"
            + combined[-tail_size:]
        )
    return combined


def describe_log_source(source: str) -> str:
    if looks_like_path(source):
        return source
    return f"inline runtime log ({len(source)} chars)"


def run_debug_iteration(job_path: Path, logs: str | None = None, feedback: str | None = None, max_iterations: int | None = None) -> None:
    job = read_json(job_path)
    
    # 1. State Machine Initialization
    if job.get("status") != "debugging":
        job["status"] = "debugging"
        job["debug_phase"] = "propose"
        job["iteration"] = 0
        job["debug_history"] = []
        if "max_iterations" not in job:
            job["max_iterations"] = 8

    # 2. Handle Iteration Overrides
    if max_iterations is not None:
        job["max_iterations"] = max_iterations
        if job.get("debug_phase") == "paused":
            job["debug_phase"] = "propose"
        write_json(job_path, job)
    elif job.get("debug_phase") == "paused":
        job["debug_phase"] = "propose"
        write_json(job_path, job)

    # 3. Check for Intelligent Convergence / Loop Termination
    test_summary = get_job_test_summary(job)
    convergence = analyze_debug_loop_convergence(job, test_summary)
    safety_ceiling = max_iterations if max_iterations is not None else 30

    if convergence["is_stuck"] or (job["iteration"] >= safety_ceiling and convergence["health"] != "converging"):
        if convergence["is_stuck"]:
            reason = f"Loop/Stall detected: {convergence['description']}"
        else:
            reason = f"Automated repair paused after {job['iteration']} attempts without passing all tests."

        print(f"\n\033[1;93m!!! {reason} Pausing for human review.\033[0m")
        job["status"] = "debugging"
        job["debug_phase"] = "paused"
        job["debug_pause_reason"] = reason
        write_json(job_path, job)
        
        # Send failure notification
        try:
            run_shell(f'{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPTS_DIR / "notify.py"))} "Job Paused: {reason}" "Issue #{job.get("issue_number", "")} reached pause condition.\nReason: {reason}\nTitle: {job.get("title", "")}" "{job.get("job_id", "")}"', cwd=ROOT, check=False)
        except:
            pass
        return

    job["iteration"] += 1
    iteration = job["iteration"]
    
    # 3. Print Iteration Header
    print("\n" + "="*80)
    print(f"\033[1;93m🔄 DEBUG ITERATION #{iteration}\033[0m")
    print("="*80 + "\n")
    
    print_phase("debug_loop", subtext=f"Iteration {iteration} (Phase: {job['debug_phase']})")

    # 3. Handle Phases
    if job["debug_phase"] == "propose":
        run_propose(job, job_path, logs, feedback)
    elif job["debug_phase"] == "implement":
        run_implement(job, job_path)
    elif job["debug_phase"] == "verify":
        run_verify(job, job_path)

def run_propose(job: dict, job_path: Path, logs_path: str | None, feedback: str | None) -> None:
    print_phase("investigation", subtext="ingesting logs & brief")
    
    # Prepare artifacts directory
    iter_dir = OUTPUT_DIR / job["job_id"] / f"iter_{job['iteration']}"
    iter_dir.mkdir(parents=True, exist_ok=True)

    # Build prompt
    prompt_template = (PROMPTS_DIR / "debug_agent.md").read_text(encoding="utf-8")
    
    # Ingest context
    brief_file = OUTPUT_DIR / job["job_id"] / "brief.md"
    brief = brief_file.read_text(encoding="utf-8") if brief_file.exists() else "No brief available"
    brief += reference_context(job)
    
    prev_impl = ""
    builder_summary = OUTPUT_DIR / job["job_id"] / "builder_summary.md"
    if builder_summary.exists():
        prev_impl = builder_summary.read_text(encoding="utf-8")
    runtime_logs = ""
    
    # 1. Build & Test Auto-Discovery
    auto_logs = []
    job_out_dir = OUTPUT_DIR / job["job_id"]
    
    # Always include build log if it exists (captured compiler errors)
    build_log = job_out_dir / "build.log"
    if build_log.exists():
        auto_logs.append(str(build_log))
        
    # Always include test log
    test_log = job_out_dir / "test.log"
    if test_log.exists():
        auto_logs.append(str(test_log))
        
    # Include xcresult summary if available
    test_results = job_out_dir / "test_results.txt"
    if test_results.exists():
        auto_logs.append(str(test_results))

    # 2. User-Linked Manual Logs
    manual_paths = job.get("last_manual_log_paths", [])
    if manual_paths:
        print(f"      - Including {len(manual_paths)} manually linked log(s)...")
        auto_logs.extend(manual_paths)

    # Combine all discovered logs into logs_path string
    if auto_logs:
        discovered_str = ",".join(auto_logs)
        if logs_path:
            logs_path += f",{discovered_str}"
        else:
            logs_path = discovered_str

    if logs_path:
        # Split and deduplicate
        raw_list = split_log_sources(logs_path)
        seen = set()
        log_list = []
        for l in raw_list:
            if l not in seen:
                log_list.append(l)
                seen.add(l)

        # Sort logs by name (which contains YYYYMMDD-HHMMSS) to ensure chronological order
        # Newest first
        log_list.sort(reverse=True)

        print(f"\n      - Ingesting {len(log_list)} log(s) (newest first) for debugging proposal...")
        for i, lp in enumerate(log_list):
            label = "MOST RECENT" if i == 0 else f"HISTORICAL ({i})"
            source_description = describe_log_source(lp)
            print(f"        [link] {source_description} -> {label}")

            section_header = f"\n=== LOG SOURCE: {source_description} [{label}] ===\n"

            if not looks_like_path(lp):
                runtime_logs = append_runtime_log(runtime_logs, section_header, lp)
                continue

            p = ROOT / lp if not Path(lp).is_absolute() else Path(lp)

            if p.is_dir():
                runtime_logs += section_header
                for lf in sorted(list(p.glob("*.log"))):
                    log_text = lf.read_text(encoding="utf-8", errors="replace")
                    runtime_logs = append_runtime_log(runtime_logs, f"\n[{lf.name}]:\n", log_text)
            elif p.exists():
                log_text = p.read_text(encoding="utf-8", errors="replace")
                runtime_logs = append_runtime_log(runtime_logs, section_header, log_text)
            else:
                print(f"        [skip] log path not found: {lp}")

    full_prompt = prompt_template.replace("{{brief}}", brief)
    full_prompt = full_prompt.replace("{{previous_implementation}}", prev_impl)
    full_prompt = full_prompt.replace("{{debug_history}}", json.dumps(job.get("debug_history", []), indent=2))
    full_prompt = full_prompt.replace("{{runtime_logs}}", runtime_logs)
    full_prompt = full_prompt.replace("{{human_feedback}}", feedback or "None")

    write_text(iter_dir / "prompt.md", full_prompt)

    # Call LLM
    print_phase("agent_thinking", subtext="proposing debug fix")
    raw_output, actual_model, session_id = run_llm(
        job["reviewer"], 
        full_prompt, 
        cwd=ROOT, 
        timeout=1200, 
        allowed_models=job.get("allowed_models"),
        role=ModelRole.DEBUGGER
    )

    # Track session IDs in the job
    if "llm_sessions" not in job:
        job["llm_sessions"] = []
    job["llm_sessions"].append({"id": session_id, "model": actual_model})
    write_text(iter_dir / "llm_response.json", raw_output)

    try:
        json_output = extract_json_block(raw_output)
        
        # Print debug thoughts if present
        debug_thoughts = raw_output.replace(json_output, "").strip()
        debug_thoughts = re.sub(r"```(?:json|markdown)?", "", debug_thoughts).strip()
        if debug_thoughts:
            print(f"      - Debugger analysis: {debug_thoughts}")

        plan = json.loads(json_output)
        job["debug_proposal"] = plan
        job["debug_phase"] = "implement"
        
        # Record iteration in history
        job.setdefault("debug_history", []).append({
            "iteration": job["iteration"],
            "model": actual_model,
            "hypothesis": plan.get("hypothesis"),
            "action": plan.get("action"),
            "result": "pending"
        })
        
        print(f"      - Proposal received: {plan.get('action')}")
        print(f"      - Hypothesis: {plan.get('hypothesis')}")

        # Check if hypothesis repeats previous attempts
        prev_hypotheses = [
            h.get("hypothesis", "").strip().lower() 
            for h in job.get("debug_history", [])[:-1] 
            if h.get("hypothesis")
        ]
        curr_hyp = (plan.get("hypothesis") or "").strip().lower()
        if curr_hyp and curr_hyp in prev_hypotheses:
            print(f"\n\033[1;93m⚠️  [Loop Notice] Debugger produced duplicate hypothesis already attempted in a previous iteration.\033[0m")
            print(f"\033[96m   👉 You may press Ctrl-C to abort and steer with [Q] Ask AI or supply guidance.\033[0m\n")
    except json.JSONDecodeError:
        print("!!! LLM response was not valid JSON. Check ai/logs.")
    
    job["updated_at"] = now_iso()
    write_json(job_path, job)
    
    # Move to implementation
    run_implement(job, job_path)

def run_implement(job: dict, job_path: Path) -> None:
    print(f"      - Implementing debug step...")
    run_shell(
        f'{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPTS_DIR / "worker_run.py"))} {shlex.quote(str(job_path))}',
        cwd=ROOT,
        check=True,
        capture=False,
        env={"AI_DEBUG_CHILD": "1"},
    )

def run_verify(job: dict, job_path: Path) -> None:
    print(f"      - Verification complete. Result recorded.")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_file", help="Path to the job JSON file")
    parser.add_argument("--logs", help="Path to runtime logs file")
    parser.add_argument("--feedback", help="Human feedback for this iteration")
    parser.add_argument("--max-iterations", type=int)
    args = parser.parse_args()

    run_debug_iteration(Path(args.job_file), args.logs, args.feedback, max_iterations=args.max_iterations)

if __name__ == "__main__":
    main()
