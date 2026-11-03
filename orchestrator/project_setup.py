"""Explicit, noninteractive project setup shared by desktop/browser onboarding."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from orchestrator.config_validation import validate_project_config
from orchestrator.project_config import load_project_config, remember_project
from orchestrator.stack_detection import detect_project_stack

FIELDS = ("project_name", "base_branch", "build_command", "test_command", "xcode_project",
          "xcode_workspace", "scheme", "test_target")


def _folder(root: Path) -> Path:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Choose an existing project folder.")
    for path in (root / ".orchestrator", root / ".orchestrator/project.json", root / ".orchestrator/config",
                 root / ".orchestrator/config/machines.json"):
        if not path.resolve().is_relative_to(root):
            raise ValueError("Project configuration must stay inside the selected folder.")
    return root


def _data(root: Path) -> dict:
    path = root / ".orchestrator/project.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("The project configuration must be a JSON object.")
    return data


def inspect_project(root: Path) -> dict[str, Any]:
    root = _folder(root)
    data = _data(root)
    stack = detect_project_stack(root)
    inferred = {"project_name": root.name, "base_branch": "main", "build_command": stack.build_command,
                "test_command": stack.test_command, "xcode_project": stack.xcode_project,
                "xcode_workspace": stack.xcode_workspace, "scheme": stack.scheme, "test_target": stack.test_target}
    inferred.update({k: v for k, v in data.items() if k in FIELDS})
    errors = validate_project_config(load_project_config(root)) if data else []
    return {"root": str(root), "configured": bool(data) and not errors,
            "inferred": inferred, "errors": errors, "stack": stack.display_name}


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".setup-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_project_setup(root: Path, values: dict[str, Any]) -> dict[str, Any]:
    root = _folder(root)
    if not isinstance(values, dict):
        raise ValueError("Settings must be an object.")
    if any(k not in (*FIELDS, "models") for k in values):
        raise ValueError("Unknown setup setting.")
    for key, value in values.items():
        if key != "models" and value is not None and (not isinstance(value, str) or len(value) > 4096 or "\x00" in value):
            raise ValueError("Setup fields must be text.")
    models = values.get("models")
    if models is not None and (not isinstance(models, list) or not models or len(models) > 32
                               or any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in models)):
        raise ValueError("Choose at least one AI model.")
    data = _data(root)
    changes = {k: v for k, v in values.items() if k in FIELDS}
    data.update(changes)
    config = replace(load_project_config(root), **changes)
    errors = validate_project_config(config)
    machines_path = root / ".orchestrator/config/machines.json"
    if not machines_path.exists() and not models:
        errors.append("Choose at least one AI model for this computer.")
    if errors:
        return {"ok": False, "root": str(root), "errors": errors}
    data.setdefault("project_name", config.project_name)
    data.setdefault("base_branch", config.base_branch)
    data.setdefault("pr_base_branch", data["base_branch"])
    _atomic_json(root / ".orchestrator/project.json", data)
    if not machines_path.exists():
        _atomic_json(machines_path, {"version": 1, "machines": [{
            "name": "local", "enabled": True, "execution_mode": "local", "repo_path": str(root),
            "roles": ["planner", "reviewer", "worker", "build", "test"], "models": models,
            "priority": 100, "max_concurrent_jobs": 1, "max_heavy_jobs": 1,
            "supports_xcode": config.uses_xcode, "supports_simulator": config.uses_xcode,
            "supports_backend_tests": True}]})
    for subdir in ("jobs/inbox", "jobs/archive", "logs", "output", "state/machines"):
        path = root / ".orchestrator" / subdir
        if not path.resolve().is_relative_to(root):
            raise ValueError("Runtime directories must stay inside the project.")
        path.mkdir(parents=True, exist_ok=True)
    remember_project(root, config.project_name, active=True)
    return {"ok": True, "root": str(root), "errors": []}
