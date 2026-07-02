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

def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")

def format_log_path(path_str: str) -> str:
    """Formats a path string with a timestamp (YYYYMMDD-HHMMSS or YYYYMMDDTHHMMSS) to be more readable."""
    # Pattern to match YYYYMMDD-HHMMSS or YYYYMMDDTHHMMSS anywhere in the string
    pattern = r"(\d{4})(\d{2})(\d{2})[-T](\d{2})(\d{2})(\d{2})"
    
    def replacement(m):
        # Format as YYYY-MM-DD HH:MM:SS
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"
    
    return re.sub(pattern, replacement, path_str)

def now_iso() -> str:
    return datetime.now().isoformat()

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

def prompt_radio(label: str, options: list[str], default: str | None = None, clear_screen: bool = True, status_bar: StatusBar | None = None) -> str:
    """Displays interactive radio buttons navigated by arrow keys."""
    if not sys.stdin.isatty():
        return default or options[0]

    idx = 0
    if default and default in options:
        idx = options.index(default)

    # Hide cursor
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    def render_radio_option(option: str, option_idx: int, selected_idx: int, highlighted: bool = True) -> str:
        cursor = ">" if option_idx == selected_idx else " "
        icon = "[x]" if option_idx == selected_idx else "[ ]"
        prefix = f"{cursor} {icon}  "
        line = f"{prefix}{option}"
        if highlighted and option_idx == selected_idx:
            hl = "\033[1;97;48;5;25m"
            res = "\033[0m"
            line = f"{hl}{prefix}{option.replace(res, hl)}\033[K{res}"
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
            output.append(get_header_string(label))
            output.append("\033[1;90m(Arrows: navigate, Enter: select, B: back)\033[0m")
            output.append("")

            for i, opt in enumerate(options):
                if opt.startswith("---"):
                    output.append(f"  \033[1;90m{opt}\033[0m")
                    continue
                output.append(render_radio_option(opt, i, idx))

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

def prompt_confirm(question: str, default: bool = True) -> bool:
    """Displays interactive yes/no radio buttons."""
    # Defensive logic for reported UI duplication
    if question.startswith("Would you Would you"):
        question = "Would you" + question[len("Would you Would you"):]
    
    default_str = "yes" if default else "no"

    choice = prompt_radio(question, ["yes", "no"], default_str)
    return choice == "yes"

def prompt_multiline(prompt: str) -> str:
    flush_stdin()
    print(f"\n\033[1;97m{prompt}\033[0m", flush=True)
    print("\033[90m(Type your input. To finish, press Enter then \033[1;97mCtrl-D\033[0m\033[90m on a new line)\033[0m", flush=True)
    if _ACTIVE_STATUS_BAR:
        _ACTIVE_STATUS_BAR.render(at_bottom=True, force=True, q_msg="Ctrl-D to finish")
    try:
        content = sys.stdin.read()
        return content.strip()
    except EOFError:
        return ""

def prompt_password(label: str, placeholder: str = "(enter to skip)") -> str:
    """Interactive password input that shows asterisks instead of clear text."""
    if not sys.stdin.isatty():
        try:
            import getpass
            return getpass.getpass(f"{label}: ").strip()
        except EOFError:
            return ""

    # Ensure cursor is visible
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()

    if _ACTIVE_STATUS_BAR:
        _ACTIVE_STATUS_BAR.render(at_bottom=True, force=True, q_msg="Enter to skip")

    input_text = ""
    bg_style = "\033[48;5;236m"
    fg_style = "\033[1;97m" # Bold White
    placeholder_style = "\033[90m" # Grey
    reset = "\033[0m"
    prompt_label = f"\033[1;96m{label}\033[0m"

    try:
        while True:
            # Render current state as asterisks
            if not input_text and placeholder:
                display = f"{placeholder_style}{placeholder}{reset}"
            else:
                display = f"{fg_style}{'*' * len(input_text)}{reset}"
            
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
        pass

def prompt_input(label: str, placeholder: str = "", default: str = "", allow_back: bool = False) -> str:
    """Interactive text input with styling, backspace handling, and back-out support."""
    if not sys.stdin.isatty():
        try:
            prompt_label = f"{label} (Enter to use '{default}'): " if default else f"{label}: "
            return input(prompt_label).strip() or default
        except EOFError:
            return default

    # Ensure cursor is visible
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()

    input_text = default
    bg_style = "\033[48;5;236m"
    fg_style = "\033[1;97m" # Bold White
    placeholder_style = "\033[90m" # Grey
    reset = "\033[0m"
    prompt_label = f"\033[1;96m{label}\033[0m"
    
    if default:
        print(f"\n\033[90mTip: Press Enter to accept the current value: '{default}'\033[0m")

    try:
        while True:
            # Render current state
            display = input_text
            if not input_text and placeholder:
                display = f"{placeholder_style}{placeholder}{reset}"
            else:
                display = f"{fg_style}{input_text}{reset}"
            
            # Construct the line (indented to match other prompts)
            line = f"\r    {prompt_label}  {bg_style} {display} {reset}\033[K"
            sys.stdout.write(line)
            sys.stdout.flush()
            
            key = get_key()
            
            if key == "enter":
                sys.stdout.write("\n")
                sys.stdout.flush()
                return input_text.strip().strip("'\"") # Remove quotes if pasted
            elif key == "backspace":
                input_text = input_text[:-1]
            elif key == "escape" or key == "esc":
                raise BackException()
            elif len(key) == 1:
                input_text += key
    finally:
        pass

    sys.stdout.flush()

    if _ACTIVE_STATUS_BAR:
        q_msg = "Enter to confirm" if default else "Enter to cancel"
        _ACTIVE_STATUS_BAR.render(at_bottom=True, force=True, q_msg=q_msg)

    input_text = default
    bg_style = "\033[48;5;236m"
    fg_style = "\033[1;97m" # Bold White
    placeholder_style = "\033[90m" # Grey
    reset = "\033[0m"
    prompt_label = f"\033[1;96m{label}\033[0m"

    try:
        while True:
            # Render current state
            display = input_text
            if not input_text and placeholder:
                display = f"{placeholder_style}{placeholder}{reset}"
            else:
                display = f"{fg_style}{input_text}{reset}"
            
            # Construct the line (indented to match other prompts)
            line = f"\r    {prompt_label} {bg_style} {display} {reset}\033[K"
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

def print_divider(char: str = "-"):
    try:
        columns, _ = os.get_terminal_size()
    except:
        columns = 80
    print(char * (columns - 2), flush=True)

def format_job_id(job_id: str) -> str:
    return f"\033[1;97m{job_id}\033[0m"

def format_index(i: int) -> str:
    return f"[\033[96m{i}\033[0m]"

def prompt_checkbox(label: str, options: list[str], defaults: list[str] | None = None, extra_keys: list[str] | None = None, footer: str | None = None, details_map: dict[str, list[str]] | None = None, status_bar: StatusBar | None = None, clear_screen: bool = True, max_selections: int | None = None, footer_actions: list[str] | None = None, details_title: str | None = None) -> list[str]:
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
            output.append(get_header_string(label))
            output.append("\033[1;90m(Arrows: navigate, Space: toggle, Enter: save, B: back)\033[0m")
            output.append("")

            
            if error_msg:
                output.append(f"\033[1;91m      ⚠️  {error_msg}\033[0m")
                error_msg = ""

            # 2. Print options
            for i, opt in enumerate(options):
                if opt.startswith("---"):
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
                    if details_title:
                        output.append(f"\n  \033[1;96m{details_title}\033[0m")
                    for line in details:
                        if line.startswith("\033"):
                            output.append(line)
                        else:
                            output.append(f"\033[90m{line}\033[0m")
            
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
                    final_render.append(get_header_string(label))
                    final_render.append("\033[1;90m(Arrows: navigate, Space: toggle, Enter: save, B: back)\033[0m")
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

    def default_xcode_command(action: str) -> str:
        if PROJECT_CONFIG.xcode_workspace:
            base = f"xcodebuild {action} -workspace {shlex.quote(PROJECT_CONFIG.xcode_workspace)}"
        elif PROJECT_CONFIG.xcode_project:
            base = f"xcodebuild {action} -project {shlex.quote(PROJECT_CONFIG.xcode_project)}"
        else:
            base = f"xcodebuild {action}"
        if PROJECT_CONFIG.scheme:
            base += f" -scheme {shlex.quote(PROJECT_CONFIG.scheme)}"
        return base

    default_build = PROJECT_CONFIG.build_command or default_xcode_command("build")
    default_test = PROJECT_CONFIG.test_command or default_xcode_command("test")

    build = default_build
    test = default_test

    if doc_path.exists():
        content = doc_path.read_text(encoding="utf-8")
        # More robust regex handling spaces and newlines
        build_match = re.search(r"## iOS app build[\s\S]*?```bash\s+([\s\S]*?)\s+```", content)
        test_match = re.search(r"## iOS app tests[\s\S]*?```bash\s+([\s\S]*?)\s+```", content)
        build = build_match.group(1).strip() if build_match else default_build
        test = test_match.group(1).strip() if test_match else default_test

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
def get_best_simulator_destination() -> str:
    """
    Intelligently finds the best available iOS simulator destination.
    Priority: 1. Booted devices, 2. Latest iOS version, 3. iPhone over iPad.
    """
    try:
        def parse_version(v_str: str) -> tuple[int, ...]:
            # Extract numbers from string like 'iOS 18.2' or 'com.apple.CoreSimulator.SimRuntime.iOS-18-0'
            parts = re.findall(r"(\d+)", v_str)
            return tuple(map(int, parts))

        # We use 'available' to exclude simulators that don't have a matching runtime installed
        res = subprocess.run(["xcrun", "simctl", "list", "devices", "available", "--json"], capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        devices_by_runtime = data.get("devices", {})

        all_candidates = []
        for runtime, devices in devices_by_runtime.items():
            if "iOS" not in runtime:
                continue

            version = parse_version(runtime)
            for d in devices:
                if d.get("isAvailable") == False: continue # simctl 'available' flag is not enough

                all_candidates.append({
                    "udid": d["udid"],
                    "name": d["name"],
                    "booted": d.get("state") == "Booted",
                    "version": version,
                    "is_iphone": "iPhone" in d["name"]
                })

        if not all_candidates:
            return "platform=iOS Simulator,name=iPhone 16"

        # Sort: Booted first, then latest version, then iPhone, then Name
        # Using negative numbers for boolean/tuple sorts for descending order
        def sort_key(c):
            return (
                not c["booted"],     # False (Booted) comes before True
                [-x for x in c["version"]], # Higher versions first
                not c["is_iphone"],  # iPhone first
                c["name"]
            )

        best = sorted(all_candidates, key=sort_key)[0]

        # Include arch=arm64 on Apple Silicon to resolve ambiguous destination warnings
        # and prevent 'device not found' when multiple archs exist for the same UDID.
        import platform
        is_arm = platform.machine().startswith("arm") or platform.processor().startswith("arm")
        arch_suffix = ",arch=arm64" if is_arm else ""

        return f"platform=iOS Simulator,id={best['udid']}{arch_suffix}"

    except Exception:
        # Final hardcoded fallback if everything fails
        return "platform=iOS Simulator,name=iPhone 16"
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
        return f"\033[1;96m{spinner}\033[0m {self.label}... \033[90m({self.hint}, {timer_str}{activity_str})\033[0m"

    def render(self, force: bool = False, last_activity_time: float | None = None):
        if self.is_silent: return
        now = time.monotonic()
        if not force and now - self.last_render_time < 0.1: return
        
        # If a status bar is active, we don't draw directly.
        # The parent loop will call StatusBar.render(activity=indicator)
        if _STATUS_BAR_NESTING == 0:
            self.hide_cursor()
            sys.stdout.write(f"\0337\r\033[K{self.get_line(last_activity_time)}\0338")
            sys.stdout.flush()
        self.last_render_time = now

    def clear(self):
        if self.is_silent: return
        if _STATUS_BAR_NESTING == 0:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        self.restore_cursor()

_STATUS_BAR_NESTING = 0
_ACTIVE_STATUS_BAR = None

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
                # \033[r: reset scroll region
                # \033[?25h: show cursor
                # \033[{lines};1H\n: move cursor to bottom line and print newline to avoid overwriting content
                sys.stdout.write(f"\033[r\033[?25h\033[{lines};1H\n")
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

def get_header_string(text: str) -> str:
    """Returns a centered header string with equals signs in cyan, responsive to terminal width."""
    text = text.strip().rstrip(":").upper()
    text = re.sub(r"\(([^)]*)\)", lambda match: f"({match.group(1).lower()})", text)
    try:
        cols, _ = os.get_terminal_size()
    except:
        cols = 80
    
    # Use a safe width (cols - 2) to prevent bleeding onto new lines
    safe_cols = cols - 2
    
    # Account for the spaces around text
    side_padding = (safe_cols - len(text) - 2) // 2
    if side_padding < 2: side_padding = 2
    
    # Final check: if text itself is too long, don't use padding at all
    if len(text) + 6 > safe_cols:
        return f"\n\033[1;96m== {text} ==\033[0m"
    else:
        return f"\n\033[1;96m{'=' * side_padding} {text} {'=' * side_padding}\033[0m"

def print_header(text: str):
    """Prints a centered header with equals signs, responsive to terminal width."""
    print(get_header_string(text))

def print_phase(phase: str, subtext: str | None = None):
    p_map = {
        "planning": ("🧠", "PLANNING"),
        "scheduling": ("📡", "SCHEDULING"),
        "execution": ("🏗️", "EXECUTION"),
        "verification": ("🧪", "VERIFICATION"),
        "review": ("👀", "REVIEW"),
        "complete": ("✅", "COMPLETE")
    }
    icon, label = p_map.get(phase, ("⚙️", phase.upper()))
    
    header_text = f"{icon} {label}"
    if subtext:
        header_text += f": {subtext.upper()}"
        
    try:
        cols, _ = os.get_terminal_size()
    except:
        cols = 80
        
    # Account for the fact that emojis like 🧠 might take 2 cells in some terminals 
    # but Python len() might be different. Let's be conservative.
    # visible_len = len(header_text) # This might be slightly off for emojis
    # But for our purposes, a rough estimate is fine.
    
    # Safe width margin (cols - 2)
    side_padding = (cols - len(header_text) - 8) // 2
    if side_padding < 2: side_padding = 2

    print(f"\n\033[1;96m{'=' * side_padding} {header_text} {'=' * side_padding}\033[0m")


def get_phase_name(phase: str) -> str:
    return phase.replace("-", " ").capitalize()


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
    except:
        cols = 80
        
    max_width = min(80, cols - 4)
    if max_width < 40:
        max_width = 40
        
    in_code_block = False
    code_block_lines = []
    
    import textwrap
    
    for line in lines:
        stripped = line.strip()
        
        # Handle code blocks
        if stripped.startswith("```"):
            if in_code_block:
                # End of code block: draw box
                formatted_lines.append("  \033[90m┌" + "─" * (max_width - 4) + "\033[0m")
                for c_line in code_block_lines:
                    formatted_lines.append(f"  \033[90m│\033[0m \033[92m{c_line}\033[0m")
                formatted_lines.append("  \033[90m└" + "─" * (max_width - 4) + "\033[0m")
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
            formatted_lines.append("")
            formatted_lines.append(f" \033[1;95m{title.upper()}\033[0m")
            formatted_lines.append(f" \033[1;95m" + "━" * len(title) + "\033[0m")
            formatted_lines.append("")
            continue
        elif stripped.startswith("## "):
            title = stripped[3:]
            formatted_lines.append("")
            formatted_lines.append(f" \033[1;96m{title}\033[0m")
            formatted_lines.append(f" \033[96m" + "─" * len(title) + "\033[0m")
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
