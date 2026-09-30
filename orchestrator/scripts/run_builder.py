#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from common import OUTPUT_DIR, PROMPTS_DIR, ROOT, read_json, write_json, write_text, print_phase, StatusBar, format_clarification_history, format_investigation_history
from llm import run_llm, extract_json_block
from model_router import ModelRole
from run_build_and_tests import run_build_and_tests
from reference_artifacts import reference_context
from orchestrator.project_config import PROJECT_CONFIG
from team_roles import format_team_context


class BuilderClarificationNeeded(RuntimeError):
    def __init__(self, question: str):
        self.question = question
        super().__init__(f"Builder clarification needed: {question}")


def format_work_package_context(package: dict | None) -> str:
    if not package:
        return ""
    budgets = package.get("budgets", {})
    lines = [
        "### BOUNDED WORK PACKAGE",
        f"Package: {package.get('id', 'main')}",
        f"Assigned role: {package.get('role', 'implementation_engineer')}",
        f"Objective: {package.get('objective', 'Complete the assigned work')}",
        f"Requested execution mode: {package.get('requested_mode', 'guided')}",
        f"Allowed scope: {', '.join(package.get('scopes', [])) or 'No safe parallel scope declared'}",
        f"Required tools: {', '.join(package.get('required_tools', [])) or 'None'}",
        "Acceptance criteria:",
        *[f"- {item}" for item in package.get("acceptance_criteria", [])],
        "Required tests:",
        *[f"- {item}" for item in package.get("tests", [])],
        f"Budget: {budgets.get('iterations', 4)} iterations, {budgets.get('minutes', 20)} minutes.",
        "You may not broaden scope, weaken acceptance criteria, change permissions, or delegate further.",
        "Return a structured handoff with work, evidence, files, tests, remaining risks, and next action.",
    ]
    return "\n".join(lines)


def validate_execution_handoff(
    payload: dict,
    package: dict,
    *,
    files_changed: list[str],
    tests_ok: bool,
) -> dict:
    claimed_status = str(payload.get("acceptance_criteria_status", "")).casefold()
    if not tests_ok and claimed_status in {"passed", "complete", "accepted"}:
        raise ValueError("Execution handoff cannot claim completion when validation failed")
    test_command = str(payload.get("test_command") or "").strip()
    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        evidence = [test_command] if test_command else []
    risks = payload.get("remaining_risks")
    if not isinstance(risks, list):
        risks = []
    return {
        "package_id": package.get("id", "main"),
        "role": package.get("role", "implementation_engineer"),
        "work_performed": str(payload.get("summary") or payload.get("action") or "Work package executed."),
        "evidence": [str(item) for item in evidence],
        "files_changed": list(files_changed),
        "tests": [test_command] if test_command else list(package.get("tests", [])),
        "acceptance_criteria_status": "passed" if tests_ok else "failed",
        "remaining_risks": [str(item) for item in risks],
        "next_action": str(payload.get("next_action") or ("Validate downstream integration." if tests_ok else "Retry or reroute the package.")),
    }


def active_work_package(job: dict) -> dict | None:
    packages = job.get("work_packages") or []
    active_id = job.get("active_work_package_id")
    if active_id:
        return next((item for item in packages if item.get("id") == active_id), None)
    return next((item for item in packages if item.get("status", "pending") in {"pending", "scheduled", "running"}), None)


def make_brief(job: dict) -> str:
    plan = job["plan"]
    verification = job.get("verification")
    references = reference_context(job)
    team_context = format_team_context(job.get("team", {}))
    if team_context:
        team_context = f"\n---\n{team_context}\n"
    
    clarification_text = ""
    history = job.get("clarification_history", [])
    if history:
        clarification_text = f"\n---\n{format_clarification_history(history)}\n"

    investigation_text = ""
    inv_history = format_investigation_history(job)
    if inv_history:
        investigation_text = f"\n---\n{inv_history}\n"

    verification_text = ""
    if verification:
        verification_text = f'''
---
## 🛡️ Senior Architect Review ({verification.get('verifier_model', 'Extreme Model')})
**Status**: {verification.get('status', 'unknown').upper()}
{verification.get('comments', '')}
'''
        if verification.get("suggested_additions"):
            verification_text += "\n**Suggested Additions**:\n" + "\n".join(f"- {x}" for x in verification["suggested_additions"])
        if verification.get("risks_identified"):
            verification_text += "\n**Risks Identified**:\n" + "\n".join(f"- {x}" for x in verification["risks_identified"])
        verification_text += "\n"

    if job["type"] in {"bug-fix", "bug-investigate"}:
        return f'''# Bug brief

Issue: #{job["issue_number"]}
Title: {job["title"]}

Summary:
{plan.get("summary", "No summary provided.")}
{clarification_text}
{investigation_text}
{verification_text}
Repro steps:
''' + "\n".join(f'- {x}' for x in plan.get("repro_steps", [])) + f'''

Expected behavior:
{plan.get("expected_behavior", "No expected behavior provided.")}

Acceptance criteria:
''' + "\n".join(f'- {x}' for x in plan.get("acceptance_criteria", [])) + f'''

Constraints:
''' + "\n".join(f'- {x}' for x in plan.get("constraints", [])) + f'''

Likely files:
''' + "\n".join(f'- {x}' for x in plan.get("likely_files", [])) + (f'''

Test recommendations (TDD):
''' + "\n".join(f'- {x}' for x in plan.get("test_recommendations", [])) if plan.get("test_recommendations") else "") + f"\n{references}\n{team_context}"
    elif job["type"] == "feature-plan":
        return f'''# Feature task brief

Issue: #{job["issue_number"]}
Title: {job["title"]}

Summary:
{plan.get("summary", "No summary provided.")}
{clarification_text}
{investigation_text}

Acceptance criteria:
''' + "\n".join(f'- {x}' for x in plan.get("acceptance_criteria", [])) + f'''

Likely files:
''' + "\n".join(f'- {x}' for x in plan.get("likely_files", [])) + f"\n{references}\n{team_context}"
    elif job["type"] == "quick-fix":
        return f'''# Quick task brief

Issue: #{job["issue_number"]}
Title: {job["title"]}

Instructions:
{plan.get("instructions", "No instructions provided.")}

Summary:
{plan.get("summary", "No summary provided.")}
{clarification_text}
{investigation_text}

Acceptance criteria:
''' + "\n".join(f'- {x}' for x in plan.get("acceptance_criteria", [])) + f'''
Likely files:
''' + "\n".join(f'- {x}' for x in plan.get("likely_files", [])) + f"\n{references}\n{team_context}"
    else:
        return f'''# Feature task brief

Issue: #{job["issue_number"]}
Title: {job["title"]}

Summary:
{plan.get("summary", "No summary provided.")}
{clarification_text}
{investigation_text}
{verification_text}
{references}
{team_context}
'''


def inspect_progress(job: dict) -> bool:
    """Returns True if work appears already done based on git/logs."""
    from common import get_repo_state, OUTPUT_DIR
    state = get_repo_state()
    plan = job.get("plan", {})
    likely_files = plan.get("likely_files", [])
    
    # Smart check: if any likely files are modified/untracked, AI probably started
    ai_started = any(f in state["modified"] or f in state["untracked"] for f in likely_files)
    
    # Check for success in test log
    brief_dir = OUTPUT_DIR / job["job_id"]
    test_log = brief_dir / "test.log"
    if test_log.exists():
        content = test_log.read_text()
        if "** TEST SUCCEEDED **" in content or "PASS" in content:
             if ai_started:
                 print("      - Artifact Forensics: Work and tests appear complete. Resuming success state.")
                 return True
    return False


def run_builder(job_path: Path, resume: bool = False) -> tuple[bool, bool, Path]:
    job = read_json(job_path)
    if "type" not in job:
        if "repro_steps" in job.get("plan", {}):
            job["type"] = "bug-fix"
        elif job.get("source") == "quick" or "quick" in job.get("job_id", ""):
            job["type"] = "quick-fix"
        else:
            job["type"] = "feature-plan"
            
    status_bar = StatusBar(job, is_processing=True)
    
    if resume and inspect_progress(job):
        summary_file = OUTPUT_DIR / job["job_id"] / "builder_summary.md"
        if summary_file.exists():
            return True, True, summary_file

    
    if job["type"] == "bug-investigate":
        print_phase("investigation")
    else:
        print_phase("implementation")
    
    status_bar.render()

    brief_dir = OUTPUT_DIR / job["job_id"]
    brief_dir.mkdir(parents=True, exist_ok=True)
    
    # Always ensure brief exists for review_ready.py
    brief_file = brief_dir / "brief.md"
    if not brief_file.exists():
        brief = make_brief(job)
        write_text(brief_file, brief)

    summary_file = brief_dir / "builder_summary.md"
    
    output = None
    actual_builder = job.get("actual_builder_used")

    if resume and summary_file.exists():
        print(f"      - Resuming: Found existing builder summary at {summary_file.name}")
        output = summary_file.read_text(encoding="utf-8")
        if not actual_builder:
            actual_builder = "unknown (resumed)"
    else:
        from generate_test_index import generate_test_index
        
        likely_files = job.get("plan", {}).get("likely_files", [])
        is_infra = any("orchestrator/" in f or "scripts/" in f for f in likely_files)
        
        stack = PROJECT_CONFIG.stack
        if is_infra:
            prompt_name = "builder_infra.md"
            test_index = generate_test_index(ROOT / "tests", likely_files=likely_files)
        else:
            prompt_name = "builder_bug.md" if job["type"] in {"bug-fix", "bug-investigate"} else "builder_feature_task.md"
            test_dir = ROOT / PROJECT_CONFIG.test_target if (PROJECT_CONFIG.test_target and (ROOT / PROJECT_CONFIG.test_target).exists()) else ROOT
            test_index = generate_test_index(test_dir, likely_files=likely_files)

        prompt_template = (PROMPTS_DIR / prompt_name).read_text(encoding="utf-8")
        brief = make_brief(job)

        stack_context = f"""
### PROJECT STACK CONTEXT
- Detected Stack: {stack.display_name}
- Primary Language: {stack.language}
- Build Command: {PROJECT_CONFIG.build_command or stack.build_command or 'None'}
- Test Command: {PROJECT_CONFIG.test_command or stack.test_command or 'None'}
"""

        debug_context = ""
        if job.get("status") == "debugging" and "debug_proposal" in job:
            prop = job["debug_proposal"]
            debug_context = f"""
### DEBUG CONTEXT (Iteration {job['iteration']})
You are in iterative debugging mode.
Hypothesis: {prop.get('hypothesis')}
Action: {prop.get('action')}
Implementation Plan: {prop.get('implementation_plan')}
Expected Signal: {prop.get('expected_signal')}

STRICT REQUIREMENT: You MUST follow the implementation plan above exactly. 
If the action is 'add_logging', ensure you follow the Logging Recipe (entry/exit, snapshots, IDs, timestamps).
"""

        yolo_context = ""
        if job.get("yolo"):
            yolo_context = """
### YOLO MODE ACTIVE
You are running in YOLO (You Only Live Once) mode. This means full automation is expected.
STRICT REQUIREMENT: Do NOT use the `clarification_needed` field unless it is physically impossible to proceed.
If you are unsure between two valid approaches, pick the safest, most conventional one, document your assumption in the summary, and proceed. DO NOT PAUSE for human confirmation.
"""

        full_prompt = f"""{prompt_template}

Grounding docs to inspect first:
- AGENTS.md
- docs/architecture.md
- docs/coding-standards.md
- docs/build-test-commands.md
{stack_context}
Available Tests (use for selecting test_command):
{test_index}
{debug_context}
{yolo_context}

Brief:
{brief}
{format_work_package_context(active_work_package(job))}
"""

        print_phase("agent_thinking")
        status_bar.render()
        package = active_work_package(job)
        execution_attempts = job.setdefault("execution_attempts", [])
        try:
            output, actual_builder, session_id = run_llm(
                job["builder"],
                full_prompt,
                cwd=ROOT,
                timeout=1200,
                allowed_models=job.get("allowed_models"),
                role=ModelRole.BUILDER,
                attempt_log=execution_attempts,
                required_execution_mode=package.get("requested_mode") if package else None,
            )
        finally:
            write_json(job_path, job)

        # Track session IDs in the job
        if "llm_sessions" not in job:
            job["llm_sessions"] = []
        job["llm_sessions"].append({"id": session_id, "model": actual_builder})
    
    print_phase("implementation")
    status_bar.render()
    
    from common import now_iso, write_json
    job["actual_builder_used"] = actual_builder
    job["updated_at"] = now_iso()

    import json
    parsed_output = {}
    try:
        json_output = extract_json_block(output)
        parsed_output = json.loads(json_output)
        
        # Check for clarification needed from builder
        if "clarification_needed" in parsed_output and parsed_output["clarification_needed"]:
            q = parsed_output["clarification_needed"]
            job["status"] = "human-needed"
            job["human_clarification_question"] = q
            write_json(job_path, job)
            print("\n" + "!"*60)
            print(f"\033[93mPAUSED: Builder needs clarification\033[0m")
            print(f"\033[1;96mQuestion:\033[0m {q}")
            print("!"*60 + "\n")
            raise BuilderClarificationNeeded(q)

        if "test_command" in parsed_output:
            job["test_command_override"] = parsed_output["test_command"]
            print(f"      - LLM selected specific tests: {job['test_command_override']}")

        if "summary" in parsed_output and parsed_output["summary"]:
            job["builder_summary"] = parsed_output["summary"]
        if "hypothesis" in parsed_output and parsed_output["hypothesis"]:
            job["builder_hypothesis"] = parsed_output["hypothesis"]
    except json.JSONDecodeError:
        if not resume: # Only warn if we just generated it
            print("      - Warning: LLM output was not valid JSON, using default test suite.")

    write_json(job_path, job)

    if not resume or not summary_file.exists():
        write_text(summary_file, output)

    build_ok, test_ok = run_build_and_tests(job, summary_file)
    
    # Return parsed output dictionary so callers can inspect what was built
    final_parsed = parsed_output if isinstance(parsed_output, dict) else {}
        
    return build_ok, test_ok, summary_file, final_parsed


if __name__ == "__main__":
    raise SystemExit("Use worker_run.py")
