#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT.parent))

from orchestrator.project_config import PROJECT_CONFIG

ROOT = PROJECT_CONFIG.root
ORCHESTRATOR_DIR = PACKAGE_ROOT
ORCHESTRATOR_RUNTIME_DIR = PROJECT_CONFIG.runtime_dir
CONFIG_DIR = ORCHESTRATOR_RUNTIME_DIR / "config"
JOBS_DIR = ORCHESTRATOR_RUNTIME_DIR / "jobs"
INBOX_DIR = JOBS_DIR / "inbox"
ARCHIVE_DIR = JOBS_DIR / "archive"
LOGS_DIR = ORCHESTRATOR_RUNTIME_DIR / "logs"
OUTPUT_DIR = ORCHESTRATOR_RUNTIME_DIR / "output"
STATE_DIR = ORCHESTRATOR_RUNTIME_DIR / "state"
MACHINE_STATE_DIR = STATE_DIR / "machines"
PROMPTS_DIR = PROJECT_CONFIG.prompts_dir
DOCS_DIR = ROOT / "docs"

# Force line-buffering for stdout/stderr to ensure interactive scripts work well over pipes/subprocesses
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
except:
    pass

for p in [CONFIG_DIR, JOBS_DIR, INBOX_DIR, ARCHIVE_DIR, LOGS_DIR, OUTPUT_DIR, STATE_DIR, MACHINE_STATE_DIR]:
    p.mkdir(parents=True, exist_ok=True)

def load_secrets():
    """Loads environment variables from .secrets/project-secrets.zsh or .env if present."""
    secrets_paths = [
        ROOT / ".secrets" / "project-secrets.zsh",
        ORCHESTRATOR_RUNTIME_DIR / ".env",
        ROOT / ".env"
    ]
    for sp in secrets_paths:
        if sp.exists():
            try:
                content = sp.read_text(encoding="utf-8")
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith("export "):
                        line = line[7:].strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass

load_secrets()

def ensure_keychain_unlocked(prompt_if_missing: bool = False) -> tuple[bool, str]:
    """Ensures macOS login keychain is unlocked using KEYCHAIN_PASSWORD if present or prompting upfront if required."""
    load_secrets()
    pwd = os.environ.get("KEYCHAIN_PASSWORD")
    keychain_path = os.path.expanduser("~/Library/Keychains/login.keychain-db")
    if not os.path.exists(keychain_path):
        keychain_path = os.path.expanduser("~/Library/Keychains/login.keychain")
    
    if pwd:
        res = subprocess.run(
            ["security", "unlock-keychain", "-p", pwd, keychain_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True
        )
        if res.returncode == 0:
            subprocess.run(
                ["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", pwd, keychain_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return True, "Keychain unlocked successfully using saved KEYCHAIN_PASSWORD."
        else:
            err = res.stderr.strip() or "Invalid password"
            return False, f"Failed to unlock keychain with saved password: {err}"
    
    # Check if already unlocked
    res = subprocess.run(
        ["security", "show-keychain-info", keychain_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if res.returncode == 0:
        return True, "Keychain is already unlocked."
        
    if prompt_if_missing:
        print("\n\033[1;93m🔑 macOS Login Keychain is locked.\033[0m")
        print("\033[90mTo avoid pausing or failing during build archiving/distribution, please enter your keychain password now:\033[0m")
        import getpass
        try:
            input_pwd = getpass.getpass("Keychain Password: ")
        except Exception:
            input_pwd = ""
        
        if input_pwd:
            res = subprocess.run(
                ["security", "unlock-keychain", "-p", input_pwd, keychain_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True
            )
            if res.returncode == 0:
                subprocess.run(
                    ["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", input_pwd, keychain_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                secrets_path = ROOT / ".secrets" / "project-secrets.zsh"
                secrets_path.parent.mkdir(parents=True, exist_ok=True)
                content = secrets_path.read_text(encoding="utf-8") if secrets_path.exists() else ""
                lines = [l for l in content.splitlines() if "KEYCHAIN_PASSWORD=" not in l]
                lines.append(f'export KEYCHAIN_PASSWORD="{input_pwd}"')
                secrets_path.write_text("\n".join(lines) + "\n")
                os.environ["KEYCHAIN_PASSWORD"] = input_pwd
                return True, "Keychain unlocked and password saved for headless builds."
            else:
                return False, "Provided password failed to unlock keychain."
                
    return False, "Keychain is locked and KEYCHAIN_PASSWORD is not configured."

def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")

def format_log_path(path_str: str) -> str:
    """Formats a path string with a timestamp (YYYYMMDD-HHMMSS or YYYY-MM-DD_HH-MM-SS or variants) to be more readable."""
    # Match hyphenated/underscored timestamps: YYYY-MM-DD[-_T]HH-MM-SS or YYYY-MM-DD[-_T]HH:MM:SS
    pattern_sep = r"(\d{4})-(\d{2})-(\d{2})[-_T](\d{2})[-:](\d{2})[-:](\d{2})"
    # Match compact timestamps: YYYYMMDD[-T]HHMMSS
    pattern_compact = r"(\d{4})(\d{2})(\d{2})[-T](\d{2})(\d{2})(\d{2})"
    
    def replacement(m):
        # Format as YYYY-MM-DD HH:MM:SS
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"
    
    res = re.sub(pattern_sep, replacement, path_str)
    return re.sub(pattern_compact, replacement, res)

def now_iso() -> str:
    return datetime.now().isoformat()


def record_clarification(job: dict[str, Any], question: str, answer: str) -> dict[str, Any]:
    """Records a clarification Q&A entry into the job's persistent history and clears the active question."""
    if not isinstance(job, dict):
        return job
    entry = {
        "question": question.strip() if isinstance(question, str) else str(question),
        "answer": answer.strip() if isinstance(answer, str) else str(answer),
        "timestamp": now_iso(),
    }
    history = job.setdefault("clarification_history", [])
    history.append(entry)
    job["human_clarification_question"] = None
    job["last_error"] = None
    job["updated_at"] = now_iso()
    return job


def format_clarification_history(history: list[dict[str, Any]]) -> str:
    """Formats clarification history as markdown for injecting into LLM prompts."""
    if not history:
        return ""
    lines = ["### User Clarifications & Technical Decisions"]
    for idx, item in enumerate(history, 1):
        q = item.get("question", "")
        a = item.get("answer", "")
        lines.append(f"{idx}. **Question**: {q}")
        lines.append(f"   **Answer / Decision**: {a}")
    return "\n".join(lines)


def record_interactive_investigation(
    job: dict[str, Any],
    tool: str,
    cli_key: str,
    duration: str,
    notes: str = "",
    new_commits: list[str] | None = None,
    duration_seconds: int = 0,
) -> dict[str, Any]:
    """Records an interactive AI CLI session and developer notes into the job and persists an investigations markdown log."""
    ts = now_iso()
    new_commits_list = list(new_commits or [])
    notes_clean = notes.strip() if isinstance(notes, str) else ""

    session_entry = {
        "tool": tool,
        "cli_key": cli_key,
        "timestamp": ts,
        "duration": duration,
        "duration_seconds": duration_seconds,
        "notes": notes_clean,
        "new_commits": new_commits_list,
    }
    investigations = job.setdefault("interactive_investigations", [])
    investigations.append(session_entry)

    if notes_clean:
        notes_list = job.setdefault("investigation_notes", [])
        notes_list.append({
            "tool": tool,
            "note": notes_clean,
            "timestamp": ts,
            "commits": new_commits_list,
        })

    # Record into llm_sessions
    import time
    session_id = f"cli-{cli_key}-{int(time.time())}"
    job.setdefault("llm_sessions", []).append({
        "id": session_id,
        "model": tool,
        "timestamp": ts,
        "tool": cli_key,
    })

    job["updated_at"] = ts

    # Write to .orchestrator/output/<job_id>/investigations.md
    job_id = job.get("job_id")
    if job_id:
        out_dir = OUTPUT_DIR / job_id
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            inv_file = out_dir / "investigations.md"

            issue_num = job.get("issue_number", "")
            title = job.get("title", "")

            md_lines = [
                f"# Interactive Investigation Log for Job #{issue_num}: {title}",
                f"**Job ID**: `{job_id}`",
                f"**Updated**: {ts}",
                "",
            ]
            for idx, item in enumerate(investigations, 1):
                i_tool = item.get("tool", "AI CLI")
                i_dur = item.get("duration", "")
                i_ts = item.get("timestamp", "")
                i_notes = item.get("notes", "") or item.get("note", "")
                i_commits = item.get("new_commits", [])

                md_lines.append(f"## Session {idx}: {i_tool} ({i_ts[:16].replace('T', ' ') if i_ts else 'N/A'})")
                if i_dur:
                    md_lines.append(f"- **Duration**: {i_dur}")
                md_lines.append(f"- **Notes / Findings**: {i_notes if i_notes else '(No notes captured)'}")
                if i_commits:
                    md_lines.append(f"- **Commits Made**:")
                    for c in i_commits:
                        md_lines.append(f"  - `{c}`")
                md_lines.append("")

            inv_file.write_text("\n".join(md_lines), encoding="utf-8")
        except Exception:
            pass

    return job


def format_investigation_history(job_or_investigations: dict[str, Any] | list[dict[str, Any]] | None) -> str:
    """Formats interactive investigation sessions and developer notes as markdown for injecting into LLM prompts and briefs."""
    if not job_or_investigations:
        return ""

    if isinstance(job_or_investigations, dict):
        investigations = job_or_investigations.get("interactive_investigations", [])
        notes = job_or_investigations.get("investigation_notes", [])
    elif isinstance(job_or_investigations, list):
        investigations = job_or_investigations
        notes = []
    else:
        return ""

    if not investigations and not notes:
        return ""

    lines = ["### 🔍 Interactive Investigation Findings & CLI Notes"]
    if investigations:
        for idx, inv in enumerate(investigations, 1):
            tool = inv.get("tool") or inv.get("cli_key", "AI CLI")
            ts = inv.get("timestamp", "")
            duration = inv.get("duration", "")
            meta_parts = [tool]
            if duration:
                meta_parts.append(duration)
            if ts:
                meta_parts.append(ts[:16].replace("T", " "))
            header = f"{idx}. **{' | '.join(meta_parts)}**"
            lines.append(header)

            note_content = inv.get("notes") or inv.get("note")
            if note_content:
                lines.append(f"   - **Findings/Notes**: {note_content}")

            commits = inv.get("new_commits", [])
            if commits:
                lines.append(f"   - **Commits Made**:")
                for c in commits:
                    lines.append(f"     - `{c}`")
    elif notes:
        for idx, item in enumerate(notes, 1):
            if isinstance(item, dict):
                tool = item.get("tool", "AI CLI")
                note_text = item.get("note", "")
                ts = item.get("timestamp", "")
                prefix = f"{idx}. **{tool}**"
                if ts:
                    prefix += f" ({ts[:16].replace('T', ' ')})"
                lines.append(f"{prefix}: {note_text}")
                commits = item.get("commits", [])
                if commits:
                    for c in commits:
                        lines.append(f"   - Commit: `{c}`")
            else:
                lines.append(f"{idx}. {item}")

    return "\n".join(lines)


def count_created_tests_in_diff(branch: str | None, base: str = "main") -> int:
    """Counts newly added test methods or @Test functions in the job branch."""
    if not branch:
        return 0
    try:
        res = subprocess.run(
            ["git", "diff", f"{base}...{branch}", "-U0"],
            capture_output=True,
            text=True,
            cwd=str(ROOT)
        )
        if res.returncode != 0:
            return 0
        count = 0
        for line in res.stdout.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                stripped = line[1:].strip()
                # Swift test function, Python test function, or swift-testing @Test
                if (stripped.startswith("func test") or
                    stripped.startswith("func Test") or
                    stripped.startswith("def test_") or
                    stripped.startswith("@Test")):
                    count += 1
        return count
    except Exception:
        return 0


def parse_test_output(test_output_text: str) -> dict[str, Any]:
    """Parses unit test execution output (Swift SPM, xcodebuild, xcbeautify, unittest, pytest) for counts and failures."""
    if not test_output_text:
        return {
            "total_run": 0,
            "passed_count": 0,
            "failed_count": 0,
            "failing_tests": [],
        }

    total = 0
    failed = 0
    failing_tests: list[str] = []

    # 1. Swift SPM / xcodebuild format: 'Executed 14 tests, with 2 failures'
    spm_match = re.search(r"Executed (\d+) tests?, with (\d+) failures?", test_output_text)
    if spm_match:
        total = int(spm_match.group(1))
        failed = int(spm_match.group(2))

    # 2. Python unittest / pytest format: 'Ran 55 tests in 1.845s'
    unit_match = re.search(r"Ran (\d+) tests?", test_output_text)
    if unit_match and total == 0:
        total = int(unit_match.group(1))
        fail_match = re.search(r"FAILED \((?:failures=(\d+))?(?:,\s*)?(?:errors=(\d+))?\)", test_output_text)
        if fail_match:
            failed = int(fail_match.group(1) or 0) + int(fail_match.group(2) or 0)

    # 3. Extract individual failing test names
    # Swift / Xcode pattern: Test Case '-[PackageTests.ViewModelTests testMethod]' failed
    for m in re.finditer(r"Test [Cc]ase '(?:-\[)?([^\s'\]]+)(?:\s+([^\s'\]]+))?\]?' failed", test_output_text):
        cls_name = m.group(1)
        mth_name = m.group(2)
        full_name = f"{cls_name}.{mth_name}" if mth_name else cls_name
        parts = full_name.split(".")
        clean_name = ".".join(parts[-2:]) if len(parts) >= 2 else full_name
        if clean_name not in failing_tests:
            failing_tests.append(clean_name)

    # xcbeautify / emoji failing patterns: ✖ -[PackageTests.ViewModelTests testMethod] or ❌ Class.testMethod
    for m in re.finditer(r"(?:✖|❌|Failing)\s+(?:-\[)?([^\s,\]]+)(?:\s+([^\s,\]]+))?\]?", test_output_text):
        cls_name = m.group(1)
        mth_name = m.group(2)
        full_name = f"{cls_name}.{mth_name}" if mth_name else cls_name
        parts = full_name.split(".")
        clean_name = ".".join(parts[-2:]) if len(parts) >= 2 else full_name
        if clean_name not in failing_tests:
            failing_tests.append(clean_name)

    # Swift Testing pattern: Test "testMethod()" failed or ✘ Test "testMethod()" failed
    for m in re.finditer(r'(?:✘\s+)?Test "(?:-\[)?([^"]+)" failed', test_output_text):
        name = m.group(1).replace("()", "").strip()
        parts = name.split(".")
        clean_name = ".".join(parts[-2:]) if len(parts) >= 2 else name
        if clean_name not in failing_tests:
            failing_tests.append(clean_name)

    # Python unittest pattern: FAIL: test_feature (test_file.TestCase)
    for m in re.finditer(r"(?:FAIL|ERROR): ([^\s]+)(?: \(([^\)]+)\))?", test_output_text):
        mth = m.group(1)
        cls_info = m.group(2)
        if cls_info:
            parts = cls_info.split(".")
            if len(parts) >= 2 and parts[-1] == mth:
                cls_name = parts[-2]
            else:
                cls_name = parts[-1]
            name = f"{cls_name}.{mth}"
        else:
            name = mth
        if name not in failing_tests:
            failing_tests.append(name)

    # If failed count wasn't parsed from summary line but failing_tests were found
    if failed == 0 and failing_tests:
        failed = len(failing_tests)
        if total < failed:
            total = failed

    # If total is still 0, count passes from line-by-line matches
    if total == 0:
        pass_count = len(re.findall(r"(?:Test [Cc]ase '?[^\n']+'?\s+passed|✔\s+[^\n]+|Passing\s+[^\n]+|Test \"[^\"]+\" passed)", test_output_text))
        if pass_count > 0 or failed > 0:
            total = pass_count + failed

    passed = max(0, total - failed)

    return {
        "total_run": total,
        "passed_count": passed,
        "failed_count": failed,
        "failing_tests": failing_tests,
    }


def get_job_test_summary(job: dict[str, Any]) -> dict[str, Any]:
    """Derives a comprehensive test metrics summary for a job."""
    branch = job.get("branch")
    base = job.get("base_branch", "main")
    created_count = job.get("created_test_count")
    if created_count is None:
        created_count = count_created_tests_in_diff(branch, base)

    recs = job.get("plan", {}).get("test_recommendations", [])
    planned_count = len(recs) if isinstance(recs, list) else 0

    job_id = job.get("job_id", "")
    out_dir = OUTPUT_DIR / job_id if job_id else None

    # Check cached summary first
    cached = job.get("test_summary")
    if cached and isinstance(cached, dict):
        total_run = cached.get("total_run", 0)
        passed_count = cached.get("passed_count", 0)
        failed_count = cached.get("failed_count", 0)
        failing_tests = list(cached.get("failing_tests", []))
        build_ok = cached.get("build_ok", True)
        tests_ok = cached.get("tests_ok", failed_count == 0)
    else:
        total_run = 0
        passed_count = 0
        failed_count = 0
        failing_tests = []
        build_ok = True
        tests_ok = True

        # Check debug history
        debug_history = job.get("debug_history", [])
        if debug_history:
            last = debug_history[-1]
            res = last.get("result")
            if isinstance(res, dict):
                build_ok = res.get("build_ok", True)
                tests_ok = res.get("tests_ok", True)

        # Parse test.log if available
        if out_dir and out_dir.exists():
            test_log = out_dir / "test.log"
            build_log = out_dir / "build.log"
            if build_log.exists():
                b_text = build_log.read_text(encoding="utf-8", errors="replace")
                if "** BUILD FAILED **" in b_text or ("error:" in b_text and "** BUILD SUCCEEDED **" not in b_text):
                    build_ok = False
            if test_log.exists():
                parsed = parse_test_output(test_log.read_text(encoding="utf-8", errors="replace"))
                total_run = parsed["total_run"]
                passed_count = parsed["passed_count"]
                failed_count = parsed["failed_count"]
                failing_tests = parsed["failing_tests"]
                if failed_count > 0:
                    tests_ok = False

    # Derive overall status
    job_status = job.get("status", "planned")
    if not build_ok or job.get("last_error_type") == "build":
        status_label = "build-failed"
    elif failed_count > 0 or not tests_ok:
        status_label = "failing"
    elif total_run > 0 and failed_count == 0:
        status_label = "passing"
    elif job_status in ["review-needed", "completed"]:
        status_label = "passing"
    elif job_status == "debugging":
        status_label = "failing"
    elif job_status in ["planned", "scheduled", "designing"]:
        status_label = "pending"
    else:
        status_label = "untested"

    return {
        "created_count": created_count,
        "planned_count": planned_count,
        "status": status_label,
        "total_run": total_run,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "failing_tests": failing_tests,
        "build_ok": build_ok,
        "tests_ok": tests_ok,
    }


def analyze_debug_loop_convergence(
    job: dict[str, Any],
    current_test_summary: dict[str, Any] | None = None
) -> dict[str, Any]:
    """
    Analyzes multi-iteration debug history to detect convergence, stagnation,
    and oscillation/ping-pong loops dynamically without relying solely on a fixed iteration cap.
    """
    history = job.get("debug_history", []) if isinstance(job, dict) else []
    completed_trials = [t for t in history if isinstance(t, dict) and t.get("result") not in ("pending", None)]
    
    if not completed_trials and not history:
        return {
            "health": "ready",
            "health_badge": "\033[93m⏳ READY\033[0m",
            "description": "Ready for initial debug attempt",
            "is_stuck": False,
            "failing_tests_delta": 0,
            "stagnant_streak": 0,
            "oscillation_detected": False,
            "recommended_action": "proceed",
        }

    # 1. Track Failing Tests Delta across trials
    failing_counts = []
    failing_sets = []
    for t in completed_trials:
        res = t.get("result")
        if isinstance(res, dict):
            fts = res.get("failing_tests")
            if fts is not None:
                failing_sets.append(set(fts))
                failing_counts.append(len(fts))
            elif res.get("tests_ok") is False:
                failing_counts.append(1)
            elif res.get("tests_ok") is True:
                failing_counts.append(0)
    
    if current_test_summary and current_test_summary.get("failing_tests") is not None:
        curr_fts = set(current_test_summary["failing_tests"])
        if not failing_sets or failing_sets[-1] != curr_fts:
            failing_sets.append(curr_fts)
            failing_counts.append(len(curr_fts))

    # 2. Check for Stagnant Failure Streak (same failure set consecutively)
    stagnant_streak = 0
    if len(failing_sets) >= 2:
        last_set = failing_sets[-1]
        for s in reversed(failing_sets[:-1]):
            if s and s == last_set:
                stagnant_streak += 1
            else:
                break

    # 3. Check for Oscillation / Ping-Pong loops (Hypotheses, actions, or diff hashes)
    hypotheses = [
        (t.get("hypothesis") or "").strip().lower()
        for t in history
        if t.get("hypothesis")
    ]
    diff_hashes = [
        t.get("diff_hash")
        for t in completed_trials
        if t.get("diff_hash")
    ]

    oscillation_detected = False
    oscillation_detail = ""
    if len(hypotheses) >= 2:
        latest_hyp = hypotheses[-1]
        for idx, prev_h in enumerate(hypotheses[:-1], 1):
            if latest_hyp and len(latest_hyp) > 10 and (latest_hyp == prev_h or latest_hyp in prev_h or prev_h in latest_hyp):
                oscillation_detected = True
                oscillation_detail = f"Attempt #{len(hypotheses)} repeated hypothesis from Attempt #{idx}"
                break

    if not oscillation_detected and len(diff_hashes) >= 3:
        latest_hash = diff_hashes[-1]
        for idx, prev_hash in enumerate(diff_hashes[:-1], 1):
            if latest_hash and latest_hash == prev_hash:
                oscillation_detected = True
                oscillation_detail = f"Attempt #{len(diff_hashes)} produced identical code state to Attempt #{idx}"
                break

    # 4. Check Convergence Direction
    failing_tests_delta = 0
    if len(failing_counts) >= 2:
        failing_tests_delta = failing_counts[-1] - failing_counts[-2]

    # 5. Determine Overall Loop Health
    if oscillation_detected:
        health = "oscillating"
        health_badge = "\033[1;93m🔄 OSCILLATING\033[0m"
        description = f"Loop detected: {oscillation_detail}"
        is_stuck = True
        recommended_action = "prompt_guidance"
    elif stagnant_streak >= 1:
        identical_runs = stagnant_streak + 1
        health = "stagnant"
        health_badge = "\033[1;91m🛑 STAGNANT\033[0m"
        description = f"Failure signature unchanged across {identical_runs} consecutive attempts"
        is_stuck = True
        recommended_action = "prompt_guidance"
    elif failing_tests_delta < 0:
        health = "converging"
        health_badge = "\033[1;92m📈 CONVERGING\033[0m"
        description = f"Progressing: failing tests reduced ({failing_counts[-2]} -> {failing_counts[-1]})"
        is_stuck = False
        recommended_action = "auto_proceed"
    elif failing_tests_delta > 0:
        health = "diverging"
        health_badge = "\033[1;91m⚠️ DIVERGING\033[0m"
        description = f"Regression detected: failing tests increased ({failing_counts[-2]} -> {failing_counts[-1]})"
        is_stuck = False
        recommended_action = "caution"
    else:
        health = "exploring"
        health_badge = "\033[1;96m🔍 EXPLORING\033[0m"
        description = "Exploring fix proposals"
        is_stuck = False
        recommended_action = "proceed"

    return {
        "health": health,
        "health_badge": health_badge,
        "description": description,
        "is_stuck": is_stuck,
        "failing_tests_delta": failing_tests_delta,
        "stagnant_streak": stagnant_streak,
        "oscillation_detected": oscillation_detected,
        "recommended_action": recommended_action,
    }




def find_latest_runtime_log(job_id: Optional[str] = None) -> Optional[str]:
    """Finds the most recent log file from output/job_id, logs/, or output/manual."""
    search_dirs = []
    if job_id:
        search_dirs.append(OUTPUT_DIR / job_id)
    search_dirs.extend([ROOT / "logs", OUTPUT_DIR / "manual"])
    
    candidates = []
    for d in search_dirs:
        if d.exists() and d.is_dir():
            for p in d.rglob("*"):
                if p.is_file() and p.suffix in {".log", ".txt"} and p.name != "batch_test_results.log":
                    candidates.append(p)
                    
    if candidates:
        candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        recent_log = candidates[0]
        try:
            return str(recent_log.relative_to(ROOT))
        except ValueError:
            return str(recent_log)
    return None


def format_file_link(path: Path | str, label: str | None = None) -> str:
    """Formats a local file path as a clickable terminal hyperlink (OSC 8) when supported."""
    path_obj = Path(path).resolve()
    display_text = label if label is not None else str(path_obj)
    abs_uri = f"file://{path_obj}"

    if sys.stdout.isatty():
        return f"\033]8;;{abs_uri}\033\\{display_text}\033]8;;\033\\"
    return f"{display_text} ({abs_uri})"


def append_log(name: str, content: str) -> Path:
    path = LOGS_DIR / f"{timestamp()}-{name}.log"
    write_text(path, content)
    return path

def is_disk_full_error(text: str) -> bool:
    """Checks if the given text contains common 'disk full' error indicators."""
    patterns = [
        r"No space left on device",
        r"ENOSPC",
        r"Disk full",
        r"could not write to file",
        r"not enough space"
    ]
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)

def is_firebase_configured() -> bool:
    """Checks if Firebase CLI is installed and logged in for automated distribution."""
    try:
        # Check CLI
        subprocess.check_output(["firebase", "--version"], stderr=subprocess.DEVNULL)
        # Check login
        login_out = subprocess.check_output(["firebase", "login:list"], stderr=subprocess.STDOUT).decode("utf-8")
        return "Logged in as" in login_out
    except:
        return False

def purge_zombie_processes(session_allowed_machines: list[str], silent: bool = True) -> int:
    """
    Scans for and purges stale build processes (running > 12h) on the fleet.
    Returns the number of purged processes.
    """
    # Import locally to avoid circular dependency
    from probe_machine import load_machines, probe_machine
    machines = load_machines()
    machines = [m for m in machines if m["name"] in session_allowed_machines]
    
    all_stale = []
    for m in machines:
        probe = probe_machine(m)
        stale = probe.get("stale_processes", [])
        if stale:
            for p in stale:
                all_stale.append((m, p))
                
    if not all_stale:
        # If no specific zombie found but we want a force purge of DD
        if not silent:
            print("  - No specific zombie processes found. Checking local DerivedData...")
        
        dd_path = Path(PROJECT_CONFIG.derived_data_path)
        if dd_path.exists():
            if not silent: print(f"  - Purging local DerivedData: {dd_path}")
            import shutil
            shutil.rmtree(dd_path, ignore_errors=True)
            return 1
        return 0

    purged_count = 0
    machines_cleared = set()
    
    for m, p in all_stale:
        if not silent:
            print(f"  - Purging {p['comm']} ({p['pid']}) on {m['name']}...")
        
        if m["execution_mode"] == "local":
            subprocess.run(["kill", "-9", p["pid"]], check=False)
            if m["name"] not in machines_cleared:
                dd_path = Path(PROJECT_CONFIG.derived_data_path)
                if dd_path.exists():
                    import shutil
                    shutil.rmtree(dd_path, ignore_errors=True)
                machines_cleared.add(m["name"])
        else:
            ssh_target = m.get("ssh_target")
            if isinstance(ssh_target, list): ssh_target = ssh_target[0]
            # Kill process and clear DD only once per machine
            cmd = f"kill -9 {p['pid']}"
            if m["name"] not in machines_cleared:
                cmd += f" && rm -rf {shlex.quote(PROJECT_CONFIG.derived_data_path)}"
                machines_cleared.add(m["name"])
            subprocess.run(["ssh", ssh_target, cmd], check=False)
        purged_count += 1
    
    return purged_count

def get_repo_state() -> dict[str, list[str]]:
    try:
        out = subprocess.check_output(["git", "status", "--porcelain"], cwd=str(ROOT)).decode("utf-8")
        modified, untracked = [], []
        for line in out.splitlines():
            if not line.strip(): continue
            if line[:2] == "??": untracked.append(line[3:])
            else: modified.append(line[3:])
        return {"modified": modified, "untracked": untracked}
    except: return {"modified": [], "untracked": []}

def gh_json(*args: str) -> Any:
    return json.loads(run(["gh", *args], cwd=ROOT).stdout)

def gh_text(*args: str) -> str:
    return run(["gh", *args], cwd=ROOT).stdout.strip()

def gh_comment(issue_number: int, body: str) -> None:
    if not shutil.which("gh"): return
    run(["gh", "issue", "comment", str(issue_number), "--body", body], cwd=ROOT, check=False)

def update_issue_status(issue_number: int, labels_to_add: str | list[str], labels_to_remove: str | list[str] | None = None) -> None:
    if not shutil.which("gh"): return
    
    cmd = ["gh", "issue", "edit", str(issue_number)]
    
    if isinstance(labels_to_add, str): labels_to_add = [labels_to_add]
    for l in labels_to_add:
        cmd.extend(["--add-label", l])
        
    if labels_to_remove:
        if isinstance(labels_to_remove, str): labels_to_remove = [labels_to_remove]
        for l in labels_to_remove:
            cmd.extend(["--remove-label", l])
            
    run(cmd, cwd=ROOT, check=False)


def get_repo_github_base_url() -> str | None:
    """Extracts the base GitHub web URL from git remote origin or upstream (e.g. 'https://github.com/owner/repo')."""
    for remote in ["origin", "upstream"]:
        try:
            res = subprocess.run(
                ["git", "remote", "get-url", remote],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                raw = res.stdout.strip()
                if "github.com" in raw:
                    if raw.endswith(".git"):
                        raw = raw[:-4]
                    if raw.startswith("git@github.com:"):
                        path = raw.split("git@github.com:", 1)[1]
                        return f"https://github.com/{path.strip('/')}"
                    if raw.startswith("ssh://git@github.com/"):
                        path = raw.split("ssh://git@github.com/", 1)[1]
                        return f"https://github.com/{path.strip('/')}"
                    if raw.startswith("https://github.com/"):
                        return raw.rstrip("/")
                    if raw.startswith("http://github.com/"):
                        return f"https://{raw[7:]}".rstrip("/")
        except Exception:
            pass

    if shutil.which("gh"):
        try:
            url = gh_text("repo", "view", "--json", "url", "--jq", ".url")
            if url and (url.startswith("http://") or url.startswith("https://")):
                return url.rstrip("/")
        except Exception:
            pass

    return None


def get_github_links(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns a list of dictionaries with info for associated GitHub items (issues and PRs).
    Each dict contains: {'type': 'pr'|'issue', 'number': int, 'label': str, 'url': str, 'valid': bool}
    """
    links: list[dict[str, Any]] = []
    base_url = get_repo_github_base_url()

    issue_number = job.get("issue_number")
    pr_number = job.get("pr_number")

    if issue_number:
        issue_url = job.get("issue_url")
        issue_valid = True
        if not issue_url and shutil.which("gh"):
            try:
                fetched = gh_text("issue", "view", str(issue_number), "--json", "url", "--jq", ".url")
                if fetched and (fetched.startswith("http://") or fetched.startswith("https://")):
                    issue_url = fetched
                    job["issue_url"] = fetched
            except Exception:
                issue_valid = False

        if not issue_url:
            if base_url:
                issue_url = f"{base_url}/issues/{issue_number}"
            else:
                issue_url = f"gh issue view {issue_number} --web"
                issue_valid = False

        links.append({
            "type": "issue",
            "number": issue_number,
            "label": f"Issue #{issue_number}",
            "url": issue_url,
            "valid": issue_valid,
        })

    if pr_number:
        pr_url = job.get("pr_url")
        pr_valid = True
        if not pr_url and shutil.which("gh"):
            try:
                fetched = gh_text("pr", "view", str(pr_number), "--json", "url", "--jq", ".url")
                if fetched and (fetched.startswith("http://") or fetched.startswith("https://")):
                    pr_url = fetched
                    job["pr_url"] = fetched
            except Exception:
                pr_valid = False

        if not pr_url:
            if base_url:
                pr_url = f"{base_url}/pull/{pr_number}"
            else:
                pr_url = f"gh pr view {pr_number} --web"
                pr_valid = False

        links.append({
            "type": "pr",
            "number": pr_number,
            "label": f"Pull Request #{pr_number}",
            "url": pr_url,
            "valid": pr_valid,
        })

    return links


def get_github_url(job: dict[str, Any]) -> tuple[str, str]:
    """Returns (kind, url) e.g. ('Pull Request #75', 'https://github.com/...') or ('Issue #123', 'https://github.com/...')"""
    links = get_github_links(job)
    if not links:
        return "GitHub", ""

    # Prefer PR if valid
    for link in links:
        if link["type"] == "pr" and link["valid"]:
            return link["label"], link["url"]

    # Next check for valid issue
    for link in links:
        if link["type"] == "issue" and link["valid"]:
            return link["label"], link["url"]

    # Fallback to any link with an http/https URL
    for link in links:
        if link["url"].startswith("http://") or link["url"].startswith("https://"):
            return link["label"], link["url"]

    return links[0]["label"], links[0]["url"]


@dataclass
class JobPaths:
    job_file: Path
    brief_file: Path
    output_dir: Path

def make_job_paths(job_id: str) -> JobPaths:
    output_dir = OUTPUT_DIR / job_id
    output_dir.mkdir(parents=True, exist_ok=True)
    return JobPaths(JOBS_DIR / f"{job_id}.json", output_dir / "brief.md", output_dir)

def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def safe_relative_path(path: Path, root: Path) -> Path:
    """Returns a relative path from root, ignoring case sensitivity differences on macOS/Windows."""
    try:
        return path.relative_to(root)
    except ValueError:
        # Fallback for case-sensitivity or symlink casing mismatch
        p_str = os.path.abspath(path)
        r_str = os.path.abspath(root)
        if p_str.lower().startswith(r_str.lower()):
            rel_str = p_str[len(r_str):].lstrip(os.sep)
            return Path(rel_str)
        return path

def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")

def read_json(path: Path) -> dict[str, Any]:
    return json.loads(read_text(path))

def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:60]

def colorize_diff_line(line: str) -> str:
    """Applies ANSI colors to a line if it starts with diff markers (+, -, @@)."""
    # Heuristic to avoid coloring log bullet points like "- Running build" or "- Tests passed"
    # Diffs usually have code or spaces immediately after the marker.
    # Log bullets usually have "- [Capitalized Word]"
    is_log_bullet = re.match(r"^[-+] [A-Z][a-z]+", line)
    
    if line.startswith("+") and not line.startswith("+++") and not is_log_bullet:
        return "\033[92m" + line + "\033[0m"
    elif line.startswith("-") and not line.startswith("---") and not is_log_bullet:
        return "\033[1;91m" + line + "\033[0m"
    elif line.startswith("@@"):
        return "\033[1;96m" + line + "\033[0m"
    elif "(+)" in line or "(-)" in line:
        return line.replace("(+)", "(\033[92m+\033[0m)").replace("(-)", "(\033[1;91m-\033[0m)")
    return line

def _print_completed_process(result: subprocess.CompletedProcess) -> None:
    def print_colored(text: str, is_stderr: bool = False):
        if not text: return
        stream = sys.stderr if is_stderr else sys.stdout
        for line in text.splitlines():
            print(colorize_diff_line(line), file=stream, flush=True)
            
    print_colored(result.stdout)
    print_colored(result.stderr, is_stderr=True)

def run(cmd: list[str], cwd: Path | None = None, check: bool = True, capture: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env: full_env.update(env)
    res = subprocess.run(cmd, cwd=str(cwd or ROOT), capture_output=capture, text=True, check=False, env=full_env)
    if capture: _print_completed_process(res)
    if check and res.returncode != 0: raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)
    return res

def run_shell(cmd: str, cwd: Path | None = None, check: bool = True, capture: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    full_env = os.environ.copy()
    if env: full_env.update(env)
    res = subprocess.run(cmd, cwd=str(cwd or ROOT), shell=True, executable="/bin/bash", capture_output=capture, text=True, check=False, env=full_env)
    
    if capture: _print_completed_process(res)

    # Check for ENAMETOOLONG errors
    if res.returncode != 0 and capture and ("ENAMETOOLONG" in (res.stdout or "") or "ENAMETOOLONG" in (res.stderr or "")):
        print("\n" + "!"*60)
        print("⚠️  BUILD ERROR: Path name too long (ENAMETOOLONG)")
        print("   This is usually caused by deeply nested Swift package paths on macOS.")
        print("   Tip: Use a shorter temporary directory for builds.")
        print("!"*60 + "\n")
    
    if capture: _print_completed_process(res)
    if check and res.returncode != 0: raise subprocess.CalledProcessError(res.returncode, cmd, res.stdout, res.stderr)
    return res

def flush_stdin():
    """Clears any pending input from stdin."""
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass

def _split_option_description(option: str) -> tuple[str, str | None]:
    """Split `label (description)` radio options without changing the returned value."""
    matches = list(re.finditer(r"\s*\(([^()]*)\)", option))
    if not matches:
        return option, None

    first_match = matches[0]
    title = option[:first_match.start()].strip()
    descriptions = [match.group(1).strip() for match in matches if match.group(1).strip()]
    if not title or not descriptions or matches[-1].end() != len(option):
        return option, None

    return title, "; ".join(descriptions)

def prompt_radio(label: str, options: list[str], default: str | None = None, clear_screen: bool = True, status_bar: StatusBar | None = None, description: str | list[str] | None = None) -> str:
    """Displays interactive radio buttons navigated by arrow keys."""
    if not sys.stdin.isatty():
        return default or options[0]

    idx = 0
    if default and default in options:
        idx = options.index(default)

    # Keep the cursor visible while typing into text fields.
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()

    def render_radio_option(option: str, option_idx: int, selected_idx: int, highlighted: bool = True) -> str:
        cursor = ">" if option_idx == selected_idx else " "
        icon = "[x]" if option_idx == selected_idx else "[ ]"
        prefix = f"{cursor} {icon}  "
        title, option_description = _split_option_description(option)
        line = f"{prefix}{title}"
        if highlighted and option_idx == selected_idx:
            hl = "\033[1;97;48;5;25m"
            res = "\033[0m"
            line = f"{hl}{prefix}{title.replace(res, hl)}\033[K{res}"
        if option_description:
            line += f"\n       \033[90m{option_description}\033[0m"
        return line

    try:
        first_render = True
        num_rendered_lines = 0
        while True:
            # Redraw strategy:
            # 1. Clear screen (flickery but safe)
            # 2. OR move cursor back up (smooth but requires knowing height)
            if clear_screen:
                sys.stdout.write("\033[?25l\033[r\033[2J\033[H")
                sys.stdout.flush()
            elif not first_render:
                # Move up by the number of lines we printed last time
                sys.stdout.write(f"\033[{num_rendered_lines}A")

            output = []
            title, desc = split_title_description(label)
            output.append(get_header_string(title))
            if desc:
                formatted_desc = desc[0].upper() + desc[1:] if len(desc) > 0 else desc
                output.append(f"\033[93m💡 {formatted_desc}\033[0m")
            if description:
                if isinstance(description, str):
                    output.append(f"\033[90m{description}\033[0m")
                else:
                    for line in description:
                        output.append(f"\033[90m{line}\033[0m")
            output.append("\033[1;90m(Arrows: navigate, Enter: select, B: back)\033[0m")
            output.append("")

            for i, opt in enumerate(options):
                if opt.startswith("---"):
                    output.append(f"  \033[1;90m{opt}\033[0m")
                    continue
                output.append(render_radio_option(opt, i, idx))

            output.append("")
            output.append("  [\033[1;91mB\033[0m] Back")

            # Print current choice placeholder at the bottom
            try:
                cols, _ = os.get_terminal_size()
            except:
                cols = 80
            output.append("-" * (cols - 2))

            final_output = "\n".join(output)
            lines = final_output.split("\n")
            num_rendered_lines = len(lines)

            if not clear_screen:
                # To prevent smearing if options change size, we must clear each line
                final_output = "\n".join([line + "\033[K" for line in lines])

            sys.stdout.write(final_output)
            sys.stdout.flush()

            if status_bar:
                orig_sub = status_bar.sub_menu
                status_bar.sub_menu = True
                status_bar.render(at_bottom=True, force=True)
                status_bar.sub_menu = orig_sub

            first_render = False
            key = get_key()

            if key == "enter":
                if not clear_screen:
                    # Clear the entire interaction including the label
                    sys.stdout.write(f"\r\033[{num_rendered_lines-1}A\033[J")
                    sys.stdout.flush()
                    # Print ONLY the final choice concisely
                    print(f"Choice: \033[1;96m{options[idx]}\033[0m")
                else:
                    sys.stdout.write(f"\r\033[{num_rendered_lines}A\033[J")
                    sys.stdout.flush()
                    print(f"Choice: \033[1;96m{options[idx]}\033[0m")
                break
            elif len(key) == 1 and key.lower() == "b": # Back
                if not clear_screen:
                    # Clear the lines we printed
                    sys.stdout.write(f"\r\033[{num_rendered_lines-1}A\033[J")
                    sys.stdout.flush()
                else:
                    sys.stdout.write(f"\r\033[{num_rendered_lines}A\033[J")
                raise BackException()
            elif key == "up" or key == "k":
                idx = (idx - 1) % len(options)
            elif key == "down" or key == "j":
                idx = (idx + 1) % len(options)
            elif key == "\x03": # Ctrl-C
                raise KeyboardInterrupt

    finally:
        # Show cursor
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        
    return options[idx]

def prompt_confirm(question: str, default: bool = True, description: str | list[str] | None = None, clear_screen: bool = True) -> bool:
    """Displays interactive yes/no radio buttons."""
    # Defensive logic for reported UI duplication
    if question.startswith("Would you Would you"):
        question = "Would you" + question[len("Would you Would you"):]
    
    default_str = "yes" if default else "no"

    choice = prompt_radio(question, ["yes", "no"], default_str, description=description, clear_screen=clear_screen)
    return choice == "yes"

def prompt_multiline(prompt: str) -> str:
    flush_stdin()
    title, desc = split_title_description(prompt)
    print_header(title)
    if desc:
        formatted_desc = desc[0].upper() + desc[1:] if len(desc) > 0 else desc
        print(f"\033[93m💡 {formatted_desc}\033[0m", flush=True)
    print_subtitle("(Type your input. To finish, press Enter then \033[1;97mCtrl-D\033[0m\033[90m on a new line)", hint=True)
    if _ACTIVE_STATUS_BAR:
        _ACTIVE_STATUS_BAR.render(at_bottom=True, force=True, q_msg="Ctrl-D to finish")
    try:
        content = sys.stdin.read()
        return content.strip()
    except EOFError:
        return ""

def print_wrapped_option(option_str: str, indent_size: int = 4, subsequent_indent_size: int = 8) -> None:
    """Prints a menu option line, preserving ANSI colors and wrapping lines with a deeper subsequent indent."""
    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = 80
    cols = max(cols, 40)

    import re
    token_pattern = re.compile(r'(\x1b\[[0-9;]*[mK])|(\s+)|([^\s\x1b]+)')
    tokens = [m.group(0) for m in token_pattern.finditer(option_str)]

    lines = []
    current_line_parts = []
    current_line_plain_len = 0
    active_ansi_state = ""

    first_indent_str = " " * indent_size
    subsequent_indent_str = " " * subsequent_indent_size

    max_width = max(cols - indent_size - 2, 20)

    for token in tokens:
        if token.startswith('\x1b'):
            current_line_parts.append(token)
            if token == '\x1b[0m':
                active_ansi_state = ""
            else:
                active_ansi_state = token
        elif token.isspace():
            if current_line_parts:
                current_line_parts.append(token)
                current_line_plain_len += len(token)
        else:
            word_len = len(token)
            if current_line_plain_len + word_len > max_width and current_line_plain_len > 0:
                if active_ansi_state:
                    current_line_parts.append('\x1b[0m')
                lines.append("".join(current_line_parts))

                current_line_parts = []
                max_width = max(cols - subsequent_indent_size - 2, 20)
                if active_ansi_state:
                    current_line_parts.append(active_ansi_state)
                current_line_parts.append(token)
                current_line_plain_len = word_len
            else:
                current_line_parts.append(token)
                current_line_plain_len += word_len

    if current_line_parts:
        lines.append("".join(current_line_parts))

    for i, line in enumerate(lines):
        ind = first_indent_str if i == 0 else subsequent_indent_str
        print(f"{ind}{line}")

def prompt_password(label: str, placeholder: str = "(enter to skip)") -> str:
    """Interactive password input that shows asterisks instead of clear text."""
    if not sys.stdin.isatty():
        return ""

    # Keep the cursor visible while typing into text fields.
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()

    if _ACTIVE_STATUS_BAR:
        _ACTIVE_STATUS_BAR.render(at_bottom=True, force=True, q_msg="Enter to skip")

    try:
        cols, _ = os.get_terminal_size()
    except:
        cols = 80
    cols = max(cols, 40)

    fixed_len = 4 + len(label) + 2
    available_width = cols - fixed_len - 4

    input_text = ""
    bg_style = "\033[48;5;236m"
    fg_style = "\033[1;97m" # Bold White
    placeholder_style = "\033[90m" # Grey
    reset = "\033[0m"
    prompt_label = f"\033[1;96m{label}\033[0m"

    if field_below:
        sys.stdout.write(f"    {prompt_label}\033[K\n")
        sys.stdout.flush()

    try:
        while True:
            # Render current state as asterisks
            if not input_text and placeholder:
                disp_placeholder = placeholder
                if len(disp_placeholder) > available_width:
                    disp_placeholder = disp_placeholder[:available_width - 3] + "..."
                display = f"{placeholder_style}{disp_placeholder}{reset}"
            else:
                stars_len = len(input_text)
                if stars_len > available_width:
                    display = f"{fg_style}{'*' * available_width}{reset}"
                else:
                    display = f"{fg_style}{'*' * stars_len}{reset}"
            
            # Construct the line
            line = f"\r    {prompt_label} {bg_style} {display} {reset}\033[K"
            sys.stdout.write(line)
            sys.stdout.flush()

            key = get_key()

            if key == "enter":
                sys.stdout.write("\n")
                sys.stdout.flush()
                return input_text.strip().strip("'\"")
            elif key == "backspace":
                input_text = input_text[:-1]
            elif key == "space":
                input_text += " "
            elif key == "esc" or key == "\x1b":
                sys.stdout.write("\n")
                raise BackException()
            elif len(key) == 1:
                input_text += key
    finally:
        # Restore cursor
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()

def prompt_input(label: str, placeholder: str = "", default: str = "", allow_back: bool = False, field_below: bool = False, q_msg: str | None = None) -> str:
    """Interactive text input with styling, backspace handling, and back-out support."""
    if not sys.stdin.isatty():
        return default

    if _ACTIVE_STATUS_BAR:
        status_hint = q_msg if q_msg is not None else ("Enter to confirm" if default else "Enter to submit | Ctrl-C to exit")
        _ACTIVE_STATUS_BAR.render(at_bottom=True, force=True, q_msg=status_hint)

    def visible_width() -> int:
        try:
            cols, _ = os.get_terminal_size()
        except Exception:
            cols = 80
        # Keep safe margin (4 spaces indent + 2 padding + 10 safety) so the field never wraps on narrow terminals.
        return max(12, cols - 16)

    def trim_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        if limit <= 1:
            return text[:limit]
        return text[: max(1, limit - 1)] + "…"

    if field_below:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()

        prompt_label = f"\033[1;96m{label}\033[0m"
        print(f"    {prompt_label}")

        input_text = default
        bg_style = "\033[48;5;236m"
        fg_style = "\033[1;97m" # Bold White
        placeholder_style = "\033[90m" # Grey
        reset = "\033[0m"

        try:
            while True:
                display = input_text
                if not input_text and placeholder:
                    placeholder_text = trim_text(placeholder, visible_width())
                    display = f"{placeholder_style}{placeholder_text}{reset}"
                else:
                    disp_text = input_text
                    if len(disp_text) > visible_width():
                        disp_text = "..." + disp_text[-(visible_width() - 3):]
                    display = f"{fg_style}{disp_text}{reset}"

                line = f"\r\033[K    {bg_style} {display} {reset}\033[K"
                sys.stdout.write(line)
                sys.stdout.flush()

                key = get_key()

                if key == "enter":
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return input_text.strip().strip("'\"") # Remove quotes if pasted
                elif key == "backspace":
                    input_text = input_text[:-1]
                elif key == "space":
                    input_text += " "
                elif key == "esc" or key == "\x1b":
                    sys.stdout.write("\n")
                    raise BackException()
                elif len(key) == 1:
                    # If 'b' or 'B' at the start with no text, treat as back
                    if allow_back and len(input_text) == 0 and key.lower() == "b":
                        sys.stdout.write("\n")
                        raise BackException()
                    input_text += key
                elif key == "up" or key == "down" or key == "left" or key == "right":
                    # For now, ignore arrows to prevent ^[[D being echoed
                    pass
        finally:
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()

    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = 80
    cols = max(cols, 40)

    fixed_len = 4 + len(label) + 2
    available_width = max(12, cols - fixed_len - 4)

    input_text = default
    bg_style = "\033[48;5;236m"
    fg_style = "\033[1;97m" # Bold White
    placeholder_style = "\033[90m" # Grey
    reset = "\033[0m"
    prompt_label = f"\033[1;96m{label}\033[0m"

    try:
        while True:
            # Render current state
            if not input_text and placeholder:
                disp_placeholder = placeholder
                if len(disp_placeholder) > available_width:
                    disp_placeholder = disp_placeholder[:available_width - 3] + "..."
                display = f"{placeholder_style}{disp_placeholder}{reset}"
            else:
                disp_text = input_text
                if len(disp_text) > available_width:
                    disp_text = "..." + disp_text[-(available_width - 3):]
                display = f"{fg_style}{disp_text}{reset}"
            
            # Construct the line (indented to match other prompts)
            label_limit = max(12, visible_width() - len(label) - 4)
            prompt_text = trim_text(label, label_limit)
            line = f"\r\033[K    \033[1;96m{prompt_text}\033[0m {bg_style} {display} {reset}\033[K"
            sys.stdout.write(line)
            sys.stdout.flush()

            key = get_key()

            if key == "enter":
                sys.stdout.write("\n")
                sys.stdout.flush()
                return input_text.strip().strip("'\"") # Remove quotes if pasted
            elif key == "backspace":
                input_text = input_text[:-1]
            elif key == "space":
                input_text += " "
            elif key == "esc" or key == "\x1b":
                sys.stdout.write("\n")
                raise BackException()
            elif len(key) == 1:
                # If 'b' or 'B' at the start with no text, treat as back
                if allow_back and len(input_text) == 0 and key.lower() == "b":
                    sys.stdout.write("\n")
                    raise BackException()
                input_text += key
            elif key == "up" or key == "down" or key == "left" or key == "right":
                # For now, ignore arrows to prevent ^[[D being echoed
                pass
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()

def get_key(blocking: bool = True) -> str:
    """Reads a single keypress, including multi-byte escape sequences for arrows."""
    import tty, termios
    try:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
    except Exception:
        # If we can't get terminal attributes (e.g., not a tty or terminal is corrupted), fallback to input()
        try:
            return sys.stdin.read(1) if not blocking else input()
        except:
            return ""

    try:
        if not blocking:
            # Set to non-blocking
            import fcntl
            old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)
            try:
                ch = sys.stdin.read(1)
                if not ch: return ""
            except:
                return ""
            finally:
                fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)
        else:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
        
        if ch == "\x1b": # Escape sequence
            # Read next 2 chars
            import fcntl
            orig_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, orig_flags | os.O_NONBLOCK)
            try:
                rest = sys.stdin.read(2)
                if rest == "[A": return "up"
                if rest == "[B": return "down"
                if rest == "[C": return "right"
                if rest == "[D": return "left"
            except: pass
            finally:
                fcntl.fcntl(fd, fcntl.F_SETFL, orig_flags)

        if ch == "\x03": # Ctrl-C
            raise KeyboardInterrupt

        if ch == "\r": return "enter"
        if ch == " ": return "space"
        if ch == "\x7f": return "backspace"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

class BackException(Exception): pass
class KeyInterruptException(Exception):
    def __init__(self, key: str, index: int | None = None, value: str | None = None):
        self.key = key
        self.index = index
        self.value = value

def print_divider(char: str = "-", max_width: int | None = None) -> None:
    """Prints a subtle horizontal divider line responsive to terminal width."""
    try:
        columns, _ = os.get_terminal_size()
    except Exception:
        columns = 80
    columns = max(10, columns)
    width = min(columns - 2, max_width) if max_width else max(8, columns - 2)
    print(f"\033[90m{char * width}\033[0m", flush=True)

def format_job_id(job_id: str) -> str:
    return f"\033[1;97m{job_id}\033[0m"

def format_index(i: int) -> str:
    return f"[\033[96m{i}\033[0m]"

def _is_free_option(opt: str) -> bool:
    clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", opt).lower().strip()
    return "(free)" in clean or "[free]" in clean or ":free" in clean or "-free" in clean or clean.endswith("free") or clean.startswith("free")

def _build_checkbox_hint(allow_bulk_select: bool, extra_keys: list[str], has_free: bool) -> str:
    parts = ["Arrows: navigate", "Space: toggle"]
    if allow_bulk_select:
        if "a" not in extra_keys:
            parts.append("A: all")
        if "f" not in extra_keys and has_free:
            parts.append("F: free")
        if "n" not in extra_keys and "u" not in extra_keys:
            parts.append("N: none")
    parts.append("Enter: save")
    parts.append("B: back")
    return f"\033[1;90m({', '.join(parts)})\033[0m"

def prompt_checkbox(label: str, options: list[str], defaults: list[str] | None = None, extra_keys: list[str] | None = None, footer: str | None = None, details_map: dict[str, list[str]] | None = None, status_bar: StatusBar | None = None, clear_screen: bool = True, max_selections: int | None = None, footer_actions: list[str] | None = None, details_title: str | None = None, allow_bulk_select: bool = True) -> list[str]:
    """Displays interactive checkboxes navigated by arrow keys, toggled by space (vertical)."""
    if not sys.stdin.isatty():
        return defaults or []

    defaults = defaults or []
    extra_keys = [k.lower() for k in (extra_keys or [])]
    selected_indices = {i for i, opt in enumerate(options) if opt in defaults}
    idx = 0
    # Ensure starting idx is not a header
    while idx < len(options) and options[idx].startswith("---"):
        idx = (idx + 1) % len(options)
    
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    
    error_msg = ""
    has_free_options = any(not opt.startswith("---") and _is_free_option(opt) for opt in options)
    instruction_hint = _build_checkbox_hint(allow_bulk_select, extra_keys, has_free_options)
    
    try:
        first_render = True
        num_rendered_lines = 0
        while True:
            # Redraw strategy
            if clear_screen:
                sys.stdout.write("\033[r\033[?25h\033[0m\r")
                sys.stdout.flush()
                os.system("clear" if os.name != "nt" else "cls")
                sys.stdout.write("\033[?25l")
                sys.stdout.flush()
            elif not first_render:
                sys.stdout.write(f"\033[{num_rendered_lines}A")

            output = []
            # 1. Print Header
            title, desc = split_title_description(label)
            output.append(get_header_string(title))
            if desc:
                formatted_desc = desc[0].upper() + desc[1:] if len(desc) > 0 else desc
                output.append(f"\033[93m💡 {formatted_desc}\033[0m")
            output.append(instruction_hint)
            output.append("")

            
            if error_msg:
                output.append(f"\033[1;91m      ⚠️  {error_msg}\033[0m")
                error_msg = ""

            # 2. Print options
            for i, opt in enumerate(options):
                if opt.startswith("---"):
                    if opt.strip() == "---":
                        output.append("")
                    else:
                        if i > 0 and output and output[-1] != "":
                            output.append("")
                        output.append(f"  \033[1;90m{opt}\033[0m")
                    continue
                cursor = "> " if i == idx else "  "
                checked = "[\033[1;96mx\033[0m]" if i in selected_indices else "[ ]"
                line = f"{cursor}{checked} {opt}"
                
                # Only highlight the entire row if it is currently hovered (idx)
                if i == idx:
                    # Highlight color (Navy Blue for consistency with main menu status bar)
                    hl = "\033[1;97;48;5;25m"
                    res = "\033[0m"
                    # Ensure reset codes inside 'checked' or 'opt' don't break the whole row highlight
                    checked_fixed = checked.replace(res, hl)
                    opt_fixed = opt.replace(res, hl)
                    # \033[K ensures the background color extends to the end of the terminal line
                    line = f"{hl}{cursor}{checked_fixed} {opt_fixed}\033[K{res}"
                
                output.append(line)
            
            # Max selections disclaimer
            if max_selections:
                output.append(f"\033[1;97mSelected machines:\033[0m \033[1;96m{len(selected_indices)} of {max_selections}\033[0m \033[90mmachine limit\033[0m")
            
            if footer:
                output.append("")
                output.append(footer)
            # 3. Print details for current selection
            if details_map:
                current_option = options[idx]
                details = details_map.get(current_option, [])
                if details:
                    output.append("")
                    if details_title:
                        output.append(f"  \033[1;96m{details_title}\033[0m")
                    for line in details:
                        if line.startswith("\033"):
                            output.append(f"  {line}")
                        else:
                            output.append(f"  \033[90m{line}\033[0m")
            
            # 4. Print footer
            if footer_actions:
                output.append("")
                output.append("\033[1;97mActions\033[0m")
                for action in footer_actions:
                    output.append(f"  {action}")
            
            # 5. Print Bottom UI
            try:
                cols, _ = os.get_terminal_size()
            except:
                cols = 80
            divider = "-" * (cols - 2)
            output.append(divider)
            
            final_output = "\n".join(output)
            lines = final_output.split("\n")
            num_rendered_lines = len(lines)
            
            if not clear_screen:
                final_output = "\n".join([line + "\033[K" for line in lines])

            sys.stdout.write(final_output)
            sys.stdout.flush()

            if status_bar:
                orig_sub = status_bar.sub_menu
                status_bar.sub_menu = True
                # Draw the status bar at the bottom. 
                # Note: This uses \0337 / \0338 to save/restore cursor.
                status_bar.render(at_bottom=True, force=True)
                status_bar.sub_menu = orig_sub
            
            first_render = False
            key = get_key()

            if key == "enter":
                if not clear_screen:
                    # Clear the lines we printed
                    sys.stdout.write(f"\r\033[{num_rendered_lines-1}A\033[J")
                    sys.stdout.flush()
                else:
                    # Final render without blue highlight
                    sys.stdout.write(f"\r\033[{num_rendered_lines}A")
                    final_render = []
                    title, desc = split_title_description(label)
                    final_render.append(get_header_string(title))
                    if desc:
                        formatted_desc = desc[0].upper() + desc[1:] if len(desc) > 0 else desc
                        final_render.append(f"\033[93m💡 {formatted_desc}\033[0m")
                    final_render.append(instruction_hint)
                    final_render.append("")
                    if footer:
                        final_render.append(footer)
                        final_render.append("")
                    for i, opt in enumerate(options):
                        if opt.startswith("---"):
                            final_render.append(f"  \033[1;90m{opt}\033[0m")
                            continue
                        checked = "[\033[1;96mx\033[0m]" if i in selected_indices else "[ ]"
                        final_render.append(f"  {checked} {opt}")
                    if footer_actions:
                        final_render.append("")
                        final_render.append("\033[1;97mActions\033[0m")
                        for action in footer_actions:
                            final_render.append(f"  {action}")
                    final_render.append("-" * (cols - 2))
                    final_render.append(f"Choice: \033[1;96m{len(selected_indices)} selected\033[0m")
                    sys.stdout.write("\n".join(final_render) + "\n")
                break
            elif len(key) == 1 and key.lower() == "b": # Back
                if not clear_screen:
                    # Clear the lines we printed
                    sys.stdout.write(f"\r\033[{num_rendered_lines-1}A\033[J")
                    sys.stdout.flush()
                else:
                    sys.stdout.write("\n")
                raise BackException()
            elif key.lower() in extra_keys:
                if not clear_screen:
                    # Clear the lines we printed
                    sys.stdout.write(f"\r\033[{num_rendered_lines-1}A\033[J")
                    sys.stdout.flush()
                else:
                    sys.stdout.write("\n")
                raise KeyInterruptException(key.lower(), index=idx, value=options[idx])
            elif allow_bulk_select and len(key) == 1 and key.lower() == "a": # Select All
                selectable = [i for i, opt in enumerate(options) if not opt.startswith("---")]
                if max_selections and len(selectable) > max_selections:
                    selected_indices = set(selectable[:max_selections])
                    error_msg = f"Maximum of {max_selections} selections allowed."
                else:
                    selected_indices = set(selectable)
            elif allow_bulk_select and len(key) == 1 and key.lower() == "f": # Select Free Only
                free_indices = [i for i, opt in enumerate(options) if not opt.startswith("---") and _is_free_option(opt)]
                if free_indices:
                    if max_selections and len(free_indices) > max_selections:
                        selected_indices = set(free_indices[:max_selections])
                        error_msg = f"Maximum of {max_selections} selections allowed."
                    else:
                        selected_indices = set(free_indices)
                else:
                    error_msg = "No free options found in list."
            elif allow_bulk_select and len(key) == 1 and key.lower() in ("n", "u"): # Deselect All
                selected_indices.clear()
            elif key == "space": # Space
                if idx in selected_indices:
                    selected_indices.remove(idx)
                else:
                    if max_selections and len(selected_indices) >= max_selections:
                        error_msg = f"Maximum of {max_selections} machines allowed for performance."
                    else:
                        selected_indices.add(idx)
            elif key == "up" or key == "k": # Up
                orig = idx
                while True:
                    idx = (idx - 1) % len(options)
                    if not options[idx].startswith("---") or idx == orig:
                        break
            elif key == "down" or key == "j": # Down
                orig = idx
                while True:
                    idx = (idx + 1) % len(options)
                    if not options[idx].startswith("---") or idx == orig:
                        break
            elif key == "\x03": # Ctrl-C
                raise KeyboardInterrupt

    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        
    return [options[i] for i in sorted(list(selected_indices))]

def extract_commands() -> tuple[str, str]:
    """Extracts build and test commands from docs/build-test-commands.md"""
    doc_path = DOCS_DIR / "build-test-commands.md"

    def default_xcode_command(action: str) -> str | None:
        if PROJECT_CONFIG.xcode_workspace:
            base = f"xcodebuild {action} -workspace {shlex.quote(PROJECT_CONFIG.xcode_workspace)}"
        elif PROJECT_CONFIG.xcode_project:
            base = f"xcodebuild {action} -project {shlex.quote(PROJECT_CONFIG.xcode_project)}"
        else:
            return None
        if PROJECT_CONFIG.scheme:
            base += f" -scheme {shlex.quote(PROJECT_CONFIG.scheme)}"
        return base

    default_build = PROJECT_CONFIG.build_command or default_xcode_command("build")
    default_test = PROJECT_CONFIG.test_command or default_xcode_command("test")

    build = default_build
    test = default_test

    if doc_path.exists():
        content = doc_path.read_text(encoding="utf-8")
        # Accept neutral headings and legacy generated iOS headings. Do not
        # borrow a command from a later section when a section has no code block.
        def documented_command(heading: str) -> str | None:
            pattern = rf"^## (?:iOS app )?{heading}\s*$((?:(?!^## ).)*?)^```bash\s*\n(.*?)^```"
            match = re.search(pattern, content, re.MULTILINE | re.DOTALL | re.IGNORECASE)
            return match.group(2).strip() if match else None

        build = PROJECT_CONFIG.build_command or documented_command("build") or default_build
        test = PROJECT_CONFIG.test_command or documented_command("tests?") or default_test

    if not build or not test:
        raise ValueError(
            "Configure build_command and test_command in .orchestrator/project.json "
            "or document them under ## Build and ## Tests in docs/build-test-commands.md."
        )

    build = build.replace("$(pwd)", str(ROOT))
    test = test.replace("$(pwd)", str(ROOT))

    # Ensure Xcode commands have a simulator destination.
    if "xcodebuild" in build and "-destination" not in build:
        build += f" -destination {shlex.quote(get_best_simulator_destination())}"
    if "xcodebuild" in test and "-destination" not in test:
        test += f" -destination {shlex.quote(get_best_simulator_destination())}"

    # Ensure Xcode commands have derivedDataPath without mutating custom shell commands.
    if "xcodebuild" in build and "-derivedDataPath" not in build:
        build += f" -derivedDataPath {shlex.quote(PROJECT_CONFIG.derived_data_path)}"
    if "xcodebuild" in test and "-derivedDataPath" not in test:
        test += f" -derivedDataPath {shlex.quote(PROJECT_CONFIG.derived_data_path)}"

    return build, test
def extract_destination_from_command(command: str) -> str | None:
    """Extracts the destination argument from a shell command string."""
    try:
        parts = shlex.split(command)
        for i in range(len(parts)):
            if parts[i] == "-destination" and i + 1 < len(parts):
                return parts[i + 1]
    except Exception:
        pass
    return None

def command_with_destination(command: str, destination: str) -> str:
    """Replaces or injects -destination <destination> into a shell command string."""
    try:
        parts = shlex.split(command)
    except Exception:
        return f"{command} -destination {shlex.quote(destination)}"
    cleaned: list[str] = []
    i = 0
    while i < len(parts):
        if parts[i] == "-destination":
            i += 2
            continue
        cleaned.append(parts[i])
        i += 1
    cleaned.extend(["-destination", destination])
    return shlex.join(cleaned)

def get_best_simulator_destination() -> str:
    """
    Intelligently finds the best available iOS simulator destination.
    Priority: 1. Booted devices, 2. Latest iOS version, 3. iPhone over iPad.
    """
    try:
        def parse_version(v_str: str) -> tuple[int, ...]:
            parts = re.findall(r"(\d+)", v_str)
            return tuple(map(int, parts))

        res = subprocess.run(["xcrun", "simctl", "list", "devices", "available", "--json"], capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        devices_by_runtime = data.get("devices", {})

        all_candidates = []
        for runtime, devices in devices_by_runtime.items():
            if "iOS" not in runtime:
                continue

            version = parse_version(runtime)
            for d in devices:
                if d.get("isAvailable") == False: continue

                all_candidates.append({
                    "udid": d["udid"],
                    "name": d["name"],
                    "booted": d.get("state") == "Booted",
                    "version": version,
                    "is_iphone": "iPhone" in d["name"]
                })

        if not all_candidates:
            return "platform=iOS Simulator,name=iPhone 16"

        def sort_key(c):
            return (
                not c["booted"],            # False (Booted) comes before True
                [-x for x in c["version"]], # Higher versions first
                not c["is_iphone"],         # iPhone first
                c["name"]
            )

        best = sorted(all_candidates, key=sort_key)[0]
        return f"platform=iOS Simulator,id={best['udid']}"

    except Exception:
        return "platform=iOS Simulator,name=iPhone 16"

def get_fallback_simulator_destinations(primary_dest: str | None = None) -> list[str]:
    """
    Returns an ordered list of candidate simulator destinations for fallback retries.
    """
    destinations: list[str] = []
    
    if primary_dest:
        clean_primary = re.sub(r",arch=[^,]+", "", primary_dest)
        if clean_primary not in destinations:
            destinations.append(clean_primary)

    try:
        res = subprocess.run(["xcrun", "simctl", "list", "devices", "available", "--json"], capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        devices_by_runtime = data.get("devices", {})

        # 1. Booted devices first
        for runtime, devices in devices_by_runtime.items():
            if "iOS" not in runtime: continue
            for d in devices:
                if d.get("isAvailable") != False and d.get("state") == "Booted":
                    d_id = f"platform=iOS Simulator,id={d['udid']}"
                    d_name = f"platform=iOS Simulator,name={d['name']}"
                    if d_id not in destinations: destinations.append(d_id)
                    if d_name not in destinations: destinations.append(d_name)

        # 2. Available iPhones by name and id
        for runtime, devices in devices_by_runtime.items():
            if "iOS" not in runtime: continue
            for d in devices:
                if d.get("isAvailable") != False and "iPhone" in d.get("name", ""):
                    d_name = f"platform=iOS Simulator,name={d['name']}"
                    d_id = f"platform=iOS Simulator,id={d['udid']}"
                    if d_name not in destinations: destinations.append(d_name)
                    if d_id not in destinations: destinations.append(d_id)
    except Exception:
        pass

    # 3. Standard well-known model names
    for name in ["iPhone 16", "iPhone 17", "iPhone 16 Pro", "iPhone 15", "iPhone 15 Pro", "iPhone 14"]:
        d_name = f"platform=iOS Simulator,name={name}"
        if d_name not in destinations:
            destinations.append(d_name)

    # 4. Generic latest OS
    for generic in ["platform=iOS Simulator,OS=latest", "generic/platform=iOS"]:
        if generic not in destinations:
            destinations.append(generic)

    return destinations

def get_simulator_diagnostic() -> dict[str, Any]:
    """
    Checks the system's iOS simulator health and runtime availability.
    """
    diag: dict[str, Any] = {
        "has_simctl": False,
        "has_runtimes": False,
        "runtimes": [],
        "available_devices_count": 0,
        "booted_devices_count": 0,
        "best_destination": None,
        "is_concrete": False,
        "error_message": ""
    }
    import shutil
    if shutil.which("xcrun") is None:
        diag["error_message"] = "Xcode Command Line Tools / xcrun is not installed."
        return diag

    diag["has_simctl"] = True
    try:
        r_res = subprocess.run(["xcrun", "simctl", "list", "runtimes", "--json"], capture_output=True, text=True, timeout=5.0)
        if r_res.returncode == 0:
            r_data = json.loads(r_res.stdout)
            runtimes = [r["name"] for r in r_data.get("runtimes", []) if "iOS" in r.get("name", "")]
            diag["runtimes"] = runtimes
            diag["has_runtimes"] = len(runtimes) > 0

        d_res = subprocess.run(["xcrun", "simctl", "list", "devices", "available", "--json"], capture_output=True, text=True, timeout=5.0)
        if d_res.returncode == 0:
            d_data = json.loads(d_res.stdout)
            devices_by_runtime = d_data.get("devices", {})
            total_avail = 0
            total_booted = 0
            for rt, devs in devices_by_runtime.items():
                if "iOS" not in rt: continue
                for d in devs:
                    if d.get("isAvailable") != False:
                        total_avail += 1
                        if d.get("state") == "Booted":
                            total_booted += 1
            diag["available_devices_count"] = total_avail
            diag["booted_devices_count"] = total_booted
    except Exception as e:
        diag["error_message"] = str(e)

    dest = get_best_simulator_destination()
    diag["best_destination"] = dest
    diag["is_concrete"] = "id=" in dest
    return diag
def get_test_plan_flags(plan_path: Path) -> str:
    """
    Manually parses an .xctestplan file and returns -only-testing flags.
    This bypasses xcodebuild issues with reading test plan files directly.
    """
    if not plan_path.exists():
        return ""
    
    try:
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        flags = []
        for target in data.get("testTargets", []):
            target_name = target.get("target", {}).get("name", PROJECT_CONFIG.test_target)
            selected = target.get("selectedTests", [])
            skipped = target.get("skippedTests", [])
            
            if selected:
                for t in selected:
                    flags.append(f"-only-testing:{target_name}/{t}")
            elif skipped:
                # If no specific tests are selected, we include the whole target but skip specific ones
                flags.append(f"-only-testing:{target_name}")
                for t in skipped:
                    flags.append(f"-skip-testing:{target_name}/{t}")
            else:
                # No selection/skips, just run the whole target
                flags.append(f"-only-testing:{target_name}")
                
        return " ".join(flags)
    except Exception as e:
        # Fallback to just using the name if parsing fails
        return f"-testPlan {plan_path.stem}"

_STATUS_BAR_NESTING = 0
_ACTIVE_STATUS_BAR = None

class ProgressIndicator:
    def __init__(self, label: str = "Thinking", hint: str = "Ctrl-C to abort"):
        self.label = label
        self.hint = hint
        self.start_time = datetime.now()
        self.frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        self.frame_idx = 0
        self.last_render_time = 0.0
        self._cursor_hidden = False
        # Silent mode ONLY if we are being called by a worker/orchestrator
        # 'dev_console' is the UI parent and should NOT be silent,
        # UNLESS specifically requested (e.g. to avoid double spinners).
        self.is_silent = os.environ.get("AI_REQUEST_SOURCE") == "orchestrator" or os.environ.get("AI_PROGRESS_SILENT") == "1"

    def hide_cursor(self):
        if self.is_silent or self._cursor_hidden or not sys.stdout.isatty():
            return
        sys.stdout.write("\033[?25l")
        sys.stdout.flush()
        self._cursor_hidden = True

    def restore_cursor(self):
        if self.is_silent or not self._cursor_hidden:
            return
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()
        self._cursor_hidden = False

    def get_line(self, last_activity_time: float | None = None) -> str:
        """Returns the formatted progress line without escape sequences for positioning."""
        elapsed = datetime.now() - self.start_time
        seconds = int(elapsed.total_seconds())
        minutes, seconds = divmod(seconds, 60)
        timer_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
        
        activity_str = ""
        if last_activity_time:
            import time
            idle_sec = int(time.time() - last_activity_time)
            if idle_sec > 5:
                activity_str = f", idle {idle_sec}s"

        spinner = self.frames[self.frame_idx]
        self.frame_idx = (self.frame_idx + 1) % len(self.frames)
        
        # Prevent terminal wrapping which spawns duplicate lines
        try:
            cols, _ = os.get_terminal_size()
        except Exception:
            cols = 80
            
        max_width = max(15, cols - 2)

        label_text = self.label.strip() if self.label else "Thinking"
        if not label_text.endswith("...") and not label_text.endswith(":") and not label_text.endswith("."):
            label_text = f"{label_text}..."

        hint = self.hint.strip() if self.hint else ""
        short_hint = "Ctrl-C" if "ctrl-c" in hint.lower() else hint

        # Determine meta candidate strings in descending order of verbosity
        meta_candidates: list[str] = []
        if hint:
            meta_candidates.append(f"{hint}, {timer_str}{activity_str}")
            if short_hint != hint:
                meta_candidates.append(f"{short_hint}, {timer_str}{activity_str}")
        if activity_str:
            meta_candidates.append(f"{timer_str}{activity_str}")
        meta_candidates.append(timer_str)
        meta_candidates.append("")

        chosen_meta = None
        final_label = label_text

        # 1. First preference: find the most verbose meta candidate where the ENTIRE label fits without truncation
        for meta in meta_candidates:
            meta_len = (len(meta) + 3) if meta else 0  # +3 for " (" and ")"
            avail = max_width - 2 - meta_len
            if avail >= len(label_text):
                chosen_meta = meta
                final_label = label_text
                break

        # 2. Second preference: if full label doesn't fit with full meta, pick a concise meta that leaves ample room for the label
        if chosen_meta is None:
            for meta in meta_candidates:
                meta_len = (len(meta) + 3) if meta else 0
                avail = max_width - 2 - meta_len
                # Only use this meta if it leaves at least 18 chars for the label
                if avail >= 18:
                    chosen_meta = meta
                    final_label = label_text[:avail - 3] + "..."
                    break
            else:
                # Fallback to no meta at all to give the label maximum possible width
                chosen_meta = ""
                avail = max(4, max_width - 2)
                final_label = label_text[:avail - 3] + "..." if len(label_text) > avail else label_text

        if chosen_meta:
            return f"\033[1;96m{spinner}\033[0m {final_label} \033[90m({chosen_meta})\033[0m"
        else:
            return f"\033[1;96m{spinner}\033[0m {final_label}"

    def render(self, force: bool = False, last_activity_time: float | None = None):
        if self.is_silent or not sys.stdout.isatty(): return
        now = time.monotonic()
        if not force and now - self.last_render_time < 0.1: return
        
        global _STATUS_BAR_NESTING, _ACTIVE_STATUS_BAR
        if _STATUS_BAR_NESTING > 0 and _ACTIVE_STATUS_BAR:
            _ACTIVE_STATUS_BAR.render(at_bottom=True, activity=self, force=force)
        else:
            self.hide_cursor()
            sys.stdout.write(f"\r\033[K{self.get_line(last_activity_time)}")
            sys.stdout.flush()
        self.last_render_time = now

    def clear(self):
        if self.is_silent or not sys.stdout.isatty(): return
        global _STATUS_BAR_NESTING, _ACTIVE_STATUS_BAR
        if _STATUS_BAR_NESTING > 0 and _ACTIVE_STATUS_BAR:
            _ACTIVE_STATUS_BAR.activity_indicator = None
            _ACTIVE_STATUS_BAR.render(at_bottom=True, force=True)
        else:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        self.restore_cursor()

    def __enter__(self):
        self.start_time = datetime.now()
        self.render(force=True)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.clear()

class StatusBar:
    def __init__(self, job: dict[str, Any] | None = None, is_processing: bool = False, sub_menu: bool = False):
        self.job = job or {}
        self.is_processing = is_processing
        self.sub_menu = sub_menu
        self.q_count = 0
        self.directory = ROOT.name
        self.branch = self._get_branch()
        self._scroll_region_set = False
        self._last_size = (0, 0)
        self._last_render_time = 0.0
        self.activity_indicator: ProgressIndicator | None = None
        self._cursor_hidden = False
        # Silent mode ONLY if we are being called by a worker/orchestrator
        self.is_silent = os.environ.get("AI_REQUEST_SOURCE") == "orchestrator" or os.environ.get("AI_PROGRESS_SILENT") == "1"
        self._last_rendered_lines = 0
        self.anchor_to_bottom = True
        try:
            settings_path = CONFIG_DIR / "settings.json"
            if settings_path.exists():
                settings = read_json(settings_path)
                self.anchor_to_bottom = settings.get("anchor_prompt_to_bottom", True)
        except:
            pass
        
        allowed = self.job.get("allowed_machines") or self.job.get("session_machines", [])
        online = self.job.get("online_machines")
        
        if online is not None:
            self.machines = f"{len(online)}/{len(allowed)}" if len(online) != len(allowed) else str(len(allowed))
        else:
            self.machines = str(len(allowed))
            
        self.models = len(self.job.get("allowed_models") or self.job.get("session_models", []))
        self.active_model = self.job.get("actual_builder_used") or self.job.get("actual_planner_used")

    def _get_branch(self) -> str:
        try: return subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ROOT)).decode("utf-8").strip()
        except: return "unknown"

    def _get_size(self) -> tuple[int, int]:
        try:
            columns, lines = os.get_terminal_size()
            return columns, lines
        except:
            return 80, 24

    def __enter__(self):
        global _STATUS_BAR_NESTING, _ACTIVE_STATUS_BAR
        _STATUS_BAR_NESTING += 1
        _ACTIVE_STATUS_BAR = self
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        global _STATUS_BAR_NESTING, _ACTIVE_STATUS_BAR
        _STATUS_BAR_NESTING -= 1
        if _STATUS_BAR_NESTING <= 0:
            self.reset_scroll_region(force=True)
            _STATUS_BAR_NESTING = 0
            _ACTIVE_STATUS_BAR = None

    def set_scroll_region(self):
        if not self.anchor_to_bottom: return
        if not sys.stdin.isatty(): return
        cols, lines = self._get_size()
        self._last_size = (cols, lines)
        # Scroll region ends at lines-4.
        cursor_code = ""
        if self.is_processing and sys.stdout.isatty():
            cursor_code = "\033[?25l"
            self._cursor_hidden = True
        sys.stdout.write(f"{cursor_code}\0337\033[1;{lines-4}r\0338")
        sys.stdout.flush()
        self._scroll_region_set = True

    def reset_scroll_region(self, force: bool = False):
        if self.anchor_to_bottom or force:
            if self._scroll_region_set or force:
                _, lines = self._get_size()
                # \0337: save cursor position
                # \033[r: reset scroll region to full screen
                # \033[?25h: show cursor
                # \0338: restore cursor position so output doesn't jump to the bottom of the screen
                sys.stdout.write(f"\0337\033[r\033[?25h\0338")
                sys.stdout.flush()
                self._scroll_region_set = False
                self._cursor_hidden = False

    def clear_footer(self):
        """Fully wipes the bottom 4 lines where the footer lives."""
        if not self.anchor_to_bottom: return
        _, lines = self._get_size()
        cmd = "\0337" # Save cursor
        for i in range(lines - 3, lines + 1):
            cmd += f"\033[{i};1H\033[K"
        cmd += "\0338"
        sys.stdout.write(cmd)
        sys.stdout.flush()

    def render(self, at_bottom: bool = True, force: bool = False, prompt: str | None = None, activity: ProgressIndicator | None = None, q_msg: str | None = None):
        if self.is_silent: return
        if not self.anchor_to_bottom:
            at_bottom = False
        now = time.monotonic()
        # Rate limit frequent renders unless forced
        if not force and at_bottom and now - self._last_render_time < 0.05:
            return
        self._last_render_time = now
        
        if activity:
            self.activity_indicator = activity

        # Auto-set scroll region if we want to render at bottom but haven't yet
        if at_bottom and not self._scroll_region_set:
            self.set_scroll_region()

        if q_msg is None:
            if self.is_processing:
                q_msg = "Ctrl-C to abort"
            elif self.sub_menu:
                q_msg = "B to go back"
            else:
                q_msg = "Ctrl-C to quit"
            
        model_info = f"models: {self.models}"
        if self.active_model:
            model_info = f"model: {self.active_model}"
            
        meta = f"dir: {self.directory} | branch: {self.branch} | machines: {self.machines} | {model_info}"
        columns, lines = self._get_size()
        
        if self._scroll_region_set and (columns, lines) != self._last_size:
            self.set_scroll_region()

        cols = max(10, columns - 2)
        prompt_width = max(12, len(q_msg))
        content = f" {q_msg:<{prompt_width}} | {meta}"
        if len(content) > cols:
            content = content[:cols-3] + "..."
        else:
            content = content + (" " * (cols - len(content)))

        bar = f"\033[1;48;5;25;97m{content}\033[0m"
        divider = "\033[90m" + ("-" * cols) + "\033[0m"
        
        if at_bottom:
            # We construct the entire 4-line footer in one sequence
            cmd = "\0337" # Save cursor
            
            # Line L: Metadata Bar
            cmd += f"\033[{lines};1H\r{bar}\033[K"

            # Line L-1: Divider
            cmd += f"\033[{lines-1};1H\r{divider}\033[K"
            
            # Line L-3: Activity (Progress)
            act_line = self.activity_indicator.get_line() if self.activity_indicator else ""
            cmd += f"\033[{lines-3};1H\r\033[K{act_line}"
            
            # Line L-2: User Prompt (Draw LAST to leave cursor here if prompting)
            prompt_str = prompt if prompt else ""
            cmd += f"\033[{lines-2};1H\r\033[K{prompt_str}"
            
            if not prompt:
                # Restore cursor position and hide cursor if NOT showing a prompt
                cmd += "\033[?25l\0338"
            else:
                # Ensure cursor is visible if prompt is shown
                cmd += "\033[?25h"
            
            sys.stdout.write(cmd)
            sys.stdout.flush()
        else:
            # We want the layout to be:
            # 1. Activity line (if any)
            # 2. Spacer line (if prompt)
            # 3. Prompt (if any)
            # 4. Divider
            # 5. Bar (footer)
            
            act_line = ""
            if activity:
                act_line = self.activity_indicator.get_line() if self.activity_indicator else ""

            # Determine cursor movement to overwrite previous render
            last_lines = getattr(self, "_last_rendered_lines", 0)
            last_had_prompt = getattr(self, "_last_had_prompt", False)
            
            cmd = ""
            if last_lines > 0:
                # If the last render had a prompt, the cursor was left on the prompt line.
                # Since the prompt line is the 3rd line from the bottom of the printed block,
                # the distance to the top of the render block is (last_lines - 3).
                # Otherwise, the cursor was left on the bottom line, so distance to top is (last_lines - 1).
                lines_to_move = (last_lines - 3) if last_had_prompt else (last_lines - 1)
                if lines_to_move > 0:
                    cmd += f"\033[{lines_to_move}A\r"
            
            total_lines = 0
            
            # 1. Write activity
            if act_line:
                cmd += f"{act_line}\033[K\n"
                total_lines += 1
            
            # 2. Write spacer and prompt placeholder
            if prompt:
                cmd += "\033[K\n"     # Spacer line
                cmd += "\033[K\n"     # Prompt line placeholder
                total_lines += 2
            
            # 3. Write divider and bar
            cmd += f"{divider}\033[K\n"
            cmd += f"{bar}\033[K"
            total_lines += 2          # Note: bar doesn't end with a newline in cmd
            
            # 4. Draw the prompt last and leave the cursor there
            if prompt:
                cmd += "\033[2A\r"    # Move up 2 lines (over bar and divider)
                cmd += f"{prompt}\033[K"
            
            self._last_rendered_lines = total_lines
            self._last_had_prompt = bool(prompt)
            
            sys.stdout.write(cmd)
            sys.stdout.flush()

def cleanup_terminal():
    try:
        columns, lines = os.get_terminal_size()
    except:
        lines = 24
    try:
        # 1. Reset terminal mode (restore ONLCR / carriage returns)
        if os.name != "nt":
            os.system("stty sane 2>/dev/null")
            
        # 2. Reset scroll region, show cursor, move to bottom
        # \033[r: Reset scroll region
        # \033[?25h: Show cursor
        # \033[lines;1H: Move to bottom line
        sys.stdout.write(f"\033[r\033[?25h\033[{lines};1H\n")
        sys.stdout.flush()
    except: pass

def get_choice_prompt(label: str, hint: str, show_cursor: bool = True) -> str:
    """Returns a styled prompt with a dark grey background and positional offset."""
    placeholder = f" {hint} "
    cursor_code = "\033[?25h" if show_cursor else "\033[?25l"
    return f"{cursor_code}\033[1;96m{label}\033[0m \033[48;5;236m\033[90m{placeholder}\033[0m\033[{len(placeholder)}D"

def print_choice_prompt(label: str, hint: str) -> None:
    """Prints a choice prompt at the current cursor position."""
    sys.stdout.write(get_choice_prompt(label, hint))
    sys.stdout.flush()

def clear_choice_placeholder() -> None:
    """Clears any remaining characters on the current line (usually placeholder text)."""
    sys.stdout.write("\033[K")
    sys.stdout.flush()

def split_title_description(text: str) -> tuple[str, str | None]:
    """Splits a title and description from strings like 'Title (description):' or 'Title: description'."""
    text = text.strip()
    if text.endswith(":"):
        text = text[:-1].strip()
    
    if "(" in text and ")" in text:
        start = text.find("(")
        end = text.rfind(")")
        title = text[:start].strip()
        description = text[start+1:end].strip()
        return title, description
    elif ":" in text:
        parts = text.split(":", 1)
        return parts[0].strip(), parts[1].strip()
    return text, None

def visible_width(text: str) -> int:
    """Calculates the visible column width of a string on a terminal,
    stripping ANSI escape sequences and accounting for wide characters/emojis."""
    clean = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\^\[\[?[A-Za-z0-9_~]*', '', text)
    width = 0
    for char in clean:
        ea = unicodedata.east_asian_width(char)
        if ea in ('W', 'F'):
            width += 2
        elif ord(char) >= 0x1F000 or (0x2600 <= ord(char) <= 0x27BF) or (0x2B00 <= ord(char) <= 0x2BFF):
            width += 2
        elif unicodedata.category(char) == 'Mn':
            width += 0
        else:
            width += 1
    return width


def fit_to_visible_width(text: str, max_width: int, ellipsis: str = "…") -> str:
    """Truncates text so that its visible width does not exceed max_width.
    Preserves text integrity and appends ellipsis if truncated."""
    if max_width <= 0:
        return ""
    if visible_width(text) <= max_width:
        return text
    
    ell_w = visible_width(ellipsis)
    target_w = max(1, max_width - ell_w)
    current_w = 0
    result_chars = []
    for char in text:
        char_w = visible_width(char)
        if current_w + char_w > target_w:
            break
        result_chars.append(char)
        current_w += char_w
    return "".join(result_chars).rstrip() + ellipsis


def get_header_string(text: str, subtitle: str | None = None) -> str:
    """Returns a centered header string with equals signs in cyan, responsive to terminal width."""
    text = text.strip().rstrip(":").upper()
    text = re.sub(r"\(([^)]*)\)", lambda match: f"({match.group(1).lower()})", text)
    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = max(80, visible_width(text) + 6)
    
    cols = max(20, cols)
    safe_cols = max(16, cols - 2)
    max_text_width = max(6, safe_cols - 4)
    if visible_width(text) > max_text_width:
        text = fit_to_visible_width(text, max_text_width)
    text_w = visible_width(text)
    avail_space = max(2, safe_cols - text_w - 2)
    left_pad_len = avail_space // 2
    right_pad_len = avail_space - left_pad_len
    left_padding = "=" * max(1, left_pad_len)
    right_padding = "=" * max(1, right_pad_len)
    header_line = f"\n\033[1;96m{left_padding} {text} {right_padding}\033[0m"
    if subtitle:
        sub_line = format_subtitle(subtitle)
        return f"{header_line}\n{sub_line}"
    return header_line


def print_header(text: str, subtitle: str | None = None) -> None:
    """Prints a centered header with equals signs, responsive to terminal width, with optional subtitle."""
    print(get_header_string(text, subtitle=subtitle))


def format_section_header(title: str) -> str:
    """Returns a consistently styled section header in bold white."""
    return f"\033[1;97m{title}\033[0m"


def print_section(title: str, subtitle: str | None = None) -> None:
    """Prints a section header in bold white and an optional subtitle in muted gray."""
    print(format_section_header(title))
    if subtitle:
        print_subtitle(subtitle)


def format_subtitle(text: str, hint: bool = False) -> str:
    """Returns a subtitle or hint string formatted with consistent muted colors."""
    if hint:
        return f"\033[1;90m{text}\033[0m"
    return f"\033[90m{text}\033[0m"


def print_subtitle(text: str, hint: bool = False) -> None:
    """Prints a subtitle or description line, ensuring proper style."""
    print(format_subtitle(text, hint=hint))


def print_phase(phase: str, subtext: str | None = None):
    p_map = {
        "planning": ("🧠", "PLANNING"),
        "scheduling": ("📡", "SCHEDULING"),
        "execution": ("🏗️", "EXECUTION"),
        "verification": ("🧪", "VERIFICATION"),
        "review": ("👀", "REVIEW"),
        "complete": ("✅", "COMPLETE"),
        "exporting": ("📦", "EXPORTING"),
        "delivery": ("🚀", "DELIVERY"),
        "investigation": ("🔍", "INVESTIGATION"),
        "implementation": ("⚙️", "IMPLEMENTATION"),
        "building": ("🔨", "BUILDING"),
        "testing": ("🧪", "TESTING"),
        "git_prep": ("🌿", "GIT PREP"),
        "status_update": ("📊", "STATUS UPDATE"),
        "debug_loop": ("🐞", "DEBUG LOOP"),
        "agent_thinking": ("💭", "AI THINKING"),
        "pull_request": ("🔀", "PULL REQUEST"),
        "cleanup": ("🧹", "CLEANUP"),
    }
    icon, label = p_map.get(phase.lower(), ("⚙️", phase.upper().replace("_", " ")))
    
    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = 80
        
    cols = max(20, cols)
    safe_cols = max(16, cols - 2)
    
    prefix = f"{icon} {label}"
    if subtext:
        header_text = f"{prefix}: {subtext.strip().upper()}"
    else:
        header_text = prefix
        
    max_text_w = max(6, safe_cols - 4)
    if visible_width(header_text) > max_text_w:
        if subtext:
            pref_w = visible_width(f"{prefix}: ")
            avail_sub_w = max(4, max_text_w - pref_w)
            short_sub = fit_to_visible_width(subtext.strip().upper(), avail_sub_w)
            header_text = f"{prefix}: {short_sub}"
            if visible_width(header_text) > max_text_w:
                header_text = fit_to_visible_width(header_text, max_text_w)
        else:
            header_text = fit_to_visible_width(header_text, max_text_w)
            
    text_w = visible_width(header_text)
    avail_space = max(2, safe_cols - text_w - 2)
    left_padding = avail_space // 2
    right_padding = avail_space - left_padding
    left_pad = "=" * max(1, left_padding)
    right_pad = "=" * max(1, right_padding)

    print(f"\n\033[1;96m{left_pad} {header_text} {right_pad}\033[0m", flush=True)


def get_phase_name(phase: str) -> str:
    return phase.replace("-", " ").capitalize()


def extract_step_from_line(line: str) -> str | None:
    """Parses step, phase, or sub-task description from stdout/stderr lines and returns a clean, user-friendly step label."""
    if not line:
        return None
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\^\[\[?[A-Za-z0-9_~]*')
    clean = ansi_escape.sub('', line).strip()
    if not clean:
        return None

    # Strip logging / stream prefixes e.g. "[stderr] ", "[stdout] ", "[tool] ", "[action] ", "[Codex] ", etc.
    payload = re.sub(r'^(?:\[(?:stderr|stdout|tool|action|progress|codex|claude|gemini|antigravity|opencode|error|info|log)\]\s*)+', '', clean, flags=re.IGNORECASE).strip()

    # 1. Match print_phase style: "=== 🧠 PLANNING ===" or "=== ⚙️ SUB-TASK 1/3: ADD AUTH SERVICE ==="
    if payload.startswith("==") and payload.endswith("=="):
        inner = payload.strip("= ")
        # Remove emojis at start
        inner = re.sub(r'^[^\w\s:]+\s*', '', inner).strip()
        
        # Sub-tasks: "SUB-TASK 1/3: ADD AUTH SERVICE" -> "Task 1/3: Add Auth Service"
        subtask_match = re.match(r'sub[- ]?task\s+(\d+/\d+)(?::\s*(.+))?', inner, re.IGNORECASE)
        if subtask_match:
            fraction = subtask_match.group(1)
            title = subtask_match.group(2)
            if title:
                clean_title = title.strip().title()
                return f"Task {fraction}: {clean_title}"
            return f"Working on Task {fraction}"

        # Debug loop: "DEBUG_LOOP: ITERATION 1 (PHASE: PROPOSE)" or "DEBUG 1/8: DIAGNOSING ERRORS"
        debug_match = re.match(r'debug(?:_loop)?(?:\s+(\d+/\d+))?(?::\s*(.+))?', inner, re.IGNORECASE)
        if debug_match:
            fraction = debug_match.group(1)
            sub = debug_match.group(2)
            if sub:
                sub_clean = sub.strip().replace("PHASE:", "").strip().title()
                if fraction:
                    return f"Debugging ({fraction}): {sub_clean}"
                return f"Debugging: {sub_clean}"
            return f"Debugging ({fraction})" if fraction else "Debugging issue"

        phase_lower = inner.lower()
        if "planning" in phase_lower:
            return "Planning feature"
        elif "scheduling" in phase_lower:
            if "syncing" in phase_lower:
                return "Syncing code to worker"
            return "Scheduling worker"
        elif "git_prep" in phase_lower or "git prep" in phase_lower:
            return "Preparing git branch"
        elif "status_update" in phase_lower or "status update" in phase_lower:
            return "Updating job status"
        elif "investigation" in phase_lower:
            return "Investigating issue"
        elif "agent_thinking" in phase_lower:
            if ":" in inner:
                sub = inner.split(":", 1)[1].strip().title()
                return f"AI Thinking: {sub}"
            return "AI reasoning in progress"
        elif "implementation" in phase_lower:
            return "Generating code changes"
        elif "building" in phase_lower:
            return "Building project (xcodebuild)"
        elif "testing" in phase_lower:
            return "Running test suite"
        elif "review" in phase_lower:
            return "Reviewing changes"
        elif "pull_request" in phase_lower or "pull request" in phase_lower:
            return "Creating pull request"
        elif "delivery" in phase_lower:
            return "Distributing build (Firebase)"
        elif "exporting" in phase_lower:
            return "Exporting job package"
        elif "execution" in phase_lower:
            return "Executing workflow"
        else:
            clean_name = inner.split(":", 1)[0].replace("_", " ").title()
            return clean_name

    # 2. Match bracketed steps: "[1/4] Preparing git branch: ai/issue-123..."
    bracket_match = re.match(r'^\[(\d+/\d+)\]\s*(.+)', payload)
    if bracket_match:
        step_num = bracket_match.group(1)
        rest = bracket_match.group(2).strip()
        rest = re.sub(r'\(.*?\)', '', rest).strip()
        rest = re.sub(r'\s+using\s+[\w\-.]+', '', rest).strip()
        rest = re.sub(r':\s*.*$', '', rest).strip()
        rest = rest.rstrip('.').strip()
        if rest:
            return f"[{step_num}] {rest}"

    # 3. Match AI Tool Calls (Read, Edit, Write, Run, Grep, Search, List)
    # E.g. "→ Read ThemisPlayground/ChatGPTAPI.swift [limit=60, offset=1]"
    tool_read = re.search(r'(?:→\s*(?:Read|View)\s+|\b(?:view_file|read_file|read_url_content)\s+|(?:Read|Reading|View)\s+file\s+)([^\s\[\],:]+)', payload, re.IGNORECASE)
    if tool_read:
        target = tool_read.group(1).strip("'\":`")
        name = Path(target).name
        if name and not name.lower().startswith("limit="):
            return f"Reading {name}"

    tool_edit = re.search(r'(?:→\s*(?:Edit|Patch)\s+|\b(?:replace_file_content|edit_file|patch_file)\s+|(?:Edit|Editing|Patch|Patching)\s+file\s+)([^\s\[\],:]+)', payload, re.IGNORECASE)
    if tool_edit:
        target = tool_edit.group(1).strip("'\":`")
        name = Path(target).name
        if name:
            return f"Editing {name}"

    tool_write = re.search(r'(?:→\s*(?:Write|Create)\s+|\b(?:write_to_file|create_file)\s+|(?:Write|Writing)\s+file\s+)([^\s\[\],:]+)', payload, re.IGNORECASE)
    if tool_write:
        target = tool_write.group(1).strip("'\":`")
        name = Path(target).name
        if name:
            return f"Writing {name}"

    tool_cmd = re.search(r'(?:→\s*(?:Run|Execute|Bash)\s+(?:command|cmd|bash|shell)?[:\s]*|\b(?:run_command|execute_command)[:\s]+|(?:Run|Running)\s+command:?\s+)([^\n]+)', payload, re.IGNORECASE)
    if tool_cmd:
        raw_cmd = tool_cmd.group(1).strip("'\":` ")
        tokens = raw_cmd.split()
        if tokens:
            cmd_name = Path(tokens[0]).name
            if len(tokens) > 1 and tokens[1] in ["test", "build", "archive", "clean", "diff", "status", "push", "pull", "commit"]:
                short_cmd = f"{cmd_name} {tokens[1]}"
            elif len(tokens) > 1 and not tokens[1].startswith("-"):
                short_cmd = f"{cmd_name} {tokens[1]}"
            else:
                short_cmd = cmd_name
            if len(short_cmd) > 28:
                short_cmd = short_cmd[:25] + "..."
            return f"Running: {short_cmd}"

    tool_search = re.search(r'(?:→\s*(?:Grep|Search)\s+|\b(?:grep_search|find_by_name|search_web)[:\s]+|(?:Search|Searching)\s+codebase:?\s+)([^\n]+)', payload, re.IGNORECASE)
    if tool_search:
        term = tool_search.group(1).strip()
        term_clean = term.strip("'\":` ")
        if len(term_clean) > 24:
            term_clean = term_clean[:21] + "..."
        return f"Searching: {term_clean}" if term_clean else "Searching codebase"

    tool_list = re.search(r'(?:→\s*List\s*(?:directory|dir)?[:\s]+|\b(?:list_dir|list_directory)[:\s]+|(?:List|Listing)\s+directory:?\s+)([^\n]+)', payload, re.IGNORECASE)
    if tool_list:
        dir_target = tool_list.group(1).strip("'\":` ")
        dir_name = Path(dir_target).name or dir_target
        return f"Listing {dir_name}" if dir_name else "Listing directory"

    # 4. Match specific test case / test suite execution lines (dynamic test feed)
    tc_start = re.search(r"Test [Cc]ase '?(?:-\[)?([^\s'\]]+)(?:\s+([^\s'\]]+))?\]?'?\s+started", payload)
    if tc_start:
        cls_name = tc_start.group(1)
        mth_name = tc_start.group(2)
        if mth_name:
            cls_part = cls_name.split(".")[-1]
            return f"Testing: {cls_part}.{mth_name}"
        else:
            parts = cls_name.split(".")
            name = ".".join(parts[-2:]) if len(parts) >= 2 else cls_name
            return f"Testing: {name}"

    st_start = re.search(r'Test "(?:-\[)?([^"]+)" started', payload)
    if st_start:
        name = st_start.group(1).replace("()", "")
        return f"Testing: {name}"

    xc_pass = re.search(r"(?:Passing|✔)\s+(?:-\[)?([A-Za-z0-9_\.]+)(?:\s+([A-Za-z0-9_]+))?\]?", payload)
    if xc_pass:
        cls_name = xc_pass.group(1)
        mth_name = xc_pass.group(2)
        if mth_name:
            cls_part = cls_name.split(".")[-1]
            return f"Passed: {cls_part}.{mth_name}"
        else:
            parts = cls_name.split(".")
            name = ".".join(parts[-2:]) if len(parts) >= 2 else cls_name
            return f"Passed: {name}"

    xc_fail = re.search(r"(?:Failing|✖|❌)\s+(?:-\[)?([A-Za-z0-9_\.]+)(?:\s+([A-Za-z0-9_]+))?\]?", payload)
    if xc_fail:
        cls_name = xc_fail.group(1)
        mth_name = xc_fail.group(2)
        if mth_name:
            cls_part = cls_name.split(".")[-1]
            return f"Failed: {cls_part}.{mth_name}"
        else:
            parts = cls_name.split(".")
            name = ".".join(parts[-2:]) if len(parts) >= 2 else cls_name
            return f"Failed: {name}"

    ts_start = re.search(r"Test Suite '([^']+)' started", payload) or re.search(r"Test Suite ([^\s]+) started", payload)
    if ts_start:
        suite = ts_start.group(1)
        if suite.lower() in ["all tests", "selected tests"] or suite.endswith(".xctest"):
            return "Running test suite"
        return f"Running suite: {suite}"

    summary_match = re.search(r"Executed (\d+) tests?, with (\d+) failures?", payload)
    if summary_match:
        tot = summary_match.group(1)
        fails = summary_match.group(2)
        return f"Finished {tot} tests ({fails} failures)"

    test_dest = re.search(r"Testing on '([^']+)'", payload)
    if test_dest:
        return f"Testing on {test_dest.group(1)}"

    # 5. Match compiler, test, tool, and progress lines
    if payload.startswith("CompileSwift normal") or payload.startswith("CompileSwift "):
        swift_file_match = re.search(r'/([^/\s]+\.swift)\b', payload)
        if swift_file_match:
            return f"Compiling {swift_file_match.group(1)}"
        return "Compiling Swift sources"
    elif payload.startswith("CompileSwiftSources"):
        return "Compiling Swift sources"
    elif payload.startswith("Compiling "):
        file_match = re.search(r'Compiling\s+([^\s]+)', payload)
        if file_match:
            return f"Compiling {Path(file_match.group(1)).name}"
        return "Compiling Swift sources"
    elif payload.startswith("CompileAssetCatalog"):
        return "Compiling asset catalog"
    elif payload.startswith("CompileStoryboard"):
        return "Compiling storyboards"
    elif payload.startswith("ProcessInfoPlistFile"):
        return "Processing Info.plist"
    elif payload.startswith("PhaseScriptExecution"):
        return "Running build scripts"
    elif payload.startswith("CodeSign"):
        return "Code signing application"
    elif payload.startswith("Ld ") or payload.startswith("Linking "):
        return "Linking binaries"
    elif payload.startswith("Fetching ") and ".git" in payload:
        repo_name = payload.split("/")[-1].replace(".git", "").split()[0]
        return f"Fetching {repo_name}"
    elif payload.startswith("Resolving package ") or payload.startswith("Resolved source packages"):
        return "Resolving package dependencies"
    elif "** TEST EXECUTE **" in payload or ("Test Suite" in payload and "started" in payload):
        return "Running test suite"
    elif "** TEST SUCCEEDED **" in payload or ("Test Suite" in payload and "passed" in payload):
        return "Tests succeeded"
    elif "** TEST FAILED **" in payload or ("Test Suite" in payload and "failed" in payload):
        return "Tests failed"
    elif "** BUILD SUCCEEDED **" in payload:
        return "Build succeeded"
    elif "** BUILD FAILED **" in payload:
        return "Build failed"
    elif "Generating (" in payload or ("received" in payload and "lines of response" in payload):
        return "Generating code"
    elif payload.startswith("Consulting "):
        return "Consulting AI model"
    elif payload.startswith("Creating GitHub issue"):
        return "Creating GitHub issue"
    elif "Archiving project" in payload or payload.startswith("Archiving "):
        return "Archiving project"
    elif "Exporting IPA" in payload or "Exporting " in payload:
        return "Exporting app package"
    elif "Uploading to Firebase" in payload or "Uploading " in payload:
        return "Uploading to Firebase"
    elif "Committing " in payload:
        return "Committing changes"
    elif "Syncing code to " in payload or "syncing code to" in payload.lower():
        return "Syncing code to worker"
    elif "Running build:" in payload:
        return "Building project"
    elif "Running tests:" in payload:
        return "Running test suite"
    elif "Analyzing errors" in payload:
        return "Analyzing build/test errors"

    return None


class LoopTroubleDetector:
    """
    Monitors process output streams and execution time for stuck loops,
    upstream API retries/overloads, repetitive tool calls, high step counts, and prolonged stalls.
    Emits clean, formatted notifications advising the user when they may want to abort.
    """

    def __init__(self, start_time: float | None = None):
        self.start_time = start_time if start_time is not None else time.monotonic()
        self.last_output_time = self.start_time
        self.last_notice_time: dict[str, float] = {}

        # Tracking
        self.upstream_errors: dict[str, int] = {}
        self.recent_tool_targets: list[str] = []
        self.step_count: int = 0
        self.fired_duration_milestones: set[int] = set()
        self.fired_step_milestones: set[int] = set()
        self.fired_idle_milestones: set[int] = set()

    def reset(self, start_time: float | None = None) -> None:
        self.start_time = start_time if start_time is not None else time.monotonic()
        self.last_output_time = self.start_time
        self.last_notice_time.clear()
        self.upstream_errors.clear()
        self.recent_tool_targets.clear()
        self.step_count = 0
        self.fired_duration_milestones.clear()
        self.fired_step_milestones.clear()
        self.fired_idle_milestones.clear()

    def _should_notify(self, key: str, cooldown: float = 45.0, now: float | None = None) -> bool:
        current_time = now if now is not None else time.monotonic()
        last = self.last_notice_time.get(key, 0.0)
        if current_time - last >= cooldown:
            self.last_notice_time[key] = current_time
            return True
        return False

    def record_chunk(self, chunk: str, now: float | None = None) -> list[str]:
        """Analyzes a chunk of streamed output and returns any trouble/loop notifications."""
        notices: list[str] = []
        for line in chunk.splitlines():
            line_notices = self.record_line(line, now=now)
            notices.extend(line_notices)
        return notices

    def record_line(self, line: str, now: float | None = None) -> list[str]:
        """Analyzes a single line for loop/trouble indicators."""
        current_time = now if now is not None else time.monotonic()
        self.last_output_time = current_time
        notices: list[str] = []

        if not line:
            return notices

        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\^\[\[?[A-Za-z0-9_~]*')
        clean = ansi_escape.sub('', line).strip()
        if not clean:
            return notices

        # Skip lines that are already orchestrator notice lines
        if any(marker in clean for marker in ["[Loop Notice]", "[Trouble Notice]", "[Duration Notice]", "[Stall Notice]", "[Step Notice]"]):
            return notices

        # 1. Detect Upstream API Errors & Overload / Retries
        upstream_match = self._match_upstream_error(clean)
        if upstream_match:
            err_type, err_detail = upstream_match
            self.upstream_errors[err_type] = self.upstream_errors.get(err_type, 0) + 1
            count = self.upstream_errors[err_type]

            # Notify on 2nd occurrence or if 1st severe error and cooldown permits
            if (count >= 2 or "overloaded" in err_type.lower() or "502" in err_type) and self._should_notify(f"upstream_{err_type}", cooldown=35.0, now=current_time):
                notice = (
                    f"\033[1;93m⚠️  [Trouble Notice] Upstream API issue detected: {err_detail} ({count} occurrence{'s' if count != 1 else ''}).\033[0m\n"
                    f"\033[96m   👉 If the remote model is overloaded or stuck in retry loops, press Ctrl-C to abort and choose another model.\033[0m"
                )
                notices.append(notice)

        # 2. Detect Tool Calls & Repetitive Action Loops
        step_label = extract_step_from_line(clean)
        if step_label:
            self.step_count += 1

            # Extract target identifier (file or command)
            target = self._extract_tool_target(clean, step_label)
            if target:
                self.recent_tool_targets.append(target)
                if len(self.recent_tool_targets) > 16:
                    self.recent_tool_targets.pop(0)

                # Check for repetition of same target (e.g. >= 4 times in recent window)
                target_occurrences = self.recent_tool_targets.count(target)
                if target_occurrences >= 4 and self._should_notify(f"loop_target_{target}", cooldown=45.0, now=current_time):
                    notice = (
                        f"\033[1;93m⚠️  [Loop Notice] Potential tool loop detected: Repeatedly accessing '{target}' ({target_occurrences} times).\033[0m\n"
                        f"\033[96m   👉 If the AI agent is looping without making progress, press Ctrl-C to abort and provide guidance or switch models.\033[0m"
                    )
                    notices.append(notice)

                # Check for ping-pong alternating pattern (A, B, A, B, A, B)
                if len(self.recent_tool_targets) >= 6:
                    r = self.recent_tool_targets[-6:]
                    if r[0] == r[2] == r[4] and r[1] == r[3] == r[5] and r[0] != r[1]:
                        if self._should_notify(f"pingpong_{r[0]}_{r[1]}", cooldown=45.0, now=current_time):
                            notice = (
                                f"\033[1;93m⚠️  [Loop Notice] Alternating ping-pong loop detected between '{r[0]}' and '{r[1]}'.\033[0m\n"
                                f"\033[96m   👉 If the agent is cycling between files without resolution, press Ctrl-C to abort.\033[0m"
                            )
                            notices.append(notice)

            # Check step milestones (15, 25, 40, 60, 80, 100)
            for threshold in [15, 25, 40, 60, 80, 100]:
                if self.step_count >= threshold and threshold not in self.fired_step_milestones:
                    self.fired_step_milestones.add(threshold)
                    if self._should_notify(f"step_milestone_{threshold}", cooldown=60.0, now=current_time):
                        notice = (
                            f"\033[1;93m💡 [Step Notice] Agent has executed {self.step_count} steps without concluding.\033[0m\n"
                            f"\033[96m   👉 You may press Ctrl-C to abort and steer with [Q] Ask AI, or switch models.\033[0m"
                        )
                        notices.append(notice)
                        break

        return notices

    def _match_upstream_error(self, line: str) -> tuple[str, str] | None:
        """Matches upstream API errors, 502/503/429 overloads, rate limits, connection failures."""
        line_lower = line.lower()
        if "502" in line or "bad gateway" in line_lower or "upstream error" in line_lower:
            return "502_upstream_error", "502 Upstream Error (Server temporarily overloaded)"
        if "503" in line or "service unavailable" in line_lower:
            return "503_unavailable", "503 Service Unavailable"
        if "504" in line or "gateway timeout" in line_lower:
            return "504_gateway_timeout", "504 Gateway Timeout"
        if "429" in line or "rate_limit" in line_lower or "too many requests" in line_lower or "resourceexhausted" in line_lower:
            return "429_rate_limit", "429 Rate Limit / Quota Exceeded"
        if "temporarily overloaded" in line_lower or "service overloaded" in line_lower:
            return "api_overloaded", "Remote AI service temporarily overloaded"
        if "retrying request" in line_lower or "retrying in " in line_lower:
            return "api_retrying", "API request failed; retrying automatically"
        if "connection refused" in line_lower or "connection reset" in line_lower:
            return "conn_reset", "API connection dropped / reset"
        return None

    def _extract_tool_target(self, line: str, step_label: str) -> str | None:
        """Extracts the file name or command target from the step label or line."""
        for prefix in ["Reading ", "Editing ", "Writing ", "Compiling "]:
            if step_label.startswith(prefix):
                return step_label[len(prefix):].strip()
        if step_label.startswith("Running: "):
            return step_label[len("Running: "):].strip()

        match = re.search(r'(?:→\s*(?:Read|Edit|Write|Patch|View)\s+|\b(?:replace_file_content|view_file|edit_file)\s+)([^\s\[\],:]+)', line, re.IGNORECASE)
        if match:
            return Path(match.group(1).strip("'\":`")).name
        return None

    def check_time_triggers(self, now: float | None = None, last_output_time: float | None = None) -> list[str]:
        """Checks duration and idle stall milestones."""
        current_time = now if now is not None else time.monotonic()
        notices: list[str] = []

        # 1. Total Elapsed Runtime Milestones (5m, 10m, 15m, 20m, 30m, 40m, 50m, 60m)
        elapsed_sec = int(current_time - self.start_time)
        elapsed_min = elapsed_sec // 60

        milestones = [5, 10, 15, 20, 30, 40, 50, 60]
        for m in milestones:
            if elapsed_min >= m and m not in self.fired_duration_milestones:
                self.fired_duration_milestones.add(m)
                step_str = f"Step {self.step_count}" if self.step_count > 0 else "in progress"
                notice = (
                    f"\033[1;93m💡 [Duration Notice] Task has been running for {m}m ({step_str}).\033[0m\n"
                    f"\033[96m   👉 If it appears stuck in loops or trouble, press Ctrl-C to abort at any time.\033[0m"
                )
                notices.append(notice)
                break

        # 2. Idle Stall Milestones (No output received for 2m, 4m, 6m)
        out_time = last_output_time or self.last_output_time
        idle_sec = int(current_time - out_time)
        idle_min = idle_sec // 60

        for stall_m in [2, 4, 6]:
            if idle_min >= stall_m and stall_m not in self.fired_idle_milestones:
                self.fired_idle_milestones.add(stall_m)
                if self._should_notify(f"stall_milestone_{stall_m}", cooldown=90.0, now=current_time):
                    notice = (
                        f"\033[1;93m💡 [Stall Notice] No output received for {stall_m}m (waiting on remote model/process).\033[0m\n"
                        f"\033[96m   👉 If the remote service is unresponsive or frozen, press Ctrl-C to abort.\033[0m"
                    )
                    notices.append(notice)
                    break

        return notices


def format_inline_markdown(text: str) -> str:
    # 1. Protect inline code blocks first (substitute with placeholder)
    code_placeholders = []
    def code_sub(match):
        code_placeholders.append(match.group(1))
        return f"\x00CODE{len(code_placeholders)-1}\x00"
    
    text = re.sub(r'`([^`]+)`', code_sub, text)
    
    # 2. Protect links
    link_placeholders = []
    def link_sub(match):
        label = match.group(1)
        url = match.group(2)
        link_placeholders.append((label, url))
        return f"\x00LINK{len(link_placeholders)-1}\x00"
    
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', link_sub, text)
    
    # 3. Replace bold
    text = re.sub(r'\*\*([^*]+)\*\*', r'\033[1;97m\1\033[0m', text)
    text = re.sub(r'__([^_]+)__', r'\033[1;97m\1\033[0m', text)
    
    # 4. Replace italics
    text = re.sub(r'\*([^*]+)\*', r'\033[3m\1\033[0m', text)
    text = re.sub(r'_([^_]+)_', r'\033[3m\1\033[0m', text)
    
    # 5. Restore links (and apply formatting to link label, but keep URL protected)
    for i, (label, url) in enumerate(link_placeholders):
        formatted_label = format_inline_markdown(label)
        link_str = f"\033[4;94m{formatted_label}\033[0m \033[90m({url})\033[0m"
        text = text.replace(f"\x00LINK{i}\x00", link_str)
        
    # 6. Restore inline code blocks
    for i, code_val in enumerate(code_placeholders):
        code_str = f"\033[1;93m{code_val}\033[0m"
        text = text.replace(f"\x00CODE{i}\x00", code_str)
        
    return text


def format_markdown_for_terminal(text: str) -> str:
    lines = text.splitlines()
    formatted_lines = []
    
    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = 80
        
    cols = max(20, cols)
    max_width = max(16, min(80, cols - 4))
        
    in_code_block = False
    code_block_lines = []
    
    import textwrap
    
    for line in lines:
        stripped = line.strip()
        
        # Handle code blocks
        if stripped.startswith("```"):
            if in_code_block:
                # End of code block: draw box
                border_w = max(2, max_width - 4)
                formatted_lines.append("  \033[90m┌" + "─" * border_w + "\033[0m")
                for c_line in code_block_lines:
                    formatted_lines.append(f"  \033[90m│\033[0m \033[92m{c_line}\033[0m")
                formatted_lines.append("  \033[90m└" + "─" * border_w + "\033[0m")
                code_block_lines = []
                in_code_block = False
            else:
                in_code_block = True
            continue
            
        if in_code_block:
            code_block_lines.append(line)
            continue
            
        # Headers
        if stripped.startswith("# "):
            title = stripped[2:]
            bar_len = max(1, min(len(title), max_width - 2))
            formatted_lines.append("")
            formatted_lines.append(f" \033[1;95m{title.upper()}\033[0m")
            formatted_lines.append(f" \033[1;95m" + "━" * bar_len + "\033[0m")
            formatted_lines.append("")
            continue
        elif stripped.startswith("## "):
            title = stripped[3:]
            bar_len = max(1, min(len(title), max_width - 2))
            formatted_lines.append("")
            formatted_lines.append(f" \033[1;96m{title}\033[0m")
            formatted_lines.append(f" \033[96m" + "─" * bar_len + "\033[0m")
            formatted_lines.append("")
            continue
        elif stripped.startswith("### "):
            title = stripped[4:]
            formatted_lines.append("")
            formatted_lines.append(f" \033[1;93m{title}\033[0m")
            formatted_lines.append("")
            continue
            
        # Bullet list item
        if stripped.startswith("- ") or stripped.startswith("* "):
            content = stripped[2:]
            wrapped = textwrap.wrap(content, width=max_width - 6, break_long_words=False, break_on_hyphens=False) or [""]
            for i, wl in enumerate(wrapped):
                formatted_wl = format_inline_markdown(wl)
                prefix = "  \033[1;96m•\033[0m " if i == 0 else "    "
                formatted_lines.append(f"{prefix}{formatted_wl}")
            continue
            
        # Numbered list item
        match = re.match(r"^(\d+)\.\s+(.*)", stripped)
        if match:
            num = match.group(1)
            content = match.group(2)
            wrapped = textwrap.wrap(content, width=max_width - 6, break_long_words=False, break_on_hyphens=False) or [""]
            for i, wl in enumerate(wrapped):
                formatted_wl = format_inline_markdown(wl)
                prefix = f"  \033[1;96m{num}.\033[0m " if i == 0 else "     "
                formatted_lines.append(f"{prefix}{formatted_wl}")
            continue
            
        # Empty lines
        if not stripped:
            formatted_lines.append("")
            continue
            
        # Standard paragraph line: wrap and format inline
        wrapped = textwrap.wrap(line, width=max_width - 2, break_long_words=False, break_on_hyphens=False) or [""]
        for wl in wrapped:
            formatted_wl = format_inline_markdown(wl)
            formatted_lines.append(f"  {formatted_wl}")
            
    return "\n".join(formatted_lines)
