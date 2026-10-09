#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from pathlib import Path

from common import (
    interactive,
    ROOT,
    PROMPTS_DIR,
    gh_text,
    make_job_paths,
    now_iso,
    read_json,
    prompt_multiline,
    prompt_radio,
    print_phase,
    timestamp,
    write_json,
    write_text,
    OUTPUT_DIR,
    prompt_checkbox,
    prompt_confirm,
    flush_stdin,
    StatusBar,
)
from llm import run_llm, SUPPORTED_MODELS, DEFAULT_FALLBACKS, extract_json_block
from model_router import ModelRole, get_prioritized_models
from model_registry import get_model, ModelTier
from probe_machine import load_machines
from manual_run import capture_logs
import test_cases as tc
from orchestrator.project_config import PROJECT_CONFIG
from orchestrator import prd
from orchestrator import slice_check

SCRIPTS_DIR = Path(__file__).resolve().parent

DEFAULTS = {
    "bug": {"planner": "gemini", "builder": "codex", "reviewer": "gemini"},
    "feature": {"planner": "gemini", "builder": "codex", "reviewer": "gemini"},
    "coverage": {"planner": "gemini", "builder": "codex", "reviewer": "gemini"},
    "quick": {"planner": "gemini-3-flash-preview", "builder": "codex", "reviewer": "gemini-3.1-flash-lite-preview"},
}

PRESETS = {
    "balanced": {"planner": "gemini", "builder": "codex", "reviewer": "gemini"},
    "hard-bug": {"planner": "gemini", "builder": "claude-opus-4-7", "reviewer": "gemini"},
    "architecture": {"planner": "claude-opus-4-7", "builder": "claude-opus-4-7", "reviewer": "gemini"},
    "fast": {"planner": "gemini-3-flash-preview", "builder": "codex", "reviewer": "gemini-3.1-flash-lite-preview"},
    "free": {"planner": "copilot", "builder": "qwen2.5-coder", "reviewer": "deepseek"},
}

BRANCH_MODE_CHOICES = ["new", "current", "manual"]
VERIFICATION_STATUSES = {"approved", "rejected", "concerns"}


def normalize_branch_mode(branch_mode: str | None) -> str:
    if not branch_mode:
        return "new"
    if "current" in branch_mode:
        return "current"
    if "manual" in branch_mode:
        return "manual"
    return branch_mode


def create_issue(title: str, body: str, labels: list[str]) -> int:
    import subprocess
    labels = list(labels)
    while True:
        try:
            label_flags = []
            for label in labels:
                label_flags.extend(["--label", label])
            out = gh_text(
                "issue",
                "create",
                "--title",
                title,
                "--body",
                body,
                *label_flags,
            )
            issue_number = int(out.rstrip("/").split("/")[-1])
            return issue_number
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            match = re.search(r"could not add label:\s*'([^']+)'\s*not found", stderr)
            if match and labels:
                missing_label = match.group(1)
                print(f"⚠️  could not add label: '{missing_label}' not found on repository. Retrying without this label...")
                if missing_label in labels:
                    labels.remove(missing_label)
                    continue
            raise



def file_job(title: str, body: str, labels: list[str]) -> tuple[int | None, str]:
    """Record a new job where the project tracks work: (issue number, the reference that names the job).

    With GitHub (`code_host`), the job is a GitHub issue and its number names the job, its branch and its test cases.
    With plain git there is no issue: a short random reference names them instead."""
    if PROJECT_CONFIG.uses_github:
        print(f"      - Creating GitHub issue: {title}...", flush=True)
        number = create_issue(title, body, labels)
        return number, str(number)
    return None, secrets.token_hex(3)


def _list_items(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(x) for x in val if x is not None]
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []


def normalize_verification(payload: Any, verifier_model: str) -> dict | None:
    if not isinstance(payload, dict):
        if isinstance(payload, list) and len(payload) > 0 and isinstance(payload[0], dict):
            payload = payload[0]
        else:
            return None
    status = str(payload.get("status", "")).lower()
    comments = payload.get("comments")
    if status not in VERIFICATION_STATUSES or not isinstance(comments, str):
        return None
    return {
        "status": status,
        "comments": comments,
        "suggested_additions": payload.get("suggested_additions") or [],
        "risks_identified": payload.get("risks_identified") or [],
        "verifier_model": verifier_model,
    }


def normalize_plan_dict(
    plan: Any,
    default_title: str = "",
    default_summary: str = "",
    job_type: str = "",
) -> dict:
    """Ensures plan is a dictionary even if the model returned an array or wrapper."""
    if not default_title:
        if job_type == "coverage":
            default_title = "[Tests] Expand test coverage"
        elif job_type == "bug":
            default_title = "[Bug] Fix issue"
        elif job_type == "design":
            default_title = "[Design] New design"
        elif job_type:
            default_title = f"[{job_type.capitalize()}] New {job_type}"
        else:
            default_title = "New Job"

    if isinstance(plan, list):
        # 1. Check if an item in the list is the plan dictionary
        plan_dict = next(
            (item for item in plan if isinstance(item, dict) and any(k in item for k in (
                "title", "summary", "audit_goals", "tasks", "target_subsystems", "complexity", "test_cases"
            ))),
            None,
        )
        if plan_dict:
            plan = plan_dict
        elif len(plan) == 1 and isinstance(plan[0], dict):
            plan = plan[0]
        elif all(isinstance(x, dict) for x in plan):
            if any("task_id" in x or "description" in x for x in plan):
                plan = {
                    "title": default_title,
                    "summary": default_summary or "Tasks",
                    "tasks": plan,
                    "recommended_job_type": "test-audit" if job_type == "coverage" else "feature-plan",
                }
            elif any("covers" in x or "expected" in x for x in plan):
                plan = {
                    "title": default_title,
                    "summary": default_summary or "Expand test coverage",
                    "test_cases": plan,
                    "audit_goals": [x.get("title", "") for x in plan if x.get("title")],
                    "recommended_job_type": "test-audit",
                }
            else:
                plan = {
                    "title": default_title,
                    "summary": default_summary or "Generated plan",
                    "tasks": plan,
                    "recommended_job_type": "test-audit" if job_type == "coverage" else "feature-plan",
                }
        elif all(isinstance(x, str) for x in plan):
            plan = {
                "title": default_title,
                "summary": "\n".join(plan),
                "audit_goals": plan,
                "recommended_job_type": "test-audit" if job_type == "coverage" else "feature-plan",
            }
        else:
            plan = {
                "title": default_title,
                "summary": str(plan),
                "recommended_job_type": "test-audit" if job_type == "coverage" else "feature-plan",
            }
    elif not isinstance(plan, dict):
        plan = {
            "title": default_title,
            "summary": str(plan),
            "recommended_job_type": "test-audit" if job_type == "coverage" else "feature-plan",
        }

    if not plan.get("title"):
        plan["title"] = default_title
    if default_summary and not plan.get("summary"):
        plan["summary"] = default_summary
    if job_type == "coverage" and not plan.get("recommended_job_type"):
        plan["recommended_job_type"] = "test-audit"
    return plan


def issue_payload_for_job(
    requested_job_type: str,
    plan: dict,
    verification: dict | None,
    clarification: str | None,
    stitch: bool,
    yolo: bool = False,
) -> tuple[str, str, list[str], str, str]:
    if requested_job_type == "bug":
        title = plan.get("title") or "[Bug] Fix issue"
        body = build_bug_issue_body(plan, verification, yolo)
        labels = ["job:bug", "source:manual", "status:planned"]
        if clarification:
            labels = ["job:bug", "source:manual", "status:human-needed"]
        return title, body, labels, "bug", plan.get("recommended_job_type") or "bug-fix"

    if requested_job_type == "coverage":
        title = plan.get("title") or "[Tests] Expand test coverage"
        body = build_coverage_issue_body(plan, verification, yolo)
        labels = ["job:coverage", "status:planned"]
        if clarification:
            labels = ["job:coverage", "status:human-needed"]
        return title, body, labels, "coverage", plan.get("recommended_job_type") or "test-audit"

    if requested_job_type == "design" or stitch:
        title = plan.get("title") or "[Design] New design"
        body = build_design_issue_body(plan, verification, yolo)
        labels = ["job:design", "status:designing"]
        if clarification:
            labels = ["job:design", "status:human-needed"]
        return title, body, labels, "design", plan.get("recommended_job_type") or "feature-design"

    title = plan.get("title") or "[Feature] New feature"
    body = build_feature_issue_body(plan, verification, yolo)
    labels = ["job:feature", "status:planned"]
    if clarification:
        labels = ["job:feature", "status:human-needed"]
    return title, body, labels, "feature", plan.get("recommended_job_type") or "feature-plan"


def build_bug_issue_body(plan: dict, verification: dict | None = None, yolo: bool = False) -> str:
    yolo_status = "✅ ON (Automated)" if yolo else "❌ OFF (Manual Step Mode)"
    lines = [
        f"## Summary\n{plan.get('summary', '')}",
        "",
        "## Metadata",
        f"- **YOLO Mode**: {yolo_status}",
        f"- **Complexity**: {plan.get('complexity', 'unknown')}",
        f"- **Recommended Job Type**: {plan.get('recommended_job_type', 'bug-fix')}",
        "",
        "## Repro Steps",
        *[f"- {x}" for x in _list_items(plan.get("repro_steps"))],
        "",
        f"## Expected Behavior\n{plan.get('expected_behavior', '')}",
        "",
        "## Acceptance Criteria",
        *[f"- {x}" for x in _list_items(plan.get("acceptance_criteria"))],
        "",
        "## Constraints",
        *[f"- {x}" for x in _list_items(plan.get("constraints"))],
        "",
        "## Likely Files",
        *[f"- {x}" for x in _list_items(plan.get("likely_files"))],
        "",
        "## Test Recommendations",
        *[f"- {x}" for x in _list_items(plan.get("test_recommendations"))],
    ]

    if verification:
        lines.extend([
            "",
            "---",
            f"## 🛡️ Senior Architect Review ({verification['verifier_model']})",
            f"**Status**: {verification['status'].upper()}",
            f"\n{verification['comments']}",
        ])
        if verification.get("suggested_additions"):
            lines.extend(["", "**Suggested Additions**:", *[f"- {x}" for x in verification["suggested_additions"]]])
        if verification.get("risks_identified"):
            lines.extend(["", "**Risks Identified**:", *[f"- {x}" for x in verification["risks_identified"]]])

    return "\n".join(lines)


def build_feature_issue_body(plan: dict, verification: dict | None = None, yolo: bool = False) -> str:
    yolo_status = "✅ ON (Automated)" if yolo else "❌ OFF (Manual Step Mode)"
    lines = [
        f"## Summary\n{plan.get('summary', '')}",
        "",
        "## Metadata",
        f"- **YOLO Mode**: {yolo_status}",
        "",
        "## Assumptions",
        *[f"- {x}" for x in _list_items(plan.get("assumptions"))],
        "",
        "## Constraints",
        *[f"- {x}" for x in _list_items(plan.get("constraints"))],
        "",
        "## Risks",
        *[f"- {x}" for x in _list_items(plan.get("risks"))],
        "",
        "## Task Breakdown",
    ]
    tasks = plan.get("tasks")
    if isinstance(tasks, list):
        for i, task in enumerate(tasks, start=1):
            if isinstance(task, dict):
                lines.extend([
                    f"",
                    f"### Task {i}: {task.get('title', f'Task {i}')}",
                    str(task.get("description", "")),
                    "",
                    "Acceptance Criteria:",
                    *[f"- {x}" for x in _list_items(task.get("acceptance_criteria"))],
                    "",
                    "Likely Files:",
                    *[f"- {x}" for x in _list_items(task.get("likely_files"))],
                    "",
                    "Tests:",
                    *[f"- {x}" for x in _list_items(task.get("tests"))],
                    "",
                    f"Complexity: {task.get('complexity', 'medium')}",
                ])
            else:
                lines.extend([f"", f"### Task {i}: {task}"])
        
    if verification:
        lines.extend([
            "",
            "---",
            f"## 🛡️ Senior Architect Review ({verification['verifier_model']})",
            f"**Status**: {verification['status'].upper()}",
            f"\n{verification['comments']}",
        ])
        if verification.get("suggested_additions"):
            lines.extend(["", "**Suggested Additions**:", *[f"- {x}" for x in verification["suggested_additions"]]])
        if verification.get("risks_identified"):
            lines.extend(["", "**Risks Identified**:", *[f"- {x}" for x in verification["risks_identified"]]])

    return "\n".join(lines)


def build_coverage_issue_body(plan: dict, verification: dict | None = None, yolo: bool = False) -> str:
    yolo_status = "✅ ON (Automated)" if yolo else "❌ OFF (Manual Step Mode)"
    lines = [
        f"## Summary\n{plan.get('summary', '')}",
        "",
        "## Metadata",
        f"- **YOLO Mode**: {yolo_status}",
        f"- **Complexity**: {plan.get('complexity', 'simple')}",
        "",
        "## Audit Goals",
        *[f"- {x}" for x in _list_items(plan.get("audit_goals"))],
        "",
        "## Target Subsystems",
        *[f"- {x}" for x in _list_items(plan.get("target_subsystems"))],
        "",
        "## Expected Test Files",
        *[f"- {x}" for x in _list_items(plan.get("expected_test_files"))],
        "",
        "## Acceptance Criteria",
        *[f"- {x}" for x in _list_items(plan.get("acceptance_criteria"))],
        "",
        "## Likely Files",
        *[f"- {x}" for x in _list_items(plan.get('likely_files'))],
    ]

    if verification:
        lines.extend([
            "",
            "---",
            f"## 🛡️ Senior Architect Review ({verification['verifier_model']})",
            f"**Status**: {verification['status'].upper()}",
            f"\n{verification['comments']}",
        ])
        if verification.get("suggested_additions"):
            lines.extend(["", "**Suggested Additions**:", *[f"- {x}" for x in verification["suggested_additions"]]])
        if verification.get("risks_identified"):
            lines.extend(["", "**Risks Identified**:", *[f"- {x}" for x in verification["risks_identified"]]])

    return "\n".join(lines)


def build_design_issue_body(plan: dict, verification: dict | None = None, yolo: bool = False) -> str:
    yolo_status = "✅ ON (Automated)" if yolo else "❌ OFF (Manual Step Mode)"
    lines = [
        f"## Summary\n{plan.get('summary', '')}",
        "",
        "## Metadata",
        f"- **YOLO Mode**: {yolo_status}",
        f"- **Aesthetic Vibe**: {plan.get('vibe', 'modern')}",
        "",
        "## Design System",
        *[f"- {x}" for x in _list_items(plan.get("design_system"))],
        "",
        "## Visual Components",
        *[f"- {x}" for x in _list_items(plan.get("visual_components"))],
        "",
        "## Interaction Flows",
        *[f"- {x}" for x in _list_items(plan.get("interaction_flows"))],
        "",
        "## Acceptance Criteria (UI/UX)",
        *[f"- {x}" for x in _list_items(plan.get("acceptance_criteria"))],
        "",
        "## Implementation Notes",
        *[f"- {x}" for x in _list_items(plan.get('implementation_notes'))],
    ]

    if verification:
        lines.extend([
            "",
            "---",
            f"## 🛡️ Senior Architect Review ({verification['verifier_model']})",
            f"**Status**: {verification['status'].upper()}",
            f"\n{verification['comments']}",
        ])

    return "\n".join(lines)


def build_quick_issue_body(plan: dict, yolo: bool = False) -> str:
    yolo_status = "✅ ON (Automated)" if yolo else "❌ OFF (Manual Step Mode)"
    return f"## Summary\n{plan['summary']}\n\n## Metadata\n- **YOLO Mode**: {yolo_status}\n\n## Instructions\n{plan['instructions']}"


def main(args_override: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_type", choices=["bug", "feature", "design", "coverage", "quick"])
    parser.add_argument("--planner")
    parser.add_argument("--builder")
    parser.add_argument("--reviewer")
    parser.add_argument("--preset", choices=PRESETS.keys())
    parser.add_argument("--no-dispatch", action="store_true")
    parser.add_argument("--branch-mode", choices=BRANCH_MODE_CHOICES + ["current", "manual"])
    parser.add_argument("--branch", help="Manual branch name override")
    parser.add_argument("--allowed-models", help="Comma-separated list of allowed models")
    parser.add_argument("--allowed-machines", help="Comma-separated list of allowed machines")
    parser.add_argument("--yolo", action="store_true", help="Automate post-implementation steps (e.g. distribution)")
    parser.add_argument("--spec-file", help="Path to a file containing the feature/bug/coverage spec")
    parser.add_argument("--stitch", action="store_true", help="Enable Google Stitch AI design phase")
    parser.add_argument("--feedback", help="User feedback for design or plan revision")
    parser.add_argument("--update", help="Path to an existing job JSON to update/re-plan")
    parser.add_argument("--free", action="store_true", help="Restrict allowed models to free models only (cost_factor == 0.0)")
    parser.add_argument("--feature", help="Put the new job under this feature (its id)")
    # Set by "Build the plan" (orchestrator/plan_run.py): the run this job belongs to.
    parser.add_argument("--plan-run", help=argparse.SUPPRESS)
    parser.add_argument("--summary", help="Summary or area of focus for the job (skips interactive input prompt if provided)")
    parser.add_argument("--skip-verify", action="store_true", help="Skip the architect's plan verification (used when a person has already accepted the architect's suggestions)")
    args = parser.parse_args(args_override)

    existing_job = None
    if args.update:
        job_path = Path(args.update)
        if job_path.exists():
            existing_job = read_json(job_path)
            print(f"      - Updating existing job: {existing_job.get('job_id')}")

    # Define defaults for 'quick' job
    job_type_for_defaults = args.job_type
    if job_type_for_defaults == "design": job_type_for_defaults = "feature"
    defaults = PRESETS[args.preset] if args.preset else (PRESETS["free"] if args.free else DEFAULTS[job_type_for_defaults])
    # A re-plan keeps the job's own choices; only what is passed explicitly changes them.
    # (Otherwise the first re-plan resets a free-only or restricted job to the paid defaults.)
    saved = existing_job or {}
    planner = args.planner or saved.get("planner") or defaults["planner"]
    builder = args.builder or saved.get("builder") or defaults["builder"]
    reviewer = args.reviewer or saved.get("reviewer") or defaults["reviewer"]

    # Load spec from file or URL if provided
    file_spec_content = ""
    if args.spec_file:
        spec_str = args.spec_file.strip("'\"")
        if spec_str.startswith(("http://", "https://")):
            print(f"      - Fetching spec from URL: {spec_str}")
            try:
                import urllib.request
                import ssl
                # Use unverified context to fix macOS certificate issues
                ctx = ssl._create_unverified_context()
                # Add User-Agent to avoid being blocked by some sites (like ChatGPT share links)
                req = urllib.request.Request(spec_str, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
                    file_spec_content = response.read().decode("utf-8")
                print(f"      - Successfully fetched {len(file_spec_content)} characters.")
            except Exception as e:
                print(f"⚠️  Failed to fetch URL: {e}")
        else:
            spec_path = Path(spec_str)
            if not spec_path.is_absolute():
                spec_path = ROOT / spec_path
            if spec_path.exists():
                print(f"      - Loading spec from file: {spec_path}")
                file_spec_content = spec_path.read_text(encoding="utf-8")
            else:
                print(f"⚠️  Spec file not found: {spec_path}")

    # Model Selection
    all_models = sorted(list(SUPPORTED_MODELS))
    if args.free:
        from model_registry import get_free_models
        free_models = [m.id for m in get_free_models()]
        if free_models:
            allowed_models = free_models
            print(f"      - Free-models-only mode active: Restricted to {len(allowed_models)} free models: {', '.join(allowed_models)}")
        else:
            allowed_models = args.allowed_models.split(",") if args.allowed_models else DEFAULT_FALLBACKS
    elif args.allowed_models:
        allowed_models = args.allowed_models.split(",")
    elif saved.get("allowed_models"):
        allowed_models = list(saved["allowed_models"])
    else:
        footer = (
            "[\033[1;96mA\033[0m] Select All  [\033[1;92mF\033[0m] Free Only  [\033[1;93mN\033[0m] Deselect All  [\033[1;91mB\033[0m] Back"
        )
        allowed_models = prompt_checkbox("select models", all_models, DEFAULT_FALLBACKS, footer=footer)
    
    if not allowed_models:
        print("\n\033[1;91m⚠️  ERROR: No AI models selected.\033[0m")
        print("A job requires at least one LLM to perform planning and code generation.")
        print("\033[90m(Run with --allowed-models <ids> or select models in the interactive prompt.)\033[0m")
        sys.exit(1)
        
    # Ensure initial choices are within allowed set (resolve aliases first)
    p_meta = get_model(planner)
    b_meta = get_model(builder)
    r_meta = get_model(reviewer)
    
    planner = p_meta.id if p_meta else planner
    builder = b_meta.id if b_meta else builder
    reviewer = r_meta.id if r_meta else reviewer

    if planner not in allowed_models:
        best_planners = get_prioritized_models(role=ModelRole.PLANNER, allowed_models=allowed_models)
        planner = best_planners[0] if best_planners else allowed_models[0]
    if builder not in allowed_models:
        best_builders = get_prioritized_models(role=ModelRole.BUILDER, allowed_models=allowed_models)
        builder = best_builders[0] if best_builders else allowed_models[0]
    if reviewer not in allowed_models:
        best_reviewers = get_prioritized_models(role=ModelRole.REVIEWER, allowed_models=allowed_models)
        reviewer = best_reviewers[0] if best_reviewers else allowed_models[0]

    # Machine Selection
    machines_config = load_machines()
    if not machines_config:
        print("\n\033[1;91m⚠️  ERROR: No worker machines configured or enabled.\033[0m")
        print("A job requires at least one worker machine to execute build and test tasks.")
        print("\033[90m(Configure machines in .orchestrator/config/machines.json or use the wizard.)\033[0m")
        sys.exit(1)

    machine_names = sorted([m["name"] for m in machines_config])
    if args.allowed_machines:
        allowed_machines = args.allowed_machines.split(",")
    elif saved.get("allowed_machines"):
        allowed_machines = list(saved["allowed_machines"])
    else:
        print("\nMachine Selection (restrict workflow to these computers):")
        initial_machines = [m["name"] for m in machines_config if m.get("enabled", True)]
        allowed_machines = prompt_checkbox("Select allowed machines:", machine_names, initial_machines)
    
    if not allowed_machines:
        print("\n\033[1;91m⚠️  ERROR: No machines selected.\033[0m")
        print("A job requires at least one machine to be selected for the fleet.")
        print("\033[90m(Run with --allowed-machines <names> or select machines in the interactive prompt.)\033[0m")
        sys.exit(1)

    branch_mode = args.branch_mode
    if not branch_mode:
        if args.job_type == "coverage":
            branch_mode = "current"
        else:
            labels = [
                "new (creates a new branch automatically)",
                "current (use existing branch + git pull)",
                "manual (no git actions; skip checkout/pull)"
            ]
            branch_mode = prompt_radio("Branch selection:", labels, labels[0])
    branch_mode = normalize_branch_mode(branch_mode)

    selected_branch = args.branch
    if branch_mode == "current" and not selected_branch:
        import subprocess
        selected_branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ROOT)).decode("utf-8").strip()
        print(f"      - Using current branch: {selected_branch}")
        
    if branch_mode == "current" and selected_branch:
        try:
            import subprocess
            # Check if upstream exists and how many commits we are ahead
            upstream_res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "@{u}"],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False
            )
            if upstream_res.returncode == 0:
                upstream = upstream_res.stdout.strip()
                ahead_count = int(subprocess.check_output(
                    ["git", "rev-list", "--count", f"{upstream}..{selected_branch}"],
                    cwd=str(ROOT)
                ).decode("utf-8").strip())
                if ahead_count > 10:
                    print(f"\n\033[1;93m⚠️  WARNING: Your local branch '{selected_branch}' is ahead of '{upstream}' by {ahead_count} commits.\033[0m")
                    print("   If you proceed, your Pull Request will include all of these commits and files,")
                    print("   which may cause the GitHub PR diff to exceed limits (300+ files) or clutter the review.")
                    print("   Consider rebasing/merging first, or using 'new' branch mode.")
        except Exception:
            pass
    elif branch_mode == "manual":
        selected_branch = None
        print("      - No branch will be assigned (Builder will skip Git operations).")
    elif branch_mode == "new":
        # Keep it None, the scheduler/builder will create one based on the job ID
        selected_branch = None
        print("      - A new branch will be created automatically.")

    print("\n" + "="*40)
    print(f"   CREATING NEW {args.job_type.upper()}")
    print("="*40)

    title_input = ""
    raw_input_text = ""
    extra_input = ""
    
    if args.job_type == "quick":
        print("\n" + "="*40)
        print(f"   CREATING QUICK PROMPT JOB")
        print("="*40)
        
        if args.summary:
            instructions = args.summary
        else:
            flush_stdin()
            instructions = prompt_multiline("What would you like to change or improve?")
        if not instructions.strip():
            raise ValueError("No input provided.")
            
        summary = instructions.split("\n")[0][:60]
        if len(instructions.split("\n")[0]) > 60: summary += "..."
        
        plan = {
            "title": f"Quick: {summary}",
            "summary": summary,
            "instructions": instructions,
            "likely_files": [],
            "acceptance_criteria": ["Implementation matches provided instructions."]
        }
        
        title = plan["title"]
        body = build_quick_issue_body(plan, yolo=args.yolo)
        labels = ["job:quick", "status:planned"]
        
        # The instruction is the plan: no planner, no verification. The job and its one issue are created below.
        job_id_kind = "quick"
        actual_planner = "user-prompt"
        verification = None
        job_type = "quick-fix"
        raw_input_text = instructions
        clarification = None
        clarification_history = []
        llm_sessions = []
        last_manual_logs = []
        needs_test_cases = False

    else:
        if file_spec_content:
            print("\nSpec loaded successfully.")
            if args.summary:
                raw_input_text = f"{file_spec_content}\n\n### ADDITIONAL CONTEXT / OVERRIDES ###\n{args.summary}"
            else:
                flush_stdin()
                additional = prompt_multiline("Additional Context / Overrides (optional):")
                if additional.strip():
                    raw_input_text = f"{file_spec_content}\n\n### ADDITIONAL CONTEXT / OVERRIDES ###\n{additional}"
                else:
                    raw_input_text = file_spec_content
        elif args.summary:
            if args.job_type == "bug":
                raw_input_text = f"SUMMARY: {args.summary}\n\nREPRO STEPS:\n\nEXPECTED BEHAVIOR:"
            elif args.job_type == "coverage":
                raw_input_text = f"COVERAGE FOCUS: {args.summary}\n\nSUBSYSTEMS: {args.summary}"
            elif args.stitch or args.job_type == "design":
                raw_input_text = f"DESIGN VISION: {args.summary}\nPREFERRED VIBE: minimalist"
            else:
                raw_input_text = f"VISION: {args.summary}"
        else:
            flush_stdin()
            if args.job_type == "bug":
                summary = prompt_multiline("Summary / Area Of Focus:")
                repro = prompt_multiline("Repro Steps (one per line, optional):")
                expected = prompt_multiline("Expected Behavior (optional):")
                raw_input_text = f"SUMMARY: {summary}\n\nREPRO STEPS:\n{repro}\n\nEXPECTED BEHAVIOR:\n{expected}"
            elif args.job_type == "coverage":
                summary = prompt_multiline("Summary / Area Of Focus:")
                subsystems = prompt_multiline("Specific Subsystems To Audit (optional):")
                raw_input_text = f"COVERAGE FOCUS: {summary}\n\nSUBSYSTEMS: {subsystems}"
            elif args.stitch or args.job_type == "design":
                vision = prompt_multiline("Design Vision & Requirements (describe the feature, UX goals, and any specific constraints):")
                
                vibe_options = [
                    "minimalist (clean, focused, white space)",
                    "glassmorphism (frosted glass, depth, vibrant colors)",
                    "brutalist (bold, raw, high contrast)",
                    "high-energy (animations, playful, dynamic)",
                    "gothic-noir (dark, moody, elegant)",
                    "custom (type your own vibe)"
                ]
                vibe_choice = prompt_radio("Select Aesthetic Vibe:", vibe_options, "minimalist (clean, focused, white space)", clear_screen=False)
                
                vibe = "minimalist"
                if "custom" in vibe_choice:
                    print("\n    (Enter a custom design style, e.g., 'Cyberpunk 2077', 'Soft UI')")
                    vibe = input("\033[1;96m    Custom Vibe:\033[0m ").strip()
                else:
                    vibe = vibe_choice.split(" ")[0]
                
                raw_input_text = f"DESIGN VISION: {vision}\nPREFERRED VIBE: {vibe}"
            else:
                vision = prompt_multiline("Feature Vision & Requirements (describe the feature and acceptance criteria):")
                raw_input_text = f"VISION: {vision}"

        if title_input.strip():
            extra_input += f"\n\nTITLE OVERRIDE: {title_input}"

        last_manual_logs = []
        if args.job_type == "bug":
            # ... (rest of log logic)
            pass

        if not raw_input_text.strip():
            raise ValueError("No input provided.")

    planning_context = {
        "job_id": f"new-{args.job_type}",
        "title": f"Planning {args.job_type.capitalize()}",
        "allowed_models": allowed_models or [],
        "allowed_machines": allowed_machines or [],
        "actual_planner_used": planner
    }
    with StatusBar(planning_context, is_processing=True):
        if args.job_type != "quick":  # a quick job is its own plan (see above)
            print_phase("planning")
        
            prompt_file = "planner_bug.md"
            if args.stitch:
                prompt_file = "designer.md"
            elif args.job_type == "feature":
                prompt_file = "planner_feature.md"
            elif args.job_type == "coverage":
                prompt_file = "planner_coverage.md"
            elif args.job_type == "design":
                prompt_file = "designer.md"
            
            prompt_template = (PROMPTS_DIR / prompt_file).read_text(encoding="utf-8")
        
            revision_context = ""
            if existing_job and args.feedback:
                prev_plan = existing_job.get("plan", {})
                revision_context = f"\n\n### PREVIOUS DESIGN/PLAN ###\n{json.dumps(prev_plan, indent=2)}\n\n### USER FEEDBACK ###\n{args.feedback}\n"
        
            # Feature, bug and coverage plans must specify test cases tied to real tests (enforced below).
            needs_test_cases = args.job_type in ("feature", "bug", "coverage") and not args.stitch
            test_case_context = ""
            if needs_test_cases:
                test_case_context = tc.PLANNER_INSTRUCTIONS + "\n" + tc.library_index(ROOT)
                prompt_template = f"{prompt_template}\n{test_case_context}"
            # The product's own documents (brief, use cases and non-goals, journey, screens, decisions, plan) come first,
            # so each plan is judged against what the product is for. It is part of the template, so retries keep it.
            product_context = prd.context_block(ROOT)
            if product_context:
                prompt_template = f"{prompt_template}\n{product_context}"
            llm_input = f"{prompt_template}\n\nRaw input:\n{raw_input_text}{extra_input}{revision_context}\n"
        
            llm_sessions = []
        
            print(f"\n[1/3] Planning {args.job_type} (Stitch AI Mode: {'Enabled' if args.stitch else 'Off'}) using {planner}...", flush=True)
            llm_raw_output, actual_planner, sid = run_llm(planner, llm_input, cwd=ROOT, allowed_models=allowed_models, role=ModelRole.PLANNER)
            llm_sessions.append({"id": sid, "model": actual_planner})

            llm_output = extract_json_block(llm_raw_output)
        
            # Print research findings if present
            research_part = llm_raw_output.replace(llm_output, "").strip()
            # Remove markdown noise (fences and leading/trailing markers)
            research_part = re.sub(r"```(?:json|markdown)?", "", research_part).strip()
        
            if research_part:
                print("\n--- Research & Discovery ---")
                print(research_part)
                print("-" * 30 + "\n")

            try:
                plan = normalize_plan_dict(
                    json.loads(llm_output, strict=False),
                    default_title=title_input,
                    default_summary=raw_input_text.splitlines()[0] if raw_input_text else "",
                    job_type=args.job_type,
                )
            except json.JSONDecodeError:
                print("\n\033[1;91mFAILED TO PARSE PLANNER OUTPUT AS JSON\033[0m")
                print("-" * 40)
                print(llm_output)
                print("-" * 40)
                raise

            # Check for clarification needed from planner
            clarification = plan.get("clarification_needed")
            clarification_history = existing_job.get("clarification_history", []) if existing_job else []
            if clarification:
                print("\n" + "!"*60)
                print(f"\033[1;93mPAUSED: Planner needs clarification\033[0m")
                print(f"\033[1;96mQuestion:\033[0m {clarification}")
                print("!"*60 + "\n")

                # Inline interactive prompt when running in an interactive terminal
                if interactive() and not getattr(args, "non_interactive", False) and not args.yolo:
                    print("\033[97m💡 Answer now to revise the plan immediately, or press Enter to pause.\033[0m")
                    try:
                        user_ans = input("\033[1;96mYour Answer (or Enter to pause):\033[0m ").strip()
                    except (EOFError, KeyboardInterrupt):
                        user_ans = ""
                    if user_ans:
                        clarification_history.append({
                            "question": clarification,
                            "answer": user_ans,
                            "timestamp": now_iso()
                        })
                        print(f"\n      - Re-planning with your answer...", flush=True)
                        clarification_feedback = f"### USER CLARIFICATION ###\nQuestion: {clarification}\nAnswer: {user_ans}\n"
                        recursive_input = f"{prompt_template}\n\n### ORIGINAL REQUEST ###\n{raw_input_text}\n\n### PREVIOUS PLAN WITH QUESTION ###\n{llm_output}\n\n{clarification_feedback}\nPlease update the plan JSON to address the user's clarification and proceed."
                        try:
                            new_llm_raw_output, actual_planner, sid = run_llm(planner, recursive_input, cwd=ROOT, allowed_models=allowed_models, role=ModelRole.PLANNER)
                            llm_sessions.append({"id": sid, "model": actual_planner})
                            llm_output = extract_json_block(new_llm_raw_output)
                            plan = normalize_plan_dict(
                                json.loads(llm_output, strict=False),
                                default_title=title_input,
                                default_summary=raw_input_text.splitlines()[0] if raw_input_text else "",
                                job_type=args.job_type,
                            )
                            clarification = plan.get("clarification_needed")
                            if not clarification:
                                print(f"      - Plan revised successfully! Proceeding...")
                        except Exception as e:
                            print(f"\033[1;91m      - Re-planning attempt failed: {e}\033[0m")

            def enforce_test_cases(plan: dict, llm_output: str, actual_planner: str) -> tuple[dict, str, str]:
                """Sends the plan back (up to twice) until its test cases cover every
                acceptance criterion and each automated case names its tests."""
                for attempt in range(2):
                    problems = tc.plan_problems(plan)
                    if not problems:
                        return plan, llm_output, actual_planner
                    print(f"      - Test cases incomplete; asking the planner to fix them ({attempt + 1}/2)...", flush=True)
                    for problem in problems:
                        print(f"        · {problem.splitlines()[0]}")
                    retry_input = (f"{prompt_template}\n\n### ORIGINAL REQUEST ###\n{raw_input_text}{extra_input}\n\n"
                                   f"### YOUR PREVIOUS PLAN ###\n{llm_output}\n\n{tc.replan_request(problems)}")
                    try:
                        retry_raw, actual_planner, retry_sid = run_llm(planner, retry_input, cwd=ROOT, allowed_models=allowed_models, role=ModelRole.PLANNER)
                        llm_sessions.append({"id": retry_sid, "model": actual_planner})
                        llm_output = extract_json_block(retry_raw)
                        plan = normalize_plan_dict(
                            json.loads(llm_output, strict=False),
                            default_title=title_input,
                            default_summary=raw_input_text.splitlines()[0] if raw_input_text else "",
                            job_type=args.job_type,
                        )
                    except Exception as e:
                        print(f"\033[1;91m      - Test-case re-plan failed: {e}\033[0m")
                        break
                remaining = tc.plan_problems(plan)
                if remaining:
                    print("\033[1;93m      - Plan still lacks adequate test cases; flagging it for your review.\033[0m")
                    plan["test_case_problems"] = remaining
                else:
                    plan.pop("test_case_problems", None)
                return plan, llm_output, actual_planner

            def enforce_slices(plan: dict, llm_output: str, actual_planner: str) -> tuple[dict, str, str]:
                """A feature plan should be a series of working slices, not a stack of layers. Sends a layered plan back
                once; if it still looks layered, keeps it with the warning visible instead of blocking."""
                found = slice_check.problems(plan)
                if found:
                    print("      - The plan looks layered; asking the planner to cut it into working slices...", flush=True)
                    for problem in found:
                        print(f"        · {problem}")
                    retry_input = (f"{prompt_template}\n\n### ORIGINAL REQUEST ###\n{raw_input_text}{extra_input}\n\n"
                                   f"### YOUR PREVIOUS PLAN ###\n{llm_output}\n\n{slice_check.replan_request(found)}")
                    try:
                        retry_raw, actual_planner, retry_sid = run_llm(planner, retry_input, cwd=ROOT, allowed_models=allowed_models, role=ModelRole.PLANNER)
                        llm_sessions.append({"id": retry_sid, "model": actual_planner})
                        llm_output = extract_json_block(retry_raw)
                        plan = normalize_plan_dict(
                            json.loads(llm_output, strict=False),
                            default_title=title_input,
                            default_summary=raw_input_text.splitlines()[0] if raw_input_text else "",
                            job_type=args.job_type,
                        )
                    except Exception as e:
                        print(f"\033[1;91m      - Slice re-plan failed: {e}\033[0m")
                remaining = slice_check.problems(plan)
                if remaining:
                    print("\033[1;93m      - Plan still looks layered; flagging it for your review.\033[0m")
                    plan["slice_warnings"] = remaining
                else:
                    plan.pop("slice_warnings", None)
                return plan, llm_output, actual_planner

            slices_matter = args.job_type == "feature" and not args.stitch

            if needs_test_cases and not clarification:
                plan, llm_output, actual_planner = enforce_test_cases(plan, llm_output, actual_planner)
            if slices_matter and not clarification:
                plan, llm_output, actual_planner = enforce_slices(plan, llm_output, actual_planner)

            # --- VERIFICATION STEP ---
            verification = None
            planner_meta = get_model(actual_planner)
        
            # Only verify if the planner used was NOT an Extreme model
            if planner_meta and planner_meta.tier > ModelTier.EXTREME and not args.skip_verify:
                # Find available verifier models in allowed_models
                extreme_options = get_prioritized_models(role=ModelRole.VERIFIER, allowed_models=allowed_models)
                if extreme_options:
                    verifier_model = extreme_options[0]
                    print(f"      - Plan generated by {actual_planner}. Verifying with Extreme model: {verifier_model}...", flush=True)
                
                    verifier_prompt = (PROMPTS_DIR / "verifier.md").read_text(encoding="utf-8")
                    if needs_test_cases:
                        verifier_prompt = f"{verifier_prompt}\n{tc.VERIFIER_INSTRUCTIONS}"
                    verifier_prompt = f"{verifier_prompt}\n{prd.context_block(ROOT, 'verifier')}"
                    verifier_input = f"{verifier_prompt}\n\n### ORIGINAL REQUEST ###\n{raw_input_text}\n\n### GENERATED PLAN ###\n{llm_output}"
                
                    try:
                        v_raw_output, actual_verifier, sid = run_llm(verifier_model, verifier_input, cwd=ROOT, allowed_models=allowed_models, role=ModelRole.VERIFIER)
                        llm_sessions.append({"id": sid, "model": actual_verifier})
                    
                        v_output = extract_json_block(v_raw_output)
                    
                        # Print verifier thoughts if present
                        v_thoughts = v_raw_output.replace(v_output, "").strip()
                        v_thoughts = re.sub(r"```(?:json|markdown)?", "", v_thoughts).strip()
                        if v_thoughts:
                            print(f"      - Verifier thoughts: {v_thoughts}")

                        verification = normalize_verification(json.loads(v_output, strict=False), actual_verifier)
                        if not verification:
                            raise ValueError("Verifier output missing required status/comments fields")
                        print(f"      - Verification complete: {verification['status'].upper()}")
                    
                        if verification["status"] in ["rejected", "concerns"]:
                            status_str = "rejected" if verification["status"] == "rejected" else "raised concerns"
                            print(f"\n\033[1;93mSenior Architect {status_str}.\033[0m")
                        
                            if args.yolo:
                                print(f"\n\033[1;92mYOLO MODE: Automatically integrating Architect suggestions...\033[0m")
                                # Construct feedback for recursive planning
                                yolo_feedback = f"### ARCHITECT FEEDBACK ({verification['status'].upper()}) ###\n"
                                yolo_feedback += f"Comments: {verification['comments']}\n"
                                if verification.get("suggested_additions"):
                                    yolo_feedback += "Suggested Additions:\n- " + "\n- ".join(verification["suggested_additions"])
                            
                                recursive_input = f"{prompt_template}\n\n### PREVIOUS PLAN ###\n{llm_output}\n\n{yolo_feedback}\n\n"
                                recursive_input += "Please update the plan JSON to address the architect's feedback while fulfilling the original request."
                            
                                print(f"      - Re-planning with {actual_planner}...", flush=True)
                                new_llm_raw_output, actual_planner, sid = run_llm(planner, recursive_input, cwd=ROOT, allowed_models=allowed_models, role=ModelRole.PLANNER)
                                llm_sessions.append({"id": sid, "model": actual_planner})
                            
                                try:
                                    llm_output = extract_json_block(new_llm_raw_output)
                                    plan = normalize_plan_dict(
                                        json.loads(llm_output, strict=False),
                                        default_title=title_input,
                                        default_summary=raw_input_text.splitlines()[0] if raw_input_text else "",
                                        job_type=args.job_type,
                                    )
                                    print(f"      - Plan revised successfully. Proceeding...")
                                    if needs_test_cases:
                                        plan, llm_output, actual_planner = enforce_test_cases(plan, llm_output, actual_planner)
                                    if slices_matter:
                                        plan, llm_output, actual_planner = enforce_slices(plan, llm_output, actual_planner)
                                    # Clear clarification so status stays 'planned'
                                    clarification = None
                                except:
                                    print(f"\033[1;91m      - Failed to parse revised plan. Falling back to human-needed.\033[0m")
                                    clarification = clarification or f"Architect {status_str}: {verification['comments']}"
                            else:
                                # Force human intervention
                                clarification = clarification or f"Architect {status_str}: {verification['comments']}"
                    except Exception as e:
                        print(f"⚠️  Verification failed: {e}. Proceeding with unverified plan.")
                        verification = None

            last_manual_logs = [] # Initialized but handled inside bug block above if needed

            title, body, labels, job_id_kind, job_type = issue_payload_for_job(
                args.job_type,
                plan,
                verification,
                clarification,
                args.stitch,
                yolo=args.yolo,
            )

        if existing_job:
            job_id = existing_job["job_id"]
            issue_number = existing_job.get("issue_number")
            job_ref = existing_job.get("job_ref") or str(issue_number)
            paths = make_job_paths(job_id)

            if issue_number:
                print(f"[2/3] Updating GitHub issue: #{issue_number}...", flush=True)
                gh_text("issue", "edit", str(issue_number), "--body", body)
        else:
            print(f"[2/3] Creating job: {title}...", flush=True)
            issue_number, job_ref = file_job(title, body, labels)
            job_id = f"{timestamp()}-{job_id_kind}-{job_ref}"
            paths = make_job_paths(job_id)

        status = "planned"
        if clarification:
            status = "human-needed"

        job = existing_job or {}
        job.update({
            "job_id": job_id,
            "type": job_type if not existing_job else job.get("type", job_type),
            "source": "manual" if args.job_type == "bug" else "planned_feature",
            "issue_number": issue_number,
            "job_ref": job_ref,
            "code_host": "github" if issue_number else "git",
            "title": title if not existing_job else job.get("title", title),
            "status": status,
            "planner": planner,
            "actual_planner_used": actual_planner,
            "verification": verification,
            "builder": builder,
            "reviewer": reviewer,
            "yolo": args.yolo,
            "branch_mode": branch_mode,
            "branch": selected_branch,
            "allowed_models": allowed_models,
            "allowed_machines": allowed_machines,
            "last_manual_log_paths": last_manual_logs,
            "reference_artifacts": job.get("reference_artifacts", []),
            "pr_number": job.get("pr_number"),
            "created_at": job.get("created_at", now_iso()),
            "updated_at": now_iso(),
            "raw_input": raw_input_text,
            "plan": plan,
            "llm_sessions": llm_sessions,
            "clarification_history": clarification_history if clarification_history else job.get("clarification_history", []),
            "interactive_investigations": existing_job.get("interactive_investigations", []) if existing_job else job.get("interactive_investigations", []),
            "investigation_notes": existing_job.get("investigation_notes", []) if existing_job else job.get("investigation_notes", []),
        })
        # Recorded as the job is created, so jobs created at the same time each know their own feature.
        if args.feature:
            job["feature"] = args.feature
            try:
                from orchestrator import features as feature_store
                feature_store.note_work_attached(PROJECT_CONFIG.runtime_dir, args.feature, "working")
            except Exception:
                pass  # a feature deleted meanwhile: the job still exists, unassigned work shows up on the page
        if args.plan_run:
            job["plan_run"] = args.plan_run
        
        # Special override for design-to-feature transition
        if existing_job and args.job_type == "feature":
            job["type"] = "feature-plan"
        
        if clarification:
            job["human_clarification_question"] = clarification
        else:
            job["human_clarification_question"] = None

        if needs_test_cases and isinstance(plan.get("test_cases"), list):
            previous = (existing_job or {}).get("plan", {}).get("test_cases") or []
            try:
                plan["test_cases"] = tc.normalize_cases(plan["test_cases"], issue_number or job_ref, job_id, existing=previous)
            except tc.TestCaseError as e:
                plan.setdefault("test_case_problems", []).append(f"`test_cases` is malformed: {e}.")
                plan["test_cases"] = []
            section = tc.issue_section(plan["test_cases"])
            if plan.get("test_case_problems"):
                section += ["", "> ⚠️ **Test cases need attention:** " + " ".join(p.splitlines()[0] for p in plan["test_case_problems"])]
            if section:
                body = body + "\n" + "\n".join(section)
            if section and issue_number:
                try:
                    gh_text("issue", "edit", str(issue_number), "--body", body)
                except Exception as e:
                    print(f"      - Note: could not add test cases to issue #{issue_number}: {e}")

        write_json(paths.job_file, job)
        write_text(paths.brief_file, body)

    # --- DESIGN PREVIEW ---
    if (args.stitch or args.job_type == "design") and not clarification:
        print("\n" + "\033[1;96m🎨 \033[0m" * 15)
        print(f"\033[1;96mDESIGN SPEC READY: {plan.get('title', 'Untitled')}\033[0m")
        print(f"\033[1;96mSummary:\033[0m {plan.get('summary', 'N/A')}")
        print(f"\033[1;96mVibe:\033[0m    \033[1;97m{plan.get('vibe', 'N/A')}\033[0m")
        
        comps = plan.get('visual_components', [])
        if comps:
            print("\033[1;96mKey Components:\033[0m")
            for comp in comps[:3]:
                print(f"  - {comp}")
            if len(comps) > 3:
                print(f"  \033[90m...and {len(comps)-3} more\033[0m")
        
        print("\033[1;96m🎨 \033[0m" * 15 + "\n")

    # --- COVERAGE & TEST EXPANSION REPORT ---
    if (args.job_type == "coverage" or job_type == "test-audit") and not clarification:
        suites = plan.get("target_suites") or plan.get("expected_test_files", [])
        if isinstance(suites, str):
            suites = [suites]
        subsystems = plan.get("target_subsystems", [])
        if isinstance(subsystems, str):
            subsystems = [subsystems]
        est_tests = plan.get("estimated_tests_added") or f"~{max(3, len(plan.get('audit_goals', [])) * 2)} focused unit tests"
        est_cov = plan.get("estimated_coverage_increase") or "+5.0% - +10.0% (target subsystems)"
        user_benefit = plan.get("user_benefit")
        if not user_benefit:
            sub_name = ', '.join(subsystems) if subsystems else 'core application modules'
            user_benefit = f"Eliminates regressions, ensures reliable error recovery, and prevents unexpected crashes across {sub_name}."

        print("\n" + "\033[1;92m=" * 58 + "\033[0m")
        print(f"   \033[1;92m🧪 TEST COVERAGE EXPANSION REPORT\033[0m")
        print("\033[1;92m=" * 58 + "\033[0m")
        print(f"   \033[1;36m• Target Title:\033[0m          \033[1;97m{plan.get('title', title)}\033[0m")
        if subsystems:
            print(f"   \033[1;36m• Focus Subsystems:\033[0m      \033[97m{', '.join(subsystems)}\033[0m")
        if suites:
            suite_str = ", ".join(suites[:4])
            if len(suites) > 4:
                suite_str += f" (+{len(suites)-4} more)"
            print(f"   \033[1;36m• Target Test Suite(s):\033[0m  \033[1;93m{suite_str}\033[0m")
        print(f"   \033[1;36m• Anticipated Tests:\033[0m     \033[1;92m{est_tests}\033[0m")
        print(f"   \033[1;36m• Estimated Coverage Δ:\033[0m  \033[1;95m{est_cov}\033[0m")
        print(f"   \033[1;36m• End-User Benefit:\033[0m      \033[97m{user_benefit}\033[0m")
        print("\033[1;92m=" * 58 + "\033[0m\n")

    try:
        job_file_display = str(paths.job_file.relative_to(ROOT))
    except ValueError:
        job_file_display = str(paths.job_file)

    print(f"      - Created job file: {job_file_display}")
    if issue_number:
        print(f"      - Created issue: #{issue_number}")

    if not args.no_dispatch and status == "planned":
        print(f"[3/3] Scheduling job {job_id}...", flush=True)
        import subprocess
        subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "schedule_job.py"), str(paths.job_file)],
            check=True,
            cwd=str(ROOT),
        )
        print("Done.", flush=True)
    else:
        print("[3/3] Skipping dispatch (--no-dispatch).", flush=True)


if __name__ == "__main__":
    main()
