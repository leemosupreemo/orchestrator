#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import json
import re
import shutil
import time
import threading
import shlex
from pathlib import Path

# Add scripts dir to path
import sys
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from common import CONFIG_DIR, DOCS_DIR, ROOT, read_json, print_header, print_divider, ProgressIndicator, StatusBar
from model_registry import cli_is_available, preferred_cli
from orchestrator.project_config import PROJECT_CONFIG

REMEDIATION_GUIDE = {
    "Homebrew": {
        "url": "https://brew.sh",
        "cmd": '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    },
    "Node.js": {
        "url": "https://nodejs.org",
        "cmd": 'brew install node'
    },
    "GitHub CLI (gh)": {
        "url": "https://cli.github.com",
        "cmd": "brew install gh",
        "auth": "gh auth login"
    },
    "Antigravity CLI": {
        "url": "https://antigravity.google/",
        "cmd": "agy",
        "auth": "agy"
    },
    "Claude Code": {
        "url": "https://code.claude.com",
        "cmd": "npm install -g @anthropic-ai/claude-code",
        "auth": "claude auth login"
    },
    "Codex CLI": {
        "url": "https://github.com/openai/codex",
        "cmd": "npm install -g @openai/codex",
        "auth": "codex login"
    },
    "Ollama": {
        "url": "https://ollama.com",
        "cmd": "brew install ollama"
    },
    "OpenCode": {
        "url": "https://opencode.ai",
        "cmd": "curl -fsSL https://opencode.ai/install.sh | sh",
        "auth": "opencode auth login"
    },
    "Xcode CLI Tools": {
        "url": "https://developer.apple.com/xcode/",
        "cmd": "xcode-select --install"
    }
}

def print_result(success: bool, name: str, info: str = "", fix_key: str | None = None):
    icon = "✅" if success else "❌"
    print(f"  {icon} {name:30} {info}")
    if not success and fix_key:
        print_remediation(fix_key)

def print_optional_result(success: bool, name: str, info: str = ""):
    icon = "✅" if success else "○"
    status = info or ("DETECTED" if success else "OPTIONAL")
    print(f"  {icon} {name:30} {status}")

def print_remediation(key: str):
    guide = REMEDIATION_GUIDE.get(key)
    if not guide:
        print(f"     \033[93m└─ RECOMMENDATION: {key}\033[0m")
        return
    print(f"     \033[93m└─ INSTALL: {guide['cmd']}\033[0m")
    print(f"        \033[1;96mDocs   : {guide['url']}\033[0m")
    if "auth" in guide:
        print(f"        \033[95mAuth   : {guide['auth']}\033[0m")

def check_file_content(path: Path, pattern: str) -> bool:
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    return re.search(pattern, content) is not None

def check_cli_auth(cli_name: str) -> tuple[bool, str]:
    """Checks if a CLI is installed and authenticated."""
    if shutil.which(cli_name) is None:
        return False, "\033[1;91mNOT INSTALLED\033[0m"
    
    try:
        if cli_name == "claude":
            res = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=5)
            ready = res.returncode == 0
        elif cli_name == "codex":
            res = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=5)
            ready = res.returncode == 0
        elif cli_name in {"gemini", "antigravity"}:
            # Antigravity/Gemini CLI doesn't have a status command yet, so we assume if it's installed it's ready
            ready = True
        elif cli_name == "opencode":
            res = subprocess.run(["opencode", "auth", "status"], capture_output=True, text=True, timeout=5)
            ready = res.returncode == 0
        elif cli_name == "ollama":
            ready = True
        else:
            ready = True
            
        return ready, "\033[92mREADY\033[0m" if ready else "\033[1;91mNOT LOGGED IN\033[0m"
    except Exception as e:
        return True, f"INSTALLED (Error checking status: {e})"

def get_auth_command(cli_name: str) -> str | None:
    """Returns the auth command for a given CLI binary name."""
    mapping = {
        "gh": "gh auth login",
        "gemini": preferred_cli("gemini"),
        "antigravity": preferred_cli("gemini"),
        "agy": "agy",
        "claude": "claude auth login",
        "codex": "codex login",
        "opencode": "opencode auth login"
    }
    return mapping.get(cli_name)

def should_render_progress_ui() -> bool:
    if os.environ.get("AI_PROGRESS_SILENT") == "1":
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()

def codex_home() -> Path:
    try:
        return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    except Exception:
        return Path("/tmp/.codex")

def read_codex_config_text() -> str:
    try:
        config_path = codex_home() / "config.toml"
        return config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except Exception:
        return ""

def detect_codex_integration(names: list[str]) -> bool:
    try:
        haystacks = [read_codex_config_text().lower()]
        plugin_cache = codex_home() / "plugins" / "cache"
        if plugin_cache.exists():
            try:
                haystacks.extend(str(path).lower() for path in plugin_cache.rglob("*"))
            except Exception:
                pass
        return any(name.lower() in haystack for name in names for haystack in haystacks)
    except Exception:
        return False

def run_with_activity(label: str, operation):
    """Run a blocking check while keeping an inline progress spinner alive."""
    if not should_render_progress_ui():
        return operation()

    indicator = ProgressIndicator(label=label, hint="working")
    result = {}

    def target():
        try:
            result["value"] = operation()
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    
    # Hide cursor
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()
    
    try:
        while thread.is_alive():
            sys.stdout.write(f"\r\033[K{indicator.get_line()}")
            sys.stdout.flush()
            time.sleep(0.1)
    finally:
        thread.join()
        sys.stdout.write("\r\033[K")
        sys.stdout.write("\033[?25h")  # Restore cursor
        sys.stdout.flush()

    if "error" in result:
        raise result["error"]
    return result.get("value")

def xcode_list_command() -> list[str]:
    args = ["xcodebuild", "-list", "-json"]
    if PROJECT_CONFIG.xcode_workspace:
        args.extend(["-workspace", PROJECT_CONFIG.xcode_workspace])
    elif PROJECT_CONFIG.xcode_project:
        args.extend(["-project", PROJECT_CONFIG.xcode_project])
    return args

def check():
    try:
        return _check()
    except KeyboardInterrupt:
        sys.stdout.write("\n\n\033[1;91mCheck aborted by user.\033[0m\n")

def _check():
    print_header("AI Agent 'Plug & Play' Verification")
    settings_path = CONFIG_DIR / "settings.json"
    
    # 0. Check Core Prerequisites
    print_header("1. Core Prerequisites")
    has_brew = shutil.which("brew") is not None
    print_result(has_brew, "Homebrew", "INSTALLED" if has_brew else "MISSING", "Homebrew")

    has_node = shutil.which("node") is not None
    print_result(has_node, "Node.js", "INSTALLED" if has_node else "MISSING", "Node.js")

    if PROJECT_CONFIG.uses_xcode:
        has_xcode = shutil.which("xcodebuild") is not None
        print_result(has_xcode, "Xcode CLI Tools", "INSTALLED" if has_xcode else "MISSING", "Xcode CLI Tools")
    
    # Git Check
    print_header("1a. Git Repository Sanity")
    is_git = (ROOT / ".git").exists()
    print_result(is_git, "Git Repository", "OK" if is_git else "MISSING", "Run 'git init' to initialize a git repository in the project root")
    if is_git:
        try:
            def git_status():
                return subprocess.run(
                    ["git", "status", "--short"],
                    cwd=str(ROOT),
                    text=True,
                    capture_output=True,
                    check=False,
                )

            res = run_with_activity("Refreshing Git index", git_status)
            status = res.stdout.strip() if res.returncode == 0 else ""
            if status:
                print(f"     \033[1;93m⚠️  Warning: Uncommitted changes detected.\033[0m")
                print("        AI workflows work best with a clean slate.")
            else:
                print(f"     \033[1;92m✅ Repository is clean.\033[0m")

            # Check for hardcoded token in remote URL or insecure remote protocol
            try:
                remote_url = subprocess.check_output(
                    ["git", "remote", "get-url", "origin"],
                    cwd=str(ROOT),
                    text=True,
                    stderr=subprocess.DEVNULL
                ).strip()
            except:
                remote_url = ""

            if remote_url:
                if "github_pat_" in remote_url or (remote_url.startswith("http") and "@" in remote_url):
                    print(f"     \033[1;91m❌ Warning: Git remote contains hardcoded credentials/PAT.\033[0m")
                    print("        This can lead to 403 authorization failures if the token lacks write access,")
                    print("        and leaks credentials in .git/config and project.json.")
                    print("        Remediation: Update to use SSH:")
                    print("          git remote set-url origin git@github.com:owner/repo.git")
                elif remote_url.startswith("https://"):
                    print(f"     \033[1;93m⚠️  Note: Git remote uses HTTPS.\033[0m")
                    print("        If automation fails to push, ensure you are authenticated or switch to SSH:")
                    print("          git remote set-url origin git@github.com:owner/repo.git")
                else:
                    print(f"     \033[1;92m✅ Git remote uses secure protocol/SSH.\033[0m")
        except: pass
    
    # 1. Check Essential Files
    print_header("2. Project Config")
    
    if PROJECT_CONFIG.firebase_distribution:
        app_dir = ROOT / PROJECT_CONFIG.project_name
        gs_path = app_dir / "GoogleService-Info.plist"
        if gs_path.exists():
            is_ok = check_file_content(gs_path, r"<key>PROJECT_ID</key>")
            print_result(is_ok, "GoogleService-Info.plist", "OK" if is_ok else "INVALID")
        else:
            print_result(False, "GoogleService-Info.plist", "MISSING",
                         f"Place in {PROJECT_CONFIG.project_name}/GoogleService-Info.plist")
    else:
        print_result(True, "Firebase config", "SKIPPED")

    m_path = CONFIG_DIR / "machines.json"
    print_result(m_path.exists(), "machines.json", "OK" if m_path.exists() else "MISSING",
                 f"Define your fleet in {m_path}")

    # Xcode Sanity
    if PROJECT_CONFIG.xcode_project or PROJECT_CONFIG.xcode_workspace:
        print_header("2a. Xcode Build Settings")
        cmd = xcode_list_command()
        display_cmd = " ".join(shlex.quote(part) for part in cmd)
        try:
            # Quick check if scheme exists
            res = run_with_activity(
                "Reading Xcode build settings",
                lambda: subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=45),
            )
            if res.returncode != 0:
                stderr = (res.stderr or res.stdout or "").strip().splitlines()
                detail = stderr[0] if stderr else f"Command failed: {display_cmd}"
                print(f"     \033[1;91m⚠️  Error checking Xcode: {detail}\033[0m")
                print(f"        \033[90mCommand: {display_cmd}\033[0m")
            elif PROJECT_CONFIG.scheme and PROJECT_CONFIG.scheme in res.stdout:
                print_result(True, f"Scheme: {PROJECT_CONFIG.scheme}", "FOUND")
            else:
                print_result(False, f"Scheme: {PROJECT_CONFIG.scheme}", "NOT FOUND", f"Ensure scheme exists in '{display_cmd}'")
            
            if PROJECT_CONFIG.test_target and PROJECT_CONFIG.test_target in res.stdout:
                print_result(True, f"Test Target: {PROJECT_CONFIG.test_target}", "FOUND")
            else:
                # Test targets aren't always in -list stdout as directly as schemes, but good enough for a heuristic
                pass
        except subprocess.TimeoutExpired:
            print("     \033[1;91m⚠️  Error checking Xcode: timed out after 45 seconds.\033[0m")
            print(f"        \033[90mCommand: {display_cmd}\033[0m")
            print("        \033[90mTry running it directly; Xcode may be indexing, resolving packages, or waiting on a corrupted project state.\033[0m")
        except Exception as e:
            print(f"     \033[1;91m⚠️  Error checking Xcode: {e}\033[0m")

    # iOS Code Signing & Distribution check
    if PROJECT_CONFIG.firebase_distribution:
        print_header("2b. iOS Code Signing & Distribution")
        
        has_team = bool(PROJECT_CONFIG.development_team)
        print_result(has_team, "Development Team ID", "CONFIGURED" if has_team else "MISSING", 
                     "Set 'development_team' in .orchestrator/project.json")
        
        has_asc = all([PROJECT_CONFIG.asc_key_id, PROJECT_CONFIG.asc_issuer_id, PROJECT_CONFIG.asc_key_path])
        has_manual = bool(PROJECT_CONFIG.provisioning_profile_specifier)
        
        if has_asc:
            key_path_str = PROJECT_CONFIG.asc_key_path
            p = Path(key_path_str).expanduser()
            if not p.is_absolute():
                p = (ROOT / p).resolve()
            else:
                p = p.resolve()

            home = Path.home().resolve()
            secure_paths = [
                home / ".private_keys",
                home / ".appstoreconnect" / "private_keys"
            ]
            is_secure_dir = any(p.parent == sp for sp in secure_paths)

            if is_secure_dir:
                print_result(True, "Signing Credentials", "ASC API KEYS (HEADLESS)")
            else:
                print_result(False, "Signing Credentials", "ASC API KEYS (INSECURE LOCATION)")
                print("     \033[93m└─ WARNING: xcodebuild ignores/rejects API keys unless they are in ~/.private_keys/ or ~/.appstoreconnect/private_keys/\033[0m")
                print(f"        Current path: {key_path_str}")
                print(f"        Remediation: Move key to ~/.private_keys/{p.name} and update 'asc_key_path' in .orchestrator/project.json")

            if p.exists():
                print_result(True, "ASC Key File", "FOUND")
                try:
                    mode = p.stat().st_mode
                    is_owner_only = (mode & 0o077) == 0
                    print_result(is_owner_only, "ASC Key Permissions", "SECURE (600)" if is_owner_only else "TOO OPEN")
                    if not is_owner_only:
                        print(f"     \033[93m└─ REMEDIATION: Run 'chmod 600 {p}' to secure the key\033[0m")
                except Exception as e:
                    print_result(False, "ASC Key Permissions", f"ERROR: {e}")
            else:
                print_result(False, "ASC Key File", f"NOT FOUND ({key_path_str})")
        elif has_manual:
            print_result(True, "Signing Credentials", "MANUAL PROFILE SPECIFIED")
            try:
                from setup_distribution import installed_provisioning_profile_names
                installed_profiles = installed_provisioning_profile_names()
                profile_name = PROJECT_CONFIG.provisioning_profile_specifier
                if profile_name in installed_profiles:
                    print_result(True, f"Provisioning Profile ({profile_name})", "FOUND (INSTALLED)")
                else:
                    print_result(False, f"Provisioning Profile ({profile_name})", "NOT FOUND", 
                                 f"Ensure the profile is downloaded and installed in ~/Library/MobileDevice/Provisioning Profiles/")
                    if installed_profiles:
                        print("        Installed profiles found on this machine:")
                        for name in sorted(installed_profiles):
                            print(f"          - {name}")
            except Exception as e:
                print_result(True, "Provisioning Profile check", f"SKIPPED (Check error: {e})")
        else:
            print_result(False, "Signing Credentials", "NOT CONFIGURED", "Neither ASC API keys nor Manual Provisioning Profile Specifier were found.")
            print("\n     \033[1;93m⚠️  iOS Distribution requires code signing setup.\033[0m")
            print("        For headless, CI, or remote Mac builds, choose one of the following:")
            print("\n        \033[1;96mOption A: Headless Auto-Signing (Recommended)\033[0m")
            print("          1. Go to App Store Connect -> Users and Access -> Integrations -> Keys.")
            print("          2. Generate an API Key (Developer or App Manager role) and download the .p8 file.")
            print("          3. Update your .orchestrator/project.json with:")
            print("             - \"asc_key_id\": \"<Key ID>\"")
            print("             - \"asc_issuer_id\": \"<Issuer ID>\"")
            print("             - \"asc_key_path\": \"<Path to your download .p8 file>\"")
            print("\n        \033[1;96mOption B: Manual Signing\033[0m")
            print("          1. Create and download an Ad-Hoc/Distribution Provisioning Profile from Apple Developer Portal.")
            print("          2. Install the profile locally on the build machine.")
            print("          3. Set the profile name in your .orchestrator/project.json:")
            print("             - \"provisioning_profile_specifier\": \"<Profile Name>\"")
            print("")

    # Grounding Docs
    print_header("3. Grounding Docs (Critical for AI)")
    docs = [
        ("Architecture", "docs/architecture.md"), 
        ("Standards", "docs/coding-standards.md"), 
        ("Build Commands", "docs/build-test-commands.md"), 
        ("AI Rules", "AGENTS.md")
    ]
    for name, d in docs:
        exists = (ROOT / d).exists()
        print_result(exists, f"Doc: {name}", "OK" if exists else "MISSING", f"Create grounding doc file at '{d}' under the project root")
        if not exists:
            print(f"        \033[90m(File: {d})\033[0m")

    # 3. Check GitHub Environment
    print_header("4. GitHub Integration")
    has_gh = shutil.which("gh") is not None
    print_result(has_gh, "GitHub CLI (gh)", "INSTALLED" if has_gh else "MISSING", "GitHub CLI (gh)")
    if has_gh:
        try:
            res = run_with_activity(
                "Checking GitHub authentication",
                lambda: subprocess.run(["gh", "auth", "status"], capture_output=True, text=True),
            )
            is_auth = res.returncode == 0
            print_result(is_auth, "GitHub Auth session", "LOGGED IN" if is_auth else "EXPIRED", "GitHub CLI (gh)")
        except: pass

    # 4. Check LLM Providers
    print_header("5. AI Providers (Need 1+)")
    
    env_keys = ["GEMINI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY", "OPENAI_API_KEY"]
    has_env_key = any(k in os.environ for k in env_keys)
    
    settings_keys = ["gemini_api_key", "anthropic_api_key", "openai_api_key"]
    has_settings_key = False
    if settings_path.exists():
        try:
            s_data = read_json(settings_path)
            has_settings_key = any(s_data.get(k) for k in settings_keys)
        except: pass
    
    print_result(has_env_key or has_settings_key, "Manual API Keys", "CONFIGURED" if (has_env_key or has_settings_key) else "NONE")

    # CLI Status Helpers
    def check_cli_status(cli_name, fix_key, status_args):
        installed = shutil.which(cli_name) is not None
        if not installed:
            print_result(False, f"{cli_name} CLI", "MISSING", fix_key)
            return False
        
        try:
            res = run_with_activity(
                f"Checking {cli_name} authentication",
                lambda: subprocess.run([cli_name] + status_args, capture_output=True, text=True, timeout=5),
            )
            is_auth = res.returncode == 0
            print_result(is_auth, f"{cli_name} Auth", "LOGGED IN" if is_auth else "NOT LOGGED IN", fix_key if not is_auth else None)
            return is_auth
        except:
            print_result(True, f"{cli_name} CLI", "INSTALLED (Status Check Error)")
            return True

    claude_ok = check_cli_status("claude", "Claude Code", ["auth", "status"])
    codex_ok = check_cli_status("codex", "Codex CLI", ["login", "status"])
    antigravity_ok = cli_is_available("gemini")
    if antigravity_ok:
        print_result(True, "Antigravity CLI", "INSTALLED")
    else:
        print_result(False, "Antigravity CLI", "MISSING", "Antigravity CLI")
        
    ollama_ok = shutil.which("ollama") is not None
    print_result(ollama_ok, "Ollama (Local AI)", "INSTALLED" if ollama_ok else "MISSING", "Ollama")

    if not (has_env_key or has_settings_key or claude_ok or codex_ok or antigravity_ok or ollama_ok):
        print("\n\033[1;91m❌ CRITICAL ERROR: No AI providers found. The Orchestrator will fail.\033[0m")
    else:
        print("\n\033[1;92m✅ READY: At least one AI provider is configured.\033[0m")

    # 5. Check SSH Config
    print_header("6. SSH & Network")
    ssh_config_path = Path.home() / ".ssh" / "config"
    print_result(ssh_config_path.exists(), "SSH Config File", "OK" if ssh_config_path.exists() else "MISSING", f"Required for remote SSH workers; create config file at '{ssh_config_path}'")
    
    # 6. Optional Integrations & Networks
    print_header("7. Optional Integrations & Networks")
    optional_integrations = [
        ("GitHub MCP/plugin", ["github"], "Inspect issues, PRs, review comments, and check runs with rich context."),
        ("XcodeBuildMCP / iOS plugin", ["xcodebuildmcp", "build-ios-apps"], "Build, run, inspect, and screenshot iOS Simulator apps directly."),
        ("Sentry MCP/plugin", ["sentry"], "Inspect production issues, events, stack traces, and release health."),
        ("Playwright MCP", ["playwright"], "Inspect and automate web UIs, dashboards, and browser-based tools."),
    ]
    for label, names, value_desc in optional_integrations:
        detected = detect_codex_integration(names)
        print_optional_result(detected, label, "DETECTED" if detected else "recommended, not required")
        print(f"        \033[90m└─ Value: {value_desc}\033[0m")
        
    # Tailscale Check (Optional but recommended for secure fleet networking)
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

    ts_ok = False
    ts_info = "recommended, not required"
    if ts_bin:
        try:
            res = subprocess.run([ts_bin, "status", "--json"], capture_output=True, text=True, timeout=1.5)
            if res.returncode == 0:
                data = json.loads(res.stdout)
                if data.get("BackendState") == "Running":
                    ts_ok = True
                    ips = data.get("TailscaleIPs")
                    ts_ip = ips[0] if (ips and isinstance(ips, list)) else ""
                    ts_info = f"RUNNING ({ts_ip})" if ts_ip else "RUNNING"
                else:
                    ts_info = f"INSTALLED (State: {data.get('BackendState', 'Not Running')})"
            else:
                ts_info = "INSTALLED (Stopped)"
        except Exception:
            ts_info = "INSTALLED (Check Error)"
            
    print_optional_result(ts_ok, "Tailscale VPN", ts_info)
    print("        \033[90m└─ Value: Secure, zero-config network tunnels between local and fleet machines.\033[0m")
    
    print("     \033[90mSee docs/recommended-mcp-plugins.md for setup guidance.\033[0m")
    
    if m_path.exists():
        print_header("Verification Complete")
        print()
    else:
        print(f"\n\033[93m💡 NEXT STEP: Create '{m_path}' to use remote workers.\033[0m")

if __name__ == "__main__":
    check()
