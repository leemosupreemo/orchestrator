"""Suggested test setup: what a project already has for testing, offered as config to accept.

Setup finds the main build and test command for common stacks. It didn't notice tests that live elsewhere, like an
xUnit project next to a Unity client, or checks that need a Docker server. This reads the repository (never runs
anything) and proposes:

- a main `test_command` when none is set: the dotnet test projects it finds;
- extra `test_commands` from scripts named like tests or checks (`tools/test-server.py`), always in the `manual` tier,
  with `requires: ["docker"]` when the script talks to Docker. A script that looks like it talks to a running server is
  flagged, and is never part of "accept all": a job must not run against your live server;
- notes, such as a compose file with no server test, or Unity without its test framework.

`suggest` is read-only. `apply` returns the project.json changes for the accepted ids.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from orchestrator import test_commands

SKIP_DIRS = {".git", "node_modules", "bin", "obj", "Library", "Temp", "Logs", "Packages", "PackageCache", ".venv", "venv",
             "build", "dist", ".orchestrator", "DerivedData", "Pods", "__pycache__", ".gradle", "target", "TestResults"}
MAX_FILES = 40_000
MAX_PROJECTS = 5
MAX_CANDIDATES = 8
SCRIPT_DIRS = {"tools", "scripts", "script", "ci", "bin"}  # not tests/: those are suites, run through their own runner
SCRIPT_NAME = re.compile(r"^(test|check|verify|smoke|validate)[-_.a-z0-9]*\.(py|sh|js|mjs)$", re.I)
TEST_PACKAGE = re.compile(r'Include="(Microsoft\.NET\.Test\.Sdk|xunit[A-Za-z.]*|NUnit[A-Za-z.0-9]*|MSTest[A-Za-z.]*)"', re.I)
COMPOSE_FILE = re.compile(r"^(docker-)?compose([.-][\w.-]+)?\.ya?ml$", re.I)
CONTAINER_NAME = re.compile(r"^\s+container_name:\s*[\"']?([\w.-]+)", re.M)
# A script that does one of these may be talking to a server that is already running, which a job must not touch.
TALKS_TO_SERVER = re.compile(r"(127\.0\.0\.1|localhost|--port|\bsocket\.|http://|baseline|--live|\blive\b)", re.I)
TALKS_TO_DOCKER = re.compile(r"\bdocker\b", re.I)
RUNNERS = {".py": "python3", ".sh": "bash", ".js": "node", ".mjs": "node"}


def _files(root: Path):
    """Every file under the project, skipping build output, packages and dependency folders."""
    seen = 0
    for folder, dirs, names in os.walk(root):
        skip = SKIP_DIRS | ({"Assets", "StreamingAssets"} if "ProjectSettings" in dirs else set())  # a Unity project's assets: huge, no test projects
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith(".")
                         and not (Path(folder) / d / ".git").exists())  # another repository cloned inside this one isn't this project
        for name in sorted(names):
            seen += 1
            if seen > MAX_FILES:
                return
            yield Path(folder) / name


def dotnet_test_projects(root: Path) -> list[str]:
    """Project files (relative paths) that reference a .NET test framework."""
    found = []
    for path in _files(root):
        if path.suffix.lower() not in (".csproj", ".fsproj"):
            continue
        try:
            if TEST_PACKAGE.search(path.read_text(encoding="utf-8", errors="ignore")[:20_000]):
                found.append(path.relative_to(root).as_posix())
        except OSError:
            continue
    return found


def dotnet_test_command(root: Path) -> str | None:
    """`dotnet test` for the test projects found (up to five, in one command). None when there are none."""
    projects = dotnet_test_projects(root)[:MAX_PROJECTS]
    if not projects:
        return None
    return " && ".join(f'dotnet test "{p}" --nologo' for p in projects)


def compose_files(root: Path) -> list[dict[str, Any]]:
    """Docker compose files, with the container names they fix (a name that isn't fixed can't be required)."""
    found = []
    for path in _files(root):
        if COMPOSE_FILE.match(path.name):
            try:
                names = CONTAINER_NAME.findall(path.read_text(encoding="utf-8", errors="ignore")[:200_000])
            except OSError:
                names = []
            found.append({"path": path.relative_to(root).as_posix(), "containers": names})
    return found


def _name_for(path: Path, taken: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", re.sub(r"^(test|check|verify|smoke|validate)[-_.]*", r"\1-", path.stem)).strip("-") or "check"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    taken.add(name)
    return name


def script_candidates(root: Path, configured: set[str]) -> list[dict[str, Any]]:
    """Scripts named like tests or checks in tools/scripts/tests folders, as `manual` test command proposals."""
    out: list[dict[str, Any]] = []
    taken: set[str] = set()
    for path in _files(root):
        rel = path.relative_to(root)
        if len(rel.parts) < 2 or rel.parts[0].lower() not in SCRIPT_DIRS or not SCRIPT_NAME.match(path.name):
            continue
        if path.suffix.lower() not in RUNNERS or rel.as_posix() in configured:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:100_000]
        except OSError:
            continue
        reasons = []
        if TALKS_TO_SERVER.search(text):
            reasons.append("it looks like it connects to a server that may already be running")
        docker = bool(TALKS_TO_DOCKER.search(text))
        requires = ["docker"] if docker else []
        out.append({"id": _name_for(path, taken), "command": f"{RUNNERS[path.suffix.lower()]} {rel.as_posix()}",
                    "source": rel.as_posix(), "tier": "manual", "requires": requires, "risky": bool(reasons),
                    "why": f"{rel.as_posix()} is named like a test or check" + (" and uses Docker" if docker else ""),
                    "warning": reasons[0] if reasons else ""})
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def _mentioned(config: dict[str, Any]) -> set[str]:
    """Scripts the project already runs, so they aren't suggested again."""
    text = " ".join([str(config.get("test_command") or ""), str(config.get("build_command") or ""),
                     *[str(c.get("command", "")) for c in config.get("test_commands") or [] if isinstance(c, dict)]])
    return {m for m in re.findall(r"[\w./-]+\.(?:py|sh|js|mjs)", text)}


def suggest(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """What to offer for this project given its current project.json (as a dict). Read-only."""
    configured = _mentioned(config)
    main = None
    if not str(config.get("test_command") or "").strip():
        command = dotnet_test_command(root)
        if command:
            main = {"id": "test_command", "command": command,
                    "why": f"{len(dotnet_test_projects(root))} .NET test project(s) found, with no main test command set"}
    have = {c.get("name") for c in config.get("test_commands") or [] if isinstance(c, dict)}
    commands = [c for c in script_candidates(root, configured) if c["id"] not in have]
    notes = []
    compose = compose_files(root)
    if compose and not any("docker" in c["requires"] for c in commands) and not any(
            "docker" in (c.get("requires") or []) for c in config.get("test_commands") or [] if isinstance(c, dict)):
        notes.append(f"Docker compose found ({compose[0]['path']}) but no test for it. A server test would be a manual "
                     "test command that needs docker; an AI job can write one.")
    from orchestrator.stack_detection import detect_project_stack
    stack = detect_project_stack(root)
    if stack.framework == "unity" and not str(config.get("test_command") or "").strip() and not main \
            and not dotnet_test_projects(root):
        notes.append("This Unity project has no Unity Test Framework package and no other tests were found. "
                     "An AI job can add tests.")
    return {"test_command": main, "commands": commands, "notes": notes, "compose": compose}


def has_suggestions(found: dict[str, Any]) -> bool:
    return bool(found["test_command"] or found["commands"])


def apply(root: Path, config: dict[str, Any], accept: list[str]) -> tuple[dict[str, Any], list[str]]:
    """(project.json changes, problems) for the accepted ids: `test_command` and/or command names. "all" means the main
    command and every command that isn't flagged as risky; a flagged one has to be named."""
    found = suggest(root, config)
    wanted = set(accept)
    changes: dict[str, Any] = {}
    problems: list[str] = []
    known = {"test_command", "all", *(c["id"] for c in found["commands"])}
    problems += [f"Nothing is suggested under the name {name}." for name in sorted(wanted - known)]
    if found["test_command"] and wanted & {"test_command", "all"}:
        changes["test_command"] = found["test_command"]["command"]
    items = [dict(c) for c in config.get("test_commands") or [] if isinstance(c, dict)]
    for c in found["commands"]:
        if c["id"] in wanted or ("all" in wanted and not c["risky"]):
            entry = {"name": c["id"], "command": c["command"], "tier": "manual"}
            if c["requires"]:
                entry["requires"] = c["requires"]
            items.append(entry)
    if len(items) != len(config.get("test_commands") or []):
        errors = test_commands.parse(items, root)[1]
        problems += errors
        changes["test_commands"] = items
    return changes, problems
