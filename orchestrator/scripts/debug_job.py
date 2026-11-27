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
from cloud_logs import CloudLogsError, is_log_ref, resolve_log_ref

SCRIPTS_DIR = Path(__file__).resolve().parent

MAX_LOG_SOURCE_PATH_CHARS = 512
MAX_LOG_LINE_CHARS = 2_000
MAX_LOG_ENTRY_CHARS = 15_000
MAX_RUNTIME_LOG_CHARS = 100_000


from orchestrator.scope_check import INTERNAL_TOOL_PREFIXES, is_internal_tool_path as _is_internal_path


def build_iteration_ledger(job: dict[str, Any]) -> dict[str, Any]:
    """Compact structured memory of prior iterations so fix loops and fallback models never re-explore dead ends."""
    history = [h for h in (job.get("debug_history") or []) if isinstance(h, dict)]
    attempts = []
    ruled_out: list[str] = []
    for entry in history[-8:]:
        result = entry.get("result")
        failed = isinstance(result, dict) and (not result.get("build_ok", True) or not result.get("tests_ok", True))
        hyp = str(entry.get("hypothesis") or "").strip()
        if failed and hyp and hyp not in ruled_out:
            ruled_out.append(hyp)
        attempts.append({
            "iteration": entry.get("iteration"),
            "model": entry.get("model"),
            "action": entry.get("action"),
            "hypothesis": hyp,
            "result": result,
        })
    changed_files = sorted(
        f for f in set([*(job.get("ai_modified_files") or []), *(job.get("ai_untracked_files") or [])])
        if isinstance(f, str) and f.strip() and not _is_internal_path(f)
    )[:40]
    failures = []
    tv = job.get("test_validation") if isinstance(job.get("test_validation"), dict) else {}
    for item in (tv.get("failures") or [])[:15]:
        if isinstance(item, dict) and item.get("test"):
            msg = str(item.get("message") or "").strip().splitlines()[0][:160] if item.get("message") else ""
            failures.append(f"{item['test']}: {msg}" if msg else str(item["test"]))
    return {
        "iteration": int(job.get("iteration") or 0),
        "attempts": attempts,
        "ruled_out_hypotheses": ruled_out,
        "changed_files": changed_files,
        "failing_tests": failures,
    }


def format_iteration_ledger(job: dict[str, Any]) -> str:
    """Human/LLM-readable Iteration Ledger injected into debug prompts before raw history."""
    ledger = build_iteration_ledger(job)
    if not ledger["attempts"] and not ledger["failing_tests"] and not ledger["changed_files"]:
        return "No prior debug iterations."
    lines = ["### Iteration Ledger (Do NOT repeat ruled-out hypotheses)"]
    if ledger["failing_tests"]:
        lines.append("Failing tests:")
        for ft in ledger["failing_tests"]:
            lines.append(f"- {ft}")
    if ledger["changed_files"]:
        lines.append("Files already modified in this job: " + ", ".join(ledger["changed_files"][:20]))
    if ledger["ruled_out_hypotheses"]:
        lines.append("Ruled-out hypotheses (already tried and still failed):")
        for h in ledger["ruled_out_hypotheses"]:
            lines.append(f"- {h}")
    if ledger["attempts"]:
        lines.append("Recent attempts:")
        lines.append(json.dumps(ledger["attempts"], indent=2))
    return "\n".join(lines)


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


def run_debug_iteration(
    job_path: Path,
    logs: str | None = None,
    feedback: str | None = None,
    max_iterations: int | None = None,
    manual: bool = False,
) -> None:
    job = read_json(job_path)

    from orchestrator.job_scope import NONPRODUCTION_TYPES
    if job.get("type") in NONPRODUCTION_TYPES:
        # Design and Tests jobs have no debug loop: the builder rewrites its own files, and is told which of its tests failed and why
        # (and anything the person said), so a separate diagnosis step would only repeat what it reads for itself.
        if feedback:
            job["debug_feedback"] = feedback
        write_json(job_path, job)
        print_phase("implementation", subtext="rewriting the failing tests" if job.get("output_scope") == "tests-only" else "revising")
        run_implement(job, job_path)
        return
    
    # 1. State Machine Initialization
    if job.get("status") != "debugging":
        job["status"] = "debugging"
        job["debug_phase"] = "propose"
        job["iteration"] = 0
        job["debug_history"] = []
        job.pop("debug_convergence_reset_at", None)
        job.pop("debug_pause_reason", None)
        if "max_iterations" not in job:
            job["max_iterations"] = 8

    # 2. Handle Iteration Overrides & Explicit Human Resume/Steer
    human_steered = bool(manual or feedback or logs or max_iterations is not None)
    was_paused = job.get("debug_phase") == "paused"
    if max_iterations is not None:
        job["max_iterations"] = max_iterations
    if was_paused:
        job["debug_phase"] = "propose"
    if human_steered and (was_paused or feedback or logs):
        # An explicit Run fix from paused state or new human guidance starts a fresh convergence window
        # instead of immediately deadlocking on prior pre-guidance history.
        job["debug_convergence_reset_at"] = len(job.get("debug_history") or [])
        job.pop("debug_pause_reason", None)
    if was_paused or max_iterations is not None or (human_steered and (feedback or logs)):
        write_json(job_path, job)

    # 3. Check for Intelligent Convergence / Loop Termination
    test_summary = get_job_test_summary(job)
    convergence = analyze_debug_loop_convergence(job, test_summary)
    safety_ceiling = max_iterations if max_iterations is not None else 30

    if not (human_steered and was_paused) and (convergence["is_stuck"] or (job["iteration"] >= safety_ceiling and convergence["health"] != "converging")):
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

    if isinstance(job.get("blocker"), dict):
        job["last_blocker"] = job.pop("blocker")
        write_json(job_path, job)

    if os.environ.get("ORCHESTRATOR_SKIP_TOOL_PREFLIGHT") != "1" and job.get("type") not in ("design", "feature-design", "bug-investigate"):
        try:
            cfg_path = ROOT / ".orchestrator" / "project.json"
            if cfg_path.is_file():
                from orchestrator import blockers as _blockers, build_tools as _build_tools, manifest_deps as _manifest_deps
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    missing_tool = _blockers.missing_tool_blocker(_build_tools.missing_project_tools(cfg))
                    if not missing_tool:
                        unhydrated = _manifest_deps.unhydrated_manifests(ROOT, cfg)
                        if unhydrated:
                            names = ", ".join(m["name"] for m in unhydrated)
                            missing_tool = {
                                "kind": "tool",
                                "scope": "project",
                                "key": unhydrated[0]["tool"],
                                "title": "Project packages aren't installed yet",
                                "detail": f"Found lockfiles without local packages installed ({names}). Click Set up environment to install them.",
                            }
                    if missing_tool:
                        print(f"\n⚠️ {missing_tool['title']}. {missing_tool['detail']} Stopping before calling the AI so you can set it up or run on another computer.")
                        _blockers.record(job, missing_tool)
                        job["status"] = "human-needed"
                        job["last_error"] = f"{missing_tool['title']}. {missing_tool['detail']}"
                        job["updated_at"] = now_iso()
                        write_json(job_path, job)
                        return
        except Exception:
            pass

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
    from orchestrator import prd
    prompt_template = f"{prompt_template}\n{prd.context_block(ROOT, 'builder')}"
    
    # Ingest context
    brief_file = OUTPUT_DIR / job["job_id"] / "brief.md"
    brief = brief_file.read_text(encoding="utf-8") if brief_file.exists() else "No brief available"
    brief += reference_context(job)
    # The fix loop's goal is the job's criteria, not green tests: a requirement added after the brief (feedback) must stay in view.
    from orchestrator import acceptance
    brief += "\n" + acceptance.block(job)
    
    prev_impl = ""
    builder_summary = OUTPUT_DIR / job["job_id"] / "builder_summary.md"
    if builder_summary.exists():
        prev_impl = builder_summary.read_text(encoding="utf-8")
    runtime_logs = ""
    
    # 1. Build & Test Auto-Discovery
    auto_logs = []
    job_out_dir = OUTPUT_DIR / job["job_id"]
    
    # Only include build log if it contains actual build failure output (skip "** BUILD SUCCEEDED **" marker)
    build_log = job_out_dir / "build.log"
    if build_log.exists():
        try:
            b_text = build_log.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            b_text = ""
        if b_text and not b_text.startswith("** BUILD SUCCEEDED"):
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

            if is_log_ref(lp):
                # `cloud:latest` / `cloud:<session>`: pull fresh device logs each iteration.
                try:
                    lp = resolve_log_ref(lp)
                    print(f"        [cloud] pulled -> {lp}")
                except CloudLogsError as exc:
                    print(f"        [skip] cloud logs unavailable: {exc}")
                    continue
            elif not looks_like_path(lp):
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

    ledger = build_iteration_ledger(job)
    job["iteration_ledger"] = ledger
    full_prompt = prompt_template.replace("{{brief}}", brief)
    full_prompt = full_prompt.replace("{{previous_implementation}}", prev_impl)
    full_prompt = full_prompt.replace("{{debug_history}}", format_iteration_ledger(job))
    full_prompt = full_prompt.replace("{{runtime_logs}}", runtime_logs)
    full_prompt = full_prompt.replace("{{human_feedback}}", feedback or "None")

    write_text(iter_dir / "prompt.md", full_prompt)

    # Call LLM
    print_phase("agent_thinking", subtext="proposing debug fix")
    try:
        raw_output, actual_model, session_id = run_llm(
            job["reviewer"], 
            full_prompt, 
            cwd=ROOT, 
            timeout=1200, 
            allowed_models=job.get("allowed_models"),
            role=ModelRole.DEBUGGER
        )
    except Exception as exc:
        # Don't burn an iteration when the model/tool couldn't even run (expired sign-in, quota, env blocker).
        # Record a structured blocker so the UI offers 1-click sign-in + auto-retry and the server auto-resumes.
        job["iteration"] = max(0, int(job.get("iteration") or 1) - 1)
        job["last_error"] = str(exc)
        job["updated_at"] = now_iso()
        from orchestrator import blockers
        if blocker := blockers.detect(f"{exc}\n{getattr(exc, 'stderr', '') or ''}"):
            blockers.record(job, blocker)
            job["status"] = "human-needed"
        write_json(job_path, job)
        raise

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
        if isinstance(plan.get("targeted_tests"), list):
            job["targeted_tests"] = [str(t).strip() for t in plan["targeted_tests"] if str(t).strip()][:10]

        if str(plan.get("action") or "").strip().lower() == "ask_human":
            question = str(plan.get("clarification_question") or plan.get("summary") or plan.get("hypothesis") or "").strip()
            if question:
                job.setdefault("debug_history", []).append({
                    "iteration": job["iteration"],
                    "model": actual_model,
                    "hypothesis": plan.get("hypothesis"),
                    "action": "ask_human",
                    "result": {
                        "build_ok": False,
                        "tests_ok": False,
                        "aborted": True,
                        "notes": "Asked human for clarification",
                    },
                })
                job["status"] = "human-needed"
                job["human_clarification_question"] = question
                job["clarification_asker"] = "debugger"
                job["clarification_asked_at"] = now_iso()
                if os.environ.get("ORCHESTRATOR_RUN_ID"):
                    job["clarification_run_id"] = os.environ["ORCHESTRATOR_RUN_ID"]
                job["last_error"] = f"Debugger clarification needed: {question[:200]}"
                job["debug_phase"] = "paused"
                job["debug_pause_reason"] = "Waiting for your answer"
                job["updated_at"] = now_iso()
                write_json(job_path, job)
                print(f"\n\033[1;93m❓ Debugger needs your input:\033[0m {question}")
                try:
                    notify_body = f"Debugger needs your input on {job.get('title', '')}:\n{question}"
                    run_shell(
                        f'{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPTS_DIR / "notify.py"))} '
                        f'"Question for you" {shlex.quote(notify_body)} {shlex.quote(str(job.get("job_id") or ""))}',
                        cwd=ROOT,
                        check=False,
                    )
                except Exception:
                    pass
                return

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
    try:
        run_shell(
            f'{shlex.quote(sys.executable)} {shlex.quote(str(SCRIPTS_DIR / "worker_run.py"))} {shlex.quote(str(job_path))}',
            cwd=ROOT,
            check=True,
            capture=False,
            env={"AI_DEBUG_CHILD": "1"},
        )
    except Exception as exc:
        # Don't leave a dangling `result: "pending"` entry when worker_run aborts early (e.g. DiskFullError / quota).
        try:
            fresh = read_json(job_path)
            hist = fresh.get("debug_history")
            if isinstance(hist, list) and hist and isinstance(hist[-1], dict) and hist[-1].get("result") == "pending":
                hist[-1]["result"] = {
                    "build_ok": False,
                    "tests_ok": False,
                    "aborted": True,
                    "notes": f"Implementation aborted before completing: {str(exc)[:200]}",
                }
                fresh["updated_at"] = now_iso()
                write_json(job_path, fresh)
        except Exception:
            pass
        raise

def run_verify(job: dict, job_path: Path) -> None:
    print(f"      - Verification complete. Result recorded.")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_file", help="Path to the job JSON file")
    parser.add_argument("--logs", help="Path to runtime logs file")
    parser.add_argument("--feedback", help="Human feedback for this iteration")
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--manual", action="store_true", help="Explicit user-initiated fix run (resets paused loop gate)")
    args = parser.parse_args()

    run_debug_iteration(Path(args.job_file), args.logs, args.feedback, max_iterations=args.max_iterations, manual=args.manual)

if __name__ == "__main__":
    main()
