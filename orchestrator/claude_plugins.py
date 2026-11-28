"""Claude Code plugins that can make a headless `claude -p` wait forever.

A plugin installed for one project (`scope: project`) whose folder is inside a macOS-protected folder (Documents, Desktop,
Downloads) is looked up whenever `claude` starts in any git repository. If the program that started `claude` has no
permission for that folder, macOS holds the open until someone answers a privacy question, and a headless job has nobody
to answer it: the call produces no output until the stall limit. Everything here only compares paths. Asking macOS about
the folder itself (an `ls`, a `stat` that reads it) would hang the same way.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROTECTED_FOLDERS = ("Documents", "Desktop", "Downloads")
PLUGINS_FILE = Path(".claude") / "plugins" / "installed_plugins.json"


def _home(home: Path | None) -> Path:
    return home if home is not None else Path.home()


def load_config(home: Path | None = None) -> dict[str, Any]:
    """`installed_plugins.json`, or {} when it's missing, unreadable or not an object."""
    try:
        data = json.loads((_home(home) / PLUGINS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _protected_folder(path_text: str, home: Path) -> tuple[str, str] | None:
    """(folder name, normalized path) when the path is a protected folder or inside one of this home's."""
    text = path_text.strip()
    if not text:
        return None
    path = home / text[2:] if text.startswith("~/") else Path(text)
    for folder in PROTECTED_FOLDERS:
        try:
            path.relative_to(home / folder)
        except ValueError:
            continue
        return folder, str(path)
    return None


def protected_project_plugins(config: Any, home: Path | None = None) -> list[dict[str, str]]:
    """Project-scoped plugins whose project folder is inside one of this home's protected folders."""
    home = _home(home)
    plugins = config.get("plugins") if isinstance(config, dict) else None
    if not isinstance(plugins, dict):
        return []
    found: list[dict[str, str]] = []
    for plugin, entries in plugins.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("scope") != "project":
                continue
            project_path = entry.get("projectPath")
            hit = _protected_folder(project_path, home) if isinstance(project_path, str) else None
            if hit:
                found.append({"plugin": str(plugin), "path": hit[1], "folder": hit[0]})
    return found


def in_git_repository(cwd: Path) -> bool:
    """Is `cwd` inside a git repository or worktree (a `.git` folder or file at or above it)?"""
    path = Path(cwd).absolute()
    return any((p / ".git").exists() for p in (path, *path.parents))


def findings(cwd: Path, home: Path | None = None, platform: str | None = None) -> list[dict[str, str]]:
    """What in the Claude plugin config could hang a `claude` started in `cwd`. Empty when nothing can."""
    if (platform or sys.platform) != "darwin" or not in_git_repository(cwd):
        return []
    return protected_project_plugins(load_config(home), home)


def describe(finding: dict[str, str]) -> str:
    """One finding as a sentence that names the plugin, the folder, what it does and how to fix it."""
    return (f"Claude Code plugin {finding['plugin']} is installed for {finding['path']}, inside your {finding['folder']} folder. "
            f"In a git repository `claude` opens that folder when it starts, and if this app has no macOS permission for "
            f"{finding['folder']} the call waits for a question nobody can answer. Allow {finding['folder']} for the app that "
            f"runs the orchestrator (System Settings > Privacy & Security > Files and Folders), or remove the entry from "
            f"~/.claude/plugins/installed_plugins.json.")


def warnings(cwd: Path, home: Path | None = None, platform: str | None = None) -> list[str]:
    return [describe(f) for f in findings(cwd, home=home, platform=platform)]
