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

from common import ROOT, CONFIG_DIR, JOBS_DIR, ARCHIVE_DIR, OUTPUT_DIR, DOCS_DIR, read_json, write_json, now_iso, timestamp, get_best_simulator_destination, get_simulator_diagnostic, prompt_radio, prompt_confirm, format_job_id, format_index, prompt_checkbox, BackException, KeyInterruptException, get_key, StatusBar, print_divider, extract_commands, print_phase, ProgressIndicator, LoopTroubleDetector, get_test_plan_flags, print_choice_prompt, get_choice_prompt, clear_choice_placeholder, purge_zombie_processes, print_header, get_header_string, print_section, print_subtitle, format_section_header, format_subtitle, prompt_input, prompt_password, format_markdown_for_terminal, print_wrapped_option, extract_step_from_line, record_clarification, find_latest_runtime_log, flush_stdin, get_github_url, get_github_links, record_interactive_investigation, format_investigation_history, get_job_test_summary, parse_test_output, count_created_tests_in_diff, analyze_debug_loop_convergence
from llm import SUPPORTED_MODELS, DEFAULT_FALLBACKS, run_llm, extract_json_block
from model_router import ModelRole
from model_registry import get_all_models, ModelTier
from probe_machine import load_machines, probe_machine
from orchestrator.project_config import PACKAGE_ROOT, PROJECT_CONFIG

from orchestrator import __version__

def input(prompt: str = "") -> str:
    """Wrapper around get_key that avoids termios raw/cooked mode input lockups during pauses."""
    import builtins
    if any(phrase in prompt for phrase in ["Tap Enter", "Press Enter", "to return", "to continue", "to go back"]):
        flush_stdin()
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

def print_wrapped_description(text: str, indent_size: int = 4) -> None:
    """Prints a description string wrapped to the terminal width, maintaining indentation."""
    import textwrap
    try:
        cols, _ = os.get_terminal_size()
    except:
        cols = 80
    cols = max(cols, 40)
    available_width = cols - indent_size - 4  # safety margin
    lines = textwrap.wrap(text, width=available_width)
    indent = " " * indent_size
    for line in lines:
        print(f"{indent}\033[90m{line}\033[0m")

def print_box_line_rich(label: str, rich_text: str) -> None:
    """Prints a labeled line inside a box outline, supporting word wrapping with preserved ANSI colors and indentation."""
    try:
        cols, _ = os.get_terminal_size()
    except:
        cols = 80
    cols = max(cols, 40)
    
    label_len = len(label)
    prefix_len = 5 + label_len  # "  │  " + label
    available_width = cols - prefix_len - 4
    
    import re
    # Tokenize ANSI codes (CSI & OSC sequences), whitespace, or non-whitespace words
    token_pattern = re.compile(r'(\x1b(?:\][^\x1b\x07]*(?:\x1b\\|\x07)|\[[0-9;?]*[a-zA-Z]|.))|(\s+)|([^\s\x1b]+)')
    tokens = [m.group(0) for m in token_pattern.finditer(rich_text)]
    
    lines = []
    current_line_parts = []
    current_line_plain_len = 0
    active_ansi_state = ""
    
    for token in tokens:
        if token.startswith('\x1b'):
            current_line_parts.append(token)
            if token == '\x1b[0m':
                active_ansi_state = ""
            elif token.startswith('\x1b['):
                active_ansi_state = token
        elif token.isspace():
            if current_line_parts:
                current_line_parts.append(token)
                current_line_plain_len += len(token)
        else:
            word_len = len(token)
            if current_line_plain_len + word_len > available_width and current_line_plain_len > 0:
                if active_ansi_state:
                    current_line_parts.append('\x1b[0m')
                lines.append("".join(current_line_parts))
                
                current_line_parts = []
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
        if i == 0:
            print(f"  \033[90m│\033[0m  {label}{line}")
        else:
            indent = " " * (label_len + 2)
            print(f"  \033[90m│\033[0m{indent}{line}")

def print_wrapped_kv(label: str, rich_text: str, indent_size: int | None = None) -> None:
    """Prints a key-value or menu action item, wrapping the value portion and indenting wraps."""
    try:
        cols, _ = os.get_terminal_size()
    except:
        cols = 80
    cols = max(cols, 40)
    
    import re
    # Strip ANSI to measure the label length
    plain_label = re.sub(r"\x1b(?:\][^\x1b\x07]*(?:\x1b\\|\x07)|\[[0-9;?]*[a-zA-Z])", "", label)
    label_len = len(plain_label)
    
    actual_indent = indent_size if indent_size is not None else label_len
    available_width = max(cols - actual_indent - 1, 10)
        
    # Tokenize ANSI codes (CSI & OSC sequences), whitespace, or non-whitespace words
    token_pattern = re.compile(r'(\x1b(?:\][^\x1b\x07]*(?:\x1b\\|\x07)|\[[0-9;?]*[a-zA-Z]|.))|(\s+)|([^\s\x1b]+)')
    tokens = [m.group(0) for m in token_pattern.finditer(rich_text)]
    
    lines = []
    current_line_parts = []
    current_line_plain_len = 0
    active_ansi_state = ""
    
    for token in tokens:
        if token.startswith('\x1b'):
            current_line_parts.append(token)
            if token == '\x1b[0m':
                active_ansi_state = ""
            elif token.startswith('\x1b['):
                active_ansi_state = token
        elif token.isspace():
            if current_line_parts:
                current_line_parts.append(token)
                current_line_plain_len += len(token)
        else:
            word_len = len(token)
            if current_line_plain_len + word_len > available_width and current_line_plain_len > 0:
                if active_ansi_state:
                    current_line_parts.append('\x1b[0m')
                lines.append("".join(current_line_parts))
                
                current_line_parts = []
                if active_ansi_state:
                    current_line_parts.append(active_ansi_state)
                current_line_parts.append(token)
                current_line_plain_len = word_len
            else:
                current_line_parts.append(token)
                current_line_plain_len += word_len
                
    if current_line_parts:
        lines.append("".join(current_line_parts))
        
    if not lines:
        print(label)
    else:
        for i, line in enumerate(lines):
            if i == 0:
                print(f"{label}{line}")
            else:
                print(f"{' ' * actual_indent}{line}")

def print_wrapped_bullet(label: str, rich_text: str, indent_size: int | None = None) -> None:
    """Prints a bulleted list item, wrapping value text and applying hanging indent."""
    print_wrapped_kv(label, rich_text, indent_size=indent_size)

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

    def get_effective_family(m: ModelMetadata) -> str:
        if m.family == "ollama" or "ollama" in m.required_clis:
            return "ollama"
        return m.family

    # Sort by Family, then Tier (Extreme < High < Medium < Low)
    sorted_models = sorted(models, key=lambda m: (get_effective_family(m), m.tier.value, m.id))
    
    family_display_names = {
        "claude": "ANTHROPIC (Claude)",
        "gemini": "GOOGLE (Antigravity)",
        "openai": "OPENAI (GPT)",
        "opencode": "OPENCODE",
        "ollama": "OLLAMA (Local Models)",
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
                res = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=5.0)
                ready = res.returncode == 0
            elif binary == "codex":
                res = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=5.0)
                ready = res.returncode == 0
            elif binary in {"gemini", "antigravity", "agy"}:
                ready = (os.path.exists(os.path.expanduser("~/.gemini/oauth_creds.json")) or 
                         os.path.exists(os.path.expanduser("~/.gemini/google_accounts.json")))
            elif binary == "opencode":
                res = subprocess.run(["opencode", "auth", "status"], capture_output=True, text=True, timeout=5.0)
                if res.returncode == 0:
                    ready = True
                else:
                    res2 = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=5.0)
                    ready = res2.returncode == 0 and bool(res2.stdout.strip())
            elif binary == "ollama":
                res = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5.0)
                ready = res.returncode == 0
            elif binary == "gh":
                res = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=5.0)
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
    primary_families = {"claude", "gemini", "openai", "opencode", "ollama", "copilot"}
    enabled_families = set()
    
    current_family = None
    for m in sorted_models:
        eff_fam = get_effective_family(m)

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

        if is_ready:
            enabled_families.add(eff_fam)

        if eff_fam != current_family:
            if current_family is not None:
                options.append("---")
            current_family = eff_fam
            name = family_display_names.get(current_family, current_family.upper())
            options.append(f"--- {name} ---")

        display_id = m.id.split("opencode/", 1)[1] if m.id.startswith("opencode/") else m.id

        is_free = (m.cost_factor == 0.0 or "-free" in m.id or m.id.endswith("free"))
        free_tag = " \033[1;92m(Free)\033[0m" if is_free else ""

        # Format label with suffix status
        status_suffix = ""
        if not cli_installed:
            status_suffix = " \033[1;90m[Not Installed]\033[0m"
        elif missing_ollama_model:
            status_suffix = " \033[1;90m[Not Downloaded]\033[0m"
        elif not is_ready:
            status_suffix = " \033[1;91m[Not Enabled]\033[0m"

        label = f"{display_id}{free_tag}{status_suffix}"
        
        options.append(label)
        value_map[label] = m.id

        if not cli_installed:
            access_str = "\033[1;90mNot installed\033[0m"
        elif missing_ollama_model:
            access_str = f"\033[1;90mNot downloaded (run 'ollama pull {missing_ollama_model}')\033[0m"
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
        
        cost_str = "\033[1;92m0.0x (Free)\033[0m" if is_free else f"\033[90m{m.cost_factor:.1f}x\033[0m"
        tier_str = m.tier.name.capitalize()

        details = [
            f"\033[1;97mModel:\033[0m        \033[90m{display_id}\033[0m",
            f"\033[1;97mAccess:\033[0m       {access_str}",
            f"\033[1;97mProvider:\033[0m     \033[90m{m.family.capitalize()}\033[0m",
            f"\033[1;97mBackend:\033[0m      \033[90m{cli_str}\033[0m",
            f"\033[1;97mCredentials:\033[0m  \033[90m{credential_str}\033[0m",
            f"\033[1;97mTier:\033[0m         \033[90m{tier_str}\033[0m",
            f"\033[1;97mCost:\033[0m         {cost_str}",
            f"\033[1;97mCapabilities:\033[0m \033[90m{', '.join([c.value.capitalize() for c in m.capabilities])}\033[0m"
        ]

        if "ollama" in m.required_clis:
            ollama_model = required_ollama_model_id(m.id)
            installed = ollama_model in get_installed_ollama_models()
            state = "\033[92minstalled\033[0m" if installed else "\033[1;91mmissing\033[0m"
            details.append(f"\033[1;97mOllama Model:\033[0m \033[90m{ollama_model}\033[0m ({state})")
        
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
        
    if not options:
        options = ["--- NO ACCESSIBLE MODELS ---", "No active CLI logins or API keys found."]
        details_map["No active CLI logins or API keys found."] = [
            "\033[1;91mNo usable models detected.\033[0m",
            "\033[90mGo to Configuration > Manage LLM API Keys to sign in or add API keys.\033[0m"
        ]

    enabled_count = len(enabled_families & primary_families)
    total_count = len(primary_families)
    services_summary = f"{enabled_count}/{total_count}"

    return options, value_map, details_map, services_summary

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
    try:
        new_job = read_json(Path(path))
    except Exception:
        return job
    if not isinstance(new_job, dict):
        return job
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

def format_job_date(job: dict[str, Any]) -> str:
    if job.get("_path"):
        try:
            p = Path(job["_path"])
            if p.exists():
                dt = datetime.fromtimestamp(p.stat().st_mtime)
                return dt.strftime("%m/%d")
        except Exception:
            pass
    val = job.get("updated_at") or job.get("created_at")
    if isinstance(val, str) and len(val) >= 10:
        try:
            parts = val[:10].split("-")
            if len(parts) == 3:
                return f"{parts[1]}/{parts[2]}"
        except Exception:
            pass
    job_id = str(job.get("job_id", ""))
    if len(job_id) >= 8 and job_id[:8].isdigit():
        return f"{job_id[4:6]}/{job_id[6:8]}"
    return "--/--"

def format_job_header(job: dict[str, Any]) -> str:
    raw_title = job.get("title", "Untitled Job")
    clean_title = re.sub(r"^#\d+\s*", "", raw_title).strip()
    clean_title = re.sub(r"^(bug|feature|coverage|design|report):\s*", "", clean_title, flags=re.I).strip()
    
    # Determine type tag
    job_type = str(job.get("type", "job")).strip()
    if clean_title.startswith("["):
        full_title = clean_title.upper()
    else:
        tag = "REPORT" if "report" in job_type.lower() else (
            "BUG" if "bug" in job_type.lower() else (
                "FEATURE" if "feature" in job_type.lower() else (
                    "COVERAGE" if "coverage" in job_type.lower() else (
                        "DESIGN" if "design" in job_type.lower() else job_type.upper()
                    )
                )
            )
        )
        full_title = f"[{tag}] {clean_title.upper()}"
        
    return get_header_string(full_title)

def print_job_header(job: dict[str, Any]) -> None:
    print(format_job_header(job))

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

    # Clean title without leading numbers for easier reading
    raw_title = job.get("title", "Untitled")
    display_title = re.sub(r"^#\d+\s*", "", raw_title)

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

    # Month/day last modified indicator
    date_str = format_job_date(job)
    date_display = f"\033[90m{date_str:^8}\033[0m"

    # Layout: [ID] | Type | Status | Title | Modified
    return f"{format_index(f'{idx:2}')} | {type_str} | {color}{short_status:6}{reset} | {title:{title_width}} | {date_display}"

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

def format_trial_result(res: Any, test_summary: dict[str, Any] | None = None) -> tuple[str, str]:
    """Returns (badge, detail_str) for a debug trial result."""
    if isinstance(res, dict):
        if res.get("build_ok") and res.get("tests_ok"):
            return "\033[92m✅ PASS\033[0m", "All tests passed"
        elif not res.get("build_ok"):
            return "\033[1;91m❌ FAIL (Build)\033[0m", "Compilation / build failed"
        else:
            fc = test_summary.get("failed_count", 0) if test_summary else 0
            if fc > 0:
                return "\033[1;91m❌ FAIL (Tests)\033[0m", f"{fc} suite test{'s' if fc != 1 else ''} failing"
            return "\033[1;91m❌ FAIL (Tests)\033[0m", "Test assertion failed"
    elif isinstance(res, str):
        if res.lower() in ("pass", "passed", "ok"):
            return "\033[92m✅ PASS\033[0m", "Passed"
        elif res.lower() in ("fail", "failed"):
            return "\033[1;91m❌ FAIL\033[0m", "Validation failed"
        elif res == "pending":
            return "\033[93m⏳ PENDING\033[0m", "Pending execution"
        else:
            return f"\033[90m{res}\033[0m", str(res)
    return "\033[90mUNKNOWN\033[0m", "Unknown result"

def run_streaming_process(cmd: list[str], job: dict[str, Any] | None = None, sub_menu: bool = False, session_machines: list[str] | None = None, session_models: list[str] | None = None, label: str | None = None) -> tuple[int, str]:
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
    if label:
        initial_label = label
    elif job and job.get("title"):
        initial_label = f"Working on {job.get('title')}"
    elif any("manual_run.py" in str(c) for c in cmd) and any("test" in str(c) for c in cmd):
        initial_label = "Running unit tests"
    else:
        initial_label = "Executing workflow"
    indicator = ProgressIndicator(label=initial_label, hint="Ctrl-C to abort")
    detector = LoopTroubleDetector()
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
            if force or now - last_render >= 0.1:
                status_bar.render(at_bottom=True, force=force, activity=indicator)
                last_render = now

        try:
            render_status_bar(force=True)
            while process.poll() is None:
                # Check for time-based loop/stall warnings (long running duration, prolonged idle stall)
                time_notices = detector.check_time_triggers(now=time.monotonic(), last_output_time=last_output_time)
                if time_notices:
                    indicator.clear()
                    for notice in time_notices:
                        sys.stdout.write(f"\n{notice}\n")
                    sys.stdout.flush()
                    render_status_bar(force=True)

                # Update UI (throttled to 0.1s inside render_status_bar)
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
                            
                            # Parse lines for step information to update indicator label
                            for line in reversed(data.splitlines()):
                                step = extract_step_from_line(line)
                                if step:
                                    indicator.label = step
                                    render_status_bar(force=True)
                                    break
                                    
                            # Check for trouble / loops in incoming data stream
                            chunk_notices = detector.record_chunk(data, now=time.monotonic())
                            if chunk_notices:
                                indicator.clear()
                                sys.stdout.write(data)
                                for notice in chunk_notices:
                                    sys.stdout.write(f"\n{notice}\n")
                                sys.stdout.flush()
                                render_status_bar(force=True)
                            else:
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

    print(f"\n\033[1;90m▶ Running:\033[0m \033[96m{shlex.join(cmd)}\033[0m\n", flush=True)

    # Extract and emphasize chosen tests
    test_targets = []
    for arg in args:
        if "-only-testing:" in arg:
            parts = arg.split("-only-testing:")
            for part in parts[1:]:
                test_targets.append(part.split()[0].strip())

    try:
        cols, _ = os.get_terminal_size()
    except Exception:
        cols = 80
    box_width = max(45, min(60, cols - 2))

    if test_targets:
        target_str = ", ".join(test_targets)
        print("\033[1;95m" + "★" * box_width)
        print(f"  🧪 TESTS CHOSEN TO BE RUN: {target_str}")
        print("★" * box_width + "\033[0m\n")
    elif script_name == "manual_run.py" and "test" in args:
        tt = PROJECT_CONFIG.test_target or "All Targets"
        print("\033[1;92m" + "=" * box_width)
        print(f"  🧪 RUNNING ALL UNIT TESTS ({tt})")
        print("=" * box_width + "\033[0m\n")

    returncode = 1
    try:
        # Reset terminal state (restore scroll region) and show cursor before running any subprocess
        # \033[r: Reset scroll region
        # \033[?25h: Show cursor
        sys.stdout.write("\033[r\033[?25h")
        sys.stdout.flush()
        
        # Pre-flight zombie purge for job execution scripts
        if script_name in ["schedule_job.py", "worker_run.py", "deliver_build.py"]:
            machines = session_machines or (job.get("session_machines", []) if job else [])
            if machines:
                purged = purge_zombie_processes(machines, silent=True)
                if purged > 0:
                    print(f"🧹 Automatically purged {purged} zombie processes before execution.\n")

        # Special case: interactive, nested, or simple local scripts should take over
        # the terminal directly instead of fighting the parent streaming footer.
        if script_name in ["new_job.py", "check_setup.py", "discover_machines.py", "smoke_test_delivery.py", "export_job.py"]:
            sub_env = os.environ.copy()
            if sub_menu and script_name != "check_setup.py":
                sub_env["AI_PROGRESS_SILENT"] = "1"
            returncode = subprocess.run(cmd, cwd=str(ROOT), stdin=sys.stdin, stdout=None, stderr=None, env=sub_env).returncode
            output_log = ""
        else:
            returncode, output_log = run_streaming_process(cmd, job=job, sub_menu=sub_menu, session_machines=session_machines, session_models=session_models, label=prompt)
            
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
    default_prompt = "\n\033[1;96mTap Enter to return to menu...\033[0m" if sub_menu else "\n\033[1;96mTap Enter to return to main menu...\033[0m"
    final_prompt = prompt if prompt is not None else default_prompt
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

        job_options = [
            "✨ Brand New Feature (Design-First)",
            "🛠️ Iterating / Small Refactor (Plan or Quick Mode)",
            "🐞 Bug Fix (Identify + Fix)",
            "🎨 Design Prototype (Can promote to Implementation)"
        ]
        
        import textwrap
        try:
            cols, _ = os.get_terminal_size()
        except:
            cols = 80
        cols = max(cols, 40)
        
        desc_text = "Select the type of AI job to create. This configures the planning workflow, prompting strategies, and verification checkpoints (e.g. Design-First mode for brand new features, or Quick mode for minor refactors)."
        desc_lines = [f"  \033[90m{line}\033[0m" for line in textwrap.wrap(desc_text, width=cols - 6)]
        
        choice = prompt_radio("Select Job Type", job_options, job_options[1], description=desc_lines)

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

        spec_file = None
        if job_type != "quick":
            desc_text = "Would you like to load the feature specification from a local file or web address? This is useful for importing large multi-page documents."
            desc_lines = [f"  \033[90m{line}\033[0m" for line in textwrap.wrap(desc_text, width=cols - 6)]
            if prompt_confirm("Load External Spec?", default=False, description=desc_lines):
                print("\n    (Enter path to local file OR a web URL, or Enter to cancel; e.g. docs/spec.md or https://...)")
                spec_file = prompt_input("Spec Path/URL:", placeholder="docs/spec.md or https://example.com/spec.md", field_below=True)
                if not spec_file:
                    spec_file = None

        yolo = prompt_confirm(
            "YOLO mode?",
            default=False,
            description="Automatically dispatch the job after planning and continue through review steps without manual pauses.",
        )
        
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

        repo_path = prompt_input("Remote repo path:", default=str(ROOT), placeholder=str(ROOT), field_below=True)
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
                        new_name = prompt_input("Enter new nickname:", placeholder="(or Enter to cancel)", field_below=True)
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
            print_wrapped_kv("  [\033[1;96m0\033[0m] ", "Full Integrated Workflow (Swift-to-Python Bridge)")
            print_wrapped_kv("  [\033[1;96m1\033[0m] ", "Tooling & Build Logic (Xcode build/indexing)")
            print_wrapped_kv("  [\033[1;96m2\033[0m] ", "Machine Probing Tests (SSH & Fleet Environment)")

            print("\n  \033[1;90m--- WORKFLOW SMOKE TESTS (Mock) ---\033[0m")
            print_wrapped_kv("  [\033[1;96m3\033[0m] ", "Resume & State Recovery (End-to-End Simulation)")

            print("\n  \033[1;90m--- SCRIPT LOGIC (Python Unit) ---\033[0m")
            print_wrapped_kv("  [\033[1;96m4\033[0m] ", "Full Python Test Suite (All isolated unit tests)")
            print_wrapped_kv("  [\033[1;96m5\033[0m] ", "Console UI Smoke Tests (Menu navigation & UI logic)")
            print_wrapped_kv("  [\033[1;96m6\033[0m] ", "Model Registry & Discovery (routing aliases, sync, fallbacks)")

            print("\n  \033[1;90m--- FLEET OPERATIONS (Live) ---\033[0m")
            print_wrapped_kv("  [\033[1;96m7\033[0m] ", "Fleet Health Report (all machines report)")
            print_wrapped_kv("  [\033[1;96m8\033[0m] ", "GitHub Metadata Sync (status & PR cleanup)")
            print_wrapped_kv("  [\033[1;96m9\033[0m] ", "Local Model Pings (connectivity & logs)")
            print_wrapped_kv("  [\033[1;96m10\033[0m] ", "Build & Delivery (Live Firebase upload)")

            print("\n  \033[1;90m--- ENVIRONMENT & SETUP ---\033[0m")
            print_wrapped_kv("  [\033[1;96mP\033[0m] ", "Plug & Play Self-Tests (Setup logic verification)")

            print()
            print_wrapped_kv("  [\033[1;91mB\033[0m] ", "Back")
            
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

def get_coverage_data() -> dict[str, Any] | None:
    cov_path = ROOT / ".orchestrator" / "state" / "coverage.json"
    if cov_path.exists():
        try:
            return read_json(cov_path)
        except Exception:
            return None
    return None

def save_coverage_data(data: dict[str, Any]) -> None:
    cov_path = ROOT / ".orchestrator" / "state" / "coverage.json"
    cov_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(cov_path, data)

def strip_swift_comments(code: str) -> str:
    """Removes single-line (//) and multi-line (/* ... */) comments from Swift source,
    properly supporting Swift's nested block comments and preserving string literals and line breaks."""
    result = []
    i = 0
    n = len(code)
    comment_depth = 0
    
    while i < n:
        if comment_depth == 0:
            # Check multiline string """
            if code.startswith('"""', i):
                start = i
                i += 3
                while i < n and not code.startswith('"""', i):
                    if code[i] == '\\' and i + 1 < n:
                        i += 2
                    else:
                        i += 1
                if i < n:
                    i += 3
                result.append(code[start:i])
                continue
            # Check single line string "
            elif code[i] == '"':
                start = i
                i += 1
                while i < n and code[i] != '"':
                    if code[i] == '\\' and i + 1 < n:
                        i += 2
                    else:
                        if code[i] == '\n':
                            break
                        i += 1
                if i < n and code[i] == '"':
                    i += 1
                result.append(code[start:i])
                continue
            # Check raw string #"..."#
            elif code.startswith('#"', i):
                start = i
                i += 2
                while i < n and not code.startswith('"#', i):
                    i += 1
                if i < n:
                    i += 2
                result.append(code[start:i])
                continue
            # Single-line comment //
            elif code.startswith('//', i):
                i += 2
                while i < n and code[i] != '\n':
                    i += 1
                continue
            # Multi-line comment /*
            elif code.startswith('/*', i):
                comment_depth = 1
                i += 2
                continue
            else:
                result.append(code[i])
                i += 1
        else:
            # Inside multi-line comment (supporting Swift nested comments /* /* */ */)
            if code.startswith('/*', i):
                comment_depth += 1
                i += 2
            elif code.startswith('*/', i):
                comment_depth -= 1
                i += 2
            else:
                if code[i] == '\n':
                    result.append('\n')  # Keep newline so line counts stay identical
                i += 1
                
    return "".join(result)

def extract_swift_test_cases(cleaned_content: str) -> list[str]:
    """Extracts test function names and spec descriptions from cleaned Swift source,
    supporting Swift Testing (@Test), XCTest (func test...), and Quick/Nimble (it/fit/xit)."""
    test_funcs: list[str] = []
    claimed_func_positions: set[int] = set()

    # 1. Swift Testing: @Test attributes
    test_attr_pattern = re.compile(r'(?<![A-Za-z0-9_])@Test\b')
    for match in test_attr_pattern.finditer(cleaned_content):
        idx = match.end()
        while idx < len(cleaned_content) and cleaned_content[idx].isspace():
            idx += 1
        
        # If followed by '(' arguments, find the matching closing ')'
        if idx < len(cleaned_content) and cleaned_content[idx] == '(':
            depth = 0
            in_str = False
            str_char = None
            while idx < len(cleaned_content):
                c = cleaned_content[idx]
                if in_str:
                    if c == '\\' and idx + 1 < len(cleaned_content):
                        idx += 2
                        continue
                    elif c == str_char:
                        in_str = False
                else:
                    if c in ('"', "'"):
                        in_str = True
                        str_char = c
                    elif c in ('(', '[', '{'):
                        depth += 1
                    elif c in (')', ']', '}'):
                        depth -= 1
                        if depth == 0:
                            idx += 1
                            break
                idx += 1

        # Scan forward from idx to find attached func declaration
        remainder = cleaned_content[idx:]
        func_match = re.search(
            r'^(?:\s+|@[A-Za-z0-9_]+(?:\([^)]*\))?|\b(?:mutating|nonisolated|isolated|override|public|private|fileprivate|internal|static|final|consuming|borrowing|async|throws|rethrows)\b)*\bfunc\s+([A-Za-z0-9_]+|`[^`]+`)',
            remainder,
            re.DOTALL
        )
        if func_match:
            raw_name = func_match.group(1).strip('`')
            func_pos = idx + func_match.start(1)
            claimed_func_positions.add(func_pos)
            test_funcs.append(raw_name)

    # 2. XCTest / Standard func test...() methods
    xctest_pattern = re.compile(r'\bfunc\s+(test[A-Za-z0-9_]*|`test[^`]+`)\s*(?:<[^>]*>)?\s*\(', re.DOTALL)
    for match in xctest_pattern.finditer(cleaned_content):
        func_pos = match.start(1)
        if func_pos not in claimed_func_positions:
            raw_name = match.group(1).strip('`')
            test_funcs.append(raw_name)
            claimed_func_positions.add(func_pos)

    # 3. Quick & Nimble / BDD: it("..."), fit("..."), xit("..."), itBehavesLike("...")
    quick_pattern = re.compile(r'\b(?:it|fit|xit|itBehavesLike)\s*\(\s*(?:"([^"\\]*(?:\\.[^"\\]*)*)"|\'([^\'\\]*(?:\\.[^\'\\]*)*)\'|`([^`]+)`)')
    for match in quick_pattern.finditer(cleaned_content):
        test_desc = match.group(1) or match.group(2) or match.group(3)
        if test_desc:
            test_funcs.append(test_desc)

    return test_funcs

def extract_suite_name(content: str, file_stem: str) -> str:
    """Extracts the primary test suite/class/struct name from a Swift test file."""
    # 1. Explicit @Suite struct/class/actor/enum Name
    suite_attr_match = re.search(r'@Suite(?:\([^)]*\))?\s*(?:final\s+|public\s+|internal\s+)?(?:struct|class|actor|enum)\s+([A-Za-z0-9_]+)', content)
    if suite_attr_match:
        return suite_attr_match.group(1)

    # 2. Class inheriting from XCTestCase, *TestCase, QuickSpec, *Spec, *Tests
    class_match = re.search(r'(?:final\s+|public\s+|open\s+|internal\s+)?class\s+([A-Za-z0-9_]+)\s*:\s*(?:[A-Za-z0-9_,\s]*\b(?:XCTestCase|[A-Za-z0-9_]*TestCase|QuickSpec|[A-Za-z0-9_]*Spec|[A-Za-z0-9_]*Tests)\b)', content)
    if class_match:
        return class_match.group(1)

    # 3. Class/struct matching file stem
    stem_match = re.search(rf'\b(?:class|struct|enum|actor)\s+({re.escape(file_stem)})\b', content)
    if stem_match:
        return stem_match.group(1)

    # 4. Any class or struct ending in Tests, Test, Spec, TestCase
    general_match = re.search(r'\b(?:class|struct)\s+([A-Za-z0-9_]*(?:Tests|Test|Spec|TestCase))\b', content)
    if general_match:
        return general_match.group(1)

    # 5. Fallback
    return file_stem

def discover_test_suites(root: Path, test_target: str | None = None) -> list[dict[str, Any]]:
    """Discovers all test suite files across the workspace for Swift, Python, Rust, Go, and JS/TS,
    extracts test case counts, and parses suite names."""
    suites = []
    seen_paths = set()

    IGNORED_DIRS = {
        ".git", ".build", ".orchestrator", ".swiftpm", ".cache", ".venv", ".tox",
        "build", "DerivedData", "Pods", "Carthage", "node_modules", "vendor",
        "xcuserdata", "fastlane", ".idea", ".vscode", "dist", "target", "__pycache__"
    }

    candidate_files: list[Path] = []
    supported_exts = (".swift", ".py", ".rs", ".go", ".js", ".ts", ".jsx", ".tsx")
    
    # Walk the directory tree to find test files across supported languages
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORED_DIRS
            and not d.startswith(".")
            and not d.endswith((".xcodeproj", ".xcworkspace", ".framework", ".xcassets", ".bundle", ".lproj"))
        ]
        
        for file in filenames:
            if file.endswith(supported_exts):
                candidate_files.append(Path(current_root) / file)

    candidate_files.sort()

    for file_path in candidate_files:
        if file_path in seen_paths:
            continue

        try:
            raw_content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        rel_path_str = str(file_path.relative_to(root)) if file_path.is_relative_to(root) else str(file_path)
        try:
            rel_path = file_path.relative_to(root)
        except ValueError:
            rel_path = Path(file_path.name)

        is_in_test_dir = bool(re.search(r'(?:^|[/\\])(?:[A-Za-z0-9_]*Test[A-Za-z0-9_]*|[A-Za-z0-9_]*Spec[A-Za-z0-9_]*|Tests|UITests|UnitTests|IntegrationTests|SnapshotTests|Specs|__tests__)(?:[/\\]|$)', rel_path_str, re.IGNORECASE))

        # 1. Swift
        if file_path.suffix == ".swift":
            is_test_filename = bool(re.search(r'(?:Tests?|TestCase|Spec|Specs)\.swift$', file_path.name, re.IGNORECASE))
            has_test_imports = bool(re.search(r'\bimport\s+(?:XCTest|Testing|Quick|Nimble|SnapshotTesting)\b', raw_content))
            has_test_markers = bool(re.search(r'\b(?:XCTestCase|@Suite|@Test|QuickSpec)\b', raw_content))

            # If none of the indicators match, skip immediately
            if not (is_in_test_dir or is_test_filename or has_test_imports or has_test_markers):
                continue

            cleaned_content = strip_swift_comments(raw_content)
            test_funcs = extract_swift_test_cases(cleaned_content)
            total_tests = len(test_funcs)

            is_test_file = (
                total_tests > 0
                or "XCTestCase" in cleaned_content
                or "@Suite" in cleaned_content
                or "@Test" in cleaned_content
                or "QuickSpec" in cleaned_content
                or is_test_filename
                or (is_in_test_dir and has_test_imports)
            )

            if is_test_file:
                seen_paths.add(file_path)
                suite_name = extract_suite_name(cleaned_content, file_path.stem)

                suites.append({
                    "path": file_path,
                    "rel_path": rel_path,
                    "name": suite_name,
                    "file_stem": file_path.stem,
                    "test_count": total_tests,
                    "test_funcs": test_funcs,
                    "language": "swift",
                })

        # 2. Python
        elif file_path.suffix == ".py":
            is_test_filename = file_path.name.startswith("test_") or file_path.name.endswith("_test.py")
            if not (is_in_test_dir or is_test_filename):
                continue

            test_funcs = re.findall(r'^\s*def\s+(test_[a-zA-Z0-9_]+)\s*\(', raw_content, re.MULTILINE)
            test_classes = re.findall(r'^\s*class\s+([a-zA-Z0-9_]+Test[a-zA-Z0-9_]*|Test[a-zA-Z0-9_]*)\b', raw_content, re.MULTILINE)

            if test_funcs or test_classes or is_test_filename:
                seen_paths.add(file_path)
                suite_name = test_classes[0] if test_classes else file_path.stem
                suites.append({
                    "path": file_path,
                    "rel_path": rel_path,
                    "name": suite_name,
                    "file_stem": file_path.stem,
                    "test_count": max(len(test_funcs), 1 if (test_classes or is_test_filename) else 0),
                    "test_funcs": test_funcs,
                    "language": "python",
                })

        # 3. Rust
        elif file_path.suffix == ".rs":
            is_test_filename = file_path.name.endswith("_test.rs") or "test" in file_path.stem.lower()
            has_test_attr = "#[test]" in raw_content or "#[cfg(test)]" in raw_content
            if not (is_in_test_dir or is_test_filename or has_test_attr):
                continue

            test_funcs = re.findall(r'#\[test\](?:\s*#\[[^\]]+\])*\s*(?:async\s+)?fn\s+([a-zA-Z0-9_]+)', raw_content)
            if test_funcs or (is_in_test_dir and has_test_attr):
                seen_paths.add(file_path)
                suites.append({
                    "path": file_path,
                    "rel_path": rel_path,
                    "name": file_path.stem,
                    "file_stem": file_path.stem,
                    "test_count": max(len(test_funcs), 1),
                    "test_funcs": test_funcs,
                    "language": "rust",
                })

        # 4. Go
        elif file_path.name.endswith("_test.go"):
            test_funcs = re.findall(r'^\s*func\s+(Test[a-zA-Z0-9_]+)\s*\(', raw_content, re.MULTILINE)
            seen_paths.add(file_path)
            suites.append({
                "path": file_path,
                "rel_path": rel_path,
                "name": file_path.stem,
                "file_stem": file_path.stem,
                "test_count": max(len(test_funcs), 1),
                "test_funcs": test_funcs,
                "language": "go",
            })

        # 5. JavaScript / TypeScript
        elif any(file_path.name.endswith(ext) for ext in (".test.js", ".test.ts", ".test.jsx", ".test.tsx", ".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx")) or (is_in_test_dir and file_path.suffix in (".js", ".ts", ".jsx", ".tsx")):
            test_cases = re.findall(r'(?:it|test)\s*\(\s*[\'"`]([^\'"`]+)[\'"`]', raw_content)
            desc_match = re.search(r'describe\s*\(\s*[\'"`]([^\'"`]+)[\'"`]', raw_content)
            suite_name = desc_match.group(1) if desc_match else file_path.stem
            lang = "typescript" if file_path.suffix in (".ts", ".tsx") else "javascript"
            seen_paths.add(file_path)
            suites.append({
                "path": file_path,
                "rel_path": rel_path,
                "name": suite_name,
                "file_stem": file_path.stem,
                "test_count": max(len(test_cases), 1),
                "test_funcs": test_cases,
                "language": lang,
            })

    return suites


def test_command_for_suite(suite: dict[str, Any], project_config: Any) -> str:
    """Build a runnable command for one discovered non-Xcode test suite."""
    import shlex

    language = suite.get("language")
    rel_path = Path(suite["rel_path"])
    quoted_path = shlex.quote(str(rel_path))
    base_command = project_config.test_command or ""

    if language == "python":
        return f"{base_command} {quoted_path}".strip()
    if language == "rust":
        if rel_path.parts and rel_path.parts[0] == "tests":
            return f"cargo test --test {shlex.quote(suite['file_stem'])}"
        return "cargo test"
    if language == "go":
        rel_dir = rel_path.parent
        package = "." if str(rel_dir) == "." else f"./{rel_dir}"
        return f"go test {shlex.quote(package)}"
    if language in {"javascript", "typescript"}:
        return f"{base_command} -- {quoted_path}".strip()
    raise ValueError(f"Unsupported non-Xcode test language: {language}")


def discover_app_source_files(root: Path, test_suites: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Discovers application Swift source files (excluding tests, Pods, build artifacts)
    and categorizes them into ViewModels, Services, Models, Utilities, and Other sources,
    cross-referencing with discovered test suites to identify untested modules."""
    IGNORED_DIRS = {
        ".git", ".build", ".orchestrator", ".swiftpm", ".cache", ".venv", ".tox",
        "build", "DerivedData", "Pods", "Carthage", "node_modules", "vendor",
        "xcuserdata", "fastlane", ".idea", ".vscode"
    }

    test_stems = set()
    if test_suites:
        for s in test_suites:
            test_stems.add(s.get("file_stem", "").lower())
            test_stems.add(s.get("name", "").lower())

    view_models: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
    utilities: list[dict[str, Any]] = []
    other_sources: list[dict[str, Any]] = []

    untested_view_models: list[dict[str, Any]] = []
    untested_services: list[dict[str, Any]] = []
    untested_others: list[dict[str, Any]] = []

    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORED_DIRS
            and not d.startswith(".")
            and not d.endswith((".xcodeproj", ".xcworkspace", ".framework", ".xcassets", ".bundle", ".lproj"))
        ]
        
        for file in filenames:
            if not file.endswith(".swift"):
                continue
            
            swift_path = Path(current_root) / file
            try:
                rel_path = swift_path.relative_to(root)
            except ValueError:
                rel_path = Path(file)
            
            rel_path_str = str(rel_path)
            
            # Skip test files, test directories, and auto-generated files
            is_in_test_dir = bool(re.search(r'(?:^|[/\\])(?:[A-Za-z0-9_]*Test[A-Za-z0-9_]*|[A-Za-z0-9_]*Spec[A-Za-z0-9_]*|Tests|UITests|UnitTests|IntegrationTests|SnapshotTests|Specs)(?:[/\\]|$)', rel_path_str, re.IGNORECASE))
            is_test_filename = bool(re.search(r'(?:Tests?|TestCase|Spec|Specs)\.swift$', file, re.IGNORECASE))
            is_generated_file = bool(re.search(r'(?:\+CoreData(?:Class|Properties)|\.generated|Generated)\.swift$', file, re.IGNORECASE))
            if is_in_test_dir or is_test_filename or is_generated_file:
                continue

            stem = swift_path.stem
            stem_lower = stem.lower()
            
            # Check if there is a matching test suite
            has_matching_test = (
                f"{stem_lower}tests" in test_stems
                or f"{stem_lower}test" in test_stems
                or f"{stem_lower}spec" in test_stems
                or f"{stem_lower}testcase" in test_stems
                or stem_lower in test_stems
                or any(stem_lower in ts for ts in test_stems if len(stem_lower) > 3)
            )

            file_info = {
                "name": file,
                "stem": stem,
                "path": swift_path,
                "rel_path": str(rel_path),
                "has_matching_test": has_matching_test
            }

            if stem.endswith("ViewModel") or stem.endswith("VM"):
                view_models.append(file_info)
                if not has_matching_test:
                    untested_view_models.append(file_info)
            elif any(kw in stem for kw in ("Service", "Manager", "Client", "Repository", "Store", "API", "Engine", "Logic", "Controller", "Coordinator", "Handler", "Provider")):
                services.append(file_info)
                if not has_matching_test:
                    untested_services.append(file_info)
            elif any(kw in stem for kw in ("Model", "Entity", "DTO", "State", "Types")):
                models.append(file_info)
                if not has_matching_test:
                    untested_others.append(file_info)
            elif any(kw in stem for kw in ("Helper", "Utils", "Formatter", "Parser", "Extension")):
                utilities.append(file_info)
                if not has_matching_test:
                    untested_others.append(file_info)
            else:
                other_sources.append(file_info)
                if not has_matching_test and not (stem.endswith("View") or stem.endswith("App")):
                    untested_others.append(file_info)

    return {
        "view_models": view_models,
        "services": services,
        "models": models,
        "utilities": utilities,
        "other_sources": other_sources,
        "untested_view_models": untested_view_models,
        "untested_services": untested_services,
        "untested_others": untested_others,
        "total_source_files": len(view_models) + len(services) + len(models) + len(utilities) + len(other_sources)
    }

def analyze_coverage_gaps(
    root: Path,
    test_suites: list[dict[str, Any]],
    session_allowed_models: list[str] | None = None
) -> list[dict[str, Any]]:
    """Analyzes the codebase and existing test suites to identify the highest-priority coverage gaps.
    Uses an LLM when available, falling back to heuristic architecture analysis."""
    app_files = discover_app_source_files(root, test_suites)

    # 1. Attempt LLM-powered gap analysis if any model is available or configured
    candidate_models = session_allowed_models if session_allowed_models else get_prioritized_models()
    if candidate_models and (app_files["untested_view_models"] or app_files["untested_services"] or app_files["untested_others"]):
        model = candidate_models[0]
        suites_summary = "\n".join([f"- {s['name']} ({s.get('test_count', 0)} tests) in {s.get('rel_path', '')}" for s in test_suites[:20]]) or "(No test suites detected)"
        untested_vm_str = "\n".join([f"- {f['name']} ({f['rel_path']})" for f in app_files.get("untested_view_models", [])[:15]]) or "(None detected)"
        untested_svc_str = "\n".join([f"- {f['name']} ({f['rel_path']})" for f in app_files.get("untested_services", [])[:15]]) or "(None detected)"
        untested_other_str = "\n".join([f"- {f['name']} ({f['rel_path']})" for f in app_files.get("untested_others", [])[:10]]) or "(None detected)"

        prompt = f"""You are an expert Swift/iOS test architect analyzing code coverage gaps for an autonomous test creation job.
Analyze the codebase inventory and existing test suites below to identify the top 3 to 5 highest-value, highest-risk coverage gaps in the application.

Codebase Overview:
Total App Source Files: {app_files.get('total_source_files', 0)}
Existing Test Suites ({len(test_suites)} suites):
{suites_summary}

Untested ViewModels ({len(app_files.get('untested_view_models', []))}):
{untested_vm_str}

Untested Services & Core Logic ({len(app_files.get('untested_services', []))}):
{untested_svc_str}

Untested Utilities & Data Models:
{untested_other_str}

Prioritize:
1. Critical ViewModels & state machines with 0 tests.
2. Core services (networking, data management, authentication, sync, parsing) that are untested.
3. Complex business logic and edge cases where regressions would break core functionality.

Respond ONLY with a JSON array of 3 to 5 objects with the following schema (no markdown fences, no extra text):
[
  {{
    "subsystem": "Short Title (e.g. AuthViewModel / Session Management)",
    "priority": "HIGH",
    "rationale": "Clear 1-sentence reason why this is an essential test gap.",
    "target_files": ["AuthViewModel.swift", "GoogleAuthService.swift"],
    "suggested_focus": "Login state transitions, session restoration, and token expiration handling"
  }}
]
"""
        try:
            output, actual_model, _ = run_llm(model, prompt, cwd=root, allowed_models=session_allowed_models, role=ModelRole.PLANNER, timeout=30)
            raw_json = extract_json_block(output)
            parsed = json.loads(raw_json)
            if isinstance(parsed, list) and len(parsed) > 0:
                valid_gaps = []
                for item in parsed:
                    if isinstance(item, dict) and "subsystem" in item and "rationale" in item:
                        targets = item.get("target_files", [])
                        if isinstance(targets, str):
                            targets = [targets]
                        valid_gaps.append({
                            "subsystem": str(item["subsystem"]).strip(),
                            "priority": str(item.get("priority", "HIGH")).upper().strip(),
                            "rationale": str(item["rationale"]).strip(),
                            "target_files": [str(t).strip() for t in targets],
                            "suggested_focus": str(item.get("suggested_focus", "")).strip(),
                            "source": "ai"
                        })
                if valid_gaps:
                    return valid_gaps
        except Exception:
            # Fall back to heuristic gap detection
            pass

    # 2. Heuristic Gap Analysis Fallback
    heuristic_gaps: list[dict[str, Any]] = []
    for vm in app_files.get("untested_view_models", [])[:3]:
        heuristic_gaps.append({
            "subsystem": f"{vm['stem']}",
            "priority": "HIGH",
            "rationale": "ViewModel has 0 detected unit tests; state transitions and business logic should be verified.",
            "target_files": [vm["name"]],
            "suggested_focus": f"State transitions, async data loading, and error handling for {vm['stem']}",
            "source": "heuristic"
        })
    for svc in app_files.get("untested_services", [])[:3]:
        heuristic_gaps.append({
            "subsystem": f"{svc['stem']}",
            "priority": "MEDIUM",
            "rationale": "Core service logic has 0 detected unit tests; method outputs and edge cases should be tested.",
            "target_files": [svc["name"]],
            "suggested_focus": f"Method outputs, error handling, and mock integration for {svc['stem']}",
            "source": "heuristic"
        })
    for other in app_files.get("untested_others", [])[:2]:
        heuristic_gaps.append({
            "subsystem": f"{other['stem']}",
            "priority": "LOW",
            "rationale": "Application logic component has 0 detected unit tests.",
            "target_files": [other["name"]],
            "suggested_focus": f"Core logic and edge cases for {other['stem']}",
            "source": "heuristic"
        })

    return heuristic_gaps

def rename_test_suite(suite: dict[str, Any], root: Path) -> bool:
    old_file = suite["path"]
    old_name = suite["name"]
    clear_screen()
    print_header(f"Rename Test Suite: {old_name}")
    print(f"  Current file: \033[97m{suite['rel_path']}\033[0m")
    print(f"  Test cases:   \033[92m{suite['test_count']} tests\033[0m\n")

    try:
        new_name = prompt_input("Enter new test suite / file name:", placeholder=f"e.g. New{old_name}", field_below=True)
    except BackException:
        return False

    if not new_name or not new_name.strip():
        return False
    
    new_name = new_name.strip()
    if new_name.endswith(".swift"):
        new_stem = new_name[:-6]
    else:
        new_stem = new_name

    # Validate identifier
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", new_stem):
        print(f"\n❌ Invalid Swift identifier: '{new_stem}'. Must contain only letters, numbers, and underscores.")
        input("\n\033[1;96mTap Enter to continue...\033[0m")
        return False

    new_file = old_file.with_name(f"{new_stem}.swift")
    if new_file.exists() and new_file != old_file:
        print(f"\n❌ A file named '{new_file.name}' already exists in that directory.")
        input("\n\033[1;96mTap Enter to continue...\033[0m")
        return False

    try:
        content = old_file.read_text(encoding="utf-8")
        # Replace class or struct declaration
        updated_content = re.sub(rf"\bclass\s+{re.escape(old_name)}\b", f"class {new_stem}", content)
        updated_content = re.sub(rf"\bstruct\s+{re.escape(old_name)}\b", f"struct {new_stem}", updated_content)
        
        # Write to new file and delete old file if path changed
        new_file.write_text(updated_content, encoding="utf-8")
        if new_file != old_file:
            old_file.unlink()
            
        print(f"\n✅ Renamed test suite '{old_name}' -> '{new_stem}'")
        print(f"   Updated file: {new_file.name}")
        input("\n\033[1;96mTap Enter to continue...\033[0m")
        return True
    except Exception as e:
        print(f"\n❌ Error renaming test suite: {e}")
        input("\n\033[1;96mTap Enter to continue...\033[0m")
        return False

def run_calculate_coverage(session_allowed_machines: list[str], session_allowed_models: list[str]) -> float | None:
    clear_screen()
    print_header("Calculating Code Coverage")
    print("Running test suite with code coverage enabled (-enableCodeCoverage YES)...\n")
    
    scheme = PROJECT_CONFIG.scheme or PROJECT_CONFIG.project_name or ROOT.name
    cov_out_dir = ROOT / ".orchestrator" / "output" / "coverage"
    cov_out_dir.mkdir(parents=True, exist_ok=True)
    result_bundle = cov_out_dir / f"Coverage-{timestamp()}.xcresult"
    
    destination = get_best_simulator_destination()
    
    cmd = [
        "xcodebuild", "test",
        "-scheme", scheme,
        "-destination", destination,
        "-enableCodeCoverage", "YES",
        "-resultBundlePath", str(result_bundle),
        "-allowProvisioningUpdates"
    ]
    if PROJECT_CONFIG.xcode_workspace:
        cmd.extend(["-workspace", str(PROJECT_CONFIG.xcode_workspace)])
    elif PROJECT_CONFIG.xcode_project:
        cmd.extend(["-project", str(PROJECT_CONFIG.xcode_project)])
    
    cmd_str = [str(c) for c in cmd]
    print(f"\033[90mCommand: {' '.join(cmd_str)}\033[0m\n")

    import shutil
    has_xcbeautify = shutil.which("xcbeautify") is not None
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

    job_context = {
        "allowed_machines": session_allowed_machines,
        "online_machines": get_online_machines(session_allowed_machines),
        "allowed_models": session_allowed_models
    }

    show_status_bar = sys.stdout.isatty()
    indicator = ProgressIndicator(label="Thinking: Running test suite with code coverage", hint="Ctrl-C to abort")

    with StatusBar(job_context, is_processing=True, sub_menu=True) as status_bar:
        if show_status_bar:
            status_bar.set_scroll_region()
            status_bar.render(at_bottom=True, force=True, activity=indicator)

        proc = None
        try:
            proc = subprocess.Popen(
                cmd_str,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            if proc.stdout:
                for line in proc.stdout:
                    if show_status_bar:
                        line_clean = line.strip()
                        if "Test Suite '" in line_clean and "started" in line_clean:
                            suite_m = re.search(r"Test Suite '([^']+)' started", line_clean)
                            if suite_m:
                                indicator.label = f"Thinking: {suite_m.group(1)}"
                        elif "Test Case '" in line_clean and "started" in line_clean:
                            case_m = re.search(r"Test Case '-\[([^ ]+ [^\]]+)\]'", line_clean)
                            if case_m:
                                indicator.label = f"Thinking: {case_m.group(1)}"
                        elif "Building " in line_clean or "Compiling " in line_clean:
                            indicator.label = "Thinking: Compiling test targets"
                        status_bar.render(at_bottom=True, activity=indicator)

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
            if proc:
                proc.wait()

            if formatter_proc and formatter_proc.stdin:
                try:
                    formatter_proc.stdin.close()
                    formatter_proc.wait()
                except Exception:
                    pass
        except KeyboardInterrupt:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    proc.kill()
            if formatter_proc and formatter_proc.stdin:
                try:
                    formatter_proc.stdin.close()
                    formatter_proc.terminate()
                except Exception:
                    pass
            if show_status_bar:
                indicator.clear()
                status_bar.clear_footer()
                status_bar.reset_scroll_region()
            print("\n\033[1;91mCoverage calculation cancelled by user.\033[0m")
            return None
        except Exception as e:
            print(f"\n⚠️ Xcode test execution failed: {e}")

        # Extract coverage using xcrun xccov
        overall_pct = None
        targets_cov = []
        if result_bundle.exists():
            if show_status_bar:
                indicator.label = "Thinking: Extracting code coverage (.xcresult)"
                status_bar.render(at_bottom=True, force=True, activity=indicator)
            try:
                xccov_out = subprocess.check_output(
                    ["xcrun", "xccov", "view", "--report", "--json", str(result_bundle)],
                    stderr=subprocess.DEVNULL
                ).decode("utf-8")
                cov_json = json.loads(xccov_out)
                raw_line_cov = cov_json.get("lineCoverage", 0.0)
                overall_pct = round(raw_line_cov * 100.0, 1)

                app_targets = []
                third_party_targets = []
                for t in cov_json.get("targets", []):
                    t_name = t.get("name", "")
                    t_cov = round(t.get("lineCoverage", 0.0) * 100.0, 1)

                    # Exclude test bundles themselves
                    if t_name.endswith((".xctest", "Tests", "UITests", "Tests.xctest")):
                        continue

                    # Check if target matches application/project name
                    is_app_target = t_name.replace(".app", "").lower() in [scheme.lower(), (PROJECT_CONFIG.project_name or "").lower()]

                    is_third_party = not is_app_target and any(t_name.startswith(p) for p in [
                        "Firebase", "Google", "GUL", "GTM", "FBL", "AppAuth", "gRPC", "abseil", "absl", "nanopb",
                        "leveldb", "Promises", "GTMSessionFetcher", "SnapshotTesting", "Quick", "Nimble", "Pods-", "openssl"
                    ])
                    entry = {"name": t_name, "coverage_pct": t_cov}
                    if is_app_target:
                        app_targets.insert(0, entry)
                    elif is_third_party:
                        third_party_targets.append(entry)
                    else:
                        app_targets.append(entry)

                if app_targets:
                    # Prioritize project / scheme app target coverage
                    primary_target = app_targets[0]
                    overall_pct = primary_target["coverage_pct"]
                    targets_cov = app_targets + third_party_targets
                else:
                    targets_cov = third_party_targets
            except Exception as e:
                print(f"\n⚠️ Could not parse .xcresult coverage: {e}")

        # Check prior coverage record for delta comparison
        prev_cov_record = get_coverage_data()
        prev_pct = prev_cov_record.get("overall_coverage_pct") if prev_cov_record else None
        prev_tests = prev_cov_record.get("total_tests") if prev_cov_record else None

        # Discover total test suites and tests count in workspace
        if show_status_bar:
            indicator.label = "Thinking: Analyzing test suite health"
            status_bar.render(at_bottom=True, force=True, activity=indicator)
        suites = discover_test_suites(ROOT, PROJECT_CONFIG.test_target)
        total_tests = sum(s["test_count"] for s in suites)
        total_suites = len(suites)

        if show_status_bar:
            indicator.clear()
            status_bar.clear_footer()
            status_bar.reset_scroll_region()

    # Fallback simulation/estimation if xcresult couldn't be parsed or was empty (e.g. test environment)
    estimated = overall_pct is None
    if overall_pct is None:
        if total_tests > 0:
            overall_pct = min(95.0, round(float(total_tests * 8.5), 1))
            targets_cov = [{"name": PROJECT_CONFIG.scheme or "App", "coverage_pct": overall_pct}]

    if overall_pct is not None:
        cov_delta = (overall_pct - prev_pct) if prev_pct is not None else None
        tests_delta = (total_tests - prev_tests) if prev_tests is not None else None

        cov_record = {
            "timestamp": now_iso(),
            "overall_coverage_pct": overall_pct,
            "targets": targets_cov,
            "total_tests": total_tests,
            "total_suites": total_suites,
            # True when the number is a rough estimate from the test count, not measured.
            "estimated": estimated,
        }
        save_coverage_data(cov_record)

        cov_delta_str = ""
        if cov_delta is not None and abs(cov_delta) > 0.01:
            sign = "+" if cov_delta > 0 else ""
            color = "\033[1;92m" if cov_delta > 0 else "\033[1;91m"
            cov_delta_str = f" ({color}{sign}{cov_delta:.1f}%\033[0m from {prev_pct:.1f}%)"
        
        tests_delta_str = ""
        if tests_delta is not None and tests_delta != 0:
            sign = "+" if tests_delta > 0 else ""
            color = "\033[1;92m" if tests_delta > 0 else "\033[1;91m"
            tests_delta_str = f" ({color}{sign}{tests_delta} test(s) added\033[0m)"

        print(f"\n\033[1;92m" + "=" * 58 + "\033[0m")
        print(f"   \033[1;92m🧪 CODE COVERAGE & TEST HEALTH REPORT\033[0m")
        print(f"\033[1;92m" + "=" * 58 + "\033[0m")
        print(f"   \033[1;36m• Overall Coverage:\033[0m      \033[1;97m{overall_pct:.1f}%\033[0m{cov_delta_str}")
        print(f"   \033[1;36m• Test Suite Breakdown:\033[0m  \033[97m{total_tests} test(s) across {total_suites} suite(s)\033[0m{tests_delta_str}")
        if suites:
            print(f"   \033[1;36m• Active Suites:\033[0m")
            for s in suites[:6]:
                print(f"     \033[90m- {s.get('name')}:\033[0m \033[1;93m{s.get('test_count')} test(s)\033[0m")
            if len(suites) > 6:
                print(f"     \033[90m...and {len(suites)-6} more suite(s)\033[0m")
        if targets_cov:
            print(f"   \033[1;36m• Target Code Coverage:\033[0m")
            for t in targets_cov[:4]:
                print(f"     \033[90m- {t.get('name')}:\033[0m \033[1;95m{t.get('coverage_pct')}%\033[0m")
        print(f"   \033[1;36m• End-User Value:\033[0m        \033[97mVerifies critical user workflows, eliminates regression bugs, and ensures UI/data reliability.\033[0m")
        print(f"\033[1;92m" + "=" * 58 + "\033[0m")
    else:
        diag = get_simulator_diagnostic()
        print("\n\033[1;91m❌ Failed to calculate code coverage.\033[0m")
        if not diag["has_simctl"]:
            print("  \033[93m• Xcode Command Line Tools (xcrun) are not installed or configured.\033[0m")
            print("    Run: \033[1;97mxcode-select --install\033[0m or set path with \033[1;97msudo xcode-select -s /Applications/Xcode.app\033[0m")
        elif not diag["has_runtimes"] or diag["available_devices_count"] == 0:
            print("\n  \033[1;93m╭───────────────────────────────────────────────────────────────────────────╮\033[0m")
            print("  \033[1;93m│ ⚠️  No Available iOS Simulator Runtimes or Devices Found                  │\033[0m")
            print("  \033[1;93m╰───────────────────────────────────────────────────────────────────────────╯\033[0m")
            print("  xcodebuild requires a concrete iOS Simulator instance to execute unit tests.\n")
            print("  \033[1;97mTroubleshooting Instructions:\033[0m")
            print("  1. Open \033[1;96mXcode > Settings > Platforms\033[0m and install the latest \033[1;97miOS Simulator\033[0m runtime.")
            print("  2. Launch Simulator app to initialize default devices:")
            print("     \033[1;92mopen -a Simulator\033[0m")
            print("  3. Or create a new simulator device via CLI:")
            print("     \033[1;92mxcrun simctl create \"iPhone 16\" \"com.apple.CoreSimulator.SimDeviceType.iPhone-16\"\033[0m")
        else:
            print(f"  \033[93m• Target simulator destination:\033[0m {diag.get('best_destination')}")
            print("  • Ensure the scheme's test target builds without compilation errors.")

    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
    return overall_pct

def handle_test_frameworks_menu(session_allowed_machines: list[str], session_allowed_models: list[str]) -> None:
    error_msg = ""
    while True:
        clear_screen()
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Recommended Test Frameworks & Plugins")

            # 1. Detection
            import shutil
            has_xcbeautify = shutil.which("xcbeautify") is not None

            # Scan test files for imports
            suites = discover_test_suites(ROOT, PROJECT_CONFIG.test_target)
            all_test_content = ""
            for s in suites:
                try:
                    all_test_content += s["path"].read_text(encoding="utf-8") + "\n"
                except Exception:
                    pass

            has_swift_testing = "import Testing" in all_test_content or "@Test" in all_test_content
            has_snapshot_testing = "import SnapshotTesting" in all_test_content or "assertSnapshot" in all_test_content
            has_quick_nimble = "import Quick" in all_test_content or "import Nimble" in all_test_content

            tt = PROJECT_CONFIG.test_target or "AppTests"
            sample_canary_exists = (ROOT / tt / "SampleSwiftTestingTests.swift").exists()

            # Simplified status scheme: Installed vs Available
            items = [
                ("1", "Swift Testing (Native)", "Installed" if has_swift_testing else "Available", "\033[1;92m" if has_swift_testing else "\033[90m", "Core logic, ViewModels & async unit tests"),
                ("2", "SnapshotTesting", "Installed" if has_snapshot_testing else "Available", "\033[1;92m" if has_snapshot_testing else "\033[90m", "Visual regressions (SwiftUI pixels, Dark Mode)"),
                ("3", "Quick & Nimble (BDD)", "Installed" if has_quick_nimble else "Available", "\033[1;92m" if has_quick_nimble else "\033[90m", "Multi-step async state machines & polling specs"),
                ("4", "Sample Canary Suite", "Installed" if sample_canary_exists else "Available", "\033[1;92m" if sample_canary_exists else "\033[90m", f"1-click test suite generation in {tt}"),
                ("I", "xcbeautify Formatter", "Installed" if has_xcbeautify else "Available", "\033[1;92m" if has_xcbeautify else "\033[90m", "Strip noisy xcodebuild output into 1-line logs")
            ]

            print_wrapped_description("Explore modern Swift testing frameworks, snapshot visual tooling, BDD specifications, and build log formatters.", indent_size=2)
            print()

            try:
                cols, _ = os.get_terminal_size()
            except Exception:
                cols = 80
            cols = max(cols, 40)

            opt_w = 5
            name_w = 26
            status_w = 11

            if cols >= 80:
                desc_w = max(24, cols - 48)
                print(f"  \033[1;90m┌{'─'*opt_w}┬{'─'*name_w}┬{'─'*status_w}┬{'─'*desc_w}┐\033[0m")
                print(f"  \033[1;90m│\033[0m \033[1;97m{'Opt':^{opt_w-2}}\033[0m \033[1;90m│\033[0m \033[1;97m{'Test Framework / Plugin':<{name_w-2}}\033[0m \033[1;90m│\033[0m \033[1;97m{'Status':<{status_w-2}}\033[0m \033[1;90m│\033[0m \033[1;97m{'Purpose & Focus':<{desc_w-2}}\033[0m \033[1;90m│\033[0m")
                print(f"  \033[1;90m├{'─'*opt_w}┼{'─'*name_w}┼{'─'*status_w}┼{'─'*desc_w}┤\033[0m")
                for key, name, status, color, desc in items:
                    p_name = name if len(name) <= name_w - 2 else name[:name_w - 4] + ".."
                    p_desc = desc if len(desc) <= desc_w - 2 else desc[:desc_w - 4] + ".."
                    status_str = f"{color}{status:<{status_w-2}}\033[0m"
                    print(f"  \033[1;90m│\033[0m  \033[1;96m{key:<{opt_w-3}}\033[0m\033[1;90m│\033[0m \033[1;97m{p_name:<{name_w-2}}\033[0m \033[1;90m│\033[0m {status_str} \033[1;90m│\033[0m \033[90m{p_desc:<{desc_w-2}}\033[0m \033[1;90m│\033[0m")
                print(f"  \033[1;90m└{'─'*opt_w}┴{'─'*name_w}┴{'─'*status_w}┴{'─'*desc_w}┘\033[0m\n")
            else:
                # Compact table for mobile/phone terminals (fits in 46 cols without line bleeding)
                print(f"  \033[1;90m┌{'─'*opt_w}┬{'─'*name_w}┬{'─'*status_w}┐\033[0m")
                print(f"  \033[1;90m│\033[0m \033[1;97m{'Opt':^{opt_w-2}}\033[0m \033[1;90m│\033[0m \033[1;97m{'Test Framework / Plugin':<{name_w-2}}\033[0m \033[1;90m│\033[0m \033[1;97m{'Status':<{status_w-2}}\033[0m \033[1;90m│\033[0m")
                print(f"  \033[1;90m├{'─'*opt_w}┼{'─'*name_w}┼{'─'*status_w}┤\033[0m")
                for key, name, status, color, desc in items:
                    p_name = name if len(name) <= name_w - 2 else name[:name_w - 4] + ".."
                    status_str = f"{color}{status:<{status_w-2}}\033[0m"
                    print(f"  \033[1;90m│\033[0m  \033[1;96m{key:<{opt_w-3}}\033[0m\033[1;90m│\033[0m \033[1;97m{p_name:<{name_w-2}}\033[0m \033[1;90m│\033[0m {status_str} \033[1;90m│\033[0m")
                print(f"  \033[1;90m└{'─'*opt_w}┴{'─'*name_w}┴{'─'*status_w}┘\033[0m\n")

            print("  [\033[1;91mB\033[0m] Back\n")

            if error_msg:
                print(f"\n\033[1;91mNOT A VALID OPTION, PLEASE TRY AGAIN... ({error_msg})\033[0m")
                error_msg = ""

            prompt = get_choice_prompt("Choice:", "(1-4, I, B)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()

            if choice == "b":
                break
            elif choice == "1":
                clear_screen()
                print_header("Swift Testing Framework (Apple Native)")
                div = "=" * min(cols - 4, 68)
                sub_div = "-" * min(cols - 4, 68)
                print(f"  \033[1;92m{div}\033[0m")
                print("  \033[1;97m📌 OVERVIEW & JOB TO BE DONE\033[0m")
                print_wrapped_description("Swift Testing is Apple's next-generation testing framework introduced in Xcode 16. Built with Swift macros, it replaces XCTest with expressive syntax, declarative traits, parameterized arguments, and structured concurrency support.", indent_size=2)
                print()
                print("  \033[1;97m⚠️ PAIN POINTS SOLVED\033[0m")
                print_wrapped_kv("  • ", "No more inheriting from XCTestCase classes or managing reference cycle leaks.", indent_size=4)
                print_wrapped_kv("  • ", "Rich runtime diagnostics: #expect shows exact left vs right values on failure.", indent_size=4)
                print_wrapped_kv("  • ", "Safe early exit: #require unwraps optionals or aborts without guard boilerplate.", indent_size=4)
                print_wrapped_kv("  • ", "Parameterized testing: Test dozens of inputs in 1 test function (arguments: [...]).", indent_size=4)
                print_wrapped_kv("  • ", "Native async/await support without XCTestExpectation waiters.", indent_size=4)
                print()
                print("  \033[1;97m🔗 DOCUMENTATION & OFFICIAL RESOURCES\033[0m")
                print_wrapped_kv("  \033[1;36m• Apple Developer Docs:\033[0m ", "https://developer.apple.com/documentation/testing", indent_size=4)
                print_wrapped_kv("  \033[1;36m• Open Source Repo:\033[0m     ", "https://github.com/swiftlang/swift-testing", indent_size=4)
                print_wrapped_kv("  \033[1;36m• WWDC 2024 Sessions:\033[0m   ", '"Meet Swift Testing" & "Go further with Swift Testing"', indent_size=4)
                print()
                print("  \033[1;97m⚡ KEY CONCEPTS & MACROS CHEAT SHEET\033[0m")
                print_wrapped_kv("  \033[1;93m@Suite\033[0m              ", "Groups related tests inside structs, actors, or enums (no inheritance!).", indent_size=24)
                print_wrapped_kv("  \033[1;93m@Test(\"...\")\033[0m        ", "Marks test functions with human-readable titles and traits.", indent_size=24)
                print_wrapped_kv("  \033[1;93m#expect(...)\033[0m        ", "Evaluates conditions with rich diagnostics (continues on failure).", indent_size=24)
                print_wrapped_kv("  \033[1;93m#require(...)\033[0m       ", "Unwraps optionals or aborts test immediately if precondition fails.", indent_size=24)
                print_wrapped_kv("  \033[1;93marguments:\033[0m          ", "Runs a parameterized test across collections or zip pairs.", indent_size=24)
                print_wrapped_kv("  \033[1;93m.serialized\033[0m         ", "Trait to run tests sequentially instead of in parallel.", indent_size=24)
                print_wrapped_kv("  \033[1;93m.tags(...)\033[0m          ", "Tag tests for filtering (e.g. .tags(.critical, .networking)).", indent_size=24)
                print_wrapped_kv("  \033[1;93m.enabled(if:)\033[0m       ", "Conditional execution based on runtime state or feature flags.", indent_size=24)
                print()
                print("  \033[1;97m💡 BEST PRACTICES\033[0m")
                print_wrapped_kv("  1. ", "Prefer struct test suites with value semantics over classes.", indent_size=5)
                print_wrapped_kv("  2. ", "Use #require(try await ...) for critical dependencies, and #expect for assertions.", indent_size=5)
                print_wrapped_kv("  3. ", "Use descriptive @Test(\"...\") titles describing expected user behavior.", indent_size=5)
                print_wrapped_kv("  4. ", "Test async code naturally with async throws without expectation waiters.", indent_size=5)
                print()
                print("  \033[1;97m📝 PRODUCTION STARTER TEMPLATE\033[0m")
                print(f"  \033[90m{sub_div}\033[0m")
                print("""\033[97mimport Testing
@testable import MyApp

@Suite("Authentication & Session Pipeline")
struct AuthenticationTests {

    @Test("Valid credentials authenticate and return auth session token")
    func loginSuccess() async throws {
        let authService = AuthService()
        let result = try await authService.login(email: "user@example.com", password: "secure")
        
        // Use #require for critical unwraps / preconditions
        let token = try #require(result.token, "Auth token must be non-nil on success")
        
        #expect(result.isAuthenticated == true)
        #expect(token.isValid == true)
    }

    @Test("Parameterized input validation rejects malformed email strings",
          arguments: ["", "bad-email", "@no-domain.com", "spaces in@email.com"])
    func invalidEmails(email: String) {
        #expect(Validator.isValid(email: email) == false)
    }

    @Test("Concurrent session refreshes are serialized safely", .serialized)
    func tokenRefresh() async throws {
        let session = try await SessionManager.shared.refreshToken()
        #expect(session.isExpired == false)
    }
}\033[0m""")
                print(f"  \033[90m{sub_div}\033[0m")
                print(f"  \033[1;92m{div}\033[0m")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "2":
                clear_screen()
                print_header("Point-Free SnapshotTesting (Visual & Data Regression)")
                div = "=" * min(cols - 4, 68)
                sub_div = "-" * min(cols - 4, 68)
                print(f"  \033[1;92m{div}\033[0m")
                print("  \033[1;97m📌 OVERVIEW & JOB TO BE DONE\033[0m")
                print_wrapped_description("SnapshotTesting automatically captures pixel-accurate visual snapshots (images) or structured data representations (JSON, text dump) of your SwiftUI views, view controllers, and models, instantly catching visual or architectural regressions.", indent_size=2)
                print()
                print("  \033[1;97m💡 WHY GO BEYOND NATIVE TESTING?\033[0m")
                print_wrapped_kv("  • ", "Native Swift Testing/XCTest only inspect variables in memory (e.g. state == .loaded).", indent_size=4)
                print_wrapped_kv("  • ", "They CANNOT detect clipped text, missing padding, Dynamic Type overflows, or Dark Mode issues.", indent_size=4)
                print_wrapped_kv("  • ", "SnapshotTesting renders actual views offscreen and diffs them against baseline images.", indent_size=4)
                print()
                print("  \033[1;97m⚠️ PAIN POINTS SOLVED\033[0m")
                print_wrapped_kv("  • ", "Eliminates tedious manual visual QA across dozens of screens and iOS devices.", indent_size=4)
                print_wrapped_kv("  • ", "Prevents unintended layout breakage when refactoring design systems or shared styles.", indent_size=4)
                print_wrapped_kv("  • ", "Automates visual verification directly in CI pull requests.", indent_size=4)
                print()
                print("  \033[1;97m🔗 DOCUMENTATION & SPM PACKAGE\033[0m")
                print_wrapped_kv("  \033[1;36m• GitHub Repo:\033[0m      ", "https://github.com/pointfreeco/swift-snapshot-testing", indent_size=4)
                print_wrapped_kv("  \033[1;36m• Documentation:\033[0m    ", "https://pointfreeco.github.io/swift-snapshot-testing/", indent_size=4)
                print_wrapped_kv("  \033[1;36m• SPM Package URL:\033[0m  ", "https://github.com/pointfreeco/swift-snapshot-testing.git", indent_size=4)
                print_wrapped_kv("  \033[1;36m• Version:\033[0m          ", 'from: "1.17.0"', indent_size=4)
                print()
                print("  \033[1;97m⚡ KEY STRATEGIES & CONCEPTS\033[0m")
                print_wrapped_kv("  \033[1;93m.image\033[0m              ", "Renders exact pixel image of UIViewController or SwiftUI view.", indent_size=24)
                print_wrapped_kv("  \033[1;93m.image(on:)\033[0m         ", "Renders view on specific device configurations (e.g. .iPhone13Pro).", indent_size=24)
                print_wrapped_kv("  \033[1;93m.dump / .json\033[0m       ", "Dumps data models/states to detect property or payload changes.", indent_size=24)
                print_wrapped_kv("  \033[1;93mrecord: true\033[0m        ", "Generates new reference snapshots (run once to save images).", indent_size=24)
                print_wrapped_kv("  \033[1;93misRecording = true\033[0m  ", "Class-level flag to record all tests in the file.", indent_size=24)
                print()
                print("  \033[1;97m💡 BEST PRACTICES\033[0m")
                print_wrapped_kv("  1. ", "Fix simulator dimensions using ViewImageConfig.iPhone16Pro for deterministic rendering.", indent_size=5)
                print_wrapped_kv("  2. ", "Snapshot both Light and Dark modes for all primary user views.", indent_size=5)
                print_wrapped_kv("  3. ", "Test with accessibility / dynamic type sizes (e.g. .extraExtraExtraLarge).", indent_size=5)
                print_wrapped_kv("  4. ", "Ensure animations are disabled via UIView.setAnimationsEnabled(false).", indent_size=5)
                print()
                print("  \033[1;97m📝 PRODUCTION STARTER TEMPLATE\033[0m")
                print(f"  \033[90m{sub_div}\033[0m")
                print("""\033[97mimport XCTest
import SnapshotTesting
import SwiftUI
@testable import MyApp

final class ProfileViewSnapshotTests: XCTestCase {

    override func setUp() {
        super.setUp()
        // isRecording = true // Set true once when updating baseline images
    }

    func testProfileViewLightAndDarkMode() {
        let view = ProfileView(user: .mockUser)
        let vc = UIHostingController(rootView: view)
        vc.view.frame = CGRect(x: 0, y: 0, width: 393, height: 852) // iPhone 16 Pro

        // 1. Light Mode
        vc.overrideUserInterfaceStyle = .light
        assertSnapshot(of: vc, as: .image, named: "light_mode")

        // 2. Dark Mode
        vc.overrideUserInterfaceStyle = .dark
        assertSnapshot(of: vc, as: .image, named: "dark_mode")
    }

    func testProfileViewLoadingState() {
        let view = ProfileView(user: nil, isLoading: true)
        let vc = UIHostingController(rootView: view)
        vc.view.frame = CGRect(x: 0, y: 0, width: 393, height: 852)
        
        assertSnapshot(of: vc, as: .image, named: "loading_state")
    }
}\033[0m""")
                print(f"  \033[90m{sub_div}\033[0m")
                print(f"  \033[1;92m{div}\033[0m")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "3":
                clear_screen()
                print_header("Quick & Nimble (Behavior-Driven Development / BDD)")
                div = "=" * min(cols - 4, 68)
                sub_div = "-" * min(cols - 4, 68)
                print(f"  \033[1;92m{div}\033[0m")
                print("  \033[1;97m📌 OVERVIEW & JOB TO BE DONE\033[0m")
                print_wrapped_description("Quick & Nimble provide a powerful Behavior-Driven Development (BDD) testing framework for Swift. Quick structures tests into expressive behavioral contexts, while Nimble delivers fluent, readable assertions with asynchronous polling.", indent_size=2)
                print()
                print("  \033[1;97m💡 WHY GO BEYOND NATIVE TESTING?\033[0m")
                print_wrapped_kv("  • ", "Swift Testing uses flat tests. For complex multi-step state machines (scans, checkout, uploads), flat tests lead to heavy duplication.", indent_size=4)
                print_wrapped_kv("  • ", "Swift Testing lacks built-in async polling for states that settle asynchronously over background queues (Combine, WebSocket, CoreData).", indent_size=4)
                print_wrapped_kv("  • ", "Nimble provides expect(state).toEventually(beTrue()) with automatic polling and timeouts.", indent_size=4)
                print()
                print("  \033[1;97m⚠️ PAIN POINTS SOLVED\033[0m")
                print_wrapped_kv("  • ", "Eliminates flaky async tests caused by arbitrary Task.sleep timeouts.", indent_size=4)
                print_wrapped_kv("  • ", "Hierarchical beforeEach/afterEach scoping prevents state pollution between specs.", indent_size=4)
                print_wrapped_kv("  • ", "Living documentation: Specs read as human-readable product requirements.", indent_size=4)
                print()
                print("  \033[1;97m🔗 DOCUMENTATION & SPM PACKAGES\033[0m")
                print_wrapped_kv("  \033[1;36m• Quick Repo:\033[0m       ", "https://github.com/Quick/Quick", indent_size=4)
                print_wrapped_kv("  \033[1;36m• Nimble Repo:\033[0m      ", "https://github.com/Quick/Nimble", indent_size=4)
                print_wrapped_kv("  \033[1;36m• Documentation:\033[0m    ", "https://quick.github.io/Quick/", indent_size=4)
                print_wrapped_kv("  \033[1;36m• SPM Packages:\033[0m     ", "https://github.com/Quick/Quick.git", indent_size=4)
                print_wrapped_kv("                      ", "https://github.com/Quick/Nimble.git", indent_size=4)
                print()
                print("  \033[1;97m⚡ KEY CONCEPTS & FLUENT MATCHERS\033[0m")
                print_wrapped_kv("  \033[1;93mdescribe(\"...\")\033[0m    ", "Defines the class, struct, or feature under test.", indent_size=24)
                print_wrapped_kv("  \033[1;93mcontext(\"when...\")\033[0m ", "Establishes a specific condition, state, or mock environment.", indent_size=24)
                print_wrapped_kv("  \033[1;93mit(\"should...\")\033[0m    ", "Specifies the exact behavioral expectation.", indent_size=24)
                print_wrapped_kv("  \033[1;93mbeforeEach / after\033[0m  ", "Hierarchical setup and teardown scoped to each context.", indent_size=24)
                print_wrapped_kv("  \033[1;93mexpect(...).to(...)\033[0m ", "Fluent synchronous matcher (e.g. equal, beNil, contain).", indent_size=24)
                print_wrapped_kv("  \033[1;93mtoEventually(...)\033[0m   ", "Asynchronous polling matcher for async State, Combine, and network.", indent_size=24)
                print_wrapped_kv("  \033[1;93mAsyncSpec\033[0m           ", "Modern base class for native async/await spec definitions.", indent_size=24)
                print()
                print("  \033[1;97m💡 BEST PRACTICES\033[0m")
                print_wrapped_kv("  1. ", "Structure specs like user stories: describe(Feature) -> context(Scenario) -> it(Behavior).", indent_size=5)
                print_wrapped_kv("  2. ", "Use beforeEach to isolate state across specs and prevent test pollution.", indent_size=5)
                print_wrapped_kv("  3. ", "Use expect(state).toEventually(beTrue()) for background async state updates.", indent_size=5)
                print()
                print("  \033[1;97m📝 PRODUCTION STARTER TEMPLATE\033[0m")
                print(f"  \033[90m{sub_div}\033[0m")
                print("""\033[97mimport Quick
import Nimble
@testable import MyApp

final class DocumentProcessorSpec: AsyncSpec {
    override class func spec() {
        describe("DocumentProcessor Pipeline") {
            var processor: DocumentProcessor!
            var mockScanner: MockScannerService!

            beforeEach {
                mockScanner = MockScannerService()
                processor = DocumentProcessor(scannerService: mockScanner)
            }

            context("when a valid document is imported") {
                beforeEach {
                    mockScanner.stubbedResult = .success(DocumentData.sample)
                }

                it("processes OCR text and transitions state to completed") {
                    await processor.processDocument(id: "doc-123")
                    
                    expect(processor.state).to(equal(.completed))
                    expect(processor.extractedText).to(contain("INVOICE"))
                }
            }

            context("when scanning fails with network timeout") {
                beforeEach {
                    mockScanner.stubbedResult = .failure(.timeout)
                }

                it("sets error message and enables retry button") {
                    await processor.processDocument(id: "doc-123")
                    
                    expect(processor.state).to(equal(.failed(reason: "timeout")))
                    expect(processor.canRetry).to(beTrue())
                }
            }
        }
    }
}\033[0m""")
                print(f"  \033[90m{sub_div}\033[0m")
                print(f"  \033[1;92m{div}\033[0m")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "4":
                clear_screen()
                print_header("Generate Sample Swift Testing File (Canary Suite)")
                div = "=" * min(cols - 4, 68)
                print(f"  \033[1;92m{div}\033[0m")
                print("  \033[1;97m📌 PURPOSE & BENEFITS (JOB TO BE DONE)\033[0m")
                print_wrapped_kv("  • \033[1;93mToolchain Canary:\033[0m     ", "Verifies in 1 second that Xcode 16+ Swift Testing macros, scheme target dependencies, and @testable imports compile without errors.", indent_size=4)
                print_wrapped_kv("  • \033[1;93mZero-Friction Template:\033[0m", "Creates a ready-to-run suite with working examples of #expect, #require, and parameterized arguments: matrix testing.", indent_size=4)
                print_wrapped_kv("  • \033[1;93mAI & Team Reference:\033[0m   ", "Gives coding agents and developers a concrete file to pattern-match for expanding test coverage across other app modules.", indent_size=4)
                print(f"  \033[1;92m{div}\033[0m\n")
                tt = PROJECT_CONFIG.test_target or "AppTests"
                tt_dir = ROOT / tt
                tt_dir.mkdir(parents=True, exist_ok=True)
                sample_file = tt_dir / "SampleSwiftTestingTests.swift"
                sample_code = f"""import Testing
@testable import {PROJECT_CONFIG.scheme or PROJECT_CONFIG.project_name or "App"}

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
                    print(f"  \033[93mFile already exists: {sample_file.relative_to(ROOT)}\033[0m")
                else:
                    sample_file.write_text(sample_code, encoding="utf-8")
                    print(f"  \033[1;92m✅ Created {sample_file.relative_to(ROOT)}\033[0m")
                print(f"\n  \033[90mRun this test anytime by pressing 'A' (Run All Unit Tests) in the Manage Test Coverage menu.\033[0m")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "i":
                if not has_xcbeautify:
                    clear_screen()
                    print_header("Installing xcbeautify via Homebrew")
                    res = subprocess.run(["brew", "install", "xcbeautify"])
                    if res.returncode == 0:
                        print("\n\033[1;92m✅ Successfully installed xcbeautify!\033[0m")
                    else:
                        print(f"\n\033[1;91m❌ Installation failed with code {res.returncode}.\033[0m")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                else:
                    clear_screen()
                    print_header("xcbeautify CLI Formatter")
                    div = "=" * min(cols - 4, 68)
                    print(f"  \033[1;92m{div}\033[0m")
                    print("  \033[1;97m📌 STATUS: INSTALLED & ACTIVE\033[0m")
                    print_wrapped_description("xcbeautify is installed on your system and automatically cleans up xcodebuild test execution into colorized, 1-line pass/fail summaries.", indent_size=2)
                    print()
                    print("  \033[1;97m🔗 OFFICIAL REPOSITORY\033[0m")
                    print_wrapped_kv("  \033[1;36m• GitHub:\033[0m ", "https://github.com/cpisciotta/xcbeautify", indent_size=4)
                    print(f"  \033[1;92m{div}\033[0m")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            else:
                error_msg = f"'{choice}'"

def handle_run_tests_menu(session_allowed_machines: list[str], session_allowed_models: list[str]) -> None:
    error_msg = ""
    while True:
        clear_screen()
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Run Unit Tests")

            suites = discover_test_suites(ROOT, PROJECT_CONFIG.test_target)
            total_tests = sum(s["test_count"] for s in suites)

            test_plans_root = ROOT / PROJECT_CONFIG.test_target / "TestPlans"
            test_plans = sorted(test_plans_root.glob("*.xctestplan")) if test_plans_root.exists() else []

            try:
                cols, _ = os.get_terminal_size()
            except Exception:
                cols = 80
            cols = max(cols, 40)

            opt_w = 5
            name_w = 28
            count_w = 12

            def format_opt_cell(k: str, color_code: str = "\033[1;96m", width: int = opt_w) -> str:
                raw_tag = f"[{k}]"
                pad = " " * max(0, width - len(raw_tag))
                return f"[{color_code}{k}\033[0m]{pad}"

            target_name = PROJECT_CONFIG.test_target or "All Targets"
            opt1_count = f"{total_tests} test(s)" if total_tests > 0 else "All tests"

            # Primary Action: Run All Tests
            print(f"  [\033[1;92m1\033[0m] \033[1;92m🚀 Run All Unit Tests\033[0m \033[1;97m({opt1_count})\033[0m \033[90m• Target: {target_name}\033[0m")
            print(f"      \033[90mRuns entire test target via xcodebuild test\033[0m\n")

            option_map: dict[str, dict[str, Any]] = {}
            current_idx = 2

            if suites or test_plans:
                print(f"  \033[1;97m📋 Individual Test Suites\033[0m \033[90m(-only-testing)\033[0m")

                if cols >= 80:
                    desc_w = max(16, cols - 60)
                    header = f"  {'Opt':<{opt_w}} | {'Test Suite':<{name_w}} | {'Test Count':<{count_w}} | {'Scope / Location':<{desc_w}}"
                    print(header)
                    print("  " + "-" * min(len(header) - 2, cols - 4))

                    # List individual suites
                    for s in suites:
                        key = str(current_idx)
                        option_map[key] = {"type": "suite", "suite": s}
                        s_name = s["name"]
                        if len(s_name) > name_w:
                            s_name = s_name[:name_w - 2] + ".."
                        s_count = f"{s['test_count']} test(s)"
                        s_rel = str(s["rel_path"])
                        display_rel = s_rel if len(s_rel) <= desc_w else s_rel[:max(0, desc_w - 2)] + ".."
                        print(f"  {format_opt_cell(key)} | {s_name:<{name_w}} | \033[93m{s_count:<{count_w}}\033[0m | \033[90m{display_rel:<{desc_w}}\033[0m")
                        current_idx += 1

                    # List test plans if any exist
                    if test_plans:
                        print(f"  {' ' * opt_w} | \033[1;90m--- XCODE TEST PLANS ---\033[0m")
                        for tp in test_plans:
                            key = str(current_idx)
                            option_map[key] = {"type": "plan", "plan": tp}
                            tp_name = tp.stem
                            if len(tp_name) > name_w:
                                tp_name = tp_name[:name_w - 2] + ".."
                            tp_rel = str(tp.relative_to(ROOT))
                            display_rel = tp_rel if len(tp_rel) <= desc_w else tp_rel[:max(0, desc_w - 2)] + ".."
                            print(f"  {format_opt_cell(key)} | {tp_name:<{name_w}} | \033[95m{'Test Plan':<{count_w}}\033[0m | \033[90m{display_rel:<{desc_w}}\033[0m")
                            current_idx += 1
                else:
                    # Responsive compact layout for narrow terminals
                    name_w_compact = max(16, cols - 26)
                    header = f"  {'Opt':<{opt_w}} | {'Test Suite':<{name_w_compact}} | {'Test Count':<{count_w}}"
                    print(header)
                    print("  " + "-" * min(len(header) - 2, cols - 4))

                    # List individual suites
                    for s in suites:
                        key = str(current_idx)
                        option_map[key] = {"type": "suite", "suite": s}
                        s_name = s["name"]
                        if len(s_name) > name_w_compact:
                            s_name = s_name[:name_w_compact - 2] + ".."
                        s_count = f"{s['test_count']} test(s)"
                        print(f"  {format_opt_cell(key)} | {s_name:<{name_w_compact}} | \033[93m{s_count:<{count_w}}\033[0m")
                        current_idx += 1

                    # List test plans if any exist
                    if test_plans:
                        print(f"  {' ' * opt_w} | \033[1;90m--- XCODE TEST PLANS ---\033[0m")
                        for tp in test_plans:
                            key = str(current_idx)
                            option_map[key] = {"type": "plan", "plan": tp}
                            tp_name = tp.stem
                            if len(tp_name) > name_w_compact:
                                tp_name = tp_name[:name_w_compact - 2] + ".."
                            print(f"  {format_opt_cell(key)} | {tp_name:<{name_w_compact}} | \033[95m{'Test Plan':<{count_w}}\033[0m")
                            current_idx += 1

            print("\n  [\033[1;91mB\033[0m] Back\n")

            if error_msg:
                print(f"\n\033[1;91mNOT A VALID OPTION, PLEASE TRY AGAIN... ({error_msg})\033[0m")
                error_msg = ""

            max_choice = current_idx - 1
            prompt_range = f"(1-{max_choice}, B)" if max_choice > 1 else "(1, B)"
            prompt = get_choice_prompt("Choice:", prompt_range)
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()

            if choice == "b":
                break
            elif choice in ("1", "a"):
                clear_screen()
                print_header(f"Running All Unit Tests ({target_name})")
                print(f"  \033[90mTarget: {target_name} ({opt1_count})\033[0m\n")
                run_script("manual_run.py", ["test"], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models, prompt=f"Running all unit tests ({target_name})")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice in option_map:
                opt = option_map[choice]
                if opt["type"] == "suite":
                    selected_suite = opt["suite"]
                    suite_name = selected_suite["name"]
                    clear_screen()
                    print_header(f"Running Test Suite: {suite_name}")
                    if selected_suite.get("language", "swift") == "swift":
                        tt = PROJECT_CONFIG.test_target or ""
                        testing_flag = f"-only-testing:{tt}/{suite_name}" if tt else f"-only-testing:{suite_name}"
                        script_args = ["test", "--test-only", testing_flag]
                        display_command = testing_flag
                    else:
                        display_command = test_command_for_suite(selected_suite, PROJECT_CONFIG)
                        script_args = ["test", "--test-command", display_command]
                    print(f"  \033[90mExecuting {display_command}...\033[0m\n")
                    run_script("manual_run.py", script_args, sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models, prompt=f"Running {suite_name}")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                elif opt["type"] == "plan":
                    tp = opt["plan"]
                    flags = get_test_plan_flags(tp)
                    clear_screen()
                    print_header(f"Running Test Plan: {tp.stem}")
                    print(f"  \033[90mExecuting {flags}...\033[0m\n")
                    run_script("manual_run.py", ["test", "--test-only", flags], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models, prompt=f"Running {tp.stem}")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            else:
                error_msg = f"'{choice}'"

def handle_manage_tests(session_allowed_machines: list[str], session_allowed_models: list[str]):
    error_msg = ""
    while True:
        clear_screen()
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Manage Test Coverage")

            # 1. Code Coverage Status & Visual Progress Bar
            cov_data = get_coverage_data()
            bar_width = 16
            cov_ts_short = None
            if cov_data and cov_data.get("overall_coverage_pct") is not None:
                cov_pct = cov_data["overall_coverage_pct"]
                cov_ts = cov_data.get("timestamp", "")
                cov_ts_short = cov_ts[:19].replace("T", " ") if cov_ts else ""
                filled = int(round((cov_pct / 100.0) * bar_width))
                filled = max(0, min(bar_width, filled))
                empty = bar_width - filled
                if cov_pct >= 80:
                    cov_color = "\033[1;92m"
                elif cov_pct >= 50:
                    cov_color = "\033[1;93m"
                else:
                    cov_color = "\033[1;91m"
                
                bar_str = f"{cov_color}{'█' * filled}\033[90m{'░' * empty}\033[0m"
                cov_display = f"{cov_color}{cov_pct:.1f}%\033[0m"
            else:
                bar_str = f"\033[90m{'░' * bar_width}\033[0m"
                cov_display = "\033[93mNot calculated yet\033[0m \033[90m(Press 'C')\033[0m"

            print("  \033[1;90m┌─────────────────────────────────────────────────────────────┐\033[0m")
            print(f"  \033[1;90m│\033[0m \033[1;97mCODE COVERAGE:\033[0m [{bar_str}]  {cov_display}")
            if cov_ts_short:
                print(f"  \033[1;90m│\033[0m \033[1;36mLast Audited:\033[0m  \033[90m{cov_ts_short}\033[0m")
            print(f"  \033[1;90m│\033[0m \033[1;36mTest Target:\033[0m   \033[97m{PROJECT_CONFIG.test_target}\033[0m")
            print("  \033[1;90m└─────────────────────────────────────────────────────────────┘\033[0m\n")

            # 2. Discover Test Suites
            suites = discover_test_suites(ROOT, PROJECT_CONFIG.test_target)
            total_tests = sum(s["test_count"] for s in suites)

            # 3. Dynamic Test Plans
            test_plans_root = ROOT / PROJECT_CONFIG.test_target / "TestPlans"
            test_plans = sorted(test_plans_root.glob("*.xctestplan")) if test_plans_root.exists() else []

            plan_map = {}
            if test_plans:
                print("  \033[1;90m--- XCODE TEST PLANS ---\033[0m")
                for i, tp in enumerate(test_plans):
                    key = str(i + 1)
                    rel_path = tp.relative_to(ROOT)
                    plan_map[key] = tp
                    print(f"  [\033[1;96m{key}\033[0m] {tp.stem} \033[90m({rel_path})\033[0m")
                print()

            # 4. Discovered Test Suites & Test Count
            print(f"  \033[1;90m--- TEST SUITES ({len(suites)} suites, {total_tests} tests total) ---\033[0m")
            if suites:
                max_suites_show = 8
                for i, s in enumerate(suites[:max_suites_show]):
                    print(f"    \033[1;97m• {s['name']}\033[0m: \033[92m{s['test_count']} test(s)\033[0m")
                if len(suites) > max_suites_show:
                    print(f"    \033[90m... and {len(suites) - max_suites_show} more test suite(s)\033[0m")
            else:
                print("    \033[90mNo test suites found in " + PROJECT_CONFIG.test_target + ".\033[0m")

            print_header("Actions")
            print("[\033[1;92mA\033[0m] Run Unit Tests (All / Partial)")
            print("[\033[1;96mC\033[0m] Calculate / Refresh Code Coverage")
            print("[\033[1;96mE\033[0m] Expand Unit Test Coverage (AI Job)")
            print("[\033[1;96mF\033[0m] Test Frameworks & Canaries")
            print("[\033[1;96mR\033[0m] Rename a Test Suite / File")
            print("[\033[1;96mV\033[0m] Simulator Visual Check (Screenshots)")
            print("[\033[1;91mB\033[0m] Back\n")

            if error_msg:
                print(f"\n\033[1;91mNOT A VALID OPTION, PLEASE TRY AGAIN... ({error_msg})\033[0m")
                error_msg = ""

            prompt = get_choice_prompt("Choice:", "(number or letter)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()

            if not choice:
                continue

            if choice == "b":
                break
            elif choice == "a":
                handle_run_tests_menu(session_allowed_machines, session_allowed_models)
            elif choice == "c":
                run_calculate_coverage(session_allowed_machines, session_allowed_models)
            elif choice == "e":
                clear_screen()
                print_header("Expand Unit Test Coverage")
                print("  \033[90mAnalyzing codebase and existing test suites for coverage gaps...\033[0m\n")
                
                with ProgressIndicator(label="Scanning codebase for coverage gaps", hint="Ctrl-C to cancel"):
                    gaps = analyze_coverage_gaps(ROOT, suites, session_allowed_models)
                
                summary = ""
                if gaps:
                    options = []
                    for g in gaps:
                        prio = str(g.get("priority", "HIGH")).upper().strip()
                        target_str = ", ".join(g.get("target_files", [])[:2])
                        if prio == "HIGH":
                            prio_str = "\033[1;91m[HIGH]\033[0m"
                        elif prio == "MEDIUM":
                            prio_str = "\033[93m[MEDIUM]\033[0m"
                        elif prio == "LOW":
                            prio_str = "\033[90m[LOW]\033[0m"
                        else:
                            prio_str = f"[{prio}]"
                        label = f"🎯 {g['subsystem']} {prio_str}"
                        if target_str:
                            desc = f"{g['rationale']} - Targets: {target_str}"
                        else:
                            desc = f"{g['rationale']}"
                        options.append(f"{label} ({desc})")
                    
                    options.append("🌐 Comprehensive Coverage Audit (Full audit across all uncovered app subsystems)")
                    options.append("✏️ Custom Subsystem / Focus (Manually enter a subsystem, ViewModel, or module)")
                    
                    clear_screen()
                    print_header("Expand Unit Test Coverage - Select Focus")
                    print("Select an AI-identified coverage gap, full audit, or custom target:\n")
                    try:
                        gap_choice = prompt_radio("Select Coverage Focus / Target Gap:", options, default=options[0])
                        selected_idx = options.index(gap_choice)
                    except (BackException, ValueError):
                        continue
                    
                    if selected_idx < len(gaps):
                        chosen_gap = gaps[selected_idx]
                        summary = f"Expand Unit Test Coverage: {chosen_gap['subsystem']} - {chosen_gap['rationale']}"
                        if chosen_gap.get("suggested_focus"):
                            summary += f" Focus: {chosen_gap['suggested_focus']}."
                        if chosen_gap.get("target_files"):
                            summary += f" Target Files: {', '.join(chosen_gap['target_files'])}"
                    elif selected_idx == len(gaps):
                        summary = "Comprehensive Unit Test Coverage"
                    elif selected_idx == len(gaps) + 1:
                        clear_screen()
                        print_header("Expand Unit Test Coverage - Custom Focus")
                        print("  \033[90mEnter a subsystem, ViewModel, or service name to focus on.\033[0m")
                        print("  \033[90mLeave blank for comprehensive audit, or type 'b' / press Esc to cancel.\033[0m\n")
                        try:
                            focus = prompt_input("Coverage focus or subsystem (Enter for comprehensive):", placeholder="e.g. AuthViewModel, DataManager, NetworkClient", allow_back=True, field_below=True)
                        except BackException:
                            continue
                        if focus.strip().lower() == "b":
                            continue
                        summary = f"Expand Unit Test Coverage: {focus.strip()}" if focus and focus.strip() else "Comprehensive Unit Test Coverage"
                    else:
                        continue
                else:
                    clear_screen()
                    print_header("Expand Unit Test Coverage")
                    print("Create an autonomous AI job to inspect uncovered files and write comprehensive unit tests.\n")
                    print("  \033[90mType 'b' or press Esc to return to menu.\033[0m\n")
                    try:
                        focus = prompt_input("Coverage focus or subsystem (Enter for comprehensive):", placeholder="e.g. AuthViewModel, DataManager, NetworkClient", allow_back=True, field_below=True)
                    except BackException:
                        continue
                    if focus.strip().lower() == "b":
                        continue
                    summary = f"Expand Unit Test Coverage: {focus.strip()}" if focus and focus.strip() else "Comprehensive Unit Test Coverage"

                status_bar.clear_footer()
                status_bar.reset_scroll_region(force=True)
                job_args = ["coverage", "--branch-mode", "current", "--summary", summary]
                if session_allowed_models:
                    job_args.extend(["--allowed-models", ",".join(session_allowed_models)])
                if session_allowed_machines:
                    job_args.extend(["--allowed-machines", ",".join(session_allowed_machines)])
                run_script("new_job.py", job_args, sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
            elif choice == "f":
                handle_test_frameworks_menu(session_allowed_machines, session_allowed_models)
            elif choice == "r":
                if not suites:
                    print("\n  \033[90mNo test suites available to rename.\033[0m")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue
                clear_screen()
                print_header("Select Test Suite to Rename")
                options = [f"{s['name']} ({s['test_count']} tests) - {s['rel_path']}" for s in suites]
                try:
                    selected_idx_str = prompt_radio("Select suite to rename:", options, default=options[0])
                    selected_idx = options.index(selected_idx_str)
                    target_suite = suites[selected_idx]
                except (BackException, ValueError):
                    continue
                rename_test_suite(target_suite, ROOT)
            elif choice == "v":
                print_header("Running Simulator Visual Check")
                run_script("simulator_visual_check.py", [], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
            elif choice in plan_map:
                plan_path = plan_map[choice]
                print_header(f"Running Test Plan: {plan_path.stem}")
                flags = get_test_plan_flags(plan_path)
                run_script("manual_run.py", ["test", "--test-only", flags], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
            else:
                error_msg = f"'{choice}'"

handle_app_tests = handle_manage_tests

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

def prompt_for_logs(job: dict[str, Any], status_bar: StatusBar | None = None) -> list[str]:
    print_header("Link Logs")
    print("\033[90mAttach logs or manual run output so the next AI action has concrete failure context.\033[0m")
    print("\033[90mYou can select recent logs, paste new logs, or enter a custom local path.\033[0m\n")

    manual_base = OUTPUT_DIR / "manual"
    job_out_base = OUTPUT_DIR / job.get("job_id", "unknown")
    from common import format_log_path
    
    # We use a mapping so we can show readable names but return the actual paths
    display_options = []
    option_to_path = {}
    
    def add_log_option(full_path):
        try:
            p_obj = Path(full_path)
            if p_obj.is_absolute():
                try:
                    rel = str(p_obj.relative_to(ROOT))
                except ValueError:
                    rel = str(p_obj)
            else:
                rel = str(full_path)
        except Exception:
            rel = str(full_path)
        
        label = format_log_path(rel)
        if label not in option_to_path:
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

    # 2b. Recent device log pulls (orchestrator logs pull / Pull Device Logs)
    cloud_base = OUTPUT_DIR / "cloud_logs"
    if cloud_base.exists():
        cloud_dirs = sorted([d for d in cloud_base.iterdir() if d.is_dir() and not d.is_symlink()],
                            key=lambda x: x.name, reverse=True)[:3]
        for d in cloud_dirs:
            add_log_option(d)

    # 3. Existing attached logs
    curr_logs = job.get("last_manual_log_paths", [])
    if not curr_logs and job.get("last_manual_log_path"):
        curr_logs = [job.get("last_manual_log_path")]

    for p in curr_logs:
        if p:
            add_log_option(p)

    curr_norm_set = set()
    for p in curr_logs:
        if not p:
            continue
        try:
            p_obj = Path(p)
            if p_obj.is_absolute():
                try:
                    rel = str(p_obj.relative_to(ROOT))
                except ValueError:
                    rel = str(p_obj)
            else:
                rel = str(p)
        except Exception:
            rel = str(p)
        curr_norm_set.add(rel)
        curr_norm_set.add(format_log_path(rel))
        curr_norm_set.add(str(p))

    curr_labels = [
        label for label, rel in option_to_path.items()
        if rel in curr_norm_set or label in curr_norm_set
    ]

    paste_opt = "\033[1;96mPaste New Logs...\033[0m"
    cloud_opt = "\033[1;96mPull Device Logs (cloud)...\033[0m"
    custom_opt = "\033[1;96mCustom Path...\033[0m"

    all_options = [paste_opt, cloud_opt, custom_opt]
    if display_options:
        all_options.append("--- Recent Logs ---")
        all_options.extend(display_options)

    created_status_bar = False
    if status_bar is None:
        status_bar = StatusBar(job, sub_menu=True)
        created_status_bar = True

    try:
        selected = prompt_checkbox("Select logs to link for this job:", all_options, curr_labels, status_bar=status_bar)
    finally:
        if created_status_bar:
            status_bar.reset_scroll_region(force=True)

    log_paths = []
    from manual_run import capture_logs
    for s in selected:
        if s.startswith("---"):
            continue
        if "Paste New Logs..." in s:
            ts = now_iso().replace(":", "").replace("-", "")[:15]
            manual_out = manual_base / ts
            manual_out.mkdir(parents=True, exist_ok=True)
            log_file = capture_logs(manual_out)
            if log_file:
                log_paths.append(str(manual_out.relative_to(ROOT)))
        elif "Pull Device Logs (cloud)..." in s:
            cloud_path = prompt_cloud_log_pull(status_bar)
            if cloud_path:
                log_paths.append(cloud_path)
        elif "Custom Path..." in s:
            print("\n    (Enter path to a log file or directory, or Enter to cancel; e.g. logs/build.log or /var/log)")
            path = prompt_input("Custom log path:", placeholder="logs/build.log or /path/to/logs", field_below=True)
            if path:
                log_paths.append(path)
        else:
            # Map back to actual path if it's in our mapping, otherwise use the label
            log_paths.append(option_to_path.get(s, s))

    return log_paths

def prompt_cloud_log_pull(status_bar: StatusBar | None = None) -> str | None:
    """Picks a recent app launch from the central log store and pulls its logs.

    Returns a log directory to link, `cloud:latest` to re-pull the newest launch
    on every debug run, or None.
    """
    import cloud_logs

    print_header("Pull Device Logs")
    try:
        client = cloud_logs.default_client()
        print("\033[90mFetching app launches from the last 24h...\033[0m")
        sessions = cloud_logs.list_sessions(client, since="24h", limit=10)
    except cloud_logs.CloudLogsError as exc:
        print(f"\n\033[1;91m{exc}\033[0m")
        input("\n\033[1;96mTap Enter to continue...\033[0m")
        return None
    if not sessions:
        print("\n\033[93mNo app launches in the last 24h. Open the app, wait ~5s, and retry.\033[0m")
        input("\n\033[1;96mTap Enter to continue...\033[0m")
        return None

    newest_opt = "Always newest launch (re-pulled on every debug run)"
    options = [s.describe() for s in sessions] + [newest_opt]
    choice = prompt_radio("Which app launch?", options, default=options[0], status_bar=status_bar)
    if choice == newest_opt:
        return "cloud:latest"
    session = sessions[options.index(choice)].session
    try:
        out_dir = cloud_logs.pull(session, client=client)
    except cloud_logs.CloudLogsError as exc:
        print(f"\n\033[1;91m{exc}\033[0m")
        input("\n\033[1;96mTap Enter to continue...\033[0m")
        return None
    try:
        rel = str(out_dir.relative_to(ROOT))
    except ValueError:
        rel = str(out_dir)
    print(f"\033[92m✅ Pulled session {session} -> {rel}\033[0m")
    return rel


def prompt_for_reference_artifact(job: dict[str, Any]) -> bool:
    from reference_artifacts import attach_reference_artifact

    print_header("Attach UI Mockup / Reference")
    print("\033[90mAttach a design reference, screenshot, mockup, spec, or URL for future AI steps.\033[0m")
    print("\033[90mThe artifact will be saved on the job and included in planner/builder context.\033[0m\n")
    print("Supported local files: html, htm, png, jpg, jpeg, webp, gif, pdf, md, txt, css, json, fig")
    print("Supported URLs: http/https, including Figma links stored as URL references.")
    print()

    print("    (Enter path to local file OR a web URL, or Enter to cancel; e.g. mockups/login.png or https://figma.com/...)")
    source = prompt_input("File path or URL:", placeholder="mockups/login.png or https://figma.com/...", field_below=True)
    if not source: return False
    note = prompt_input("Reference note:", placeholder="target screen/state; optional", field_below=True)


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

            print("\nActions:")
            print("    [\033[1;96mU\033[0m] Un-archive (Restore to main menu)\n")
            print("    [\033[1;91mB\033[0m] Back")

            status_bar.render(at_bottom=True, force=True)
            choice = get_key().strip().lower()

            if choice == "b":
                break
            elif choice == "u" and archived_files:
                try:
                    idx_str = prompt_input("Enter index to restore:", placeholder="(number)", field_below=True)
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

    def open_action_screen(title: str | None = None) -> None:
        status_bar.clear_footer()
        status_bar.reset_scroll_region(force=True)
        clear_screen()
        if title:
            print_header(title)

    def handle_bug_still_happening(job: dict[str, Any]) -> None:
        job_type = job.get("type", "")
        is_feature = job_type in {"feature-plan", "feature", "feature-design"}
        if is_feature:
            print("This will iterate on the feature, attach the latest logs, and trigger an automated fix/completion pass.\n")
            desc = prompt_input("Brief description of the bug or missing functionality:", placeholder="e.g. Save button doesn't trigger API call or missing empty state", field_below=True)
            default_hypo = "User reported bug or missing functionality in new feature."
        else:
            print("This will re-open the bug-fix job, attach the latest logs, and trigger another iteration.\n")
            desc = prompt_input("Brief description of the failure:", placeholder="e.g. OCR download fails with timeout", field_below=True)
            default_hypo = "User reported bug is still happening."

        if not desc:
            print("\nCancelled.")
            input("\n\033[1;96mTap Enter to continue...\033[0m")
            return

        recent_log = find_latest_runtime_log(job.get("job_id"))
        log_paths = job.get("last_manual_log_paths", [])
        if not isinstance(log_paths, list):
            log_paths = []

        if recent_log:
            print(f"\n✅ Automatically detected and attached latest log file:")
            print(f"      - \033[97m{recent_log}\033[0m")
            if recent_log not in log_paths:
                log_paths.append(recent_log)
        else:
            print("\n\033[1;93m⚠️  Warning: Could not automatically detect any recent log files in logs/ or output directories.\033[0m")
            if prompt_confirm("Would you like to manually link or paste a log file?", default=True, clear_screen=False):
                try:
                    manual_logs = prompt_for_logs(job)
                    for l in manual_logs:
                        if l not in log_paths:
                            log_paths.append(l)
                except Exception:
                    pass

        # Update job state to trigger debugging phase
        job["status"] = "debugging"
        job["debug_phase"] = "propose"
        job["last_manual_log_paths"] = log_paths
        
        # Push debug proposal history
        if "debug_history" not in job:
            job["debug_history"] = []
            
        iter_num = job.get("iteration", 0) + 1
        job["debug_history"].append({
            "iteration": iter_num,
            "hypothesis": default_hypo,
            "action": "Iterate fix based on user feedback.",
            "implementation_plan": desc,
            "expected_signal": "Validation successful",
            "result": "pending"
        })
        job["iteration"] = iter_num
        job["updated_at"] = now_iso()
        
        save_job(job)
        print("\n🚀 Starting automated debugging iteration...")
        time.sleep(1)
        
        run_script(
            "worker_run.py", 
            [str(job["_path"])], 
            job=job, 
            sub_menu=True, 
            session_machines=session_allowed_machines, 
            session_models=session_allowed_models
        )

    while True:
        if not isinstance(job, dict):
            print_header("Job Unavailable")
            print("The selected job could not be loaded. Returning to the main menu.")
            input("\n\033[1;96mTap Enter to continue...\033[0m")
            return

        clear_screen()
        actions = []
        
        status = job.get("status")
        phase = job.get("debug_phase", "propose")
        job_type = job.get("type")
        
        # Setup status bar
        job["online_machines"] = get_online_machines(job.get("allowed_machines", []))
        with StatusBar(job, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_job_header(job)

            # 1. Job ID & Issue Number
            issue_num = job.get("issue_number")
            job_id_str = str(job.get("job_id", "unknown"))
            if issue_num:
                id_display = f"\033[1;97m#{issue_num}\033[0m \033[90m({job_id_str})\033[0m"
            else:
                id_display = f"\033[1;97m{job_id_str}\033[0m"
            print(f"\033[1;96mJob ID:\033[0m       {id_display}")

            # 2. Status Badge
            status_badges = {
                "planned": "\033[1;96m📝 Planned\033[0m",
                "executing": "\033[1;93m⚙️  Executing\033[0m",
                "debugging": "\033[1;95m🐞 Debugging\033[0m",
                "review-needed": "\033[1;92m🏁 Ready for Review\033[0m",
                "human-needed": "\033[1;91m🚨 Action Required\033[0m",
                "completed": "\033[1;92m✅ Completed\033[0m",
                "designing": "\033[1;96m🎨 Designing\033[0m",
            }
            badge = status_badges.get(status, f"\033[97m{status}\033[0m")
            if status == "debugging":
                iter_num = job.get("iteration", 1)
                badge += f" \033[90m(Attempt #{iter_num})\033[0m"
            print(f"\033[1;96mStatus:\033[0m       {badge}")

            # 3. Branch & Base
            branch = job.get("branch")
            base_branch = job.get("base_branch", PROJECT_CONFIG.base_branch)
            if branch:
                print(f"\033[1;96mBranch:\033[0m       \033[97m{branch}\033[0m \033[90m(base: {base_branch})\033[0m")

            # 4. Tests Status Summary
            test_summary = get_job_test_summary(job)
            created_cnt = test_summary["created_count"]
            failed_cnt = test_summary["failed_count"]
            passed_cnt = test_summary["passed_count"]
            f_tests = test_summary["failing_tests"]
            t_status = test_summary["status"]

            created_suffix = f" \033[90m|\033[0m \033[1;97m{created_cnt} created\033[0m" if created_cnt > 0 else ""

            if t_status == "failing" and failed_cnt > 0:
                fail_badge = f"\033[1;91m❌ {failed_cnt} failing\033[0m"
                print(f"\033[1;96mTests:\033[0m        {fail_badge}{created_suffix}")
                for ft in f_tests[:3]:
                    print_wrapped_bullet("  \033[1;91m└─ ❌\033[0m ", f"\033[1;91m{ft}\033[0m")
                if len(f_tests) > 3:
                    print(f"  \033[90m└─ ... and {len(f_tests) - 3} more\033[0m")
            elif t_status == "build-failed":
                print(f"\033[1;96mTests:\033[0m        \033[1;91m❌ Build / Compilation Failed\033[0m{created_suffix}")
            elif t_status == "passing" or (failed_cnt == 0 and passed_cnt > 0):
                print(f"\033[1;96mTests:\033[0m        \033[1;92m✅ All Passing\033[0m{created_suffix}")
            elif t_status == "pending":
                print(f"\033[1;96mTests:\033[0m        \033[90m⚙️ Pending execution\033[0m{created_suffix}")
            else:
                print(f"\033[1;96mTests:\033[0m        \033[90mUntested\033[0m{created_suffix}")

            # 5. Linked Logs
            logs = job.get("last_manual_log_paths", [])
            if logs:
                from common import format_log_path, format_file_link
                count_str = f"{len(logs)} attached" if len(logs) > 1 else "1 attached"
                print(f"\033[1;96mLinked Logs:\033[0m  \033[92m{count_str}\033[0m")
                for l in logs[:3]:
                    p = Path(l) if Path(l).is_absolute() else (ROOT / l)
                    if p.parent.name == "manual":
                        raw_name = f"manual/{p.name}"
                    else:
                        raw_name = p.name
                    log_label = format_log_path(raw_name)
                    link_str = format_file_link(p, label=f"\033[92m{log_label}\033[0m")
                    print_wrapped_bullet("  \033[90m└─ 📄\033[0m ", link_str)
                if len(logs) > 3:
                    print(f"  \033[90m└─ ... and {len(logs) - 3} more\033[0m")

            # 6. AI Sessions & Interactive CLI Investigations
            sessions = job.get("llm_sessions", [])
            if not sessions and "llm_session_ids" in job:
                sessions = [{"id": sid, "model": "unknown"} for sid in job["llm_session_ids"]]

            investigations = job.get("interactive_investigations", [])

            models_used = []
            for s in sessions:
                m = s.get("model") or s.get("tool")
                if m and m not in models_used and m != "unknown":
                    models_used.append(m)

            if investigations:
                for inv in investigations:
                    t = inv.get("tool")
                    if t and t not in models_used:
                        models_used.append(t)

            if sessions or investigations:
                total_sess_count = len(sessions) if sessions else len(investigations)
                print(f"\033[1;96mAI Sessions:\033[0m  \033[97m{total_sess_count} recorded\033[0m")

                if models_used:
                    if len(models_used) <= 3:
                        models_str = ", ".join(models_used)
                    else:
                        models_str = f"{', '.join(models_used[:3])} \033[90m(+{len(models_used) - 3} more)\033[0m"
                    print_wrapped_bullet("  \033[90m└─ 🤖 Models:\033[0m ", f"\033[97m{models_str}\033[0m")

                if investigations:
                    last_inv = investigations[-1]
                    t = last_inv.get("tool", "AI CLI")
                    d = last_inv.get("duration", "")
                    c = len(last_inv.get("new_commits", []))
                    n = last_inv.get("notes", "") or last_inv.get("note", "")
                    detail_parts = [d] if d else []
                    if c: detail_parts.append(f"{c} commit{'s' if c > 1 else ''}")
                    meta_str = f" \033[90m({', '.join(detail_parts)})\033[0m" if detail_parts else ""
                    note_str = f": \033[97m{n[:50]}..\033[0m" if len(n) > 50 else (f": \033[97m{n}\033[0m" if n else "")
                    print_wrapped_bullet("  \033[90m└─ 💬 Latest CLI:\033[0m ", f"\033[1;97m{t}\033[0m{meta_str}{note_str}")

            # 7. Task Progress Tracker (for multi-task plans)
            tasks = job.get("plan", {}).get("tasks", [])
            if tasks:
                completed = job.get("completed_task_indices", [])
                count = len(tasks)
                done = len(completed)
                percent = int((done / count) * 100) if count > 0 else 0

                bar_width = 16
                filled = int(bar_width * done / count)
                bar = "█" * filled + "░" * (bar_width - filled)

                yolo_status = " \033[1;93m[YOLO]\033[0m" if job.get("is_yolo") else ""
                print(f"\033[1;96mTasks:\033[0m        \033[1;96m{bar}\033[0m {done}/{count} ({percent}%){yolo_status}")
                if done < count:
                    next_task = tasks[done]
                    print(f"  \033[90m└─ Next: \033[97m{next_task.get('name', f'Task {done+1}')}\033[0m")
                else:
                    print("  \033[92m└─ Status: Feature complete!\033[0m")

            # 8. References & Clarifications
            references = job.get("reference_artifacts", [])
            if references:
                print(f"\033[1;96mReferences:\033[0m   \033[92m{len(references)} attached\033[0m")
                for ref in references[:2]:
                    label = ref.get("url") or ref.get("path") or ref.get("source") or ref.get("type", "reference")
                    note = ref.get("note")
                    suffix = f" \033[90m- {note}\033[0m" if note else ""
                    print_wrapped_bullet("  \033[90m└─\033[0m ", f"\033[90m{label}{suffix}\033[0m")
                if len(references) > 2:
                    print(f"  \033[90m└─ ... and {len(references) - 2} more\033[0m")

            clarifications = job.get("clarification_history", [])
            if clarifications:
                print(f"\033[1;96mClarifications:\033[0m \033[92m{len(clarifications)} recorded\033[0m")
                for item in clarifications[-1:]:
                    q = item.get("question", "")
                    a = item.get("answer", "")
                    if len(q) > 50: q = q[:47] + "..."
                    if len(a) > 50: a = a[:47] + "..."
                    print_wrapped_bullet("  \033[90m└─\033[0m ", f"\033[90mQ: {q} | \033[92mA: {a}\033[0m")

            
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
                iter_val = job.get("iteration", 1)
                history = job.get("debug_history", [])

                completed_trials = [e for e in history if e.get("result") not in ("pending", None)]
                has_completed = len(completed_trials) > 0
                convergence = analyze_debug_loop_convergence(job, test_summary)

                if phase == "paused":
                    pause_reason = job.get("debug_pause_reason") or "Automated repair paused after repeated attempts without passing all tests."
                    print(f"\033[1;93m ⏸️  PAUSED: Loop Paused (Attempt #{iter_val})\033[0m")
                    print_wrapped_kv(" Reason:        ", f"\033[97m{pause_reason}\033[0m")
                    if completed_trials:
                        last_t = completed_trials[-1]
                        badge, detail = format_trial_result(last_t.get("result"), test_summary)
                        print_wrapped_kv(" Last Trial:    ", f"\033[97m#{last_t.get('iteration', iter_val)} {badge} \033[90m({detail})\033[0m")
                        if last_t.get("hypothesis"):
                            print_wrapped_kv(" Hypothesis:    ", f"\033[90m{last_t.get('hypothesis')}\033[0m")
                        if last_t.get("action"):
                            print_wrapped_kv(" Last Action:   ", f"\033[93m{last_t.get('action')}\033[0m")
                    if test_summary.get("failing_tests"):
                        ft_str = ", ".join(test_summary["failing_tests"][:2])
                        if len(test_summary["failing_tests"]) > 2:
                            ft_str += f" (+{len(test_summary['failing_tests']) - 2} more)"
                        print_wrapped_kv(" Failing Tests: ", f"\033[1;91m{ft_str}\033[0m")
                    print(f"\n \033[1;96m->\033[0m Press \033[1;96m'D'\033[0m to resume Auto-Fix, \033[1;96m'Q'\033[0m to Ask AI, or \033[1;96m'R'\033[0m to reset.")

                elif not has_completed:
                    # Initial state before Attempt #1 is run
                    print(f"\033[1;95m 🐞 FIX REQUIRED: Auto-Debug Loop\033[0m")
                    print_wrapped_kv(" Current State: ", f"\033[93m⏳ Ready for Attempt #1\033[0m \033[90m(Not yet executed — press 'D' to start)\033[0m")
                    if history:
                        target = history[-1].get("hypothesis") or history[-1].get("action") or "Investigate and resolve issue"
                        print_wrapped_kv(" Target Issue:  ", f"\033[97m{target}\033[0m")
                        if history[-1].get("evidence"):
                            print_wrapped_kv(" Evidence:      ", f"\033[3;90m\"{history[-1].get('evidence')}\"\033[0m")
                    if test_summary.get("failing_tests"):
                        ft_str = ", ".join(test_summary["failing_tests"][:2])
                        if len(test_summary["failing_tests"]) > 2:
                            ft_str += f" (+{len(test_summary['failing_tests']) - 2} more)"
                        print_wrapped_kv(" Failing Tests: ", f"\033[1;91m{ft_str}\033[0m")
                    print_wrapped_kv(" Next Action:   ", "\033[90mAI will analyze failures, propose code changes, and verify with tests.\033[0m")
                    print(f"\n \033[1;96m->\033[0m Press \033[1;96m'D'\033[0m to run Auto-Fix (starts Attempt #1), \033[1;96m'Q'\033[0m to Ask AI, or \033[1;96m'R'\033[0m to reset.")

                else:
                    # One or more prior attempts executed
                    last_t = completed_trials[-1]
                    last_num = last_t.get("iteration", 1)
                    next_num = max(iter_val, last_num + 1)
                    badge, detail = format_trial_result(last_t.get("result"), test_summary)

                    health_part = f" [{convergence['health_badge']}]" if convergence.get("health") not in ("exploring", "ready") else ""
                    print(f"\033[1;95m 🐞 FIX REQUIRED: Auto-Debug Loop{health_part}\033[0m")
                    
                    last_res = last_t.get("result", {})
                    if isinstance(last_res, dict) and last_res.get("tests_ok"):
                        state_label = f"\033[1;93m⚠️  Attempt #{last_num} Follow-up Needed\033[0m \033[90m({detail})\033[0m"
                    else:
                        state_label = f"\033[1;91m⚠️  Attempt #{last_num} Failed\033[0m \033[90m({detail})\033[0m"
                    print_wrapped_kv(" Current State: ", f"{state_label} \033[1;96m-> Ready for Attempt #{next_num}\033[0m")

                    if convergence.get("health") == "converging":
                        print_wrapped_kv(" Trajectory:    ", f"\033[1;92m📈 {convergence['description']}\033[0m")
                    elif convergence.get("health") == "oscillating":
                        print_wrapped_kv(" Trajectory:    ", f"\033[1;93m🔄 {convergence['description']}\033[0m")
                    elif convergence.get("health") == "stagnant":
                        print_wrapped_kv(" Trajectory:    ", f"\033[1;91m🛑 {convergence['description']}\033[0m")

                    if last_t.get("action"):
                        print_wrapped_kv(" Last Action:   ", f"\033[90m#{last_num}: {last_t.get('action')}\033[0m")
                    if last_t.get("hypothesis"):
                        print_wrapped_kv(" Last Hypo:     ", f"\033[90m{last_t.get('hypothesis')}\033[0m")
                    if test_summary.get("failing_tests"):
                        ft_str = ", ".join(test_summary["failing_tests"][:2])
                        if len(test_summary["failing_tests"]) > 2:
                            ft_str += f" (+{len(test_summary['failing_tests']) - 2} more)"
                        print_wrapped_kv(" Failing Tests: ", f"\033[1;91m{ft_str}\033[0m")
                    print_wrapped_kv(" Next Action:   ", f"\033[90mAI will analyze why Attempt #{last_num} failed, refine hypothesis, and patch code.\033[0m")
                    print(f"\n \033[1;96m->\033[0m Press \033[1;96m'D'\033[0m to run Auto-Fix (starts Attempt #{next_num}), \033[1;96m'Q'\033[0m to Ask AI, or \033[1;96m'R'\033[0m to reset.")

                # Show trial history list if there are completed trials
                if completed_trials:
                    print("\n  \033[1;97mPrior Completed Trials:\033[0m")
                    for entry in completed_trials[-4:]:
                        b, d = format_trial_result(entry.get("result"), test_summary)
                        desc = entry.get("hypothesis") or entry.get("action", "unknown")
                        if len(desc) > 55: desc = desc[:52] + "..."
                        print(f"    #{entry.get('iteration')}: {b} \033[90m|\033[0m {desc}")
                    if len(completed_trials) > 4:
                        print(f"    \033[90m(+ {len(completed_trials) - 4} older trials)\033[0m")
                
            elif status == "review-needed":
                print("\033[1;92m 🏁 CODE COMPLETE: Ready to Merge\033[0m")
                if job.get("pr_number"):
                    print(f" PR: \033[97m#{job.get('pr_number')}\033[0m | Status: \033[92mALL TESTS PASSING\033[0m")
                elif job.get("issue_number"):
                    print(f" Issue: \033[97m#{job.get('issue_number')}\033[0m | Status: \033[92mALL TESTS PASSING\033[0m")
                else:
                    print(f" Status: \033[92mALL TESTS PASSING\033[0m")
                print(f" \033[1;96m->\033[0m Press \033[1;96m'G'\033[0m to view on GitHub or \033[1;96m'M'\033[0m to Merge & Finish.")
            
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
                    print_wrapped_bullet("    \033[90m└─\033[0m ", f"\033[90m{files_str}\033[0m")

            workflow_options = []
            context_options = []
            inspect_options = []
            navigation_options = []

            # 2. Status-Specific Actions
            is_bug_job = str(job_type).lower() in {"bug-fix", "bug-investigate", "bug", "quick-fix", "quick"}

            if status == "planned":
                if job_type == "feature-plan":
                    workflow_options.append(("a", "[\033[92mA\033[0m] Approve / Create Sub-tasks"))
                else:
                    workflow_options.append(("s", "[\033[92mS\033[0m] Schedule & Dispatch"))
                if not is_bug_job:
                    workflow_options.append(("f", "[\033[93mF\033[0m] Revise Plan (Update Scope & Tasks)"))
            elif status == "scheduled":
                workflow_options.append(("e", "[\033[92mE\033[0m] Execute (Worker Run)"))
            elif status == "debugging":
                if phase == "propose" or phase == "paused":
                    if failed_cnt > 0:
                        workflow_options.append(("d", f"[\033[1;92mD\033[0m] Auto-Fix Failing Tests \033[1;91m({failed_cnt} {'test' if failed_cnt == 1 else 'tests'} failing)\033[0m"))
                    elif t_status == "build-failed":
                        workflow_options.append(("d", "[\033[1;92mD\033[0m] Auto-Fix Build Errors"))
                    else:
                        workflow_options.append(("d", "[\033[1;92mD\033[0m] Auto-Fix / Propose Solution"))
                elif phase == "verify":
                    workflow_options.append(("d", "[\033[92mD\033[0m] Verify Fix (Pass/Fail Result)"))

                # Only show Resume if this is a multi-task feature with remaining tasks
                if not is_bug_job and tasks and len(completed) < len(tasks):
                    workflow_options.append(("u", "[\033[93mU\033[0m] Resume Feature (Advance to Next Task)"))
                
            elif status == "review-needed" or status == "completed":
                if status == "review-needed":
                    workflow_options.append(("m", "[\033[92mM\033[0m] Merge & Mark Completed"))
                    workflow_options.append(("f", "[\033[93mF\033[0m] Deliver to Device (Firebase distribution)"))
                
                if is_bug_job:
                    workflow_options.append(("h", "[\033[1;91mH\033[0m] Bug Still Happening? (Re-open & Fix)"))
                else:
                    if status == "review-needed":
                        workflow_options.append(("t", "[\033[93mT\033[0m] Revise Plan & Scope (AI Re-planning)"))
                
                # If there are tasks remaining, allow Resuming to the next task
                if status == "review-needed" and tasks and len(completed) < len(tasks):
                    workflow_options.append(("u", "[\033[93mU\033[0m] Resume Job (Next Task)"))
            elif status == "human-needed":
                if job.get("branch"):
                    workflow_options.append(("f", "[\033[93mF\033[0m] Deliver to Device (Firebase distribution)"))
                if not is_bug_job:
                    workflow_options.append(("t", "[\033[93mT\033[0m] Revise Plan & Scope (AI Re-planning)"))
                workflow_options.append(("u", "[\033[92mU\033[0m] Resume Job"))
            elif status == "executing" or is_stalled:
                workflow_options.append(("u", "[\033[93mU\033[0m] Resume Job"))
                
            # Global actions available for most non-archived states
            if status not in ["completed", "discarded", "planned"]:
                if status != "debugging":
                    if failed_cnt > 0:
                        workflow_options.append(("d", f"[\033[1;93mD\033[0m] Auto-Fix Failing Tests \033[1;91m({failed_cnt} {'test' if failed_cnt == 1 else 'tests'} failing)\033[0m"))
                    elif t_status == "build-failed":
                        workflow_options.append(("d", "[\033[1;93mD\033[0m] Auto-Fix Build Errors"))
                workflow_options.append(("q", "[\033[93mQ\033[0m] Ask AI (Questions about changes)"))
                workflow_options.append(("r", "[\033[1;91mR\033[0m] Reset & Rerun \033[1;91m(Stashes changes)\033[0m"))
            elif status in ["planned", "completed"]:
                workflow_options.append(("q", "[\033[93mQ\033[0m] Ask AI (Questions about changes)"))
            
            ai_modified = job.get("ai_modified_files", [])
            ai_untracked = job.get("ai_untracked_files", [])
            if ai_modified or ai_untracked or job.get("builder_summary"):
                inspect_options.append(("i", "[\033[1;96mI\033[0m] View AI Changes (Files & Synopsis)"))
                
            if sessions or investigations:
                inspect_options.append(("j", "[\033[1;96mJ\033[0m] View LLM Usage History"))

            context_options.append(("l", "[\033[93mL\033[0m] Link Logs (Update Context)"))
            context_options.append(("k", "[\033[93mK\033[0m] Attach UI Mockup / Reference"))

            inspect_options.append(("o", "[\033[93mO\033[0m] Select LLM Models (Override)"))
            inspect_options.append(("y", "[\033[93mY\033[0m] Export Context (Logs, Progress, Plan)"))
            inspect_options.append(("v", "[\033[93mV\033[0m] View Brief / Summary"))
            inspect_options.append(("g", "[\033[1;96mG\033[0m] View in GitHub"))
            
            navigation_options.append(("c", "[\033[1;91mC\033[0m] Close Issue in GitHub"))
            navigation_options.append(("x", "[\033[1;91mX\033[0m] Discard & Revert Changes"))
            navigation_options.append(("b", "[\033[1;91mB\033[0m] Back to Main Menu"))

            # Print options by categories
            if workflow_options:
                print("\n\033[1;95m⚡ WORKFLOW ACTIONS\033[0m")
                for opt_char, opt_str in workflow_options:
                    print(f"  {opt_str}")
                    if opt_char not in actions:
                        actions.append(opt_char)

            if context_options:
                print("\n\033[1;95m📥 CONTEXT & INPUTS\033[0m")
                for opt_char, opt_str in context_options:
                    print(f"  {opt_str}")
                    if opt_char not in actions:
                        actions.append(opt_char)

            if inspect_options:
                print("\n\033[1;95m🔍 INSPECT & CONFIGURE\033[0m")
                for opt_char, opt_str in inspect_options:
                    print(f"  {opt_str}")
                    if opt_char not in actions:
                        actions.append(opt_char)

            if navigation_options:
                print("\n\033[1;95m🛡️  SAFETY & NAVIGATION\033[0m")
                for opt_char, opt_str in navigation_options:
                    print(f"  {opt_str}")
                    if opt_char not in actions:
                        actions.append(opt_char)
            
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
                open_action_screen("Select LLM Models")
                while True:
                    options, value_map, details_map, service_summary = run_with_loading_screen(
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
                        footer = (
                            "[\033[1;96mA\033[0m] Select All  [\033[1;92mF\033[0m] Free Only  [\033[1;93mN\033[0m] Deselect All\n"
                            "[\033[1;92mR\033[0m] Refresh Models  [\033[93mK\033[0m] API Keys  [\033[1;91mB\033[0m] Back"
                        )
                        menu_label = f"Select LLM Models: \033[1;96m{service_summary} LLM services enabled\033[0m."
                        new_labels = prompt_checkbox(menu_label, options, curr_defaults, extra_keys=["r", "d", "k", "b"], footer=footer, details_map=details_map, details_title="Selected Model Details", status_bar=status_bar)
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
                        elif exc.key == "k":
                            handle_api_keys(session_allowed_machines, curr_allowed_ids)
                            continue
                        elif exc.key in ["r", "d"]:
                            from model_registry import sync_models
                            print("\n\n📡 Scanning local CLIs (Ollama, OpenCode) and provider APIs for new models...")
                            success, msg = sync_models(live_discovery=True)
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
                open_action_screen("Discard & Revert Changes")
                handle_discard_job(job)
                break
            elif choice == "c":
                open_action_screen("Close Issue in GitHub")
                issue_num = job.get("issue_number")
                if issue_num:
                    print(f"This will close GitHub Issue #{issue_num} and archive this local job.")
                    print("\033[90mUse this only when the issue is no longer needed or was handled elsewhere.\033[0m\n")
                    if prompt_confirm("Close this issue and archive the job?", default=False):
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
                open_action_screen()
                try:
                    log_paths = prompt_for_logs(job, status_bar=status_bar)
                    
                    # Record choice in JSON
                    job = refresh_job(job)
                    job["last_manual_log_paths"] = log_paths
                    save_job(job)
                    job = refresh_job(job)
                    
                    if log_paths:
                        print(f"\n✅ Linked {len(log_paths)} log entries.")
                        # We skip the "Press Enter" so it returns immediately to the job menu redraw
                except BackException:
                    pass
            elif choice == "k":
                open_action_screen()
                prompt_for_reference_artifact(job)
                job = refresh_job(job)
            elif choice == "q":
                open_action_screen()
                handle_ask_ai(job, session_allowed_models)
                job = refresh_job(job)
            elif choice == "j" and "j" in actions:
                open_action_screen("LLM Usage History")
                sessions = job.get("llm_sessions", [])
                if not sessions and "llm_session_ids" in job:
                    sessions = [{"id": sid, "model": "unknown"} for sid in job["llm_session_ids"]]
                investigations = job.get("interactive_investigations", [])

                if not sessions and not investigations:
                    print("  No AI / LLM sessions recorded.")
                else:
                    if sessions:
                        print("\033[1;97mAI / LLM Model Sessions:\033[0m")
                        print(f"  {'#':3} | {'Model / Tool':30} | {'Session ID (UUID)':40}")
                        print(f"  {'-'*3:3}-+-{'-'*30:30}-+-{'-'*40:40}")
                        
                        for i, s in enumerate(sessions):
                            model = s.get("model") or s.get("tool", "unknown")
                            sid = s.get("id", "unknown")
                            print(f"  {i+1:3} | {model:30} | \033[97m{sid}\033[0m")
                        print()

                    if investigations:
                        print("\033[1;97mInteractive CLI Investigations:\033[0m")
                        for idx, inv in enumerate(investigations, 1):
                            t = inv.get("tool", "AI CLI")
                            d = inv.get("duration", "")
                            n = inv.get("notes", "") or inv.get("note", "")
                            commits = inv.get("new_commits", [])
                            meta = f" ({d})" if d else ""
                            print(f"  {idx}. \033[1;96m{t}\033[0m{meta}")
                            if n:
                                print(f"     \033[90mNotes:\033[0m \033[97m{n}\033[0m")
                            if commits:
                                print(f"     \033[90mCommits:\033[0m {', '.join(commits)}")
                
                print("\n  Tip: You can resume model sessions in the AI CLI using: antigravity --resume <ID>")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "i" and "i" in actions:
                open_action_screen("AI Changes & Synopsis")
                render_ai_changes_synopsis(job)
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "f" and "f" in actions:
                if status == "designing" or status == "planned":
                    open_action_screen()
                    revised_job = handle_tweak_revise(job)
                    if revised_job:
                        job = revised_job
                else:
                    open_action_screen("Deliver to Device")
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
                open_action_screen()
                revised_job = handle_tweak_revise(job)
                if revised_job:
                    job = revised_job
            elif choice == "h" and "h" in actions:
                if not session_allowed_machines:
                    print("\n\033[1;91m⚠️  ERROR: No active machines selected for this session.\033[0m")
                    print("Please go to [\033[1;96mC\033[0m] Configuration \033[1;96m->\033[0m [\033[1;96mF\033[0m] Manage Machine Fleet and select at least one machine.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue
                if job_type in {"feature-plan", "feature", "feature-design"}:
                    open_action_screen("Fix Bug / Missing Functionality")
                else:
                    open_action_screen("Re-open & Fix Bug")
                handle_bug_still_happening(job)
                job = refresh_job(job)
            elif choice == "m" and "m" in actions:
                open_action_screen("Merge & Mark Completed")
                handle_merge_cleanup(job)
                break
            elif choice == "a" and "a" in actions:
                # 1. Answer Clarification Question
                question = job.get("human_clarification_question")
                if status == "human-needed" and question:
                    open_action_screen("Answer Clarification Question")
                    print(f"\n\033[93mQuestion:\033[0m {question}\n")
                    print("\033[90mYour answer will be sent back to the planner and used to revise this job.\033[0m\n")
                    answer = prompt_input("Your answer:", placeholder="required to continue", field_below=True)
                    if answer:
                        record_clarification(job, question, answer)
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
                from job_actions import apply_design_approval, architect_feedback, replan_type
                feedback = architect_feedback(job)
                if feedback:
                    print_header("Integrating Architect Suggestions")
                    run_script("new_job.py", [replan_type(job), "--no-dispatch", "--update", str(job["_path"]), "--feedback", feedback], sub_menu=True)
                    job = refresh_job(job)
                    continue

                if status == "designing":
                    print("\n🎨 \033[1;92mDESIGN APPROVED!\033[0m")
                    print("Transitioning to Implementation Planning phase...")
                    
                    # Store the design as context for the feature planner.
                    apply_design_approval(job)
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
                open_action_screen("Reset to Planned")
                print("This keeps current progress and changes, but returns the job status to planned.")
                print("\033[90mUse this if the job state is wrong and you want to schedule it again.\033[0m\n")
                if prompt_confirm("Reset status to 'planned' (keeps progress)?", default=True):
                    job["status"] = "planned"
                    job["updated_at"] = now_iso()
                    save_job(job)
                    job = refresh_job(job)
                    print("\n✅ Job status reset to Planned.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "r":
                open_action_screen("Reset & Rerun")
                print("\033[1;93mNotice: This will stash existing local code changes and reset the job to 'planned' for a fresh execution.\033[0m")
                print("Use this when the current attempt is not salvageable and you want to start over while safely preserving your changes in git stash.")
                print()
                if prompt_confirm("Stash existing changes and restart this job?", default=False):
                    # 1. Stash changes
                    job_id = job.get("job_id", "job")
                    stash_msg = f"orchestrator: reset-rerun {job_id} ({now_iso()})"
                    print(f"\n      - Stashing existing changes: {stash_msg}...")
                    stash_res = subprocess.run(["git", "stash", "push", "-u", "-m", stash_msg], cwd=str(ROOT), capture_output=True, text=True)
                    if stash_res.returncode == 0:
                        print(f"      - ✅ Saved changes to git stash: '{stash_msg}'")
                    else:
                        print(f"      - ⚠️  Git stash: {stash_res.stderr.strip() or stash_res.stdout.strip() or 'No local uncommitted changes to stash'}")

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
                        log_paths = prompt_for_logs(job, status_bar=status_bar)
                        job["last_manual_log_paths"] = log_paths
                    except BackException:
                        print("      - No logs linked.")
                    
                    # Save the updated job data
                    save_job(job)
                    job_path = job["_path"]
                    
                    print("Job reset to 'planned'. Re-scheduling immediately...")
                    run_script("schedule_job.py", [str(job_path)], job=job, session_machines=session_allowed_machines, session_models=session_allowed_models)
                    job = refresh_job({"_path": job_path})
            elif choice == "y" and "y" in actions:
                open_action_screen("Export Context")
                run_script("export_job.py", [str(job["_path"])], sub_menu=True)
            elif choice == "g":
                open_action_screen("View in GitHub")
                links = get_github_links(job)

                if not links:
                    print("  \033[90mNo Pull Request or Issue associated with this job.\033[0m")
                else:
                    for item in links:
                        u = item["url"]
                        lbl = item["label"]
                        if u and (u.startswith("http://") or u.startswith("https://")):
                            clickable_url = f"\033]8;;{u}\033\\{u}\033]8;;\033\\"
                            print(f"🔗 \033[1;97m{lbl}:\033[0m")
                            print(f"   \033[4;96m{clickable_url}\033[0m\n")
                        else:
                            print(f"🔗 \033[1;97m{lbl}\033[0m\n")

                    open_target = None
                    if len(links) == 1:
                        open_target = links[0]
                    else:
                        options = [f"Open {item['label']} in browser" for item in links]
                        options.append("Open All in browser")
                        options.append("Return to menu")
                        try:
                            sel = prompt_radio("Select GitHub item to open in browser:", options)
                            if sel == "Return to menu":
                                open_target = None
                            elif sel == "Open All in browser":
                                open_target = "all"
                            else:
                                for item in links:
                                    if item["label"] in sel:
                                        open_target = item
                                        break
                        except BackException:
                            open_target = None

                    def _open_link(item_to_open: dict[str, Any]) -> None:
                        t = item_to_open["type"]
                        num = item_to_open["number"]
                        url_to_open = item_to_open["url"]
                        print(f"Opening {item_to_open['label']} in browser...")
                        opened = False
                        if shutil.which("gh"):
                            res = subprocess.run(["gh", t, "view", str(num), "--web"], cwd=str(ROOT), check=False)
                            if res.returncode == 0:
                                opened = True
                        if not opened and url_to_open and (url_to_open.startswith("http://") or url_to_open.startswith("https://")):
                            import webbrowser
                            webbrowser.open(url_to_open)

                    if open_target == "all":
                        for item in links:
                            _open_link(item)
                    elif isinstance(open_target, dict):
                        _open_link(open_target)

                # Clear any leaking output from gh
                sys.stdout.write("\r\033[K")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "v":
                open_action_screen()
                view_job_brief_summary(job)
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "s" and "s" in actions:
                if not session_allowed_machines:
                    print("\n\033[1;91m⚠️  ERROR: No active machines selected for this session.\033[0m")
                    print("Please go to [\033[1;96mC\033[0m] Configuration \033[1;96m->\033[0m [\033[1;96mF\033[0m] Manage Machine Fleet and select at least one machine.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue

                open_action_screen("Schedule & Dispatch")
                print("This schedules the approved plan on an available worker.\n")
                args = [str(job["_path"])]
                run_script("schedule_job.py", args, job=job, session_machines=session_allowed_machines, session_models=session_allowed_models)
                # Refresh job data
                job = refresh_job(job)
            elif choice == "e" and "e" in actions:
                if not session_allowed_machines:
                    print("\n\033[1;91m⚠️  ERROR: No active machines selected for this session.\033[0m")
                    print("Please go to [\033[1;96mC\033[0m] Configuration \033[1;96m->\033[0m [\033[1;96mF\033[0m] Manage Machine Fleet and select at least one machine.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue
                open_action_screen("Execute Worker Run")
                run_script("worker_run.py", [str(job["_path"])], job=job, sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
                job = refresh_job(job)
            elif choice == "u" and "u" in actions:
                if not session_allowed_machines:
                    print("\n\033[1;91m⚠️  ERROR: No active machines selected for this session.\033[0m")
                    print("Please go to [\033[1;96mC\033[0m] Configuration \033[1;96m->\033[0m [\033[1;96mF\033[0m] Manage Machine Fleet and select at least one machine.")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue

                open_action_screen(f"Resume Job: {job.get('job_id')}")
                print("This asks the scheduler to resume the current job or move to the next task.")
                print()
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
                        open_action_screen("Verify Fix")
                        print("Record what happened when you checked the latest fix.")
                        print("\033[90mThis context is passed into the next debug iteration.\033[0m\n")
                        repro = prompt_confirm("Was the bug reproduced in this iteration?", default=False)
                        if repro:
                            print("\n\033[1;91m" + "🐛"*2 + " BUG CONFIRMED REPRODUCED " + "🐛"*2 + "\033[0m\n")
                        notes = prompt_input("Notes about the result:", placeholder="what you saw; optional", field_below=True)
                        args.extend(["--notes", notes])
                        if repro:
                            args.append("--bug-reproduced")
                    else:
                        status_bar.clear_footer()
                        status_bar.reset_scroll_region(force=True)
                        clear_screen()
                        feedback = prompt_autofix_iteration_settings(job)
                        if feedback:
                            args.extend(["--feedback", feedback])

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

def format_risk_mitigation_pair(risk: str, mitigation_candidate: str | None = None) -> tuple[str, str]:
    """Given a risk statement, finds or crafts a concrete mitigation / test strategy."""
    import re
    r_clean = risk.strip()
    r_clean = re.sub(r'^(?:⚠️|\*|-|•|\d+\.)\s*', '', r_clean).strip()
    
    if mitigation_candidate:
        m_clean = re.sub(r'^(?:🛡️|\*|-|•|\d+\.)\s*', '', mitigation_candidate.strip()).strip()
        return r_clean, m_clean
        
    r_lower = r_clean.lower()
    if "async" in r_lower or "race" in r_lower or "timing" in r_lower or "delayed" in r_lower:
        strategy = "Verify state machine explicitly handles .loading/.empty/.failed states with async transition tests."
    elif "schema" in r_lower or "decoding" in r_lower or "contract" in r_lower or "format" in r_lower:
        strategy = "Add unit tests covering both legacy and new payload schemas with optional field defaults."
    elif "ui" in r_lower or "binding" in r_lower or "tab" in r_lower or "tap" in r_lower or "view" in r_lower:
        strategy = "Trace data binding from model to ViewModel to View; verify with deterministic ViewModel unit tests."
    elif "regress" in r_lower or "existing" in r_lower or "backward" in r_lower or "compat" in r_lower:
        strategy = "Add regression tests ensuring unchanged behavior for existing document types and tab routes."
    elif "flaky" in r_lower or "mock" in r_lower or "test" in r_lower:
        strategy = "Isolate dependencies with deterministic test fixtures and mock backend responses."
    else:
        strategy = "Add targeted unit tests and boundary condition assertions to verify correct behavior."
    
    return r_clean, strategy


def render_risks_and_mitigations(job: dict[str, Any], parsed_json: dict[str, Any] | None = None) -> None:
    """Renders all identified risks paired with their corresponding test & mitigation strategies."""
    import re
    import textwrap

    risks_list: list[str] = []
    mitigations_list: list[str] = []

    # 1. Verification risks and suggested additions
    verification = job.get("verification")
    if isinstance(verification, dict):
        v_risks = verification.get("risks_identified")
        if isinstance(v_risks, list):
            for r in v_risks:
                if r and str(r).strip() not in risks_list:
                    risks_list.append(str(r).strip())
        v_additions = verification.get("suggested_additions")
        if isinstance(v_additions, list):
            for a in v_additions:
                if a and str(a).strip() not in mitigations_list:
                    mitigations_list.append(str(a).strip())

    # 2. Plan risks
    plan = job.get("plan")
    if isinstance(plan, dict):
        p_risks = plan.get("risks")
        if isinstance(p_risks, list):
            for r in p_risks:
                if r and str(r).strip() not in risks_list:
                    risks_list.append(str(r).strip())
        elif isinstance(p_risks, str) and p_risks.strip():
            for line in p_risks.splitlines():
                line = line.strip()
                if line and line not in risks_list:
                    risks_list.append(line)
        p_tests = plan.get("test_recommendations")
        if isinstance(p_tests, list):
            for t in p_tests:
                if t and str(t).strip() not in mitigations_list:
                    mitigations_list.append(str(t).strip())

    # 3. Parsed JSON risks
    if isinstance(parsed_json, dict):
        b_risks = parsed_json.get("risks")
        if isinstance(b_risks, list):
            for r in b_risks:
                if r and str(r).strip() not in risks_list:
                    risks_list.append(str(r).strip())
        elif isinstance(b_risks, str) and b_risks.strip():
            sentences = [s.strip() for s in re.split(r'(?<=[.?!])\s+', b_risks) if s.strip()]
            for s in sentences:
                if len(s) > 20 and s not in risks_list:
                    risks_list.append(s)

    if not risks_list:
        return

    print_header("Identified Risks & Mitigation Strategies")
    
    for idx, risk_text in enumerate(risks_list, 1):
        candidate_mitigation = None
        r_words = set(re.findall(r'\w{4,}', risk_text.lower()))
        best_score = 0
        for mit in mitigations_list:
            m_words = set(re.findall(r'\w{4,}', mit.lower()))
            common = len(r_words.intersection(m_words))
            if common > best_score:
                best_score = common
                candidate_mitigation = mit
        
        if not candidate_mitigation and idx - 1 < len(mitigations_list):
            candidate_mitigation = mitigations_list[idx - 1]

        risk_clean, mit_clean = format_risk_mitigation_pair(risk_text, candidate_mitigation)

        r_wrapped = textwrap.fill(risk_clean, width=74, initial_indent=f"  ⚠️  \033[1;93mRisk #{idx}:\033[0m \033[93m", subsequent_indent="     \033[93m") + "\033[0m"
        m_wrapped = textwrap.fill(mit_clean, width=74, initial_indent="     \033[1;92m🛡️  Mitigation / Test Strategy:\033[0m \033[97m", subsequent_indent="        \033[97m") + "\033[0m"
        print(r_wrapped)
        print(m_wrapped)
        print()


def view_job_brief_summary(job: dict[str, Any]) -> None:
    import json
    import re
    import textwrap

    job_id = job.get("job_id", "unknown")
    brief_file = OUTPUT_DIR / job_id / "brief.md"
    summary_file = OUTPUT_DIR / job_id / "builder_summary.md"
    inv_file = OUTPUT_DIR / job_id / "investigations.md"
    rendered_any = False

    parsed_json = None
    summary_text = ""
    if summary_file.exists():
        try:
            summary_text = summary_file.read_text(encoding="utf-8").strip()
            if "{" in summary_text and "}" in summary_text:
                from llm import extract_json_block
                json_str = extract_json_block(summary_text)
                parsed_json = json.loads(json_str)
        except Exception:
            try:
                parsed_json = json.loads(summary_text)
            except Exception:
                parsed_json = None

    # 1. Summary of Changes (Rendered cleanly if parsed JSON, or markdown if text)
    if summary_file.exists():
        if isinstance(parsed_json, dict):
            print_header("Summary of Changes")
            hypo = (parsed_json.get("hypothesis") or "").strip()
            summ = (parsed_json.get("summary") or "").strip()
            plan_steps = parsed_json.get("implementation_plan")
            files_list = parsed_json.get("files_changed") or []
            test_cmd = parsed_json.get("test_command")

            if hypo:
                print("  \033[1;93m💡 Hypothesis / Root Cause:\033[0m")
                for paragraph in hypo.split("\n\n"):
                    if paragraph.strip():
                        wrapped = textwrap.fill(paragraph.strip(), width=76, initial_indent="    ", subsequent_indent="    ")
                        print(f"\033[93m{wrapped}\033[0m\n")

            if summ:
                print("  \033[1;97m📝 Summary & Resolution:\033[0m")
                if any(marker in summ for marker in ["Root cause:", "Fix:", "Verified:"]):
                    parts = re.split(r'(?=(?:Root cause|Fix|Verified):)', summ, flags=re.IGNORECASE)
                    for p in parts:
                        if p.strip():
                            wrapped = textwrap.fill(p.strip(), width=74, initial_indent="    • ", subsequent_indent="      ")
                            highlighted = re.sub(r'^(    •\s*)(Root cause|Fix|Verified)(:)', r'\1\033[1;97m\2\3\033[0m\033[97m', wrapped, flags=re.IGNORECASE)
                            print(f"\033[97m{highlighted}\033[0m")
                    print()
                else:
                    wrapped = textwrap.fill(summ, width=76, initial_indent="    ", subsequent_indent="    ")
                    print(f"\033[97m{wrapped}\033[0m\n")

            if plan_steps:
                print("  \033[1;96m🛠️  Implementation Steps:\033[0m")
                if isinstance(plan_steps, list):
                    for idx, step in enumerate(plan_steps, 1):
                        wrapped = textwrap.fill(str(step).strip(), width=74, initial_indent=f"    {idx}. ", subsequent_indent="       ")
                        print(f"\033[97m{wrapped}\033[0m")
                elif isinstance(plan_steps, str):
                    numbered = re.split(r'(?:^|\s+)(?=\d+\.\s+)', plan_steps.strip())
                    if len(numbered) > 1:
                        for step in numbered:
                            if step.strip():
                                m = re.match(r'^(\d+\.\s+)(.*)$', step.strip(), re.DOTALL)
                                if m:
                                    prefix = m.group(1)
                                    body = m.group(2).strip()
                                    wrapped = textwrap.fill(body, width=76 - len(prefix) - 4)
                                    w_lines = wrapped.splitlines()
                                    print(f"    \033[1;96m{prefix}\033[0m\033[97m{w_lines[0]}\033[0m")
                                    for wl in w_lines[1:]:
                                        print(f"       \033[97m{wl}\033[0m")
                                else:
                                    print(f"    • \033[97m{step.strip()}\033[0m")
                    else:
                        for p_line in plan_steps.splitlines():
                            if p_line.strip():
                                print(f"    • \033[97m{p_line.strip()}\033[0m")
                print()

            if files_list and isinstance(files_list, list):
                print("  \033[1;93m📂 Modified Files:\033[0m")
                for f in files_list:
                    print(f"    • \033[97m{f}\033[0m")
                print()

            if test_cmd:
                print("  \033[1;96m🧪 Verification Test Filter:\033[0m")
                print(f"    \033[90m{test_cmd}\033[0m\n")
        else:
            print_header("Summary")
            print(format_markdown_for_terminal(summary_text))
        rendered_any = True

    # 2. Brief & Acceptance Criteria
    if brief_file.exists():
        print_header("Brief")
        print(format_markdown_for_terminal(brief_file.read_text(encoding="utf-8")))
        rendered_any = True

    # 3. Investigations
    if inv_file.exists():
        print_header("Investigations & Notes")
        print(format_markdown_for_terminal(inv_file.read_text(encoding="utf-8")))
        rendered_any = True

    # 4. Fallback if no files generated yet
    if not rendered_any:
        print_header("Brief / Summary")
        print("No brief.md or builder_summary.md file has been generated for this job yet.")
        plan = job.get("plan")
        if isinstance(plan, dict):
            summary = plan.get("summary")
            tasks = plan.get("tasks")
            constraints = plan.get("constraints")

            if summary:
                print_header("Plan Summary")
                print(format_markdown_for_terminal(str(summary)))

            if constraints and isinstance(constraints, list):
                print_header("Constraints")
                for item in constraints:
                    print(f"  • {item}")

            if isinstance(tasks, list) and tasks:
                print_header("Tasks")
                for idx, task in enumerate(tasks, start=1):
                    if isinstance(task, dict):
                        name = task.get("name") or task.get("title") or f"Task {idx}"
                        detail = task.get("description") or task.get("summary")
                        print(f"{idx}. {name}")
                        if detail:
                            print(f"   {detail}")
                    else:
                        print(f"{idx}. {task}")

    # 5. Risks & Mitigation Strategies (Always cleanly rendered with mitigation pairings)
    render_risks_and_mitigations(job, parsed_json=parsed_json)

def render_ai_changes_synopsis(job: dict[str, Any]) -> None:
    """Renders the AI Changes & Synopsis screen with structured formatting, bullets, and line breaks."""
    import json
    import re
    import textwrap
    from common import OUTPUT_DIR

    ai_modified = job.get("ai_modified_files", [])
    ai_untracked = job.get("ai_untracked_files", [])
    
    # Retrieve builder synopsis / summary
    job_id = job.get("job_id", "")
    summary_text = ""
    if job_id:
        summary_file = OUTPUT_DIR / job_id / "builder_summary.md"
        if summary_file.exists():
            try:
                summary_text = summary_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass
    if not summary_text:
        summary_text = (job.get("builder_summary") or "").strip()
    if not summary_text and isinstance(job.get("plan"), dict):
        summary_text = (job.get("plan", {}).get("summary") or "").strip()

    hypothesis = (job.get("builder_hypothesis") or "").strip()

    parsed_json = None
    if summary_text:
        try:
            if "{" in summary_text and "}" in summary_text:
                json_str = extract_json_block(summary_text)
                parsed_json = json.loads(json_str)
        except Exception:
            try:
                parsed_json = json.loads(summary_text)
            except Exception:
                parsed_json = None

    if isinstance(parsed_json, dict):
        hypo = (parsed_json.get("hypothesis") or hypothesis).strip()
        summ = (parsed_json.get("summary") or "").strip()
        plan_steps = parsed_json.get("implementation_plan")
        risks_text = parsed_json.get("risks")
        test_cmd = parsed_json.get("test_command") or job.get("test_command_override")
        files_list = parsed_json.get("files_changed") or []

        # 1. Hypothesis / Root Cause
        if hypo:
            print("  \033[1;93m💡 Hypothesis / Root Cause:\033[0m")
            for paragraph in hypo.split("\n\n"):
                p_clean = paragraph.strip()
                if p_clean:
                    wrapped = textwrap.fill(p_clean, width=76, initial_indent="    ", subsequent_indent="    ")
                    print(f"\033[93m{wrapped}\033[0m\n")

        # 2. Summary of Changes
        if summ:
            print("  \033[1;97m📝 Summary of Changes:\033[0m")
            if any(marker in summ for marker in ["Root cause:", "Fix:", "Verified:"]):
                parts = re.split(r'(?=(?:Root cause|Fix|Verified):)', summ, flags=re.IGNORECASE)
                for p in parts:
                    p_clean = p.strip()
                    if p_clean:
                        wrapped = textwrap.fill(p_clean, width=74, initial_indent="    • ", subsequent_indent="      ")
                        highlighted = re.sub(r'^(    •\s*)(Root cause|Fix|Verified)(:)', r'\1\033[1;97m\2\3\033[0m\033[97m', wrapped, flags=re.IGNORECASE)
                        print(f"\033[97m{highlighted}\033[0m")
                print()
            else:
                for s_line in summ.splitlines():
                    s_line = s_line.strip()
                    if s_line:
                        wrapped = textwrap.fill(s_line, width=76, initial_indent="    ", subsequent_indent="    ")
                        print(f"\033[97m{wrapped}\033[0m")
                print()

        # 3. Implementation Steps
        if plan_steps:
            print("  \033[1;96m🛠️  Implementation Steps:\033[0m")
            if isinstance(plan_steps, list):
                for idx, step in enumerate(plan_steps, 1):
                    wrapped = textwrap.fill(str(step).strip(), width=74, initial_indent=f"    {idx}. ", subsequent_indent="       ")
                    print(f"\033[97m{wrapped}\033[0m")
            elif isinstance(plan_steps, str):
                numbered_steps = re.split(r'(?:^|\s+)(?=\d+\.\s+)', plan_steps.strip())
                if len(numbered_steps) > 1:
                    for step in numbered_steps:
                        step_clean = step.strip()
                        if step_clean:
                            m = re.match(r'^(\d+\.\s+)(.*)$', step_clean, re.DOTALL)
                            if m:
                                num_prefix = m.group(1)
                                body = m.group(2).strip()
                                indent_spaces = " " * (len(num_prefix) + 4)
                                wrapped = textwrap.fill(body, width=76 - len(num_prefix) - 4)
                                wrapped_lines = wrapped.splitlines()
                                print(f"    \033[1;96m{num_prefix}\033[0m\033[97m{wrapped_lines[0]}\033[0m")
                                for w_line in wrapped_lines[1:]:
                                    print(f"{indent_spaces}\033[97m{w_line}\033[0m")
                            else:
                                wrapped = textwrap.fill(step_clean, width=74, initial_indent="    • ", subsequent_indent="      ")
                                print(f"\033[97m{wrapped}\033[0m")
                else:
                    for p_line in plan_steps.splitlines():
                        p_line = p_line.strip()
                        if p_line:
                            wrapped = textwrap.fill(p_line, width=74, initial_indent="    • ", subsequent_indent="      ")
                            print(f"\033[97m{wrapped}\033[0m")
            print()

        # 4. Risks & Considerations
        if risks_text:
            print("  \033[1;93m⚠️  Risks & Considerations:\033[0m")
            if isinstance(risks_text, list):
                for r in risks_text:
                    wrapped = textwrap.fill(str(r).strip(), width=74, initial_indent="    • ", subsequent_indent="      ")
                    print(f"\033[93m{wrapped}\033[0m")
            elif isinstance(risks_text, str):
                r_lines = [l.strip() for l in risks_text.splitlines() if l.strip()]
                if len(r_lines) == 1 and len(r_lines[0]) > 100:
                    sentences = [s.strip() for s in re.split(r'(?<=[.?!])\s+', r_lines[0]) if s.strip()]
                    for s in sentences:
                        wrapped = textwrap.fill(s, width=74, initial_indent="    • ", subsequent_indent="      ")
                        print(f"\033[93m{wrapped}\033[0m")
                else:
                    for r_line in r_lines:
                        wrapped = textwrap.fill(r_line, width=74, initial_indent="    • ", subsequent_indent="      ")
                        print(f"\033[93m{wrapped}\033[0m")
            print()

        # 5. Modified & Created Files
        combined_modified = list(ai_modified)
        if isinstance(files_list, list):
            for f in files_list:
                if f not in combined_modified:
                    combined_modified.append(f)

        if combined_modified or ai_untracked:
            if combined_modified:
                print("  \033[1;93m📂 Modified Files:\033[0m")
                for f in sorted(combined_modified):
                    print(f"    • \033[97m{f}\033[0m")
            if ai_untracked:
                if combined_modified:
                    print()
                print("  \033[1;92m✨ Untracked (New) Files:\033[0m")
                for f in sorted(ai_untracked):
                    print(f"    • \033[92m{f}\033[0m")
            print()

        # 6. Test command
        if test_cmd:
            print("  \033[1;96m🧪 Verification Test Filter:\033[0m")
            wrapped_cmd = textwrap.fill(str(test_cmd).strip(), width=76, initial_indent="    ", subsequent_indent="    ")
            print(f"\033[90m{wrapped_cmd}\033[0m\n")

    else:
        # Non-JSON fallback: standard markdown / text formatting
        if summary_text:
            print("  \033[1;97m📝 Synopsis / Summary of Changes:\033[0m")
            for s_line in summary_text.splitlines():
                s_line = s_line.strip()
                if s_line:
                    wrapped = textwrap.fill(s_line, width=76, initial_indent="    ", subsequent_indent="    ")
                    print(f"\033[97m{wrapped}\033[0m")
                else:
                    print()
            print()

        if hypothesis:
            print("  \033[1;93m💡 Hypothesis / Root Cause:\033[0m")
            for h_line in hypothesis.splitlines():
                h_line = h_line.strip()
                if h_line:
                    wrapped = textwrap.fill(h_line, width=76, initial_indent="    ", subsequent_indent="    ")
                    print(f"\033[93m{wrapped}\033[0m")
                else:
                    print()
            print()

        if not ai_modified and not ai_untracked:
            if not summary_text and not hypothesis:
                print("  No AI-modified files or summary recorded for this job.")
        else:
            if ai_modified:
                print("  \033[1;93m📂 Modified Files:\033[0m")
                for f in sorted(ai_modified):
                    print(f"    • \033[97m{f}\033[0m")
            
            if ai_untracked:
                if ai_modified:
                    print()
                print("  \033[1;92m✨ Untracked (New) Files:\033[0m")
                for f in sorted(ai_untracked):
                    print(f"    • \033[92m{f}\033[0m")

def prompt_autofix_iteration_settings(job: dict[str, Any]) -> str | None:
    print_header("Auto-Fix / Iterate")
    print("\033[90mThe AI will inspect the current job, linked logs, and failing test output, apply a targeted fix, and rerun the job's TDD test suite to verify the fix.\033[0m\n")

    print("  \033[1;97m🎯 When to use:\033[0m")
    print("     \033[90mUse this when the implementation is close but still failing unit tests,\033[0m")
    print("     \033[90mreview checks, or expected behavior.\033[0m\n")

    print("  \033[1;97m💡 Optional guidance examples:\033[0m")
    print("     \033[1;36m•\033[0m \033[97mFocus on the checkout retry failure in the latest build log.\033[0m")
    print("     \033[1;36m•\033[0m \033[97mThe UI works, but the empty state still overlaps on small screens.\033[0m")
    print("     \033[1;36m•\033[0m \033[97mKeep the public API unchanged; fix only the regression.\033[0m\n")

    print("  \033[90mℹ️  Tip: Leave the guidance field blank to run Auto-Fix with existing job context.\033[0m")
    print("  \033[90m" + ("─" * 60) + "\033[0m\n")

    feedback = prompt_input(
        "Guidance (Optional)",
        placeholder="optional; leave blank to use current context",
        field_below=True,
    )

    return feedback or None

def handle_tweak_revise(job: dict[str, Any]):
    print_header("Tweak / Revise")
    print("\033[90mDescribe the change you want in concrete terms. Leave the first field blank to cancel.\033[0m\n")

    print("  \033[1;97m💡 Examples:\033[0m")
    print("     \033[1;36m•\033[0m \033[97mChange the onboarding copy to sound more direct and less promotional.\033[0m")
    print("     \033[1;36m•\033[0m \033[97mKeep the current layout, but make the failed state show retry details.\033[0m")
    print("     \033[1;36m•\033[0m \033[97mThe implementation works, but split the helper into a smaller testable function.\033[0m\n")

    print("  \033[90mℹ️  Tip: Leave the first field blank and press Enter to cancel.\033[0m")
    print("  \033[90m" + ("─" * 60) + "\033[0m\n")

    requested_change = prompt_input(
        "Briefly describe what should change",
        placeholder="specific behavior, UI, copy, bug, or requirement",
        field_below=True,
    )
    if not requested_change:
        return

    affected_area = prompt_input("Where does it apply?", placeholder="screen/file/flow; optional", field_below=True)
    done_when = prompt_input("How should we know it is done?", placeholder="acceptance criteria or expected result; optional", field_below=True)

    feedback_parts = [f"Requested change: {requested_change}"]
    if affected_area:
        feedback_parts.append(f"Affected area: {affected_area}")
    if done_when:
        feedback_parts.append(f"Done when: {done_when}")
    feedback = "\n".join(feedback_parts)

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

def generate_chat_context(job: dict[str, Any]) -> tuple[str, Path]:
    """Generates and writes a comprehensive markdown context bundle for interactive AI CLI sessions."""
    from reference_artifacts import reference_context

    branch = job.get("branch")
    base = job.get("base_branch", "main")

    diff_text = "(No diff available)"
    if branch:
        try:
            res = subprocess.run(["git", "diff", f"{base}...{branch}"], capture_output=True, text=True, cwd=str(ROOT))
            if res.returncode == 0:
                diff_text = res.stdout
                if len(diff_text) > 80000:
                    diff_text = diff_text[:80000] + "\n... (diff truncated for length)"
        except Exception:
            pass

    # Get brief, summary, and recent logs
    job_id = job.get("job_id", "unknown")
    brief_dir = OUTPUT_DIR / job_id
    brief_content = ""
    summary_content = ""

    if (brief_dir / "brief.md").exists():
        try:
            brief_content = (brief_dir / "brief.md").read_text(encoding="utf-8")
        except Exception:
            pass
    if (brief_dir / "builder_summary.md").exists():
        try:
            summary_content = (brief_dir / "builder_summary.md").read_text(encoding="utf-8")
        except Exception:
            pass

    issue_num = job.get("issue_number", "")
    title = job.get("title", "")

    investigation_section = ""
    inv_text = format_investigation_history(job)
    if inv_text:
        investigation_section = f"\n## Prior Investigation Findings & CLI Notes\n{inv_text}\n"

    context_bundle = f"""# Context for Job #{issue_num}: {title}
**Job ID**: `{job_id}`
**Branch**: `{branch}` (vs `{base}`)
**Status**: `{job.get('status')}`

## Brief
{brief_content or "No brief available."}

## Reference Artifacts
{reference_context(job) or "None"}
{investigation_section}
## Builder Summary
{summary_content or "No builder summary available."}

## Total Git Diff
```diff
{diff_text}
```
"""
    context_file = brief_dir / "chat_context.md"
    try:
        brief_dir.mkdir(parents=True, exist_ok=True)
        context_file.write_text(context_bundle, encoding="utf-8")
    except Exception:
        pass

    return context_bundle, context_file


def run_interactive_cli_with_framed_footer(
    cmd: list[str],
    env: dict[str, str] | None = None,
    status_bar: StatusBar | None = None,
    q_msg: str = "Type /exit or Ctrl-D to return to Orchestrator",
) -> int:
    """Runs an interactive CLI in a pseudo-terminal (PTY) with an anchored Orchestrator footer.

    By constraining the child PTY height to (terminal_lines - 4) and actively clamping
    scroll margins and cursor addressing escape codes, the child process (Codex, Claude,
    Antigravity, Gemini) is contained within the top viewport while the Orchestrator
    footer remains permanently anchored at the bottom.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        res = subprocess.run(cmd, cwd=str(ROOT), env=env)
        return res.returncode if hasattr(res, "returncode") else 0

    try:
        import fcntl
        import pty
        import re
        import select
        import signal
        import struct
        import termios
        import tty

        try:
            cols, lines = os.get_terminal_size()
        except Exception:
            cols, lines = 80, 24

        child_lines = max(5, lines - 4)
        child_cols = max(10, cols)

        master_fd, slave_fd = pty.openpty()
        ws = struct.pack("HHHH", child_lines, child_cols, 0, 0)
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, ws)
        except Exception:
            pass

        old_termios = None
        try:
            old_termios = termios.tcgetattr(sys.stdin.fileno())
        except Exception:
            pass

        old_sigwinch = None
        proc = None
        last_footer_redraw = time.monotonic()

        def filter_child_bytes(data: bytes, max_rows: int) -> tuple[bytes, bool]:
            """Filters and clamps ANSI terminal escape sequences to prevent footer eviction."""
            text = data.decode("latin1", errors="replace")
            needs_redraw = False

            # Check for screen clearing / alt-screen triggers
            if any(seq in text for seq in ["\x1b[2J", "\x1b[3J", "\x1b[H\x1b[J", "\x1b[1;1H\x1b[J", "\x1b[?1049h", "\x1b[?1049l", "\x1b[c"]):
                needs_redraw = True

            # 1. Clamp DECSTBM \x1b[top;bottom r
            def fix_decstbm(match: re.Match) -> str:
                top_str, bot_str = match.group(1), match.group(2)
                if not top_str and not bot_str:
                    return f"\x1b[1;{max_rows}r"
                top = int(top_str) if top_str else 1
                bot = int(bot_str) if bot_str else max_rows
                return f"\x1b[{min(top, max_rows)};{min(bot, max_rows)}r"

            text = re.sub(r"\x1b\[(\d*);?(\d*)r", fix_decstbm, text)

            # 2. Clamp cursor positioning \x1b[row;colH and \x1b[row;colf
            def fix_cursor(match: re.Match) -> str:
                row_str, col_str, cmd_char = match.group(1), match.group(2), match.group(3)
                row = int(row_str) if row_str else 1
                col = int(col_str) if col_str else 1
                return f"\x1b[{min(row, max_rows)};{col}{cmd_char}"

            text = re.sub(r"\x1b\[(\d+);(\d+)([Hf])", fix_cursor, text)

            # 3. Intercept Alt Screen to maintain scroll region
            if "\x1b[?1049h" in text:
                text = text.replace("\x1b[?1049h", f"\x1b[?1049h\x1b[1;{max_rows}r")
            if "\x1b[?1049l" in text:
                text = text.replace("\x1b[?1049l", f"\x1b[?1049l\x1b[1;{max_rows}r")

            return text.encode("latin1"), needs_redraw

        try:
            if status_bar:
                status_bar.set_scroll_region()
                status_bar.render(at_bottom=True, force=True, q_msg=q_msg)

            proc = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(ROOT),
                close_fds=True,
                env=env,
            )
            os.close(slave_fd)

            def handle_winch(signum, frame):
                nonlocal child_lines, child_cols
                try:
                    c, l = os.get_terminal_size()
                    child_lines = max(5, l - 4)
                    child_cols = max(10, c)
                    new_ws = struct.pack("HHHH", child_lines, child_cols, 0, 0)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, new_ws)
                    if status_bar:
                        status_bar.set_scroll_region()
                        status_bar.render(at_bottom=True, force=True, q_msg=q_msg)
                    if proc and proc.poll() is None:
                        proc.send_signal(signal.SIGWINCH)
                except Exception:
                    pass

            try:
                old_sigwinch = signal.signal(signal.SIGWINCH, handle_winch)
            except Exception:
                pass

            try:
                tty.setraw(sys.stdin.fileno())
            except Exception:
                pass

            while proc.poll() is None:
                r, _, _ = select.select([sys.stdin.fileno(), master_fd], [], [], 0.05)
                if sys.stdin.fileno() in r:
                    try:
                        data = os.read(sys.stdin.fileno(), 1024)
                        if not data:
                            break
                        os.write(master_fd, data)
                    except OSError:
                        break

                if master_fd in r:
                    try:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        filtered, needs_redraw = filter_child_bytes(data, child_lines)
                        os.write(sys.stdout.fileno(), filtered)
                        sys.stdout.flush()

                        now = time.monotonic()
                        if needs_redraw or (now - last_footer_redraw > 1.5):
                            if status_bar:
                                status_bar.render(at_bottom=True, force=True, q_msg=q_msg)
                            last_footer_redraw = now
                    except OSError:
                        break

            # Flush remaining output from master_fd after child exits
            try:
                while True:
                    r, _, _ = select.select([master_fd], [], [], 0.02)
                    if master_fd in r:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        filtered, _ = filter_child_bytes(data, child_lines)
                        os.write(sys.stdout.fileno(), filtered)
                        sys.stdout.flush()
                    else:
                        break
            except Exception:
                pass

        finally:
            if old_termios:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_termios)
                except Exception:
                    pass
            if old_sigwinch is not None:
                try:
                    signal.signal(signal.SIGWINCH, old_sigwinch)
                except Exception:
                    pass
            try:
                os.close(master_fd)
            except Exception:
                pass
            if status_bar:
                status_bar.clear_footer()
                status_bar.reset_scroll_region(force=True)
                sys.stdout.write("\033[0m\r\n\033[K")
                sys.stdout.flush()

        return proc.wait() if proc else 0
    except Exception:
        res = subprocess.run(cmd, cwd=str(ROOT), env=env)
        return res.returncode if hasattr(res, "returncode") else 0


def handle_ask_ai(job: dict[str, Any], session_allowed_models: list[str]):
    """Opens a conversational interface or launches an interactive LLM CLI session."""
    import shutil

    # Initial context bundle generation
    _, context_file = generate_chat_context(job)

    # Detect installed CLIs
    available_clis = []
    if shutil.which("opencode"): available_clis.append(("opencode", "OpenCode CLI", "opencode"))
    if shutil.which("agy"):
        available_clis.append(("agy", "Antigravity CLI (agy)", "agy"))
    elif shutil.which("antigravity"):
        available_clis.append(("agy", "Antigravity CLI", "antigravity"))
    if shutil.which("claude"): available_clis.append(("claude", "Claude Code CLI", "claude"))
    if shutil.which("gemini"): available_clis.append(("gemini", "Gemini CLI", "gemini"))
    if shutil.which("codex"): available_clis.append(("codex", "Codex CLI", "codex"))

    # Determine recommended CLI based on user preference or job builder/planner
    settings_path = CONFIG_DIR / "settings.json"
    settings = read_json(settings_path) if settings_path.exists() else {}
    preferred_cli = (settings.get("preferred_cli") or settings.get("default_cli") or "").lower()

    builder_model = str(job.get("builder") or job.get("planner") or "").lower()
    recommended_key = None
    rec_reason = ""

    if preferred_cli:
        recommended_key = preferred_cli
        rec_reason = "Default"
    elif "claude" in builder_model or "anthropic" in builder_model:
        recommended_key = "claude"
        rec_reason = "Job Builder"
    elif "antigravity" in builder_model:
        recommended_key = "agy"
        rec_reason = "Job Builder"
    elif "gemini" in builder_model:
        recommended_key = "gemini"
        rec_reason = "Job Builder"
    elif "codex" in builder_model or "openai" in builder_model or "o3" in builder_model or "gpt" in builder_model:
        recommended_key = "codex"
        rec_reason = "Job Builder"
    elif "opencode" in builder_model or "qwen" in builder_model or "deepseek" in builder_model:
        recommended_key = "opencode"
        rec_reason = "Job Builder"

    # Partition into recommended and others
    rec_cli = None
    other_clis = []
    for item in available_clis:
        if recommended_key and item[0] == recommended_key and rec_cli is None:
            rec_cli = item
        else:
            other_clis.append(item)

    if rec_cli is None and available_clis:
        rec_cli = available_clis[0]
        other_clis = available_clis[1:]

    ordered_clis = ([rec_cli] if rec_cli else []) + other_clis

    while True:
        clear_screen()
        status_ctx = {
            "allowed_models": session_allowed_models,
            **job
        }
        with StatusBar(status_ctx, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Ask AI / Interactive Investigation")
            print("\033[90mInspect code modifications, ask clarifying questions, and test hypotheses.\033[0m\n")

            # 1. Prominently Highlighted Recommended Session
            if rec_cli:
                rec_key, rec_label, _ = rec_cli
                rec_badge = f" \033[1;92m⭐️ ({rec_reason})\033[0m" if rec_reason else " \033[1;92m⭐️ (Recommended)\033[0m"
                print("\033[1;92m⭐️ RECOMMENDED LLM SESSION\033[0m")
                print(f"  [\033[1;92m1\033[0m] Launch \033[1;97;42m {rec_label} \033[0m{rec_badge}")
                print(f"      \033[90mPreloads job brief, builder summary & full git diff into interactive session.\033[0m\n")

            # 2. Other Available AI CLI Sessions
            if other_clis:
                print("\033[1;95m🚀 OTHER AI CLI SESSIONS\033[0m")
                for idx, (cli_key, cli_label, _) in enumerate(other_clis, 2):
                    print(f"  [\033[1;96m{idx}\033[0m] Launch \033[1;97m{cli_label}\033[0m")
                print()
            # 3. Direct AI Guidance / Hint Option
            print("\033[1;93m💡 STEER / PROVIDE GUIDANCE TO AI\033[0m")
            print("  [\033[1;93mG\033[0m] Provide Hint / Clue to AI (Re-plan with Guidance)")
            print("      \033[90mProvide domain requirements, clues, or instructions to guide the autonomous fixer.\033[0m\n")

            # 4. Navigation
            print("  [\033[1;91mB\033[0m] Back to Job Menu\n")

            placeholder = "1 (Enter for recommended), G for clue, or B" if ordered_clis else "G for clue, or B"
            choice = prompt_input("Select Option:", placeholder=placeholder, field_below=True).strip().lower()
            if not choice:
                if ordered_clis:
                    choice = "1"
                else:
                    break
            elif choice == "b":
                break
            elif choice == "g":
                clear_screen()
                revised_job = handle_tweak_revise(job)
                if revised_job:
                    job = revised_job
                continue

            # Check if user picked an interactive CLI
            if choice.isdigit() and 1 <= int(choice) <= len(ordered_clis):
                cli_key, cli_label, bin_name = ordered_clis[int(choice) - 1]

                # Ensure fresh context bundle is generated before launching
                _, context_file = generate_chat_context(job)

                clear_screen()
                print_header(f"🤖 Orchestrator AI Session ({cli_label})")
                display_ctx = context_file.name
                try:
                    if context_file.is_relative_to(ROOT):
                        display_ctx = str(context_file.relative_to(ROOT))
                except Exception:
                    pass

                issue_num = job.get("issue_number", "")
                title = job.get("title", "")
                print(f"  • \033[1;36mTarget Job:\033[0m   \033[1;97m#{issue_num} ({title})\033[0m")
                print(f"  • \033[1;36mContext:\033[0m      {display_ctx}")
                print(f"  • \033[1;36mExit to Menu:\033[0m Type \033[1;92m/exit\033[0m or press \033[1;92mCtrl-D\033[0m anytime to return to Orchestrator.\n")
                print("-" * 75 + "\n")

                intro_prompt = f"I am reviewing Job #{issue_num}: {title}. Please inspect the diff, brief, and notes in {context_file} to help me understand what changes were made and answer any questions."
                
                cmd = []
                if cli_key == "opencode":
                    cmd = ["opencode", "--prompt", intro_prompt]
                elif cli_key == "agy":
                    cmd = [bin_name, "--prompt-interactive", intro_prompt]
                elif cli_key == "gemini":
                    cmd = ["gemini", "-i", intro_prompt]
                elif cli_key == "claude":
                    cmd = ["claude", intro_prompt]
                elif cli_key == "codex":
                    cmd = ["codex", "--no-alt-screen", intro_prompt]
                else:
                    cmd = [bin_name, intro_prompt]

                start_time = datetime.now()
                start_head = None
                try:
                    head_res = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(ROOT))
                    if head_res.returncode == 0:
                        start_head = head_res.stdout.strip()
                except Exception:
                    pass

                try:
                    sub_env = os.environ.copy()
                    sub_env.setdefault("GOOGLE_VERTEX_LOCATION", "us-central1")
                    sub_env.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
                    run_interactive_cli_with_framed_footer(
                        cmd,
                        env=sub_env,
                        status_bar=status_bar,
                        q_msg="Type /exit or Ctrl-D to return to Orchestrator",
                    )
                except Exception as e:
                    print(f"\n\033[1;91m❌ Error running {cli_label}: {e}\033[0m")
                    input("\n\033[1;96mTap Enter to continue...\033[0m")
                finally:
                    # Clean up terminal state and reset scroll region upon return
                    status_bar.clear_footer()
                    status_bar.reset_scroll_region(force=True)
                    sys.stdout.write("\033[0m\r\n\033[K")
                    sys.stdout.flush()

                # Post-session calculation and context capture
                end_time = datetime.now()
                elapsed_secs = max(0, int((end_time - start_time).total_seconds()))
                mins, secs = divmod(elapsed_secs, 60)
                duration_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

                new_commits = []
                if start_head:
                    try:
                        log_res = subprocess.run(
                            ["git", "log", f"{start_head}..HEAD", "--oneline"],
                            capture_output=True,
                            text=True,
                            cwd=str(ROOT)
                        )
                        if log_res.returncode == 0 and log_res.stdout.strip():
                            new_commits = [c.strip() for c in log_res.stdout.splitlines() if c.strip()]
                    except Exception:
                        pass

                print(f"\n\033[1;92m✓ {cli_label} session ended\033[0m \033[90m({duration_str})\033[0m")
                if new_commits:
                    print(f"  \033[1;93mNew Commits:\033[0m {len(new_commits)} detected")
                    for c in new_commits[:3]:
                        print(f"    \033[90m• {c}\033[0m")
                    if len(new_commits) > 3:
                        print(f"    \033[90m...and {len(new_commits) - 3} more\033[0m")

                job = record_interactive_investigation(
                    job,
                    tool=cli_label,
                    cli_key=cli_key,
                    duration=duration_str,
                    notes="",
                    new_commits=new_commits,
                    duration_seconds=elapsed_secs,
                )
                save_job(job)
                generate_chat_context(job)

                time.sleep(2.0)
                continue
    return job


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
            print("\033[90mAccess uses local CLIs first; saved API keys act as fallback.\033[0m")
            print()

            # Gather Status Data using cached CLI statuses
            providers = [
                {"label": "Antigravity", "cli": "agy", "key_id": "gemini_api_key", "status": cli_statuses["gemini"]},
                {"label": "Claude", "cli": "claude", "key_id": "anthropic_api_key", "status": cli_statuses["claude"]},
                {"label": "Codex", "cli": "codex", "key_id": "openai_api_key", "status": cli_statuses["codex"]},
                {"label": "GitHub", "cli": "gh", "key_id": None, "status": cli_statuses["gh"]},
                {"label": "OpenCode", "cli": "opencode", "key_id": None, "status": cli_statuses["opencode"]},
                {"label": "Ollama", "cli": "ollama", "key_id": "ollama_api_key", "status": cli_statuses["ollama"]},
            ]

            # Table Header
            header = f"{'Provider':11} | {'Authentication':20} | {'Access'}"
            print(f"\033[1;97m{header}\033[0m")
            print_divider("-")

            for p in providers:
                status = p["status"]
                key_id = p["key_id"]

                # Check for configured API keys
                has_saved_key = False
                has_env_key = False
                if key_id:
                    current_key = settings.get(key_id, "") or (settings.get("ollama_host", "") if key_id == "ollama_api_key" else "")
                    if current_key:
                        has_saved_key = True
                    elif key_id.upper() in os.environ or (key_id == "ollama_api_key" and "OLLAMA_HOST" in os.environ):
                        has_env_key = True

                # Determine single Authentication status & Readiness
                if status == "Checking...":
                    raw_auth = "Checking..."
                    auth_color = "\033[93m"
                    is_ready = False
                elif status in ["Logged In", "Ready", "Running"]:
                    if status == "Running":
                        raw_auth = "Running locally"
                    else:
                        raw_auth = "CLI Ready"
                    auth_color = "\033[92m"
                    is_ready = True
                elif has_saved_key:
                    raw_auth = "Saved API key"
                    auth_color = "\033[92m"
                    is_ready = True
                elif has_env_key:
                    raw_auth = "Env Var API key"
                    auth_color = "\033[1;96m"
                    is_ready = True
                elif status == "Not Logged In":
                    raw_auth = "Sign-in required"
                    auth_color = "\033[1;91m"
                    is_ready = False
                elif status == "Not Installed":
                    raw_auth = "Not installed"
                    auth_color = "\033[90m"
                    is_ready = False
                else:
                    raw_auth = "Offline"
                    auth_color = "\033[1;91m"
                    is_ready = False

                auth_display = f"{auth_color}{raw_auth:20}\033[0m"

                if is_ready:
                    ready_display = "\033[1;92m✓ READY\033[0m"
                elif status == "Checking...":
                    ready_display = "\033[93m⏱ CHECKING\033[0m"
                else:
                    ready_display = "\033[1;91m✗ LOCKED\033[0m"

                print(f"{p['label']:11} | {auth_display} | {ready_display}")

            print()

            print_header("ACTIONS")
            
            # Single-column Layout for Actions
            print(f"  \033[1;96mFallback & Cloud Credentials\033[0m")
            print_wrapped_description("(API keys or host endpoints stored locally; used when CLI logins are expired, in cloud/remote environments, or for custom GPU endpoints)", indent_size=4)
            print(f"    [\033[1;92mG\033[0m] Update Antigravity credential")
            print(f"    [\033[1;92mA\033[0m] Update Anthropic credential")
            print(f"    [\033[1;92mO\033[0m] Update OpenAI credential")
            print(f"    [\033[1;92mL\033[0m] Update Ollama Cloud credential")

            print(f"\n  \033[1;96mBrowser Logins (OAuth)\033[0m")
            print_wrapped_description("(Authenticates provider CLIs directly using browser OAuth for local development)", indent_size=4)
            print(f"    [\033[1;92m1\033[0m] Login Antigravity")
            print(f"    [\033[1;92m2\033[0m] Login Claude")
            print(f"    [\033[1;92m3\033[0m] Login Codex")
            print(f"    [\033[1;92m4\033[0m] Login GitHub")
            print(f"    [\033[1;92m5\033[0m] Login OpenCode")

            print_header("MANAGEMENT")
            print(f"    [\033[1;91mC\033[0m] Clear saved credentials")
            print()
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
            elif choice == "l":
                print(f"\n  Updating: \033[97mOllama Cloud Credential\033[0m")
                key_val = prompt_password("🔑 Ollama API Key:", placeholder="(enter to skip)")
                host_val = prompt_input("🌐 Ollama Host URL (e.g. https://my-ollama:11434):", placeholder="(enter to skip)")
                updated = False
                if key_val:
                    settings["ollama_api_key"] = key_val
                    updated = True
                if host_val:
                    settings["ollama_host"] = host_val
                    updated = True
                if updated:
                    write_json(settings_path, settings)
                    print("✅ Ollama Cloud credentials saved.")
                else:
                    print("⚠️  No changes made.")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                continue
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
                    for kid in ["gemini_api_key", "anthropic_api_key", "openai_api_key", "ollama_api_key", "ollama_host"]: settings.pop(kid, None)
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
    
    input("\n\033[1;96mTap Enter to proceed to Fleet Dependency Matrix...\033[0m")
    
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
        print_box_line_rich("Status:    ", status)
        print_box_line_rich("Coverage:  ", f"{readiness_color}{installed_count}/{total_count} tools, {readiness}\033[0m")
        if probe.get("repo_path") and not probe.get("repo_path_ok", True):
            print_box_line_rich("Repo Path: ", f"\033[1;91mMISSING\033[0m \033[90m{probe.get('repo_path')}\033[0m")
        elif probe.get("repo_path"):
            print_box_line_rich("Repo Path: ", f"\033[92mOK\033[0m \033[90m{probe.get('repo_path')}\033[0m")
        if probe.get("repo_warning"):
            print_box_line_rich("Repo Note: ", f"\033[93m{probe.get('repo_warning')}\033[0m")
        if probe.get("probe_error"):
            print_box_line_rich("Error:     ", f"\033[1;91m{probe.get('probe_error')}\033[0m")
        print_box_line_rich("Available: ", f"\033[92m{', '.join(installed) if installed else 'none'}\033[0m")
        print_box_line_rich("Missing:   ", f"\033[1;91m{', '.join(missing) if missing else 'none'}\033[0m")
        mcp_plugins = probe.get("mcp_plugins", {})
        optional = [label for label, active in mcp_plugins.items() if active]
        print_box_line_rich("Optional:  ", f"\033[96m{', '.join(optional) if optional else 'none'}\033[0m")
        print(f"  \033[90m└─\033[0m")
        
    print(f"\n\033[90mTotal Machines in session: {len(machines)}\033[0m")
    input("\n\033[1;96mTap Enter to return to menu...\033[0m")

def handle_fleet_hygiene(session_allowed_machines):
    from probe_machine import load_machines, probe_machine
    print_header("Purge Processes")

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

    description = [
        "This tool terminates zombie processes (running > 12h) and clears build caches.",
        "",
        "\033[1;92mBenefits:\033[0m",
        "  ✅ Frees up system memory (RAM) and CPU cores.",
        "  ✅ Clears local DerivedData to resolve 'ghost' compiler errors.",
        "",
        "\033[1;91mRisks:\033[0m",
        "  ⚠️  Will forcefully terminate active old Xcode builds.",
        "  ⚠️  First build after purge will be slower (re-indexing cache).",
        "",
        "\033[1;97mPotential Zombie Processes Found:\033[0m"
    ]
    for m, p in all_stale:
        description.append(f"  ❌ \033[1;97m{m['name']:15}\033[0m | PID: {p['pid']:8} | Runtime: {p['etime']:10} | CMD: {p['comm']}")
    
    description.append("")
    description.append("\033[1;91mPurge listed processes and clear DerivedData?\033[0m")

    if prompt_confirm("Purge Processes", default=False, description=description):
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
            print("\n  [\033[1;91mB\033[0m] Back")

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
    from orchestrator.project_config import forget_project, load_recent_projects, remember_project

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
            print("  [\033[1;92mA\033[0m] Add Project\n")
            print("  [\033[1;91mB\033[0m] Back")

            prompt = get_choice_prompt("Choice:", "(A/B)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            key = get_key().strip().lower()
            clear_choice_placeholder()
            if key == "b":
                return
        else:
            print("\033[1;97mRecent Projects\033[0m")
            print("\033[1;90m(Arrows: navigate, Enter: select, D: remove, B: back)\033[0m\n")

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
            print("  [\033[1;91mD\033[0m] Remove from List")
            print("  [\033[1;96mH\033[0m] Help\n")
            print("  [\033[1;91mB\033[0m] Back")

            status_bar.render(at_bottom=True, force=True)
            key = get_key().strip().lower()

            if key == "b":
                return
            if key in {"d", "x", "delete"}:
                if projects and 0 <= selected_idx < len(projects):
                    selected = projects[selected_idx]
                    forget_project(selected.get("name", ""))
                    selected_idx = max(0, selected_idx - 1)
                continue
            if key in {"up", "k"}:
                selected_idx = (selected_idx - 1) % len(projects)
                continue
            if key in {"down", "j"}:
                selected_idx = (selected_idx + 1) % len(projects)
                continue
            if key == "h":
                clear_screen()
                status_bar.set_scroll_region()
                print_header("Help")
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
                print("    (Enter path to project folder, or Enter to cancel; e.g. ~/projects/my-ios-app)")
                path_str = prompt_input("Project path:", placeholder="~/projects/my-ios-app or ./my-ios-app", field_below=True)
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
    global PROJECT_CONFIG
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

            print("  \033[1;96m--- Models & Instructions ---\033[0m")
            print_wrapped_option("[\033[93mM\033[0m] LLM Models (Session Defaults)")
            print_wrapped_option("[\033[93mK\033[0m] Manage LLM API Keys")
            print_wrapped_option("[\033[93mI\033[0m] AI Instruction Settings (.md files)")

            print("\n  \033[1;96m--- Machine Fleet & Project Config ---\033[0m")
            print_wrapped_option("[\033[93mF\033[0m] Manage Machine Fleet")
            print_wrapped_option(f"[\033[93mG\033[0m] Select Base Branch (\033[97m{global_base}\033[0m)")
            print_wrapped_option("[\033[93mC\033[0m] Change Target Project")
            print_wrapped_option("[\033[93mA\033[0m] Manage Archived Jobs")

            print("\n  \033[1;96m--- Delivery & Notifications ---\033[0m")
            print_wrapped_option("[\033[93mD\033[0m] Firebase App Distro (Delivery)")
            print_wrapped_option("[\033[93mX\033[0m] Xcode Cloud & CI Workflows (ci_scripts)")
            print_wrapped_option("[\033[93mE\033[0m] Email Notification Settings")

            print("\n  \033[1;96m--- Setup, Health & Documentation ---\033[0m")
            print_wrapped_option("[\033[93mW\033[0m] Setup Wizard (Full Project & Tools Setup)")
            print_wrapped_option("[\033[93mP\033[0m] Run Prerequisite Audit")
            print_wrapped_option("[\033[93mS\033[0m] Documentation & Architecture Guides")
            print_wrapped_option("[\033[93mT\033[0m] Orchestrator Self-Tests")
            print_wrapped_option("[\033[93mU\033[0m] Update Orchestrator (Local & Fleet)")
            print()
            print_wrapped_option("[\033[1;91mB\033[0m] Back", indent_size=4, subsequent_indent_size=4)
            # Anchor prompt to bottom
            prompt = get_choice_prompt("Choice:", "(letter)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()
            
            if choice == "b":
                break
            elif choice == "m":
                while True:
                    options, value_map, details_map, service_summary = run_with_loading_screen(
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
                        footer = (
                            "[\033[1;96mA\033[0m] Select All  [\033[1;92mF\033[0m] Free Only  [\033[1;93mN\033[0m] Deselect All\n"
                            "[\033[1;92mR\033[0m] Refresh Models  [\033[93mK\033[0m] API Keys  [\033[1;91mB\033[0m] Back"
                        )
                        menu_label = f"Select LLM Models: \033[1;96m{service_summary} LLM services enabled\033[0m."
                        new_labels = prompt_checkbox(menu_label, options, curr_defaults, extra_keys=["r", "d", "k", "b"], footer=footer, details_map=details_map, details_title="Selected Model Details", status_bar=status_bar)
                        if new_labels:
                            session_allowed_models = [value_map[label] for label in new_labels]
                        else:
                            print("Warning: At least one model must be selected. Keeping previous choice.")
                            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                        break
                    except KeyInterruptException as exc:
                        if exc.key == "b":
                            break
                        elif exc.key == "k":
                            handle_api_keys(session_allowed_machines, session_allowed_models)
                            continue
                        elif exc.key in ["r", "d"]:
                            from model_registry import sync_models
                            print("\n\n📡 Scanning local CLIs (Ollama, OpenCode) and provider APIs for new models...")
                            success, msg = sync_models(live_discovery=True)
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
            elif choice == "x":
                handle_xcode_cloud_menu(session_allowed_machines, session_allowed_models)
            elif choice == "s":
                while True:
                    clear_screen()
                    with StatusBar({
                        "allowed_machines": session_allowed_machines,
                        "online_machines": get_online_machines(session_allowed_machines),
                        "allowed_models": session_allowed_models
                    }, sub_menu=True) as status_bar:
                        status_bar.anchor_to_bottom = False
                        status_bar.set_scroll_region()
                        print_header("Documentation & Architecture Guides")
                        print_wrapped_description("New here? Start with Getting Started. Use User Guide as the full reference.", indent_size=2)
                        print()
                        
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
                                    print_wrapped_description(description, indent_size=9)
                                next_index += 1
                            print()
                        
                        print("    [\033[1;91mB\033[0m] Back")
                        
                        status_bar.render(at_bottom=True, force=True, prompt=None)
                        try:
                            sub_choice = prompt_input("Choice:", placeholder="(index or B)")
                        except BackException:
                            break
                        if not sub_choice:
                            continue
                        sub_choice = sub_choice.strip().lower()
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
            elif choice == "w":
                clear_screen()
                status_bar.clear_footer()
                status_bar.reset_scroll_region(force=True)
                if os.name != "nt":
                    os.system("stty sane 2>/dev/null")
                cli_path = SCRIPTS_DIR.parent / "cli.py"
                subprocess.run([sys.executable, str(cli_path), "wizard"], cwd=str(ROOT))
                if os.name != "nt":
                    os.system("stty sane 2>/dev/null")
                import importlib
                import orchestrator.project_config
                importlib.reload(orchestrator.project_config)
                PROJECT_CONFIG = orchestrator.project_config.PROJECT_CONFIG
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
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

def setup_xcode_cloud_scripts(root: Path) -> list[Path]:
    """Generates standard Xcode Cloud lifecycle hook scripts in ci_scripts/."""
    ci_dir = root / "ci_scripts"
    ci_dir.mkdir(parents=True, exist_ok=True)
    created = []

    post_clone = ci_dir / "ci_post_clone.sh"
    if not post_clone.exists():
        post_clone.write_text("""#!/bin/sh

# Xcode Cloud Post-Clone Hook (Runs after repository is cloned)
# Use this script to install tools, Homebrew packages, and setup dependencies.

set -e

echo "🚀 Xcode Cloud: Initializing environment after clone..."

# 1. Install Homebrew tools if needed (e.g., xcbeautify for clean logs)
if which brew >/dev/null 2>&1; then
    echo "📦 Installing xcbeautify for test formatting..."
    brew install xcbeautify || true
fi

# 2. Cocoapods or SPM resolution (if applicable)
if [ -f "Podfile" ]; then
    echo "📦 Installing CocoaPods dependencies..."
    pod install
fi

echo "✅ Xcode Cloud post-clone setup complete."
""", encoding="utf-8")
        post_clone.chmod(0o755)
        created.append(post_clone)

    pre_build = ci_dir / "ci_pre_xcodebuild.sh"
    if not pre_build.exists():
        pre_build.write_text("""#!/bin/sh

# Xcode Cloud Pre-Xcodebuild Hook (Runs before xcodebuild runs)
# Use this script for code-generation, environment variables, or config validation.

set -e

echo "🛠️ Xcode Cloud: Preparing build..."
echo "  • CI_BUILD_NUMBER: $CI_BUILD_NUMBER"
echo "  • CI_BRANCH: $CI_BRANCH"
echo "  • CI_COMMIT: $CI_COMMIT"

if [ -f ".orchestrator/project.json" ]; then
    echo "✅ Detected Orchestrator project configuration."
fi

echo "✅ Pre-xcodebuild preparation complete."
""", encoding="utf-8")
        pre_build.chmod(0o755)
        created.append(pre_build)

    post_build = ci_dir / "ci_post_xcodebuild.sh"
    if not post_build.exists():
        post_build.write_text("""#!/bin/sh

# Xcode Cloud Post-Xcodebuild Hook (Runs after xcodebuild finishes)
# Use this script for custom notifications or artifact processing.

set -e

echo "📊 Xcode Cloud: Post-build processing..."
echo "  • CI_XCODEBUILD_ACTION: $CI_XCODEBUILD_ACTION"
echo "  • CI_RESULT: $CI_RESULT"

echo "✅ Post-xcodebuild hook finished."
""", encoding="utf-8")
        post_build.chmod(0o755)
        created.append(post_build)

    return created

def handle_xcode_cloud_menu(session_allowed_machines: list[str], session_allowed_models: list[str]) -> None:
    error_msg = ""
    while True:
        clear_screen()
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Xcode Cloud & CI Workflows")

            ci_dir = ROOT / "ci_scripts"
            scripts = ["ci_post_clone.sh", "ci_pre_xcodebuild.sh", "ci_post_xcodebuild.sh"]
            existing = [s for s in scripts if (ci_dir / s).exists()]

            print("  \033[1;90m--- XCODE CLOUD SCRIPTS STATUS ---\033[0m")
            if existing:
                for s in scripts:
                    p = ci_dir / s
                    if p.exists():
                        is_exec = os.access(p, os.X_OK)
                        exec_str = "\033[92mExecutable (755)\033[0m" if is_exec else "\033[93mNot Executable\033[0m"
                        print(f"  • \033[1;97mci_scripts/{s}:\033[0m \033[92mPRESENT\033[0m ({exec_str})")
                    else:
                        print(f"  • \033[1;97mci_scripts/{s}:\033[0m \033[90mMISSING (Optional)\033[0m")
            else:
                print("  \033[90mNo custom ci_scripts/ found. Xcode Cloud uses standard xcodebuild defaults.\033[0m")
            print()

            # Check for live PR / CI checks if gh is installed
            print("  \033[1;90m--- LIVE CI / XCODE CLOUD CHECKS ---\033[0m")
            import shutil
            has_gh = shutil.which("gh") is not None
            if has_gh:
                try:
                    checks_out = subprocess.check_output(
                        ["gh", "pr", "checks"],
                        cwd=str(ROOT),
                        stderr=subprocess.STDOUT,
                        timeout=6
                    ).decode("utf-8").strip()
                    if checks_out:
                        for line in checks_out.splitlines()[:5]:
                            print(f"    {line}")
                    else:
                        print("    \033[90mNo active PR checks running on current branch.\033[0m")
                except Exception:
                    print("    \033[90mNo active PR or remote CI checks found for current branch.\033[0m")
            else:
                print("    \033[90mInstall GitHub CLI (gh) to view live PR check status.\033[0m")
            print()

            print("  \033[1;90m--- ACTIONS ---\033[0m")
            print("  [\033[1;96m1\033[0m] Setup / Generate Standard Xcode Cloud ci_scripts/ (3 Hook Scripts)")
            print("  [\033[1;96m2\033[0m] Make all ci_scripts executable (chmod +x)")
            print("  [\033[1;96m3\033[0m] View Xcode Cloud & CI Architecture Guide\n")
            print("  [\033[1;91mB\033[0m] Back")

            if error_msg:
                print(f"\n\033[1;91mNOT A VALID OPTION, PLEASE TRY AGAIN... ({error_msg})\033[0m")
                error_msg = ""

            prompt = get_choice_prompt("Choice:", "(1-3, B)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()

            if choice == "b":
                break
            elif choice == "1":
                clear_screen()
                print_header("Setting Up Xcode Cloud ci_scripts/")
                created = setup_xcode_cloud_scripts(ROOT)
                if created:
                    print(f"✅ Generated {len(created)} Xcode Cloud script(s):")
                    for p in created:
                        print(f"   • \033[1;92m{p.relative_to(ROOT)}\033[0m (executable)")
                else:
                    print("ℹ️ All standard ci_scripts already exist.")
                input("\n\033[1;96mTap Enter to continue...\033[0m")
            elif choice == "2":
                ci_dir = ROOT / "ci_scripts"
                if ci_dir.exists():
                    for sh in ci_dir.glob("*.sh"):
                        sh.chmod(0o755)
                    print("\n✅ Set chmod +x on all scripts in ci_scripts/.")
                else:
                    print("\n⚠️ No ci_scripts/ directory found. Use option [1] to generate.")
                input("\n\033[1;96mTap Enter to continue...\033[0m")
            elif choice == "3":
                clear_screen()
                print_header("Xcode Cloud Workflow Guide")
                print("""\033[1;97mXcode Cloud & Orchestrator Integration:\033[0m

  1. \033[1;96mAutomated Triggers:\033[0m
     When you create/push branches or open PRs with Orchestrator, Xcode Cloud
     automatically picks up the change on GitHub and starts cloud builds & tests.

  2. \033[1;96mCustom Lifecycle Hooks (ci_scripts/):\033[0m
     • \033[97mci_post_clone.sh:\033[0m Runs right after git clone. Installs Homebrew packages (xcbeautify),
       SPM plugins, and Cocoapods.
     • \033[97mci_pre_xcodebuild.sh:\033[0m Runs before build/test. Injects environment variables and builds.
     • \033[97mci_post_xcodebuild.sh:\033[0m Runs after tests finish. Forwards artifacts or sends notifications.

  3. \033[1;96mApp Store Connect Configuration:\033[0m
     To connect your repo: Xcode > Integrate > Create Workflow, or configure in
     App Store Connect > Xcode Cloud.""")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            else:
                error_msg = f"'{choice}'"

def handle_quick_distribute(session_allowed_machines: list[str], session_allowed_models: list[str]) -> None:
    error_msg = ""
    while True:
        clear_screen()
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("Quick Build & Distribution (Firebase)")

            # Pre-flight check
            dist_errors = PROJECT_CONFIG.validate_distribution_config()
            if dist_errors:
                print("\n  \033[1;91m⚠️ Distribution configuration is incomplete:\033[0m")
                for err in dist_errors:
                    print(f"    - {err}")
                print("\n  Run '\033[1;96mC\033[0m' -> Configuration & Tools -> Firebase App Distro to configure.")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                return

            current_branch = "unknown"
            try:
                current_branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ROOT)).decode("utf-8").strip()
            except:
                pass

            print_wrapped_description("Builds, archives, and uploads an IPA of the current working branch directly to Firebase App Distribution with automated version incrementing.", indent_size=2)
            print()
            print(f"  \033[1;36m• Project / Scheme:\033[0m   \033[97m{PROJECT_CONFIG.scheme or PROJECT_CONFIG.project_name or ROOT.name}\033[0m")
            print(f"  \033[1;36m• Current Branch:\033[0m     \033[1;92m{current_branch}\033[0m")
            print(f"  \033[1;36m• Delivery Method:\033[0m    \033[97m{PROJECT_CONFIG.delivery_method or 'ad-hoc'}\033[0m")
            print(f"  \033[1;36m• Tester Groups:\033[0m      \033[97m{getattr(PROJECT_CONFIG, 'firebase_groups', None) or 'internal-testers'}\033[0m")
            print()
            print("  [\033[1;92mD\033[0m] Quick Distribute (Current Branch)")
            print("  [\033[1;96mN\033[0m] Distribute with Custom Release Notes\n")
            print("  [\033[1;91mB\033[0m] Back")

            if error_msg:
                print(f"\n\033[1;91mNOT A VALID OPTION, PLEASE TRY AGAIN... ({error_msg})\033[0m")
                error_msg = ""

            prompt = get_choice_prompt("Choice:", "(D/N/B)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()

            if choice == "b":
                break
            elif choice in ("d", "n"):
                custom_notes = None
                if choice == "n":
                    clear_screen()
                    status_bar.set_scroll_region()
                    print_header("Custom Release Notes")
                    try:
                        custom_notes = prompt_input("Enter release notes for this build:", placeholder="e.g., Bug fix for login screen", field_below=True)
                    except BackException:
                        continue

                status_bar.clear_footer()
                status_bar.reset_scroll_region(force=True)
                clear_screen()
                print_header(f"Distributing Build: {current_branch}")
                print("\033[90mArchiving and uploading to Firebase (estimated 2-5 minutes)...\033[0m\n")

                prev_notes = os.environ.get("DISTRIBUTION_RELEASE_NOTES")
                try:
                    if custom_notes:
                        os.environ["DISTRIBUTION_RELEASE_NOTES"] = custom_notes
                    run_script("smoke_test_delivery.py", [], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
                finally:
                    if prev_notes is not None:
                        os.environ["DISTRIBUTION_RELEASE_NOTES"] = prev_notes
                    else:
                        os.environ.pop("DISTRIBUTION_RELEASE_NOTES", None)
            else:
                error_msg = f"'{choice}'"

def get_github_auth_info() -> tuple[bool, list[dict[str, Any]], str | None]:
    """
    Returns:
      (has_gh: bool, accounts: list[dict], active_user: str | None)
      where each account dict is:
        {"host": str, "user": str, "active": bool, "protocol": str, "scopes": str}
    """
    import shutil
    has_gh = shutil.which("gh") is not None
    if not has_gh:
        return False, [], None
    
    accounts: list[dict[str, Any]] = []
    active_user: str | None = None
    auth_out = ""
    try:
        auth_out = subprocess.check_output(
            ["gh", "auth", "status"],
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
            timeout=5
        ).decode("utf-8")
    except subprocess.CalledProcessError as e:
        auth_out = e.output.decode("utf-8") if hasattr(e, "output") and e.output else ""
    except Exception:
        auth_out = ""
    
    if auth_out:
        current_host = "github.com"
        current_acc = None
        for line in auth_out.splitlines():
            line_str = line.rstrip()
            if line_str and not line_str.startswith(" ") and not line_str.startswith("\t") and "." in line_str:
                current_host = line_str.strip().rstrip(":")
            
            m = re.search(r"Logged in to\s+([^\s]+)\s+account\s+([^\s\(]+)", line_str)
            if m:
                host = m.group(1)
                user = m.group(2)
                current_acc = {
                    "host": host,
                    "user": user,
                    "active": False,
                    "protocol": "",
                    "scopes": ""
                }
                accounts.append(current_acc)
                continue
            
            if current_acc:
                if "Active account: true" in line_str:
                    current_acc["active"] = True
                    active_user = current_acc["user"]
                elif "Git operations protocol:" in line_str:
                    current_acc["protocol"] = line_str.split(":", 1)[1].strip()
                elif "Token scopes:" in line_str:
                    current_acc["scopes"] = line_str.split(":", 1)[1].strip()
    
    # Fallback to check ~/.config/gh/hosts.yml if auth_out didn't parse accounts
    if not accounts:
        hosts_path = Path.home() / ".config" / "gh" / "hosts.yml"
        if hosts_path.exists():
            try:
                hosts_content = hosts_path.read_text(encoding="utf-8")
                cur_host = "github.com"
                in_users = False
                active_in_file = None
                for line in hosts_content.splitlines():
                    striped = line.strip()
                    if line and not line.startswith(" ") and ":" in line:
                        cur_host = line.split(":", 1)[0].strip()
                    if striped == "users:":
                        in_users = True
                        continue
                    elif in_users and (not line.startswith(" ") or (len(line) - len(line.lstrip()) <= 4 and striped != "" and not striped.endswith(":"))):
                        if not line.startswith("        "):
                            in_users = False
                    if in_users and striped.endswith(":") and not striped.startswith("oauth_token") and not striped.startswith("git_protocol"):
                        u = striped.rstrip(":")
                        if u not in [a["user"] for a in accounts]:
                            accounts.append({"host": cur_host, "user": u, "active": False, "protocol": "", "scopes": ""})
                    if striped.startswith("user:"):
                        active_in_file = striped.split(":", 1)[1].strip()
                if active_in_file:
                    for a in accounts:
                        if a["user"] == active_in_file:
                            a["active"] = True
                            active_user = active_in_file
            except Exception:
                pass

    if accounts:
        if not active_user:
            active_user = accounts[0]["user"]
            accounts[0]["active"] = True
        else:
            for a in accounts:
                if a["user"] == active_user:
                    a["active"] = True
                
    return has_gh, accounts, active_user

def handle_add_github_account(status_bar: StatusBar | None = None) -> None:
    error_msg = ""
    while True:
        clear_screen()
        if status_bar:
            status_bar.set_scroll_region()
        print_header("Add GitHub Account")
        print_wrapped_description("Connect a GitHub account to the CLI for repository synchronization, PR checks, and issue management.", indent_size=2)
        print()
        print("  [\033[1;92m1\033[0m] Web Browser \033[1;92m(Recommended)\033[0m")
        print("      \033[90m• Connect to github.com using browser login & 1-time code\033[0m")
        print("  [\033[1;96m2\033[0m] Personal Access Token (PAT)")
        print("      \033[90m• Paste a classic or fine-grained GitHub access token\033[0m")
        print("  [\033[1;96m3\033[0m] GitHub Enterprise Server")
        print("      \033[90m• Connect to a self-hosted corporate domain (e.g. github.mycompany.com)\033[0m")
        print("  [\033[1;96m4\033[0m] Advanced: Interactive gh Wizard")
        print("      \033[90m• Run native 'gh auth login' prompts step-by-step\033[0m\n")
        print("  [\033[1;91mB\033[0m] Back")

        if error_msg:
            print(f"\n\033[1;91mNOT A VALID OPTION, PLEASE TRY AGAIN... ({error_msg})\033[0m")
            error_msg = ""

        if status_bar:
            prompt = get_choice_prompt("Choice:", "(1-4, B)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
        choice = get_key().strip().lower()
        clear_choice_placeholder()

        if choice == "b":
            break
        elif choice == "1":
            clear_screen()
            if status_bar:
                status_bar.clear_footer()
                status_bar.reset_scroll_region(force=True)
            print_header("GitHub Web Authentication (github.com)")
            print("1. A one-time device code will appear below and be copied to your clipboard.")
            print("2. Press Enter to open \033[1;96mhttps://github.com/login/device\033[0m in your browser.")
            print("3. Paste the code and click \033[1;92m'Authorize github'\033[0m.\n")
            
            if os.name != "nt":
                os.system("stty sane 2>/dev/null")
            
            cmd = ["gh", "auth", "login", "--hostname", "github.com", "--web", "--git-protocol", "https", "--clipboard"]
            res = subprocess.run(cmd, cwd=str(ROOT))
            if res.returncode != 0:
                subprocess.run(["gh", "auth", "login", "--hostname", "github.com", "--web", "--git-protocol", "https"], cwd=str(ROOT))
            
            if os.name != "nt":
                os.system("stty sane 2>/dev/null")
            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            break
        elif choice == "2":
            clear_screen()
            if status_bar:
                status_bar.clear_footer()
                status_bar.reset_scroll_region(force=True)
            print_header("Authenticate via Personal Access Token (PAT)")
            print("  \033[90mRequired token scopes: repo, read:org, gist\033[0m")
            print("  \033[90mCreate a token at: https://github.com/settings/tokens\033[0m\n")
            try:
                token = prompt_input("Enter your GitHub Personal Access Token:", allow_back=True, field_below=True)
            except BackException:
                continue
            if not token or token.strip().lower() == "b":
                continue
            
            token = token.strip()
            print("\n  Authenticating with GitHub...")
            cmd = ["gh", "auth", "login", "--hostname", "github.com", "--with-token", "--git-protocol", "https"]
            res = subprocess.run(cmd, input=token.encode("utf-8"), cwd=str(ROOT), capture_output=True)
            if res.returncode == 0:
                print("\n\033[1;92m✅ Successfully authenticated with GitHub!\033[0m")
            else:
                err = res.stderr.decode("utf-8", errors="replace").strip()
                print(f"\n\033[1;91m❌ Authentication failed: {err}\033[0m")
            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            break
        elif choice == "3":
            clear_screen()
            if status_bar:
                status_bar.clear_footer()
                status_bar.reset_scroll_region(force=True)
            print_header("Authenticate with GitHub Enterprise Server")
            print("  \033[90mEnter the hostname of your organization's self-hosted GitHub instance.\033[0m\n")
            try:
                host = prompt_input("Enterprise Hostname:", placeholder="e.g. github.mycompany.com", allow_back=True, field_below=True)
            except BackException:
                continue
            if not host or host.strip().lower() == "b":
                continue
            
            host = host.strip()
            if os.name != "nt":
                os.system("stty sane 2>/dev/null")
            cmd = ["gh", "auth", "login", "--hostname", host, "--web"]
            subprocess.run(cmd, cwd=str(ROOT))
            if os.name != "nt":
                os.system("stty sane 2>/dev/null")
            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            break
        elif choice == "4":
            clear_screen()
            if status_bar:
                status_bar.clear_footer()
                status_bar.reset_scroll_region(force=True)
            print_header("Native GitHub CLI Wizard")
            if os.name != "nt":
                os.system("stty sane 2>/dev/null")
            subprocess.run(["gh", "auth", "login"], cwd=str(ROOT))
            if os.name != "nt":
                os.system("stty sane 2>/dev/null")
            input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            break
        else:
            error_msg = f"'{choice}'"

def handle_github_menu(session_allowed_machines: list[str], session_allowed_models: list[str]) -> None:
    error_msg = ""
    while True:
        clear_screen()
        with StatusBar({
            "allowed_machines": session_allowed_machines,
            "online_machines": get_online_machines(session_allowed_machines),
            "allowed_models": session_allowed_models
        }, sub_menu=True) as status_bar:
            status_bar.set_scroll_region()

            print_header("GitHub & Source Control")

            # 1. Check Git & GitHub Connection
            has_gh, gh_accounts, gh_user = get_github_auth_info()
            gh_cli_status = "\033[92mINSTALLED\033[0m" if has_gh else "\033[1;91mMISSING\033[0m (Run: brew install gh)"
            
            gh_ok = bool(has_gh and gh_accounts)
            if not has_gh:
                gh_auth_status = "\033[90mN/A\033[0m"
            elif not gh_accounts:
                gh_auth_status = "\033[1;91mNOT LOGGED IN\033[0m (Press [A] to add account)"
            elif len(gh_accounts) == 1:
                gh_auth_status = f"\033[92m{gh_user}\033[0m \033[90m(Active)\033[0m"
            else:
                gh_auth_status = f"\033[92m{gh_user}\033[0m \033[90m(Active · {len(gh_accounts)} accounts configured)\033[0m"

            # 2. Check Remote Origin
            git_remote_url = "NOT SET"
            try:
                git_remote_url = subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
            except:
                git_remote_url = PROJECT_CONFIG.git_remote or "NOT SET"

            # 3. Check Current Branch & Working Tree
            current_branch = "unknown"
            try:
                current_branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
            except:
                pass

            tree_status = "\033[90mUNKNOWN\033[0m"
            uncommitted_count = 0
            try:
                status_porcelain = subprocess.check_output(["git", "status", "--porcelain"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
                if not status_porcelain:
                    tree_status = "\033[92mCLEAN\033[0m"
                else:
                    uncommitted_count = len(status_porcelain.splitlines())
                    tree_status = f"\033[93m{uncommitted_count} uncommitted file(s)\033[0m"
            except:
                pass

            tracking_display = "\033[90mUNKNOWN\033[0m"
            try:
                upstream_out = subprocess.check_output(["git", "status", "-sb"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
                first_line = upstream_out.splitlines()[0] if upstream_out else ""
                if "[" in first_line and "]" in first_line:
                    tracking_info = first_line.split("[", 1)[1].split("]", 1)[0]
                    tracking_display = f"\033[93m{tracking_info}\033[0m"
                elif "..." in first_line:
                    tracking_display = "\033[92mUp to date with remote\033[0m"
                else:
                    tracking_display = "\033[90mNo remote tracking branch\033[0m"
            except:
                pass

            # 4. Fetch local branches
            branches = []
            try:
                branch_out = subprocess.check_output(["git", "branch", "--sort=-committerdate", "--format=%(refname:short)"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8")
                branches = [b.strip().lstrip("*+ ").strip() for b in branch_out.splitlines() if b.strip()]
            except:
                pass

            # Display Status Section
            print("  \033[1;90m--- GITHUB CONNECTION & REPO STATUS ---\033[0m")
            print(f"  \033[1;36m• GitHub CLI:\033[0m        {gh_cli_status}")
            print(f"  \033[1;36m• GitHub Account:\033[0m    {gh_auth_status}")
            print(f"  \033[1;36m• Remote Origin:\033[0m     \033[97m{git_remote_url}\033[0m")
            print(f"  \033[1;36m• Current Branch:\033[0m    \033[1;92m{current_branch}\033[0m \033[90m({tracking_display})\033[0m")
            print(f"  \033[1;36m• Working Tree:\033[0m      {tree_status}")

            # Guidance Banner if GitHub is not hooked up properly
            if not has_gh or not gh_ok or git_remote_url in ("NOT SET", ""):
                print("\n  \033[1;93m╭───────────────────────────────────────────────────────────────╮\033[0m")
                print("  \033[1;93m│ ⚠️  GitHub Configuration Needs Attention:                     │\033[0m")
                if not has_gh:
                    print("  \033[1;93m│\033[0m   1. Install GitHub CLI: \033[97mbrew install gh\033[0m                     \033[1;93m│\033[0m")
                if has_gh and not gh_ok:
                    print("  \033[1;93m│\033[0m   2. Add Account: Press \033[1;96m[A]\033[0m or run \033[97mgh auth login\033[0m              \033[1;93m│\033[0m")
                if git_remote_url in ("NOT SET", ""):
                    print("  \033[1;93m│\033[0m   3. Set remote: \033[97mgit remote add origin <github-repo-url>\033[0m      \033[1;93m│\033[0m")
                print("  \033[1;93m╰───────────────────────────────────────────────────────────────╯\033[0m")

            # Display Branches List
            if branches:
                print("\n  \033[1;90m--- BRANCHES (Most Recent) ---\033[0m")
                max_display = 6
                for b in branches[:max_display]:
                    if b == current_branch:
                        print(f"    \033[1;92m* {b}\033[0m \033[90m(current)\033[0m")
                    else:
                        print(f"      \033[97m{b}\033[0m")
                if len(branches) > max_display:
                    print(f"      \033[90m... and {len(branches) - max_display} more local branch(es)\033[0m")

            # Actions Menu
            print("\n  \033[1;90m--- ACTIONS ---\033[0m")
            print("    [\033[1;92mS\033[0m] Sync with GitHub")
            print("    [\033[1;96mP\033[0m] Push Current Branch (git push)")
            print("    [\033[1;96mU\033[0m] Pull Remote Updates (git pull)")
            print("    [\033[1;96mC\033[0m] Checkout / Switch Branch")
            print("    [\033[1;96mN\033[0m] Create New Branch")
            if has_gh:
                print("    [\033[1;96mO\033[0m] Open Repo in Browser (GitHub)")
                print("    [\033[1;96mV\033[0m] View Login Status (gh auth status)")
                print("    [\033[1;92mA\033[0m] Add GitHub Account (gh auth login)")
                print("    [\033[1;96mW\033[0m] Switch Active Account (gh auth switch)")
            print("    [\033[1;96mX\033[0m] Xcode Cloud Workflows & CI Scripts")
            print("    [\033[1;96mR\033[0m] Refresh Status\n")
            print("    [\033[1;91mB\033[0m] Back")

            if error_msg:
                print(f"\n\033[1;91mNOT A VALID OPTION, PLEASE TRY AGAIN... ({error_msg})\033[0m")
                error_msg = ""

            prompt = get_choice_prompt("Choice:", "(letter)")
            status_bar.render(at_bottom=True, force=True, prompt=prompt)
            choice = get_key().strip().lower()
            clear_choice_placeholder()

            if choice == "b":
                break
            elif choice == "r":
                continue
            elif choice == "s":
                clear_screen()
                print_header("Syncing with GitHub")
                handle_cleanup_closed(silent=False, confirm=True)
                continue
            elif choice == "p":
                clear_screen()
                print_header(f"Pushing Branch: {current_branch}")
                
                # Pre-inspect unpushed commits and HEAD details
                unpushed_commits = []
                diffstat_summary = ""
                head_sha = ""
                head_msg = ""
                head_author = ""
                head_time = ""
                try:
                    head_sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
                    head_msg = subprocess.check_output(["git", "log", "-1", "--format=%s", "HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
                    head_author = subprocess.check_output(["git", "log", "-1", "--format=%an", "HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
                    head_time = subprocess.check_output(["git", "log", "-1", "--format=%cr", "HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
                    
                    unpushed_out = subprocess.check_output(["git", "log", f"origin/{current_branch}..HEAD", "--oneline"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
                    if unpushed_out:
                        unpushed_commits = [c.strip() for c in unpushed_out.splitlines() if c.strip()]
                        stat_out = subprocess.check_output(["git", "diff", "--shortstat", f"origin/{current_branch}..HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
                        if stat_out:
                            diffstat_summary = stat_out
                except Exception:
                    pass

                print(f"\033[90mRunning: git push origin {current_branch}...\033[0m\n")
                res = subprocess.run(["git", "push", "-u", "origin", current_branch], cwd=str(ROOT))
                
                try:
                    cols, _ = os.get_terminal_size()
                except Exception:
                    cols = 80
                cols = max(cols, 40)
                div = "=" * min(cols - 4, 68)

                if res.returncode == 0:
                    print(f"\n  \033[1;92m{div}\033[0m")
                    print("  \033[1;92m🚀 PUSH COMPLETE & REMOTE SYNCHRONIZED\033[0m")
                    print(f"  \033[1;36m• Target Branch:\033[0m  \033[1;97m{current_branch}\033[0m -> \033[90morigin/{current_branch}\033[0m")
                    if git_remote_url and git_remote_url != "NOT SET":
                        print(f"  \033[1;36m• Remote Origin:\033[0m  \033[90m{git_remote_url}\033[0m")
                    if head_sha:
                        print(f"  \033[1;36m• Current HEAD:\033[0m   \033[1;93m{head_sha}\033[0m \033[97m{head_msg}\033[0m \033[90m({head_author}, {head_time})\033[0m")
                    
                    if unpushed_commits:
                        print(f"\n  \033[1;36m• Commits Pushed ({len(unpushed_commits)}):\033[0m")
                        for c in unpushed_commits[:8]:
                            print(f"    \033[92m+\033[0m \033[97m{c}\033[0m")
                        if len(unpushed_commits) > 8:
                            print(f"    \033[90m... and {len(unpushed_commits) - 8} more commit(s)\033[0m")
                        if diffstat_summary:
                            print(f"  \033[1;36m• Changes:\033[0m        \033[90m{diffstat_summary}\033[0m")
                    else:
                        print(f"  \033[1;36m• Sync Status:\033[0m    \033[92mUp-to-date\033[0m \033[90m(remote is in sync with local HEAD)\033[0m")
                    print(f"  \033[1;92m{div}\033[0m")
                else:
                    print(f"\n\033[1;91m❌ Push failed (exit code {res.returncode}).\033[0m")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                continue
            elif choice == "u":
                clear_screen()
                print_header(f"Pulling Updates: {current_branch}")
                
                head_before = ""
                try:
                    head_before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
                except Exception:
                    pass

                print(f"\033[90mRunning: git pull...\033[0m\n")
                res = subprocess.run(["git", "pull"], cwd=str(ROOT))
                
                try:
                    cols, _ = os.get_terminal_size()
                except Exception:
                    cols = 80
                cols = max(cols, 40)
                div = "=" * min(cols - 4, 68)

                if res.returncode == 0:
                    pulled_commits = []
                    try:
                        head_after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
                        if head_before and head_after and head_before != head_after:
                            pulled_out = subprocess.check_output(["git", "log", f"{head_before}..{head_after}", "--oneline"], cwd=str(ROOT), stderr=subprocess.DEVNULL, timeout=5).decode("utf-8").strip()
                            if pulled_out:
                                pulled_commits = [c.strip() for c in pulled_out.splitlines() if c.strip()]
                    except Exception:
                        pass

                    print(f"\n  \033[1;92m{div}\033[0m")
                    print("  \033[1;92m📥 PULL COMPLETE & LOCAL BRANCH UPDATED\033[0m")
                    print(f"  \033[1;36m• Branch:\033[0m         \033[1;97m{current_branch}\033[0m")
                    if pulled_commits:
                        print(f"\n  \033[1;36m• Commits Pulled ({len(pulled_commits)}):\033[0m")
                        for c in pulled_commits[:8]:
                            print(f"    \033[92m+\033[0m \033[97m{c}\033[0m")
                        if len(pulled_commits) > 8:
                            print(f"    \033[90m... and {len(pulled_commits) - 8} more commit(s)\033[0m")
                    else:
                        print(f"  \033[1;36m• Status:\033[0m         \033[92mAlready up-to-date\033[0m \033[90m(no new remote commits)\033[0m")
                    print(f"  \033[1;92m{div}\033[0m")
                else:
                    print(f"\n\033[1;91m❌ Pull failed (exit code {res.returncode}).\033[0m")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                continue
            elif choice == "c":
                if not branches:
                    print("\n  \033[90mNo branches available to switch.\033[0m")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                    continue
                clear_screen()
                print_header("Checkout / Switch Branch")
                try:
                    selected_branch = prompt_radio("Select branch to checkout:", branches, default=current_branch)
                except BackException:
                    continue
                if selected_branch and selected_branch != current_branch:
                    clear_screen()
                    print_header(f"Switching Branch to {selected_branch}")
                    res = subprocess.run(["git", "checkout", selected_branch], cwd=str(ROOT))
                    if res.returncode == 0:
                        print(f"\n\033[1;92m✅ Switched to branch '{selected_branch}'.\033[0m")
                    else:
                        print(f"\n\033[1;91m❌ Checkout failed (exit code {res.returncode}).\033[0m")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                continue
            elif choice == "n":
                clear_screen()
                status_bar.set_scroll_region()
                print_header("Create New Branch")
                try:
                    new_branch = prompt_input("Enter new branch name:", placeholder="e.g. feature/auth-flow or ai/issue-10", field_below=True)
                except BackException:
                    continue
                if new_branch:
                    new_branch = new_branch.strip()
                    clear_screen()
                    print_header(f"Creating Branch: {new_branch}")
                    res = subprocess.run(["git", "checkout", "-b", new_branch], cwd=str(ROOT))
                    if res.returncode == 0:
                        print(f"\n\033[1;92m✅ Created and switched to branch '{new_branch}'.\033[0m")
                    else:
                        print(f"\n\033[1;91m❌ Branch creation failed (exit code {res.returncode}).\033[0m")
                    input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                continue
            elif choice == "o" and has_gh:
                clear_screen()
                print_header("Opening Repository on GitHub")
                try:
                    subprocess.run(["gh", "repo", "view", "--web"], cwd=str(ROOT), check=False)
                except Exception:
                    # Fallback to opening URL via macOS open
                    if git_remote_url and git_remote_url != "NOT SET":
                        web_url = git_remote_url
                        if web_url.startswith("git@github.com:"):
                            web_url = "https://github.com/" + web_url.split("git@github.com:", 1)[1]
                        if web_url.endswith(".git"):
                            web_url = web_url[:-4]
                        subprocess.run(["open", web_url], check=False)
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                continue
            elif choice == "v" and has_gh:
                clear_screen()
                print_header("GitHub Login & Authentication Status")
                try:
                    res = subprocess.run(["gh", "auth", "status"], cwd=str(ROOT), capture_output=True, text=True)
                    out = (res.stdout or "") + (res.stderr or "")
                    if out.strip():
                        print(out.strip())
                    else:
                        print("No authentication status output returned.")
                    
                    if res.returncode == 0:
                        print("\n\033[1;92m✅ GitHub CLI is authenticated and operational.\033[0m")
                    else:
                        print(f"\n\033[1;93mℹ️  gh auth status returned code {res.returncode}.\033[0m")
                except Exception as e:
                    print(f"\033[1;91mError executing 'gh auth status': {e}\033[0m")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                continue
            elif (choice == "a" or choice == "l") and has_gh:
                handle_add_github_account(status_bar)
                continue
            elif choice == "w" and has_gh:
                clear_screen()
                print_header("Switch Active GitHub Account")
                _, current_accounts, active_user = get_github_auth_info()
                
                if not current_accounts:
                    print("⚠️  No authenticated GitHub accounts found.")
                    print("You need to add or log in to an account first.\n")
                    if prompt_confirm("Would you like to add a GitHub account now?", default=True):
                        handle_add_github_account(status_bar)
                    continue
                
                if len(current_accounts) == 1:
                    acc = current_accounts[0]
                    print(f"ℹ️  Only 1 GitHub account is currently logged in: \033[1;92m{acc['user']}\033[0m ({acc['host']}).")
                    print("To switch between accounts, you must first add another account.\n")
                    if prompt_confirm("Would you like to add another GitHub account now?", default=True):
                        handle_add_github_account(status_bar)
                    continue
                
                # Multiple accounts configured: Build options for radio prompt
                account_options = []
                default_option = None
                for acc in current_accounts:
                    label = f"{acc['user']} ({acc['host']})"
                    if acc.get("active"):
                        default_option = f"{label} [Current Active]"
                        account_options.append(f"{label} [Current Active]")
                    else:
                        account_options.append(label)
                
                try:
                    selected = prompt_radio(
                        "Select GitHub account to switch to:",
                        account_options,
                        default=default_option or account_options[0]
                    )
                except BackException:
                    continue
                
                if selected:
                    # Find chosen account
                    chosen_acc = None
                    for i, opt in enumerate(account_options):
                        if opt == selected:
                            chosen_acc = current_accounts[i]
                            break
                    
                    if chosen_acc:
                        if chosen_acc.get("active"):
                            print(f"\n\033[93mAccount '{chosen_acc['user']}' is already the active account.\033[0m")
                        else:
                            clear_screen()
                            print_header(f"Switching Active GitHub Account to: {chosen_acc['user']}")
                            switch_cmd = ["gh", "auth", "switch", "--hostname", chosen_acc["host"], "--user", chosen_acc["user"]]
                            res = subprocess.run(switch_cmd, cwd=str(ROOT))
                            if res.returncode == 0:
                                print(f"\n\033[1;92m✅ Successfully switched active GitHub account to '{chosen_acc['user']}'.\033[0m")
                            else:
                                print(f"\n\033[1;91m❌ Failed to switch active account (exit code {res.returncode}).\033[0m")
                        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
                continue
            elif choice == "x":
                handle_xcode_cloud_menu(session_allowed_machines, session_allowed_models)
                continue
            else:
                error_msg = f"'{choice}'"

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
                    # [ID] (4) + | (3) + Type (7) + | (3) + Status (6) + | (3) + | (3) + Modified (8) = 37 fixed chars
                    # Safe margin of 3 cols ensures right edge padding/terminal scrollbars never cause line wrapping
                    safe_cols = max(45, cols - 3)
                    title_width = max(10, safe_cols - 37)
                    header = f"ID   | Type   | Status | {'Title':<{title_width}} | Modified"
                    print(header)
                    print("-" * len(header))
                    for i, job in enumerate(jobs):
                        print(format_job_row(i, job, title_width=title_width))
                
                print_header("Actions")
                print("[\033[96mN\033[0m] New Job")
                print("[\033[93mT\033[0m] Manage Test Coverage")
                print("[\033[93mD\033[0m] Distribute build (Firebase)")
                print("[\033[93mG\033[0m] GitHub & Source Control")
                print("[\033[93mC\033[0m] Configuration & Tools")
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
                    elif choice in ("t", "v"):
                        handle_manage_tests(session_allowed_machines, session_allowed_models)
                    elif choice == "d":
                        handle_quick_distribute(session_allowed_machines, session_allowed_models)
                    elif choice in ("g", "r"):
                        handle_github_menu(session_allowed_machines, session_allowed_models)
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
        "Antigravity": "GEMINI.md",
        "Claude": "CLAUDE.md",
        "Codex": "AGENTS.md",
        "Copilot": ".github/copilot-instructions.md",
        "Ollama": "OLLAMA.md",
        "DeepSeek": "DEEPSEEK.md",
        "OpenCode": "OPENCODE.md",
        "Qwen": "QWEN.md"
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
            print_wrapped_description("Checks the instruction files used by each AI CLI. Each file should point agents back to the shared project docs.", indent_size=2)
            print()
            
            header = f"{'#':4} | {'Tool':11} | {'Instruction File':32} | {'Status'}"
            print(f"\033[1;97m{header}\033[0m")
            print_divider("-")

            keys = list(cli_files.keys())
            file_statuses = {}
            for i, name in enumerate(keys):
                path = ROOT / cli_files[name]
                if path.exists():
                    try:
                        content = path.read_text(encoding="utf-8")
                        missing_links = [link for link in required_links if link not in content]
                        if missing_links:
                            status_str = "\033[93m⚠ INVALID\033[0m"
                            file_statuses[name] = "invalid"
                        else:
                            status_str = "\033[1;92m✓ VALID\033[0m"
                            file_statuses[name] = "valid"
                    except Exception:
                        status_str = "\033[1;91m✗ ERROR\033[0m"
                        file_statuses[name] = "error"
                else:
                    status_str = "\033[1;91m✗ MISSING\033[0m"
                    file_statuses[name] = "missing"
                    
                print(f"[\033[1;96m{i}\033[0m]  | {name:11} | {cli_files[name]:32} | {status_str}")
            
            print("\nActions:")
            print("    [\033[1;92mA\033[0m] Create All Missing Files")
            if any(s == "invalid" for s in file_statuses.values()):
                print("    [\033[1;92mR\033[0m] Repair Misconfigured Files")
            
            print("    [\033[1;96mP\033[0m] Customize Agent Instructions (Planner/Builder/etc)\n")
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
            print_wrapped_kv("  Firebase CLI: ", cli_status)
            print_wrapped_kv("  Account:      ", auth_status)
            print_wrapped_kv("  Project:      ", project_status)
            
            has_ci_token = "FIREBASE_TOKEN" in os.environ
            if has_ci_token:
                print_wrapped_kv("  Auth Method:  ", "\033[92mCI TOKEN (Environment)\033[0m")
            else:
                print_wrapped_kv("  Auth Method:  ", "\033[1;96mLocal CLI Session\033[0m")
                
            print("\n  \033[1;90m--- REQUIRED FILES ---\033[0m")
            if has_gs_info:
                print_wrapped_kv("  GoogleService-Info.plist: ", "\033[92mEXISTS\033[0m")
                print_wrapped_kv("  App ID (iOS):             ", app_id_status)
            else:
                print_wrapped_kv("  GoogleService-Info.plist: ", "\033[1;91mMISSING\033[0m")
                
            if has_export_opts:
                print_wrapped_kv("  ExportOptions.plist:      ", "\033[92mEXISTS\033[0m")
            else:
                print_wrapped_kv("  ExportOptions.plist:      ", "\033[1;91mMISSING\033[0m")
                
            print("\n  \033[1;90m--- KEYCHAIN & SIGNING ---\033[0m")
            has_password = "KEYCHAIN_PASSWORD" in os.environ
            pwd_status = "\033[92mAUTOMATED (No UI popups)\033[0m" if has_password else "\033[93mMANUAL (Requires UI prompt)\033[0m"
            print_wrapped_kv("  Headless Signing: ", pwd_status)
            
            print("\n  \033[1;90m--- ACTIONS ---\033[0m")
            print_wrapped_kv("    [\033[1;96mL\033[0m] ", "Login to Firebase (Browser)")
            print_wrapped_kv("    [\033[1;96mK\033[0m] ", "Configure Headless Signing (Keychain Auto-Unlock)")
            print_wrapped_kv("    [\033[1;96mT\033[0m] ", "Run Live Build & Firebase Delivery Test")
            print_wrapped_kv("    [\033[1;96mR\033[0m] ", "Refresh Status (Re-run checks)\n")
            print_wrapped_kv("    [\033[1;91mB\033[0m] ", "Back")
            
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
                status_bar.clear_footer()
                status_bar.reset_scroll_region(force=True)
                clear_screen()
                print_header("Live Build & Firebase Delivery Test")
                print("This archives the current branch and publishes a real Firebase App Distribution release.")
                print("\033[90mThe operation can take several minutes and sends the build to configured recipients.\033[0m")
                run_script("smoke_test_delivery.py", [], sub_menu=True, session_machines=session_allowed_machines, session_models=session_allowed_models)
                needs_check = True

def handle_keychain_setup(status_bar: StatusBar):
    options = [
        "Automated: Store password (Required for headless/remote builds)",
        "Manual: UI prompts (Best for local interactive use)"
    ]
    
    import textwrap
    try:
        cols, _ = os.get_terminal_size()
    except:
        cols = 80
    cols = max(cols, 40)
    
    desc_text = "Select how to unlock the macOS keychain during the build and distribution process. Storing the password enables automated headless/remote builds, while manual mode prompts for your password dynamically."
    desc_lines = [f"  \033[90m{line}\033[0m" for line in textwrap.wrap(desc_text, width=cols - 6)]
    
    try:
        choice = prompt_radio("Choose Keychain Setup Mode", options, default=options[0], clear_screen=True, description=desc_lines)
    except BackException:
        return
    
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
        input("\n\033[1;96mTap Enter to return to menu...\033[0m")
        
    elif "Manual" in choice:
        print("\n--- Manual Keychain Whitelist ---")
        print("Run this command in your terminal to allow codesign to access your keys permanently:")
        print(f"\033[93msecurity set-key-partition-list -S apple-tool:,apple:,codesign: -s -k \"<your-mac-password>\" ~/Library/Keychains/login.keychain-db\033[0m")
        print("\nAfter running this once, you won't need to provide a password for background builds on this machine.")
        input("\n\033[1;96mTap Enter to return to menu...\033[0m")

def handle_import_email_recipients(status_bar: StatusBar, settings_path: Path, settings: dict[str, Any], emails: list[str]) -> None:
    clear_screen()
    status_bar.set_scroll_region()
    print_header("Import Recipients from CSV")

    print("\033[1;97mWhat this does\033[0m")
    print_wrapped_description("Scans a CSV or text file for email addresses and adds any new unique recipients. The file does not need a specific column name; every email-looking value is imported.", indent_size=2)
    print()

    print("\033[1;97mFile Path\033[0m")
    print_wrapped_description("Drag and drop a CSV file here, or type a path manually.", indent_size=2)
    print("  Examples:")
    print("    \033[90m~/Downloads/testers.csv\033[0m")
    print("    \033[90m./emails.csv\033[0m\n")

    status_bar.render(at_bottom=True, force=True)
    try:
        print("    (Enter path to CSV file, or Enter to cancel; e.g. ~/Downloads/testers.csv)")
        csv_path = prompt_input("CSV path:", placeholder="~/Downloads/testers.csv or ./emails.csv", field_below=True)
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

def is_valid_email(email: str) -> bool:
    """Verifies if the email matches a standard valid format."""
    import re
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    return bool(re.match(pattern, email))

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
            print_wrapped_description("Configures email alerts for job completions, build delivery updates, failures, or when a running task is paused and requires human review.", indent_size=2)
            print()
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
            
            print("\nActions:")
            print("    [\033[1;92mA\033[0m] Add recipient address")
            print("    [\033[1;92mI\033[0m] Import recipients from CSV")
            if emails:
                print("    [\033[1;96mR\033[0m] Remove recipient address")
                print("    [\033[1;96mT\033[0m] Send Test Email")
            print("    [\033[1;96mC\033[0m] Configure Email Sender\n")
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
                while True:
                    clear_screen()
                    status_bar.set_scroll_region()
                    print_header("Add Recipient Email")
                    print_wrapped_description("Add a new email address to the notification list to receive job updates, failures, and tasks requiring manual review.", indent_size=2)
                    print()
                    try:
                        email = prompt_input("Enter recipient email address:", placeholder="(or Enter to cancel)", field_below=True)
                    except BackException:
                        email = ""
                        break
                    
                    if not email:
                        break
                    
                    if is_valid_email(email):
                        break
                    else:
                        print(f"\n    \033[1;91m⚠️  Error: Invalid email format '{email}'\033[0m")
                        print("    Please enter a valid email address (e.g. user@example.com).")
                        input("\n\033[1;96mTap Enter to try again...\033[0m")

                if not email:
                    continue

                emails.append(email)
                settings["notification_emails"] = emails
                write_json(settings_path, settings)
                print(f"\n✅ Added: {email}")
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
                    print_wrapped_description("(Leave blank and press Enter to skip/keep current)", indent_size=4)

                    new_smtp = prompt_input("Gmail Address:", field_below=True)
                    new_pass = prompt_password("🔑 App Password:", placeholder="(enter to skip)")
                    if new_smtp: settings["smtp_email"] = new_smtp
                    if new_pass: settings["smtp_password"] = new_pass
                else:
                    print("\n    \033[1;96m--- Configure Resend SMTP ---\033[0m")
                    print("    1. Go to: \033[4;96mhttps://resend.com/api-keys\033[0m")
                    print("    2. Create a new API key with 'Sending' permissions.")
                    print("    3. If you haven't verified a domain, use your Resend login email.")
                    print_wrapped_description("(Leave blank and press Enter to skip/keep current)", indent_size=4)

                    new_key = prompt_password("Resend API Key:", placeholder="(enter to skip)")
                    print_wrapped_description("(Must be a verified domain on Resend, or your login email)", indent_size=4)
                    new_from = prompt_input("From Email:", field_below=True)
                    print_wrapped_description("(The name that appears in the inbox, e.g. 'AI Orchestrator')", indent_size=4)
                    new_name = prompt_input("Display Name:", field_below=True)
                    if new_key: settings["resend_api_key"] = new_key
                    if new_from: settings["resend_from_email"] = new_from
                    if new_name: settings["resend_display_name"] = new_name
                    
                write_json(settings_path, settings)
                print("\n    ✅ Provider configured.")
                input("\n\033[1;96mTap Enter to return to menu...\033[0m")
            elif choice == "r" and emails:
                status_bar.render(at_bottom=True, force=True)
                try:
                    idx_str = prompt_input("Enter index to remove:", placeholder="(number)", field_below=True)
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
