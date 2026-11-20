#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any

from common import OUTPUT_DIR, PROMPTS_DIR, ROOT, read_json, write_text, print_phase, StatusBar, format_clarification_history, format_investigation_history
from llm import run_llm, extract_json_block
from model_router import ModelRole, resolve_builder_model
from run_build_and_tests import run_build_and_tests
from reference_artifacts import reference_context
from orchestrator import reuse_brief
from orchestrator.project_config import PROJECT_CONFIG


class BuilderClarificationNeeded(RuntimeError):
    def __init__(self, question: str):
        self.question = question
        super().__init__(f"Builder clarification needed: {question}")


def make_brief(job: dict) -> str:
    plan = job["plan"]
    verification = job.get("verification")
    references = reference_context(job)
    
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

    # Every brief has the same shape: a heading per part, and a plain note when a part is empty (people read briefs on the
    # job page; the builder reads the same text), so "Likely files:" with nothing under it never looks like a gap.
    def items(values: Any, empty: str) -> str:
        values = [str(v).strip() for v in (values or []) if str(v).strip()] if isinstance(values, list) else []
        return "\n".join(f"- {v}" for v in values) if values else f"_{empty}_"

    def text(value: Any, empty: str) -> str:
        value = str(value or "").strip()
        return value if value else f"_{empty}_"

    def part(title: str, body: str) -> str:
        return f"## {title}\n\n{body.strip()}\n"

    issue = f"#{job['issue_number']}" if job.get("issue_number") else "_No issue linked._"
    head = f"**Issue:** {issue}  \n**Title:** {job.get('title') or '_Untitled_'}\n"
    extras = "\n".join(x.strip() for x in (clarification_text, investigation_text) if x and x.strip())
    files = part("Likely files", items(plan.get("likely_files"), "None listed yet: the builder works out which files to change."))
    criteria = part("Acceptance criteria", items(plan.get("acceptance_criteria"), "None listed: done when the summary above is met and the tests pass."))

    if job["type"] in {"bug-fix", "bug-investigate"}:
        sections = [
            part("Summary", text(plan.get("summary"), "No summary given.")),
            extras,
            verification_text.strip(),
            part("Steps to reproduce", items(plan.get("repro_steps"), "None given.")),
            part("Expected behavior", text(plan.get("expected_behavior"), "Not described.")),
            criteria,
            part("Constraints", items(plan.get("constraints"), "None.")),
            files,
            part("Test recommendations (TDD)", items(plan.get("test_recommendations"), "")) if plan.get("test_recommendations") else "",
        ]
        title = "Bug brief"
    elif job["type"] in ("feature-plan", "quick-fix"):
        tasks = plan.get("tasks") or []
        plan_lines = "\n".join(f"{i}. {t.get('title', '')}" + (f": {t['description']}" if t.get("description") else "")
                               for i, t in enumerate(tasks, start=1) if isinstance(t, dict))
        quick = job["type"] == "quick-fix"
        sections = [
            part("Instructions", text(plan.get("instructions"), "None beyond the summary: the summary is the whole request.")) if quick else "",
            part("Fast-tracked plan", "Build all of it in this one pass.\n\n" + plan_lines) if quick and plan_lines else "",
            part("Summary", text(plan.get("summary"), "No summary given.")),
            extras,
            criteria,
            files,
        ]
        title = "Quick task brief" if quick else "Feature task brief"
    else:
        sections = [part("Summary", text(plan.get("summary"), "No summary given.")), extras, verification_text.strip()]
        title = "Feature task brief"
    body = "\n".join(x for x in sections if x)
    return f"# {title}\n\n{head}\n{body}\n{references}\n"

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


def _quick_scope_guardrail(job: dict, likely_files: list) -> str:
    """Extra scoping guidance for unplanned quick jobs so broad/visual requests don't degrade into token stubs."""
    if job.get("type") != "quick-fix" or likely_files:
        return ""
    return """
### QUICK TASK SCOPING GUARDRAIL
This quick task has no detailed plan or target file list. Before editing:
- Inspect the relevant subsystem end-to-end so broad or qualitative requests (e.g., visual quality, realism, rendering, performance, polish) produce cohesive, noticeable improvements across shaders, materials, spawning/callers, or runtime logic rather than minimal token tweaks.
- Every new helper, method, or field MUST be wired into real runtime callers (never leave dead helpers).
- Tests MUST execute real behavior or compilation — NEVER write tests that merely open source/shader files and assert substrings (`assertIn` / regex), and never use `test_command` to hide the repository's configured test suite.
"""


def run_builder(job_path: Path, resume: bool = False) -> tuple[bool, bool, Path]:
    job = read_json(job_path)
    from orchestrator.job_scope import NONPRODUCTION_TYPES
    if job.get("type") in NONPRODUCTION_TYPES:
        raise RuntimeError("The production builder is disabled for Design and Tests jobs.")
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

    from orchestrator import builder_check
    if resume and summary_file.exists() and builder_check.writes_blocked(summary_file.read_text(encoding="utf-8", errors="replace")):
        # That run changed nothing (its writes were refused), so there is nothing to resume from: build it again.
        print(f"      - The saved builder summary says its writes were blocked, so nothing was built. Running the builder again.")
        resume = False
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

        from test_cases import prompt_block
        test_case_block = prompt_block(job.get("plan", {}).get("test_cases") or [])
        from orchestrator import architecture, prd
        product_context = prd.context_block(ROOT, "builder") + architecture.context_block(ROOT, "builder")

        # What the project already has that matches this task, so the builder reuses it instead of writing a second copy.
        # Bug fixes edit existing code by nature. A failed search just means no suggestions, never a failed build.
        reuse_found: list[dict] = []
        reuse_context = ""
        if job["type"] not in {"bug-fix", "bug-investigate"}:
            try:
                reuse_found = reuse_brief.candidates(ROOT, job)
            except Exception:
                reuse_found = []
            reuse_context = reuse_brief.prompt_block(reuse_found, ROOT, likely_files)
        job["reuse_suggested"] = reuse_found  # the job page reports how many of these the finished change used

        try:
            from orchestrator import parallel_mece
            peer_lanes = parallel_mece.lane_context(
                parallel_mece.load_active_peers(
                    job_path.parent,
                    current_job_id=job.get("job_id") or job_path.stem,
                    current_job=job,
                ),
                role="builder",
            )
        except Exception:
            peer_lanes = ""

        ui_screenshot_block = ""
        try:
            from orchestrator import ux_review
            ux_cfg = ux_review.from_project(ROOT)
            touches_ui = bool(ux_review.frontend_files(likely_files, ux_cfg.get("paths"))) if likely_files else False
            if touches_ui or ux_review.has_screens(ux_cfg):
                ui_screenshot_block = f"""### UI screenshot evidence
For every new screen or visible UI alteration, update `{OUTPUT_DIR / job['job_id'] / 'ux-screens.json'}` before finishing.
List every changed page and interactive state, preserving entries from earlier tasks in this job. Use this JSON shape:
{{"routes": ["/changed-page"], "screens": [{{"name": "Dialog open", "route": "/changed-page", "actions": [{{"click": "button.open-dialog"}}]}}], "notes": []}}
Routes must be relative to the configured app. Actions support {{"click": "CSS selector"}} and {{"fill": "CSS selector", "value": "non-sensitive test value"}}.
Use stable selectors and test data. Do not put credentials in the capture plan. For a state that cannot be reached automatically, add a clear explanation under notes.
Ensure the app at ui_review.url serves this job's changes; use ui_review.start_command for a local app if needed.
The runner captures configured pages plus this inventory at the configured phone/desktop widths and colour modes, and saves them in UX review.
Screenshot capture is evidence, separate from whether the optional AI review is enabled. If no UI changes, no capture plan is needed.
"""
        except Exception:
            ui_screenshot_block = ""

        full_prompt = f"""{prompt_template}

Grounding docs to inspect first:
- AGENTS.md
- docs/architecture.md
- docs/coding-standards.md
- docs/build-test-commands.md
{stack_context}
Available Tests (use for selecting test_command):
{test_index}
{test_case_block}{debug_context}
{yolo_context}
{_quick_scope_guardrail(job, likely_files)}
{product_context}
{peer_lanes}{reuse_context}{ui_screenshot_block}Brief:
{brief}
"""

        print_phase("agent_thinking")
        status_bar.render()
        tree_before = builder_check.tree_fingerprint(ROOT)
        output, actual_builder, session_id = run_llm(
            resolve_builder_model(job), 
            full_prompt, 
            cwd=ROOT, 
            timeout=1200, 
            allowed_models=job.get("allowed_models"),
            role=ModelRole.BUILDER
        )

        if builder_check.writes_blocked(output) and not builder_check.changed(tree_before, builder_check.tree_fingerprint(ROOT)):
            # Its own report says the AI tool refused every write, and the working tree agrees. Building and testing now would only
            # test the old code against tests it never wrote, so stop here and say why.
            write_text(summary_file, output)
            raise RuntimeError("The builder could not change any files: its writes were blocked, most likely by the AI tool's permission mode. "
                               "Nothing was built. Check that tool's permissions, then rerun the job.")

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
        from common import classify_clarification
        clar_kind, q = classify_clarification(parsed_output.get("clarification_needed"))
        if clar_kind == "blocking":
            raise RuntimeError(f"Builder blocked: {q}")
        if clar_kind == "ignore" and q:
            print(f"      - Ignoring non-question clarification note: {q}")
        if clar_kind == "question":
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
