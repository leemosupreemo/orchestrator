#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import subprocess
import shlex
import time
from pathlib import Path

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from common import (
    ROOT,
    OUTPUT_DIR,
    PROJECT_CONFIG,
    timestamp,
    write_text,
    extract_commands,
    JOBS_DIR,
    read_json,
    extract_destination_from_command,
    command_with_destination,
    get_fallback_simulator_destinations,
    parse_test_output,
)


def strip_xcode_test_plan(cmd: str) -> str:
    """Remove -testPlan <name> so focused -only-testing flags control the run."""
    parts = shlex.split(cmd)
    cleaned: list[str] = []
    i = 0
    while i < len(parts):
        if parts[i] == "-testPlan":
            i += 2
            continue
        cleaned.append(parts[i])
        i += 1
    return shlex.join(cleaned)


def cleanup_logs(manual_base: Path, keep: int = 3):
    """Keeps only the 'keep' most recent log directories, UNLESS they are linked to an active job."""
    linked_paths = set()
    for f in JOBS_DIR.glob("*.json"):
        try:
            job_data = read_json(f)
            paths = job_data.get("last_manual_log_paths", [])
            for p in paths:
                full_p = ROOT / p if not Path(p).is_absolute() else Path(p)
                linked_paths.add(str(full_p.resolve()))
        except:
            continue

    dirs = sorted([d for d in manual_base.iterdir() if d.is_dir() and d.name != "latest"], key=lambda x: x.name)
    keep_dirs = set(dirs[-keep:]) if len(dirs) >= keep else set(dirs)
    
    for d in dirs:
        if str(d.resolve()) in linked_paths:
            keep_dirs.add(d)
    
    for d in dirs:
        if d not in keep_dirs:
            print(f"      - Cleaning up old UNLINKED manual logs: {d.name}")
            import shutil
            shutil.rmtree(d)


def print_execution_header(cmd: str, log_file: Path, mode: str = "run") -> None:
    """Displays a structured execution banner before starting a build or test run."""
    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = 80
    box_width = max(55, min(75, cols - 2))

    is_test = mode in ["test", "both"] or ("xcodebuild" in cmd and "test" in cmd) or ("swift test" in cmd)
    is_build = mode in ["build"] or ("xcodebuild" in cmd and "build" in cmd)

    try:
        rel_log = str(log_file.relative_to(ROOT))
    except Exception:
        rel_log = str(log_file)

    dest = extract_destination_from_command(cmd)

    target_info = None
    if is_test:
        if "-only-testing:" in cmd:
            tests = [p.split()[0] for p in cmd.split("-only-testing:")[1:]]
            target_info = ", ".join(tests)
        elif PROJECT_CONFIG.test_target:
            target_info = f"All Unit Tests ({PROJECT_CONFIG.test_target})"
        else:
            target_info = "All Unit Tests"

    import shutil
    has_xcbeautify = shutil.which("xcbeautify") is not None and "xcodebuild" in cmd
    formatter_info = "xcbeautify (active)" if has_xcbeautify else ("raw xcodebuild" if "xcodebuild" in cmd else "standard")

    header_title = "🧪 TEST EXECUTION PIPELINE" if is_test else ("🔨 BUILD EXECUTION PIPELINE" if is_build else "🚀 COMMAND EXECUTION PIPELINE")

    print("\n\033[1;96m" + "=" * box_width)
    print(f"  {header_title}")
    print("=" * box_width + "\033[0m")
    if target_info:
        print(f"  • Target/Suite:   \033[1;97m{target_info}\033[0m")
    if dest:
        print(f"  • Destination:    \033[1;93m{dest}\033[0m")
    print(f"  • Formatter:      \033[1;90m{formatter_info}\033[0m")
    print(f"  • Live Log:       \033[94m{rel_log}\033[0m")
    print("\033[1;96m" + "-" * box_width + "\033[0m\n", flush=True)


def print_test_results_summary(
    full_output: str,
    log_file: Path,
    duration_sec: float,
    exit_code: int,
) -> None:
    """Displays a comprehensive test execution outcome banner."""
    parsed = parse_test_output(full_output)
    total = parsed.get("total_run", 0)
    passed = parsed.get("passed_count", 0)
    failed = parsed.get("failed_count", 0)
    failing_tests = parsed.get("failing_tests", [])

    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = 80
    box_width = max(55, min(75, cols - 2))

    dur_str = f"{duration_sec:.1f}s" if duration_sec < 60 else f"{int(duration_sec // 60)}m {duration_sec % 60:.1f}s"

    try:
        rel_log = str(log_file.relative_to(ROOT))
    except Exception:
        rel_log = str(log_file)

    if exit_code == 0 and failed == 0:
        print("\n\033[1;92m" + "=" * box_width)
        print(f"  ✅ TEST RUN SUCCEEDED  (Duration: {dur_str})")
        print("=" * box_width + "\033[0m")
        if total > 0:
            print(f"  • Status:     \033[1;92mPASSED\033[0m (All {total} tests passed)")
            print(f"  • Total Run:  \033[1;97m{total}\033[0m")
            print(f"  • Passed:     \033[1;92m{passed}\033[0m (100.0%)")
            print(f"  • Failed:     \033[1;90m0\033[0m")
        else:
            print(f"  • Status:     \033[1;92mPASSED\033[0m")
        print(f"  • Full Log:   \033[94m{rel_log}\033[0m")
        print("\033[1;92m" + "=" * box_width + "\033[0m\n", flush=True)
    else:
        print("\n\033[1;91m" + "=" * box_width)
        if total > 0 or failed > 0:
            print(f"  ❌ TEST RUN FAILED  (Duration: {dur_str})")
            print("=" * box_width + "\033[0m")
            print(f"  • Status:     \033[1;91mFAILED\033[0m (Exit code: {exit_code})")
            print(f"  • Total Run:  \033[1;97m{total}\033[0m")
            print(f"  • Passed:     \033[1;92m{passed}\033[0m")
            print(f"  • Failed:     \033[1;91m{failed}\033[0m")
            if failing_tests:
                print(f"\n  \033[1;91mFailing Test Case(s) ({len(failing_tests)}):\033[0m")
                for t in failing_tests[:15]:
                    print(f"    \033[1;91m✖\033[0m \033[97m{t}\033[0m")
                if len(failing_tests) > 15:
                    print(f"    \033[1;90m... and {len(failing_tests) - 15} more (see full log)\033[0m")
        else:
            print(f"  ❌ TEST EXECUTION FAILED BEFORE TESTS RAN  (Duration: {dur_str})")
            print("=" * box_width + "\033[0m")
            print(f"  • Status:     \033[1;91mPRE-FLIGHT / COMPILATION FAILURE\033[0m (Exit code: {exit_code})")
            print(f"  • Details:    Build compilation or simulator runner failed before test execution.")
        print(f"\n  • Full Log:   \033[94m{rel_log}\033[0m")
        print("\033[1;91m" + "=" * box_width + "\033[0m\n", flush=True)


def print_build_results_summary(
    full_output: str,
    log_file: Path,
    duration_sec: float,
    exit_code: int,
) -> None:
    """Displays a build execution outcome banner."""
    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = 80
    box_width = max(55, min(75, cols - 2))

    dur_str = f"{duration_sec:.1f}s" if duration_sec < 60 else f"{int(duration_sec // 60)}m {duration_sec % 60:.1f}s"
    try:
        rel_log = str(log_file.relative_to(ROOT))
    except Exception:
        rel_log = str(log_file)

    if exit_code == 0:
        print("\n\033[1;92m" + "=" * box_width)
        print(f"  ✅ BUILD SUCCEEDED  (Duration: {dur_str})")
        print("=" * box_width + "\033[0m")
        print(f"  • Status:     \033[1;92mSUCCESS\033[0m")
        print(f"  • Full Log:   \033[94m{rel_log}\033[0m")
        print("\033[1;92m" + "=" * box_width + "\033[0m\n", flush=True)
    else:
        print("\n\033[1;91m" + "=" * box_width)
        print(f"  ❌ BUILD FAILED  (Duration: {dur_str})")
        print("=" * box_width + "\033[0m")
        print(f"  • Status:     \033[1;91mFAILED\033[0m (Exit code: {exit_code})")
        print(f"  • Full Log:   \033[94m{rel_log}\033[0m")
        print("\033[1;91m" + "=" * box_width + "\033[0m\n", flush=True)


def stream_command(
    cmd: str,
    log_file: Path,
    is_retry: bool = False,
    attempted_dests: set[str] | None = None,
    mode: str = "run",
) -> bool:
    """Runs a command and streams output to both terminal and file in real-time.
    Automatically retries with fallback simulator destinations if xcodebuild destination mismatch occurs."""
    if attempted_dests is None:
        attempted_dests = set()

    current_dest = extract_destination_from_command(cmd)
    if current_dest:
        attempted_dests.add(current_dest)

    if not is_retry:
        print_execution_header(cmd, log_file, mode=mode)
    else:
        print(f"🚀 Retrying Execution: {cmd}", flush=True)
        print(f"📝 Logging to: {log_file.relative_to(ROOT)}", flush=True)
    
    start_time = time.monotonic()

    import shutil
    has_xcbeautify = shutil.which("xcbeautify") is not None and "xcodebuild" in cmd

    formatter_proc = None
    if has_xcbeautify:
        try:
            formatter_proc = subprocess.Popen(
                ["xcbeautify"],
                stdin=subprocess.PIPE,
                stdout=sys.stdout,
                stderr=sys.stderr,
                text=True,
                bufsize=1
            )
        except Exception:
            formatter_proc = None

    captured_lines: list[str] = []
    with open(log_file, "w", encoding="utf-8") as f:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=True,
            cwd=str(ROOT),
            text=True,
            bufsize=1,
            executable="/bin/bash"
        )
        
        for line in process.stdout:
            f.write(line)
            f.flush()
            captured_lines.append(line)
            if formatter_proc and formatter_proc.stdin:
                try:
                    formatter_proc.stdin.write(line)
                    formatter_proc.stdin.flush()
                except Exception:
                    sys.stdout.write(line)
                    sys.stdout.flush()
            else:
                sys.stdout.write(line)
                sys.stdout.flush()
            
        process.wait()

        if formatter_proc and formatter_proc.stdin:
            try:
                formatter_proc.stdin.close()
                formatter_proc.wait()
            except Exception:
                pass

        duration_sec = time.monotonic() - start_time
        full_output = "".join(captured_lines)

        # If xcodebuild failed due to destination mismatch, attempt automated fallback retry
        if process.returncode != 0 and "xcodebuild" in cmd and "Unable to find a device matching the provided destination specifier" in full_output:
            fallbacks = get_fallback_simulator_destinations(current_dest)
            for candidate in fallbacks:
                if candidate in attempted_dests:
                    continue
                attempted_dests.add(candidate)
                print(f"\n\033[1;93m⚠️  Simulator destination '{current_dest}' rejected by xcodebuild.\033[0m")
                print(f"\033[1;96m🔄 Retrying automatically with fallback destination: {candidate}...\033[0m\n", flush=True)
                new_cmd = command_with_destination(cmd, candidate)
                if stream_command(new_cmd, log_file, is_retry=True, attempted_dests=attempted_dests, mode=mode):
                    return True

        # Print final execution results summary
        is_test = mode in ["test", "both"] or ("xcodebuild" in cmd and "test" in cmd) or ("swift test" in cmd)
        is_build = mode in ["build"] or ("xcodebuild" in cmd and "build" in cmd)

        if is_test:
            print_test_results_summary(full_output, log_file, duration_sec, process.returncode)
        elif is_build:
            print_build_results_summary(full_output, log_file, duration_sec, process.returncode)

        return process.returncode == 0


def capture_logs(manual_out: Path) -> Path | None:
    """Prompts the user to paste logs and saves them to manual_out / 'captured.log'."""
    print("\n📥 Paste your logs below. When finished, press Ctrl-D on a new line.", flush=True)
    try:
        logs = sys.stdin.read()
    except EOFError:
        logs = ""

    if logs.strip():
        log_file = manual_out / "captured.log"
        write_text(log_file, logs)
        print(f"✅ Logs captured to captured.log")
        return log_file
    else:
        print("⚠️  No logs provided. Skipping.")
        return None


def main():
    parser = argparse.ArgumentParser(description="Run manual build/tests and save logs.")
    parser.add_argument("mode", choices=["build", "test", "both", "run", "capture"], help="What to run")
    parser.add_argument("--test-only", help="Additional -only-testing flags for xcodebuild")
    parser.add_argument("--test-command", help="Exact command for a selected non-Xcode test suite")
    parser.add_argument("--run-cmd", help="Custom command to run for 'run' mode")
    args = parser.parse_args()

    ts = timestamp()
    manual_base = OUTPUT_DIR / "manual"
    manual_out = manual_base / ts
    manual_out.mkdir(parents=True, exist_ok=True)
    
    # Update 'latest' symlink
    latest_link = manual_base / "latest"
    if latest_link.exists() or latest_link.is_symlink():
        latest_link.unlink()
    try:
        latest_link.symlink_to(ts, target_is_directory=True)
    except Exception as e:
        print(f"Warning: Could not create 'latest' symlink: {e}")

    if args.mode in {"build", "test", "both"}:
        build_cmd, test_cmd = extract_commands()

    if args.mode == "capture":
        capture_logs(manual_out)

    if args.mode == "run":
        if not args.run_cmd:
            print("❌ Error: --run-cmd is required for 'run' mode.")
            sys.exit(1)
        success = stream_command(args.run_cmd, manual_out / "run.log", mode="run")
        if not success:
            sys.exit(1)

    if args.mode in ["build", "both"]:
        success = stream_command(build_cmd, manual_out / "build.log", mode="build")
        if not success:
            if args.mode == "both":
                print("Skipping tests due to build failure.")
            sys.exit(1)

    if args.mode in ["test", "both"]:
        final_test_cmd = args.test_command or test_cmd
        if args.test_only:
            if "xcodebuild test" in args.test_only:
                final_test_cmd = args.test_only
            else:
                # Simply append the extra flags to the end of the base test command
                final_test_cmd = f"{strip_xcode_test_plan(test_cmd)} {args.test_only}"

        success = stream_command(final_test_cmd, manual_out / "test.log", mode="test")
        if not success:
            sys.exit(1)

    print(f"\n📝 All logs saved to: {manual_out.relative_to(ROOT)}")
    cleanup_logs(manual_base)


if __name__ == "__main__":
    main()
