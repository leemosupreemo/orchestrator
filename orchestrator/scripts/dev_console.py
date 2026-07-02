#!/usr/bin/env python3
from __future__ import annotations

import os
os.environ["AI_REQUEST_SOURCE"] = "dev_console"
import sys
import json
import re
import selectors
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Add scripts dir to path for internal imports
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

# Force line-buffering for stdout/stderr to ensure interactive scripts work well over pipes/subprocesses
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(line_buffering=True)
except:
    pass

from common import ROOT, CONFIG_DIR, JOBS_DIR, ARCHIVE_DIR, OUTPUT_DIR, DOCS_DIR, read_json, write_json, now_iso, prompt_radio, prompt_confirm, format_job_id, format_index, prompt_checkbox, BackException, KeyInterruptException, get_key, StatusBar, print_divider, extract_commands, print_phase, ProgressIndicator, get_test_plan_flags, print_choice_prompt, get_choice_prompt, clear_choice_placeholder, purge_zombie_processes, print_header, prompt_input, prompt_password, format_markdown_for_terminal
from llm import SUPPORTED_MODELS, DEFAULT_FALLBACKS, run_llm
from model_registry import get_all_models, ModelTier
from probe_machine import load_machines, probe_machine
from orchestrator.project_config import PACKAGE_ROOT, PROJECT_CONFIG

from orchestrator import __version__

def input(prompt: str = "") -> str:
    """Wrapper around get_key that avoids termios raw/cooked mode input lockups during pauses."""
    import builtins
    if any(phrase in prompt for phrase in ["Tap Enter", "Press Enter", "to return", "to continue", "to go back"]):
        sys.stdout.write(prompt)
        sys.stdout.flush()
        try:
            get_key(blocking=True)
        except (KeyboardInterrupt, EOFError):
            pass
        return ""
    
    try:
        return builtins.input(prompt)
    except (KeyboardInterrupt, EOFError):
        sys.stdout.write("\n")
        sys.stdout.flush()
        return ""

FLEET_AVAILABILITY: dict[str, bool] = {}
FLEET_DETAILS: dict[str, dict[str, Any]] = {}

SELF_TEST_STATIC_COMMANDS: dict[str, tuple[str, str, list[str]]] = {
    "3": ("Running Resume Logic Smoke Test", "smoke_test_cli_workflow.py", ["--scenario", "resume"]),
    "4": ("Running ALL Tooling Tests", "-m unittest discover", ["tests"]),
    "5": ("Running Console UI Smoke Tests", "-m unittest", ["tests/test_console_smoke.py"]),
    "6": ("Running Model Registry & Discovery Tests", "-m unittest", ["tests/test_model_registry.py"]),
    "7": ("Running Fleet-Wide LLM Connectivity Check", "fleet_llm_check.py", []),
    "8": ("Running Fleet Github Synchronization", "sync_fleet.py", []),
    "9": ("Running Local Model Connectivity Ping Tests", "live_check_workflow.py", ["--checks", "models", "--models", "all"]),
    "10": ("Running Build & Delivery Smoke Test", "smoke_test_delivery.py", []),
    "p": ("Plug & Play Self-Tests", "-m unittest", ["tests/test_check_setup.py"]),
}

UNAVAILABLE_MODEL_MARKERS = ("[Not Installed]", "[Not Enabled]")

import shutil

def get_model_selection_data() -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
    """
    Returns (options, value_map, details_map) for an intelligent model selection UI.
    - options: List of display labels for the checkbox (including category headers)
    - value_map: Map of display label -> model ID
    - details_map: Map of display label -> list of detail strings
    """
    models = get_all_models()
    
    # Sort by Family, then Tier (Extreme < High < Medium < Low)
    sorted_models = sorted(models, key=lambda m: (m.family, m.tier.value))
    
    family_display_names = {
        "claude": "ANTHROPIC (Claude)",
        "gemini": "GOOGLE (Antigravity)",
        "openai": "OPENAI (GPT)",
        "opencode": "OPENCODE",
        "deepseek": "DEEPSEEK (Ollama)",
        "copilot": "GITHUB (Copilot)",
        "codex": "OPENAI (Legacy)"
    }

    settings_path = CONFIG_DIR / "settings.json"
    settings = read_json(settings_path) if settings_path.exists() else {}

    # Cache for CLI auth check: {cli_name: is_authed}
    cli_auth_cache = {}
    ollama_models_cache: set[str] | None = None

    def is_cli_authed(cli: str) -> bool:
        if cli in cli_auth_cache:
            return cli_auth_cache[cli]

        binary = cli
        if cli == "gemini":
            if shutil.which("agy") is not None:
                binary = "agy"
            elif shutil.which("antigravity") is not None:
                binary = "antigravity"

        if shutil.which(binary) is None:
            cli_auth_cache[cli] = False
            return False

        ready = False
        try:
            if binary == "claude":
                res = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=1.5)
                ready = res.returncode == 0
            elif binary == "codex":
                res = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=1.5)
                ready = res.returncode == 0
            elif binary in {"gemini", "antigravity", "agy"}:
                ready = (os.path.exists(os.path.expanduser("~/.gemini/oauth_creds.json")) or 
                         os.path.exists(os.path.expanduser("~/.gemini/google_accounts.json")))
            elif binary == "opencode":
                res = subprocess.run(["opencode", "auth", "status"], capture_output=True, text=True, timeout=1.5)
                if res.returncode == 0:
                    ready = True
                else:
                    res2 = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=1.5)
                    ready = res2.returncode == 0 and bool(res2.stdout.strip())
            elif binary == "ollama":
                res = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=1.5)
                ready = res.returncode == 0
            elif binary == "gh":
                res = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=1.5)
                ready = res.returncode == 0
            else:
                ready = True
        except Exception:
            ready = False

        cli_auth_cache[cli] = ready
        return ready

    def get_installed_ollama_models() -> set[str]:
        nonlocal ollama_models_cache
        if ollama_models_cache is not None:
            return ollama_models_cache

        installed: set[str] = set()
        try:
            res = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=1.5)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split()
                    if not parts or parts[0].lower() == "name":
                        continue
                    name = parts[0].strip()
                    installed.add(name)
                    installed.add(name.split(":", 1)[0])
        except Exception:
            pass

        ollama_models_cache = installed
        return installed

    def required_ollama_model_id(model_id: str) -> str:
        if model_id == "deepseek":
            return "deepseek-coder"
        return model_id

    options = []
    value_map = {}
    details_map = {}
    
    current_family = None
    for m in sorted_models:
        if m.family != current_family:
            current_family = m.family
            name = family_display_names.get(current_family, current_family.upper())
            options.append(f"--- {name} ---")

        tier_str = m.tier.name.capitalize()
        alias_info = f" (Standard)" if m.aliases else ""
        effort_info = f" ({m.reasoning_effort})" if m.reasoning_effort else ""
        
        # Check if the model family has credentials configured
        has_api_key = False
        if m.family == "gemini":
            has_api_key = bool(settings.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY"))
        elif m.family == "claude":
            has_api_key = bool(settings.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"))
        elif m.family in {"openai", "codex"}:
            has_api_key = bool(settings.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or settings.get("codex_api_key") or os.environ.get("CODEX_API_KEY"))

        # Check if the required CLI is installed
        cli_installed = True
        cli_authed = True
        missing_cli = None
        missing_ollama_model = None
        resolved_clis = []
        
        for cli in m.required_clis:
            binary = cli
            if cli == "gemini":
                if shutil.which("agy") is not None:
                    binary = "agy"
                elif shutil.which("antigravity") is not None:
                    binary = "antigravity"
            resolved_clis.append(binary)
            if shutil.which(binary) is None:
                cli_installed = False
                missing_cli = cli
                break
            elif not is_cli_authed(cli):
                cli_authed = False
            elif binary == "ollama":
                ollama_model = required_ollama_model_id(m.id)
                if ollama_model not in get_installed_ollama_models():
                    cli_authed = False
                    missing_ollama_model = ollama_model

        # Overall readiness
        is_ready = (cli_installed and cli_authed) or has_api_key

        # Format label with suffix status
        status_suffix = ""
        if not cli_installed:
            status_suffix = " \033[1;90m[Not Installed]\033[0m"
        elif not is_ready:
            status_suffix = " \033[1;91m[Not Enabled]\033[0m"

        label = f"{m.id}{status_suffix}"
        
        options.append(label)
        value_map[label] = m.id

        if not cli_installed:
            access_str = "\033[1;90mNot installed\033[0m"
        elif is_ready:
            access_str = "\033[92mReady\033[0m"
        else:
            access_str = "\033[1;91mNot enabled\033[0m"

        if not m.required_clis:
            cli_str = "none"
        else:
            cli_str = ", ".join(resolved_clis)

        credential_sources = []
        if has_api_key:
            credential_sources.append("API key")
        if cli_installed and cli_authed and not missing_ollama_model:
            credential_sources.append("CLI login/local model")
        credential_str = ", ".join(credential_sources) if credential_sources else "none detected"
        
        details = [
            f"\033[1;97mModel:\033[0m        \033[90m{m.id}\033[0m",
            f"\033[1;97mAccess:\033[0m       {access_str}",
            f"\033[1;97mProvider:\033[0m     \033[90m{m.family.capitalize()}\033[0m",
            f"\033[1;97mBackend:\033[0m      \033[90m{cli_str}\033[0m",
            f"\033[1;97mCredentials:\033[0m  \033[90m{credential_str}\033[0m",
            f"\033[1;97mTier:\033[0m         \033[90m{tier_str}\033[0m",
            f"\033[1;97mCost:\033[0m         \033[90m{m.cost_factor:.1f}x\033[0m",
            f"\033[1;97mCapabilities:\033[0m \033[90m{', '.join([c.value.capitalize() for c in m.capabilities])}\033[0m"
        ]

        if "ollama" in m.required_clis:
            ollama_model = required_ollama_model_id(m.id)
            installed = ollama_model in get_installed_ollama_models()
            state = "\033[92minstalled\033[0m" if installed else "\033[1;91mmissing\033[0m"
            details.append(f"\033[1;97mLocal model:\033[0m  {state} \033[90m({ollama_model})\033[0m")
        
        if not cli_installed:
            details.append(f"\033[1;91mWarning: Missing required CLI '{missing_cli}'\033[0m")
        elif missing_ollama_model:
            details.append(f"\033[1;91mWarning: Ollama model '{missing_ollama_model}' is not installed. Run: ollama pull {missing_ollama_model}\033[0m")
        elif not is_ready:
            details.append(f"\033[1;91mWarning: CLI '{m.required_clis[0]}' is not logged in / authenticated\033[0m")
            
        if m.reasoning_effort:
            details.append(f"\033[1;97mEffort:\033[0m        \033[90m{m.reasoning_effort}\033[0m")
        if m.aliases:
            details.append(f"\033[1;97mAliases:\033[0m       \033[90m{', '.join(m.aliases)}\033[0m")
            
        details_map[label] = details
        
    return options, value_map, details_map

def print_model_selection_loading(title: str, status_bar: StatusBar | None = None):
    """Show the model selection screen before running slower availability checks."""
    clear_screen()
    print_header(title)
    print("\n\033[90mChecking model availability from local CLIs and configured API keys...\033[0m")
    print("\033[90mThis can take a few seconds when provider CLIs are slow to respond.\033[0m")
    if status_bar:
        status_bar.render(at_bottom=True, force=True)
    sys.stdout.flush()

def run_with_loading_screen(title: str, lines: list[str], label: str, status_bar: StatusBar | None, work):
    """Render a loading screen while a blocking menu data loader runs."""
    clear_screen()
    print_header(title)
    for line in lines:
        print(line)
    sys.stdout.flush()

    if not sys.stdout.isatty() or not status_bar:
        return work()

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}
    indicator = ProgressIndicator(label=label, hint="Ctrl-C to abort")

    def target() -> None:
        try:
            result["value"] = work()
        except BaseException as exc:
            error["value"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            status_bar.render(at_bottom=True, force=True, activity=indicator)
            time.sleep(0.1)
        thread.join()
    except KeyboardInterrupt:
        raise
    finally:
        status_bar.activity_indicator = None
        status_bar.render(at_bottom=True, force=True)

    if "value" in error:
        raise error["value"]
    return result.get("value")

def resolve_model_selection_defaults(allowed_models: list[str], value_map: dict[str, str]) -> list[str]:
    from model_registry import get_model

    resolved_ids = []
    for mid in allowed_models:
        if m := get_model(mid):
            resolved_ids.append(m.id)
        else:
            resolved_ids.append(mid)

    return [
        label
        for label, mid in value_map.items()
        if mid in resolved_ids and not any(marker in label for marker in UNAVAILABLE_MODEL_MARKERS)
    ]

def get_online_machines(allowed: list[str]) -> list[str]:
    return [m for m in allowed if FLEET_AVAILABILITY.get(m, False)]

def clear_screen():
    # Force reset scroll region and show cursor before clearing to prevent layout bugs
    from orchestrator.scripts.common import cleanup_terminal
    cleanup_terminal()
    os.system("clear" if os.name != "nt" else "cls")
    # Explicitly move to top-left to avoid misalignment if 'clear' didn't do it perfectly
    sys.stdout.write("\033[1;1H")
    sys.stdout.flush()

def list_jobs() -> list[dict[str, Any]]:
    job_files = sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    jobs = []
    for f in job_files:
        try:
            job = read_json(f)
            job["_path"] = f
            jobs.append(job)
        except Exception:
            continue
    return jobs

def refresh_job(job: dict[str, Any]) -> dict[str, Any]:
    """Safely re-reads the job JSON while preserving the internal _path field."""
    path = job.get("_path")
    if not path:
        return job
    new_job = read_json(Path(path))
    new_job["_path"] = Path(path)
    return new_job

def save_job(job: dict[str, Any]):
    """Saves job JSON while safely handling the internal _path field."""
    path = job.get("_path")
    if not path:
        return
    # Copy to avoid side effects on the live object
    data = dict(job)
    data.pop("_path", None)
    write_json(Path(path), data)

def format_job_row(idx: int, job: dict[str, Any], title_width: int = 30) -> str:
    status_map = {
        "planned": "plan",
        "executing": "exec",
        "review-needed": "ready",
        "human-needed": "human",
        "debugging": "debug",
        "decomposed": "appvd",
        "designing": "design"
    }
    status_colors = {
        "planned": "\033[1;96m",      # Blue
        "executing": "\033[93m",    # Yellow
        "review-needed": "\033[92m", # Green
        "human-needed": "\033[1;91m",  # Red
        "debugging": "\033[95m",     # Magenta
        "decomposed": "\033[90m",    # Grey",
        "designing": "\033[1;96m",     # Blue
    }
    reset = "\033[0m"

    status_raw = job.get("status", "unknown")
    color = status_colors.get(status_raw, reset)
    short_status = status_map.get(status_raw, status_raw[:6])

    # Prepend issue number if available for uniqueness
    issue_num = job.get("issue_number")
    raw_title = job.get("title", "Untitled")
    display_title = f"#{issue_num} {raw_title}" if issue_num else raw_title

    # Responsive title truncation
    title = display_title[:title_width]
    if len(display_title) > title_width:
        title = title[:max(0, title_width - 2)] + ".."

    # Write out full type: bug or feature
    job_type = job.get("type", "bug")
    if "bug" in job_type:
        type_str = "\033[1;91mbug    \033[0m"
    elif "feature" in job_type:
        type_str = "\033[95mfeature\033[0m"
    elif "design" in job_type:
        type_str = "\033[1;96mdesign \033[0m"
    else:
        type_str = f"\033[90m{job_type[:7]:7}\033[0m"

    # Log indicator: checkmark or X
    has_logs = job.get("last_manual_log_paths")
    if has_logs:
        log_display = "\033[92m✓\033[0m"
    else:
        log_display = "\033[90mx\033[0m"

    # New compact layout: [ID] | Type | Status | Title | Logs
    return f"{format_index(f'{idx:2}')} | {type_str} | {color}{short_status:6}{reset} | {title:{title_width}} | {log_display}"

def script_failure_summary(output_log: str) -> str | None:
    ansi_re = re.compile(r"\033\[[0-9;]*m")
    lines = [ansi_re.sub("", line).strip() for line in output_log.splitlines()]
    lines = [line for line in lines if line]

    diagnostic_titles = {
        "signing setup needs attention",
        "xcode apple id session needs attention",
    }
    for idx, line in enumerate(lines):
        if line.lower() not in diagnostic_titles:
            continue
        summary_lines = [line]
        for follow in lines[idx + 1:idx + 35]:
            if follow == "Next steps:":
                continue
            if (follow.startswith("Triggering final") or 
                follow.startswith("- Sending notifications") or 
                follow.startswith("Cleaning up") or 
                "smoke-test job" in follow or
                follow.startswith("❌")):
                break
            summary_lines.append(follow)
        return "\n".join(summary_lines)

    generic_failure_patterns = (
        "❌ Distribution failed",
        "❌ Distribution script failed",
        "❌ Smoke delivery failed",
        "❌ SMOKE TEST FAILED",
    )
    error_lines = []
    for line in lines:
        if line.startswith(generic_failure_patterns):
            continue
        if "❌" in line or line.lower().startswith("error:") or "exception:" in line.lower():
            if line not in error_lines:
                error_lines.append(line)
    if error_lines:
        return "\n".join(error_lines[:5])
    return None

def run_streaming_process(cmd: list[str], job: dict[str, Any] | None = None, sub_menu: bool = False, session_machines: list[str] | None = None, session_models: list[str] | None = None) -> tuple[int, str]:
    if job:
        job["online_machines"] = get_online_machines(job.get("allowed_machines", []))
    else:
        # Use session context if no specific job
        job = {
            "allowed_machines": session_machines or [],
            "online_machines": get_online_machines(session_machines or []),
            "allowed_models": session_models or []
        }
    
    show_status_bar = sys.stdout.isatty()
    last_render = 0.0
    last_output_time = time.monotonic()
    indicator = ProgressIndicator(label="Thinking", hint="Ctrl-C to abort")
    output_chunks = []

    with StatusBar(job, is_processing=True, sub_menu=sub_menu) as status_bar:
        if show_status_bar:
            status_bar.set_scroll_region()
        
        # Suppress inner progress indicators to avoid double spinners in the console
        sub_env = os.environ.copy()
        sub_env["AI_PROGRESS_SILENT"] = "1"
            
        process = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, # Don't share stdin, it confuses things
            text=False, # Use bytes for non-blocking compatibility
            bufsize=0,
            env=sub_env,
        )

        selector = selectors.DefaultSelector()
        if process.stdout:
            selector.register(process.stdout, selectors.EVENT_READ)

        def render_status_bar(force: bool = False) -> None:
            nonlocal last_render
            if not show_status_bar:
                return
            now = time.monotonic()
            if force or now - last_render >= 0.5:
                status_bar.render(at_bottom=True, force=force, activity=indicator)
                last_render = now


        try:
            render_status_bar(force=True)
            while process.poll() is None:
                # Update UI (throttled to 0.5s inside render_status_bar)
                render_status_bar()

                # Check for direct key interrupts (non-blocking)
                try:
                    key = get_key(blocking=False)
                    # We don't need to manually check Ctrl-C here as get_key() raises KeyboardInterrupt
                except KeyboardInterrupt:
                    # Explicitly kill the subprocess on interrupt
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except:
                        process.kill()
                    raise # Re-raise to take user back to menu

                events = selector.select(timeout=0.05) # Faster poll for smooth spinner
                if not events:
                    continue

                for event_key, _ in events:
                    # Use os.read for truly non-blocking raw read
                    try:
                        fd = event_key.fileobj.fileno()
                        chunk = os.read(fd, 16384) # Larger buffer for high-volume logs
                        if chunk:
                            last_output_time = time.monotonic()
                            data = chunk.decode("utf-8", errors="replace")
                            output_chunks.append(data)
                            
                            # Safety: Force cursor back into scroll region (bottom line)
                            # before printing remote logs. This prevents 'last line only' bug
                            # if the remote machine sent cursor movements.
                            _, lines = os.get_terminal_size()
                            sys.stdout.write(f"\033[{lines-4};1H")
                            
                            # Print directly to stdout and flush immediately
                            sys.stdout.write(data)
                            sys.stdout.flush()
                    except (OSError, ValueError):
                        pass

            # Final read after process exit: make sure we read everything till EOF
            if process.stdout:
                try:
                    os.set_blocking(process.stdout.fileno(), True)
                except Exception:
                    pass
                while True:
                    try:
                        chunk = os.read(process.stdout.fileno(), 8192)
                        if not chunk:
                            break
                        data = chunk.decode("utf-8", errors="replace")
                        output_chunks.append(data)
                        indicator.clear()
                        sys.stdout.write(data)
                        sys.stdout.flush()
                    except (OSError, ValueError):
                        break
        finally:
            indicator.clear()
            selector.close()
            # Ensure we are in a clean state before final render
            if show_status_bar:
                # Fully clear the footer area before resetting region
                status_bar.clear_footer()
                status_bar.reset_scroll_region()

    return process.returncode or 0, "".join(output_chunks)

def run_script(script_name: str, args: list[str], job: dict[str, Any] | None = None, sub_menu: bool = False, session_machines: list[str] | None = None, session_models: list[str] | None = None, prompt: str | None = None):
    import shlex
    if script_name == "-m unittest" or script_name == "-m unittest discover":
        cmd = [sys.executable, "-u"] + script_name.split() + args
    else:
        script_path = SCRIPTS_DIR / script_name
        cmd = [sys.executable, "-u", str(script_path)] + args

    print(f"\nRunning: {shlex.join(cmd)}\n")

    # Extract and emphasize chosen tests
    test_targets = []
    for arg in args:
        if "-only-testing:" in arg:
            parts = arg.split("-only-testing:")
            for part in parts[1:]:
                test_targets.append(part.split()[0].strip())

    if test_targets:
        try:
            cols, _ = os.get_terminal_size()
        except Exception:
            cols = 80
        box_width = max(45, min(60, cols - 2))
        target_str = ", ".join(test_targets)
        print("\033[1;95m" + "★" * box_width)
        print(f"  🧪 TESTS CHOSEN TO BE RUN: {target_str}")
        print("★" * box_width + "\033[0m\n")

    returncode = 1
    try:
        # Reset terminal state, clear screen, and move cursor to top-left before running any subprocess
        # \033[r: Reset scroll region
        # \033[2J: Clear entire screen
        # \033[H: Move cursor to 1,1
        # \033[?25h: Show cursor
        sys.stdout.write("\033[r\033[2J\033[H\033[?25h")
        sys.stdout.flush()
        
        # Pre-flight zombie purge for job execution scripts
        if script_name in ["schedule_job.py", "worker_run.py", "deliver_build.py"]:
            machines = session_machines or (job.get("session_machines", []) if job else [])
            if machines:
                purged = purge_zombie_processes(machines, silent=True)
                if purged > 0:
                    print(f"🧹 Automatically purged {purged} zombie processes before execution.\n")

        # Special case: interactive or simple local scripts should take over terminal directly
        if script_name in ["new_job.py", "check_setup.py", "discover_machines.py", "smoke_test_delivery.py"]:
            sub_env = os.environ.copy()
            if sub_menu:
                sub_env["AI_PROGRESS_SILENT"] = "1"
            returncode = subprocess.run(cmd, cwd=str(ROOT), stdin=sys.stdin, stdout=None, stderr=None, env=sub_env).returncode
            output_log = ""
        else:
            returncode, output_log = run_streaming_process(cmd, job=job, sub_menu=sub_menu, session_machines=session_machines, session_models=session_models)
            
        if returncode != 0:
            print(f"\n\033[1;91mScript failed (exit {returncode}).\033[0m")

            # Try to provide a succinct summary
            summary = None

            # 1. If we have a job object, look for recent AI analysis
            if job and "job_id" in job:
                out_dir = OUTPUT_DIR / job["job_id"]
                if out_dir.exists():
                    # Find newest analysis file
                    analyses = sorted(list(out_dir.glob("*_analysis_*.md")), key=lambda x: x.stat().st_mtime, reverse=True)
                    if analyses:
                        content = analyses[0].read_text(encoding="utf-8")

                        # Extract "Top blocker" or "Likely causes"
                        match = re.search(r"## Top blocker\n(.*?)(?:\n##|$)", content, re.DOTALL)
                        if match:
                            summary = "Blocker: " + match.group(1).strip()

            # 2. Extract diagnostics or error lines from the output log if no summary found
            if not summary and output_log:
                summary = script_failure_summary(output_log)
                if not summary:
                    lines = [line.strip() for line in output_log.splitlines() if line.strip()]
                    if lines:
                        recent_lines = []
                        for line in reversed(lines):
                            if len(recent_lines) >= 5:
                                break
                            if line.startswith("$ ") or "progress" in line.lower():
                                continue
                            recent_lines.insert(0, line)
                        if not recent_lines:
                            recent_lines = lines[-5:]
                        summary = "Recent output:\n  " + "\n  ".join(recent_lines)

            if summary:
                print("\n\033[1;93mFailure summary\033[0m")
                print(summary)
            else:
                print("\n\033[90m(No detailed summary available from artifacts)\033[0m")
        else:
            print("\n\033[92m", end="")
            print_divider("=")
            print("  ✅  SCRIPT COMPLETED SUCCESSFULLY")
            print_divider("=")
            print("\033[0m", end="")

    except KeyboardInterrupt:
        print("\nProcess interrupted by user.")
    finally:
        # Guarantee that terminal line-wrapping and newline translation are restored
        # in case a crashed subprocess left the tty in raw or cbreak mode.
        if os.name != "nt":
            os.system("stty sane 2>/dev/null")
        sys.stdout.write("\r")
        sys.stdout.flush()
    
    # Use provided prompt or a descriptive default
    final_prompt = prompt if prompt is not None else "\n\033[1;96mTap Enter to return to main menu...\033[0m"
    if final_prompt:
        try:
            input(final_prompt)
        except (KeyboardInterrupt, EOFError):
            pass
            
    return returncode

def handle_new_job(session_allowed_models: list[str] | None = None, session_allowed_machines: list[str] | None = None):
    try:
        # 1. Critical Pre-requisite Checks
        if not session_allowed_machines:
            print("\n\033[1;91m⚠️  ERROR: No active machines selected for this session.\033[0m")
            print("A job requires at least one worker machine to execute build and test tasks.")
            print("\n\033[1;97mTo fix this:\033[0m")
            print("1. Go to [\033[1;96mC\033[0m] Configuration \033[1;96m->\033[0m [\033[1;96mF\033[0m] Manage Machine Fleet")
            print("2. Ensure at least one machine is enabled and reachable (green checkmark).")
            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            return

        if not session_allowed_models:
            print("\n\033[1;91m⚠️  ERROR: No AI models selected for this session.\033[0m")
            print("A job requires at least one LLM to perform planning and code generation.")
            print("\n\033[1;97mTo fix this:\033[0m")
            print("1. Go to [\033[1;96mC\033[0m] Configuration \033[1;96m->\033[0m [\033[1;96mM\033[0m] Manage Model Selection")
            print("2. Select at least one AI model. Ensure you have the required CLI tools logged in.")
            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            return

        print_header("Create New Job")
        job_options = [
            "✨ Brand New Feature (Design-First)",
            "🛠️ Iterating / Small Refactor (Plan or Quick Mode)",
            "🐞 Bug Fix (Identify + Fix)",
            "🎨 Design Prototype (Can promote to Implementation)",
            "🧪 Test Coverage Audit (Maintenance)"
        ]
        choice = prompt_radio("Select Job Type:", job_options, job_options[1])

        # Map back to internal type
        job_type = "feature"
        stitch_mode = False

        if "Brand New" in choice:
            job_type = "feature"
            stitch_mode = True
        elif "Iterating" in choice:
            if prompt_confirm("Use Quick Mode? (skips heavy planning for small changes/refactors)", default=False):
                job_type = "quick"
            else:
                job_type = "feature"
            stitch_mode = False
        elif "Bug Fix" in choice:
            job_type = "bug"
            stitch_mode = False
        elif "Design Prototype" in choice:
            job_type = "design"
            stitch_mode = True
        elif "Test Coverage" in choice:
            job_type = "coverage"
            stitch_mode = False

        spec_file = None
        if job_type != "quick":
            if prompt_confirm("Load spec from a local file or web address? (useful for large multi-page docs)", default=False):
                # We try to help the user with a list of files in ROOT if they start typing
                print("\n    (Enter path to .md/.txt file OR a web URL)")
                spec_file = prompt_input("Spec Path/URL:", placeholder="feature_spec.md or https://gist.../raw")

        yolo = prompt_confirm("YOLO mode? (select YOLO to auto-dispatch job after planning)", default=False)
        
        branch_options = ["new (creates a new branch to work in)", "current-branch (git pull)", "manual (no git actions)"]
        branch_choice = prompt_radio("Branch selection:", branch_options, branch_options[0])
        
        # Map friendly UI names back to what new_job.py expects
        clean_branch_choice = "new"
        if "current-branch" in branch_choice:
            clean_branch_choice = "current"
        elif "manual" in branch_choice:
            clean_branch_choice = "manual"
        elif "new (" in branch_choice:
            clean_branch_choice = "new"
        else:
            # Fallback to the first word if it's something unknown
            clean_branch_choice = branch_choice.split(" ")[0].lower()
        
        advanced = False
        if job_type != "quick":
            advanced = prompt_confirm("Show advanced options? (Configure LLM presets, model overrides, and machine filtering)", default=False)
        
        # Map friendly UI names back to what new_job.py expects
        args = [job_type, "--branch-mode", clean_branch_choice]
        
        if stitch_mode:
            args.append("--stitch")
        
        if spec_file:
            args.extend(["--spec-file", spec_file])
        
        if session_allowed_models:
            args.extend(["--allowed-models", ",".join(session_allowed_models)])
        if session_allowed_machines:
            args.extend(["--allowed-machines", ",".join(session_allowed_machines)])
        
        if advanced:
            preset_options = [
                "--- Recommended Tiers ---",
                "balanced (Antigravity/Codex/Antigravity) (Standard balance of cost and quality)",
                "fast (Codex/Codex/Antigravity) (Cheapest/fastest models for simple fixes)",
                "--- Professional Tiers ---",
                "hard-bug (Antigravity/Claude-Opus/Antigravity) (Opus for complex logic and debugging)",
                "architecture (Claude-Opus/Claude-Opus/Antigravity) (Opus for both planning and building)",
                "--- Custom ---",
                "skip (manual setup) (Select each model role manually)"
            ]
            preset_choice = prompt_radio("LLM Model Configuration - Select a Preset:", preset_options, "balanced (Antigravity/Codex/Antigravity) (Standard balance of cost and quality)")
            
            # Extract preset key from choice (everything before the first space)
            preset = preset_choice.split(" ")[0]
            if "skip" in preset:
                model_opts = ["antigravity", "claude-opus-4-7", "codex"]
                planner = prompt_radio("Select Planner:", model_opts, "antigravity")
                builder = prompt_radio("Select Builder:", model_opts, "claude-opus-4-7")
                reviewer = prompt_radio("Select Reviewer:", model_opts, "antigravity")
                args.extend(["--planner", planner, "--builder", builder, "--reviewer", reviewer])
            else:
                args.extend(["--preset", preset])
                
        if not yolo:
            args.append("--no-dispatch")
        else:
            args.append("--yolo")
        
        run_script("new_job.py", args)
    except BackException:
        return

def check_machine_availability(machine: dict[str, Any]) -> bool:
    """Quickly checks if a machine is reachable."""
    mode = machine.get("execution_mode", "local")
    if mode == "local":
        return True
    
    ssh_target = machine.get("ssh_target")
    if not ssh_target:
        return False
    
    if isinstance(ssh_target, str):
        ssh_target = [ssh_target]
    
    # Try just the first target with a very short timeout
    target = ssh_target[0]
    try:
        # Just check if we can connect to SSH port
        res = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=2", "-o", "BatchMode=yes", target, "exit"],
            capture_output=True,
            check=False
        )
        return res.returncode == 0
    except:
        return False

def get_machine_ip_and_tailscale(machine: dict[str, Any]) -> dict[str, Any]:
    """Retrieves the IP address and tailscale enabled status for a machine."""
    import socket
    import subprocess
    import json
    import shutil
    import os

    mode = machine.get("execution_mode", "local")
    if mode == "local":
        ts_paths = ["tailscale", "/Applications/Tailscale.app/Contents/Resources/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/tailscale", "/opt/tailscale/bin/tailscale", "/usr/local/bin/tailscale"]
        ts_bin = None
        for path in ts_paths:
            resolved = shutil.which(path)
            if resolved:
                ts_bin = resolved
                break
            if os.path.exists(path):
                ts_bin = path
                break
        
        ts_enabled = False
        ts_ip = None
        if ts_bin:
            try:
                res = subprocess.run([ts_bin, "status", "--json"], capture_output=True, text=True, timeout=1.5)
                if res.returncode == 0:
                    data = json.loads(res.stdout)
                    if data.get("BackendState") == "Running":
                        ts_enabled = True
                        ips = data.get("TailscaleIPs")
                        if ips and isinstance(ips, list):
                            ts_ip = ips[0]
            except Exception:
                pass
                
        # Primary IP
        primary_ip = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('10.254.254.254', 1))
            primary_ip = s.getsockname()[0]
            s.close()
        except Exception:
            primary_ip = "127.0.0.1"
            
        if ts_enabled and ts_ip:
            return {"ip": ts_ip, "tailscale": True}
        return {"ip": primary_ip or "127.0.0.1", "tailscale": ts_enabled}
        
    else:
        # SSH machine
        ssh_targets = machine.get("ssh_target")
        if not ssh_targets:
            return {"ip": "unknown", "tailscale": False}
        if isinstance(ssh_targets, str):
            targets = [ssh_targets]
        else:
            targets = list(ssh_targets)
            
        if not targets:
            return {"ip": "unknown", "tailscale": False}
            
        target = targets[0]
        host = target.split("@")[-1] if "@" in target else target
        
        is_ts_ip = host.startswith("100.")
        ts_enabled = is_ts_ip
        
        is_online = FLEET_AVAILABILITY.get(machine.get("name"), False)
        if is_online:
            try:
                res = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=1.5", "-o", "BatchMode=yes", target, "tailscale status --json"],
                    capture_output=True,
                    text=True,
                    timeout=2.0
                )
                if res.returncode == 0:
                    data = json.loads(res.stdout)
                    if data.get("BackendState") == "Running":
                        ts_enabled = True
                        ips = data.get("TailscaleIPs")
                        if ips and isinstance(ips, list):
                            host = ips[0]
                else:
                    res2 = subprocess.run(
                        ["ssh", "-o", "ConnectTimeout=1.5", "-o", "BatchMode=yes", target, "which tailscale && tailscale status"],
                        capture_output=True,
                        text=True,
                        timeout=2.0
                    )
                    if res2.returncode == 0 and "Logged out" not in res2.stdout and "stopped" not in res2.stdout.lower():
                        ts_enabled = True
            except Exception:
                pass
                
        return {"ip": host, "tailscale": ts_enabled}

def refresh_fleet_status(machines_config: list[dict[str, Any]]) -> None:
    """Checks reachability and gets IP/Tailscale info for all machines in parallel."""
    import threading
    global FLEET_AVAILABILITY, FLEET_DETAILS
    
    def worker(m):
        name = m["name"]
        is_online = check_machine_availability(m)
        FLEET_AVAILABILITY[name] = is_online
        if is_online or m.get("execution_mode", "local") == "local":
            FLEET_DETAILS[name] = get_machine_ip_and_tailscale(m)
        else:
            FLEET_DETAILS[name] = {"ip": "unknown", "tailscale": False}

    threads = []
    for m in machines_config:
        t = threading.Thread(target=worker, args=(m,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

def discover_and_add_machines(session_allowed_machines: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    from discover_machines import discover_machine_candidates, machine_entry_from_candidate

    print_header("Discovering Remote Machines")
    candidates = discover_machine_candidates()
    suitable = candidates.get("suitable", [])
    needs_keys = candidates.get("needs_keys", [])
    unsuitable = candidates.get("unsuitable", [])

    if not suitable and not needs_keys and not unsuitable:
        print("\nNo remote candidates found (excluding local machine).")
        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
        return load_machines(), session_allowed_machines

    machines_config = load_machines()
    existing_names = {m.get("name") for m in machines_config}
    existing_targets = set()
    for m in machines_config:
        target = m.get("ssh_target")
        if isinstance(target, list):
            existing_targets.update(target)
        elif target:
            existing_targets.add(target)

    added_names: list[str] = []
    if suitable:
        print(f"\nFound {len(suitable)} suitable machine(s). Review each candidate below.")

    for idx, candidate in enumerate(suitable, 1):
        entry = machine_entry_from_candidate(candidate, repo_path=str(ROOT))
        name = entry["name"]
        target = entry["ssh_target"]

        print(f"\n--- Candidate {idx} of {len(suitable)}: \033[97m{candidate.get('hostname', target)}\033[0m ---")
        print(f"  SSH Target: \033[90m{target}\033[0m")
        print(f"  Python:     \033[90m{candidate.get('python', 'unknown')}\033[0m")
        print(f"  Xcode:      \033[90m{candidate.get('xcode', 'unknown')}\033[0m")

        if name in existing_names or target in existing_targets:
            print("  \033[90mAlready configured; skipping.\033[0m")
            continue

        if not prompt_confirm(f"Add {name} to the fleet now?", default=True):
            continue

        repo_path = prompt_input("Remote repo path:", default=str(ROOT), placeholder=str(ROOT))
        if not repo_path:
            repo_path = str(ROOT)
        entry["repo_path"] = repo_path

        machines_config.append(entry)
        existing_names.add(name)
        existing_targets.add(target)
        added_names.append(name)
        if name not in session_allowed_machines:
            session_allowed_machines.append(name)
        print(f"  ✅ Added \033[97m{name}\033[0m")

    if needs_keys:
        print(f"\n\033[93mSSH setup needed for {len(needs_keys)} discovered machine(s):\033[0m")
        for candidate in needs_keys:
            print(f"  - {candidate.get('host')}: {candidate.get('error', 'SSH authentication failed')}")

    if unsuitable:
        print(f"\n\033[90mSkipped {len(unsuitable)} unsuitable machine(s).\033[0m")

    if added_names:
        write_json(CONFIG_DIR / "machines.json", {"version": 1, "machines": machines_config})
        print(f"\n✅ Saved {len(added_names)} machine(s) to \033[97m{CONFIG_DIR / 'machines.json'}\033[0m")
    else:
        print("\nNo new machines added.")

    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
    return machines_config, session_allowed_machines

def remove_machine_from_fleet(machine_name: str, session_allowed_machines: list[str]) -> tuple[list[dict[str, Any]], list[str], bool]:
    machines_config = load_machines()
    target = next((m for m in machines_config if m.get("name") == machine_name), None)
    if not target:
        print(f"\n\033[1;91mMachine not found: {machine_name}\033[0m")
        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
        return machines_config, session_allowed_machines, False

    if len(machines_config) <= 1:
        print("\n\033[1;91mCannot remove the only configured machine.\033[0m")
        print("Add another machine before removing this one.")
        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
        return machines_config, session_allowed_machines, False

    print(f"\n  Removing machine: \033[97m{machine_name}\033[0m")
    ssh_target = target.get("ssh_target")
    if ssh_target:
        print(f"  SSH Target: \033[90m{ssh_target}\033[0m")

    if not prompt_confirm("Remove this machine from the fleet?", default=False):
        print("⚠️  No changes made.")
        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
        return machines_config, session_allowed_machines, False

    machines_config = [m for m in machines_config if m.get("name") != machine_name]
    session_allowed_machines = [m for m in session_allowed_machines if m != machine_name]
    write_json(CONFIG_DIR / "machines.json", {"version": 1, "machines": machines_config})
    FLEET_AVAILABILITY.pop(machine_name, None)
    FLEET_DETAILS.pop(machine_name, None)

    print(f"✅ Removed \033[97m{machine_name}\033[0m")
    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
    return machines_config, session_allowed_machines, True

def handle_fleet_management(session_allowed_machines: list[str]) -> list[str]:
    global FLEET_AVAILABILITY
    
    # Pre-check availability for all machines (in parallel)
    machines_config = load_machines()
    print("\n\033[93mPinging fleet for availability...\033[0m")
    refresh_fleet_status(machines_config)
        
    # Automatically de-select offline machines from the session
    session_allowed_machines = [m for m in session_allowed_machines if FLEET_AVAILABILITY.get(m, False)]

    # Limit session machines to 10 max
    if len(session_allowed_machines) > 10:
        session_allowed_machines = session_allowed_machines[:10]

    while True:
        clear_screen()
        
        # Setup status bar
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines)
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Machine Fleet Management")
            
            if len(machines_config) > 10:
                print(f"\n    \033[93m⚠️  FLEET SIZE WARNING: {len(machines_config)} machines configured.\033[0m")
                print("    Performance degrades above 10 machines due to sequential probing.")
                print("    Please select up to 10 active machines for this session below.\n")

            machine_names = sorted([m["name"] for m in machines_config])
            
            # Build details map for each machine
            details_map = {}
            for m in machines_config:
                name = m.get("name", "unknown")
                is_online = FLEET_AVAILABILITY.get(name, False)
                status_str = "\033[92mONLINE\033[0m" if is_online else "\033[1;91mOFFLINE\033[0m"
                capabilities = []
                if m.get("supports_xcode"):
                    capabilities.append("Xcode")
                if m.get("supports_simulator"):
                    capabilities.append("Simulator")
                if m.get("supports_backend_tests"):
                    capabilities.append("Backend tests")
                
                models_list = m.get("models", [])
                models_str = ", ".join(models_list) or "none"
                if len(models_str) > 60:
                    models_str = f"{len(models_list)} models ({models_str[:50]}...)"

                details_info = FLEET_DETAILS.get(name, {})
                ip_val = details_info.get("ip")
                if not ip_val:
                    # Fallback extraction
                    ssh_targets = m.get("ssh_target")
                    if ssh_targets:
                        target = ssh_targets[0] if isinstance(ssh_targets, list) else ssh_targets
                        ip_val = target.split("@")[-1] if "@" in target else target
                    else:
                        ip_val = "127.0.0.1" if m.get("execution_mode") == "local" else "unknown"
                        
                ts_val = details_info.get("tailscale", False)
                ts_str = "\033[92menabled\033[0m" if ts_val else "\033[90mdisabled\033[0m"

                details = [
                    f"\033[1;97mStatus\033[0m       {status_str} (Tailscale: {ts_str})",
                    f"\033[1;97mIP/Mode\033[0m      \033[90m{ip_val}\033[0m ({m.get('execution_mode', 'unknown')})",
                    f"\033[1;97mRoles/Pri\033[0m    \033[90m{', '.join(m.get('roles', [])) or 'none'}\033[0m (Priority: \033[90m{m.get('priority', 'N/A')}\033[0m)",
                    f"\033[1;97mModels\033[0m       \033[90m{models_str}\033[0m",
                    f"\033[1;97mCapabilities\033[0m \033[90m{', '.join(capabilities) or 'none'}\033[0m"
                ]
                details_map[name] = details
            
            print(f"\033[90mConfigured machines: {len(machines_config)}\033[0m\n")
            
            try:
                # We use prompt_checkbox with 'd' as an extra key for discovery and 'n' for rename
                # Enforce 10 machine limit via max_selections
                footer = (
                    "[\033[1;92mD\033[0m] Discover remote machines\n"
                    "[\033[1;92mN\033[0m] Rename selected machine\n"
                    "[\033[1;91mR\033[0m] Remove selected machine\n"
                    "[\033[1;91mZ\033[0m] Zombie purge & cache cleanup\n"
                    "[\033[1;91mB\033[0m] Back"
                )
                new_allowed = prompt_checkbox(
                    "Active Machines for This Session",
                    machine_names,
                    session_allowed_machines,
                    extra_keys=["d", "n", "r", "z", "b"],
                    footer=footer,
                    details_map=details_map,
                    details_title="Selected Machine",
                    status_bar=status_bar,
                    max_selections=10
                )
                
                if not new_allowed:
                    print("\n\033[1;91m⚠️  ERROR: You must select at least one machine to continue.\033[0m")
                    print("The Orchestrator cannot function without a compute node.")
                    input("\n\033[1;96mPress Enter to go back and select a machine...\033[0m")
                    continue

                return new_allowed
            except BackException:
                if not session_allowed_machines:
                    print("\n\033[1;91m⚠️  ERROR: No machines selected.\033[0m")
                    print("Please select at least one machine before going back.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue
                return session_allowed_machines
            except KeyInterruptException as exc:
                if exc.key == "b":
                    if not session_allowed_machines:
                        print("\n\033[1;91m⚠️  ERROR: No machines selected.\033[0m")
                        print("Please select at least one machine before going back.")
                        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                        continue
                    return session_allowed_machines
                if exc.key == "d":
                    machines_config, session_allowed_machines = discover_and_add_machines(session_allowed_machines)
                    print("\n\033[93mRe-pinging fleet...\033[0m")
                    refresh_fleet_status(machines_config)
                    continue
                elif exc.key == "n" and exc.value:
                    old_name = exc.value
                    print(f"\n  Renaming machine: \033[97m{old_name}\033[0m")
                    try:
                        new_name = prompt_input("Enter new nickname:", placeholder="(or Enter to cancel)")
                    except BackException:
                        continue
                    if new_name and new_name != old_name:
                        # Update machines.json
                        m_config = load_machines()
                        found = False
                        for m in m_config:
                            if m["name"] == old_name:
                                m["name"] = new_name
                                found = True
                                break
                        if found:
                            settings_path = CONFIG_DIR / "machines.json"
                            # We need to maintain the same structure
                            full_config = {"version": 1, "machines": m_config}
                            write_json(settings_path, full_config)
                            
                            # Update session mapping if needed
                            if old_name in session_allowed_machines:
                                session_allowed_machines.remove(old_name)
                                session_allowed_machines.append(new_name)
                                
                            # Update active machines config and availability mapping
                            machines_config = m_config
                            if old_name in FLEET_AVAILABILITY:
                                FLEET_AVAILABILITY[new_name] = FLEET_AVAILABILITY.pop(old_name)
                            
                            print(f"✅ Machine renamed to: \033[97m{new_name}\033[0m")
                            # Hide cursor during Tap Enter prompt
                            sys.stdout.write("\033[?25l")
                            sys.stdout.flush()
                            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    else:
                        print("⚠️  No changes made.")
                        # Hide cursor during Tap Enter prompt
                        sys.stdout.write("\033[?25l")
                        sys.stdout.flush()
                        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue
                elif exc.key == "r" and exc.value:
                    machines_config, session_allowed_machines, removed = remove_machine_from_fleet(exc.value, session_allowed_machines)
                    if removed:
                        print("\n\033[93mRe-pinging fleet...\033[0m")
                        refresh_fleet_status(machines_config)
                    continue
                elif exc.key == "z":
                    handle_fleet_hygiene(session_allowed_machines)
                    continue
                return session_allowed_machines

def handle_tooling_tests(session_allowed_machines: list[str], session_allowed_models: list[str]):
    error_msg = ""
    while True:
        clear_screen()
        
        # Setup status bar
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Tooling & AI Test Menu")
            
            print("\n  \033[1;90m--- AI INTEGRATION (Xcode/Swift) ---\033[0m")
            print("  [\033[1;96m0\033[0m] Full Integrated Workflow (Swift-to-Python Bridge)")
            print("  [\033[1;96m1\033[0m] Tooling & Build Logic   (Xcode build/indexing)")
            print("  [\033[1;96m2\033[0m] Machine Probing Tests   (SSH & Fleet Environment)")

            print("\n  \033[1;90m--- WORKFLOW SMOKE TESTS (Mock) ---\033[0m")
            print("  [\033[1;96m3\033[0m] Resume & State Recovery (End-to-End Simulation)")

            print("\n  \033[1;90m--- SCRIPT LOGIC (Python Unit) ---\033[0m")
            print("  [\033[1;96m4\033[0m] Full Python Test Suite  (All isolated unit tests)")
            print("  [\033[1;96m5\033[0m] Console UI Smoke Tests  (Menu navigation & UI logic)")
            print("  [\033[1;96m6\033[0m] Model Registry & Discovery (routing aliases, sync, fallbacks)")

            print("\n  \033[1;90m--- FLEET OPERATIONS (Live) ---\033[0m")
            print("  [\033[1;96m7\033[0m] Fleet Health Report     (all machines report)")
            print("  [\033[1;96m8\033[0m] GitHub Metadata Sync    (status & PR cleanup)")
            print("  [\033[1;96m9\033[0m] Local Model Pings       (connectivity & logs)")
            print("  [\033[1;96m10\033[0m] Build & Delivery       (Live Firebase upload)")

            print("\n  \033[1;90m--- ENVIRONMENT & SETUP ---\033[0m")
            print("  [\033[1;96mP\033[0m] Plug & Play Self-Tests  (Setup logic verification)")

            print("\n[\033[1;91mB\033[0m] Back")
            
            if error_msg:
                print(f"\n\033[1;91mNOT A VALID OPTION, PLEASE TRY AGAIN... ({error_msg})\033[0m")
                error_msg = ""

            # Anchor prompt to bottom
            prompt = get_choice_prompt("Choice:", "(number or letter)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()
            
            if not choice: continue
            print(choice)
            
            if choice == "b":
                break
            elif choice == "0":
                print_header("Running System Workflow Tests")
                build_cmd, test_cmd = extract_commands()
                run_script("manual_run.py", ["test", "--test-only", f"{test_cmd} -only-testing:{PROJECT_CONFIG.test_target}/WorkflowTests"], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
            elif choice == "1":
                print_header("Running LLM & Tool Logic Tests")
                build_cmd, test_cmd = extract_commands()
                run_script("manual_run.py", ["test", "--test-only", f"{test_cmd} -only-testing:{PROJECT_CONFIG.test_target}/LLMTests"], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
            elif choice == "2":
                print_header("Running Machine Probing Tests")
                build_cmd, test_cmd = extract_commands()
                run_script("manual_run.py", ["test", "--test-only", f"{test_cmd} -only-testing:{PROJECT_CONFIG.test_target}/ProbingTests"], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
            elif choice in SELF_TEST_STATIC_COMMANDS:
                header, script_name, args = SELF_TEST_STATIC_COMMANDS[choice]
                print_header(header)
                run_script(script_name, args, sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
            else:
                error_msg = f"'{choice}'"

def check_origin_update_status(pkg_dir: Path) -> dict[str, Any]:
    """Best-effort check for remote package updates without changing the worktree."""
    try:
        subprocess.run(
            ["git", "fetch", "--quiet", "origin"],
            cwd=str(pkg_dir),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(pkg_dir),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if not branch or branch == "HEAD":
            return {"state": "unknown", "reason": "Current checkout is detached."}

        remote_ref = f"origin/{branch}"
        counts = subprocess.check_output(
            ["git", "rev-list", "--left-right", "--count", f"HEAD...{remote_ref}"],
            cwd=str(pkg_dir),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip().split()
        ahead = int(counts[0])
        behind = int(counts[1])
        if behind == 0:
            return {"state": "current", "ahead": ahead, "behind": behind, "remote_ref": remote_ref}
        if ahead:
            return {"state": "diverged", "ahead": ahead, "behind": behind, "remote_ref": remote_ref}
        return {"state": "behind", "ahead": ahead, "behind": behind, "remote_ref": remote_ref}
    except Exception as e:
        return {"state": "unknown", "reason": str(e)}

DOC_MENU_DESCRIPTIONS = {
    "getting-started.md": "Start here: what Orchestrator does and the first commands to run.",
    "user-guide.md": "Full setup, project selection, commands, and troubleshooting.",
    "dev-console.md": "How to use the interactive console and common job actions.",
    "ai-workflow.md": "How planning, building, review, and verification fit together.",
    "recommended-mcp-plugins.md": "Optional MCP servers, plugins, and extensions that improve workflows.",
    "architecture.md": "Internal architecture for contributors and maintainers.",
    "build-test-commands.md": "Canonical build and test commands agents should use.",
    "coding-standards.md": "Repo conventions for code style, testing, and docs.",
    "migration-guide.md": "Moving from older local scripts to the package workflow.",
    "README.md": "Project overview, install, features, and quick start.",
    "AGENTS.md": "Instructions coding agents should follow in this repo.",
    "AI_AGENT_SETUP.md": "Project-specific AI agent setup notes.",
}

DOC_MENU_ORDER = [
    "getting-started.md",
    "user-guide.md",
    "dev-console.md",
    "ai-workflow.md",
    "recommended-mcp-plugins.md",
    "architecture.md",
    "build-test-commands.md",
    "coding-standards.md",
    "migration-guide.md",
    "README.md",
    "AGENTS.md",
    "AI_AGENT_SETUP.md",
]

ORCHESTRATOR_HELP_DOCS_DIR = PACKAGE_ROOT.parent / "docs"
ORCHESTRATOR_HELP_DOC_NAMES = [
    "getting-started.md",
    "user-guide.md",
    "recommended-mcp-plugins.md",
]

def doc_menu_sort_key(path: Path) -> tuple[int, str]:
    try:
        idx = DOC_MENU_ORDER.index(path.name)
    except ValueError:
        idx = len(DOC_MENU_ORDER)
    return idx, path.name.lower()

def doc_path_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except Exception:
        return str(path)

def unique_doc_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = doc_path_key(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique

def doc_menu_plain_name(path: Path) -> str:
    return path.stem.replace("-", " ").title()

def handle_update_orchestrator(session_allowed_machines: list[str]):
    clear_screen()
    print_header("Update Orchestrator")
    
    from orchestrator.project_config import PACKAGE_ROOT
    pkg_dir = PACKAGE_ROOT.parent
    is_git = (pkg_dir / ".git").exists()
    
    print(f"  Current Version: \033[97mv{__version__}\033[0m")
    print(f"  Package Location: \033[90m{pkg_dir}\033[0m")
    
    if is_git:
        print("\n  \033[1;96mGit Repository detected.\033[0m")
        print("  Checking origin for updates...")
        update_status = check_origin_update_status(pkg_dir)
        state = update_status.get("state")
        should_update = False

        if state == "current":
            ahead = update_status.get("ahead", 0)
            suffix = f" Local branch is {ahead} commit(s) ahead." if ahead else ""
            print(f"  \033[92mAlready up to date with {update_status.get('remote_ref', 'origin')}.\033[0m{suffix}")
        elif state == "behind":
            behind = update_status.get("behind", 0)
            print(f"  \033[1;93m{behind} update(s) available from {update_status.get('remote_ref', 'origin')}.\033[0m")
            should_update = prompt_confirm("Pull available updates and re-install locally?", default=True)
        elif state == "diverged":
            ahead = update_status.get("ahead", 0)
            behind = update_status.get("behind", 0)
            print(f"  \033[1;93mRemote has {behind} update(s), and this branch has {ahead} local commit(s).\033[0m")
            should_update = prompt_confirm("Pull from origin anyway and re-install locally?", default=False)
        else:
            print(f"  \033[1;93mCould not check origin automatically: {update_status.get('reason', 'unknown error')}\033[0m")
            should_update = prompt_confirm("Pull latest changes from origin and re-install locally?", default=True)

        if should_update:
            print("\n  Updating local package...")
            try:
                subprocess.run(["git", "pull"], cwd=str(pkg_dir), check=False)
                subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."], cwd=str(pkg_dir), check=False)
                print("\n  ✅ Local update complete.")
            except Exception as e:
                print(f"\n  ❌ Local update failed: {e}")
    else:
        print("\n  \033[1;93mNote: Local source code not found in a Git repository.\033[0m")
        print("  If you installed via pipx, run: \033[97mpipx upgrade orchestrator\033[0m")
        print("  If you installed via pip, run:  \033[97mpip install --upgrade orchestrator\033[0m")

    # Fleet Update
    machines = load_machines()
    remote_machines = [m for m in machines if m.get("execution_mode") == "ssh" and m.get("enabled", True)]
    if remote_machines:
        print(f"\n  Detected {len(remote_machines)} enabled remote worker(s).")
        if prompt_confirm("Update the orchestrator package on all remote workers?", default=True):
            for m in remote_machines:
                print(f"\n  - Updating {m['name']}...")
                run_script("worker_tools.py", ["install", "--machine", m["name"]])
            print("\n  ✅ Fleet update complete.")

    input("\n\033[1;96mTap Enter to return to menu...\033[0m")

def handle_app_tests(session_allowed_machines: list[str], session_allowed_models: list[str]):
    error_msg = ""
    while True:
        clear_screen()
        
        # Setup status bar
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("App Unit & UI Test Menu")
            
            # Dynamic Test Plans
            test_plans_root = ROOT / PROJECT_CONFIG.test_target / "TestPlans"
            test_plans = sorted(test_plans_root.glob("*.xctestplan"))
            
            plan_map = {}
            print("\n  \033[1;90m--- XCODE TEST PLANS ---\033[0m")
            if test_plans:
                for i, tp in enumerate(test_plans):
                    key = str(i + 1)
                    rel_path = tp.relative_to(ROOT)
                    plan_map[key] = tp
                    print(f"  [\033[1;96m{key}\033[0m] {tp.stem} ({rel_path})")
            else:
                print("  \033[90mNo Xcode Test Plans found.\033[0m")
                print("\n  \033[1;97mHow to add tests:\033[0m")
                print("    1. In Xcode: File > New > File...")
                print("    2. Select 'Test Plan' and save to: " + PROJECT_CONFIG.test_target + "/TestPlans/")
                print("    3. Add your Unit/UI test targets to the plan.")
                print("    4. They will automatically appear here for the AI to use.")

            print("\n  \033[1;90m--- VISUAL CHECKS ---\033[0m")
            print("  [\033[1;96mV\033[0m] Simulator Visual Check (build, launch, screenshots)")

            print("\n  [\033[1;91mB\033[0m] Back")
            
            if error_msg:
                print(f"\n\033[1;91mNOT A VALID OPTION, PLEASE TRY AGAIN... ({error_msg})\033[0m")
                error_msg = ""

            # Anchor prompt to bottom
            prompt = get_choice_prompt("Choice:", "(number or letter)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()
            
            if not choice: continue
            
            if choice.isdigit():
                is_ambiguous = any(len(k) > 1 and k.startswith(choice) for k in plan_map.keys())
                if is_ambiguous:
                    sys.stdout.write(choice)
                    sys.stdout.flush()
                    choice = choice + input().strip().lower()
                else:
                    print(choice)
            else:
                print(choice)
            
            if choice == "b":
                break
            elif choice in plan_map:
                plan_path = plan_map[choice]
                print_header(f"Running Test Plan: {plan_path.stem}")
                flags = get_test_plan_flags(plan_path)
                run_script("manual_run.py", ["test", "--test-only", flags], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
            elif choice == "v":
                print_header("Running Simulator Visual Check")
                run_script("simulator_visual_check.py", [], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
            else:
                error_msg = f"'{choice}'"

def archive_job(job: dict[str, Any], status: str = "completed"):
    print(f"      - Archiving job {job.get('job_id')}...")
    job["status"] = status
    job["completed_at"] = now_iso()
    
    job_path = Path(job["_path"])
    archive_path = ARCHIVE_DIR / job_path.name
    
    # Remove _path from dict before saving to avoid issues
    path_to_save = job.pop("_path")
    write_json(path_to_save, job)
    
    import shutil
    shutil.move(str(path_to_save), str(archive_path))
    return archive_path

def handle_cleanup_closed(silent: bool = False, confirm: bool = True):
    if not silent: print_phase("cleanup", subtext="syncing with github")
    jobs = list_jobs()
    if not jobs:
        print("No active jobs to check.")
        if not silent and confirm:
            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
        return

    print(f"Checking {len(jobs)} jobs...")
    
    # Get all closed issues in one go for efficiency
    try:
        closed_issues_raw = subprocess.check_output(
            ["gh", "issue", "list", "--state", "closed", "--limit", "1000", "--json", "number"],
            cwd=str(ROOT)
        ).decode("utf-8")
        closed_numbers = {issue["number"] for issue in json.loads(closed_issues_raw)}
    except Exception as e:
        print(f"!!! Error fetching closed issues: {e}")
        if not silent and confirm:
            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
        return

    archived_count = 0
    for job in jobs:
        issue_num = job.get("issue_number")
        if issue_num and issue_num in closed_numbers:
            print(f"  - Issue #{issue_num} is closed. Archiving...")
            archive_job(job, status="completed")
            archived_count += 1
            
    if archived_count > 0:
        print(f"\n✅ Sync complete: Archived {archived_count} jobs found closed on GitHub.")
    else:
        print(f"\n✅ Sync complete: Verified {len(jobs)} active jobs are still open on GitHub.")
    
    if not silent and confirm:
        input("\n\033[1;96mTap Enter to return to menu...\033[0m")

def auto_link_latest_logs(job: dict[str, Any]):
    """Intelligently links the 3 absolute newest logs across manual and job-specific sources."""
    candidates = []
    
    # 1. Check job-specific output directory
    job_out_base = OUTPUT_DIR / job.get("job_id", "unknown")
    if job_out_base.exists():
        # Find timestamped .log and .txt files
        job_logs = list(job_out_base.glob("*_202[0-9]-*.log")) + list(job_out_base.glob("*_202[0-9]-*.txt"))
        # Also include the 'test.log' and 'build.log' if they are fresh (modified in last 12h)
        for standard in ["test.log", "build.log", "test_results.txt"]:
            p = job_out_base / standard
            if p.exists() and (time.time() - p.stat().st_mtime) < 43200: # 12h
                candidates.append(p)
        candidates.extend(job_logs)

    # 2. Check manual logs directory
    manual_base = OUTPUT_DIR / "manual"
    if manual_base.exists():
        log_dirs = [d for d in manual_base.iterdir() if d.is_dir() and d.name != "latest"]
        candidates.extend(log_dirs)
    
    if not candidates:
        return
        
    # Sort all candidates by mtime (newest first)
    candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    
    # Take top 3 unique paths
    log_paths = []
    seen_names = set()
    for c in candidates:
        if len(log_paths) >= 3: break
        
        try:
            rel = str(c.relative_to(ROOT))
        except:
            rel = str(c)
            
        if rel not in seen_names:
            log_paths.append(rel)
            seen_names.add(rel)
            
    if log_paths:
        print(f"      - Auto-linking {len(log_paths)} newest logs across all sources...")
        job["last_manual_log_paths"] = log_paths
        save_job(job)

def prompt_for_logs(job: dict[str, Any]) -> list[str]:
    manual_base = OUTPUT_DIR / "manual"
    job_out_base = OUTPUT_DIR / job.get("job_id", "unknown")
    from common import format_log_path
    
    # We use a mapping so we can show readable names but return the actual paths
    display_options = []
    option_to_path = {}
    
    def add_log_option(full_path):
        try:
            rel = str(full_path.relative_to(ROOT))
        except:
            rel = str(full_path)
        
        label = format_log_path(rel)
        display_options.append(label)
        option_to_path[label] = rel

    # 1. Job-specific timestamped logs (from previous runs)
    if job_out_base.exists():
        # Find all .log and .txt files with timestamps
        log_files = sorted(list(job_out_base.glob("*_202[0-9]-*.log")) + list(job_out_base.glob("*_202[0-9]-*.txt")), 
                          key=lambda x: x.stat().st_mtime, reverse=True)[:5]
        for f in log_files:
            add_log_option(f)

    # 2. Recent manual log directories
    if manual_base.exists():
        log_dirs = sorted([d for d in manual_base.iterdir() if d.is_dir() and d.name != "latest"], 
                         key=lambda x: x.name, reverse=True)[:3]
        for d in log_dirs:
            add_log_option(d)
    
    log_paths = []
    
    if not display_options:
        print(f"\nNo log entries found for this job or in manual folder.")
        print("Or select a custom log file path by pressing L")
        print("\n[\033[1;96mL\033[0m] Custom Log Path")
        print("[\033[1;91mB\033[0m] Back")
        print_choice_prompt("\nChoice:", "(action)")
        sub_choice = get_key().strip().lower()
        clear_choice_placeholder()
        print(sub_choice)
        if sub_choice == "l":
            path = prompt_input("Enter custom log path:", placeholder="(or Enter to cancel)")
            if path: log_paths.append(path)
        else:
            raise BackException()
    else:
        paste_opt = "Paste New Logs..."
        custom_opt = "Custom Path..."
        
        # Determine defaults (currently linked)
        curr_logs = job.get("last_manual_log_paths", [])
        if not curr_logs and job.get("last_manual_log_path"):
            curr_logs = [job.get("last_manual_log_path")]
            
        # Convert curr_logs to formatted labels for the checkbox defaults
        curr_labels = [format_log_path(l) for l in curr_logs]

        print_header("Link Logs for Context")
        all_options = [paste_opt, custom_opt] + display_options
        selected = prompt_checkbox("Select logs to link for this job:", all_options, curr_labels)
        
        from manual_run import capture_logs
        for s in selected:
            if s == paste_opt:
                ts = now_iso().replace(":", "").replace("-", "")[:15]
                manual_out = manual_base / ts
                manual_out.mkdir(parents=True, exist_ok=True)
                log_file = capture_logs(manual_out)
                if log_file:
                    log_paths.append(str(manual_out.relative_to(ROOT)))
            elif s == custom_opt:
                path = input("Enter custom log path \033[90m(or Enter to skip)\033[0m: ").strip()
                if path: log_paths.append(path)
            else:
                # Map back to actual path if it's in our mapping, otherwise use the label
                log_paths.append(option_to_path.get(s, s))
                
    return log_paths

def prompt_for_reference_artifact(job: dict[str, Any]) -> bool:
    from reference_artifacts import attach_reference_artifact

    print_header("Attach UI Mockup / Reference")
    print("Supported local files: html, htm, png, jpg, jpeg, webp, gif, pdf, md, txt, css, json, fig")
    print("Supported URLs: http/https, including Figma links stored as URL references.")
    print()

    source = prompt_input("Enter local file path or URL:", placeholder="(or Enter to cancel)")
    if not source: return False
    note = prompt_input("Optional note for agents:", placeholder="(e.g. target screen/state)")


    try:
        artifact = attach_reference_artifact(job, source, note)
    except Exception as exc:
        print(f"\n\033[1;91mCould not attach reference: {exc}\033[0m")
        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
        return False

    artifacts = job.get("reference_artifacts", [])
    artifacts.append(artifact)
    job["reference_artifacts"] = artifacts
    job["updated_at"] = now_iso()
    save_job(job)

    label = artifact.get("url") or artifact.get("path")
    print(f"\n✅ Attached reference: {label}")
    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
    return True

def perform_job_revert(job: dict[str, Any]) -> int:
    """Surgically reverts files modified/added by AI for this job. Returns count of files reverted."""
    ai_modified = job.get("ai_modified_files", [])
    ai_untracked = job.get("ai_untracked_files", [])
    count = 0

    if ai_modified or ai_untracked:
        print(f"      - Reverting {len(ai_modified) + len(ai_untracked)} files...")

        # Revert modified
        for f in ai_modified:
            print(f"        [revert] {f}")
            subprocess.run(["git", "checkout", "--", f], cwd=str(ROOT), capture_output=True)
            count += 1

        # Delete untracked
        for f in ai_untracked:
            full_path = ROOT / f
            if full_path.exists():
                print(f"        [delete] {f}")
                if full_path.is_dir():
                    import shutil
                    shutil.rmtree(full_path)
                else:
                    os.remove(full_path)
                count += 1
    return count

def handle_discard_job(job: dict[str, Any]):
    print_header(f"Discarding & Reverting Job: {job.get('job_id')}")

    warning = "\033[1;91m⚠️  WARNING: THIS WILL PERMANENTLY DELETE ALL LOCAL PROGRESS & CODE CHANGES.\033[0m\n"
    warning += "Are you sure you want to DISCARD this job and REVERT its changes?"
    if not prompt_confirm(warning, default=False):
        return

    # 1. Surgical File Revert
    count = perform_job_revert(job)
    if count == 0:
        print("      - No granular AI change log found. Falling back to branch management.")

    # 2. Branch Management
    branch = job.get("branch")

    if branch:
        try:
            curr = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ROOT)).decode("utf-8").strip()
        except:
            curr = ""
            
        if branch.startswith("ai/issue-"):
            print(f"      - AI-managed branch detected: {branch}")
            if curr == branch:
                base_branch = job.get("base_branch", PROJECT_CONFIG.base_branch)
                print(f"      - Switching to base branch {base_branch}...")
                subprocess.run(["git", "checkout", base_branch], cwd=str(ROOT), capture_output=True)
            
            print(f"      - Deleting branch {branch}...")
            subprocess.run(["git", "branch", "-D", branch], cwd=str(ROOT), capture_output=True)
        else:
            if not ai_modified and not ai_untracked:
                print(f"      - Note: Branch '{branch}' is not AI-managed.")
                print(f"        You may need to manually run 'git reset --hard HEAD' to discard changes.")

    # Archive with discarded status
    archive_path = archive_job(job, status="discarded")
    print(f"\n✅ Job discarded and AI changes surgically reverted.")
    input("\n\033[1;96mTap Enter to return to menu...\033[0m")

def handle_merge_cleanup(job: dict[str, Any]):
    pr_number = job.get("pr_number")
    branch = job.get("branch")

    if not pr_number:
        print("!!! Error: No PR number found for this job.")
        return

    print_header(f"Merging & Cleaning up PR #{pr_number}")

    # 1. Proactive Draft Check
    try:
        pr_view = subprocess.check_output(["gh", "pr", "view", str(pr_number), "--json", "isDraft"], cwd=str(ROOT)).decode("utf-8")
        is_draft = json.loads(pr_view).get("isDraft", False)
        if is_draft:
            print("\n\033[1;91m!!! Error: PR is still a DRAFT.\033[0m")
            print("Please mark the PR as 'Ready for Review' on GitHub before merging.")
            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            return
    except Exception as e:
        print(f"      - Note: Could not verify PR draft status: {e}")

    # 2. Attempt Merge
    print(f"      - Merging PR #{pr_number}...")
    merge_res = subprocess.run(["gh", "pr", "merge", str(pr_number), "--merge", "--delete-branch"], cwd=str(ROOT))

    if merge_res.returncode != 0:
        print("\n\033[1;91m!!! Error: PR merge failed.\033[0m")
        print("The merge was blocked by GitHub (check draft status, CI failures, or conflicts).")
        print("\033[93mLocal branch cleanup ABORTED to preserve your work.\033[0m")
        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
        return

    # 3. Local branch cleanup (ONLY if merge succeeded and AI-managed)
    if branch:
        if branch.startswith("ai/issue-"):
            print(f"      - Deleting local branch: {branch}...")
            # Check current branch first
            try:
                curr = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ROOT)).decode("utf-8").strip()
                if curr == branch:
                    subprocess.run(["git", "checkout", PROJECT_CONFIG.base_branch], cwd=str(ROOT), capture_output=True)
            except:
                pass
            
            subprocess.run(["git", "branch", "-D", branch], cwd=str(ROOT), capture_output=True)
        else:
            print(f"      - Note: Branch '{branch}' is not AI-managed. Preserving local branch.")

    # 4. Archive Job
    archive_path = archive_job(job)

    print(f"\n✅ Successfully merged and archived to {archive_path.name}")
    input("\n\033[1;96mTap Enter to return to menu...\033[0m")

def handle_archived_jobs_menu(session_allowed_machines, session_allowed_models):
    while True:
        clear_screen()
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Archived AI Jobs")

            archived_dir = ARCHIVE_DIR
            if not archived_dir.exists():
                archived_dir.mkdir(parents=True, exist_ok=True)
            # List archived jobs
            archived_files = sorted(list(archived_dir.glob("*.json")), key=lambda x: x.stat().st_mtime, reverse=True)

            if not archived_files:
                print("\033[90m    No archived jobs found.\033[0m")
            else:
                for i, f in enumerate(archived_files):
                    try:
                        data = read_json(f)
                        title = data.get("title", "Untitled")
                        job_id = data.get("job_id", f.stem)
                        status = data.get("status", "unknown")
                        print(f"    [{i:2}] \033[97m{job_id:15}\033[0m | {status:10} | {title}")
                    except:
                        print(f"    [{i:2}] \033[1;91m{f.name:15}\033[0m | CORRUPT")

            print("\n    Actions:")
            print("    [\033[1;96mU\033[0m] Un-archive (Restore to main menu)")
            print("    [\033[1;91mB\033[0m] Back")

            status_bar.render(at_bottom=True, force=True)
            choice = get_key().strip().lower()

            if choice == "b":
                break
            elif choice == "u" and archived_files:
                try:
                    idx_str = prompt_input("Enter index to restore:", placeholder="(number)")
                except BackException:
                    continue
                try:
                    idx = int(idx_str)
                    if 0 <= idx < len(archived_files):
                        src = archived_files[idx]
                        dest = JOBS_DIR / src.name
                        import shutil
                        shutil.move(str(src), str(dest))
                        print(f"\n✅ Job restored: \033[97m{src.name}\033[0m")
                        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                except ValueError:
                    pass

def handle_job_selection(job: dict[str, Any], session_allowed_machines: list[str], session_allowed_models: list[str]):
    global PROJECT_CONFIG
    error_msg = ""
    while True:
        clear_screen()
        actions = []
        
        status = job.get("status")
        phase = job.get("debug_phase", "propose")
        job_type = job.get("type")
        
        # Setup status bar
        job["online_machines"] = get_online_machines(job.get("allowed_machines", []))
        with StatusBar(job, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header(job.get("title", "Untitled Job"))
            print(f"Job ID: {format_job_id(job.get('job_id', 'unknown'))}")
            
            # Display Linked Logs
            logs = job.get("last_manual_log_paths", [])
            if logs:
                from common import format_log_path
                log_display = ", ".join([format_log_path(Path(l).stem) for l in logs[:3]])
                print(f"\033[92mLinked Logs: {log_display}\033[0m")
            
            # TASK PROGRESS TRACKER
            tasks = job.get("plan", {}).get("tasks", [])
            if tasks:
                completed = job.get("completed_task_indices", [])
                count = len(tasks)
                done = len(completed)
                percent = int((done / count) * 100) if count > 0 else 0
                
                # Progress Bar [####------] 40%
                bar_width = 20
                filled = int(bar_width * done / count)
                bar = "█" * filled + "░" * (bar_width - filled)
                
                yolo_status = "\033[1;93m[YOLO MODE ON]\033[0m" if job.get("is_yolo") else "\033[90m[Manual Step Mode]\033[0m"
                print(f"Progress: \033[1;96m{bar}\033[0m {done}/{count} tasks ({percent}%) | {yolo_status}")
                
                # Show active/next task
                if done < count:
                    next_task = tasks[done]
                    print(f"Next: \033[97m{next_task.get('name', f'Task {done+1}')}\033[0m")
                else:
                    print("\033[92mStatus: Feature complete!\033[0m")
                
            print(f"Status: {job.get('status')}")

            # LLM SESSIONS
            sessions = job.get("llm_sessions", [])
            # Fallback to old format for compatibility during transition
            if not sessions and "llm_session_ids" in job:
                sessions = [{"id": sid, "model": "unknown"} for sid in job["llm_session_ids"]]
                
            if sessions:
                print(f"\033[1;96mLLM Sessions:\033[0m {', '.join([s['id'][:8] for s in sessions])}")
                if len(sessions) > 1:
                    print("[\033[1;96mJ\033[0m] List all LLM session IDs")
                    actions.append("j")

            references = job.get("reference_artifacts", [])

            if references:
                print(f"References: \033[92m{len(references)} attached\033[0m")
                for ref in references[:3]:
                    label = ref.get("url") or ref.get("path") or ref.get("source") or ref.get("type", "reference")
                    note = ref.get("note")
                    suffix = f" \033[90m- {note}\033[0m" if note else ""
                    print(f"  - {label}{suffix}")
                if len(references) > 3:
                    print(f"  \033[90m...and {len(references) - 3} more\033[0m")
            
            # PROACTIVE STATUS CONTEXT (What should the user do?)
            print("-" * 60)
            if status == "human-needed":
                print("\033[1;91m 🚨 BLOCKED: Action Required\033[0m")
                
                # Check for Architect feedback
                verification = job.get("verification")
                if verification and verification.get("status") in ["rejected", "concerns"]:
                    print(f"\n \033[1;93m🛡️ Senior Architect Feedback ({verification.get('status').upper()}):\033[0m")
                    print(f" {verification.get('comments')}")
                    
                    if verification.get("suggested_additions"):
                        print(f"\n \033[1;96mSuggested Additions:\033[0m")
                        for s in verification["suggested_additions"]:
                            print(f" - {s}")

                question = job.get("human_clarification_question")
                error = job.get("last_error")
                if question:
                    print(f" \033[93mQuestion:\033[0m {question}")
                    print("[\033[1;96mA\033[0m] Answer Question & Re-plan")
                    if "a" not in actions: actions.append("a")
                elif error:
                    print(f" \033[1;91mError:\033[0m {error}")
                else:
                    print(" \033[90mReason: Unknown (Check logs or try re-running with more context)\033[0m")
                print(f" \033[1;96m->\033[0m Press \033[1;96m'L'\033[0m to link logs then \033[1;96m'U'\033[0m to Resume, or \033[1;96m'R'\033[0m to RESET progress & delete changes.")
                
                # Dynamic suggestions button
                if verification and verification.get("status") in ["rejected", "concerns"]:
                    print("[\033[1;96mA\033[0m] Accept Architect Suggestions & Re-run")
                    actions.append("a")
                
            elif status == "designing":
                print("\033[1;96m 🎨 DESIGNING: Stitch AI Spec Ready\033[0m")
                print(f" Designer: \033[97m{job.get('planner')}\033[0m | Vibe: \033[93m{job.get('plan', {}).get('vibe', 'N/A')}\033[0m")
                print(f" \033[1;96m->\033[0m Press \033[1;96m'V'\033[0m to Review the high-fidelity design spec.")
                print(f" \033[1;96m->\033[0m Press \033[1;96m'F'\033[0m to Provide Feedback & Revise the design.")
                print(f" \033[1;96m->\033[0m Press \033[1;96m'A'\033[0m to Approve & Decompose into implementation tasks.")
                
                print("\nActions:")
                print("[\033[1;96mA\033[0m] Approve Design & Start Implementation")
                print("[\033[1;96mF\033[0m] Provide Feedback / Revise")
                actions.extend(["a", "f"])
                
            elif status == "planned":
                print("\033[1;96m 📝 READY: Planning Complete\033[0m")
                print(f" Pipeline: \033[97m{job.get('planner')} \033[1;96m->\033[0m {job.get('builder')} \033[1;96m->\033[0m {job.get('reviewer')}\033[0m")
                print(f" \033[1;96m->\033[0m Press \033[1;96m'S'\033[0m to Schedule & Dispatch this job to a worker.")
                
            elif status == "executing":
                # Check for stalled job (more than 12 hours)
                is_stalled = False
                try:
                    # Parse ISO format with timezone handling
                    # Python 3.11+ fromisoformat handles the -05:00 correctly
                    updated_at = datetime.fromisoformat(job.get("updated_at", now_iso()))
                    if (datetime.now().astimezone() - updated_at).total_seconds() > 12 * 3600:
                        is_stalled = True
                except Exception:
                    pass
                
                if is_stalled:
                    print("\033[1;91m ⚠️  STALLED: Job hasn't responded for >12 hours\033[0m")
                    print(f" Last Update: {job.get('updated_at')}")
                    print(f" \033[1;96m->\033[0m It is likely the worker crashed or the machine went offline.")
                    print(f" \033[1;96m->\033[0m Press \033[1;96m'P'\033[0m to Reset to Planned (keeps progress) or \033[1;96m'R'\033[0m to Re-run.")
                    
                    print("\nActions:")
                    print("[\033[1;96mP\033[0m] Reset Status to Planned")
                    actions.append("p")
                else:
                    print("\033[1;93m ⚙️  ACTIVE: Worker in Progress\033[0m")
                    machine = job.get("assigned_machine", "local/pending")
                    print(f" Machine:  \033[97m{machine}\033[0m | Branch: \033[97m{job.get('branch', 'pending')}\033[0m")
                    print(f" \033[1;96m->\033[0m Wait for completion or monitor progress in the status bar below.")
                
            elif status == "debugging":
                if phase == "paused":
                    print("\033[1;93m ⏸️  PAUSED: Max Iterations Reached\033[0m")
                    print(f" Attempt:  \033[97m{job.get('iteration')} / {job.get('max_iterations')}\033[0m")
                    print(f" \033[1;96m->\033[0m AI reached its limit. Tests are still failing.")

                    history = job.get("debug_history", [])
                    if history:
                        last = history[-1]
                        print(f"\n  \033[1;97mFailure Hypothesis:\033[0m \033[90m{last.get('hypothesis')}\033[0m")
                        print(f"  \033[1;97mNext Action:\033[0m \033[93m{last.get('action')}\033[0m")
                    print(f"\n \033[1;96m->\033[0m Press \033[1;96m'D'\033[0m to increase limits and resume, or \033[1;96m'R'\033[0m to reset.")
                else:
                    print("\033[1;95m 🔍 FIXING: Auto-Debug Loop\033[0m")
                    iter_val = job.get("iteration", 1)
                    max_iter = job.get("max_iterations", 8)
                    print(f" Attempt:  \033[97m{iter_val} / {max_iter}\033[0m")
                    if job.get("debug_history"):
                       last = job["debug_history"][-1]
                       print(f" Latest Hypothesis: \033[90m{last.get('hypothesis', 'Analyzing failure...')}\033[0m")
                       if last.get("evidence"):
                           print(f" Evidence Proof:   \033[3;90m\"{last.get('evidence')}\"\033[0m")
                    print(f" \033[1;96m->\033[0m AI is currently auto-patching code to pass tests.")
                # Succinct Debug History Summary
                history = job.get("debug_history", [])
                if history:
                    print("\n  \033[1;97mDebug Trials:\033[0m")
                    for entry in history[-3:]: # Show last 3 trials
                        res = entry.get("result", "pending")
                        if isinstance(res, dict):
                            if res.get("build_ok") and res.get("tests_ok"):
                                res_str = "\033[92mPASS\033[0m"
                            elif not res.get("build_ok"):
                                res_str = "\033[1;91mFAIL (Build)\033[0m"
                            else:
                                res_str = "\033[1;91mFAIL (Tests)\033[0m"
                        else:
                            res_str = f"\033[90m{res}\033[0m"
                        
                        # Show Hypothesis (the 'Why') instead of just Action (the 'How')
                        desc = entry.get("hypothesis") or entry.get("action", "unknown")
                        if len(desc) > 60: desc = desc[:57] + "..."
                        print(f"    #{entry.get('iteration')}: {res_str:12} | {desc}")
                    if len(history) > 3:
                        print(f"    \033[90m(+ {len(history)-3} older trials)\033[0m")
                
            elif status == "review-needed":
                print("\033[1;92m 🏁 CODE COMPLETE: Ready to Merge\033[0m")
                print(f" PR: \033[97m#{job.get('pr_number')}\033[0m | Status: \033[92mALL TESTS PASSING\033[0m")
                print(f" \033[1;96m->\033[0m Press \033[1;96m'G'\033[0m to view PR on GitHub or \033[1;96m'M'\033[0m to Merge & Finish.")
            
            print("-" * 60)

            # Change Summary
            branch = job.get("branch")
            # Logical base is what the job was cut from, but global base is for "Total Progress"
            job_base = job.get("base_branch", PROJECT_CONFIG.base_branch)
            
            # Determine global "source of truth" base for the project
            # 1. User settings
            # 2. configured default branch for this project
            # 3. 'main'
            # 4. 'master'
            settings_path = CONFIG_DIR / "settings.json"
            global_base = PROJECT_CONFIG.base_branch
            if settings_path.exists():
                try:
                    s = read_json(settings_path)
                    global_base = s.get("global_base_branch", PROJECT_CONFIG.base_branch)
                except:
                    pass

            try:
                # Verify the selected base exists, otherwise fall back to common names
                res = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{global_base}"], capture_output=True)
                if res.returncode != 0:
                    # Fallback chain
                    for fallback in [PROJECT_CONFIG.base_branch, "main", "master"]:
                        res_f = subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{fallback}"], capture_output=True)
                        if res_f.returncode == 0:
                            global_base = fallback
                            break
            except:
                pass
            
            job["global_base"] = global_base # Cache for UI display

            summary_line = ""
            changed_files_list = []
            local_summary = ""
            local_files = []
            
            # 1. Total Job Progress (Branch vs Global Base)
            if branch and branch != global_base:
                try:
                    res = subprocess.run(["git", "diff", "--shortstat", f"{global_base}...{branch}"], capture_output=True, text=True)
                    if res.returncode == 0 and res.stdout.strip():
                        summary_line = res.stdout.strip()
                    
                    res_files = subprocess.run(["git", "diff", "--name-only", f"{global_base}...{branch}"], capture_output=True, text=True)
                    if res_files.returncode == 0:
                        changed_files_list = [f for f in res_files.stdout.splitlines() if f.strip()]
                except:
                    pass
            
            # 2. Local Unsaved Changes (HEAD vs Filesystem)
            try:
                res = subprocess.run(["git", "diff", "--shortstat", "HEAD"], capture_output=True, text=True)
                if res.returncode == 0 and res.stdout.strip():
                    local_summary = res.stdout.strip()
                
                res_files = subprocess.run(["git", "diff", "--name-only", "HEAD"], capture_output=True, text=True)
                if res_files.returncode == 0:
                    local_files = [f for f in res_files.stdout.splitlines() if f.strip()]
            except:
                pass

            if summary_line or changed_files_list or local_summary or local_files:
                print(f"  \033[1;97mDELTA (Total Progress)\033[0m")
                
                # 0. Branch Info
                if branch:
                    print(f"    \033[90m└─\033[0m \033[90mBranch:\033[0m \033[97m{branch}\033[0m \033[90m(vs {global_base})\033[0m")

                
                # 1. Total Stats Line
                if summary_line:
                    parts = [p.strip() for p in summary_line.split(",")]
                    stats_parts = []
                    for p in parts:
                        if "insertion" in p: stats_parts.append(f"\033[92m+{p.split()[0]}\033[0m")
                        elif "deletion" in p: stats_parts.append(f"\033[1;91m-{p.split()[0]}\033[0m")
                    
                    file_count = parts[0].split()[0]
                    stats_str = f" ({', '.join(stats_parts)})" if stats_parts else ""
                    print(f"    \033[90m└─\033[0m \033[97m{file_count} files total{stats_str}\033[0m")
                
                # 2. Local/Unsaved Stats (if any)
                if local_summary:
                    parts = [p.strip() for p in local_summary.split(",")]
                    local_stats = []
                    for p in parts:
                        if "insertion" in p: local_stats.append(f"\033[92m+{p.split()[0]}\033[0m")
                        elif "deletion" in p: local_stats.append(f"\033[1;91m-{p.split()[0]}\033[0m")
                    print(f"    \033[90m└─\033[0m \033[93m{parts[0].split()[0]} files unsaved (local)\033[0m \033[90m{' '.join(local_stats)}\033[0m")

                # 3. Combined File List (Preview)
                all_impacted = sorted(list(set(changed_files_list + local_files)))
                if all_impacted:
                    display_files = all_impacted[:5]
                    files_str = ", ".join([Path(f).name for f in display_files])
                    if len(all_impacted) > 5:
                        files_str += f" \033[90m(+{len(all_impacted)-5} more)\033[0m"
                    print(f"    \033[90m└─\033[0m \033[90m{files_str}\033[0m")

            print("\nActions:")
            
            # 2. Status-Specific Actions
            if status == "planned":
                if job_type == "feature-plan":
                    print("[\033[92mA\033[0m] Approve / Create Sub-tasks")
                    actions.append("a")
                else:
                    print("[\033[92mS\033[0m] Schedule & Dispatch")
                    actions.append("s")
                print("[\033[93mF\033[0m] Tweak / Revise Plan")
                actions.append("f")
            elif status == "scheduled":
                print("[\033[92mE\033[0m] Execute (Worker Run)")
                actions.append("e")
            elif status == "debugging":
                if phase == "propose" or phase == "paused":
                    print("[\033[92mD\033[0m] Fix Failing Tests")
                    actions.append("d")
                elif phase == "verify":
                    print("[\033[92mD\033[0m] Verify Fix")
                    actions.append("d")
                
                # Broaden Resume Availability: Let users escape debugging loops
                print("[\033[93mU\033[0m] Resume Job (Next Task)")
                actions.append("u")

                print("[\033[93mF\033[0m] Tweak / Give Hint")
                actions.append("f")
                
            elif status == "review-needed":
                print("[\033[92mM\033[0m] Merge & Mark Completed")
                actions.append("m")
                print("[\033[93mF\033[0m] Deliver to Device (Firebase distribution)")
                actions.append("f")
                print("[\033[93mT\033[0m] Tweak / Iterate Further")
                actions.append("t")
                
                # If there are tasks remaining, allow Resuming to the next task
                if tasks and len(completed) < len(tasks):
                    print("[\033[93mU\033[0m] Resume Job (Next Task)")
                    actions.append("u")
            elif status == "human-needed":
                if job.get("branch"):
                    print("[\033[93mF\033[0m] Deliver to Device (Firebase distribution)")
                    actions.append("f")
                print("[\033[93mT\033[0m] Tweak / Revise")
                actions.append("t")
                print("[\033[92mU\033[0m] Resume Job")
                actions.append("u")
            elif status == "executing" or is_stalled:
                print("[\033[93mU\033[0m] Resume Job")
                actions.append("u")
                
            # Global actions available for most non-archived states
            if status not in ["completed", "discarded", "planned"]:
                if "d" not in actions:
                    print("[\033[93mD\033[0m] Auto-Fix / Iterate")
                    actions.append("d")
                if "r" not in actions:
                    print("[\033[1;91mR\033[0m] Full Re-run \033[1;91m(Deletes existing changes & resets to Planned)\033[0m")
                    actions.append("r")
            
            ai_modified = job.get("ai_modified_files", [])
            ai_untracked = job.get("ai_untracked_files", [])
            if ai_modified or ai_untracked:
                print("[\033[1;96mI\033[0m] View AI Changes (Files)")
                actions.append("i")
                
            print("[\033[93mL\033[0m] Link Logs (Update Context)")
            print("[\033[93mK\033[0m] Attach UI Mockup / Reference")
            print("[\033[93mQ\033[0m] Ask AI (Questions about changes)")
            actions.extend(["l", "k", "q"])

            print("[\033[93mO\033[0m] Select LLM Models (Override)")
            print("[\033[93mT\033[0m] Export Context (Logs, Progress, Plan)")
            print("[\033[93mV\033[0m] View Brief / Summary")
            print("[\033[1;96mG\033[0m] View in GitHub")
            print("[\033[1;91mC\033[0m] Close Issue in GitHub")
            print("[\033[1;91mX\033[0m] Discard & Revert Changes")
            print("[\033[1;91mB\033[0m] Back to Main Menu")
            actions.extend(["v", "g", "c", "r", "x", "m", "f", "o", "b", "u", "t"])
            
            if error_msg:
                print(f"\n\033[1;91mNOT A VALID OPTION, PLEASE TRY AGAIN... ({error_msg})\033[0m")
                error_msg = ""

            # Anchor prompt to bottom
            prompt = get_choice_prompt("Choice:", "(action)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()
            
            if not choice:
                clear_choice_placeholder()
                continue
            
            if choice not in actions:
                clear_choice_placeholder()
                error_msg = f"'{choice}'"
                continue

            # Echo the choice inside the box
            sys.stdout.write(f"\033[48;5;236m\033[1;96m{choice}\033[0m")
            sys.stdout.flush()
            clear_choice_placeholder()
            print(choice)
            
            if choice == "b":
                break
            elif choice == "o":
                while True:
                    options, value_map, details_map = run_with_loading_screen(
                        "Override LLM Models for Job",
                        [
                            "\n\033[90mChecking model availability from local CLIs and configured API keys...\033[0m",
                            "\033[90mThis can take a few seconds when provider CLIs are slow to respond.\033[0m",
                        ],
                        "Checking model availability",
                        status_bar,
                        get_model_selection_data,
                    )
                    
                    # Map current IDs back to enabled labels for defaults.
                    curr_allowed_ids = job.get("allowed_models", list(DEFAULT_FALLBACKS))
                    curr_defaults = resolve_model_selection_defaults(curr_allowed_ids, value_map)

                    try:
                        footer = "[\033[1;92mR\033[0m] Sync Registry  [\033[1;92mD\033[0m] Live Discovery  [\033[1;91mB\033[0m] Back"
                        new_labels = prompt_checkbox("select models", options, curr_defaults, extra_keys=["r", "d", "b"], footer=footer, details_map=details_map, status_bar=status_bar)
                        if new_labels:
                            job["allowed_models"] = [value_map[label] for label in new_labels]
                            save_job(job)
                            print(f"\n✅ Updated allowed models for this job.")
                        else:
                            print("Warning: At least one model must be selected. No changes made.")
                        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                        break
                    except KeyInterruptException as exc:
                        if exc.key == "b":
                            break
                        if exc.key in ["r", "d"]:
                            live = (exc.key == "d")
                            from model_registry import sync_models
                            action = "Syncing with registry..." if not live else "Pinging provider APIs..."
                            print(f"\n\n📡 {action}")
                            success, msg = sync_models(live_discovery=live)
                            if success:
                                print(f"✅ {msg}")
                            else:
                                print(f"❌ {msg}")
                            input("\n\033[1;96mTap Enter to continue...\033[0m")
                            continue # Re-open the menu with new data
                        raise
                    except BackException:
                        break

            elif choice == "x":
                handle_discard_job(job)
                break
            elif choice == "c":
                issue_num = job.get("issue_number")
                if issue_num:
                    if prompt_confirm(f"Are you sure you want to close Issue #{issue_num}?", default=False):
                        print(f"Closing Issue #{issue_num}...")
                        subprocess.run(["gh", "issue", "close", str(issue_num)], cwd=str(ROOT))
                        archive_job(job)
                        print(f"Job archived.")
                        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                        break # Back to main menu since job is archived
                else:
                    print("No issue number associated with this job.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "l":
                log_paths = prompt_for_logs(job)
                
                # Record choice in JSON
                job = refresh_job(job)
                job["last_manual_log_paths"] = log_paths
                save_job(job)
                job = refresh_job(job)
                
                if log_paths:
                    print(f"\n✅ Linked {len(log_paths)} log entries.")
                    # We skip the "Press Enter" so it returns immediately to the job menu redraw
            elif choice == "k":
                prompt_for_reference_artifact(job)
                job = refresh_job(job)
            elif choice == "q":
                handle_ask_ai(job, session_allowed_models)
                job = refresh_job(job)
            elif choice == "j":
                print_header("LLM Session Tracking")
                sessions = job.get("llm_sessions", [])
                if not sessions and "llm_session_ids" in job:
                    sessions = [{"id": sid, "model": "unknown"} for sid in job["llm_session_ids"]]
                
                if not sessions:
                    print("  No sessions recorded.")
                else:
                    # Table Header
                    print(f"  {'#':3} | {'Model Name':30} | {'Session ID (UUID)':40}")
                    print(f"  {'-'*3:3}-+-{'-'*30:30}-+-{'-'*40:40}")
                    
                    for i, s in enumerate(sessions):
                        model = s.get("model", "unknown")
                        sid = s.get("id", "unknown")
                        print(f"  {i:3} | {model:30} | \033[97m{sid}\033[0m")
                
                print("\n  Tip: You can resume these in the Antigravity CLI using: antigravity --resume <ID>")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "i" and "i" in actions:
                print_header("AI Modified Files")
                ai_modified = job.get("ai_modified_files", [])
                ai_untracked = job.get("ai_untracked_files", [])
                
                if not ai_modified and not ai_untracked:
                    print("  No AI-modified files recorded for this job.")
                else:
                    if ai_modified:
                        print("  \033[93mModified Files:\033[0m")
                        for f in sorted(ai_modified):
                            print(f"    - {f}")
                    
                    if ai_untracked:
                        if ai_modified: print()
                        print("  \033[92mUntracked (New) Files:\033[0m")
                        for f in sorted(ai_untracked):
                            print(f"    - {f}")
                            
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "f" and "f" in actions:
                if status == "designing" or status == "planned" or status == "debugging":
                    job = handle_tweak_revise(job)
                else:
                    dist_errors = PROJECT_CONFIG.validate_distribution_config()
                    if dist_errors:
                        print_header("Distribution Setup Needed")
                        for err in dist_errors:
                            print(f" \033[1;91m- {err}\033[0m")
                        print("\n \033[93mYou must configure signing and accounts before distributing.\033[0m")
                        if prompt_confirm("Would you like to run the Setup Wizard now?", default=True):
                            print("\n\033[1;96mStarting Orchestrator Wizard...\033[0m")
                            # Reset terminal state so the wizard has full access
                            status_bar.reset_scroll_region(force=True)
                            sys.stdout.write("\033[?25h")
                            sys.stdout.flush()
                            
                            cli_path = SCRIPTS_DIR.parent / "cli.py"
                            subprocess.run([sys.executable, str(cli_path), "wizard"], cwd=str(ROOT))
                            
                            # Restore scroll region for this menu
                            status_bar.set_scroll_region()
                            
                            # Reload config to pick up wizard changes
                            import importlib
                            import orchestrator.project_config
                            importlib.reload(orchestrator.project_config)
                            PROJECT_CONFIG = orchestrator.project_config.PROJECT_CONFIG
                            print("\n✅ Project configuration reloaded.")
                            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                        else:
                            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                        continue

                    print_phase("delivery", subtext="firebase distribution")
                    print("This will archive, export, and upload the build to Firebase.")
                    print("Estimated time: 5-10 minutes.")
                    run_script("deliver_build.py", [str(job["_path"])], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
                    job = refresh_job(job)
            elif choice == "t" and "t" in actions:
                job = handle_tweak_revise(job)
            elif choice == "m" and "m" in actions:
                handle_merge_cleanup(job)
                break
            elif choice == "a" and "a" in actions:
                # 1. Answer Clarification Question
                question = job.get("human_clarification_question")
                if status == "human-needed" and question:
                    print_header("Answer Clarification Question")
                    print(f"\n \033[93mQuestion:\033[0m {question}\n")
                    answer = input("\033[1;96mYour Answer:\033[0m ").strip()
                    if answer:
                        # Clear old question to avoid re-triggering if planning fails
                        job["human_clarification_question"] = None
                        save_job(job)
                        
                        # Trigger re-plan with the answer as feedback
                        feedback = f"### USER CLARIFICATION ###\n{answer}"
                        
                        # Determine which job type to use for re-planning
                        job_type_map = {
                            "bug-fix": "bug",
                            "bug-investigate": "bug",
                            "feature-plan": "feature",
                            "test-audit": "coverage",
                            "feature-design": "design"
                        }
                        requested_type = job_type_map.get(job.get("type"), "feature")
                        
                        run_script("new_job.py", [requested_type, "--no-dispatch", "--update", str(job["_path"]), "--feedback", feedback], sub_menu=True)
                        job = refresh_job(job)
                    continue

                # 2. Accept Architect Suggestions
                verification = job.get("verification")
                if status == "human-needed" and verification and verification.get("status") in ["rejected", "concerns"]:
                    print_header("Integrating Architect Suggestions")
                    feedback = f"Please re-plan and incorporate the Senior Architect's suggestions:\n"
                    feedback += f"Comments: {verification.get('comments')}\n"
                    if verification.get("suggested_additions"):
                        feedback += "Suggested Additions:\n- " + "\n- ".join(verification["suggested_additions"])
                    
                    job_type_map = {
                        "bug-fix": "bug",
                        "bug-investigate": "bug",
                        "feature-plan": "feature",
                        "test-audit": "coverage",
                        "feature-design": "design"
                    }
                    requested_type = job_type_map.get(job.get("type"), "feature")
                    
                    run_script("new_job.py", [requested_type, "--no-dispatch", "--update", str(job["_path"]), "--feedback", feedback], sub_menu=True)
                    job = refresh_job(job)
                    continue

                if status == "designing":
                    print("\n🎨 \033[1;92mDESIGN APPROVED!\033[0m")
                    print("Transitioning to Implementation Planning phase...")
                    
                    # Store design as context for the next planner
                    design_spec = job.get("plan", {})
                    job["design_spec"] = design_spec
                    job["status"] = "planned"
                    job["type"] = "feature-plan"
                    
                    # Update raw_input so the Feature Planner sees the design
                    design_context = f"\n\n### APPROVED DESIGN SPEC (Stitch AI) ###\n"
                    design_context += f"Summary: {design_spec.get('summary')}\n"
                    design_context += f"Vibe: {design_spec.get('vibe')}\n"
                    design_context += "Visual Components:\n- " + "\n- ".join(design_spec.get("visual_components", [])) + "\n"
                    design_context += "Interaction Flows:\n- " + "\n- ".join(design_spec.get("interaction_flows", [])) + "\n"
                    
                    job["raw_input"] = job.get("raw_input", "") + design_context
                    save_job(job)
                    
                    # Now trigger the planning script to generate the task list
                    print(f"\n[1/1] Generating implementation tasks based on the design...")
                    run_script("new_job.py", ["feature", "--no-dispatch", "--update", str(job["_path"])], sub_menu=True)
                    
                    # Re-read to get the new tasks and status
                    job = refresh_job(job)
                    print(f"\n✅ Design transitioned to Feature Plan with {len(job.get('plan', {}).get('tasks', []))} tasks.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue

                if not session_allowed_machines:
                    print("\n\033[1;91m⚠️  ERROR: No active machines selected for this session.\033[0m")
                    print("Please go to [\033[1;96mC\033[0m] Configuration \033[1;96m->\033[0m [\033[1;96mF\033[0m] Manage Machine Fleet and select at least one machine.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue
                # Approve the plan and schedule for sequential execution
                job["status"] = "planned"
                job["approved"] = True
                save_job(job)
                
                print(f"\n✅ Plan approved. Scheduling sequential execution...")
                run_script("schedule_job.py", [str(job["_path"])], job=job, session_machines=session_allowed_machines, session_models=session_allowed_models)
                job = refresh_job(job)
            elif choice == "p" and "p" in actions:
                if prompt_confirm("Reset status to 'planned' (keeps progress)?", default=True):
                    job["status"] = "planned"
                    job["updated_at"] = now_iso()
                    save_job(job)
                    job = refresh_job(job)
                    print("\n✅ Job status reset to Planned.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "r":
                warning = "\033[1;91m⚠️  WARNING: THIS WILL PERMANENTLY DELETE ALL LOCAL PROGRESS & CODE CHANGES.\033[0m\n"
                warning += "Are you sure you want to reset status to 'planned' and start over?"
                if prompt_confirm(warning, default=False):
                    # 1. Clean up local files
                    print("\n      - Cleaning up existing changes...")
                    perform_job_revert(job)

                    # 2. Add branch selection logic here
                    branch_options = ["new", "current-branch (git pull)", "manual (no git actions)"]
                    branch_choice = prompt_radio("Branch selection for re-run:", branch_options, "new")
                    
                    branch_arg = "new"
                    if "current-branch" in branch_choice:
                        branch_arg = "current"
                    elif "manual" in branch_choice:
                        branch_arg = "manual"
                    
                    # Save the branch mode to the job, then reset
                    job["branch_mode"] = branch_arg
                    if branch_arg == "new":
                        job["branch"] = None
                    
                    job["status"] = "planned"
                    job["updated_at"] = now_iso()
                    job["pr_number"] = None
                    
                    # 5) when choosing to re-run a job, ensure the linking prompt comes up.
                    try:
                        log_paths = prompt_for_logs(job)
                        job["last_manual_log_paths"] = log_paths
                    except BackException:
                        print("      - No logs linked.")
                    
                    # Save the updated job data
                    save_job(job)
                    job_path = job["_path"]
                    
                    print("Job reset to 'planned'. Re-scheduling immediately...")
                    run_script("schedule_job.py", [str(job_path)], job=job, session_machines=session_allowed_machines, session_models=session_allowed_models)
                    job = refresh_job({"_path": job_path})
            elif choice == "t" and "t" in actions:
                run_script("export_job.py", [str(job["_path"])], sub_menu=True)
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "g":
                if job.get("pr_number"):
                    print(f"Opening PR #{job['pr_number']} in browser...")
                    subprocess.run(["gh", "pr", "view", str(job["pr_number"]), "--web"], cwd=str(ROOT))
                else:
                    print(f"Opening Issue #{job['issue_number']} in browser...")
                    subprocess.run(["gh", "issue", "view", str(job["issue_number"]), "--web"], cwd=str(ROOT))
                
                # Clear any leaking output from gh
                sys.stdout.write("\r\033[K")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "v":
                brief_file = OUTPUT_DIR / job["job_id"] / "brief.md"
                summary_file = OUTPUT_DIR / job["job_id"] / "builder_summary.md"
                if brief_file.exists():
                    print_header("Brief")
                    print(format_markdown_for_terminal(brief_file.read_text(encoding="utf-8")))
                if summary_file.exists():
                    print_header("Summary")
                    print(format_markdown_for_terminal(summary_file.read_text(encoding="utf-8")))
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "s" and "s" in actions:
                if not session_allowed_machines:
                    print("\n\033[1;91m⚠️  ERROR: No active machines selected for this session.\033[0m")
                    print("Please go to [\033[1;96mC\033[0m] Configuration \033[1;96m->\033[0m [\033[1;96mF\033[0m] Manage Machine Fleet and select at least one machine.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue
                    
                dry_run = prompt_confirm("Dry run?", default=False)
                args = [str(job["_path"])]
                if dry_run:
                    args.append("--dry-run")
                run_script("schedule_job.py", args, job=job, session_machines=session_allowed_machines, session_models=session_allowed_models)
                # Refresh job data
                job = refresh_job(job)
            elif choice == "e" and "e" in actions:
                if not session_allowed_machines:
                    print("\n\033[1;91m⚠️  ERROR: No active machines selected for this session.\033[0m")
                    print("Please go to [\033[1;96mC\033[0m] Configuration \033[1;96m->\033[0m [\033[1;96mF\033[0m] Manage Machine Fleet and select at least one machine.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue
                run_script("worker_run.py", [str(job["_path"])], job=job, sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
                job = refresh_job(job)
            elif choice == "u" and "u" in actions:
                if not session_allowed_machines:
                    print("\n\033[1;91m⚠️  ERROR: No active machines selected for this session.\033[0m")
                    print("Please go to [\033[1;96mC\033[0m] Configuration \033[1;96m->\033[0m [\033[1;96mF\033[0m] Manage Machine Fleet and select at least one machine.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue

                print_header(f"Resuming Job: {job.get('job_id')}")
                # Use schedule_job with --resume to intelligently pick between re-attach or takeover
                run_script("schedule_job.py", [str(job["_path"]), "--resume"], job=job, session_machines=session_allowed_machines, session_models=session_allowed_models)
                job = refresh_job(job)
            elif choice == "d" and "d" in actions:
                if not session_allowed_machines:
                    print("\n\033[1;91m⚠️  ERROR: No active machines selected for this session.\033[0m")
                    print("Please go to [\033[1;96mC\033[0m] Configuration \033[1;96m->\033[0m [\033[1;96mF\033[0m] Manage Machine Fleet and select at least one machine.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue

                try:
                    args = [str(job["_path"])]
                    
                    if status == "debugging" and phase == "verify":
                        repro = prompt_confirm("Was the bug reproduced in this iteration?", default=False)
                        if repro:
                            print("\n\033[1;91m" + "🐛"*2 + " BUG CONFIRMED REPRODUCED " + "🐛"*2 + "\033[0m\n")
                        notes = input("Notes about the result: ").strip()
                        args.extend(["--notes", notes])
                        if repro:
                            args.append("--bug-reproduced")
                    else:
                        current_total = job.get("max_iterations", 8)
                        # Migration: Bump legacy 5 to new 8
                        if current_total == 5: current_total = 8

                        default_extra = 5
                        feedback = prompt_input("Add info to help iteration:", placeholder="(optional)")
                        while True:
                            extra_input = prompt_input("Additional iterations:", default=str(default_extra))
                            if not extra_input:
                                extra = default_extra
                                break
                            try:
                                extra = int(extra_input)
                                if extra < 0:
                                    print("\033[1;91m      ⚠️  Please enter a positive number.\033[0m")
                                    continue
                                break
                            except ValueError:
                                print("\033[1;91m      ⚠️  Invalid input. Please enter a number.\033[0m")

                        final_limit = current_total + extra
                        args.extend(["--max-iterations", str(final_limit)])

                    # ALWAYS auto-link freshest logs before an iteration loop
                    auto_link_latest_logs(job)
                    curr_logs = job.get("last_manual_log_paths", [])
                    if curr_logs:
                        args.extend(["--logs", ",".join(curr_logs)])

                    print_header("Starting Iteration Loop")
                    run_script("debug_job.py", args, sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
                    
                except BackException:
                    pass # Return to job menu
                
                job = refresh_job(job)

def handle_tweak_revise(job: dict[str, Any]):
    print_header("Tweak / Revise")
    print("\033[90mProvide feedback, new requirements, or context to help the AI refine its work.\033[0m")
    feedback = prompt_input("Feedback:", placeholder="(or Enter to cancel)")
    if not feedback:
        return

    # Determine which job type to use for re-planning
    job_type_map = {
        "bug-fix": "bug",
        "bug-investigate": "bug",
        "feature-plan": "feature",
        "test-audit": "coverage",
        "feature-design": "design"
    }
    current_type = job.get("type", "feature")
    requested_type = job_type_map.get(current_type, "feature")
    status = job.get("status")

    print(f"\n[1/1] Revising based on feedback...")
    args = [requested_type, "--no-dispatch", "--update", str(job["_path"]), "--feedback", feedback]
    
    # Enable stitch if we are designing or have a design spec
    if job.get("design_spec") or status == "designing" or "design" in current_type:
        if "--stitch" not in args:
            args.append("--stitch")
    
    run_script("new_job.py", args, sub_menu=True)
    return refresh_job(job)

def handle_ask_ai(job: dict[str, Any], session_allowed_models: list[str]):
    """Opens a conversational interface to ask questions about job changes."""
    from reference_artifacts import reference_context

    print_header("Ask AI: Change Context Inquiry")
    print("\033[90mAsk questions about what was changed, why, or how it works.\033[0m")
    print("\033[90mNo code changes will be made.\033[0m\n")

    # 1. Gather Context
    branch = job.get("branch")
    base = job.get("base_branch", "main")
    
    # Try to get the total diff
    diff_text = "(No diff available)"
    if branch:
        try:
            res = subprocess.run(["git", "diff", f"{base}...{branch}"], capture_output=True, text=True)
            if res.returncode == 0:
                diff_text = res.stdout
                if len(diff_text) > 50000:
                    diff_text = diff_text[:50000] + "\n... (diff truncated for length)"
        except:
            pass

    # Get brief and summary
    brief_dir = OUTPUT_DIR / job["job_id"]
    brief_content = ""
    summary_content = ""
    
    if (brief_dir / "brief.md").exists():
        brief_content = (brief_dir / "brief.md").read_text(encoding="utf-8")
    if (brief_dir / "builder_summary.md").exists():
        summary_content = (brief_dir / "builder_summary.md").read_text(encoding="utf-8")

    context = f"""### JOB CONTEXT
Title: {job.get('title')}
Job ID: {job.get('job_id')}
Status: {job.get('status')}
Branch: {branch} (vs {base})

### JOB BRIEF
{brief_content}

### REFERENCE ARTIFACTS
{reference_context(job) or "None"}

### BUILDER SUMMARY (AI Implementation Notes)
{summary_content}

### TOTAL DIFF
```diff
{diff_text}
```
"""

    system_prompt = """You are an expert technical consultant. You are helping a developer understand changes made by an AI coding agent.
Your goal is to answer questions about the code modifications, the rationale behind them, and how the new logic works.
You MUST NOT propose or perform any code changes. Be concise, accurate, and focus on the provided diff and job context."""

    while True:
        question = prompt_input("Question:", placeholder="(or Enter to go back)")
        if not question:
            break
            
        full_prompt = f"{system_prompt}\n\n{context}\n\n### USER QUESTION\n{question}"
        
        # Use the job's preferred builder or a fallback
        model = job.get("builder", "gemini-3.1-pro-preview")
        
        try:
            print_phase("agent_thinking")
            # We don't pass a role so it doesn't try to parse JSON
            output, actual_model, session_id = run_llm(model, full_prompt, allowed_models=session_allowed_models)
            
            # Record session ID in job if present
            if job:
                if "llm_sessions" not in job:
                    job["llm_sessions"] = []
                job["llm_sessions"].append({"id": session_id, "model": actual_model})
            
            print(f"\n\033[1;97mAI Response ({actual_model}):\033[0m")
            print("-" * 60)
            print(output)
            print("-" * 60)
            
        except Exception as e:
            print(f"\n\033[1;91mError calling AI: {e}\033[0m")
            
    return

def handle_api_keys(session_allowed_machines, session_allowed_models):
    settings_path = CONFIG_DIR / "settings.json"

    def get_cli_status(cmd_args):
        import shutil
        if not shutil.which(cmd_args[0]):
            return "Not Installed"
        try:
            res = subprocess.run(cmd_args, capture_output=True, text=True, timeout=3)
            if res.returncode == 0:
                combined = (res.stdout + res.stderr).lower()
                if "logged in" in combined or "account:" in combined or "email:" in combined:
                    return "Logged In"
                return "Ready"
            return "Not Logged In"
        except:
            return "Unknown"

    def get_gemini_status():
        import shutil
        import os
        if not (shutil.which("agy") or shutil.which("antigravity") or shutil.which("gemini")): return "Not Installed"
        if os.path.exists(os.path.expanduser("~/.gemini/oauth_creds.json")) or os.path.exists(os.path.expanduser("~/.gemini/google_accounts.json")):
            return "Logged In"
        return "Not Logged In"

    def get_ollama_status():
        import shutil
        if not shutil.which("ollama"): return "Not Installed"
        try:
            res = subprocess.run(['ollama', 'list'], capture_output=True, text=True, timeout=2)
            return "Running" if res.returncode == 0 else "Offline"
        except:
            return "Offline"

    def get_opencode_status():
        import shutil
        if not shutil.which("opencode"): return "Not Installed"
        try:
            # OpenCode is ready if 'opencode models' returns models
            res = subprocess.run(['opencode', 'models'], capture_output=True, text=True, timeout=3)
            if res.returncode == 0 and res.stdout.strip():
                return "Ready"
            return "Not Logged In"
        except:
            return "Offline"

    import threading

    # We cache CLI statuses here so we don't query them on every loop/invalid keypress.
    cli_statuses = {
        "gemini": "Checking...",
        "claude": "Checking...",
        "codex": "Checking...",
        "gh": "Checking...",
        "opencode": "Checking...",
        "ollama": "Checking..."
    }

    def refresh_cli_statuses():
        for k in cli_statuses:
            cli_statuses[k] = "Checking..."
            
        def run_check(key, func, *args):
            try:
                cli_statuses[key] = func(*args)
            except Exception:
                cli_statuses[key] = "Unknown"

        threads = [
            threading.Thread(target=run_check, args=("gemini", get_gemini_status)),
            threading.Thread(target=run_check, args=("claude", get_cli_status, ['claude', 'auth', 'status'])),
            threading.Thread(target=run_check, args=("codex", get_cli_status, ['codex', 'login', 'status'])),
            threading.Thread(target=run_check, args=("gh", get_cli_status, ['gh', 'auth', 'status'])),
            threading.Thread(target=run_check, args=("opencode", get_opencode_status)),
            threading.Thread(target=run_check, args=("ollama", get_ollama_status)),
        ]
        for t in threads:
            t.start()
        return threads

    # Trigger initial check in background
    threads = refresh_cli_statuses()
    first_render = True

    while True:
        # Settings file read on each loop is extremely fast, so we do it here.
        settings = read_json(settings_path) if settings_path.exists() else {}

        clear_screen()
        # Setup status bar
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("AI Provider Access")
            print("\033[90mAccess is checked through provider CLIs first; saved credentials are fallback only.\033[0m")
            print("\033[90mColumns:\033[0m")
            print("  • \033[97mCLI Readiness\033[0m : Status of the local CLI executable (Signed in, CLI ready, Not installed, etc.)")
            print("  • \033[97mCredentials\033[0m   : API Key status (Saved in settings, set via Env Var, or using active CLI Login)")
            print("  • \033[97mAccess\033[0m        : Overall usability status (READY if CLI is authenticated OR an API key is available)\n")

            # Gather Status Data using cached CLI statuses
            providers = [
                {"label": "Antigravity CLI", "cli": "agy", "key_id": "gemini_api_key", "status": cli_statuses["gemini"]},
                {"label": "Claude Code", "cli": "claude", "key_id": "anthropic_api_key", "status": cli_statuses["claude"]},
                {"label": "Codex / GPT-5", "cli": "codex", "key_id": "openai_api_key", "status": cli_statuses["codex"]},
                {"label": "GitHub CLI", "cli": "gh", "key_id": None, "status": cli_statuses["gh"]},
                {"label": "OpenCode CLI", "cli": "opencode", "key_id": None, "status": cli_statuses["opencode"]},
                {"label": "Ollama (Local)", "cli": "ollama", "key_id": None, "status": cli_statuses["ollama"]},
            ]

            # Table Header - Narrower for better responsiveness
            header = f"{'Provider/Tool':15} | {'CLI Readiness':13} | {'Credentials':11} | {'Access'}"
            print(f"\033[1;97m{header}\033[0m")
            print_divider("-")

            for p in providers:
                status = p["status"]
                
                # 1. CLI Column
                if status == "Checking...":
                    raw_cli_status = "Checking..."
                    cli_display = f"\033[93m{raw_cli_status:13}\033[0m"
                    cli_ready = False
                elif status in ["Logged In", "Ready", "Running"]:
                    raw_cli_status = {
                        "Logged In": "Signed in",
                        "Ready": "CLI ready",
                        "Running": "Running",
                    }[status]
                    cli_display = f"\033[92m{raw_cli_status:13}\033[0m"
                    cli_ready = True
                elif status == "Not Installed":
                    raw_cli_status = "Not installed"
                    cli_display = f"\033[90m{raw_cli_status:13}\033[0m"
                    cli_ready = False
                elif status == "Not Logged In":
                    raw_cli_status = "Need sign-in"
                    cli_display = f"\033[1;91m{raw_cli_status:13}\033[0m"
                    cli_ready = False
                elif status == "Offline":
                    raw_cli_status = "Offline"
                    cli_display = f"\033[1;91m{raw_cli_status:13}\033[0m"
                    cli_ready = False
                else:
                    raw_cli_status = "Failed"
                    cli_display = f"\033[1;91m{raw_cli_status:13}\033[0m"
                    cli_ready = False

                # 2. Credential Column
                key_ready = False
                raw_key_status = "None"
                key_color = "\033[90m" # Default grey

                if p["key_id"]:
                    current_key = settings.get(p["key_id"], "")
                    if current_key:
                        raw_key_status = "Saved"
                        key_color = "\033[92m" # Green
                        key_ready = True
                    elif p["key_id"].upper() in os.environ:
                        raw_key_status = "Env Var"
                        key_color = "\033[1;96m" # Blue
                        key_ready = True
                    elif cli_ready:
                        raw_key_status = "CLI Login"
                        key_color = "\033[1;96m" # Cyan
                        key_ready = False
                else:
                    raw_key_status = "CLI Only"
                    key_color = "\033[90m"

                key_display = f"{key_color}{raw_key_status:11}\033[0m"

                # 3. Overall Ready Column
                if cli_ready or key_ready:
                    ready_display = "\033[1;92m✓ READY\033[0m"
                elif status == "Checking...":
                    ready_display = "\033[93m⏱ CHECKING\033[0m"
                else:
                    ready_display = "\033[1;91m✗ LOCKED\033[0m"

                print(f"{p['label']:15} | {cli_display} | {key_display} | {ready_display}")

            print()

            print_header("ACTIONS")
            
            # Single-column Layout for Actions
            print(f"  \033[1;96mFallback Credentials\033[0m")
            print(f"    \033[90m(API keys stored locally; used when CLI logins are expired or in headless/CI environments)\033[0m")
            print(f"    [\033[1;92mG\033[0m] Update Antigravity credential")
            print(f"    [\033[1;92mA\033[0m] Update Anthropic credential")
            print(f"    [\033[1;92mO\033[0m] Update OpenAI credential")

            print(f"\n  \033[1;96mBrowser Logins (OAuth)\033[0m")
            print(f"    \033[90m(Authenticates provider CLIs directly using browser OAuth for local development)\033[0m")
            print(f"    [\033[1;92m1\033[0m] Login Antigravity")
            print(f"    [\033[1;92m2\033[0m] Login Claude")
            print(f"    [\033[1;92m3\033[0m] Login Codex")
            print(f"    [\033[1;92m4\033[0m] Login GitHub")
            print(f"    [\033[1;92m5\033[0m] Login OpenCode")

            print_header("MANAGEMENT")
            print(f"    \033[90m(Clear configured API keys or exit the provider setup submenu)\033[0m")
            print(f"    [\033[1;91mC\033[0m] Clear saved credentials")
            print(f"    [\033[1;91mB\033[0m] Back")

            # If we are loading statuses for the first time, render the loading indicator,
            # wait for threads, and redraw immediately.
            if first_render and threads:
                first_render = False
                status_bar.render(at_bottom=True, force=True, prompt=get_choice_prompt("Choice:", "(loading status...)"))
                for t in threads:
                    t.join()
                threads = None
                continue

            # Anchor prompt to bottom
            prompt = get_choice_prompt("Choice:", "(action)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()

            if choice == "b": break

            key_to_update = None
            login_cmd = None

            if choice == "g": key_to_update = "gemini_api_key"
            elif choice == "a": key_to_update = "anthropic_api_key"
            elif choice == "o": key_to_update = "openai_api_key"
            elif choice == "1":
                if cli_statuses["gemini"] == "Logged In":
                    print("\n\033[1;93mYou are already logged in to Antigravity CLI.\033[0m")
                    if not prompt_confirm("Do you still want to launch it?", default=False):
                        continue
                login_cmd = ["agy"]
            elif choice == "2": login_cmd = ["claude", "auth", "login"]
            elif choice == "3": login_cmd = ["codex", "login"]
            elif choice == "4": login_cmd = ["gh", "auth", "login"]
            elif choice == "5": login_cmd = ["opencode", "auth", "login"]
            elif choice == "c":
                warning = "\033[1;91m⚠️  DANGER: THIS WILL PERMANENTLY REMOVE ALL MANUAL API KEYS FROM YOUR CONFIG.\033[0m\n"
                warning += "You will have to re-enter them manually. Are you sure?"
                if prompt_confirm(warning, default=False):
                    for kid in ["gemini_api_key", "anthropic_api_key", "openai_api_key"]: settings.pop(kid, None)
                    write_json(settings_path, settings)
                    print("✅ Saved keys cleared.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                continue

            if key_to_update:
                print(f"\n  Updating: \033[97m{key_to_update}\033[0m")
                new_val = prompt_password("🔑 Key:", placeholder="(enter to skip)")
                if new_val:
                    settings[key_to_update] = new_val
                    write_json(settings_path, settings)
                    print("✅ Key saved.")
                else:
                    print("⚠️  No changes made.")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif login_cmd:
                print_header(f"Launching {' '.join(login_cmd)}")
                print("The CLI will open your browser for authentication.")
                print("Follow the prompts and return here when finished.\n")
                if choice == "1":
                    print("\033[1;93m⚠️  NOTE: Antigravity CLI will launch its interactive session.")
                    print("To return to this menu, type '/exit' or 'exit' and press Enter inside the prompt.\033[0m")
                    input("\n\033[1;96mTap Enter to launch Antigravity CLI...\033[0m")
                subprocess.run(login_cmd)
                threads = refresh_cli_statuses()
                first_render = True
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
def handle_system_health(session_allowed_machines: list[str], session_allowed_models: list[str], status_bar: StatusBar | None = None):
    # 1. Local Prerequisites & Environment (formerly check_setup.py)
    run_script("check_setup.py", [], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models, prompt="")
    sys.stdout.write("\033[r\033[2J\033[H\033[?25h")
    sys.stdout.flush()
    
    from probe_machine import load_machines, probe_machine
    
    binaries = ["antigravity", "gemini", "claude", "codex", "gh", "ollama", "opencode", "xcodebuild", "firebase"]

    def collect_fleet_dependency_results():
        machines_config = load_machines()
        machines = [m for m in machines_config if m["name"] in session_allowed_machines]
        results = []
        for m in machines:
            if m["name"] != "local":
                print(f"  - Probing remote machine \033[1;96m{m['name']}...\033[0m")
            probe = probe_machine(m)
            results.append((m["name"], probe))
        return machines, results

    machines, results = run_with_loading_screen(
        "Fleet Dependency Matrix",
        ["\033[90mProbing current fleet to verify cross-machine capability matching...\033[0m\n"],
        "Auditing fleet prerequisites",
        status_bar,
        collect_fleet_dependency_results,
    )

    labels = {
        "antigravity": "Antigravity",
        "gemini": "Gemini",
        "claude": "Claude",
        "codex": "Codex",
        "gh": "GitHub CLI",
        "ollama": "Ollama",
        "opencode": "OpenCode",
        "xcodebuild": "Xcode",
        "firebase": "Firebase",
    }
    
    print("\n\033[1;97mMachine Capability Report\033[0m")
    print("\033[90mEach machine is checked for the CLIs required by planner, builder, review, delivery, and local test workflows.\033[0m")

    for name, probe in results:
        bins = probe.get("binaries", {})
        installed = [labels.get(binary, binary) for binary in binaries if bins.get(binary, False)]
        missing = [labels.get(binary, binary) for binary in binaries if not bins.get(binary, False)]
        reachable = probe.get("reachable", True)
        status = "\033[92mONLINE\033[0m" if reachable else "\033[1;91mUNREACHABLE\033[0m"
        installed_count = len(installed)
        total_count = len(binaries)
        readiness_color = "\033[92m" if not missing and reachable else "\033[93m" if reachable else "\033[1;91m"
        readiness = "complete" if not missing and reachable else "partial" if reachable else "offline"

        print(f"\n  \033[90m┌─\033[0m \033[1;97m{name}\033[0m")
        print(f"  \033[90m│\033[0m  Status:    {status}")
        print(f"  \033[90m│\033[0m  Coverage:  {readiness_color}{installed_count}/{total_count} tools, {readiness}\033[0m")
        if probe.get("repo_path") and not probe.get("repo_path_ok", True):
            print(f"  \033[90m│\033[0m  Repo Path: \033[1;91mMISSING\033[0m \033[90m{probe.get('repo_path')}\033[0m")
        elif probe.get("repo_path"):
            print(f"  \033[90m│\033[0m  Repo Path: \033[92mOK\033[0m \033[90m{probe.get('repo_path')}\033[0m")
        if probe.get("repo_warning"):
            print(f"  \033[90m│\033[0m  Repo Note: \033[93m{probe.get('repo_warning')}\033[0m")
        if probe.get("probe_error"):
            print(f"  \033[90m│\033[0m  Error:     \033[1;91m{probe.get('probe_error')}\033[0m")
        print(f"  \033[90m│\033[0m  Available: \033[92m{', '.join(installed) if installed else 'none'}\033[0m")
        print(f"  \033[90m│\033[0m  Missing:   \033[1;91m{', '.join(missing) if missing else 'none'}\033[0m")
        mcp_plugins = probe.get("mcp_plugins", {})
        optional = [label for label, active in mcp_plugins.items() if active]
        print(f"  \033[90m│\033[0m  Optional:  \033[96m{', '.join(optional) if optional else 'none'}\033[0m")
        print(f"  \033[90m└─\033[0m")
        
    print(f"\n\033[90mTotal Machines in session: {len(machines)}\033[0m")
    input("\n\033[1;96mTap Enter to return to menu...\033[0m")

def handle_fleet_hygiene(session_allowed_machines):
    from probe_machine import load_machines, probe_machine
    print_header("Zombie Purge & Cache Cleanup")

    print("\033[1;97mWhat does this do?\033[0m")
    print(" This tool scans your fleet for 'Zombie' processes—builds or simulators that")
    print(" have been running for more than 12 hours. These often consume massive RAM")
    print(" and CPU, slowing down new jobs.\n")

    print("\033[1;92mBenefits:\033[0m")
    print(" ✅ Frees up system memory and CPU cores.")
    print(" ✅ Clears local DerivedData (build caches) to fix 'ghost' compiler errors.")
    print(" ✅ Improves overall scheduling reliability.\n")

    print("\033[1;91mRisks:\033[0m")
    print(" ⚠️  Will forcefully terminate any active (but old) Xcode builds.")
    print(" ⚠️  First build after purge will be slower (re-indexing cache).\n")

    print("\033[90mScanning for stale processes (running > 12h)...\033[0m\n")

    machines = load_machines()
    machines = [m for m in machines if m["name"] in session_allowed_machines]

    all_stale = []
    for m in machines:
        if m["name"] != "local":
            print(f"      - Probing remote machine \033[1;96m{m['name']}\033[0m...")
        probe = probe_machine(m)
        stale = probe.get("stale_processes", [])
        if stale:
            for p in stale:
                all_stale.append((m, p))
                print(f"  ❌ \033[1;97m{m['name']:15}\033[0m | PID: {p['pid']:8} | Runtime: {p['etime']:10} | CMD: {p['comm']}")

    if not all_stale:
        print("\033[92m✅ No zombie processes detected. Fleet is clean.\033[0m")
        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
        return

    print(f"\nFound {len(all_stale)} potential zombie processes.")
    if prompt_confirm("\033[1;91mPurge listed processes and clear DerivedData?\033[0m", default=False):
        purged = purge_zombie_processes(session_allowed_machines, silent=False)
        print(f"\n✅ Zombie purge & cache cleanup complete. Purged {purged} processes.")
        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
    else:
        print("\nNo processes were harmed.")
        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
def handle_role_prompts(session_allowed_machines, session_allowed_models):
    while True:
        clear_screen()
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Customize Agent Instructions (Role Prompts)")
            print("The Orchestrator uses specialized AI Agents for different tasks.")
            print("You can override their instructions by placing .md files in:")
            print(f"  \033[1;97m{PROJECT_CONFIG.runtime_dir.name}/prompts/\033[0m\n")

            prompts_dir = PROJECT_CONFIG.runtime_dir / "prompts"
            if prompts_dir.exists() and any(prompts_dir.iterdir()):
                print(f"✅ \033[1;92mCustom prompts are active\033[0m in \033[97m{prompts_dir.relative_to(ROOT)}\033[0m")
                print("Agents will follow your specific rules instead of defaults.")
            else:
                print("❌ \033[90mNo custom prompts found. Using system defaults.\033[0m")

            print("\nActions:")
            print("  [\033[1;92m1\033[0m] Copy system default templates to project (to enable editing)")
            if prompts_dir.exists():
                print("  [\033[1;91m2\033[0m] Delete custom prompts (revert to system defaults)")
            print("  [\033[1;91mB\033[0m] Back")

            # Anchor prompt to bottom
            prompt = get_choice_prompt("Choice:", "(index or letter)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()

            if choice == "b":
                break
            elif choice == '1':
                print("\nCopying templates...")
                cli_path = SCRIPTS_DIR.parent / "cli.py"
                subprocess.run([sys.executable, str(cli_path), "wizard", "--copy-prompt-overrides", "--non-interactive", "--force"], cwd=str(ROOT))
                print("\n✅ Templates copied! You can now edit them.")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == '2' and prompts_dir.exists():
                if prompt_confirm("Are you sure you want to delete all custom prompts?", default=False):
                    import shutil
                    shutil.rmtree(prompts_dir)
                    print("✅ Custom prompts deleted. Reverted to system defaults.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")

def handle_change_target_project(status_bar: StatusBar) -> None:
    from orchestrator.project_config import load_recent_projects, remember_project

    selected_idx = 0

    while True:
        recent = load_recent_projects()
        projects = recent.get("projects", [])
        active_name = recent.get("active")

        if selected_idx >= len(projects):
            selected_idx = max(0, len(projects) - 1)

        clear_screen()
        status_bar.set_scroll_region()
        print_header("Change Target Project")

        print("\033[1;97mCurrent Project\033[0m")
        print(f"  Name: \033[97m{PROJECT_CONFIG.project_name}\033[0m")
        print(f"  Root: \033[90m{ROOT}\033[0m")
        print("\n\033[90mChanging projects restarts the console with that project as the active workspace.\033[0m\n")

        if not projects:
            print("\033[1;97mRecent Projects\033[0m")
            print("  No recent projects found.\n")
            print("\033[1;97mActions\033[0m")
            print("  [\033[1;92mA\033[0m] Add Project")
            print("  [\033[1;91mB\033[0m] Back")

            prompt = get_choice_prompt("Choice:", "(A/B)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            key = get_key().strip().lower()
            clear_choice_placeholder()
            if key == "b":
                return
        else:
            print("\033[1;97mRecent Projects\033[0m")
            print("\033[1;90m(Arrows: navigate, Enter: select, B: back)\033[0m\n")

            for idx, project in enumerate(projects):
                is_selected = idx == selected_idx
                is_active = project.get("name") == active_name
                cursor = ">" if is_selected else " "
                icon = "[x]" if is_selected else "[ ]"
                prefix = f"{cursor} {icon}  "
                active_marker = " \033[92m(active)\033[0m" if is_active else ""
                name = project.get("name", "Unnamed Project")
                if is_selected:
                    hl = "\033[1;97;48;5;25m"
                    res = "\033[0m"
                    marker = active_marker.replace(res, hl)
                    print(f"{hl}{prefix}{name}{marker}\033[K{res}")
                else:
                    print(f"{prefix}{name}{active_marker}")

            print("\n\033[1;97mActions\033[0m")
            print("  [\033[1;92mA\033[0m] Add Project")
            print("  [\033[1;92mH\033[0m] Add project help")
            print("  [\033[1;91mB\033[0m] Back")

            status_bar.render(at_bottom=True, force=True)
            key = get_key().strip().lower()

            if key == "b":
                return
            if key in {"up", "k"}:
                selected_idx = (selected_idx - 1) % len(projects)
                continue
            if key in {"down", "j"}:
                selected_idx = (selected_idx + 1) % len(projects)
                continue
            if key == "h":
                clear_screen()
                status_bar.set_scroll_region()
                print_header("Add Project Help")
                print("\033[1;97mRecommended\033[0m")
                print("  Press \033[1;92mA\033[0m from the project menu, then paste or drag the project folder path.")
                print("  The console will add it to Recent Projects and switch to it after you confirm.\n")
                print("\033[1;97mTerminal alternative\033[0m")
                print("  Run one of these from any terminal, then return to this menu:\n")
                print("    \033[1;96morchestrator use /path/to/project\033[0m")
                print("    \033[1;96morchestrator init --project /path/to/project\033[0m")
                prompt = get_choice_prompt("Back:", "(Enter)")
                status_bar.render(at_bottom=True, force=True, prompt=prompt)
                get_key()
                clear_choice_placeholder()
                continue
            if key == "enter":
                selected = projects[selected_idx]
                remember_project(Path(selected["root"]), selected["name"], active=True)
                clear_screen()
                status_bar.set_scroll_region()
                print_header("Switching Target Project")
                print(f"  Project: \033[97m{selected['name']}\033[0m")
                print(f"  Root:    \033[90m{selected['root']}\033[0m")
                print("\nRestarting console...")
                status_bar.render(at_bottom=True, force=True)
                if "ORCHESTRATOR_PROJECT_ROOT" in os.environ:
                    del os.environ["ORCHESTRATOR_PROJECT_ROOT"]
                os.execvp("orchestrator", ["orchestrator", "console"])

        if key == "a":
            clear_screen()
            status_bar.set_scroll_region()
            print_header("Add Target Project")
            print("Paste or drag the project folder path here.")
            print("Relative paths are resolved from the current active project.\n")
            print("\033[1;97mExamples\033[0m")
            print("  \033[90m~/projects/my-ios-app\033[0m")
            print("  \033[90m./my-ios-app\033[0m\n")
            status_bar.render(at_bottom=True, force=True)
            try:
                path_str = prompt_input("Project path:", placeholder="(Enter to cancel)")
            except BackException:
                continue
            if not path_str:
                continue
            path_str = path_str.strip("'\"").strip()
            if not path_str:
                continue
            p = Path(path_str).expanduser()
            if not p.is_absolute():
                p = (ROOT / p).resolve()
            else:
                p = p.resolve()

            if not p.exists():
                print(f"\n\033[1;91mError: Path does not exist.\033[0m")
                print(f"  Path checked: \033[90m{p}\033[0m")
                prompt = get_choice_prompt("Continue:", "(Enter)")
                status_bar.render(at_bottom=True, force=True, prompt=prompt)
                get_key()
                clear_choice_placeholder()
                continue

            if not p.is_dir():
                print(f"\n\033[1;91mError: Path is not a directory.\033[0m")
                print(f"  Path: \033[90m{p}\033[0m")
                prompt = get_choice_prompt("Continue:", "(Enter)")
                status_bar.render(at_bottom=True, force=True, prompt=prompt)
                get_key()
                clear_choice_placeholder()
                continue

            try:
                from orchestrator.project_config import project_display_name
                proj_name = project_display_name(p)
            except:
                proj_name = p.name

            print(f"\nFound project: \033[1;92m{proj_name}\033[0m")
            print(f"Path: \033[90m{p}\033[0m\n")

            if prompt_confirm("Add this project and switch to it now?"):
                remember_project(p, proj_name, active=True)
                clear_screen()
                status_bar.set_scroll_region()
                print_header("Switching Target Project")
                print(f"  Project: \033[97m{proj_name}\033[0m")
                print(f"  Root:    \033[90m{p}\033[0m")
                print("\nRestarting console...")
                status_bar.render(at_bottom=True, force=True)
                if "ORCHESTRATOR_PROJECT_ROOT" in os.environ:
                    del os.environ["ORCHESTRATOR_PROJECT_ROOT"]
                os.execvp("orchestrator", ["orchestrator", "console"])
            continue

def handle_configuration_menu(session_allowed_machines: list[str], session_allowed_models: list[str]) -> tuple[list[str], list[str]]:
    """Secondary menu for advanced setup, tools, and configuration."""
    while True:
        clear_screen()
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Configuration & Advanced Tools")

            settings_path = CONFIG_DIR / "settings.json"
            global_base = PROJECT_CONFIG.base_branch
            if settings_path.exists():
                try:
                    s = read_json(settings_path)
                    global_base = s.get("global_base_branch", PROJECT_CONFIG.base_branch)
                except:
                    pass

            print("    [\033[93mM\033[0m] LLM Models (Session Defaults)")
            print("    [\033[93mK\033[0m] Manage LLM API Keys")
            print("    [\033[93mF\033[0m] Manage Machine Fleet")
            print("    [\033[93mP\033[0m] Run Prerequisite Audit")
            print(f"    [\033[93mG\033[0m] Select Base Branch (\033[97m{global_base}\033[0m)")
            print("    [\033[93mC\033[0m] Change Target Project")
            print("    [\033[93mA\033[0m] Manage Archived Jobs")
            print("    [\033[93mI\033[0m] AI Instruction Settings (.md files)")
            print("    [\033[93mE\033[0m] Email Notification Settings")
            print("    [\033[93mS\033[0m] Documentation & Architecture Guides")

            print("    [\033[93mD\033[0m] Firebase App Distro (Delivery)")

            print("    [\033[93mT\033[0m] Orchestrator Self-Tests")
            print("    [\033[93mU\033[0m] Update Orchestrator (Local & Fleet)")
            print("    [\033[1;91mB\033[0m] Back")
            # Anchor prompt to bottom
            prompt = get_choice_prompt("Choice:", "(letter)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()
            
            if choice == "b":
                break
            elif choice == "m":
                while True:
                    options, value_map, details_map = run_with_loading_screen(
                        "Select LLM Models for Session",
                        [
                            "\n\033[90mChecking model availability from local CLIs and configured API keys...\033[0m",
                            "\033[90mThis can take a few seconds when provider CLIs are slow to respond.\033[0m",
                        ],
                        "Checking model availability",
                        status_bar,
                        get_model_selection_data,
                    )

                    curr_defaults = resolve_model_selection_defaults(session_allowed_models, value_map)

                    try:
                        footer = "[\033[1;92mR\033[0m] Sync Registry  [\033[1;92mD\033[0m] Live Discovery  [\033[1;91mB\033[0m] Back"
                        new_labels = prompt_checkbox("select models", options, curr_defaults, extra_keys=["r", "d", "b"], footer=footer, details_map=details_map, status_bar=status_bar)
                        if new_labels:
                            session_allowed_models = [value_map[label] for label in new_labels]
                        else:
                            print("Warning: At least one model must be selected. Keeping previous choice.")
                            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                        break
                    except KeyInterruptException as exc:
                        if exc.key == "b":
                            break
                        if exc.key in ["r", "d"]:
                            live = (exc.key == "d")
                            from model_registry import sync_models
                            action = "Syncing with registry..." if not live else "Pinging provider APIs..."
                            print(f"\n\n📡 {action}")
                            success, msg = sync_models(live_discovery=live)
                            if success:
                                print(f"✅ {msg}")
                            else:
                                print(f"❌ {msg}")
                            input("\n\033[1;96mTap Enter to continue...\033[0m")
                            continue # Re-open menu
                        raise
                    except BackException:
                        break
            elif choice == "k":
                handle_api_keys(session_allowed_machines, session_allowed_models)
            elif choice == "f":
                session_allowed_machines = handle_fleet_management(session_allowed_machines)
            elif choice == "p":
                handle_system_health(session_allowed_machines, session_allowed_models, status_bar=status_bar)
            elif choice == "z":
                handle_fleet_hygiene(session_allowed_machines)
            elif choice == "e":
                handle_email_settings(session_allowed_machines, session_allowed_models)
            elif choice == "g":
                clear_screen()
                print_header("Select Project Base Branch")
                print("\033[90mThis branch is used as the 'source of truth' for delta calculations.\033[0m\n")
                # List local branches as options
                try:
                    res = subprocess.run(["git", "branch", "--format=%(refname:short)"], capture_output=True, text=True)
                    branches = [b.strip() for f in res.stdout.splitlines() if (b := f.strip())]
                    # Ensure the configured base, main, and master are prioritized if they exist
                    priority = [PROJECT_CONFIG.base_branch, "main", "master"]
                    options = [p for p in priority if p in branches] + [b for b in branches if b not in priority]
                    
                    new_base = prompt_radio("Select Primary Base Branch:", options, default=global_base, status_bar=status_bar)
                    if new_base:
                        settings = read_json(settings_path) if settings_path.exists() else {}
                        settings["global_base_branch"] = new_base
                        write_json(settings_path, settings)
                        print(f"\n✅ Base branch set to: \033[97m{new_base}\033[0m")
                        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                except BackException:
                    continue
                except Exception as ex:
                    print(f"\033[1;91mError updating base branch: {ex}\033[0m")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")

            elif choice == "c":
                handle_change_target_project(status_bar)
            elif choice == "a":
                handle_archived_jobs_menu(session_allowed_machines, session_allowed_models)
            elif choice == "i":
                handle_instruction_files(session_allowed_machines, session_allowed_models)
            elif choice == "d":
                handle_firebase_distro(session_allowed_machines, session_allowed_models)
            elif choice == "s":
                while True:
                    clear_screen()
                    with StatusBar({
                        "allowed_machines": session_allowed_machines,
                        "online_machines": get_online_machines(session_allowed_machines),
                        "allowed_models": session_allowed_models
                    }, sub_menu=True) as status_bar:
                        status_bar.set_scroll_region()
                        print_header("Documentation & Architecture Guides")
                        print("  New here? Start with Getting Started. Use User Guide as the full reference.\n")
                        
                        help_docs = [
                            ORCHESTRATOR_HELP_DOCS_DIR / name
                            for name in ORCHESTRATOR_HELP_DOC_NAMES
                            if (ORCHESTRATOR_HELP_DOCS_DIR / name).exists()
                        ]
                        orchestrator_docs = sorted(unique_doc_paths(help_docs), key=doc_menu_sort_key)
                        orchestrator_doc_keys = {doc_path_key(doc) for doc in orchestrator_docs}

                        docs = list(DOCS_DIR.glob("*.md"))
                        root_docs = [ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "AI_AGENT_SETUP.md"]
                        project_docs = sorted(
                            [
                                doc
                                for doc in unique_doc_paths(docs + [d for d in root_docs if d.exists()])
                                if doc_path_key(doc) not in orchestrator_doc_keys
                            ],
                            key=doc_menu_sort_key,
                        )
                        all_docs = orchestrator_docs + project_docs
                        
                        next_index = 0
                        for section_title, section_docs in [
                            ("Orchestrator Docs", orchestrator_docs),
                            ("Project Docs", project_docs),
                        ]:
                            if not section_docs:
                                continue
                            print(f"  \033[1;97m{section_title}\033[0m")
                            for doc in section_docs:
                                name = doc_menu_plain_name(doc)
                                description = DOC_MENU_DESCRIPTIONS.get(doc.name)
                                print(f"    [\033[1;96m{next_index:2}\033[0m] {name}")
                                if description:
                                    print(f"         \033[90m{description}\033[0m")
                                next_index += 1
                            print()
                        
                        print("    [\033[1;91mB\033[0m] Back")
                        
                        prompt = get_choice_prompt("Choice:", "(index or B)")
                        status_bar.render(at_bottom=True, force=True, prompt=prompt)
                        sub_choice = get_key().strip().lower()
                        clear_choice_placeholder()
                        
                        if sub_choice == "b":
                            break
                        
                        if sub_choice.isdigit():
                            idx = int(sub_choice)
                            if 0 <= idx < len(all_docs):
                                target_doc = all_docs[idx]
                                clear_screen()
                                print_header(f"READING: {target_doc.name}")
                                # Print content with basic wrapping/indenting
                                try:
                                    print(format_markdown_for_terminal(target_doc.read_text(encoding="utf-8")))
                                except Exception as e:
                                    print(f"❌ Error reading file: {e}")
                                
                                input("\n\033[1;96mTap Enter to return to docs menu...\033[0m")
                                continue
            elif choice == "t":
                handle_tooling_tests(session_allowed_machines, session_allowed_models)
            elif choice == "u":
                handle_update_orchestrator(session_allowed_machines)
            elif choice == "v":
                handle_app_tests(session_allowed_machines, session_allowed_models)
            elif choice == "r":
                print_header("Refreshing & Syncing with GitHub")
                handle_cleanup_closed(silent=False, confirm=False)
                print("\n\033[93mRefreshing machine availability...\033[0m")
                machines_config = load_machines()
                refresh_fleet_status(machines_config)
                session_allowed_machines = [m for m in session_allowed_machines if FLEET_AVAILABILITY.get(m, False)]
                print("\n✅ Refresh and sync successful.")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
    
    return session_allowed_machines, session_allowed_models

def check_essential_environment(session_allowed_machines: list[str]):
    """Performs a quick silent check for critical setup issues and stale processes."""
    import shutil
    has_gh = shutil.which("gh") is not None
    
    # Check for AI availability (simplified)
    has_env_key = any(k in os.environ for k in ["GEMINI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY", "OPENAI_API_KEY"])
    
    settings_path = CONFIG_DIR / "settings.json"
    has_settings_key = False
    if settings_path.exists():
        try:
            s = read_json(settings_path)
            has_settings_key = any(s.get(k) for k in ["gemini_api_key", "anthropic_api_key", "openai_api_key", "codex_api_key"])
        except: pass
        
    has_ollama = shutil.which("ollama") is not None
    has_opencode = shutil.which("opencode") is not None
    
    # 1. Critical Environment Check
    if not has_gh or not (has_env_key or has_settings_key or has_ollama or has_opencode):
        print_header("First-Time Setup Detected")
        print("\n    \033[93mIt looks like your environment isn't fully configured yet.\033[0m")
        print("    (Missing GitHub CLI or AI Providers)")
        
        if prompt_confirm("\n    Would you like to run the guided Environment Self-Check now?", default=True):
            from check_setup import check
            check()
            print("\n    Check complete. You can run it again any time from the Configuration menu.")
            input("    \033[1;96mPress Enter to start the Orchestrator...\033[0m")

def main_loop():
    # Sync with GitHub on launch
    clear_screen()
    handle_cleanup_closed(silent=True)
    
    session_allowed_models = list(DEFAULT_FALLBACKS)
    
    # Initialize allowed machines and availability from config
    global FLEET_AVAILABILITY
    try:
        machines_config = load_machines()
        print("\n\033[93mPinging fleet for availability...\033[0m")
        refresh_fleet_status(machines_config)
        
        session_allowed_machines = [m["name"] for m in machines_config if m.get("enabled", True) and FLEET_AVAILABILITY.get(m["name"], False)]
    except:
        session_allowed_machines = ["mac1"]

    check_essential_environment(session_allowed_machines)
    
    # Return to normal screen after checks
    clear_screen()
    
    error_msg = ""
    
    while True:
        try:
            clear_screen()
            
            # Setup status bar (rendered at bottom later)
            with StatusBar({
                "allowed_machines": session_allowed_machines,
                "online_machines": get_online_machines(session_allowed_machines),
                "allowed_models": session_allowed_models
            }) as status_bar:
                status_bar.set_scroll_region()

                # Simple, Ultra-Legible Spaced Title (Bold Cyan)
                print("\033[1;96m" + r"""
  O R C H E S T R A T O R
  -----------------------""" + f"\n  v{__version__}\033[0m")
                print_header("AI Job History")
                jobs = list_jobs()
                if not jobs:
                    print("No jobs found in ai/jobs/.")
                    print("Create a new job to populate this history.")
                else:
                    # Responsive width calculation
                    try:
                        cols, _ = os.get_terminal_size()
                    except:
                        cols = 80
                    
                    # Columns and separators (visible characters):
                    # [ID] (8) + | Type (10) + | Status (9) + | Logs (7) = 34
                    title_width = max(10, cols - 35)
                    header = f"ID   | Type    | Status | {'Title':<{title_width}} | Logs"
                    print(header)
                    print("-" * len(header))
                    for i, job in enumerate(jobs):
                        print(format_job_row(i, job, title_width=title_width))
                
                print_header("Actions")
                print("[\033[96mN\033[0m] New Job")
                print("[\033[93mV\033[0m] Run your unit tests")
                print("[\033[93mC\033[0m] Configuration & Tools")
                print("[\033[93mR\033[0m] Refresh & Sync (GitHub)")
                print("[\033[1;91mQ\033[0m] Quit")
                print()
                
                if error_msg:
                    print(f"\n\033[1;91mNOT A VALID OPTION, PLEASE TRY AGAIN... ({error_msg})\033[0m")
                    error_msg = ""

                # Anchor prompt to bottom
                prompt = get_choice_prompt("Choice:", "(number or letter)")
                status_bar.render(at_bottom=True, force=True, prompt=prompt)
                
                try:
                    choice = get_key().strip().lower()
                except KeyboardInterrupt:
                    print("\n\nLeaving Orchestrator... have a good day!")
                    sys.stdout.write("\033[r\033[?25h")
                    sys.stdout.flush()
                    sys.exit(0)
                
                if not choice:
                    clear_choice_placeholder()
                    continue

                if choice.isdigit():
                    num_jobs = len(jobs)
                    is_ambiguous = any(i >= 10 and str(i).startswith(choice) for i in range(num_jobs))
                    
                    if is_ambiguous:
                        # Clear placeholder text but stay in the styled box
                        sys.stdout.write("\033[K")
                        # Echo the first digit inside the styled box (white text on dark grey)
                        current_digits = choice
                        sys.stdout.write(f"\033[48;5;236m\033[1;96m{current_digits}\033[0m")
                        sys.stdout.flush()
                        
                        while True:
                            try:
                                key = get_key()
                            except KeyboardInterrupt:
                                choice = ""
                                break

                            if not key: continue
                            
                            if key.isdigit():
                                current_digits += key
                                # Echo new digit
                                sys.stdout.write(f"\033[48;5;236m\033[97m{key}\033[0m")
                                sys.stdout.flush()
                                
                                # If we've reached a point where no more ambiguity is possible, auto-commit
                                if not any(i >= 10 and str(i).startswith(current_digits + "0") for i in range(num_jobs)) and \
                                   not any(i >= 10 and str(i).startswith(current_digits) and i > int(current_digits) for i in range(num_jobs)):
                                    if int(current_digits) < num_jobs:
                                        choice = current_digits
                                        break
                            elif key in ("backspace", "delete", "\x7f"):
                                if len(current_digits) > 1:
                                    current_digits = current_digits[:-1]
                                    sys.stdout.write("\b \b") # This won't work perfectly with background colors
                                    # Redraw digits to maintain background
                                    sys.stdout.write(f"\rChoice: \033[48;5;236m\033[1;96m{current_digits}\033[0m")
                                    sys.stdout.flush()
                                elif len(current_digits) == 1:
                                    # Backed all the way out
                                    choice = ""
                                    break
                            elif key == "enter":
                                choice = current_digits
                                break
                            elif key.lower() == "b":
                                choice = ""
                                break
                        
                        if not choice: 
                            clear_choice_placeholder()
                            continue
                    else:
                        # Single digit, no ambiguity
                        sys.stdout.write(f"\033[48;5;236m\033[1;96m{choice}\033[0m")
                        sys.stdout.flush()
                
                clear_choice_placeholder()
                print(choice) # Final confirmation newline
                
                if not choice:
                    continue

                try:
                    if choice.isdigit():
                        # Validate numeric choice immediately
                        try:
                            idx = int(choice)
                            if 0 <= idx < len(jobs):
                                handle_job_selection(jobs[idx], session_allowed_machines, session_allowed_models)
                            else:
                                error_msg = f"Job #{choice} does not exist"
                        except ValueError:
                            error_msg = f"'{choice}' is not a valid number"
                        continue

                    if choice == "q":
                        print("\nLeaving Orchestrator... have a good day!")
                        break
                    elif choice == "c":
                        session_allowed_machines, session_allowed_models = handle_configuration_menu(session_allowed_machines, session_allowed_models)
                        continue
                    elif choice == "n":
                        handle_new_job(session_allowed_models=session_allowed_models, session_allowed_machines=session_allowed_machines)
                    elif choice == "v":
                        handle_app_tests(session_allowed_machines, session_allowed_models)
                    elif choice == "r":
                        print_header("Refreshing & Syncing with GitHub")
                        handle_cleanup_closed(silent=False, confirm=False)
                        
                        print("\n\033[93mRefreshing machine availability...\033[0m")
                        machines_config = load_machines()
                        refresh_fleet_status(machines_config)
                        
                        # Filter session_allowed_machines to only include those that are still online
                        session_allowed_machines = [m for m in session_allowed_machines if FLEET_AVAILABILITY.get(m, False)]
                        
                        print("\n✅ Refresh and sync successful.")
                        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                        continue
                    else:
                        error_msg = f"'{choice}'"
                        continue
                except KeyboardInterrupt:
                    # Returning from sub-menu
                    continue
        except BackException:
            continue

def handle_instruction_files(session_allowed_machines: list[str] | None = None, session_allowed_models: list[str] | None = None):
    session_allowed_machines = session_allowed_machines or []
    session_allowed_models = session_allowed_models or []

    cli_files = {
        "Antigravity CLI": "GEMINI.md",
        "Claude Code": "CLAUDE.md",
        "Codex": "AGENTS.md",
        "Copilot": ".github/copilot-instructions.md",
        "Ollama": "OLLAMA.md",
        "DeepSeek": "DEEPSEEK.md",
        "OpenCode": "OPENCODE.md"
    }

    required_links = [
        "docs/architecture.md",
        "docs/coding-standards.md",
        "docs/build-test-commands.md",
        "docs/ai-workflow.md"
    ]

    template = """# {filename}

## Purpose

This file provides project-scoped instructions for {name} in this repository.

Keep this file lightweight. Durable project truth belongs in shared docs, not here.

## Source of truth

Read these files first and treat them as authoritative:
- `docs/architecture.md`
- `docs/coding-standards.md`
- `docs/build-test-commands.md`
- `docs/ai-workflow.md`

## Working expectations

- Prefer minimal, reviewable diffs.
- Do not modify unrelated files.
- Preserve existing architecture unless explicitly requested.
- Focus on correctness and idiomatic code.
- Keep changes tightly scoped to the current task.
"""

    while True:
        clear_screen()
        # Setup status bar
        with StatusBar(sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Manage CLI Instructions (.md files)")
            print("  Checks the instruction files used by each AI CLI.")
            print("  Each file should point agents back to the shared project docs.\n")
            
            keys = list(cli_files.keys())
            file_statuses = {}
            for i, name in enumerate(keys):
                path = ROOT / cli_files[name]
                if path.exists():
                    try:
                        content = path.read_text(encoding="utf-8")
                        missing_links = [link for link in required_links if link not in content]
                        if missing_links:
                            status_str = "\033[93mMISCONFIGURED\033[0m"
                            file_statuses[name] = "invalid"
                        else:
                            status_str = "\033[92mVALID\033[0m"
                            file_statuses[name] = "valid"
                    except Exception:
                        status_str = "\033[1;91mERROR\033[0m"
                        file_statuses[name] = "error"
                else:
                    status_str = "\033[1;91mMISSING\033[0m"
                    file_statuses[name] = "missing"
                    
                print(f"    [\033[1;96m{i}\033[0m] {name:15} -> {cli_files[name]:30} [{status_str}]")
            
            print("\n  Actions:")
            print("    [\033[1;92mA\033[0m] Create All Missing Files")
            if any(s == "invalid" for s in file_statuses.values()):
                print("    [\033[1;92mR\033[0m] Repair Misconfigured Files")
            
            print("    [\033[1;92mP\033[0m] Customize Agent Instructions (Planner/Builder/etc)")
            print("    [\033[1;91mB\033[0m] Back")
            
            # Anchor prompt to bottom
            prompt = get_choice_prompt("Choice:", "(index or letter)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()
            
            if choice == "b":
                break
            elif choice == "p":
                handle_role_prompts(session_allowed_machines, session_allowed_models)
            elif choice == "a":
                created = 0
                for name, status in file_statuses.items():
                    if status == "missing":
                        filename = cli_files[name]
                        path = ROOT / filename
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(template.format(filename=filename, name=name), encoding="utf-8")
                        created += 1
                print(f"\n    ✅ Created {created} missing instruction files.")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "r" and any(s == "invalid" for s in file_statuses.values()):
                repaired = 0
                for name, status in file_statuses.items():
                    if status == "invalid":
                        path = ROOT / cli_files[name]
                        content = path.read_text(encoding="utf-8")
                        append_text = "\n\n## Source of truth\n\nRead these files first and treat them as authoritative:\n"
                        for link in required_links:
                            if link not in content:
                                append_text += f"- `{link}`\n"
                        path.write_text(content + append_text, encoding="utf-8")
                        repaired += 1
                print(f"\n    ✅ Repaired {repaired} files by appending required links.")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice.isdigit():
                idx = int(choice)
                if 0 <= idx < len(keys):
                    name = keys[idx]
                    filename = cli_files[name]
                    path = ROOT / filename
                    if not path.exists():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(template.format(filename=filename, name=name), encoding="utf-8")
                        print(f"\n    ✅ Created {filename}.")
                        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    else:
                        print(f"\n    Opening {filename} in editor...")
                        if file_statuses[name] == "invalid":
                            print("    \033[93mRemember to add references to universal docs (docs/architecture.md, etc.)\033[0m")
                            import time
                            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                        editor = os.environ.get("EDITOR", "nano")
                        subprocess.run([editor, str(path)])


def handle_firebase_distro(session_allowed_machines: list[str], session_allowed_models: list[str]):
    needs_check = True
    
    cli_status = ""
    auth_status = ""
    project_status = ""
    app_id_status = ""
    has_gs_info = False
    has_export_opts = False
    
    while True:
        if needs_check:
            # 1. Check Firebase CLI
            try:
                subprocess.check_output(["firebase", "--version"])
                cli_status = "\033[92mINSTALLED\033[0m"
            except FileNotFoundError:
                cli_status = "\033[1;91mMISSING\033[0m (Run: npm install -g firebase-tools)"
            
            # 2. Check Auth Status
            try:
                login_out = subprocess.check_output(["firebase", "login:list"], stderr=subprocess.STDOUT).decode("utf-8").strip()
                if "Logged in as" in login_out:
                    account = login_out.split("Logged in as ")[-1].strip()
                    auth_status = f"\033[92m{account}\033[0m"
                else:
                    auth_status = "\033[1;91mNOT LOGGED IN\033[0m (Run: firebase login)"
            except Exception:
                auth_status = "\033[1;91mERROR\033[0m"
            
            # 3. Check Active Project
            project = "NONE"
            try:
                use_out = subprocess.check_output(["firebase", "use"], stderr=subprocess.STDOUT).decode("utf-8").strip()
                if "Active Project:" in use_out:
                    project = use_out.split("Active Project:")[-1].strip()
                    project_status = f"\033[92m{project}\033[0m"
                else:
                    # Fallback 1: .firebaserc
                    firebaserc = ROOT / ".firebaserc"
                    if firebaserc.exists():
                        try:
                            rc_data = read_json(firebaserc)
                            project = rc_data.get("projects", {}).get("default", "NONE")
                            if project != "NONE":
                                project_status = f"\033[92m{project}\033[0m \033[90m(from .firebaserc)\033[0m"
                            else:
                                project_status = "\033[1;91mNONE\033[0m"
                        except:
                            project_status = "\033[1;91mNONE\033[0m"
                    else:
                        # Fallback 2: GoogleService-Info.plist
                        gs_info = ROOT / PROJECT_CONFIG.project_name / "GoogleService-Info.plist"
                        if gs_info.exists():
                            try:
                                project = subprocess.check_output(["/usr/libexec/PlistBuddy", "-c", "Print :PROJECT_ID", str(gs_info)], stderr=subprocess.DEVNULL).decode("utf-8").strip()
                                project_status = f"\033[92m{project}\033[0m \033[90m(from config)\033[0m"
                            except:
                                project_status = "\033[1;91mNONE\033[0m"
                        else:
                            project_status = "\033[1;91mNONE\033[0m"
            except Exception:
                project_status = "\033[1;91mERROR\033[0m"
            
            # Required files checks
            gs_info = ROOT / PROJECT_CONFIG.project_name / "GoogleService-Info.plist"
            has_gs_info = gs_info.exists()
            if has_gs_info:
                try:
                    app_id = subprocess.check_output(["/usr/libexec/PlistBuddy", "-c", "Print :GOOGLE_APP_ID", str(gs_info)], stderr=subprocess.DEVNULL).decode("utf-8").strip()
                    app_id_status = f"\033[92m{app_id}\033[0m"
                except Exception:
                    app_id_status = "\033[1;91mCOULD NOT EXTRACT\033[0m"
            else:
                app_id_status = "\033[1;91mN/A\033[0m"
            
            export_opts = ROOT / "ExportOptions.plist"
            has_export_opts = export_opts.exists()
            
            needs_check = False
            
        clear_screen()
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Firebase App Distro Health & Settings")
            
            print("\n  \033[1;90m--- SYSTEM CHECKS ---\033[0m")
            print(f"  Firebase CLI: {cli_status}")
            print(f"  Account:      {auth_status}")
            print(f"  Project:      {project_status}")
            
            has_ci_token = "FIREBASE_TOKEN" in os.environ
            if has_ci_token:
                print(f"  Auth Method:  \033[92mCI TOKEN (Environment)\033[0m")
            else:
                print(f"  Auth Method:  \033[1;96mLocal CLI Session\033[0m")
                
            print("\n  \033[1;90m--- REQUIRED FILES ---\033[0m")
            if has_gs_info:
                print(f"  GoogleService-Info.plist: \033[92mEXISTS\033[0m")
                print(f"  App ID (iOS):             {app_id_status}")
            else:
                print(f"  GoogleService-Info.plist: \033[1;91mMISSING\033[0m")
                
            if has_export_opts:
                print(f"  ExportOptions.plist:      \033[92mEXISTS\033[0m")
            else:
                print(f"  ExportOptions.plist:      \033[1;91mMISSING\033[0m")
                
            print("\n  \033[1;90m--- KEYCHAIN & SIGNING ---\033[0m")
            has_password = "KEYCHAIN_PASSWORD" in os.environ
            pwd_status = "\033[92mAUTOMATED (No UI popups)\033[0m" if has_password else "\033[93mMANUAL (Requires UI prompt)\033[0m"
            print(f"  Headless Signing: {pwd_status}")
            
            print("\n  \033[1;90m--- ACTIONS ---\033[0m")
            print("    [\033[1;96mL\033[0m] Login to Firebase (Browser)")
            print("    [\033[1;96mK\033[0m] Configure Headless Signing (Keychain Auto-Unlock)")
            print("    [\033[1;96mT\033[0m] Test Distribution Script (Dry Run via build delivery)")
            print("    [\033[1;96mR\033[0m] Refresh Status (Re-run checks)")
            print("    [\033[1;91mB\033[0m] Back")
            
            status_bar.render(at_bottom=True, force=True, prompt=None)
            choice = get_key().strip().lower()
            
            if choice == "b":
                break
            elif choice == "r":
                needs_check = True
            elif choice == "l":
                print_header("Launching firebase login")
                print("The CLI will open your browser for authentication.")
                print("Follow the prompts and return here when finished.\n")
                subprocess.run(["firebase", "login"])
                needs_check = True
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "k":
                handle_keychain_setup(status_bar)
                needs_check = True
            elif choice == "t":
                print("\n    Launching smoke test delivery...")
                run_script("smoke_test_delivery.py", [], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
                needs_check = True

def handle_keychain_setup(status_bar: StatusBar):
    options = [
        "Automated: Store password (Required for headless/remote builds)",
        "Manual: UI prompts (Best for local interactive use)",
        "Cancel"
    ]
    
    choice = prompt_radio("Choose Keychain Setup Mode:", options, default=options[0], clear_screen=False)
    
    if "Auto" in choice:
        print("\n\033[1;96mEnter your macOS login password (it will be saved to .secrets/project-secrets.zsh):\033[0m")
        # Use getpass style input if possible, but for simplicity in this console:
        import getpass
        pwd = getpass.getpass("🔑 Password: ")
        if pwd:
            secrets_path = ROOT / ".secrets" / "project-secrets.zsh"
            secrets_path.parent.mkdir(parents=True, exist_ok=True)
            
            content = ""
            if secrets_path.exists():
                content = secrets_path.read_text()
            
            # Remove existing KEYCHAIN_PASSWORD if any
            lines = [line for line in content.splitlines() if "KEYCHAIN_PASSWORD=" not in line]
            lines.append(f'export KEYCHAIN_PASSWORD="{pwd}"')
            
            secrets_path.write_text("\n".join(lines) + "\n")
            os.environ["KEYCHAIN_PASSWORD"] = pwd
            print("\n✅ Password saved. Background builds will now unlock the keychain automatically.")
        else:
            print("\n⚠️  No password entered. Setup cancelled.")
            
        if status_bar:
            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
        
    elif "Manual" in choice:
        print("\n--- Manual Keychain Whitelist ---")
        print("Run this command in your terminal to allow codesign to access your keys permanently:")
        print(f"\033[93msecurity set-key-partition-list -S apple-tool:,apple:,codesign: -s -k \"<your-mac-password>\" ~/Library/Keychains/login.keychain-db\033[0m")
        print("\nAfter running this once, you won't need to provide a password for background builds on this machine.")
        
        if status_bar:
            input("\n\033[1;96mTap Enter to return to menu...\033[0m")

def handle_import_email_recipients(status_bar: StatusBar, settings_path: Path, settings: dict[str, Any], emails: list[str]) -> None:
    clear_screen()
    status_bar.set_scroll_region()
    print_header("Import Recipients from CSV")

    print("\033[1;97mWhat this does\033[0m")
    print("  Scans a CSV or text file for email addresses and adds any new unique recipients.")
    print("  The file does not need a specific column name; every email-looking value is imported.\n")

    print("\033[1;97mFile Path\033[0m")
    print("  Drag and drop a CSV file here, or type a path manually.")
    print("  Examples:")
    print("    \033[90m~/Downloads/testers.csv\033[0m")
    print("    \033[90m./emails.csv\033[0m\n")

    status_bar.render(at_bottom=True, force=True)
    try:
        csv_path = prompt_input("CSV path:", placeholder="(Enter to cancel)")
    except BackException:
        return
    if not csv_path:
        return

    path = Path(csv_path.strip("'\"")).expanduser()
    if not path.is_absolute():
        path = ROOT / path

    if not path.exists():
        print(f"\n\033[1;91mError: File not found.\033[0m")
        print(f"  Path checked: \033[90m{path}\033[0m")
        prompt = get_choice_prompt("Continue:", "(Enter)")
        status_bar.render(at_bottom=True, force=True, prompt=prompt)
        get_key()
        clear_choice_placeholder()
        return
    
    try:
        content = path.read_text(encoding="utf-8")
        found = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', content)
        existing = {email.lower() for email in emails}
        added_count = 0
        for email in found:
            email_lower = email.lower()
            if email_lower not in existing:
                emails.append(email_lower)
                existing.add(email_lower)
                added_count += 1
        
        print("\n\033[1;97mImport Results\033[0m")
        print(f"  File:             \033[90m{path}\033[0m")
        print(f"  Email candidates: \033[97m{len(found)}\033[0m")
        if added_count > 0:
            settings["notification_emails"] = emails
            write_json(settings_path, settings)
            print(f"  Added:            \033[92m{added_count} new recipient(s)\033[0m")
        else:
            print("  Added:            \033[93m0 new recipients; all matches were duplicates or none were found.\033[0m")
    except Exception as ex:
        print(f"\n\033[1;91mError reading CSV: {ex}\033[0m")

    prompt = get_choice_prompt("Continue:", "(Enter)")
    status_bar.render(at_bottom=True, force=True, prompt=prompt)
    get_key()
    clear_choice_placeholder()

def handle_email_settings(session_allowed_machines: list[str], session_allowed_models: list[str]):
    settings_path = CONFIG_DIR / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = read_json(settings_path)
        except:
            pass
    
    emails = settings.get("notification_emails", [])
    provider = settings.get("notification_provider", "gmail")
    smtp_email = settings.get("smtp_email", "")
    smtp_password = "********" if settings.get("smtp_password") else ""
    
    while True:
        # Refresh dynamic values from settings at start of loop
        provider = settings.get("notification_provider", "gmail")
        smtp_email = settings.get("smtp_email", "")

        clear_screen()
        # Setup status bar
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Email Notification Settings")
            print("  \033[90mConfigures email alerts for job completions, build delivery updates, failures,\033[0m")
            print("  \033[90mor when a running task is paused and requires human review.\033[0m\n")
            if provider == "resend":
                sender_display = f"{settings.get('resend_from_email', 'NOT SET')} (via Resend)"
            else:
                sender_display = f"{smtp_email if smtp_email else 'NOT CONFIGURED'} (via Gmail)"
            print(f"  Sender: {sender_display}")
            if not emails:
                print("  Recipients: No email addresses defined.")
            else:
                print("  Recipients:")
                for i, email in enumerate(emails):
                    print(f"    [{i}] {email}")
            
            print("\n  Actions:")
            print("    [\033[1;92mA\033[0m] Add recipient address")
            print("    [\033[1;92mI\033[0m] Import recipients from CSV")
            if emails:
                print("    [\033[1;96mR\033[0m] Remove recipient address")
                print("    [\033[1;96mT\033[0m] Send Test Email")
            print("    [\033[1;96mC\033[0m] Configure Email Sender")
            print("    [\033[1;91mB\033[0m] Back")
            
            status_bar.render(at_bottom=True, force=True)
            choice = get_key().strip().lower()
            
            if choice == "b":
                break
            elif choice == "t" and emails:
                print(f"\n    Sending test email via {provider} to all recipients...")
                # Use run_script to execute notify.py
                run_script("notify.py", [f"Test Notification ({provider})", f"This is a test message from the AI Orchestrator console using {provider}.", "test-job-id"], session_machines=session_allowed_machines, session_models=session_allowed_models)
            elif choice == "a":
                status_bar.render(at_bottom=True, force=True)
                try:
                    email = prompt_input("Enter recipient email address:", placeholder="(or Enter to cancel)")
                except BackException:
                    continue
                if not email: continue

                emails.append(email)
                settings["notification_emails"] = emails
                write_json(settings_path, settings)
                print(f"✅ Added: {email}")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "i":
                handle_import_email_recipients(status_bar, settings_path, settings, emails)
            elif choice == "c":
                clear_screen()
                print_header("Configure Email Provider")
                curr_provider = settings.get("notification_provider", "gmail")
                # Remove clear_screen=False so the radio menu is visible
                try:
                    provider = prompt_radio("Select Provider:", ["gmail", "resend"], curr_provider)
                except BackException:
                    continue
                settings["notification_provider"] = provider
                
                if provider == "gmail":
                    print("\n    \033[1;96m--- Configure Gmail SMTP ---\033[0m")
                    print("    1. Go to: \033[4;96mhttps://myaccount.google.com/apppasswords\033[0m")
                    print("    2. Log in and create a name (e.g. 'AI Orchestrator')")
                    print("    3. Copy the 16-character code generated.")
                    print("    \033[90m(Leave blank and press Enter to skip/keep current)\033[0m")

                    new_smtp = prompt_input("Gmail Address:")
                    new_pass = prompt_password("🔑 App Password:", placeholder="(enter to skip)")
                    if new_smtp: settings["smtp_email"] = new_smtp
                    if new_pass: settings["smtp_password"] = new_pass
                else:
                    print("\n    \033[1;96m--- Configure Resend SMTP ---\033[0m")
                    print("    1. Go to: \033[4;96mhttps://resend.com/api-keys\033[0m")
                    print("    2. Create a new API key with 'Sending' permissions.")
                    print("    3. If you haven't verified a domain, use your Resend login email.")
                    print("    \033[90m(Leave blank and press Enter to skip/keep current)\033[0m")

                    new_key = prompt_password("Resend API Key:", placeholder="(enter to skip)")
                    print("    \033[90m(Must be a verified domain on Resend, or your login email)\033[0m")
                    new_from = prompt_input("From Email:")
                    print("    \033[90m(The name that appears in the inbox, e.g. 'AI Orchestrator')\033[0m")
                    new_name = prompt_input("Display Name:")
                    if new_key: settings["resend_api_key"] = new_key
                    if new_from: settings["resend_from_email"] = new_from
                    if new_name: settings["resend_display_name"] = new_name
                    
                write_json(settings_path, settings)
                print("\n    ✅ Provider configured.")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "r" and emails:
                status_bar.render(at_bottom=True, force=True)
                try:
                    idx_str = prompt_input("Enter index to remove:", placeholder="(number)")
                except BackException:
                    continue
                try:
                    idx = int(idx_str)
                    if 0 <= idx < len(emails):
                        emails.pop(idx)
                        settings["notification_emails"] = emails
                        write_json(settings_path, settings)
                except ValueError:
                    pass

if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "keychain-setup":
            handle_keychain_setup(None)
            sys.exit(0)
            
        main_loop()
    except KeyboardInterrupt:
        from orchestrator.scripts.common import cleanup_terminal
        cleanup_terminal()
        print("\nExiting console.")
        sys.exit(0)
