from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestrator.project_config import (
    DEFAULT_RUNTIME_DIRNAME,
    find_project_root,
    forget_project,
    load_project_config,
    load_recent_projects,
    project_display_name,
    remember_project,
    resolve_project_reference,
    safe_cwd,
    safe_resolve,
)
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
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=str(root),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if url:
            if "github_pat_" in url or (url.startswith("http") and "@" in url):
                print("\n\033[1;91m⚠️  WARNING: Your git remote URL contains a hardcoded Personal Access Token (PAT) or embedded credentials.\033[0m")
                print("   This is insecure and can cause push failures (e.g. 403 Forbidden) if the PAT lacks write scopes.")
                print("   We recommend changing your remote to use SSH before proceeding:")
                print("     git remote set-url origin git@github.com:username/repository.git\n")
        return url
    except Exception:
        return None


def build_test_docs(config: dict, stack: Any = None) -> str:
    if config.get("build_command"):
        build_command = config["build_command"]
    elif config.get("xcode_workspace") and config.get("scheme"):
        build_command = f"xcodebuild build -workspace {config['xcode_workspace']} -scheme {config['scheme']}"
    elif config.get("xcode_project") and config.get("scheme"):
        build_command = f"xcodebuild build -project {config['xcode_project']} -scheme {config['scheme']}"
    elif stack and stack.build_command:
        build_command = stack.build_command
    else:
        build_command = "swift build"

    if config.get("test_command"):
        test_command = config["test_command"]
    elif config.get("xcode_workspace") and config.get("scheme"):
        test_command = f"xcodebuild test -workspace {config['xcode_workspace']} -scheme {config['scheme']}"
    elif config.get("xcode_project") and config.get("scheme"):
        test_command = f"xcodebuild test -project {config['xcode_project']} -scheme {config['scheme']}"
    elif stack and stack.test_command:
        test_command = stack.test_command
    else:
        test_command = "swift test"

    return f"""# Build And Test Commands

Canonical validation commands for this project.

## Build

```bash
{build_command}
```

## Tests

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


def architecture_docs(project_name: str, scheme: str = "App", stack: Any = None) -> str:
    if stack and stack.language == "python":
        return f"""# Architecture Guide

Architectural overview and guidelines for {project_name}.

## Overview

- **Platform:** Python ({stack.framework or 'Modular Service'})
- **Design Pattern:** Modular Service Architecture / Clean Architecture

## Core Layers

1. **Domain / Models:** Data structures, schemas, and domain entities.
2. **Services / Workflows:** Core business logic and use-case handlers.
3. **Interface / Entrypoints:** CLI commands, API routers, or scripts.
4. **Clients / Adapters:** Database, external integrations, and filesystem access.

## Concurrency & Execution

- Use structured async/await (`asyncio`) or multi-threading where applicable.
- Keep business logic isolated and testable without side effects.
"""
    if stack and stack.language == "rust":
        return f"""# Architecture Guide

Architectural overview and guidelines for {project_name}.

## Overview

- **Platform:** Rust ({stack.framework or 'Cargo'})
- **Design Pattern:** Modular Crate Architecture / Clean Architecture

## Core Layers

1. **Domain Primitives:** Core structs, enums, and trait definitions.
2. **Modules / Logic:** Domain operations implementing business rules.
3. **I/O & Adapters:** Network, CLI, filesystem, and external adapters.

## Concurrency & Memory Safety

- Leverage Rust's ownership and borrow checker guarantees.
- Prefer explicit error handling via `Result` and `Option`.
"""
    if stack and stack.language in ("typescript", "javascript"):
        return f"""# Architecture Guide

Architectural overview and guidelines for {project_name}.

## Overview

- **Platform:** Node.js / TypeScript ({stack.framework or 'npm'})
- **Design Pattern:** Modular Component / Service Architecture

## Core Layers

1. **Types / Schemas:** Data transfer objects, interfaces, and validation schemas.
2. **Services / Modules:** Core business logic and reusable functions.
3. **Entrypoints / Controllers:** Route handlers, CLI commands, or UI components.

## Concurrency & Execution

- Use standard async/await and Promises.
- Avoid unhandled promise rejections and state leaks.
"""
    if stack and stack.language == "go":
        return f"""# Architecture Guide

Architectural overview and guidelines for {project_name}.

## Overview

- **Platform:** Go ({stack.framework or 'go test'})
- **Design Pattern:** Standard Go Package Layout

## Core Layers

1. **Domain Types:** Structs and interfaces defining domain boundaries.
2. **Internal Packages:** Isolated packages with clear responsibilities.
3. **Entrypoints:** Main packages under `cmd/`.

## Concurrency & Error Handling

- Use goroutines and channels with `context.Context` cancellation.
- Explicit error returns (`if err != nil`).
"""

    return f"""# Architecture Guide

Architectural overview and guidelines for {project_name}.

## Overview

- **Primary Scheme / Target:** `{scheme}`
- **Platform:** iOS / macOS (Swift)
- **Design Pattern:** MVVM (Model-View-ViewModel) / Clean Architecture

## Core Layers

1. **Models / Entities:** Domain data structures and business logic primitives.
2. **ViewModels / State Holders:** Observable view models managing UI state and user intent.
3. **Views / UI:** SwiftUI / UIKit views responding to view model state.
4. **Services / Repositories:** Network, persistence, and external integration clients.

## Concurrency & Data Flow

- Utilize Swift Concurrency (`async/await`, `@MainActor`, `Task`) for asynchronous operations.
- Avoid shared mutable global state; use dependency injection where appropriate.
"""


def coding_standards_docs(project_name: str, stack: Any = None) -> str:
    if stack and stack.language == "python":
        return f"""# Coding Standards

Coding standards and conventions for {project_name}.

## Python Standards

- **Language Version:** Python 3.10+
- **Style Guidelines:** Follow PEP 8 and use modern type hints (`def foo(val: str) -> int:`).
- **Naming Conventions:**
  - Classes: `UpperCamelCase`
  - Functions, variables, modules: `snake_case`
  - Constants: `UPPER_SNAKE_CASE`
- **Error Handling:** Use explicit exception types and avoid bare `except:`.
- **Testing:**
  - Write unit and integration tests using {stack.framework or 'pytest'}.
  - Test command: `{stack.test_command or 'pytest'}`.
"""
    if stack and stack.language == "rust":
        return f"""# Coding Standards

Coding standards and conventions for {project_name}.

## Rust Standards

- **Language Edition:** Rust 2021+
- **Formatting & Linting:** Run `cargo fmt --check` and `cargo clippy`.
- **Naming Conventions:**
  - Structs, Enums, Traits: `UpperCamelCase`
  - Functions, variables, modules: `snake_case`
  - Constants: `SCREAMING_SNAKE_CASE`
- **Error Handling:** Use `Result<T, E>` and the `?` operator; avoid `.unwrap()` in production code.
- **Testing:**
  - Unit tests in `src/` under `#[cfg(test)]`, integration tests in `tests/`.
  - Test command: `{stack.test_command or 'cargo test'}`.
"""
    if stack and stack.language in ("typescript", "javascript"):
        return f"""# Coding Standards

Coding standards and conventions for {project_name}.

## TypeScript / JavaScript Standards

- **Language Standards:** Strict TypeScript / modern ECMAScript.
- **Formatting:** Consistent formatting (Prettier / ESLint conventions).
- **Naming Conventions:**
  - Classes, Interfaces, Types: `UpperCamelCase`
  - Functions, variables: `lowerCamelCase`
  - Constants: `UPPER_SNAKE_CASE`
- **Error Handling:** Structured error throwing and typed error handling.
- **Testing:**
  - Unit and integration tests for all features and fixes.
  - Test command: `{stack.test_command or 'npm test'}`.
"""
    if stack and stack.language == "go":
        return f"""# Coding Standards

Coding standards and conventions for {project_name}.

## Go Standards

- **Language Standards:** Go 1.20+
- **Formatting:** Always run `gofmt`.
- **Naming Conventions:**
  - Exported identifiers: `UpperCamelCase`
  - Unexported identifiers: `lowerCamelCase`
- **Error Handling:** Explicit error checking (`if err != nil`); avoid panics in libraries.
- **Testing:**
  - Unit tests in `*_test.go` with standard `testing` package.
  - Test command: `{stack.test_command or 'go test ./...'}`.
"""

    return f"""# Coding Standards

Coding standards and conventions for {project_name}.

## Swift Standards

- **Language Version:** Swift 6.0+
- **Formatting:** Follow standard Swift style guidelines (consistent indentation, clear naming).
- **Naming Conventions:**
  - Types and Protocols: `UpperCamelCase`
  - Functions, variables, and properties: `lowerCamelCase`
  - Enums: `lowerCamelCase` for cases
- **Error Handling:** Prefer explicit Swift `Error` types and structured `do/catch` over force unwrapping (`!`).
- **Testing:**
  - Write unit tests for new features and bug fixes.
  - Utilize Swift Testing (`@Test`, `#expect`) or XCTest frameworks.
  - Test command: `swift test` or `xcodebuild test`.
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


def write_starter_docs(root: Path, config: dict, force: bool, stack: Any = None) -> list[Path]:
    if stack is None:
        from orchestrator.stack_detection import detect_project_stack
        stack = detect_project_stack(root)

    paths = [
        root / "AGENTS.md",
        root / "docs" / "build-test-commands.md",
        root / "docs" / "ai-workflow.md",
        root / "docs" / "architecture.md",
        root / "docs" / "coding-standards.md",
    ]
    write_text_file(paths[1], build_test_docs(config, stack), force)
    write_text_file(paths[2], ai_workflow_docs(config["project_name"]), force)
    write_text_file(paths[0], agents_docs(config["project_name"]), force)
    write_text_file(paths[3], architecture_docs(config["project_name"], config.get("scheme", "App"), stack), force)
    write_text_file(paths[4], coding_standards_docs(config["project_name"], stack), force)
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


def print_wizard_bar(skip_available: bool = True, status_bar: Any | None = None, text_input: bool = False, enter_hint: str | None = None):
    """Refreshes the sticky footer for the wizard."""
    if not status_bar:
        return
        
    # Standardized color scheme: Green for active/skip, Red for exit/quit
    skip_msg = " [\033[1;92mS\033[0m] Skip Section |" if skip_available else ""
    bar_label = f"{skip_msg} [\033[1;91mQ\033[0m] Quit Wizard"
    
    # Visible plain text for metadata bar
    plain_skip = " [S] Skip Section |" if skip_available else ""
    q_msg = f"{plain_skip} [Q] Quit Wizard"
    if text_input:
        q_msg = ("[Ctrl-S] Skip section | " if skip_available else "") + "[Ctrl-Q] Quit"
    
    if enter_hint:
        q_msg += f" | Enter: {enter_hint}"

    # We use the status bar to draw at the bottom
    status_bar.render(at_bottom=True, force=True, q_msg=q_msg)


def prompt_text(label: str, default: str | None = None, skip_available: bool = True, status_bar: Any | None = None, *, optional: bool = False, enter_hint: str | None = None) -> str:
    from orchestrator.scripts.common import get_key, clear_choice_placeholder
    suffix = f" [{default}]" if default else ""
    hint = enter_hint or ("keep default" if default else "skip this field" if optional else "submit")
    field_label = f"{label} (optional)" if optional else label
    
    while True:
        if status_bar:
            print_wizard_bar(skip_available, status_bar, text_input=True, enter_hint=hint)
            sys.stdout.write("\033[?25h")
        
        # Ensure we are at the start of a line
        sys.stdout.write("\r")
        print(f"{field_label}{suffix} (Enter: {hint}): ", end="", flush=True)
        
        # Use get_key for consistent hotkeys
        val = ""
        while True:
            key = get_key()
            if key == "enter":
                print()
                return val or (default or "")
            if key == "\x11":
                print("\033[1;91mquit\033[0m")
                if status_bar: status_bar.reset_scroll_region()
                sys.exit(0)
            if key == "\x13" and skip_available:
                print("\033[1;92mskip\033[0m")
                raise SkipSectionException()
            if key == "backspace":
                if val:
                    val = val[:-1]
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
            elif key == "space":
                val += " "
                sys.stdout.write(" ")
                sys.stdout.flush()
            elif len(key) == 1:
                val += key
                sys.stdout.write(key)
                sys.stdout.flush()


def prompt_yes_no(label: str, default: bool = False, skip_available: bool = True, status_bar: Any | None = None) -> bool:
    from orchestrator.scripts.common import get_key
    suffix = "Y/n" if default else "y/N"
    
    if status_bar:
        print_wizard_bar(skip_available, status_bar, enter_hint="Yes" if default else "No")
        
    sys.stdout.write("\r")
    print(f"{label} [{suffix}] (Enter: {'Yes' if default else 'No'}): ", end="", flush=True)
    
    while True:
        key = get_key().strip().lower()
        if key in {"q", "\x11"}:
            if status_bar:
                status_bar.reset_scroll_region()
            raise SystemExit(0)
        if key in {"s", "\x13"} and skip_available:
            raise SkipSectionException()
        if key in {"enter", ""}:
            print("yes" if default else "no")
            return default
        if key in {"y", "yes", "n", "no"}:
            answer = key in {"y", "yes"}
            print("yes" if answer else "no")
            return answer
        print("Please choose Y or N (Enter keeps the default): ", end="", flush=True)


def prompt_password(label: str, placeholder: str = "(enter to skip)") -> str:
    from orchestrator.scripts.common import prompt_password as _prompt_password
    return _prompt_password(label, placeholder)


def prompt_radio(label: str, options: list[str], default: str | None = None, clear_screen: bool = True, status_bar: Any | None = None, description: str | None = None) -> str:
    from orchestrator.scripts.common import BackException, prompt_radio as _prompt_radio
    try:
        return _prompt_radio(label, options, default, clear_screen, status_bar, description)
    except BackException as exc:
        raise SkipSectionException() from exc


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
        "orchestrator_package_path": "~/.orchestrator/package",
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
    prompts_source = safe_resolve(Path(__file__)).parent / "prompts"
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


def audit_project_setup(root: Path) -> dict[str, Any]:
    """
    Evaluates current workspace setup status across all core pillars:
    - Project & Xcode Configuration
    - Grounding Documentation
    - AI Providers & Models
    - GitHub Source Control
    - Worker Fleet
    - Distribution & Signing (Optional)
    """
    runtime_dir = root / DEFAULT_RUNTIME_DIRNAME
    project_file = runtime_dir / "project.json"
    machines_file = runtime_dir / "config" / "machines.json"
    settings_file = runtime_dir / "config" / "settings.json"
    
    categories: dict[str, list[dict[str, Any]]] = {}
    gaps: list[str] = []
    
    # 1. Project & Xcode
    from orchestrator.stack_detection import detect_project_stack
    stack = detect_project_stack(root)
    xcode_proj, xcode_ws, detected_scheme, detected_schemes, detected_targets = infer_xcode(root)
    has_project_file = project_file.exists()
    project_data = {}
    if has_project_file:
        try:
            project_data = json.loads(project_file.read_text(encoding="utf-8"))
        except Exception:
            pass
            
    proj_items = []
    # Detected Stack
    proj_items.append({"name": "Detected Stack", "status": "ok", "detail": f"{stack.display_name}"})

    # Git repo
    is_git = (root / ".git").exists()
    if is_git:
        remote = git_remote(root)
        if remote:
            remote_display = remote if len(remote) <= 35 else (remote[:32] + "...")
            proj_items.append({"name": "Git Repository", "status": "ok", "detail": f"Initialized ({remote_display})"})
        else:
            proj_items.append({"name": "Git Repository", "status": "ok", "detail": "Initialized (No remote)"})
    else:
        proj_items.append({"name": "Git Repository", "status": "gap", "detail": "MISSING (.git not found)", "fix": "Run 'git init'"})
        gaps.append("Git repository is not initialized (.git missing)")

    # Xcode Project/Workspace
    xcode_target = xcode_ws or xcode_proj
    if xcode_target:
        proj_items.append({"name": "Xcode Project", "status": "ok", "detail": f"{Path(xcode_target).name}"})
    else:
        proj_items.append({"name": "Xcode Project", "status": "optional", "detail": "Not detected (Swift package or non-Xcode)"})

    # Project JSON
    if has_project_file and project_data:
        if stack.uses_xcode:
            scheme_name = project_data.get("scheme") or detected_scheme or "Not set"
            proj_items.append({"name": "Project Config", "status": "ok", "detail": f"project.json (Scheme: {scheme_name})"})
        else:
            build_info = project_data.get("build_command") or stack.build_command or "None"
            proj_items.append({"name": "Project Config", "status": "ok", "detail": f"project.json (Build: {build_info})"})
    else:
        proj_items.append({"name": "Project Config", "status": "gap", "detail": "MISSING (.orchestrator/project.json)", "fix": "Configure project"})
        gaps.append("Project configuration file (.orchestrator/project.json) is missing")

    categories["Project & Xcode"] = proj_items

    # 2. Grounding Docs
    doc_items = []
    req_docs = [
        ("AGENTS.md", root / "AGENTS.md", "Core AI Rules"),
        ("Build Commands", root / "docs" / "build-test-commands.md", "docs/build-test-commands.md"),
        ("Architecture", root / "docs" / "architecture.md", "docs/architecture.md"),
        ("Coding Standards", root / "docs" / "coding-standards.md", "docs/coding-standards.md"),
        ("AI Workflow", root / "docs" / "ai-workflow.md", "docs/ai-workflow.md"),
    ]
    for label, path, rel_path in req_docs:
        if path.exists():
            doc_items.append({"name": label, "status": "ok", "detail": "Present"})
        else:
            doc_items.append({"name": label, "status": "gap", "detail": f"MISSING ({rel_path})", "fix": f"Create {rel_path}"})
            gaps.append(f"Grounding doc '{label}' is missing ({rel_path})")

    # Helper script
    helper_script_path = root / "scripts" / "orchestrator"
    if helper_script_path.exists():
        doc_items.append({"name": "Helper CLI Script", "status": "ok", "detail": "scripts/orchestrator (Ready)"})
    else:
        doc_items.append({"name": "Helper CLI Script", "status": "optional", "detail": "scripts/orchestrator (Not created)"})

    # Role prompts
    prompts_dir = runtime_dir / "prompts"
    if prompts_dir.exists() and any(prompts_dir.iterdir()):
        doc_items.append({"name": "Role Prompts", "status": "ok", "detail": "Custom overrides installed"})
    else:
        doc_items.append({"name": "Role Prompts", "status": "optional", "detail": "Default built-in (No overrides)"})

    categories["Grounding & Documentation"] = doc_items

    # 3. AI Providers & Models
    ai_items = []
    from orchestrator.scripts.model_registry import get_all_models
    all_models = get_all_models()
    required_clis = set()
    for m in all_models:
        required_clis.update(m.required_clis)

    ready_clis = []
    for cli in sorted(required_clis):
        is_ready, msg = check_cli_auth(cli)
        if is_ready:
            ready_clis.append(cli)
            ai_items.append({"name": f"{cli.capitalize()} CLI", "status": "ok", "detail": "READY"})
        elif shutil.which(cli):
            ai_items.append({"name": f"{cli.capitalize()} CLI", "status": "gap", "detail": "Installed (NOT LOGGED IN)", "fix": f"Log in to {cli}"})
        else:
            ai_items.append({"name": f"{cli.capitalize()} CLI", "status": "optional", "detail": "Not installed"})

    # Check API keys
    has_keys = False
    if settings_file.exists():
        try:
            s_data = json.loads(settings_file.read_text(encoding="utf-8"))
            has_keys = any(s_data.get(k) for k in ["gemini_api_key", "anthropic_api_key", "openai_api_key", "ollama_api_key"])
        except Exception:
            pass
    if not has_keys:
        has_keys = any(k in os.environ for k in ["GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY"])

    if has_keys:
        ai_items.append({"name": "Fallback API Keys", "status": "ok", "detail": "Configured"})
    else:
        ai_items.append({"name": "Fallback API Keys", "status": "optional", "detail": "None (Using CLI auth)"})

    if not ready_clis and not has_keys:
        gaps.append("No active AI provider or API key is available")

    # Configured models
    active_models = []
    if machines_file.exists():
        try:
            m_data = json.loads(machines_file.read_text(encoding="utf-8"))
            for m in m_data.get("machines", []):
                if m.get("enabled", True) and m.get("models"):
                    active_models.extend(m.get("models", []))
            active_models = list(dict.fromkeys(active_models))
        except Exception:
            pass
    if active_models:
        ai_items.append({"name": "Active Models", "status": "ok", "detail": f"{len(active_models)} configured ({', '.join(active_models[:3])})"})
    else:
        ai_items.append({"name": "Active Models", "status": "gap", "detail": "None configured", "fix": "Select active models"})
        gaps.append("No active AI models are configured in machines.json")

    categories["AI Providers & Models"] = ai_items

    # 4. GitHub Integration
    gh_items = []
    from orchestrator.scripts.dev_console import get_github_auth_info
    has_gh, gh_accounts, gh_active_user = get_github_auth_info()
    if has_gh:
        gh_items.append({"name": "GitHub CLI (gh)", "status": "ok", "detail": "Installed"})
        if gh_accounts:
            gh_items.append({"name": "GitHub Account", "status": "ok", "detail": f"{gh_active_user} (Active · {len(gh_accounts)} account(s))"})
        else:
            gh_items.append({"name": "GitHub Account", "status": "gap", "detail": "NOT LOGGED IN", "fix": "Run 'gh auth login'"})
            gaps.append("GitHub CLI is installed but not authenticated")
    else:
        gh_items.append({"name": "GitHub CLI (gh)", "status": "gap", "detail": "MISSING", "fix": "Run 'brew install gh'"})
        gaps.append("GitHub CLI ('gh') is not installed")

    categories["GitHub Source Control"] = gh_items

    # 5. Worker Fleet
    fleet_items = []
    if machines_file.exists():
        try:
            m_data = json.loads(machines_file.read_text(encoding="utf-8"))
            machines_list = m_data.get("machines", [])
            local_m = [m for m in machines_list if m.get("execution_mode") == "local"]
            remote_m = [m for m in machines_list if m.get("execution_mode") == "ssh"]
            fleet_items.append({"name": "Fleet Config", "status": "ok", "detail": f"machines.json ({len(machines_list)} machine(s))"})
            if remote_m:
                fleet_items.append({"name": "Remote Workers", "status": "ok", "detail": f"{len(remote_m)} SSH worker(s) configured"})
            else:
                fleet_items.append({"name": "Remote Workers", "status": "optional", "detail": "None (Local execution only)"})
        except Exception:
            fleet_items.append({"name": "Fleet Config", "status": "gap", "detail": "Invalid machines.json", "fix": "Regenerate machines.json"})
            gaps.append("machines.json is corrupted")
    else:
        fleet_items.append({"name": "Fleet Config", "status": "gap", "detail": "MISSING (machines.json)", "fix": "Create machines.json"})
        gaps.append("Fleet configuration (machines.json) is missing")

    categories["Worker Fleet"] = fleet_items

    # 6. Distribution & Code Signing (Optional)
    dist_items = []
    fb_enabled = project_data.get("firebase_distribution", False)
    if fb_enabled:
        dist_items.append({"name": "Firebase Distribution", "status": "ok", "detail": "ENABLED"})
        team = project_data.get("development_team")
        if team:
            dist_items.append({"name": "Development Team ID", "status": "ok", "detail": team})
        else:
            dist_items.append({"name": "Development Team ID", "status": "gap", "detail": "MISSING", "fix": "Set team_id"})
            gaps.append("Firebase distribution enabled but Development Team ID is missing")
            
        plist_p = project_data.get("firebase_plist_path")
        if plist_p and (root / plist_p).exists():
            dist_items.append({"name": "GoogleService-Info.plist", "status": "ok", "detail": "Found"})
        else:
            dist_items.append({"name": "GoogleService-Info.plist", "status": "gap", "detail": f"MISSING ({plist_p or 'Not specified'})", "fix": "Add plist"})
            gaps.append("GoogleService-Info.plist is missing")
    else:
        dist_items.append({"name": "Firebase Distribution", "status": "optional", "detail": "Not configured (Optional)"})

    categories["Distribution & Signing"] = dist_items

    total_checks = sum(len(items) for items in categories.values())
    ok_count = sum(sum(1 for i in items if i["status"] == "ok") for items in categories.values())
    gap_count = len(gaps)

    return {
        "categories": categories,
        "gaps": gaps,
        "total_checks": total_checks,
        "ok_count": ok_count,
        "gap_count": gap_count,
    }


def print_setup_audit_report(audit: dict[str, Any]) -> None:
    """Renders the current setup status and gaps audit report."""
    print(f"\n\033[1;96m{'='*20} Setup Status & Gaps Audit {'='*20}\033[0m\n")
    for category_name, items in audit["categories"].items():
        print(f"  \033[1;97m{category_name}\033[0m")
        for item in items:
            status = item["status"]
            if status == "ok":
                icon = "\033[92m✅\033[0m"
            elif status == "gap":
                icon = "\033[1;91m⚠️ \033[0m"
            else:
                icon = "\033[90m○ \033[0m"
            print(f"    {icon} {item['name']:24} : {item['detail']}")
        print()

    if audit["gap_count"] == 0:
        print(f"  \033[1;92m✨ All core components configured ({audit['ok_count']}/{audit['total_checks']} checks passing).\033[0m")
    else:
        print(f"  \033[1;93m🔍 Found {audit['gap_count']} setup gap(s) requiring attention:\033[0m")
        for i, gap in enumerate(audit["gaps"], 1):
            print(f"    \033[93m{i}. {gap}\033[0m")
    print(f"\n\033[1;96m{'='*67}\033[0m\n")


def wizard_stage(number: int, title: str) -> None:
    print(f"\n\033[1;96m[{number}/5] {title}\033[0m")


def wizard_edit_fields(fields: dict[str, str], status_bar: Any) -> dict[str, str]:
    """Review defaults together and edit only the fields that need changing."""
    fields = dict(fields)
    while True:
        labels = list(fields)
        for index, (label, value) in enumerate(fields.items(), 1):
            print(f"  {index}. {label}: {value or '(not set)'}")
        try:
            choice = prompt_text("Field number to edit", "", status_bar=status_bar, enter_hint="continue")
            if not choice:
                return fields
            if not choice.isdigit() or not 1 <= int(choice) <= len(labels):
                print("Choose a field number from the list.")
                continue
            label = labels[int(choice) - 1]
            fields[label] = prompt_text(label, fields[label], status_bar=status_bar, enter_hint="keep default" if fields[label] else "leave unchanged")
        except SkipSectionException:
            return fields


def wizard_choose_items(title: str, options: list[tuple[str, str]], selected: list[str], status_bar: Any, *, enter_hint: str = "keep selection") -> list[str]:
    """Choose numbered items without needing to memorize identifiers."""
    while True:
        print(title)
        for index, (identifier, label) in enumerate(options, 1):
            mark = "x" if identifier in selected else " "
            print(f"  {index}. [{mark}] {label}")
        try:
            value = prompt_text("Numbers separated by commas; 0 clears", "", status_bar=status_bar, enter_hint=enter_hint)
        except SkipSectionException:
            return selected
        if not value:
            return selected
        if value == "0":
            return []
        parts = [part.strip() for part in value.split(",")]
        if all(part.isdigit() and 1 <= int(part) <= len(options) for part in parts):
            return list(dict.fromkeys(options[int(part) - 1][0] for part in parts))
        print("Choose numbers from the list, for example 1,3.")


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
    root_arg = args.project or args.root
    root = safe_resolve(Path(root_arg).expanduser()) if root_arg else find_project_root()
    models = parse_csv(args.models)

    old_project_root_env = os.environ.get("ORCHESTRATOR_PROJECT_ROOT")
    os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(root)
    try:
        return _run_wizard_impl(args, root, models)
    finally:
        if old_project_root_env is None:
            os.environ.pop("ORCHESTRATOR_PROJECT_ROOT", None)
        else:
            os.environ["ORCHESTRATOR_PROJECT_ROOT"] = old_project_root_env


def _run_wizard_impl(args: argparse.Namespace, root: Path, models: list[str]) -> int:
    from orchestrator.scripts.common import StatusBar
    from orchestrator.scripts.model_registry import get_all_models

    if args.non_interactive and not models:
        print("Wizard requires at least one model. Pass --models codex or run interactively.")
        return 1

    # Define variables early for validation and use
    firebase_enabled = args.firebase
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
        config_dir = runtime_dir / "config"
        project_file = runtime_dir / "project.json"
        machines_file = config_dir / "machines.json"
        settings_file = config_dir / "settings.json"
        review_paths: list[Path] = []

        # Interactive stage overview
        if not args.non_interactive:
            print("Project → AI setup → Optional tools → Review and apply → Verify and finish")
            print("Settings are saved after review. CLI logins happen immediately when selected.")
            wizard_stage(1, "Project")

        # ----------------------------------------------------
        # 1. Project & Stack Configuration
        # ----------------------------------------------------
        from orchestrator.stack_detection import detect_project_stack
        stack = detect_project_stack(root)
        xcode_proj, xcode_ws, det_scheme, det_schemes, det_targets = infer_xcode(root)

        existing_p_cfg: dict[str, Any] = {}
        if project_file.exists() and not args.force:
            try:
                existing_p_cfg = json.loads(project_file.read_text(encoding="utf-8"))
            except Exception:
                existing_p_cfg = {}

        p_name = existing_p_cfg.get("project_name") or args.project_name or root.name
        p_branch = existing_p_cfg.get("base_branch") or args.base_branch or "main"

        if stack.uses_xcode:
            p_scheme = existing_p_cfg.get("scheme") or args.scheme or det_scheme or "App"
            p_target = existing_p_cfg.get("test_target") or args.test_target or next((t for t in det_targets if t.endswith("Tests")), f"{p_scheme}Tests")
            p_xcode = existing_p_cfg.get("xcode_project") or existing_p_cfg.get("xcode_workspace") or xcode_proj or xcode_ws or "None"
            p_build_cmd = existing_p_cfg.get("build_command") or getattr(args, "build_command", None)
            p_test_cmd = existing_p_cfg.get("test_command") or getattr(args, "test_command", None)

        else:
            p_scheme = existing_p_cfg.get("scheme") or getattr(args, "scheme", None)
            p_target = existing_p_cfg.get("test_target") or getattr(args, "test_target", "") or ""
            p_build_cmd = existing_p_cfg.get("build_command") or getattr(args, "build_command", None) or stack.build_command or ""
            p_test_cmd = existing_p_cfg.get("test_command") or getattr(args, "test_command", None) or stack.test_command or ""

        def edit_project_fields():
            nonlocal p_name, p_branch, p_scheme, p_target, p_build_cmd, p_test_cmd
            print(f"Detected stack: {stack.display_name}")
            if stack.uses_xcode:
                print(f"Xcode container: {p_xcode}")
                fields = wizard_edit_fields({"Project Name": p_name, "Xcode Scheme": p_scheme,
                                            "Test Target": p_target, "Base Branch": p_branch}, status_bar)
                p_name, p_scheme, p_target, p_branch = fields.values()
            else:
                fields = wizard_edit_fields({"Project Name": p_name, "Base Branch": p_branch,
                                            "Build Command": p_build_cmd, "Test Command": p_test_cmd}, status_bar)
                p_name, p_branch, p_build_cmd, p_test_cmd = fields.values()

        if not args.non_interactive:
            edit_project_fields()

        # ----------------------------------------------------
        # 2. AI Providers & Models
        # ----------------------------------------------------
        if not args.non_interactive:
            wizard_stage(2, "AI setup")
        pending_keys: dict[str, str] = {}
        all_models = get_all_models()
        configured_models: list[str] = []
        if machines_file.exists():
            try:
                m_conf = json.loads(machines_file.read_text(encoding="utf-8"))
                for m in m_conf.get("machines", []):
                    for mod in m.get("models", []):
                        if mod not in configured_models:
                            configured_models.append(mod)
            except Exception:
                pass

        if not models and configured_models:
            models = list(configured_models)

        if not args.models and not args.non_interactive:
            try:
                print(f"\n\033[1;96m{'='*20} AI Providers & Models {'='*20}\033[0m")
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

                # If no models configured yet, auto-select ready ones
                if not models:
                    for m in all_models:
                        if not m.required_clis:
                            continue
                        if all(cli_status.get(c, False) for c in m.required_clis):
                            models.append(m.id)

                # Check if settings.json already has API keys
                if settings_file.exists():
                    try:
                        s_vals = json.loads(settings_file.read_text(encoding="utf-8"))
                        if s_vals.get("gemini_api_key") and "gemini" not in models: models.append("gemini")
                        if s_vals.get("anthropic_api_key") and "claude" not in models: models.append("claude")
                        if s_vals.get("openai_api_key") and "codex" not in models: models.append("codex")
                        if s_vals.get("ollama_api_key") and "ollama" not in models: models.append("ollama")
                    except Exception:
                        pass

                print(f"\nActive models: \033[1;92m{', '.join(models) if models else 'None'}\033[0m")
                print("\033[90m(Press [Enter] to keep active models, or choose an option below)\033[0m")

                # Interactive Login / Key / Model Setup Loop
                while True:
                    not_logged_in = [cli for cli, ready in cli_status.items() if not ready and shutil.which(cli)]

                    prompt = "\033[1;97m[Enter] Keep active models\033[0m | \033[1;97m[M] Modify models\033[0m | \033[1;97m[L] Log in via CLI\033[0m | \033[1;97m[K] Enter API Key\033[0m"
                    if not models:
                        prompt = "\033[1;97m[M] Select models\033[0m | \033[1;97m[L] Log in via CLI\033[0m | \033[1;97m[K] Enter API Key\033[0m | \033[1;91m[C] Cancel\033[0m"

                    print_wizard_bar(skip_available=bool(models), status_bar=status_bar)
                    print(f"\n{prompt}")
                    from orchestrator.scripts.common import get_key
                    sys.stdout.write("Choice: ")
                    sys.stdout.flush()
                    choice = get_key().strip().lower()

                    if choice == 'q':
                        print("\033[1;91mquit\033[0m")
                        status_bar.reset_scroll_region()
                        sys.exit(0)
                    if choice == 's' and models:
                        print("\033[1;92mskip\033[0m")
                        break

                    if choice in {'enter', ''} and models:
                        print("\033[1;92mkeep current models\033[0m")
                        break
                    elif choice == 'm':
                        print("\033[97mmodify models\033[0m")
                        models = wizard_choose_items("Available models", [(m.id, m.id) for m in all_models], models, status_bar)
                        print(f"Active models: {', '.join(models) or 'None — select at least one'}")
                    elif choice == 'l':
                        print("\033[97mlogin\033[0m")
                        if not not_logged_in:
                            print("All detected CLIs are already authenticated.")
                            continue
                        print("\nInstalled but unauthenticated providers:")
                        for i, cli in enumerate(not_logged_in, 1):
                            print(f"  {i}. {cli}")

                        sub_choice = input(f"\nSelect number to log in (or \033[1;91mEnter to go back\033[0m): ").strip()
                        if not sub_choice: continue
                        try:
                            idx = int(sub_choice) - 1
                            if 0 <= idx < len(not_logged_in):
                                target_cli = not_logged_in[idx]
                                auth_cmd = get_auth_command(target_cli)
                                if auth_cmd:
                                    print(f"\nRunning: \033[97m{auth_cmd}\033[0m")
                                    subprocess.run(auth_cmd.split(), check=False)

                                    # Re-check status
                                    is_ready, msg = check_cli_auth(target_cli)
                                    cli_status[target_cli] = is_ready
                                    print(f"  {target_cli.ljust(10)} : {msg}")

                                    if is_ready:
                                        for m in all_models:
                                            if m.id in models: continue
                                            if not m.required_clis: continue
                                            if all(cli_status.get(c, False) for c in m.required_clis):
                                                models.append(m.id)
                                else:
                                    print(f"No auth command known for {target_cli}.")
                        except ValueError:
                            print("Invalid choice.")
                    elif choice == 'k':
                        print("\033[97mAPI Key Entry\033[0m")
                        key_options = [
                            "Antigravity / Gemini (gemini_api_key)",
                            "Anthropic Claude (anthropic_api_key)",
                            "OpenAI Codex (openai_api_key)",
                            "Ollama Host / Key (ollama_api_key)"
                        ]
                        try:
                            selected_key_opt = prompt_radio("Select Provider Key to Configure:", key_options, default=key_options[0], status_bar=status_bar)
                        except SkipSectionException:
                            continue
                        
                        key_id = None
                        model_id = None
                        if "gemini" in selected_key_opt:
                            key_id, model_id = "gemini_api_key", "gemini"
                        elif "anthropic" in selected_key_opt:
                            key_id, model_id = "anthropic_api_key", "claude"
                        elif "openai" in selected_key_opt:
                            key_id, model_id = "openai_api_key", "codex"
                        elif "ollama" in selected_key_opt:
                            key_id, model_id = "ollama_api_key", "ollama"
                            
                        if key_id:
                            entered_key = prompt_password(f"{key_id} (optional)", placeholder="(Enter: skip this field)")
                            if entered_key:
                                pending_keys[key_id] = entered_key
                                print(f"✅ {key_id} will be saved after review.")
                                if model_id and model_id not in models:
                                    models.append(model_id)
                    elif choice == 'c' and not models:
                        print("\033[1;91mcancel\033[0m")
                        status_bar.reset_scroll_region()
                        return 1
            except SkipSectionException:
                pass

        if not args.non_interactive and args.models:
            print(f"Active models: {', '.join(models)} (from --models)")

        if not models:
            status_bar.reset_scroll_region()
            print("Wizard requires at least one model. Pass --models codex or run interactively.")
            return 1

        optional_tools: list[str] = []
        if not args.non_interactive:
            wizard_stage(3, "Optional tools")
            options = [("github", "GitHub account and PR integration"),
                       ("workers", "Remote SSH workers"),
                       ("prompts", "Custom role prompts")]
            if stack.uses_xcode or firebase_enabled or existing_p_cfg.get("firebase_distribution") or existing_p_cfg.get("development_team") or existing_p_cfg.get("firebase_plist_path"):
                options.append(("delivery", "Firebase delivery and Apple signing"))
            optional_tools = wizard_choose_items("Choose tools to configure now (all optional)", options, [], status_bar, enter_hint="skip optional tools")
            print("Unselected tools keep their existing settings. You can configure them later.")
        configure_keychain = False
        install_workers: list[str] = []

        # ----------------------------------------------------
        # 3. GitHub Integration
        # ----------------------------------------------------
        if not args.non_interactive and "github" in optional_tools:
            try:
                print(f"\n\033[1;96m{'='*20} GitHub Integration {'='*20}\033[0m")
                print("Login and account switching update your GitHub CLI session immediately.")
                from orchestrator.scripts.dev_console import get_github_auth_info
                has_gh, gh_accounts, gh_active_user = get_github_auth_info()
                if not has_gh:
                    print("  \033[1;93m⚠️  GitHub CLI ('gh') is not installed.\033[0m")
                    print("     Install via: \033[97mbrew install gh\033[0m to enable automatic PR management & issue tracking.")
                elif not gh_accounts:
                    print("  \033[1;93m⚠️  GitHub CLI is installed, but no active account is logged in.\033[0m")
                    if prompt_yes_no("Log in to GitHub now (gh auth login)?", default=True, status_bar=status_bar):
                        subprocess.run(["gh", "auth", "login"], check=False)
                        has_gh, gh_accounts, gh_active_user = get_github_auth_info()
                        if gh_accounts:
                            print(f"  \033[1;92m✅ Logged in as {gh_active_user}.\033[0m")
                else:
                    acc_info = f"{gh_active_user} (Active · {len(gh_accounts)} account(s))" if len(gh_accounts) > 1 else f"{gh_active_user} (Active)"
                    print(f"Current status: \033[1;92m✅ Authenticated as {acc_info}\033[0m")
                    print("\033[90m(Press [Enter] to keep active account, or choose an action below)\033[0m\n")
                    print("  [Enter] Keep active account")
                    print("  [A] Add another GitHub account")
                    print("  [W] Switch active GitHub account")
                    print("  [V] View auth status details")
                    gh_choice = prompt_text("Select action (A/W/V)", "", status_bar=status_bar, enter_hint="keep active account").strip().lower()
                    if gh_choice == 'v':
                        subprocess.run(["gh", "auth", "status"], check=False)
                    elif gh_choice == 'a':
                        subprocess.run(["gh", "auth", "login"], check=False)
                    elif gh_choice == 'w':
                        if len(gh_accounts) > 1:
                            opt_list = [f"{a['user']} ({a['host']})" for a in gh_accounts]
                            selected = prompt_radio("Select Account to Switch To:", opt_list, default=opt_list[0], status_bar=status_bar)
                            user_to_switch = selected.split()[0]
                            subprocess.run(["gh", "auth", "switch", "--hostname", "github.com", "--user", user_to_switch], check=False)
                            print(f"  ✅ Switched active GitHub account to {user_to_switch}.")
                        else:
                            if prompt_yes_no("Only 1 account found. Add another account to switch?", default=True, status_bar=status_bar):
                                subprocess.run(["gh", "auth", "login"], check=False)
            except SkipSectionException:
                pass

        # ----------------------------------------------------
        # 4. Role Prompts
        # ----------------------------------------------------
        prompts_dir = runtime_dir / "prompts"
        has_custom_prompts = prompts_dir.exists() and any(prompts_dir.iterdir())

        overwrite_prompts = args.force
        copy_prompts = args.copy_prompt_overrides
        if not copy_prompts and not args.non_interactive and "prompts" in optional_tools:
            try:
                print(f"\n\033[1;96m{'='*20} Role Prompts {'='*20}\033[0m")
                if has_custom_prompts:
                    print(f"Current status: \033[1;92m✅ Custom prompt templates exist in {prompts_dir.relative_to(root)}\033[0m\n")
                    if prompt_yes_no("Overwrite custom prompts with latest default templates?", default=False, status_bar=status_bar):
                        copy_prompts = True
                        overwrite_prompts = True
                else:
                    print("Current status: \033[90mUsing built-in default role prompts\033[0m")
                    print("You can customize the AI's coding style by editing local copies of instruction prompts.\n")
                    copy_prompts = prompt_yes_no("Copy default role prompt templates into your project now?", default=False, status_bar=status_bar)
            except SkipSectionException:
                copy_prompts = False


        # ----------------------------------------------------
        # 5. Worker Fleet
        # ----------------------------------------------------
        existing_machines = []
        if machines_file.exists():
            try:
                m_conf = json.loads(machines_file.read_text(encoding="utf-8"))
                existing_machines = m_conf.get("machines", [])
            except Exception:
                pass

        if not args.non_interactive and "workers" in optional_tools:
            try:
                print(f"\n\033[1;96m{'='*20} Worker Fleet {'='*20}\033[0m")
                if existing_machines:
                    print(f"Current configured machines ({len(existing_machines)}):")
                    for m in existing_machines:
                        m_type = "Local" if m.get("execution_mode") == "local" else f"SSH ({m.get('ssh_target')})"
                        print(f"  • \033[97m{m['name']}\033[0m : {m_type}")
                    print()
                    scan_fleet = True
                else:
                    scan_fleet = True

                if scan_fleet:
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
                            status = c.get("status")
                            if status == "suitable":
                                print(f"  {i}. \033[97m{c['hostname']}\033[0m ({c['host']})")
                            elif status == "needs_ssh_keys":
                                print(f"  {i}. \033[90m{c['host']} (🔑 SSH key/authorization required)\033[0m")
                            else:
                                print(f"  {i}. \033[90m{c['host']} (⚠️ Connection error/unsuitable: {c.get('error', 'unknown')})\033[0m")

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

        install_workers = [machine["name"] for machine in ssh_machines] if args.install_workers else []
        if not args.non_interactive and ssh_machines and not install_workers:
            try:
                if prompt_yes_no("After saving, install/check packages on the added SSH workers?", default=False, status_bar=status_bar):
                    install_workers = [machine["name"] for machine in ssh_machines]
            except SkipSectionException:
                pass

        # ----------------------------------------------------
        # 6. Delivery & Code Signing
        # ----------------------------------------------------
        curr_p_cfg = existing_p_cfg
        curr_fb = curr_p_cfg.get("firebase_distribution", False)
        curr_team = curr_p_cfg.get("development_team")
        curr_method = curr_p_cfg.get("delivery_method") or "ad-hoc"
        curr_plist = curr_p_cfg.get("firebase_plist_path")
        curr_profile = curr_p_cfg.get("provisioning_profile_specifier")

        has_existing_distribution = bool(curr_fb or curr_team or curr_plist)

        if "delivery" in optional_tools and not firebase_enabled and not args.non_interactive:
            try:
                print(f"\n\033[1;96m{'='*20} Delivery & Code Signing {'='*20}\033[0m")
                if has_existing_distribution:
                    print("Current settings:")
                    print(f"  • Firebase Distribution : \033[97m{'ENABLED' if curr_fb else 'Disabled'}\033[0m")
                    print(f"  • Apple Development Team: \033[97m{curr_team or 'Not configured'}\033[0m")
                    print(f"  • Distribution Method   : \033[97m{curr_method}\033[0m")
                    print(f"  • GoogleService-Info    : \033[97m{curr_plist or 'Not configured'}\033[0m")
                    print(f"  • Provisioning Profile  : \033[97m{curr_profile or 'Automatic / None'}\033[0m\n")

                    if prompt_yes_no("Modify distribution and signing settings?", default=False, status_bar=status_bar):
                        firebase_enabled = True
                else:
                    print("Current status: \033[90mFirebase distribution is not configured (Optional)\033[0m\n")
                    firebase_enabled = prompt_yes_no("Configure Firebase distribution & code signing now?", default=False, status_bar=status_bar)
            except SkipSectionException:
                firebase_enabled = False

        if firebase_enabled:
            try:
                if not args.non_interactive:
                    import orchestrator.scripts.setup_distribution as sd
                    detected_team, detected_method = sd.detect_identity_info()

                    print("\n\033[1;96m--- iOS Signing Configuration ---\033[0m")
                    firebase_plist_path = firebase_plist_path or prompt_text("Firebase plist path", curr_plist or f"{root.name}/GoogleService-Info.plist", status_bar=status_bar)
                    team_id = team_id or prompt_text("Apple Development Team ID", curr_team or detected_team, status_bar=status_bar)
                    method = method or prompt_text("Distribution method (ad-hoc, debugging, app-store)", curr_method or detected_method or "ad-hoc", status_bar=status_bar)

                    # Interactive Provisioning Profile Selection
                    if not provisioning_profile:
                        provisioning_profile = curr_profile

                    if not provisioning_profile:
                        installed_profiles = sorted(list(sd.installed_provisioning_profile_names()))
                        if installed_profiles:
                            options = installed_profiles + ["[Enter profile name manually]", "[Skip]"]
                            print("\n🔍 Detected installed provisioning profiles:")
                            choice = prompt_radio("Select Provisioning Profile (optional; choose [Skip] to continue):", options, default=options[0], clear_screen=False, status_bar=status_bar)
                            if choice == "[Enter profile name manually]":
                                provisioning_profile = prompt_text("Provisioning Profile Specifier/Name", status_bar=status_bar, optional=True)
                            elif choice == "[Skip]":
                                provisioning_profile = None
                            else:
                                provisioning_profile = choice
                        else:
                            if prompt_yes_no("No installed provisioning profiles detected. Enter one manually?", False, status_bar=status_bar):
                                provisioning_profile = prompt_text("Provisioning Profile Specifier/Name", status_bar=status_bar, optional=True)
                    else:
                        if prompt_yes_no(f"Use currently configured Provisioning Profile ({provisioning_profile})?", True, status_bar=status_bar):
                            pass
                        else:
                            installed_profiles = sorted(list(sd.installed_provisioning_profile_names()))
                            default_option = provisioning_profile if provisioning_profile in installed_profiles else None
                            options = installed_profiles + ["[Enter profile name manually]", "[Skip]"]
                            print("\n🔍 Detected installed provisioning profiles:")
                            choice = prompt_radio("Select Provisioning Profile (optional; choose [Skip] to continue):", options, default=default_option or options[0], clear_screen=False, status_bar=status_bar)
                            if choice == "[Enter profile name manually]":
                                provisioning_profile = prompt_text("Provisioning Profile Specifier/Name", status_bar=status_bar, optional=True)
                            elif choice == "[Skip]":
                                provisioning_profile = None
                            else:
                                provisioning_profile = choice

                    if prompt_yes_no("Configure App Store Connect API keys for automated signing?", False, status_bar=status_bar):
                        asc_key_id = asc_key_id or prompt_password("ASC Key ID (optional)", placeholder="(Enter: skip this field)")
                        asc_issuer_id = asc_issuer_id or prompt_password("ASC Issuer ID (optional)", placeholder="(Enter: skip this field)")
                        asc_key_path = asc_key_path or prompt_text("ASC Key Path (.p8)", status_bar=status_bar, optional=True)
                    if prompt_yes_no("Configure automated keychain unlocking?", True, status_bar=status_bar):
                        configure_keychain = True

            except SkipSectionException:
                firebase_enabled = False
                configure_keychain = False

        required_field_errors = []
        if not p_name:
            required_field_errors.append("Project Name is required.")
        if stack.uses_xcode:
            if not p_xcode or p_xcode == "None":
                required_field_errors.append("Xcode project or workspace is required.")
            if not p_scheme:
                required_field_errors.append("Xcode Scheme is required.")
            if not p_target and not p_test_cmd:
                required_field_errors.append("Test Target or Test Command is required.")
        else:
            if not p_build_cmd:
                required_field_errors.append("Build Command is required for non-Xcode projects.")
            if not p_test_cmd:
                required_field_errors.append("Test Command is required for non-Xcode projects.")
        if required_field_errors:
            status_bar.reset_scroll_region()
            print("Wizard cannot apply the configuration:")
            for error in required_field_errors:
                print(f"  - {error}")
            return 1

        if not args.non_interactive:
            wizard_stage(4, "Review and apply")
            while True:
                print(f"Project: {p_name} | Base branch: {p_branch}")
                if stack.uses_xcode:
                    print(f"Scheme: {p_scheme} | Test target: {p_target}")
                else:
                    print(f"Build: {p_build_cmd or '(none)'}")
                    print(f"Test: {p_test_cmd or '(none)'}")
                print(f"Models for all workers: {', '.join(models)}")
                print(f"Workers to add/update: {', '.join(m['name'] for m in ssh_machines) or 'None'}")
                print(f"Worker package installs: {', '.join(install_workers) or 'None'}")
                print(f"Role prompts: {'Overwrite with defaults' if copy_prompts and overwrite_prompts else 'Copy missing defaults' if copy_prompts else 'Keep existing'}")
                print(f"API keys to save: {', '.join(pending_keys) or 'None'} (values hidden)")
                if firebase_enabled:
                    print(f"Delivery: Firebase | Team: {team_id} | Method: {method}")
                    print(f"Firebase plist: {firebase_plist_path} | Profile: {provisioning_profile or 'Automatic'}")
                    print("Regenerate scripts/distribute_ios.sh and scripts/ExportOptions.plist")
                    print(f"App Store Connect credentials: {'Configure' if asc_key_id or asc_issuer_id or asc_key_path else 'Keep existing'}")
                print(f"Keychain setup after saving: {'Yes' if configure_keychain else 'No'}")
                print("Save project and worker configuration; create missing grounding docs and helper script.")
                if args.force:
                    print("--force: existing generated configuration and starter docs will be overwritten.")
                action = prompt_text("[Enter] Apply | [P] Edit project | [M] Edit models | [C] Cancel", "", skip_available=False, status_bar=status_bar, enter_hint="apply changes").lower()
                if action == "c":
                    print("Setup cancelled. No project files were changed.")
                    return 0
                if action == "p":
                    edit_project_fields()
                    continue
                if action == "m":
                    selected = wizard_choose_items("Available models", [(m.id, m.id) for m in all_models], models, status_bar)
                    if selected:
                        models = selected
                    else:
                        print("At least one model is required; keeping the previous selection.")
                    continue
                if action == "":
                    break
                print("Choose P, M, C, or Enter to apply.")

        if existing_p_cfg and not args.force:
            existing_p_cfg["project_name"] = p_name
            existing_p_cfg["base_branch"] = p_branch
            existing_p_cfg["pr_base_branch"] = p_branch
            if stack.uses_xcode:
                existing_p_cfg["scheme"] = p_scheme
                existing_p_cfg["test_target"] = p_target
            else:
                existing_p_cfg["build_command"] = p_build_cmd or None
                existing_p_cfg["test_command"] = p_test_cmd or None
                if p_scheme:
                    existing_p_cfg["scheme"] = p_scheme
                if p_target:
                    existing_p_cfg["test_target"] = p_target
            project_file.write_text(json.dumps(existing_p_cfg, indent=2) + "\n", encoding="utf-8")
            if not args.non_interactive:
                print("  ✅ Project configuration saved.")
        else:
            init_args = argparse.Namespace(
                root=str(root),
                project=None,
                project_name=p_name,
                scheme=p_scheme,
                test_target=p_target,
                base_branch=p_branch,
                build_command=p_build_cmd,
                test_command=p_test_cmd,
                force=args.force,
                with_starter_docs=True,
                with_helper_script=True
            )
            init_project(init_args)
            review_paths.append(project_file)

        # Ensure all starter grounding docs and helper script exist
        try:
            p_cfg = json.loads(project_file.read_text(encoding="utf-8")) if project_file.exists() else {}
        except Exception:
            p_cfg = {}
        p_name = p_cfg.get("project_name", root.name)
        p_scheme = p_cfg.get("scheme", "App" if stack.uses_xcode else "")

        missing_grounding = False
        if not (root / "AGENTS.md").exists():
            write_text_file(root / "AGENTS.md", agents_docs(p_name), force=False)
            missing_grounding = True
        if not (root / "docs" / "build-test-commands.md").exists():
            write_text_file(root / "docs" / "build-test-commands.md", build_test_docs(p_cfg or {"project_name": p_name, "scheme": p_scheme, "build_command": p_build_cmd, "test_command": p_test_cmd, "xcode_workspace": None, "xcode_project": None}, stack), force=False)
            missing_grounding = True
        if not (root / "docs" / "architecture.md").exists():
            write_text_file(root / "docs" / "architecture.md", architecture_docs(p_name, p_scheme, stack), force=False)
            missing_grounding = True
        if not (root / "docs" / "coding-standards.md").exists():
            write_text_file(root / "docs" / "coding-standards.md", coding_standards_docs(p_name, stack), force=False)
            missing_grounding = True
        if not (root / "docs" / "ai-workflow.md").exists():
            write_text_file(root / "docs" / "ai-workflow.md", ai_workflow_docs(p_name), force=False)
            missing_grounding = True
        if not (root / "scripts" / "orchestrator").exists():
            write_helper_script(root, force=False)
            missing_grounding = True

        if missing_grounding and not args.non_interactive:
            print("  ✅ Missing grounding docs have been generated.")

        if pending_keys:
            settings = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.exists() else {}
            settings.update(pending_keys)
            settings_file.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        if copy_prompts:
            review_paths.extend(copy_prompt_overrides(root, overwrite_prompts))
        update_machine_models(config_dir, models, ssh_machines)
        if configure_keychain:
            print("Opening keychain setup. This changes your local keychain configuration.")
            if run_script("dev_console.py", ["keychain-setup"]) != 0:
                print("Configuration saved, but keychain setup failed. Run orchestrator wizard to retry.")
                return 1
        if firebase_enabled:
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
        # ----------------------------------------------------
        # 5. Verify and finish
        if not args.non_interactive:
            wizard_stage(5, "Verify and finish")
        if not args.non_interactive:
            print("Checking saved configuration...")
            if validate_config_command() != 0:
                print("Configuration saved, but validation failed. Fix the errors and run orchestrator check-config.")
                return 1

        if not args.non_interactive:
            print("Indexing project files and tests...")
            if run_script("index_project.py", []) != 0:
                print("Configuration saved, but indexing failed. Run orchestrator wizard to retry.")
                return 1
            cfg = load_project_config()
            if cfg.xcode_project or cfg.xcode_workspace:
                try:
                    check_xcode = prompt_yes_no("Check Xcode build settings (does not compile the app)?", default=True, status_bar=status_bar)
                except SkipSectionException:
                    check_xcode = False
                if check_xcode:
                    cmd_args = ["xcodebuild", "-scheme", cfg.scheme, "-showBuildSettings"]
                    if cfg.xcode_workspace:
                        cmd_args.extend(["-workspace", cfg.xcode_workspace])
                    else:
                        cmd_args.extend(["-project", cfg.xcode_project])
                    try:
                        subprocess.run(cmd_args, capture_output=True, text=True, check=True, timeout=30, cwd=str(root))
                        print("Xcode build settings verified.")
                    except Exception as exc:
                        print(f"Configuration saved, but Xcode validation failed: {exc}")
                        print("Check the scheme/container above and rerun orchestrator wizard.")
                        return 1
                else:
                    print("Xcode build settings check skipped.")

        if args.verify or install_workers:
            print("Checking tools and requested worker packages...")
            if verify_wizard_setup(install_workers) != 0:
                print("Configuration saved, but setup checks failed. Resolve the errors and run orchestrator check.")
                return 1
        else:
            print("Full tool and worker checks were not requested; run orchestrator check when ready.")
        remember_project(root, project_display_name(root), active=True)
        status_bar.reset_scroll_region()
        print(f"\n\033[1;92m{'='*20} Wizard Complete {'='*20}\033[0m")
        print("Configuration saved. Review these files before creating jobs:")
        for path in dict.fromkeys([*review_paths, project_file, machines_file, settings_file]):
            print(f"  - {path.relative_to(root)}")
        print("Next: orchestrator console")
        return 0


def init_project(args: argparse.Namespace) -> int:
    root_arg = getattr(args, "project", None) or args.root
    root = safe_resolve(Path(root_arg).expanduser()) if root_arg else find_project_root()
    runtime_dir = root / DEFAULT_RUNTIME_DIRNAME
    config_dir = runtime_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for subdir in ["jobs/inbox", "jobs/archive", "logs", "output", "state/machines"]:
        (runtime_dir / subdir).mkdir(parents=True, exist_ok=True)

    from orchestrator.stack_detection import detect_project_stack
    stack = detect_project_stack(root)

    xcode_project, xcode_workspace, detected_scheme, detected_schemes, detected_targets = infer_xcode(root)
    project_name = args.project_name or root.name

    build_cmd = getattr(args, "build_command", None) or (None if stack.uses_xcode else stack.build_command)
    test_cmd = getattr(args, "test_command", None) or (None if stack.uses_xcode else stack.test_command)

    if stack.uses_xcode:
        scheme = args.scheme or detected_scheme
        test_target = args.test_target or next((target for target in detected_targets if target.endswith("Tests")), f"{scheme}Tests")
    else:
        scheme = getattr(args, "scheme", None)
        test_target = getattr(args, "test_target", "") or ""

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
        "build_command": build_cmd,
        "test_command": test_cmd,
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
                    "supports_xcode": stack.uses_xcode,
                    "supports_simulator": stack.uses_xcode,
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
        write_starter_docs(root, config, args.force, stack=stack)

    if args.with_helper_script:
        write_helper_script(root, args.force)

    print("Run: orchestrator check")
    remember_project(root, config["project_name"], active=True)
    return 0


def validate_config_command() -> int:
    config = load_project_config()
    errors = validate_project_config(config)
    if config.firebase_distribution:
        errors.extend(config.validate_distribution_config())
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
    current_dir = safe_resolve(Path(__file__)).parent
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
    if not root.exists() or not root.is_dir():
        print(f"Project path does not exist: {root}")
        return 1
    remember_project(root, project_display_name(root), active=True)
    print_project_context(root)
    return 0


def remove_project_command(reference: str) -> int:
    if forget_project(reference):
        print(f"Removed '{reference}' from remembered projects.")
        return 0
    else:
        print(f"Project '{reference}' not found in remembered projects.")
        return 1


def prune_projects_command() -> int:
    load_recent_projects(prune_missing=True)
    print("Pruned missing projects. Current remembered projects:")
    return list_projects_command()


def update_command(args: argparse.Namespace) -> int:
    from orchestrator.project_config import PACKAGE_ROOT
    from orchestrator import __version__
    from orchestrator.scripts.common import print_header, print_section
    
    pkg_dir = PACKAGE_ROOT.parent
    is_git = (pkg_dir / ".git").exists()
    
    print_header("Orchestrator Update")
    print(f"Current Version: \033[97mv{__version__}\033[0m")
    
    if is_git:
        print_section("\n--- Local Update (Git Repository) ---", f"Location: {pkg_dir}")
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
        print_section("\n--- Fleet Update ---")
        try:
            return run_script("worker_tools.py", ["install"])
        except Exception as e:
            print(f"❌ Fleet update failed: {e}")
            return 1
        
    return 0


def fix_command(args: argparse.Namespace) -> int:
    if args.project and apply_project_env(args.project):
        return 1

    feedback = args.feedback
    if not feedback:
        print("\033[1;91mError: Please provide feedback or a bug description. Example: orchestrator fix \"button X is broken\"\033[0m")
        return 1

    from orchestrator.scripts.common import JOBS_DIR, read_json, print_header
    
    target_job_path = None
    if args.job:
        job_file = JOBS_DIR / f"{args.job}.json" if not args.job.endswith(".json") else Path(args.job)
        if job_file.exists():
            target_job_path = job_file
    
    if not target_job_path:
        job_files = sorted(list(JOBS_DIR.glob("*.json")), key=lambda p: p.stat().st_mtime, reverse=True)
        if job_files:
            target_job_path = job_files[0]

    print_header("FAST AUTO-FIX")
    print(f"   \033[1;36m• Feedback:\033[0m \033[1;97m{feedback}\033[0m", flush=True)

    if target_job_path and target_job_path.exists():
        job_data = read_json(target_job_path)
        print(f"   \033[1;36m• Target Job:\033[0m \033[97m{job_data.get('job_id')} ({job_data.get('title')})\033[0m\n", flush=True)
        
        script_args = ["bug", "--no-dispatch", "--update", str(target_job_path), "--feedback", feedback]
        if getattr(args, "free", False):
            script_args.append("--free")
        res = run_script("new_job.py", script_args)
        if res == 0:
            print("\n\033[1;92m🚀 Feedback applied. Dispatching auto-fix execution...\033[0m\n", flush=True)
            return run_script("schedule_job.py", [str(target_job_path)])
        return res
    else:
        print("   \033[1;36m• Creating new quick bug fix job...\033[0m\n", flush=True)
        script_args = ["bug", "--title", f"Quick Fix: {feedback[:40]}", "--raw-input", feedback]
        if getattr(args, "free", False):
            script_args.append("--free")
        return run_script("new_job.py", script_args)


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
        current = safe_resolve(safe_cwd())
        local_root = None
        for candidate in [current, *current.parents]:
            if (candidate / DEFAULT_RUNTIME_DIRNAME / "project.json").exists():
                local_root = candidate
                break
        
        if local_root:
            os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(local_root)
            argv = ["console"]
        else:
            recent = load_recent_projects(prune_missing=True)
            projects = recent.get("projects", [])

            from orchestrator.scripts.common import (
                clear_choice_placeholder,
                get_key,
                print_choice_prompt,
                print_divider,
                print_header,
                print_subtitle,
            )

            print_header("Orchestrator")
            print_subtitle("No initialized project found in current directory.\n")
            print("    [\033[93m1\033[0m] Initialize new project (Wizard)")

            if projects:
                print("\n  \033[1;36m--- EXISTING PROJECTS ---\033[0m")
                for i, p in enumerate(projects, 2):
                    print(f"    [\033[93m{i}\033[0m] Open {p['name']}")
                    print(f"        \033[90m{p['root']}\033[0m")

            print("\n    [\033[1;91mQ\033[0m] Quit\n")
            print_divider()

            while True:
                sys.stdout.write("\r")
                print_choice_prompt("Choice:", "(index or letter)")
                if len(projects) > 8:
                    try:
                        choice = input().strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        print()
                        return 0
                else:
                    choice = get_key().strip().lower()
                clear_choice_placeholder()
                
                if choice == "q":
                    print()
                    return 0
                if choice in {"1", "w"}:
                    print()
                    argv = ["wizard"]
                    break
                if choice.isdigit():
                    idx = int(choice) - 2
                    if 0 <= idx < len(projects):
                        proj_root = Path(projects[idx]["root"])
                        if not proj_root.exists() or not proj_root.is_dir():
                            print(f"\n\033[1;91mProject path no longer exists: {proj_root}\033[0m")
                            recent = load_recent_projects(prune_missing=True)
                            projects = recent.get("projects", [])
                            continue
                        os.environ["ORCHESTRATOR_PROJECT_ROOT"] = str(proj_root)
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
    init_parser.add_argument("--build-command", help="Build command for non-Xcode projects")
    init_parser.add_argument("--test-command", help="Test command for non-Xcode projects")
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
    wizard_parser.add_argument("--build-command", help="Build command for non-Xcode projects")
    wizard_parser.add_argument("--test-command", help="Test command for non-Xcode projects")
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
    wizard_parser.add_argument("--provisioning-profile-specifier")
    wizard_parser.add_argument("--asc-key-id")
    wizard_parser.add_argument("--asc-issuer-id")
    wizard_parser.add_argument("--asc-key-path")
    wizard_parser.add_argument("--verify", action="store_true", help="Run setup and config checks before exiting")
    wizard_parser.add_argument("--install-workers", action="store_true", help="Run worker install/check for SSH machines added by this wizard")
    wizard_parser.add_argument("--non-interactive", action="store_true")

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
    subparsers.add_parser("projects", help="List remembered projects")
    use_parser = subparsers.add_parser("use", help="Select active project")
    use_parser.add_argument("project", help="Recent project name or project root path")
    remove_parser = subparsers.add_parser("remove", help="Remove a project from remembered projects")
    remove_parser.add_argument("project", help="Recent project name or project root path")
    subparsers.add_parser("prune", help="Prune missing projects from remembered projects")

    update_parser = subparsers.add_parser("update")
    update_parser.add_argument("--fleet", action="store_true", help="Update all enabled remote workers in the fleet")
    update_parser.add_argument("--project", help="Recent project name or project root path (required for --fleet)")

    distribute_parser = subparsers.add_parser("distribute", help="Quickly build and distribute current project to Firebase")
    distribute_parser.add_argument("--project", help="Recent project name or project root path")
    distribute_parser.add_argument("--notes", help="Release notes for this build")

    fix_parser = subparsers.add_parser("fix", help="Fast 1-line bug fix / feedback for active or new job")
    fix_parser.add_argument("feedback", help="Description of what is broken or what to fix")
    fix_parser.add_argument("--job", help="Specific job ID or job JSON file path")
    fix_parser.add_argument("--project", help="Recent project name or project root path")
    fix_parser.add_argument("--free", action="store_true", help="Restrict execution to free models only (cost_factor == 0.0)")

    ui_parser = subparsers.add_parser("ui", help="Open the local web interface")
    ui_parser.add_argument("--project", help="Recent project name or project root path")
    ui_parser.add_argument("--host", default="127.0.0.1",
                           help="Interface to bind (default 127.0.0.1; a Tailscale IP to reach it from a phone)")
    ui_parser.add_argument("--port", type=int, default=8765)
    ui_parser.add_argument("--no-open", action="store_true", help="Don't open a browser")

    logs_parser = subparsers.add_parser(
        "logs",
        help="App runtime logs from the central log store (setup | sessions | pull | tail)",
        description="Run 'orchestrator logs <action> --help' for options.",
    )
    logs_parser.add_argument("--project", help="Recent project name or project root path")
    logs_parser.add_argument("logs_args", nargs=argparse.REMAINDER,
                             help="setup | sessions | pull [--latest|--session ID] | tail")

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
    if args.command == "distribute":
        if args.project and apply_project_env(args.project):
            return 1
        if getattr(args, "notes", None):
            os.environ["DISTRIBUTION_RELEASE_NOTES"] = args.notes
        return run_script("smoke_test_delivery.py", [])
    if args.command == "fix":
        return fix_command(args)
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
    if args.command == "remove":
        return remove_project_command(args.project)
    if args.command == "prune":
        return prune_projects_command()
    if args.command == "update":
        if args.fleet and args.project and apply_project_env(args.project):
            return 1
        return update_command(args)
    if args.command == "ui":
        if args.project and apply_project_env(args.project):
            return 1
        from orchestrator.web.server import main as ui_main
        ui_args = ["--host", args.host, "--port", str(args.port)] + (["--no-open"] if args.no_open else [])
        return ui_main(ui_args)
    if args.command == "logs":
        if args.project and apply_project_env(args.project):
            return 1
        return run_script("cloud_logs.py", args.logs_args or ["--help"])
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
