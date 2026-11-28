#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import re
import shlex
from pathlib import Path
from typing import Callable

from common import (
    PROJECT_CONFIG,
    DOCS_DIR, 
    OUTPUT_DIR, 
    PROMPTS_DIR,
    ROOT, 
    run_shell, 
    write_text, 
    extract_commands, 
    print_phase, 
    is_disk_full_error, 
    purge_zombie_processes,
    get_best_simulator_destination,
    get_fallback_simulator_destinations,
    command_with_destination,
    extract_destination_from_command,
    timeout_note,
)
from orchestrator.disk_space import low_disk_message
from llm import run_llm
from model_router import ModelRole


class EnvironmentValidationError(RuntimeError):
    """Raised when local machine setup prevents meaningful build/test validation."""


class DiskFullError(RuntimeError):
    """Raised when a build or test still ran out of disk space after cleaning up and retrying. The scheduler reads the
    message (it says "No space left on device") to move the job to another machine."""


def require_build_space() -> None:
    if message := low_disk_message(ROOT, getattr(PROJECT_CONFIG, "runtime_dir", None)):
        raise DiskFullError(message)


def disk_full_message(job: dict, step: str) -> str:
    machine = job.get("assigned_machine") or "local"
    return f"No space left on device: {machine} ran out of disk space while {step}, even after cleaning up."


from orchestrator.env_issues import detect_simulator_environment_issue  # noqa: E402,F401


def nice_timestamp() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

def capture_system_logs(out_path: Path):
    """Captures recent simulator logs if a simulator is booted."""
    try:
        # Try to find a booted simulator
        cmd = ["xcrun", "simctl", "list", "devices", "booted", "--json"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            booted = []
            for runtime in data.get("devices", {}).values():
                for device in runtime:
                    booted.append(device["udid"])
            
            if booted:
                # Capture last 2 minutes of logs from the first booted sim
                udid = booted[0]
                print(f"      - Capturing system logs from simulator {udid}...")
                log_cmd = ["xcrun", "simctl", "spawn", udid, "log", "show", "--last", "2m", "--style", "syslog"]
                log_res = subprocess.run(log_cmd, capture_output=True, text=True)
                if log_res.returncode == 0:
                    write_text(out_path, log_res.stdout)
                    return True
    except Exception as e:
        print(f"      - Warning: Failed to capture system logs: {e}")
    return False

DEFAULT_BUILD_TIMEOUT = 90 * 60
DEFAULT_TEST_TIMEOUT = 60 * 60


def command_timeout(kind: str) -> int | None:
    """Seconds the main `build` or `test` command may run: `build_timeout` / `test_timeout` in project.json, else a default
    long enough for a big Unity import or an Xcode build. 0 turns the limit off. Without one, a hung run blocks a job forever."""
    value = getattr(PROJECT_CONFIG, f"{kind}_timeout", None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value or None
    return DEFAULT_BUILD_TIMEOUT if kind == "build" else DEFAULT_TEST_TIMEOUT


def timeout_report(kind: str, command: str, seconds: int, output: str) -> str:
    """What to tell the fix step (and you) when a build or test run was stopped. Not sent to an AI to analyze: a hang
    has no error to read, and a second look would only wait again."""
    what = "build" if kind == "build" else "tests"
    return (f"# The {what} didn't finish\n\n{timeout_note(seconds)}\n\nCommand: `{command}`\n\n"
            f"This usually means something is waiting and never answering: a test that connects to a server or Docker that isn't "
            f"running, a Unity or Xcode process stuck on a dialog or a lock (is the project open in an editor?), or a loop. "
            f"If it is just slow, raise `{kind}_timeout` (seconds, 0 for no limit) in .orchestrator/project.json.\n\n"
            f"## Last output\n\n```\n{chr(10).join(output.splitlines()[-60:])}\n```\n")


FAILURE_LINE_RE = re.compile(
    r"(?:\berror\s*:\s|\bfatal error:\s|\bFAIL:\s|\bERROR:\s|\bFAILED\b|AssertionError|Traceback \(most recent call last\)|Test case .* failed|Test suite .* failed)",
    re.I,
)
_PASSING_SUMMARY_RE = re.compile(r"^\s*Passed!\s*-\s*Failed:\s*0\b", re.I)


def extract_deterministic_failure_summary(kind: str, output: str) -> str:
    """Fast deterministic extraction of compiler or test failure lines without burning an extra LLM call."""
    lines = (output or "").splitlines()
    matched: list[str] = []
    for idx, line in enumerate(lines):
        if _PASSING_SUMMARY_RE.search(line):
            continue
        if FAILURE_LINE_RE.search(line):
            snippet = "\n".join(lines[idx : min(len(lines), idx + 4)]).strip()
            if snippet and snippet not in matched:
                matched.append(snippet)
            if len(matched) >= 12:
                break
    tail = "\n".join(lines[-35:]).strip()
    header = f"## Deterministic {kind.capitalize()} Failure Summary"
    body: list[str] = [header, ""]
    if matched:
        body.append("### Key Error / Failure Signals")
        for m in matched:
            body.append(f"- `{m.splitlines()[0][:240]}`")
        body.append("")
    if tail:
        body.extend(["### Tail Output", "```", tail, "```"])
    return "\n".join(body) + "\n"


def analyze_failure(kind: str, output: str, *, use_llm: bool = False) -> str:
    """Summarize build/test failures deterministically by default so the autonomous debug loop doesn't burn
    an extra LLM call before `debug_job.py` inspects the raw logs."""
    if not use_llm:
        return extract_deterministic_failure_summary(kind, output)
    prompt = (PROMPTS_DIR / "build_checker.md").read_text(encoding="utf-8")
    analysis, _, session_id = run_llm("gemini", f"{prompt}\n\nFailure type: {kind}\n\nOutput:\n{output}\n", cwd=ROOT, role=ModelRole.REVIEWER)
    return analysis


def extract_xcresult_summary(output: str) -> str:
    """Extracts xcresult path from xcodebuild output and generates a text summary of failures."""
    match = re.search(r"Test session results, code coverage, and logs:\s+(.*\.xcresult)", output)
    if not match:
        return ""
    
    xcresult_path = match.group(1).strip()
    try:
        # Get the summary of the test run
        cmd = ["xcrun", "xcresulttool", "get", "--path", xcresult_path, "--format", "json"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            return f"Failed to run xcresulttool: {res.stderr}"
        
        data = json.loads(res.stdout)
        
        # This is a high-level summary. To get detailed failures, we'd need to traverse the actions.
        # For now, let's just provide the path and the high-level summary if available.
        # A more detailed parsing could be added if needed.
        return f"\n--- XCRESULT DATA ---\nPath: {xcresult_path}\n" + json.dumps(data, indent=2)[:5000] + "\n"
    except Exception as e:
        return f"Error parsing xcresult: {e}"


def resolve_command(cmd: str) -> str:
    """Resolves $(pwd) and injects the best local simulator into a command string."""
    if not cmd: return cmd
    
    # 1. Resolve $(pwd) locally
    cmd = cmd.replace("$(pwd)", str(ROOT))
    
    # 2. Inject/Replace local simulator destination if it's an xcodebuild command
    if "xcodebuild" in cmd:
        try:
            dest = get_best_simulator_destination()
            return command_with_destination(cmd, dest)
        except Exception:
            return cmd
    return cmd


def uses_configured_test_tool(command: str, base_command: str) -> bool:
    """Recognize focused commands using the project's configured executable.

    This is a format check, not a sandbox: commands already run as shell code.
    Reject malformed quoting and prose instead of maintaining a language list.
    """
    try:
        command_parts = shlex.split(command)
        base_parts = shlex.split(base_command)
    except ValueError:
        return False
    return bool(command_parts and base_parts and command_parts[0] == base_parts[0])


OUTPUT_DIR_ENV = "ORCHESTRATOR_OUTPUT_DIR"
NO_TESTS_NEEDED = {"n/a", "none", "no tests", "no specific tests", "skipped", "validated"}


def run_build_and_tests(
    job: dict, summary_file: Path, *, job_owned: bool = False, is_current: Callable[[], bool] | None = None,
) -> tuple[bool, bool]:
    """Build and run the project's tests: the main `test_command`, then the extra `test_commands` once it passes."""
    job.pop("_test_execution", None)
    # Where a project's own checks can leave pictures and reports for this job; the job page shows them.
    if job.get("job_id"):
        os.environ[OUTPUT_DIR_ENV] = str((OUTPUT_DIR / job["job_id"]).resolve())
    build_ok, test_ok = run_main_build_and_tests(job, summary_file)
    if not (build_ok and test_ok):
        return build_ok, test_ok
    if (getattr(PROJECT_CONFIG, "verification_mode", "off") != "assist"
            and (job.get("test_command_override") or "").strip().lower() in NO_TESTS_NEEDED):
        return build_ok, test_ok  # the job said no tests apply; extra suites are no exception
    return build_ok, run_extra_test_commands(job, job_owned=job_owned, is_current=is_current)


def run_extra_test_commands(
    job: dict, *, job_owned: bool = False, is_current: Callable[[], bool] | None = None,
) -> bool:
    """Run the per-job `test_commands` (and `backend_test_command`). Blocked ones are reported and don't fail the job."""
    from orchestrator import test_commands
    commands, errors = test_commands.from_config(PROJECT_CONFIG)
    for error in errors:
        print(f"      - Warning: {error}")
    mode = getattr(PROJECT_CONFIG, "verification_mode", "off")
    out_dir = OUTPUT_DIR / job["job_id"]
    if mode == "assist" and errors:
        write_text(out_dir / "test.log", "\n".join(errors) + "\n")
        return False
    if not any(c.tier == "per-job" for c in commands):
        return True
    print_phase("testing", subtext="extra test commands")
    if mode == "assist":
        from orchestrator import verification_assist

        results = verification_assist.run(
            commands, ROOT, out_dir, PROJECT_CONFIG.runtime_dir, job,
            job_owned=job_owned, is_current=is_current or (lambda: True),
        )
    elif not errors and mode == "observe":
        from orchestrator import verification

        results = verification.observe(commands, ROOT, out_dir, job["job_id"])
    else:
        results = test_commands.run_all(commands, ROOT, out_dir)
    print("      - Extra test commands:\n" + test_commands.summary(results))
    if test_commands.passed(results):
        return True
    report = test_commands.failure_report(results)
    write_text(out_dir / "test.log", report)  # what the fix step and the failure views read
    write_text(out_dir / f"test_analysis_{nice_timestamp()}.md", analyze_failure("test", report))
    return False


def run_main_build_and_tests(job: dict, summary_file: Path) -> tuple[bool, bool]:
    out_dir = OUTPUT_DIR / job["job_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    
    ts = nice_timestamp()

    likely_files = job.get("plan", {}).get("likely_files", [])
    is_infra = any("orchestrator/" in f or "scripts/" in f for f in likely_files)
    
    raw_build_cmd, raw_test_cmd = extract_commands()

    # Determine test command first to see if we should skip build
    test_cmd = raw_test_cmd
    override = job.get("test_command_override")
    fell_back = False
    
    is_standalone_test = False
    if override:
        o = override.strip().lower()
        # Robust N/A check
        is_na = o in NO_TESTS_NEEDED
        
        from orchestrator import builder_check
        missing_targets = [] if is_na else builder_check.missing_test_targets(override, ROOT)
        source_only_targets = [] if (is_na or missing_targets) else builder_check.source_only_test_files(override, ROOT)
        if is_na:
            test_cmd = None
        elif missing_targets:
            # The builder named a test it never wrote (or wrote elsewhere). Running it finds nothing, so run the project's own tests instead.
            print(f"      - Warning: ignoring the builder's test command, {', '.join(missing_targets)} doesn't exist. Running the project's tests instead.")
            test_cmd = raw_test_cmd
            fell_back = True
        elif source_only_targets and raw_test_cmd and override.strip() != raw_test_cmd.strip():
            # Don't let source-text substring assertions silently replace the repository's configured test suite.
            print(f"      - Warning: ignoring source-text-only test override ({', '.join(source_only_targets)}). Running the project's tests instead.")
            test_cmd = raw_test_cmd
            fell_back = True
        elif "python3" in override or "pytest" in override or "sh " in override or "./" in override:
            test_cmd = override
            is_standalone_test = True
        elif "xcodebuild test" in override:
            test_cmd = override
        elif uses_configured_test_tool(override, raw_test_cmd):
            test_cmd = override
        elif "xcodebuild" in raw_test_cmd and override.startswith(("-only-testing", "-skip-testing")):
            # Traditional -only-testing override
            test_cmd = f"{raw_test_cmd} {override}"
        else:
            # If it's just some random text from the LLM, don't execute it
            print(f"      - Warning: Ignoring malformed test command override: {override}")
            test_cmd = raw_test_cmd
            fell_back = override.strip() != raw_test_cmd.strip()

    # Resolve commands locally on the worker machine
    build_cmd = resolve_command(raw_build_cmd)
    final_test_cmd = resolve_command(test_cmd)

    # Only run xcodebuild build if it's not a standalone non-ios test or if we are forced
    if is_infra and is_standalone_test and "xcodebuild" in build_cmd:
        print_phase("skipping_ios_build", subtext="infra task with standalone tests")
        build_ok = True
    else:
        print_phase("building")
        # Retry loop for disk full
        max_retries = 1
        build_ok = False
        for attempt in range(max_retries + 1):
            print(f"      - Running build: {build_cmd} (Attempt {attempt + 1})")
            build_limit = command_timeout("build")
            build_result = run_shell(build_cmd, cwd=ROOT, check=False, capture=True, timeout=build_limit)

            if getattr(build_result, "timed_out", False):  # don't run a hung build a second time to capture its output
                report = timeout_report("build", build_cmd, build_limit, getattr(build_result, "stdout", "") or "")
                write_text(out_dir / "build.log", report)
                write_text(out_dir / f"build_analysis_{ts}.md", report)
                print(f"      - Build STOPPED: {timeout_note(build_limit)}")
                return False, False

            if build_result.returncode != 0:
                stdout_str = getattr(build_result, "stdout", None) or ""
                stderr_str = getattr(build_result, "stderr", None) or ""
                if not stdout_str and not stderr_str:
                    cap_result = run_shell(build_cmd, cwd=ROOT, check=False, capture=True, timeout=build_limit)
                    stdout_str = getattr(cap_result, "stdout", "") or ""
                    stderr_str = getattr(cap_result, "stderr", "") or ""
                full_output = stdout_str + "\n" + stderr_str

                # If destination mismatch, attempt automated fallback retry
                if "xcodebuild" in build_cmd and "Unable to find a device matching the provided destination specifier" in full_output:
                    curr_dest = extract_destination_from_command(build_cmd)
                    for candidate in get_fallback_simulator_destinations(curr_dest):
                        if candidate == curr_dest: continue
                        print(f"\n\033[1;93m⚠️  Simulator destination rejected during build ({curr_dest}). Retrying with: {candidate}...\033[0m")
                        retry_cmd = command_with_destination(build_cmd, candidate)
                        retry_res = run_shell(retry_cmd, cwd=ROOT, check=False, capture=False, timeout=build_limit)
                        if retry_res.returncode == 0:
                            build_cmd = retry_cmd
                            build_ok = True
                            break
                    if build_ok:
                        write_text(out_dir / "build.log", "** BUILD SUCCEEDED ON FALLBACK DESTINATION **")
                        break

                environment_issue = detect_simulator_environment_issue(full_output) if "xcodebuild" in build_cmd else None
                if environment_issue:
                    write_text(out_dir / "environment_failure.md", environment_issue + "\n\n--- Raw output ---\n" + full_output)
                    write_text(out_dir / "build.log", full_output)
                    raise EnvironmentValidationError(environment_issue)

                if is_disk_full_error(full_output) and attempt < max_retries:
                    print("\n\033[1;93m⚠️  DISK FULL DETECTED. Attempting automated recovery...\033[0m")
                    purge_zombie_processes([job.get("assigned_machine", "local")], silent=False)
                    print("\033[1;92m✅ Cleanup complete. Retrying build...\033[0m\n")
                    continue
                if is_disk_full_error(full_output):
                    write_text(out_dir / "build.log", full_output)
                    raise DiskFullError(disk_full_message(job, "building"))
                print("      - Build FAILED. Analyzing errors...")
                analysis = analyze_failure("build", full_output)
                write_text(out_dir / f"build_analysis_{ts}.md", analysis)
                write_text(out_dir / "build.log", full_output)
                return False, False
            build_ok = True
            write_text(out_dir / "build.log", "** BUILD SUCCEEDED **")
            break

    if not final_test_cmd:
        print_phase("skipping_tests", subtext="no test command provided or N/A")
        return build_ok, True

    print_phase("testing")
    
    # Retry loop for disk full during test execution
    max_retries = 1
    test_ok = False
    from orchestrator import builder_check
    own_test_cmd = resolve_command(raw_test_cmd)
    full_suite = " ".join(final_test_cmd.split()) == " ".join(own_test_cmd.split())
    for attempt in range(max_retries + 2):  # one extra pass, for the fall back to the project's own tests below
        print(f"      - Running tests: {final_test_cmd} (Attempt {attempt + 1})")
        test_limit = command_timeout("test")
        test_result = run_shell(final_test_cmd, cwd=ROOT, check=False, capture=True, timeout=test_limit)
        full_test_output = test_result.stdout + "\n\nSTDERR:\n" + test_result.stderr
        if getattr(test_result, "timed_out", False):
            report = timeout_report("test", final_test_cmd, test_limit, test_result.stdout)
            write_text(out_dir / f"test_{ts}.log", full_test_output)
            write_text(out_dir / "test.log", report + "\n\n" + full_test_output)
            write_text(out_dir / f"test_analysis_{ts}.md", report)
            print(f"      - Tests STOPPED: {timeout_note(test_limit)}")
            return build_ok, False
        
        # If destination mismatch, attempt automated fallback retry
        if test_result.returncode != 0 and "xcodebuild" in final_test_cmd and "Unable to find a device matching the provided destination specifier" in full_test_output:
            curr_dest = extract_destination_from_command(final_test_cmd)
            for candidate in get_fallback_simulator_destinations(curr_dest):
                if candidate == curr_dest: continue
                print(f"\n\033[1;93m⚠️  Simulator destination rejected during testing ({curr_dest}). Retrying with: {candidate}...\033[0m")
                retry_cmd = command_with_destination(final_test_cmd, candidate)
                retry_res = run_shell(retry_cmd, cwd=ROOT, check=False, capture=True, timeout=test_limit)
                full_test_output = retry_res.stdout + "\n\nSTDERR:\n" + retry_res.stderr
                if retry_res.returncode == 0:
                    test_result = retry_res
                    final_test_cmd = retry_cmd
                    break

        if test_result.returncode != 0 and is_disk_full_error(full_test_output) and attempt < max_retries:
            print("\n\033[1;93m⚠️  DISK FULL DETECTED during tests. Attempting automated recovery...\033[0m")
            purge_zombie_processes([job.get("assigned_machine", "local")], silent=False)
            print("\033[1;92m✅ Cleanup complete. Retrying tests...\033[0m\n")
            continue
        if test_result.returncode != 0 and is_disk_full_error(full_test_output):
            write_text(out_dir / "test.log", full_test_output)
            raise DiskFullError(disk_full_message(job, "running tests"))
        
        write_text(out_dir / f"test_{ts}.log", full_test_output)
        write_text(out_dir / "test.log", full_test_output)

        if builder_check.no_tests_ran(full_test_output) and own_test_cmd and final_test_cmd != own_test_cmd:
            print("      - The builder's test command found no tests. Running the project's own tests instead.")
            final_test_cmd = own_test_cmd
            fell_back = True
            full_suite = True
            continue

        if test_result.returncode == 0:
            if not builder_check.no_tests_ran(full_test_output):
                job["_test_execution"] = {"command": final_test_cmd, "full_suite": full_suite,
                                          "fell_back": fell_back}
            print("      - Tests passed.")
            return True, True

        if test_result.returncode != 0:
            environment_issue = detect_simulator_environment_issue(full_test_output) if "xcodebuild" in final_test_cmd else None
            if environment_issue:
                write_text(out_dir / "environment_failure.md", environment_issue + "\n\n--- Raw output ---\n" + full_test_output)
                raise EnvironmentValidationError(environment_issue)

            print("\n\033[1;91m" + "!"*60)
            print("  ❌  TESTS FAILED. Analyzing failures...  ❌")
            print("!"*60 + "\033[0m\n")
            
            # ... (rest of the failure analysis)
        
        # Extract specific failures for immediate console visibility
        failures = []
        for line in test_result.stdout.splitlines():
            if "Test case" in line and "failed" in line:
                failures.append(line.strip())
            elif "Test suite" in line and "failed" in line:
                failures.append(line.strip())
        
        if failures:
            print("\n--- Failure Summary ---")
            for f in failures[:10]: # Don't flood the console if many failed
                print(f"  {f}")
            if len(failures) > 10:
                print(f"  ... and {len(failures) - 10} more")
            print("-----------------------\n")

        if builder_check.no_tests_ran(full_test_output):
            # A test-setup problem, not a code failure: say so directly instead of asking a model to guess from an empty run.
            analysis = (f"## No tests ran\n\nThe test command `{final_test_cmd}` found no tests to run, so nothing was checked. "
                        "This is a problem with which tests were selected, not with the code: the named test files don't exist, "
                        "or the discovery path or pattern matches nothing. Fix the test command (Settings, or the job's test selection), or create the tests.")
        else:
            analysis = analyze_failure("test", full_test_output)
        
        print(f"--- AI Analysis Summary ---")
        # Print first few lines of analysis to provide immediate context
        print("\n".join(analysis.splitlines()[:15]))
        print("... (Full analysis in test_analysis.md)")
        print("---------------------------\n")

        # Extract xcresult summary
        xc_summary = extract_xcresult_summary(test_result.stdout) if "xcodebuild" in final_test_cmd else None
        if xc_summary:
            write_text(out_dir / f"test_results_{ts}.txt", xc_summary)

        write_text(out_dir / f"test_analysis_{ts}.md", analysis)
        
        # Capture system logs on failure
        if "xcodebuild" in final_test_cmd:
            capture_system_logs(out_dir / f"system_{ts}.log")
        
        return True, False

    print("      - Tests passed.")
    return True, True
