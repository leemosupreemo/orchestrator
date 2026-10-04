#!/usr/bin/env python3
"""Non-menu entry points for actions that otherwise live only in the console,
so the web UI (and scripts) can run them. Each one reuses the console's own
implementation; the helpers below are shared with the console, not copies.

    job_actions.py merge   <job.json>                 # Merge & Mark Completed
    job_actions.py complete <job.json>                # archive a reviewed job that has no PR
    job_actions.py discard <job.json>                 # revert the job's AI changes, delete its branch, archive it
    job_actions.py answer  <job.json> --answer TEXT   # answer the planner's question
    job_actions.py approve <job.json>                 # accept suggestions / approve design / approve plan
    job_actions.py revise  <job.json> --change TEXT [--where TEXT] [--done-when TEXT]
    job_actions.py tests   [--json]                   # test suites, test plans, last coverage
    job_actions.py run-suite <name>                   # run one discovered test suite
    job_actions.py run-plan  <name>                   # run one .xctestplan
    job_actions.py coverage                           # measure code coverage
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from common import read_json, record_clarification, write_json

SCRIPTS_DIR = Path(__file__).resolve().parent

# Job type -> new_job.py type when re-planning (same mapping the console uses).
REPLAN_TYPES = {
    "bug-fix": "bug",
    "bug-investigate": "bug",
    "feature-plan": "feature",
    "test-audit": "coverage",
    "feature-design": "design",
}


def replan_type(job: dict[str, Any]) -> str:
    return REPLAN_TYPES.get(job.get("type"), "feature")


def architect_feedback(job: dict[str, Any]) -> str | None:
    """Re-plan feedback from a rejected/concerned architect review, or None."""
    verification = job.get("verification")
    if job.get("status") != "human-needed" or not verification or verification.get("status") not in ("rejected", "concerns"):
        return None
    feedback = "Please re-plan and incorporate the Senior Architect's suggestions:\n"
    feedback += f"Comments: {verification.get('comments')}\n"
    if verification.get("suggested_additions"):
        feedback += "Suggested Additions:\n- " + "\n- ".join(verification["suggested_additions"])
    return feedback


def apply_design_approval(job: dict[str, Any]) -> None:
    """Turns an approved design into a feature plan's input (mutates `job`)."""
    design_spec = job.get("plan", {})
    job["design_spec"] = design_spec
    job["status"] = "planned"
    job["type"] = "feature-plan"
    design_context = "\n\n### APPROVED DESIGN SPEC (Stitch AI) ###\n"
    design_context += f"Summary: {design_spec.get('summary')}\n"
    design_context += f"Vibe: {design_spec.get('vibe')}\n"
    design_context += "Visual Components:\n- " + "\n- ".join(design_spec.get("visual_components", [])) + "\n"
    design_context += "Interaction Flows:\n- " + "\n- ".join(design_spec.get("interaction_flows", [])) + "\n"
    job["raw_input"] = job.get("raw_input", "") + design_context


def revision_feedback(change: str, where: str = "", done_when: str = "") -> str:
    parts = [f"Requested change: {change}"]
    if where:
        parts.append(f"Affected area: {where}")
    if done_when:
        parts.append(f"Done when: {done_when}")
    return "\n".join(parts)


def run_script(name: str, args: list[str]) -> int:
    return subprocess.call([sys.executable, str(SCRIPTS_DIR / name), *args])


def load_job(path: Path) -> dict[str, Any]:
    job = read_json(path)
    job["_path"] = path
    return job


# --------------------------------------------------------------------------- job actions


def merge(path: Path) -> int:
    from dev_console import handle_merge_cleanup

    handle_merge_cleanup(load_job(path))
    return 0


def complete(path: Path) -> int:
    """Archive a reviewed job that has no PR to merge. Its branch is left alone."""
    from dev_console import archive_job

    job = load_job(path)
    if job.get("pr_number"):
        print("This job has a pull request; use Merge & complete so the PR is merged.")
        return 1
    job["restore_status"] = job.get("status")  # so the web UI can undo this
    archive_job(job, status="completed")
    print("Job marked complete.")
    return 0


def discard(path: Path, keep_changes: bool = False) -> int:
    from dev_console import handle_discard_job, archive_job

    job = load_job(path)
    if keep_changes:
        print(f"Deleting job {job.get('job_id')} and keeping changes...")
        archive_job(job, status="discarded")
        print("\n✅ Job deleted and code changes kept.")
        return 0
    handle_discard_job(job, confirmed=True)
    return 0


def answer(path: Path, text: str) -> int:
    job = read_json(path)
    question = job.get("human_clarification_question")
    if job.get("status") != "human-needed" or not question:
        print("This job isn't waiting on a question.")
        return 1
    original = json.loads(json.dumps(job))
    record_clarification(job, question, text)
    write_json(path, job)
    print("Answer recorded. Re-planning with it...\n")
    code = run_script("new_job.py", [replan_type(job), "--no-dispatch", "--update", str(path),
                                     "--feedback", f"### USER CLARIFICATION ###\n{text}"])
    if code != 0:
        # The re-plan failed. Put the question back so the job isn't left "waiting" on nothing and can be answered again.
        current = read_json(path)
        if current.get("status") == "human-needed" and not current.get("human_clarification_question"):
            current["human_clarification_question"] = question
            current["clarification_history"] = original.get("clarification_history", [])
            write_json(path, current)
            print("The re-plan failed, so your question is back. Answer it again to retry.")
    return code


def approve(path: Path) -> int:
    """The console's [A] Approve for everything except answering a question."""
    job = read_json(path)
    feedback = architect_feedback(job)
    if feedback:
        print("Integrating the architect's suggestions and re-planning...\n")
        # The person has accepted the architect's input; asking the architect again would only raise fresh concerns, forever.
        return run_script("new_job.py", [replan_type(job), "--no-dispatch", "--update", str(path), "--feedback", feedback, "--skip-verify"])
    if job.get("status") == "designing":
        print("Design approved. Generating implementation tasks from it...\n")
        apply_design_approval(job)
        write_json(path, job)
        return run_script("new_job.py", ["feature", "--no-dispatch", "--update", str(path)])
    if job.get("status") != "planned":
        print(f"Nothing to approve: the job is '{job.get('status')}'.")
        return 1
    job["approved"] = True
    write_json(path, job)
    print("Plan approved. Scheduling...\n")
    return run_script("schedule_job.py", [str(path)])


def revise(path: Path, change: str, where: str, done_when: str) -> int:
    """The console's Revise Plan (handle_tweak_revise) without the prompts."""
    job = read_json(path)
    args = [replan_type(job), "--no-dispatch", "--update", str(path),
            "--feedback", revision_feedback(change, where, done_when)]
    # Keep the job's own choices, so re-planning never stops to ask for models, machines or a branch mode.
    if job.get("allowed_models"):
        args += ["--allowed-models", ",".join(job["allowed_models"])]
    if job.get("allowed_machines"):
        args += ["--allowed-machines", ",".join(job["allowed_machines"])]
    args += ["--branch-mode", job.get("branch_mode") or ("current" if job.get("branch") else "new")]
    if job.get("design_spec") or job.get("status") == "designing" or "design" in str(job.get("type", "")):
        args.append("--stitch")
    print("Revising the plan with your feedback...\n")
    return run_script("new_job.py", args)


# --------------------------------------------------------------------------- tests


def frameworks_inventory() -> dict[str, Any]:
    from dev_console import ROOT, PROJECT_CONFIG, discover_test_suites
    import shutil
    has_xcbeautify = shutil.which("xcbeautify") is not None
    suites = discover_test_suites(ROOT, PROJECT_CONFIG.test_target)
    all_test_content = ""
    for s in suites:
        try:
            p = s.get("path")
            if p and Path(p).is_file():
                all_test_content += Path(p).read_text(encoding="utf-8") + "\n"
        except Exception:
            pass

    has_swift_testing = "import Testing" in all_test_content or "@Test" in all_test_content
    has_snapshot_testing = "import SnapshotTesting" in all_test_content or "assertSnapshot" in all_test_content
    has_quick_nimble = "import Quick" in all_test_content or "import Nimble" in all_test_content

    tt = PROJECT_CONFIG.test_target or "AppTests"
    sample_canary_exists = (ROOT / tt / "SampleSwiftTestingTests.swift").exists() if PROJECT_CONFIG.test_target else False

    return {
        "swift_testing": {"installed": bool(has_swift_testing), "name": "Swift Testing (Native)", "desc": "Core logic, ViewModels & async unit tests"},
        "snapshot_testing": {"installed": bool(has_snapshot_testing), "name": "SnapshotTesting", "desc": "Visual regressions (SwiftUI pixels, Dark Mode)"},
        "quick_nimble": {"installed": bool(has_quick_nimble), "name": "Quick & Nimble (BDD)", "desc": "Multi-step async state machines & polling specs"},
        "canary_suite": {"installed": bool(sample_canary_exists), "name": "Sample Canary Suite", "desc": f"1-click test suite generation in {tt}"},
        "xcbeautify": {"installed": bool(has_xcbeautify), "name": "xcbeautify Formatter", "desc": "Strip noisy xcodebuild output into 1-line logs"},
    }


def scaffold_canary() -> int:
    from dev_console import ROOT, PROJECT_CONFIG
    tt = PROJECT_CONFIG.test_target or "AppTests"
    tt_dir = ROOT / tt
    tt_dir.mkdir(parents=True, exist_ok=True)
    sample_file = tt_dir / "SampleSwiftTestingTests.swift"
    scheme = PROJECT_CONFIG.scheme or PROJECT_CONFIG.project_name or "App"
    sample_code = f"""import Testing
@testable import {scheme}

@Suite("Sample Orchestrator Suite")
struct SampleSwiftTestingTests {{

    @Test("Basic arithmetic validation with expect")
    func basicAssertion() {{
        #expect(2 + 2 == 4)
    }}

    @Test("Parameterized calculation validation", arguments: [
        (2, 3, 5),
        (10, 20, 30),
        (-5, 5, 0)
    ])
    func parameterizedTest(a: Int, b: Int, expected: Int) {{
        #expect(a + b == expected)
    }}

    @Test("Async requirement verification")
    func asyncRequirement() async throws {{
        let value: Int? = 42
        let unwrapped = try #require(value, "Value must be present")
        #expect(unwrapped > 0)
    }}
}}
"""
    if sample_file.exists():
        print(f"File already exists: {sample_file}")
        return 0
    sample_file.write_text(sample_code, encoding="utf-8")
    print(f"✅ Created canary test suite: {sample_file}")
    return 0


def test_inventory() -> dict[str, Any]:
    from dev_console import ROOT, PROJECT_CONFIG, discover_test_suites, get_coverage_data

    suites = discover_test_suites(ROOT, PROJECT_CONFIG.test_target)
    plans_root = ROOT / (PROJECT_CONFIG.test_target or "") / "TestPlans"
    plans = sorted(plans_root.glob("*.xctestplan")) if PROJECT_CONFIG.test_target and plans_root.exists() else []
    return {
        "suites": [{"name": s["name"], "tests": s["test_count"], "path": str(s.get("rel_path") or ""),
                    "language": s.get("language", "swift")} for s in suites],
        "plans": [p.stem for p in plans],
        "coverage": get_coverage_data(),
        "frameworks": frameworks_inventory(),
    }


def run_suite(name: str) -> int:
    from dev_console import ROOT, PROJECT_CONFIG, discover_test_suites, test_command_for_suite

    suite = next((s for s in discover_test_suites(ROOT, PROJECT_CONFIG.test_target) if s["name"] == name), None)
    if not suite:
        print(f"No test suite named '{name}'.")
        return 1
    if suite.get("language", "swift") == "swift":
        target = PROJECT_CONFIG.test_target or ""
        flag = f"-only-testing:{target}/{name}" if target else f"-only-testing:{name}"
        return run_script("manual_run.py", ["test", "--test-only", flag])
    return run_script("manual_run.py", ["test", "--test-command", test_command_for_suite(suite, PROJECT_CONFIG)])


def run_plan(name: str) -> int:
    from dev_console import ROOT, PROJECT_CONFIG
    from common import get_test_plan_flags

    plan = ROOT / (PROJECT_CONFIG.test_target or "") / "TestPlans" / f"{name}.xctestplan"
    if not plan.is_file():
        print(f"No test plan named '{name}'.")
        return 1
    return run_script("manual_run.py", ["test", "--test-only", get_test_plan_flags(plan)])


def coverage() -> int:
    from dev_console import run_calculate_coverage

    return 0 if run_calculate_coverage([], []) is not None else 1


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    for name, help_text in [("merge", "Merge the job's PR, clean up its branch and archive it"),
                            ("approve", "Accept architect suggestions, approve a design, or approve a plan"),
                            ("complete", "Archive a reviewed job that has no PR to merge")]:
        sub.add_parser(name, help=help_text).add_argument("job_file")
    for name, help_text in [("discard", "Revert the job's AI changes, delete its AI branch and archive it"),
                            ("delete", "Delete a job, optionally keeping code changes and branch")]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("job_file")
        p.add_argument("--keep-changes", action="store_true", help="Keep code changes instead of reverting")
    answer_p = sub.add_parser("answer", help="Answer the planner's clarification question and re-plan")
    answer_p.add_argument("job_file")
    answer_p.add_argument("--answer", required=True)
    revise_p = sub.add_parser("revise", help="Re-plan a job with requested changes")
    revise_p.add_argument("job_file")
    revise_p.add_argument("--change", required=True)
    revise_p.add_argument("--where", default="")
    revise_p.add_argument("--done-when", default="")
    tests_p = sub.add_parser("tests", help="List test suites, test plans and the last coverage result")
    tests_p.add_argument("--json", action="store_true")
    sub.add_parser("run-suite", help="Run one test suite").add_argument("name")
    sub.add_parser("run-plan", help="Run one test plan").add_argument("name")
    sub.add_parser("coverage", help="Measure code coverage")
    sub.add_parser("scaffold-canary", help="Generate sample Swift Testing canary suite")
    args = parser.parse_args(argv)

    if args.action == "tests":
        inventory = test_inventory()
        if args.json:
            print(json.dumps(inventory))
        else:
            for s in inventory["suites"]:
                print(f"{s['name']:40} {s['tests']:4} tests  {s['path']}")
            for p in inventory["plans"]:
                print(f"plan: {p}")
        return 0
    if args.action == "run-suite":
        return run_suite(args.name)
    if args.action == "run-plan":
        return run_plan(args.name)
    if args.action == "coverage":
        return coverage()
    if args.action == "scaffold-canary":
        return scaffold_canary()

    path = Path(args.job_file).resolve()
    if not path.is_file():
        print(f"Job not found: {path}")
        return 1
    if args.action == "merge":
        return merge(path)
    if args.action == "approve":
        return approve(path)
    if args.action == "complete":
        return complete(path)
    if args.action in ("discard", "delete"):
        return discard(path, keep_changes=bool(getattr(args, "keep_changes", False)))
    if args.action == "revise":
        return revise(path, args.change.strip(), args.where.strip(), args.done_when.strip())
    return answer(path, args.answer.strip())


if __name__ == "__main__":
    raise SystemExit(main())
