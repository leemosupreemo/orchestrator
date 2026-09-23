from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .contract import INPUT_FIELDS, Operation


_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,127}$")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PACKAGE = Path(__file__).resolve().parents[1]


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
    catalog = _read(_PACKAGE / "config" / "models.json", {"models": []})["models"]
    if "allowed_models" in inputs:
        selected = inputs["allowed_models"]
        ids = {model["id"] for model in catalog}
        if not isinstance(selected, list) or not selected or any(model not in ids for model in selected):
            raise ValueError("Choose valid models")
        settings["web_allowed_models"] = selected
        _write(settings_path, settings)
    return {"models": catalog, "selected": settings.get("web_allowed_models", [])}


def _keys(root: Path) -> dict[str, Any]:
    settings = _read(_settings_path(root), {})
    keys = {"openai": "openai_api_key", "anthropic": "anthropic_api_key", "gemini": "gemini_api_key"}
    return {"configured": {name: bool(settings.get(field) or os.environ.get(field.upper())) for name, field in keys.items()},
            "setup": "Enter API keys or complete provider CLI sign-in on the Mac running Orchestrator."}


def _fleet(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    path = root / ".orchestrator" / "config" / "machines.json"
    data = _read(path, {"machines": []})
    action = inputs.get("action", "list")
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
    elif action != "list":
        raise ValueError("Unknown fleet action")
    return {"machines": data["machines"]}


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
    elif action == "provider":
        provider = inputs.get("provider")
        if provider not in {"gmail", "smtp", "none"}:
            raise ValueError("Invalid notification provider")
        settings["notification_provider"] = provider
        _write(path, settings)
    elif action != "list":
        raise ValueError("Unknown email action")
    return {"notification_emails": emails, "notification_provider": settings.get("notification_provider")}


def _docs(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    docs = root / "docs"
    name = inputs.get("name")
    if name is None:
        return {"documents": [p.name for p in sorted(docs.glob("*.md"))]}
    if not isinstance(name, str) or not _NAME.fullmatch(name) or not name.endswith(".md"):
        raise ValueError("Invalid document name")
    path = docs / name
    if not path.is_file():
        raise ValueError("Unknown document")
    return {"name": name, "content": path.read_text(encoding="utf-8")}


def _instructions(root: Path, inputs: dict[str, Any]) -> dict[str, Any]:
    directory = root / ".orchestrator" / "prompts"
    directory.mkdir(parents=True, exist_ok=True)
    action = inputs.get("action", "list")
    if action == "list":
        return {"files": [p.name for p in sorted(directory.glob("*.md"))]}
    name = inputs.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name) or not name.endswith(".md"):
        raise ValueError("Invalid instruction file")
    path = directory / name
    if action == "read":
        return {"name": name, "content": path.read_text(encoding="utf-8")}
    if action == "write":
        content = inputs.get("content")
        if not isinstance(content, str) or len(content) > 65536:
            raise ValueError("Invalid instruction content")
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
    commands = {
        Operation.TOOL_AUDIT: [sys.executable, "-m", "orchestrator", "check", "--project", str(root)],
        Operation.TOOL_SELF_TESTS: [sys.executable, "-m", "unittest", "discover", "tests"],
        Operation.TOOL_UPDATE: [sys.executable, "-m", "orchestrator", "update", *(["--fleet"] if inputs.get("fleet") else [])],
    }
    result = subprocess.run(commands[operation], cwd=root, env={**os.environ, "ORCHESTRATOR_PROJECT_ROOT": str(root)},
                            capture_output=True, text=True, timeout=1800, check=False)
    return {"exit_code": result.returncode, "output": (result.stdout + result.stderr)[-20000:]}


def run_tool(root: Path, operation: Operation, inputs: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(inputs, dict) or set(inputs) - INPUT_FIELDS.get(operation, frozenset()):
        raise ValueError("Unexpected tool input")
    if operation == Operation.TOOL_MODELS:
        return _models(root, inputs)
    if operation == Operation.TOOL_KEYS:
        return _keys(root)
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
        return {"configured": bool(config.get("firebase_distribution")), "delivery_provider": config.get("delivery_provider"),
                "setup": "Run the Orchestrator wizard on the Mac to configure signing and Firebase App Distribution."}
    if operation == Operation.TOOL_PROJECT:
        return {"current": str(root), "change": "Select another configured project in the web project picker."}
    if operation in {Operation.TOOL_AUDIT, Operation.TOOL_SELF_TESTS, Operation.TOOL_UPDATE}:
        return _run_local(root, operation, inputs)
    raise ValueError("Unknown tool operation")
