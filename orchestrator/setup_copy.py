"""Copy a project's setup from another computer on the same account.

A computer that is further along in setup publishes a *profile* of what it has set up for a project, and a newer computer
lists those profiles and copies one. Only settings that mean the same thing on any Mac travel: build and test commands,
the code host, model choices, notification emails, custom role prompts. Nothing secret (API keys, tokens, passwords,
signing keys) and nothing tied to one computer's folders (key paths, derived data) ever leaves the computer it is on.

Sign-ins (GitHub CLI, an AI tool, Xcode's licence) belong to each computer and can't be copied, so after a copy the
checklist still shows them as to do, labelled with where they are already done (see `annotate`).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

# project.json: the same on every computer. Leaves out asc_* (App Store Connect keys), *_path values and remote_logs.
PROJECT_KEYS = (
    "project_name", "base_branch", "pr_base_branch", "branch_prefix", "git_remote", "code_host",
    "build_command", "test_command", "backend_test_command", "test_commands", "build_timeout", "test_timeout",
    "test_target", "scheme", "xcode_project", "xcode_workspace", "detected_schemes", "detected_targets",
    "app_bundle_id", "development_team", "signing_style",
    "delivery_provider", "delivery_method", "delivery_platform", "delivery_build_command", "delivery_artifact",
    "firebase_distribution", "firebase_app_id", "firebase_project_id", "firebase_hosting_target",
    "firebase_testers", "firebase_groups", "notification_display_name",
)
# Relative to the project folder, so an absolute one belongs to the other computer.
RELATIVE_ONLY = {"xcode_project", "xcode_workspace"}
# settings.json: choices only. Keys, passwords, webhooks and folder paths stay where they are.
SETTINGS_KEYS = (
    "notification_emails", "notification_provider", "desktop_notifications", "default_models", "global_base_branch",
    "firebase_tester_groups", "firebase_app_id", "firebase_invite_url",
)
PROMPT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,59}\.md")
MAX_PROMPTS = 20
MAX_PROMPT_CHARS = 20_000
MAX_VALUE_CHARS = 4_000


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _plain(value: Any, depth: int = 0) -> bool:
    """Plain data a person would have typed: text, numbers, booleans, and short lists or maps of those."""
    if isinstance(value, (bool, int, float)) or value is None:
        return True
    if isinstance(value, str):
        return len(value) <= MAX_VALUE_CHARS
    if depth >= 3:
        return False
    if isinstance(value, list):
        return len(value) <= 50 and all(_plain(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return len(value) <= 50 and all(isinstance(k, str) and len(k) <= 80 and _plain(v, depth + 1) for k, v in value.items())
    return False


def _pick(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    """The listed keys of `source` whose values are plain data (and, for RELATIVE_ONLY keys, not absolute paths)."""
    if not isinstance(source, dict):
        return {}
    out = {}
    for key in keys:
        value = source.get(key)
        if value is None or not _plain(value):
            continue
        if key in RELATIVE_ONLY and (not isinstance(value, str) or value.startswith(("/", "~"))):
            continue
        out[key] = value
    return out


def export_profile(runtime: Path) -> dict[str, Any]:
    """What this computer shares for a project: whitelisted settings and the role prompts it customised."""
    prompts: dict[str, str] = {}
    folder = runtime / "prompts"
    if folder.is_dir():
        for path in sorted(folder.glob("*.md"))[:MAX_PROMPTS]:
            if not PROMPT_NAME.fullmatch(path.name):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if 0 < len(text.strip()) and len(text) <= MAX_PROMPT_CHARS:
                prompts[path.name] = text
    return {"project": _pick(_read_json(runtime / "project.json"), PROJECT_KEYS),
            "settings": _pick(_read_json(runtime / "config" / "settings.json"), SETTINGS_KEYS),
            "prompts": prompts}


def progress(setup: dict[str, Any]) -> dict[str, Any]:
    """The checklist reduced to what other computers show: how far along, and which steps are done."""
    items = [{"id": i["id"], "title": i["title"], "required": bool(i["required"]), "done": bool(i["done"])}
             for i in setup.get("items", []) if isinstance(i, dict) and i.get("id")]
    return {"required_done": int(setup.get("required_done", 0)), "required_total": int(setup.get("required_total", 0)),
            "complete": bool(setup.get("complete")), "items": items}


def has_config(profile: dict[str, Any]) -> bool:
    return bool(profile.get("project") or profile.get("settings") or profile.get("prompts"))


def with_ahead(sources: list[dict[str, Any]], mine: dict[str, Any]) -> list[dict[str, Any]]:
    """Mark each other computer that is further along than this one, most advanced first. Only computers that have
    something to copy are offered."""
    out = []
    for source in sources:
        if not isinstance(source, dict) or not source.get("has_config"):
            continue
        theirs = source.get("progress") or {}
        ahead = theirs.get("required_done", 0) > mine.get("required_done", 0) or (bool(theirs.get("complete")) and not mine.get("complete"))
        out.append({**source, "ahead": ahead})
    return sorted(out, key=lambda s: (not s["ahead"], -(s.get("progress") or {}).get("required_done", 0), -(s.get("synced_at") or 0)))


def apply_profile(runtime: Path, profile: Any, source_name: str, source_progress: dict[str, Any] | None = None,
                  now: float | None = None) -> dict[str, list[str]]:
    """Write a copied profile into this project's settings. Values are checked again here: they came over the network.
    Copied values replace this computer's own for the same keys; everything else it has set up stays. Returns what was
    written, by group, and remembers where it came from so the checklist can say what is still to do."""
    if not isinstance(profile, dict) or not has_config(profile):
        raise ValueError("That computer hasn't shared anything to copy yet.")
    applied: dict[str, list[str]] = {"project": [], "settings": [], "prompts": []}

    project_path = runtime / "project.json"
    project = _read_json(project_path)
    for key, value in _pick(profile.get("project"), PROJECT_KEYS).items():
        project[key] = value
        applied["project"].append(key)
    if applied["project"]:
        _write_json(project_path, project)

    settings_path = runtime / "config" / "settings.json"
    settings = _read_json(settings_path)
    for key, value in _pick(profile.get("settings"), SETTINGS_KEYS).items():
        settings[key] = value
        applied["settings"].append(key)

    prompts = profile.get("prompts") if isinstance(profile.get("prompts"), dict) else {}
    for name, text in list(prompts.items())[:MAX_PROMPTS]:
        if isinstance(name, str) and PROMPT_NAME.fullmatch(name) and isinstance(text, str) and 0 < len(text.strip()) and len(text) <= MAX_PROMPT_CHARS:
            target = runtime / "prompts" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
            applied["prompts"].append(name)

    ready = [i["id"] for i in (source_progress or {}).get("items", []) if isinstance(i, dict) and i.get("done") and isinstance(i.get("id"), str)]
    settings["setup_copied_from"] = {"name": str(source_name)[:80], "at": now if now is not None else time.time(), "ready": ready[:40]}
    _write_json(settings_path, settings)
    return applied


def annotate(setup: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """After a copy, add what this computer still lacks that the source has done, so the shield is honest about it:
    copied steps are green because they are set up here, and the rest say where they are already done."""
    copied = settings.get("setup_copied_from")
    if not isinstance(copied, dict):
        return setup
    name = str(copied.get("name") or "another computer")
    ready = set(copied.get("ready") or [])
    for item in setup.get("items", []):
        if not item["done"] and item["id"] in ready:
            item["detail"] = f"{item['detail']} (Done on {name}, but sign-ins and installs are per computer.)"
            item["done_on_source"] = True
    setup["copied_from"] = {"name": name, "at": copied.get("at")}
    return setup
