from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestrator.project_config import DEFAULT_RUNTIME_DIRNAME, find_project_root, load_project_config
from orchestrator.project_config import load_recent_projects, project_display_name
from orchestrator.project_config import remember_project, resolve_project_reference
from orchestrator.config_validation import validate_machine_config, validate_project_config


RUNTIME_GITIGNORE = """# Generated Swift Orchestrator runtime output
jobs/
logs/
output/
state/
"""


def write_text_file(path: Path, content: str, force: bool) -> None:
    if path.exists() and not force:
        print(f"Already exists: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    
    # Try to make the path relative to ROOT if possible, otherwise use name
    try:
        from orchestrator.scripts.common import ROOT
        display_path = path.relative_to(ROOT)
    except Exception:
        display_path = path.name
    print(f"  ✅ Created {display_path}")


def run_json_command(args: list[str], root: Path) -> dict:
    try:
        result = subprocess.run(
            args,
            cwd=str(root),
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def infer_xcode(root: Path) -> tuple[str | None, str | None, str, list[str], list[str]]:
    xcode_project = next(iter(sorted(root.glob("*.xcodeproj"))), None)
    xcode_workspace = next(iter(sorted(root.glob("*.xcworkspace"))), None)
    args = ["xcodebuild", "-list", "-json"]
    if xcode_workspace:
        args.extend(["-workspace", xcode_workspace.name])
    elif xcode_project:
        args.extend(["-project", xcode_project.name])
    data = run_json_command(args, root) if (xcode_project or xcode_workspace) else {}
    container = data.get("workspace") or data.get("project") or {}
    schemes = sorted(container.get("schemes") or [])
    targets = sorted(container.get("targets") or [])
    fallback_scheme = (xcode_project or xcode_workspace).stem if (xcode_project or xcode_workspace) else root.name
    scheme = schemes[0] if schemes else fallback_scheme
    return (
        xcode_project.name if xcode_project else None,
        xcode_workspace.name if xcode_workspace else None,
        scheme,
        schemes,
        targets,
    )


def git_remote(root: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=str(root),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def build_test_docs(config: dict) -> str:
    if config["build_command"]:
        build_command = config["build_command"]
    elif config["xcode_workspace"]:
        build_command = f"xcodebuild build -workspace {config['xcode_workspace']} -scheme {config['scheme']}"
    elif config["xcode_project"]:
        build_command = f"xcodebuild build -project {config['xcode_project']} -scheme {config['scheme']}"
    else:
        build_command = "swift build"

    if config["test_command"]:
        test_command = config["test_command"]
    elif config["xcode_workspace"]:
        test_command = f"xcodebuild test -workspace {config['xcode_workspace']} -scheme {config['scheme']}"
    elif config["xcode_project"]:
        test_command = f"xcodebuild test -project {config['xcode_project']} -scheme {config['scheme']}"
    else:
        test_command = "swift test"

    return f"""# Build And Test Commands

Canonical validation commands for this project.

## iOS app build

```bash
{build_command}
```

## iOS app tests

```bash
{test_command}
```

## Orchestrator config check

```bash
orchestrator check-config
```
"""


def ai_workflow_docs(project_name: str) -> str:
    return f"""# AI Workflow

Use `orchestrator console` from the repository root to create and manage jobs for {project_name}.

Recommended flow:

1. Run `orchestrator check` after initial setup or toolchain changes.
2. Run `orchestrator check-config` after editing `.orchestrator/project.json` or machine config.
3. Create jobs from the console.
4. Review generated branches and pull requests before merging.
5. Keep generated `.orchestrator/jobs/`, `logs/`, `output/`, and `state/` files out of Git.
"""


def agents_docs(project_name: str) -> str:
    return f"""# AGENTS.md

Repository guidance for coding agents working on {project_name}.

- Use `docs/build-test-commands.md` for canonical validation commands.
- Prefer minimal, reviewable diffs.
- Do not modify unrelated files.
- Do not commit secrets, generated runtime logs, or unrelated environment changes.
- Run `orchestrator check-config` after changing `.orchestrator/project.json`.
"""


def helper_script() -> str:
    return """#!/usr/bin/env sh
# Robust Orchestrator wrapper.
# Detects local source in swift-orchestrator repo or falls back to global command.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$REPO_ROOT/orchestrator/cli.py" ] && [ -f "$REPO_ROOT/pyproject.toml" ]; then
    # Running inside the development repository
    export PYTHONPATH="$REPO_ROOT:${PYTHONPATH:-}"
    exec python3 -m orchestrator "$@"
elif command -v orchestrator >/dev/null 2>&1; then
    # Running in a project that has orchestrator installed globally/pipx
    exec orchestrator "$@"
else
    echo "Error: 'orchestrator' command not found."
    echo "Install via pipx: pipx install 'git+https://github.com/leemosupreemo/orchestrator.git'"
    echo "Or run from source: PYTHONPATH=. python3 -m orchestrator"
    exit 1
fi
"""


def write_starter_docs(root: Path, config: dict, force: bool) -> list[Path]:
    paths = [
        root / "AGENTS.md",
        root / "docs" / "build-test-commands.md",
        root / "docs" / "ai-workflow.md",
    ]
    write_text_file(paths[1], build_test_docs(config), force)
    write_text_file(paths[2], ai_workflow_docs(config["project_name"]), force)
    write_text_file(paths[0], agents_docs(config["project_name"]), force)
    return paths


def write_helper_script(root: Path, force: bool) -> Path:
    script_path = root / "scripts" / "orchestrator"
    write_text_file(script_path, helper_script(), force)
    if script_path.exists():
        script_path.chmod(script_path.stat().st_mode | 0o111)
    return script_path


def read_json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_file(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {path}")


def resolve_cli_project(project: str | None) -> Path | None:
    if project:
        p = resolve_project_reference(project)
        if p:
            return p
        print(f"Error: Project '{project}' not found in recent projects.")
        return None
    
    root = find_project_root()
    if not root:
        print("Error: Not in a Swift Orchestrator project and no project specified.")
        return None
    return root


def print_project_context(root: Path) -> None:
    from orchestrator.project_config import project_display_name
    name = project_display_name(root)
    print(f"\033[1;92mProject: {name}\033[0m")
    print(f"\033[90mContext: {root}\033[0m")


def apply_project_env(project: str | None) -> int:
    root = resolve_cli_project(project)
    if not root:
        return 1
    os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(root)
    print_project_context(root)
    return 0


def print_wizard_bar(skip_available: bool = True, status_bar: Any | None = None):
    """Refreshes the sticky footer for the wizard."""
    if not status_bar:
        return
        
    # Standardized color scheme: Green for active/skip, Red for exit/quit
    skip_msg = " [\033[1;92mS\033[0m] Skip Section |" if skip_available else ""
    bar_label = f"{skip_msg} [\033[1;91mQ\033[0m] Quit Wizard"
    
    # Visible plain text for metadata bar
    plain_skip = " [S] Skip Section |" if skip_available else ""
    q_msg = f"{plain_skip} [Q] Quit Wizard"
    
    # We use the status bar to draw at the bottom
    status_bar.render(at_bottom=True, force=True, q_msg=q_msg)


def prompt_text(label: str, default: str | None = None, skip_available: bool = True, status_bar: Any | None = None) -> str:
    from orchestrator.scripts.common import prompt_input, flush_stdin, BackException
    
    if status_bar:
        print_wizard_bar(skip_available, status_bar)
    
    flush_stdin()
    try:
        # prompt_input handles backspace and escape (BackException)
        res = prompt_input(label, default=default or "")
        if not res and default:
            return default
        return res
    except BackException:
        if skip_available:
            raise SkipSectionException()
        return default or ""


def prompt_yes_no(label: str, default: bool = False, skip_available: bool = True, status_bar: Any | None = None) -> bool:
    from orchestrator.scripts.common import flush_stdin, get_key
    
    if status_bar:
        print_wizard_bar(skip_available, status_bar)
        
    flush_stdin()
    
    suffix = " [Y/n]" if default else " [y/N]"
    sys.stdout.write("\r")
    print(f"{label}{suffix}: ", end="", flush=True)
    
    while True:
        key = get_key().strip().lower()
        if key == "q":
            print("\033[1;91mquit\033[0m")
            if status_bar: status_bar.reset_scroll_region()
            sys.exit(0)
        if key == "s" and skip_available:
            print("\033[1;92mskip\033[0m")
            raise SkipSectionException()
        if key in {"enter", ""}:
            res = default
            print("\033[90m(yes)\033[0m" if res else "\033[90m(no)\033[0m")
            return res
        if key in {"y", "n"}:
            res = (key == "y")
            print("\033[1;92myes\033[0m" if res else "\033[1;91mno\033[0m")
            return res


def prompt_password(label: str, placeholder: str = "(enter to skip)") -> str:
    from orchestrator.scripts.common import prompt_password as _prompt_password
    return _prompt_password(label, placeholder)


def prompt_radio(label: str, options: list[str], default: str | None = None, clear_screen: bool = True, status_bar: Any | None = None) -> str:
    from orchestrator.scripts.common import prompt_radio as _prompt_radio
    return _prompt_radio(label, options, default, clear_screen, status_bar)


class SkipSectionException(Exception): pass


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def check_cli_auth(cli_name: str) -> tuple[bool, str]:
    if cli_name == "gemini":
        if shutil.which("agy") is not None:
            cli_name = "agy"
        elif shutil.which("antigravity") is not None:
            cli_name = "antigravity"
        else:
            cli_name = "gemini"

    if shutil.which(cli_name) is None:
        return False, "\033[1;91mNOT INSTALLED\033[0m"
    
    try:
        if cli_name == "claude":
            res = subprocess.run(["claude", "auth", "status"], capture_output=True, text=True, timeout=5)
            ready = res.returncode == 0
        elif cli_name == "codex":
            res = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=5)
            ready = res.returncode == 0
        elif cli_name in {"gemini", "antigravity", "agy"}:
            ready = True
        elif cli_name == "opencode":
            res = subprocess.run(["opencode", "auth", "status"], capture_output=True, text=True, timeout=5)
            ready = res.returncode == 0
        elif cli_name == "ollama":
            ready = True
        elif cli_name == "gh":
            res = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=5)
            ready = res.returncode == 0
        else:
            ready = True
            
        return ready, "\033[92mREADY\033[0m" if ready else "\033[1;91mNOT LOGGED IN\033[0m"
    except Exception as e:
        return True, f"INSTALLED (Error checking status: {e})"


def get_auth_command(cli_name: str) -> str | None:
    mapping = {
        "gh": "gh auth login",
        "gemini": "agy",
        "antigravity": "antigravity",
        "agy": "agy",
        "claude": "claude auth login",
        "codex": "codex login",
        "opencode": "opencode auth login"
    }
    return mapping.get(cli_name)


def test_ssh_connection(target: str) -> bool:
    try:
        subprocess.run(["ssh", "-o", "ConnectTimeout=5", target, "true"], check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError:
        return False


def create_ssh_machine(name: str, ssh_target: str, repo_path: str) -> dict[str, Any]:
    if not repo_path:
        raise ValueError("Repo path is required for SSH machines.")
    return {
        "name": name,
        "enabled": True,
        "execution_mode": "ssh",
        "ssh_target": ssh_target,
        "repo_path": repo_path,
        "roles": ["worker", "build", "test"],
        "models": ["gemini", "codex", "claude"],
        "priority": 50,
        "max_concurrent_jobs": 1,
        "max_heavy_jobs": 1,
        "supports_xcode": True,
        "supports_simulator": True,
        "supports_backend_tests": False,
        "tags": ["remote"],
    }


def parse_ssh_machine(value: str) -> dict[str, Any]:
    try:
        name, rest = value.split("=", 1)
        ssh_target, repo_path = rest.split(":", 1)
        return create_ssh_machine(name, ssh_target, repo_path)
    except ValueError as exc:
        raise ValueError("--ssh-machine must use NAME=SSH_TARGET:/absolute/repo/path") from exc


def copy_prompt_overrides(root: Path, force: bool) -> list[Path]:
    prompts_source = Path(__file__).resolve().parent / "prompts"
    prompts_dest = root / DEFAULT_RUNTIME_DIRNAME / "prompts"
    prompts_dest.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for prompt_file in sorted(prompts_source.glob("*.md")):
        dest = prompts_dest / prompt_file.name
        if dest.exists() and not force:
            print(f"Already exists: {dest}")
            copied.append(dest)
            continue
        shutil.copyfile(prompt_file, dest)
        print(f"Created {dest}")
        copied.append(dest)
    return copied


def update_machine_models(config_dir: Path, models: list[str], ssh_machines: list[dict]) -> None:
    machines_file = config_dir / "machines.json"
    machines_config = read_json_file(machines_file)
    existing = {machine["name"]: machine for machine in machines_config.get("machines", [])}
    for machine in existing.values():
        machine["models"] = models
    for machine in ssh_machines:
        machine["models"] = models
        existing[machine["name"]] = machine
    machines_config["machines"] = list(existing.values())
    write_json_file(machines_file, machines_config)


def verify_wizard_setup(install_workers: list[str]) -> int:
    check_result = run_script("check_setup.py", [])
    config_result = validate_config_command()
    worker_failures = 0
    for machine_name in install_workers:
        install_result = run_script("worker_tools.py", ["install", "--machine", machine_name])
        check_worker_result = run_script("worker_tools.py", ["check", "--machine", machine_name])
        if install_result != 0 or check_worker_result != 0:
            worker_failures += 1
    return 1 if check_result != 0 or config_result != 0 or worker_failures else 0


def run_wizard(args: argparse.Namespace) -> int:
    from orchestrator.scripts.common import StatusBar, flush_stdin
    from orchestrator.scripts.model_registry import get_all_models
    from orchestrator.project_config import load_project_config
    
    root_arg = args.project or args.root
    root = Path(root_arg).expanduser().resolve() if root_arg else find_project_root()
    models = parse_csv(args.models)
    
    if args.non_interactive and not models:
        print("Wizard requires at least one model. Pass --models codex or run interactively.")
        return 1

    # Load current config to use as defaults and check for issues
    cfg = load_project_config()
    
    # If distribution is enabled but has errors, we want to force the section to help the user fix it
    dist_errors = cfg.validate_distribution_config()
    force_delivery = bool(cfg.firebase_distribution and dist_errors)

    # Define variables early for validation and use
    firebase_enabled = args.firebase or cfg.firebase_distribution
    team_id = args.team_id
    method = args.method
    firebase_plist_path = args.firebase_plist_path
    provisioning_profile = args.provisioning_profile
    asc_key_id = args.asc_key_id
    asc_issuer_id = args.asc_issuer_id
    asc_key_path = args.asc_key_path

    from orchestrator import __version__
    print(f"\n\033[1;96m{'='*20} Orchestrator Wizard v{__version__} {'='*20}\033[0m")

    # Start Status Bar context for the entire wizard
    with StatusBar(sub_menu=True) as status_bar:
        status_bar.set_scroll_region()
        flush_stdin()
        
        try:
            ssh_machines = [parse_ssh_machine(value) for value in args.ssh_machine]
        except ValueError as exc:
            status_bar.reset_scroll_region()
            print(str(exc))
            return 1

        if args.non_interactive and firebase_enabled:
            # Validate required args for non-interactive firebase setup
            if not firebase_plist_path or not team_id or not method:
                print("❌ Error: --firebase in non-interactive mode requires --firebase-plist-path, --team-id, and --method.")
                status_bar.reset_scroll_region()
                return 1

        runtime_dir = root / DEFAULT_RUNTIME_DIRNAME
        project_file = runtime_dir / "project.json"
        needs_init = args.force or not project_file.exists()
        review_paths: list[Path] = []
        
        if needs_init:
            init_args = argparse.Namespace(
                root=str(root),
                project=None,
                project_name=args.project_name,
                scheme=args.scheme,
                test_target=args.test_target,
                base_branch=args.base_branch,
                force=args.force,
                with_starter_docs=True,
                with_helper_script=True
            )
            init_project(init_args)
            review_paths.append(project_file)

        if args.delivery_only:
            print("\033[1;96mJumping to iOS Distribution & Signing setup...\033[0m")
        else:
            all_models = get_all_models()
            if not models and not args.non_interactive:
                try:
                    from orchestrator.scripts.common import prompt_checkbox, flush_stdin
                    print(f"\n\033[1;96m{'='*20} LLM Setup {'='*20}\033[0m")
                    print("Checking available providers and models...")

                    # Get unique CLIs required by models
                    required_clis = set()
                    for m in all_models:
                        required_clis.update(m.required_clis)

                    cli_status = {}
                    for cli in sorted(required_clis):
                        is_ready, msg = check_cli_auth(cli)
                        cli_status[cli] = is_ready
                        print(f"  {cli.ljust(10)} : {msg}")

                    # 1. Model Selection
                    model_options = []
                    defaults = []
                    for m in all_models:
                        is_ready = all(cli_status.get(c, False) for c in m.required_clis)
                        status_label = "\033[92mREADY\033[0m" if is_ready else "\033[91mNOT LOGGED IN\033[0m"
                        label = f"{m.id} ({status_label})"
                        model_options.append(label)
                        if is_ready:
                            defaults.append(label)

                    print_wizard_bar(skip_available=False, status_bar=status_bar)
                    selected_labels = prompt_checkbox(
                        "Select AI Models to enable:",
                        model_options,
                        defaults=defaults,
                        status_bar=status_bar,
                        clear_screen=False
                    )

                    selected_ids = [label.split(" (")[0] for label in selected_labels]

                    # 2. Provider Login (if needed)
                    while True:
                        needed_clis = set()
                        for m_id in selected_ids:
                            m = next(mod for mod in all_models if mod.id == m_id)
                            for cli in m.required_clis:
                                if not cli_status.get(cli, False) and shutil.which(cli):
                                    needed_clis.add(cli)

                        if not needed_clis:
                            break

                        print(f"\n\033[1;93mSome selected models require login.\033[0m")
                        login_options = sorted(list(needed_clis))
                        to_login = prompt_checkbox(
                            "Select providers to log in now:",
                            login_options,
                            status_bar=status_bar,
                            clear_screen=False,
                            footer="\033[90m(Unselected providers will result in those models being disabled)\033[0m"
                        )

                        if not to_login:
                            break

                        for cli in to_login:
                            auth_cmd = get_auth_command(cli)
                            if auth_cmd:
                                print(f"\nRunning: \033[97m{auth_cmd}\033[0m")
                                subprocess.run(auth_cmd.split(), check=False)
                                is_ready, msg = check_cli_auth(cli)
                                cli_status[cli] = is_ready
                                print(f"  {cli.ljust(10)} : {msg}")
                            else:
                                print(f"No auth command known for {cli}.")

                    # Final assignment
                    for m_id in selected_ids:
                        m = next(mod for mod in all_models if mod.id == m_id)
                        if all(cli_status.get(c, False) for c in m.required_clis):
                            models.append(m_id)

                    print("\n\033[90m(You can change active models later in the Dev Console)\033[0m")

                except SkipSectionException:
                    pass

            if not models:
                status_bar.reset_scroll_region()
                print("Wizard requires at least one model. Pass --models codex or run interactively.")
                return 1

            prompts_dir = runtime_dir / "prompts"
            has_custom_prompts = prompts_dir.exists() and any(prompts_dir.iterdir())

            copy_prompts = args.copy_prompt_overrides
            if not copy_prompts and not args.non_interactive:
                try:
                    print(f"\n\033[1;96m{'='*20} Role Prompts {'='*20}\033[0m")
                    if has_custom_prompts:
                        print(f"✅ Custom prompts already exist in \033[97m{prompts_dir.relative_to(root)}\033[0m.")
                    else:
                        print("You can customize the AI's coding style by editing local copies of its instruction prompts.")
                        copy_prompts = prompt_yes_no("Copy default role prompt templates into your project now?", True, status_bar=status_bar)
                except SkipSectionException:
                    copy_prompts = False

            if copy_prompts:
                review_paths.extend(copy_prompt_overrides(root, args.force))

            if not args.non_interactive:
                try:
                    print(f"\n\033[1;96m{'='*20} Workers {'='*20}\033[0m")

                    import socket
                    from orchestrator.scripts.discover_machines import get_local_ssh_hosts, check_machine_suitability

                    print_wizard_bar(skip_available=True, status_bar=status_bar)
                    hosts = get_local_ssh_hosts()
                    candidates = []
                    if hosts:
                        local_hostname = socket.gethostname().lower()
                        for host in hosts:
                            if host.lower() == local_hostname or host.lower() == local_hostname + ".local":
                                continue
                            info = check_machine_suitability(host)
                            if info:
                                candidates.append(info)

                    if candidates:
                        print(f"\n✨ Discovered {len(candidates)} potential machine(s):")
                        for i, c in enumerate(candidates, 1):
                            print(f"  {i}. \033[97m{c['hostname']}\033[0m ({c['host']})")

                        print(f"\n\033[90m(Enter numbers to add, e.g. '1,2'. Leave empty to skip.)\033[0m")
                        print_wizard_bar(skip_available=True, status_bar=status_bar)
                        sys.stdout.write("Select machines to add: ")
                        sys.stdout.flush()

                        from orchestrator.scripts.common import get_key
                        choices = ""
                        while True:
                            key = get_key()
                            if key == "enter":
                                print()
                                break
                            if key == "q":
                                print("\033[1;91mquit\033[0m")
                                status_bar.reset_scroll_region()
                                sys.exit(0)
                            if key == "s":
                                print("\033[1;92mskip\033[0m")
                                raise SkipSectionException()
                            if key == "backspace":
                                if choices:
                                    choices = choices[:-1]
                                    sys.stdout.write("\b \b")
                                    sys.stdout.flush()
                            elif len(key) == 1:
                                choices += key
                                sys.stdout.write(key)
                                sys.stdout.flush()

                        if choices:
                            for choice in choices.split(','):
                                try:
                                    idx = int(choice.strip()) - 1
                                    if 0 <= idx < len(candidates):
                                        c = candidates[idx]
                                        status = c.get("status")
                                        host = c['host']

                                        remote_user = None
                                        if status != "suitable":
                                            import getpass
                                            default_user = getpass.getuser()
                                            print(f"\n🔑 Machine '{host}' requires SSH configuration.")
                                            remote_user = prompt_text(f"Remote SSH username for {host} (default: {default_user})", default_user, status_bar=status_bar)

                                        ssh_target = f"{remote_user}@{host}" if remote_user else host
                                        hostname = c.get("hostname") or host

                                        repo_path = prompt_text(f"Remote repo path for {hostname}", status_bar=status_bar)
                                        try:
                                            machine = create_ssh_machine(
                                                name=hostname.split('.')[0],
                                                ssh_target=ssh_target,
                                                repo_path=repo_path
                                            )
                                            if test_ssh_connection(machine["ssh_target"]):
                                                ssh_machines.append(machine)
                                            else:
                                                print(f"\n❌ Connection test to {machine['ssh_target']} failed.")
                                                if status == "needs_ssh_keys":
                                                    print("  How to fix: Run the following command in a new terminal to copy your SSH key:")
                                                    print(f"\033[1;96m    ssh-copy-id {machine['ssh_target']}\033[0m")
                                                if prompt_yes_no("Add this machine anyway?", False, status_bar=status_bar):
                                                    ssh_machines.append(machine)
                                        except ValueError as exc:
                                            print(f"  ❌ {exc}")
                                except ValueError:
                                    pass

                    if prompt_yes_no("Add another SSH worker machine manually?", False, status_bar=status_bar):
                        name = prompt_text("Machine name", "mac2", status_bar=status_bar)
                        target = prompt_text("SSH target", name, status_bar=status_bar)
                        repo_path = prompt_text("Remote repo path", status_bar=status_bar)
                        try:
                            machine = create_ssh_machine(name, target, repo_path)
                            if test_ssh_connection(machine["ssh_target"]) or prompt_yes_no("Connection failed. Add this machine anyway?", False, status_bar=status_bar):
                                ssh_machines.append(machine)
                        except ValueError as exc:
                            status_bar.reset_scroll_region()
                            print(str(exc))
                            return 1

                except SkipSectionException:
                    pass

            update_machine_models(runtime_dir / "config", models, ssh_machines)

        if force_delivery or args.delivery_only:
            firebase_enabled = True
        elif not firebase_enabled and not args.non_interactive:
            try:
                print(f"\n\033[1;96m{'='*20} Delivery {'='*20}\033[0m")
                if dist_errors:
                    print("\033[1;93m⚠️  iOS Distribution configuration is currently incomplete.\033[0m")
                firebase_enabled = prompt_yes_no("Configure Firebase distribution now?", False, status_bar=status_bar)
            except SkipSectionException:
                firebase_enabled = False

        if firebase_enabled:
            try:
                if not args.non_interactive:
                    import orchestrator.scripts.setup_distribution as sd
                    detected_team, detected_method = sd.detect_identity_info()

                    print("\n\033[1;96m--- iOS Signing Configuration ---\033[0m")
                    firebase_plist_path = firebase_plist_path or prompt_text("Firebase plist path", f"{root.name}/GoogleService-Info.plist", status_bar=status_bar)
                    team_id = team_id or prompt_text("Apple Development Team ID", getattr(load_project_config(), "development_team", detected_team), status_bar=status_bar)
                    method = method or prompt_text("Distribution method (ad-hoc, debugging)", detected_method or "debugging", status_bar=status_bar)

                    from orchestrator.scripts.common import prompt_password
                    if prompt_yes_no("Configure App Store Connect API keys for automated signing? (Recommended)", False, status_bar=status_bar):
                        asc_key_id = asc_key_id or prompt_password("ASC Key ID", placeholder="(invisible)")
                        asc_issuer_id = asc_issuer_id or prompt_password("ASC Issuer ID", placeholder="(invisible)")
                        asc_key_path = asc_key_path or prompt_text("ASC Key Path (.p8)", status_bar=status_bar)
                    else:
                        print("\n\033[1;93mManual Signing:\033[0m You must provide a Provisioning Profile name (specifier).")
                        print("\033[90mThis is required for 'xcodebuild' to find your signing certificates.\033[0m")
                        print("\n\033[1;97mHow to find this in Xcode:\033[0m")
                        print("  1. Select your Project in the Navigator.")
                        print("  2. Select your App Target.")
                        print("  3. Go to \033[1;96mBuild Settings\033[0m.")
                        print("  4. Search for \033[1;96m'Provisioning Profile'\033[0m.")
                        print(f"  5. Use the exact name shown there (e.g. '{root.name} Development').")
                        
                        detected_profile = sd.detect_provisioning_profile_specifier(root)
                        installed_profiles = sorted(list(sd.installed_provisioning_profile_names()))
                        if installed_profiles:
                            print("\n\033[1;97mInstalled profiles on this machine:\033[0m")
                            for p in installed_profiles[:5]:
                                print(f"  - {p}")
                            if len(installed_profiles) > 5:
                                print(f"  - ...and {len(installed_profiles) - 5} more")

                        # We force the prompt if it's missing, but with a safety limit to avoid infinite loops
                        attempts = 0
                        while not provisioning_profile and attempts < 10:
                             attempts += 1
                             provisioning_profile = prompt_text("Provisioning Profile Specifier", detected_profile, status_bar=status_bar)
                             if not provisioning_profile:
                                 print(f"\n\033[1;91mError: Provisioning Profile is required for manual signing (Attempt {attempts}/10).\033[0m")
                                 print("\033[90mTip: If you don't have a profile yet, create one in Xcode (Signing & Capabilities).\033[0m")
                        
                        if not provisioning_profile:
                            print("\033[1;91mToo many failed attempts or input unavailable. Skipping signing setup.\033[0m")
                            raise SkipSectionException()

                    print("\n\033[1;96m--- Headless Signing (Keychain) ---\033[0m")
                    print("\033[90mTo build without UI popups, the CLI can unlock your login keychain automatically.")
                    print("This requires storing your macOS login password securely in .secrets/project-secrets.zsh.\033[0m")
                    if prompt_yes_no("Configure automated keychain unlocking?", True, status_bar=status_bar):
                        run_script("dev_console.py", ["keychain-setup"])

                script_args = ["--force"]
                if firebase_plist_path: script_args.extend(["--firebase-plist", firebase_plist_path])
                if team_id: script_args.extend(["--team-id", team_id])
                if method: script_args.extend(["--method", method])
                if provisioning_profile: script_args.extend(["--provisioning-profile", provisioning_profile])
                if asc_key_id: script_args.extend(["--asc-key-id", asc_key_id])
                if asc_issuer_id: script_args.extend(["--asc-issuer-id", asc_issuer_id])
                if asc_key_path: script_args.extend(["--asc-key-path", asc_key_path])
                if root: script_args.extend(["--root", str(root)])

                res = run_script("setup_distribution.py", script_args)
                if res != 0:
                    print("\n❌ iOS signing configuration failed. Please check the errors above.")
                    return 1
            except SkipSectionException:
                pass

        # Final step: Project Grounding / Indexing
        if not args.non_interactive:
            print(f"\n\033[1;96m{'='*20} Grounding {'='*20}\033[0m")
            res = run_script("index_project.py", [])
            if res != 0:
                print("\n❌ Project grounding/indexing failed. Please check the errors above.")
                return 1
            
            # Xcode Smoke Test
            cfg = load_project_config()
            if (cfg.xcode_project or cfg.xcode_workspace) and prompt_yes_no("Run a quick Xcode build validation (Smoke Test)?", True, status_bar=status_bar):
                print("\n🔍 Verifying Xcode build settings...")
                cmd_args = ["xcodebuild", "-scheme", cfg.scheme, "-showBuildSettings"]
                if cfg.xcode_workspace: cmd_args.extend(["-workspace", cfg.xcode_workspace])
                elif cfg.xcode_project: cmd_args.extend(["-project", cfg.xcode_project])
                
                try:
                    subprocess.run(cmd_args, capture_output=True, text=True, check=True, timeout=30, cwd=str(root))
                    print("✅ Xcode configuration verified.")
                except Exception as e:
                    print(f"❌ Xcode validation failed: {e}")

        status_bar.reset_scroll_region()
        print(f"\n\033[1;92m{'='*20} Wizard Complete {'='*20}\033[0m")
        print("\033[90mReview these Markdown/config files before creating jobs:\033[0m")
        for path in [*review_paths, runtime_dir / "project.json", runtime_dir / "config" / "machines.json", runtime_dir / "config" / "settings.json"]:
            print(f"  - \033[97m{path.relative_to(root)}\033[0m")
            
        install_workers = [machine["name"] for machine in ssh_machines] if args.install_workers else []
        if not args.non_interactive and ssh_machines and not install_workers:
            print(f"\033[1;96m{'='*20} Final Setup {'='*20}\033[0m")
            if prompt_yes_no("Install/check SSH worker packages now?", False, status_bar=status_bar):
                install_workers = [machine["name"] for machine in ssh_machines]
        
        remember_project(root, project_display_name(root), active=True)
        
        status_bar.reset_scroll_region()
        from orchestrator.scripts.common import print_header
        print_header("WIZARD FINISHED")
        print("\033[1;92m✅ Project is now ready for autonomous builds.\033[0m")
        print("\nNext steps:")
        print("  1. Review \033[97m.orchestrator/project.json\033[0m to verify signing settings.")
        print("  2. Re-run your smoke test to confirm distribution works.")
        print("  3. Create your first job via the Dev Console.")
        print("-" * 60)
        
        if args.verify or install_workers:
            return verify_wizard_setup(install_workers)
        return 0


def init_project(args: argparse.Namespace) -> int:
    root_arg = getattr(args, "project", None) or args.root
    root = Path(root_arg).expanduser().resolve() if root_arg else find_project_root()
    runtime_dir = root / DEFAULT_RUNTIME_DIRNAME
    config_dir = runtime_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for subdir in ["jobs/inbox", "jobs/archive", "logs", "output", "state/machines"]:
        (runtime_dir / subdir).mkdir(parents=True, exist_ok=True)

    xcode_project, xcode_workspace, detected_scheme, detected_schemes, detected_targets = infer_xcode(root)
    project_name = args.project_name or root.name
    scheme = args.scheme or detected_scheme
    test_target = args.test_target or next((target for target in detected_targets if target.endswith("Tests")), f"{scheme}Tests")
    config = {
        "project_name": project_name,
        "base_branch": args.base_branch,
        "pr_base_branch": args.base_branch,
        "branch_prefix": "ai/issue",
        "git_remote": git_remote(root),
        "xcode_project": xcode_project,
        "xcode_workspace": xcode_workspace,
        "scheme": scheme,
        "detected_schemes": detected_schemes,
        "test_target": test_target,
        "detected_targets": detected_targets,
        "derived_data_path": f"/tmp/{project_name.lower()}_orchestrator_dd",
        "build_command": None,
        "test_command": None,
        "backend_test_command": None,
        "app_bundle_id": None,
        "visual_app_path": None,
        "delivery_provider": None,
        "distribution_script_path": None,
        "delivery_method": None,
        "firebase_plist_path": None,
        "provisioning_profile_specifier": None,
        "development_team": None,
        "asc_key_id": None,
        "asc_issuer_id": None,
        "asc_key_path": None,
        "remote_package_install_path": "~/.orchestrator/package",
        "firebase_distribution": False,
        "notification_display_name": f"{project_name} AI Orchestrator",
    }

    project_file = runtime_dir / "project.json"
    if project_file.exists() and not args.force:
        print(f"Project config already exists: {project_file}")
    else:
        project_file.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        print(f"  ✅ Created {project_file.relative_to(root)}")

    machines_file = config_dir / "machines.json"
    if not machines_file.exists() or args.force:
        machines_file.write_text(json.dumps({
            "version": 1,
            "machines": [
                {
                    "name": "local",
                    "enabled": True,
                    "execution_mode": "local",
                    "ssh_target": None,
                    "repo_path": str(root),
                    "roles": ["planner", "reviewer", "worker", "build", "test"],
                    "models": ["gemini", "codex", "claude"],
                    "priority": 100,
                    "max_concurrent_jobs": 1,
                    "max_heavy_jobs": 1,
                    "supports_xcode": True,
                    "supports_simulator": True,
                    "supports_backend_tests": False,
                    "interactive_reserved": True,
                    "tags": ["interactive", "primary"],
                }
            ],
        }, indent=2) + "\n", encoding="utf-8")
        print(f"  ✅ Created {machines_file.relative_to(root)}")

    settings_file = config_dir / "settings.json"
    if not settings_file.exists() or args.force:
        settings_file.write_text(json.dumps({
            "notification_emails": [],
            "notification_provider": "resend",
        }, indent=2) + "\n", encoding="utf-8")
        print(f"  ✅ Created {settings_file.relative_to(root)}")

    gitignore_file = runtime_dir / ".gitignore"
    if not gitignore_file.exists() or args.force:
        gitignore_file.write_text(RUNTIME_GITIGNORE, encoding="utf-8")
        print(f"  ✅ Created {gitignore_file.relative_to(root)}")

    if args.with_starter_docs:
        write_starter_docs(root, config, args.force)

    if args.with_helper_script:
        write_helper_script(root, args.force)

    print("Run: orchestrator check")
    remember_project(root, config["project_name"], active=True)
    return 0


def validate_config_command() -> int:
    config = load_project_config()
    errors = validate_project_config(config)
    errors.extend(validate_machine_config(config.config_dir))
    if errors:
        print("Configuration errors:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("Configuration OK")
    return 0


def run_script(script_name: str, script_args: list[str]) -> int:
    # Resolve scripts directory relative to this file
    current_dir = Path(__file__).resolve().parent
    scripts_dir = current_dir / "scripts"
    
    # Fallback: if not found (e.g. due to rename), try to find via project root
    if not (scripts_dir / script_name).exists():
        root = find_project_root()
        candidate = root / "orchestrator" / "scripts"
        if (candidate / script_name).exists():
            scripts_dir = candidate
            
    script_path = scripts_dir / script_name
    if not script_path.exists():
        print(f"Error: Script not found: {script_path}")
        print("This might happen if the project directory was renamed. Try: pip install -e .")
        return 1

    env = os.environ.copy()
    env.setdefault("ORCHESTRATOR_PROJECT_ROOT", str(find_project_root()))
    return subprocess.call([sys.executable, str(script_path), *script_args], env=env)


def list_projects_command() -> int:
    data = load_recent_projects()
    active = data.get("active")
    projects = data.get("projects", [])
    if not projects:
        print("No recent projects.")
        return 0
    for project in projects:
        marker = "*" if project.get("name") == active else " "
        print(f"{marker} Project: {project.get('name')}")
        print(f"  Root:    {project.get('root')}")
    return 0


def use_project_command(reference: str) -> int:
    root = resolve_cli_project(reference)
    if not root:
        return 1
    if not root.exists():
        print(f"Project path does not exist: {root}")
        return 1
    remember_project(root, project_display_name(root), active=True)
    print_project_context(root)
    return 0


def update_command(args: argparse.Namespace) -> int:
    from orchestrator.project_config import PACKAGE_ROOT
    from orchestrator import __version__
    
    pkg_dir = PACKAGE_ROOT.parent
    is_git = (pkg_dir / ".git").exists()
    
    print(f"\n\033[1;96m{'='*20} Orchestrator Update {'='*20}\033[0m")
    print(f"Current Version: \033[97mv{__version__}\033[0m")
    
    if is_git:
        print("\n\033[1;96m--- Local Update (Git Repository) ---\033[0m")
        print(f"Location: {pkg_dir}")
        try:
            subprocess.run(["git", "pull"], cwd=str(pkg_dir), check=False)
            subprocess.run([sys.executable, "-m", "pip", "install", "-e", "."], cwd=str(pkg_dir), check=False)
            print("\n✅ Local package updated successfully.")
        except Exception as e:
            print(f"\n❌ Error during local update: {e}")
            return 1
    else:
        print("\n\033[1;93mNote: Local source code not found in a Git repository.\033[0m")
        print("If you installed via pipx, run: \033[97mpipx upgrade orchestrator\033[0m")
        print("If you installed via pip, run:  \033[97mpip install --upgrade orchestrator\033[0m")

    if getattr(args, "fleet", False):
        print("\n\033[1;96m--- Fleet Update ---\033[0m")
        try:
            return run_script("worker_tools.py", ["install"])
        except Exception as e:
            print(f"❌ Fleet update failed: {e}")
            return 1
        
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 1


def _main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    
    if not argv:
        current = Path.cwd().resolve()
        local_root = None
        for candidate in [current, *current.parents]:
            if (candidate / DEFAULT_RUNTIME_DIRNAME / "project.json").exists():
                local_root = candidate
                break
        
        if local_root:
            os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(local_root)
            argv = ["console"]
        else:
            recent = load_recent_projects()
            projects = recent.get("projects", [])

            print(f"\n\033[1;96m{'='*20} Orchestrator {'='*20}\033[0m")
            print("No initialized project found in current directory.\n")
            print("    [\033[93m1\033[0m] Initialize new project (Wizard)")

            if projects:
                print("\n  \033[1;36m--- EXISTING PROJECTS ---\033[0m")
                for i, p in enumerate(projects, 2):
                    print(f"    [\033[93m{i}\033[0m] Open {p['name']}")
                    print(f"        \033[90m{p['root']}\033[0m")

            print("\n    [\033[1;91mQ\033[0m] Quit\n")
            print("-" * 37)

            from orchestrator.scripts.common import get_key, print_choice_prompt, clear_choice_placeholder
            while True:
                sys.stdout.write("\r")
                print_choice_prompt("Choice:", "(index or letter)")
                choice = get_key().strip().lower()
                clear_choice_placeholder()
                
                if choice == "q":
                    print()
                    return 0
                if choice == "1":
                    print()
                    argv = ["wizard"]
                    break
                if choice.isdigit():
                    idx = int(choice) - 2
                    if 0 <= idx < len(projects):
                        os.environ["ORCHESTRATOR_PROJECT_ROOT"] = projects[idx]["root"]
                        print()
                        argv = ["console"]
                        break

    parser = argparse.ArgumentParser(prog="orchestrator")
    from orchestrator import __version__
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--root")
    init_parser.add_argument("--project", help="Project root path. Alias for --root.")
    init_parser.add_argument("--project-name")
    init_parser.add_argument("--scheme")
    init_parser.add_argument("--test-target")
    init_parser.add_argument("--base-branch", default="main")
    init_parser.add_argument("--force", action="store_true")
    init_parser.add_argument("--with-starter-docs", action="store_true")
    init_parser.add_argument("--with-helper-script", action="store_true")

    wizard_parser = subparsers.add_parser("wizard")
    wizard_parser.add_argument("--root")
    wizard_parser.add_argument("--project", help="Project root path. Alias for --root.")
    wizard_parser.add_argument("--project-name")
    wizard_parser.add_argument("--scheme")
    wizard_parser.add_argument("--test-target")
    wizard_parser.add_argument("--base-branch", default="main")
    wizard_parser.add_argument("--force", action="store_true")
    wizard_parser.add_argument("--models", help="Comma-separated model aliases or model IDs, for example codex,antigravity")
    wizard_parser.add_argument("--copy-prompt-overrides", action="store_true")
    wizard_parser.add_argument("--ssh-machine", action="append", default=[], help="Add SSH worker as NAME=SSH_TARGET:/absolute/repo/path")
    wizard_parser.add_argument("--firebase", action="store_true")
    wizard_parser.add_argument("--distribution-script-path")
    wizard_parser.add_argument("--firebase-plist-path")
    wizard_parser.add_argument("--team-id")
    wizard_parser.add_argument("--method")
    wizard_parser.add_argument("--provisioning-profile")
    wizard_parser.add_argument("--asc-key-id")
    wizard_parser.add_argument("--asc-issuer-id")
    wizard_parser.add_argument("--asc-key-path")
    wizard_parser.add_argument("--verify", action="store_true", help="Run setup and config checks before exiting")
    wizard_parser.add_argument("--install-workers", action="store_true", help="Run worker install/check for SSH machines added by this wizard")
    wizard_parser.add_argument("--non-interactive", action="store_true")
    wizard_parser.add_argument("--delivery-only", action="store_true", help="Only run the iOS distribution/signing setup section")

    console_parser = subparsers.add_parser("console")
    console_parser.add_argument("--project", help="Recent project name or project root path")
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--project", help="Recent project name or project root path")
    check_config_parser = subparsers.add_parser("check-config")
    check_config_parser.add_argument("--project", help="Recent project name or project root path")
    worker_check = subparsers.add_parser("worker-check")
    worker_check.add_argument("--machine")
    worker_check.add_argument("--project", help="Recent project name or project root path")
    worker_install = subparsers.add_parser("worker-install")
    worker_install.add_argument("--machine")
    worker_install.add_argument("--project", help="Recent project name or project root path")
    subparsers.add_parser("projects")
    use_parser = subparsers.add_parser("use")
    use_parser.add_argument("project", help="Recent project name or project root path")

    update_parser = subparsers.add_parser("update")
    update_parser.add_argument("--fleet", action="store_true", help="Update all enabled remote workers in the fleet")
    update_parser.add_argument("--project", help="Recent project name or project root path (required for --fleet)")

    passthrough = subparsers.add_parser("script")
    passthrough.add_argument("--project", help="Recent project name or project root path")
    passthrough.add_argument("script_name")
    passthrough.add_argument("script_args", nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    if args.command == "init":
        return init_project(args)
    if args.command == "wizard":
        return run_wizard(args)
    if args.command == "console":
        if args.project and apply_project_env(args.project):
            return 1
        return run_script("dev_console.py", [])
    if args.command == "check":
        if args.project and apply_project_env(args.project):
            return 1
        return run_script("check_setup.py", [])
    if args.command == "check-config":
        if args.project and apply_project_env(args.project):
            return 1
        return validate_config_command()
    if args.command == "worker-check":
        if args.project and apply_project_env(args.project):
            return 1
        script_args = ["check"]
        if args.machine:
            script_args.extend(["--machine", args.machine])
        return run_script("worker_tools.py", script_args)
    if args.command == "worker-install":
        if args.project and apply_project_env(args.project):
            return 1
        script_args = ["install"]
        if args.machine:
            script_args.extend(["--machine", args.machine])
        return run_script("worker_tools.py", script_args)
    if args.command == "projects":
        return list_projects_command()
    if args.command == "use":
        return use_project_command(args.project)
    if args.command == "update":
        if args.fleet and args.project and apply_project_env(args.project):
            return 1
        return update_command(args)
    if args.command == "script":
        if args.project and apply_project_env(args.project):
            return 1
        return run_script(args.script_name, args.script_args)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(1)
