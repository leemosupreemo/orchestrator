"""Run Design and Tests without the production builder, debugger or git pipeline."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from common import (ROOT, OUTPUT_DIR, PROMPTS_DIR, command_with_destination, extract_destination_from_command, get_fallback_simulator_destinations,
                    get_repo_state, now_iso, read_json, write_json, write_text)
from llm import extract_json_block, run_llm
from model_router import ModelRole
from orchestrator import job_scope, xcode_tests
from orchestrator.project_config import PROJECT_CONFIG
from reference_artifacts import attach_reference_artifact, reference_context

# Output that means the build environment failed before any test could run, not that a test failed.
ENVIRONMENT_FAILURES = re.compile(
    r"Could not resolve package dependencies|sandbox_apply: Operation not permitted|Unable to find a (?:destination|device) matching"
    r"|Failed to resolve (?:package|dependencies)|command not found", re.I)


# test_validation kinds that mean the tests are written and only checking them is left.
RESUMABLE_VALIDATION = {"environment", "pending", "unregistered"}


def sandbox_safe(command: str) -> str:
    """The read-only sandbox around validation can't be nested, so tools that sandbox their own manifests are told not to."""
    # Manifests and package plugins sandbox themselves, and so does the Swift compiler when it runs a macro (SwiftUI's @State is one):
    # each is a nested sandbox, which macOS refuses. The outer sandbox is the one that keeps the project read-only.
    command = re.sub(r"(^|[;&|]\s*)xcodebuild\b(?! -IDEPackageSupportDisableManifestSandbox)",
                     r"\1xcodebuild -IDEPackageSupportDisableManifestSandbox=1 -IDEPackageSupportDisablePluginExecutionSandbox=1 "
                     "-skipMacroValidation -skipPackagePluginValidation OTHER_SWIFT_FLAGS='$(inherited) -disable-sandbox'", command)
    return re.sub(r"\bswift (build|test)\b(?! --disable-sandbox)", r"swift \1 --disable-sandbox", command)


def environment_failure(output: str) -> str:
    """The line that shows the environment failed, or an empty string when the output reads like failing tests."""
    line = next((line for line in output.splitlines() if ENVIRONMENT_FAILURES.search(line)), "")
    return line.strip()[:300]


def owned_files_intact(job: dict) -> bool:
    """The job's saved test files are all still there. Edits the person made to them are fine: checking the files as they are now
    is what a retry is for, and a rewrite still leaves edited files alone."""
    owned = job.get("output_digests") or {}
    return bool(owned) and all((ROOT / path).is_file() for path in owned)


XCTEST_FAILURE = re.compile(r"error: -\[(?:[\w.]+\.)?(\w+) (\w+)\] : (.+)")
PYTEST_FAILURE = re.compile(r"^FAILED (\S+)(?: - (.+))?$")


def failing_tests(output: str, limit: int = 12) -> list[dict]:
    """The tests that failed in a run's output, each with what it complained about (XCTest and pytest summaries)."""
    found: dict[str, list[str]] = {}
    for line in output.splitlines():
        xc = XCTEST_FAILURE.search(line)
        if xc:
            found.setdefault(f"{xc[1]}.{xc[2]}", []).append(xc[3].strip()[:200])
            continue
        py = PYTEST_FAILURE.match(line.strip())
        if py:
            found.setdefault(py[1], []).append((py[2] or "").strip()[:200])
    return [{"test": test, "message": "; ".join(dict.fromkeys(messages))[:300]} for test, messages in list(found.items())[:limit]]


def retry_context(job: dict, out: Path) -> str:
    """What a rewrite is told about the last attempt: which tests failed and why, and anything the person said."""
    check = job.get("test_validation") or {}
    parts = []
    if check.get("kind") == "tests" and not check.get("passed"):
        failures = check.get("failures")
        if not failures:
            try:
                failures = failing_tests((out / "test.log").read_text(encoding="utf-8", errors="replace"))
            except OSError:
                failures = []
        listing = "\n".join(f"- {f['test']}: {f['message']}" for f in failures) or "(the log named no individual test)"
        parts.append("\n## The last attempt's tests failed\nThese are the tests you (or an earlier attempt) wrote that did not pass:\n" + listing +
                     "\n\nDecide for each failure whether the TEST or the CODE is wrong. Read the production code to be sure.\n"
                     "- The test expects the wrong thing (misread the code, wrong fixture, timing): rewrite it to check what the code really should do.\n"
                     "- The code is wrong (the test states what the code should do and it does not): do NOT weaken the test and do NOT keep it in "
                     "your files. Leave it out and list it under \"defects\" with the test's source, so it can be added once the code is fixed.\n"
                     "Return the complete contents of every test file you change; a file you return replaces the old one, so remove a test by returning "
                     "the file without it.")
    said = str(job.get("debug_feedback") or "").strip()
    if said:
        parts.append(f"\n## What the person said\n{said[:2000]}")
    return "\n".join(parts)


def clean_defects(raw: object, previous: list | None = None) -> list[dict]:
    """Production defects the builder reported, trimmed to plain text; earlier ones stay unless the same test is reported again."""
    merged = {str(d.get("test") or d.get("summary")): d for d in previous or [] if isinstance(d, dict)}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not str(item.get("summary") or "").strip():
            continue
        defect = {key: " ".join(str(item.get(key) or "").split())[:400] for key in ("test", "summary", "expected", "actual", "file")}
        defect["code"] = str(item.get("code") or "")[:3000]
        merged[defect["test"] or defect["summary"]] = {k: v for k, v in defect.items() if v}
    return list(merged.values())[:12]


def keep_registration(job: dict, files: list[str]) -> None:
    """Add the new test files to the real project's test target too, so they run in Xcode. The project file is only touched when it
    has no uncommitted edits of the person's own."""
    project = xcode_tests.project_file(ROOT)
    check = job["test_validation"]
    if project is None:
        return
    rel = project.relative_to(ROOT).as_posix()
    status = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=ROOT, capture_output=True, text=True)
    mine = OUTPUT_DIR.parent / "xcode_registered.json"  # the project file as the last registration left it
    def fingerprint() -> str:
        return hashlib.sha256(project.read_bytes()).hexdigest()
    try:
        ours = json.loads(mine.read_text(encoding="utf-8")).get("sha") == fingerprint()
    except (OSError, ValueError):
        ours = False
    if status.returncode or (status.stdout.strip() and not ours):  # uncommitted edits that aren't this job's own earlier ones
        check["note"] = f"{rel} has uncommitted changes, so the new test files weren't added to the Xcode test target. Add them in Xcode."
        return
    added, note = xcode_tests.register(ROOT, files)
    if note:
        check["note"] = note
    elif added:
        check["registered"] = added
        job["ai_modified_files"] = sorted(set(job.get("ai_modified_files") or []) | {rel})
        write_text(mine, json.dumps({"sha": fingerprint()}))


def run_validation(job: dict, out: Path) -> None:
    """Run the project's tests against a disposable copy; raise if they can't run or don't pass."""
    command = PROJECT_CONFIG.test_command or PROJECT_CONFIG.stack.test_command
    if not command:
        from common import extract_commands
        command = extract_commands()[1]
    job["test_validation"] = {"command": command, "passed": False, "kind": "tests"}
    with tempfile.TemporaryDirectory(prefix="orchestrator-coverage-") as scratch:
        # Build caches and test fixtures may change in this disposable copy.
        # Only validated test proposals are ever copied to the real project.
        work = Path(scratch).resolve() / "project"
        def ignore(directory, names):
            return set(names) & {".git", ".orchestrator", ".build", "target", "node_modules", ".venv", "venv", "__pycache__"} if Path(directory) == ROOT else set()
        shutil.copytree(ROOT, work, symlinks=True, ignore=ignore)
        for dependency in ("node_modules", ".venv", "venv"):
            if (ROOT / dependency).is_dir():
                (work / dependency).symlink_to(ROOT / dependency, target_is_directory=True)
        config = ROOT / ".orchestrator/project.json"
        if config.is_file():
            (work / ".orchestrator").mkdir(exist_ok=True)
            shutil.copy2(config, work / ".orchestrator/project.json")
        # Xcode only builds the files its project lists: add the new test files to the copy's test target so they really run.
        test_files = [str(f) for f in (job.get("output_digests") or {})]
        registered, register_note = xcode_tests.register(work, test_files)
        isolated_command = job_scope.protect_command(sandbox_safe(command.replace(str(ROOT), str(work))), [ROOT])
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "COVERAGE_FILE": str(Path(scratch) / ".coverage"), "TMPDIR": scratch,
               "ORCHESTRATOR_PROJECT_ROOT": str(work), "ORCHESTRATOR_RUNTIME_DIR": str(work / ".orchestrator"),
               "AI_RUNTIME_DIR": str(work / ".orchestrator")}
        env.pop("ORCHESTRATOR_CONFIG", None)
        if env.get("PYTHONPATH"):
            env["PYTHONPATH"] = os.pathsep.join(str(work / Path(part).relative_to(ROOT)) if part and Path(part).is_relative_to(ROOT) else part
                                                  for part in env["PYTHONPATH"].split(os.pathsep))
        def attempt(test_command: str) -> subprocess.CompletedProcess:
            return subprocess.run(job_scope.protect_command(sandbox_safe(test_command.replace(str(ROOT), str(work))), [ROOT]), shell=True,
                                  cwd=work, env=env, capture_output=True, text=True, timeout=1200)

        validation = subprocess.run(isolated_command, shell=True, cwd=work, env=env,
                                    capture_output=True, text=True, timeout=1200)
        # The configured simulator may not exist on this computer: try the ones that do before calling it an environment failure.
        if validation.returncode and "xcodebuild" in command and re.search(r"Unable to find a device matching", validation.stdout + validation.stderr):
            configured = extract_destination_from_command(command)
            for candidate in get_fallback_simulator_destinations(configured):
                if candidate == configured:
                    continue
                retry = attempt(command_with_destination(command, candidate))
                if retry.returncode == 0 or not re.search(r"Unable to find a device matching", retry.stdout + retry.stderr):
                    validation = retry
                    job["test_validation"]["command"] = command_with_destination(command, candidate)
                    job["test_validation"]["destination"] = candidate
                    break
        reports = [Path(scratch) / ".coverage", work / "coverage.xml", work / "lcov.info", work / "coverage/lcov.info"]
        for report in reports:
            if report.is_file():
                (out / "coverage").mkdir(exist_ok=True)
                shutil.copy2(report, out / "coverage" / report.name)
    output = validation.stdout + validation.stderr
    write_text(out / "test.log", output)
    job["test_validation"]["passed"] = validation.returncode == 0
    if validation.returncode == 0:
        left_out = xcode_tests.not_run(ROOT, test_files, output)
        if left_out:  # a pass that never ran the new tests proves nothing
            job["test_validation"].update(passed=False, kind="unregistered", not_run=left_out,
                                          detail=register_note or "They aren't part of the project's test target.")
            raise ValueError(f"The tests passed, but {len(left_out)} of the new test file{'' if len(left_out) == 1 else 's'} never ran: "
                             f"{', '.join(Path(f).name for f in left_out)}. {job['test_validation']['detail']} "
                             "The files are saved; add them to the test target, then Try again.")
        if registered:  # they ran in the copy, so make them part of the real project too, unless that would overwrite the person's edits
            keep_registration(job, test_files)
    if validation.returncode:
        broken = environment_failure(output)
        if broken:  # nothing ran, so there is nothing for a fix attempt to look into
            job["test_validation"].update(kind="environment", detail=broken)
            raise ValueError(f"The tests couldn't run: the build environment failed before any test started ({broken}). "
                             "The new test files are saved. Try again to check them without rewriting them.")
        job["test_validation"]["failures"] = failing_tests(output)
        raise ValueError("Test validation failed. Review test.log; production fixes require a separate Bug fix job.")
    job["test_validation"]["kind"] = "passed"


def execute_nonproduction_job(job_path: Path) -> None:
    job = read_json(job_path)
    design = job.get("type") in job_scope.DESIGN_TYPES
    job.update(status="executing", worker_pid=os.getpid(), updated_at=now_iso())
    write_json(job_path, job)
    out = OUTPUT_DIR / job["job_id"]
    out.mkdir(parents=True, exist_ok=True)
    try:
        if not design and (job.get("test_validation") or {}).get("kind") in RESUMABLE_VALIDATION and owned_files_intact(job):
            # The tests were written, then checking them couldn't finish (the environment failed, or the run was stopped):
            # check them again without calling a model.
            run_validation(job, out)
            job.update(status="review-needed", last_error=None, human_clarification_question=None)
            return
        before = get_repo_state()
        dirty = set(before["modified"]) | set(before["untracked"])
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, text=True)
        if untracked.returncode == 0:
            dirty.update(untracked.stdout.rstrip("\0").split("\0"))
        owned = job.get("output_digests") or {}
        for path, digest in owned.items():
            target = ROOT / path
            if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                dirty.discard(path)
        template = "builder_design.md" if design else "builder_coverage.md"
        prompt = (PROMPTS_DIR / template).read_text(encoding="utf-8")
        prompt += f"\nJob: {job['title']}\nPlan:\n{json.dumps(job.get('plan', {}), indent=2)}\n{reference_context(job)}"
        prompt += retry_context(job, out)
        from orchestrator import prd
        prompt += prd.context_block(ROOT, "builder")
        output, model, session = run_llm(job["builder"], prompt, cwd=ROOT, timeout=1200,
                                       allowed_models=job.get("allowed_models"), role=ModelRole.BUILDER,
                                       protected_paths=[ROOT])
        result = json.loads(extract_json_block(output))
        if result.get("clarification_needed"):
            job["human_clarification_question"] = result["clarification_needed"]
            raise ValueError(result["clarification_needed"])
        proposals = result.get("files")
        if isinstance(proposals, list) and not design:
            conflicts = [p.get("path") for p in proposals if isinstance(p, dict) and p.get("path") in dirty]
            if conflicts:
                raise ValueError("Test outputs overlap existing local changes: " + ", ".join(conflicts))
        written = job_scope.write_files(ROOT, job["type"], job["job_id"], proposals)
        job["actual_builder_used"] = model
        job.setdefault("llm_sessions", []).append({"id": session, "model": model})
        job["builder_summary"] = str(result.get("summary") or "")
        job.pop("debug_feedback", None)  # said once, to this attempt
        if not design:
            job["production_defects"] = clean_defects(result.get("defects"), job.get("production_defects"))
        tracked_result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True)
        tracked = set(tracked_result.stdout.rstrip("\0").split("\0")) if tracked_result.returncode == 0 else set()
        job["ai_modified_files"] = sorted(set(job.get("ai_modified_files") or []) | {path for path in written if path in tracked})
        job["ai_untracked_files"] = sorted(set(job.get("ai_untracked_files") or []) | {path for path in written if path not in tracked})
        job["output_digests"] = {**owned, **{path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in written}}
        job["output_scope"] = "design-mockups" if design else "tests-only"
        write_text(out / "builder_summary.md", job["builder_summary"] + "\n\nFiles:\n" + "\n".join(f"- {path}" for path in written))
        if design:
            for path in written:
                # write_files has validated every design output as an HTML or SVG mockup.
                ref = attach_reference_artifact(job, path, note="Generated visual mockup")
                ref["role"] = "design"
                job.setdefault("reference_artifacts", []).append(ref)
            job["design_artifacts"] = written
        else:
            job["test_validation"] = {"kind": "pending", "passed": False}
            write_json(job_path, job)  # the files are on disk now; a stop from here on resumes at checking them
            run_validation(job, out)
        job.update(status="review-needed", last_error=None, human_clarification_question=None)
    except Exception as exc:
        job.update(status="human-needed", last_error=str(exc))
        print(f"Design/Tests job stopped: {exc}")
    finally:
        job.update(worker_pid=None, updated_at=now_iso())
        write_json(job_path, job)
