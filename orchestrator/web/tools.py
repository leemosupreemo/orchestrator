from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract import INPUT_FIELDS, Operation


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,127}$")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PACKAGE = Path(__file__).resolve().parents[1]
_INSTRUCTION_FILES = {
    "Antigravity CLI": "GEMINI.md", "Claude Code": "CLAUDE.md", "Codex": "AGENTS.md",
    "Copilot": ".github/copilot-instructions.md", "Ollama": "OLLAMA.md",
    "DeepSeek": "DEEPSEEK.md", "OpenCode": "OPENCODE.md", "Qwen": "QWEN.md",
}
_REQUIRED_DOCS = ("docs/architecture.md", "docs/coding-standards.md", "docs/build-test-commands.md", "docs/ai-workflow.md")


def _read(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _settings_path(root: Path) -> Path:
    return root / ".orchestrator" / "config" / "settings.json"


def _models(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    settings_path = _settings_path(root)
    settings = _read(settings_path, {})
    sync_result = None
    if inputs.get("refresh") or inputs.get("live_discovery"):
        sys.path.insert(0, str(_PACKAGE / "scripts"))
        from model_registry import sync_models
        success, message = sync_models(live_discovery=bool(inputs.get("live_discovery")))
        sync_result = {"success": success, "message": message}
    catalog = _read(_PACKAGE / "config" / "models.json", {"models": []})["models"]
    if "allowed_models" in inputs:
        selected = inputs["allowed_models"]
        ids = {model["id"] for model in catalog}
        if not isinstance(selected, list) or not selected or any(model not in ids for model in selected):
            raise ValueError("Choose valid models")
        settings["web_allowed_models"] = selected
        _write(settings_path, settings)
    return {"models": catalog, "selected": settings.get("web_allowed_models", []), "sync": sync_result}


def _keys(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    settings = _read(_settings_path(root), {})
    keys = {"openai": "openai_api_key", "anthropic": "anthropic_api_key", "gemini": "gemini_api_key"}
    action = inputs.get("action", "status")
    if action == "clear":
        for field in keys.values():
            settings.pop(field, None)
        _write(_settings_path(root), settings)
    elif action != "status":
        raise ValueError("Unknown key action")
    checks = {"claude": ["claude", "auth", "status"], "codex": ["codex", "login", "status"],
              "gh": ["gh", "auth", "status"], "opencode": ["opencode", "models"], "ollama": ["ollama", "list"]}
    cli_status = {}
    for name, command in checks.items():
        if not shutil.which(name):
            cli_status[name] = "Not installed"
            continue
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
            cli_status[name] = "Ready" if result.returncode == 0 else "Not ready"
        except (OSError, subprocess.TimeoutExpired):
            cli_status[name] = "Unknown"
    cli_status["gemini"] = "Not installed" if not any(shutil.which(name) for name in ("agy", "antigravity", "gemini")) else (
        "Ready" if any((Path.home() / ".gemini" / filename).is_file() for filename in ("oauth_creds.json", "google_accounts.json")) else "Not ready")
    return {"configured": {name: bool(settings.get(field) or os.environ.get(field.upper())) for name, field in keys.items()},
            "cli_status": cli_status,
            "cli_available": {name: bool(shutil.which(name)) for name in ("agy", "gemini", "claude", "codex", "gh", "opencode", "ollama")},
            "sign_in_commands": {"gemini": "agy", "claude": "claude auth login", "codex": "codex login", "github": "gh auth login", "opencode": "opencode auth login"},
            "setup": "Run provider sign-in or enter fallback API keys in the local Orchestrator CLI on the Mac worker. Credentials never travel through Firestore."}


def _fleet(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    path = root / ".orchestrator" / "config" / "machines.json"
    data = _read(path, {"machines": []})
    action = inputs.get("action", "list")
    settings_path = _settings_path(root)
    settings = _read(settings_path, {})
    if action == "discover":
        sys.path.insert(0, str(_PACKAGE / "scripts"))
        from discover_machines import discover_machine_candidates
        return {"candidates": discover_machine_candidates(), "machines": data["machines"]}
    if action == "probe":
        sys.path.insert(0, str(_PACKAGE / "scripts"))
        from probe_machine import probe_machine
        selected = inputs.get("name")
        machines = [m for m in data["machines"] if not selected or m.get("name") == selected]
        if not machines:
            raise ValueError("Unknown machine")
        return {"probes": [probe_machine({**m, "repo_path": m.get("repo_path", str(root))}) for m in machines]}
    if action == "purge":
        selected = inputs.get("machine_ids") or settings.get("web_allowed_machines") or [m["name"] for m in data["machines"]]
        if not isinstance(selected, list) or any(name not in {m["name"] for m in data["machines"]} for name in selected):
            raise ValueError("Invalid machine selection")
        script = ("import json, sys; sys.path.insert(0, sys.argv[1]); "
                  "from common import purge_zombie_processes; "
                  "print(json.dumps({'purged': purge_zombie_processes(json.loads(sys.argv[2]), silent=True)}))")
        result = subprocess.run([sys.executable, "-c", script, str(_PACKAGE / "scripts"), json.dumps(selected)],
                                cwd=root, env={**os.environ, "ORCHESTRATOR_PROJECT_ROOT": str(root)},
                                capture_output=True, text=True, timeout=300, check=False)
        if result.returncode:
            return {"exit_code": result.returncode, "output": (result.stdout + result.stderr)[-20000:]}
        return json.loads(result.stdout.splitlines()[-1])
    if action in {"enable", "disable"}:
        name = inputs.get("name")
        matches = [m for m in data["machines"] if m.get("name") == name]
        if len(matches) != 1:
            raise ValueError("Unknown machine")
        matches[0]["enabled"] = action == "enable"
        _write(path, data)
    elif action == "add":
        name, target, repo = inputs.get("name"), inputs.get("ssh_target"), inputs.get("repo_path")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or any(m.get("name") == name for m in data["machines"]):
            raise ValueError("Invalid or duplicate machine name")
        if not isinstance(target, str) or not target or any(c.isspace() for c in target):
            raise ValueError("Invalid SSH target")
        if not isinstance(repo, str) or not repo.startswith("/"):
            raise ValueError("Remote repository path must be absolute")
        data["machines"].append({"name": name, "execution_mode": "ssh", "ssh_target": target, "repo_path": repo, "enabled": True})
        _write(path, data)
    elif action == "select":
        selected = inputs.get("machine_ids")
        names = {m.get("name") for m in data["machines"] if m.get("enabled", True)}
        if not isinstance(selected, list) or not 1 <= len(selected) <= 10 or len(set(selected)) != len(selected) or any(name not in names for name in selected):
            raise ValueError("Choose 1–10 enabled machines")
        settings["web_allowed_machines"] = selected
        _write(settings_path, settings)
    elif action == "rename":
        name, new_name = inputs.get("name"), inputs.get("new_name")
        if not isinstance(new_name, str) or not _NAME.fullmatch(new_name) or any(m.get("name") == new_name for m in data["machines"]):
            raise ValueError("Invalid or duplicate new machine name")
        matches = [m for m in data["machines"] if m.get("name") == name]
        if len(matches) != 1:
            raise ValueError("Unknown machine")
        matches[0]["name"] = new_name
        settings["web_allowed_machines"] = [new_name if m == name else m for m in settings.get("web_allowed_machines", [])]
        _write(path, data)
        _write(settings_path, settings)
    elif action == "remove":
        name = inputs.get("name")
        if len(data["machines"]) < 2 or not any(m.get("name") == name for m in data["machines"]):
            raise ValueError("Cannot remove the last or an unknown machine")
        data["machines"] = [m for m in data["machines"] if m.get("name") != name]
        settings["web_allowed_machines"] = [m for m in settings.get("web_allowed_machines", []) if m != name]
        _write(path, data)
        _write(settings_path, settings)
    elif action != "list":
        raise ValueError("Unknown fleet action")
    return {"machines": data["machines"], "selected": settings.get("web_allowed_machines", [])}


def _email_settings(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    path = _settings_path(root)
    settings = _read(path, {})
    emails = settings.get("notification_emails", [])
    action = inputs.get("action", "list")
    if action in {"add", "remove"}:
        email = inputs.get("email")
        if not isinstance(email, str) or not _EMAIL.fullmatch(email):
            raise ValueError("Invalid email address")
        emails = [e for e in emails if e != email]
        if action == "add":
            emails.append(email)
        settings["notification_emails"] = emails
        _write(path, settings)
    elif action == "import":
        content = inputs.get("content")
        if not isinstance(content, str) or len(content) > 20000:
            raise ValueError("Paste CSV or text under 20 KB")
        found = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", content)
        emails = list(dict.fromkeys([*emails, *(email for email in found if _EMAIL.fullmatch(email))]))
        settings["notification_emails"] = emails
        _write(path, settings)
    elif action == "provider":
        provider = inputs.get("provider")
        if provider not in {"gmail", "resend"}:
            raise ValueError("Invalid notification provider")
        sender = inputs.get("sender_email")
        display = inputs.get("display_name")
        if sender is not None and (not isinstance(sender, str) or not _EMAIL.fullmatch(sender)):
            raise ValueError("Invalid sender email")
        if display is not None and (not isinstance(display, str) or len(display) > 100):
            raise ValueError("Invalid display name")
        settings["notification_provider"] = provider
        if sender:
            settings["smtp_email" if provider == "gmail" else "resend_from_email"] = sender
        if display and provider == "resend":
            settings["resend_display_name"] = display
        _write(path, settings)
    elif action == "test":
        if not emails:
            raise ValueError("Add a recipient before sending a test")
        provider = settings.get("notification_provider", "gmail")
        result = subprocess.run([sys.executable, str(_PACKAGE / "scripts" / "notify.py"), f"Test Notification ({provider})",
                                 f"This is a test message from AI Orchestrator using {provider}.", "test-job-id"],
                                cwd=root, env={**os.environ, "ORCHESTRATOR_PROJECT_ROOT": str(root)},
                                capture_output=True, text=True, timeout=120, check=False)
        return {"exit_code": result.returncode, "output": (result.stdout + result.stderr)[-20000:]}
    elif action != "list":
        raise ValueError("Unknown email action")
    return {"notification_emails": emails, "notification_provider": settings.get("notification_provider", "gmail"),
            "sender_email": settings.get("resend_from_email" if settings.get("notification_provider") == "resend" else "smtp_email", ""),
            "display_name": settings.get("resend_display_name", ""),
            "sender_credential_configured": bool(settings.get("resend_api_key" if settings.get("notification_provider") == "resend" else "smtp_password"))}


def _docs(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    docs = root / "docs"
    help_docs = _PACKAGE.parent / "docs"
    available = {p.name: p for p in sorted(help_docs.glob("*.md"))}
    available.update({f"docs/{p.name}": p for p in sorted(docs.glob("*.md"))})
    available.update({p.name: p for p in sorted(docs.glob("*.md")) if p.name not in available})
    available.update({p.name: p for p in (root / "README.md", root / "AGENTS.md", root / "AI_AGENT_SETUP.md") if p.is_file()})
    name = inputs.get("name")
    if name is None:
        return {"documents": sorted(available)}
    if not isinstance(name, str) or name not in available:
        raise ValueError("Invalid document name")
    return {"name": name, "content": available[name].read_text(encoding="utf-8")}


def _instructions(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    directory = root / ".orchestrator" / "prompts"
    action = inputs.get("action", "list")
    def status() -> dict[str, Any]:
        return {"files": {name: {"path": rel, "status": "missing" if not (root / rel).exists() else
                "valid" if all(link in (root / rel).read_text(encoding="utf-8") for link in _REQUIRED_DOCS) else "misconfigured"}
                for name, rel in _INSTRUCTION_FILES.items()},
                "role_prompts": [p.name for p in sorted(directory.glob("*.md"))]}
    if action == "list":
        result = status()
        result["files"] = {item["path"]: item["status"] for item in result["files"].values()}
        return result
    if action in {"create_missing", "repair"}:
        changed = []
        for label, rel in _INSTRUCTION_FILES.items():
            path = root / rel
            if action == "create_missing" and not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# {rel}\n\nProject instructions for {label}.\n\n## Source of truth\n\n" +
                                "".join(f"- `{link}`\n" for link in _REQUIRED_DOCS), encoding="utf-8")
                changed.append(rel)
            elif action == "repair" and path.exists():
                content = path.read_text(encoding="utf-8")
                missing = [link for link in _REQUIRED_DOCS if link not in content]
                if missing:
                    path.write_text(content + "\n\n## Source of truth\n\n" + "".join(f"- `{link}`\n" for link in missing), encoding="utf-8")
                    changed.append(rel)
        return {"changed": changed, **status()}
    name = inputs.get("name")
    if action in {"copy_role_prompts", "reset_role_prompts"}:
        if action == "copy_role_prompts":
            from orchestrator.cli import copy_prompt_overrides
            copy_prompt_overrides(root, force=False)
            return status()
        import shutil
        if directory.exists():
            shutil.rmtree(directory)
        return {"role_prompts": []}
    if name not in _INSTRUCTION_FILES.values() and not (isinstance(name, str) and _NAME.fullmatch(name) and name.endswith(".md")):
        raise ValueError("Invalid instruction file")
    path = root / name if name in _INSTRUCTION_FILES.values() else directory / name
    if action == "read":
        if not path.is_file():
            raise ValueError("Instruction file does not exist")
        return {"name": name, "content": path.read_text(encoding="utf-8")}
    if action == "write":
        content = inputs.get("content")
        if not isinstance(content, str) or len(content) > 65536:
            raise ValueError("Invalid instruction content")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"name": name, "content": content}
    raise ValueError("Unknown instruction action")


def _archive(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    directory = root / ".orchestrator" / "jobs"
    archived = directory / "archive"
    action = inputs.get("action", "list")
    if action == "list":
        return {"jobs": [p.stem for p in sorted(archived.glob("*.json"))]}
    job_id = inputs.get("job_id")
    if action != "restore" or not isinstance(job_id, str) or not _NAME.fullmatch(job_id):
        raise ValueError("Unknown archive action or job")
    source = archived / f"{job_id}.json"
    target = directory / f"{job_id}.json"
    if not source.is_file() or target.exists():
        raise ValueError("Cannot restore job")
    source.replace(target)
    return {"restored": job_id}


def _run_local(root: Path, operation: Operation, inputs: dict[str, Any]) -> dict[str, Any]:
    suite = inputs.get("suite", "all")
    config = _read(root / ".orchestrator" / "project.json", {})
    target = config.get("test_target")
    if suite in {"workflow", "tooling", "probing"} and (not isinstance(target, str) or not _NAME.fullmatch(target)):
        raise ValueError("Project test target is not configured")
    xcode_suites = {"workflow": "WorkflowTests", "tooling": "LLMTests", "probing": "ProbingTests"}
    suites = {
        **{key: [sys.executable, str(_PACKAGE / "scripts" / "manual_run.py"), "test", "--test-only", f"-only-testing:{target}/{name}"]
           for key, name in xcode_suites.items()},
        "all": [sys.executable, "-m", "unittest", "discover", "tests"],
        "console": [sys.executable, "-m", "unittest", "tests/test_console_smoke.py"],
        "models": [sys.executable, "-m", "unittest", "tests/test_model_registry.py"],
        "setup": [sys.executable, "-m", "unittest", "tests/test_check_setup.py"],
        "resume": [sys.executable, str(_PACKAGE / "scripts" / "smoke_test_cli_workflow.py"), "--scenario", "resume"],
        "delivery": [sys.executable, str(_PACKAGE / "scripts" / "smoke_test_delivery.py"), "--non-interactive"],
        "fleet": [sys.executable, str(_PACKAGE / "scripts" / "fleet_llm_check.py")],
        "fleet_sync": [sys.executable, str(_PACKAGE / "scripts" / "sync_fleet.py")],
        "model_ping": [sys.executable, str(_PACKAGE / "scripts" / "live_check_workflow.py"), "--checks", "models", "--models", "all"],
    }
    if operation == Operation.TOOL_SELF_TESTS and suite not in suites:
        raise ValueError("Unknown self-test suite")
    if operation == Operation.TOOL_UPDATE and inputs.get("action", "status") == "status":
        from orchestrator import __version__
        return {"version": __version__, "package_location": str(_PACKAGE.parent), "git_checkout": (_PACKAGE.parent / ".git").exists(),
                "setup": "Use Run local update to pull and reinstall the Mac worker, or Run fleet update for enabled remote workers."}
    if operation == Operation.TOOL_UPDATE and inputs.get("action") not in {"local", "fleet", None}:
        raise ValueError("Unknown update action")
    if operation == Operation.TOOL_UPDATE and (inputs.get("action") == "fleet" or inputs.get("fleet")):
        command = [sys.executable, "-m", "orchestrator", "update", "--fleet", "--project", str(root)]
    elif operation == Operation.TOOL_UPDATE:
        command = [sys.executable, "-m", "orchestrator", "update"]
    else:
        command = None
    commands = {
        Operation.TOOL_AUDIT: [sys.executable, str(_PACKAGE / "scripts" / "check_setup.py")],
        Operation.TOOL_SELF_TESTS: suites.get(suite, suites["all"]),
        Operation.TOOL_UPDATE: command,
    }
    cwd = _PACKAGE.parent if operation == Operation.TOOL_SELF_TESTS and suite in {"all", "console", "models", "setup"} else root
    result = subprocess.run(commands[operation], cwd=cwd, env={**os.environ, "ORCHESTRATOR_PROJECT_ROOT": str(root)},
                            capture_output=True, text=True, timeout=1800, check=False)
    response = {"exit_code": result.returncode, "output": (result.stdout + result.stderr)[-20000:]}
    if operation == Operation.TOOL_AUDIT:
        machines = _read(root / ".orchestrator" / "config" / "machines.json", {"machines": []})["machines"]
        response["fleet"] = _fleet(root, {"action": "probe"})["probes"] if machines else []
    return response


def run_app_tests(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    config = _read(root / ".orchestrator" / "project.json", {})
    target = config.get("test_target")
    if not isinstance(target, str) or not _NAME.fullmatch(target):
        raise ValueError("Project test target is not configured")
    plans = sorted((root / target / "TestPlans").glob("*.xctestplan"))
    action = inputs.get("action", "list")
    if action == "list":
        return {"plans": [p.name for p in plans], "visual_check": True, "test_target": target}
    if action == "plan":
        selected = inputs.get("plan")
        plan = next((p for p in plans if p.name == selected), None)
        if plan is None:
            raise ValueError("Select a configured Xcode test plan")
        data = json.loads(plan.read_text(encoding="utf-8"))
        flags = []
        for entry in data.get("testTargets", []):
            name = entry.get("target", {}).get("name", target)
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ValueError("Invalid test target in plan")
            selected_tests = entry.get("selectedTests", [])
            if selected_tests:
                flags.extend(f"-only-testing:{name}/{test}" for test in selected_tests)
            else:
                flags.append(f"-only-testing:{name}")
        command = [sys.executable, str(_PACKAGE / "scripts" / "manual_run.py"), "test", "--test-only", " ".join(flags)]
    elif action == "visual":
        command = [sys.executable, str(_PACKAGE / "scripts" / "simulator_visual_check.py")]
    else:
        raise ValueError("Unknown app test action")
    result = subprocess.run(command, cwd=root, env={**os.environ, "ORCHESTRATOR_PROJECT_ROOT": str(root)},
                            capture_output=True, text=True, timeout=3600, check=False)
    return {"exit_code": result.returncode, "output": (result.stdout + result.stderr)[-20000:]}


def run_refresh(root: Path) -> dict[str, Any]:
    from .jobs import list_jobs
    active_jobs = list_jobs(root)
    closed = set()
    if active_jobs:
        result = subprocess.run(["gh", "issue", "list", "--state", "closed", "--limit", "1000", "--json", "number"],
                                cwd=root, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            raise RuntimeError("GitHub issue sync failed: " + result.stderr[-500:])
        closed = {item["number"] for item in json.loads(result.stdout)}
    archived = []
    archive_dir = root / ".orchestrator" / "jobs" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for job in active_jobs:
        if job.get("issue_number") in closed:
            path = root / ".orchestrator" / "jobs" / f"{job['job_id']}.json"
            data = _read(path, {})
            data["status"] = "completed"
            data["completed_at"] = datetime.now(timezone.utc).isoformat()
            _write(path, data)
            path.replace(archive_dir / path.name)
            archived.append(job["job_id"])
    machines = _read(root / ".orchestrator" / "config" / "machines.json", {"machines": []}).get("machines", [])
    availability = {}
    for machine in machines:
        name = machine.get("name")
        if machine.get("execution_mode", "local") == "local":
            availability[name] = True
        elif machine.get("execution_mode") == "ssh":
            target = machine.get("ssh_target")
            if isinstance(target, list):
                target = target[0] if target else None
            if isinstance(target, str) and target:
                probe = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, "true"],
                                       cwd=root, capture_output=True, text=True, timeout=10, check=False)
                availability[name] = probe.returncode == 0
            else:
                availability[name] = False
    return {"archived_jobs": archived, "machine_availability": availability}


def run_tool(root: Path, operation: Operation, inputs: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(inputs, dict) or set(inputs) - INPUT_FIELDS.get(operation, frozenset()):
        raise ValueError("Unexpected tool input")
    if operation == Operation.TOOL_MODELS:
        return _models(root, inputs)
    if operation == Operation.TOOL_KEYS:
        return _keys(root, inputs)
    if operation == Operation.TOOL_FLEET:
        return _fleet(root, inputs)
    if operation == Operation.TOOL_BASE_BRANCH:
        path = _settings_path(root)
        settings = _read(path, {})
        if "branch" in inputs:
            branch = inputs["branch"]
            if not isinstance(branch, str) or not _BRANCH.fullmatch(branch) or ".." in branch or "//" in branch:
                raise ValueError("Invalid branch")
            settings["global_base_branch"] = branch
            _write(path, settings)
        return {"branch": settings.get("global_base_branch", "main")}
    if operation == Operation.TOOL_EMAIL:
        return _email_settings(root, inputs)
    if operation == Operation.TOOL_DOCS:
        return _docs(root, inputs)
    if operation == Operation.TOOL_INSTRUCTIONS:
        return _instructions(root, inputs)
    if operation == Operation.TOOL_ARCHIVE:
        return _archive(root, inputs)
    if operation == Operation.TOOL_DISTRIBUTION:
        config = _read(root / ".orchestrator" / "project.json", {})
        if inputs.get("action") == "smoke_test":
            result = subprocess.run([sys.executable, str(_PACKAGE / "scripts" / "smoke_test_delivery.py"), "--non-interactive"],
                                    cwd=root, capture_output=True, text=True, timeout=1800, check=False)
            return {"exit_code": result.returncode, "output": (result.stdout + result.stderr)[-20000:]}
        if inputs.get("action", "status") != "status":
            raise ValueError("Unknown distribution action")
        app_dir = root / (config.get("project_name") or config.get("scheme") or root.name)
        plist = app_dir / "GoogleService-Info.plist"
        app_id = None
        if plist.is_file():
            try:
                app_id = plistlib.loads(plist.read_bytes()).get("GOOGLE_APP_ID")
            except (ValueError, TypeError):
                pass
        firebase_rc = _read(root / ".firebaserc", {})
        return {"configured": bool(config.get("firebase_distribution")), "delivery_provider": config.get("delivery_provider"),
                "firebase_cli": bool(shutil.which("firebase")), "project_id": firebase_rc.get("projects", {}).get("default"),
                "google_service_info": plist.is_file(), "app_id": app_id, "export_options": (root / "ExportOptions.plist").is_file(),
                "headless_signing": bool(os.environ.get("KEYCHAIN_PASSWORD")), "ci_token": bool(os.environ.get("FIREBASE_TOKEN")),
                "setup": "On the Mac worker run firebase login and orchestrator keychain-setup, then refresh status."}
    if operation == Operation.TOOL_PROJECT:
        return {"current": str(root), "change": "Select another configured project in the web project picker."}
    if operation in {Operation.TOOL_AUDIT, Operation.TOOL_SELF_TESTS, Operation.TOOL_UPDATE}:
        return _run_local(root, operation, inputs)
    raise ValueError("Unknown tool operation")
