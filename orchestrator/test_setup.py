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

import ast
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
# A script that names an address AND opens a connection, or is called "live", may talk to a server that is already
# running, which a job must not touch. A word like "baseline" or "live" in a comment is not enough.
ADDRESS = re.compile(r"(127\.0\.0\.1|localhost|--port|\bhttps?://|\bport\s*=)", re.I)
NETWORK = re.compile(r"(\bsocket\b|requests\.|urllib|http\.client|urlopen|httpx|aiohttp|websocket|HttpClient|\bcurl\b|\.connect\()", re.I)
LIVE_NAME = re.compile(r"live", re.I)
TEST_METHOD = re.compile(r"\[(Fact|Theory|Test|TestMethod|TestCase)\b")
FRAMEWORKS = (("xunit", "xUnit"), ("nunit", "NUnit"), ("mstest", "MSTest"))
# What each kind of script is, in words for someone who doesn't write tests.
KINDS = {
    "server": ("Live server test", "Connects to a game server that is already running. It is an integration test: "
               "it checks that the real pieces work together, and it can change data on that server."),
    "docker": ("Docker server test", "Needs Docker running. It is an integration test: it starts or talks to a copy of "
               "your server and checks the pieces work together."),
    "data": ("Data check", "Looks over your project's files and data for mistakes, such as missing or inconsistent "
             "entries. It is not a test of the code itself."),
    "script": ("Script check", "A script from your project that checks something. Run it to see what it reports."),
}
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


def describe(path: Path, text: str) -> str:
    """What a script says about itself: its docstring (Python) or its leading comment, first lines, short."""
    lines: list[str] = []
    if path.suffix.lower() == ".py":
        try:
            doc = ast.get_docstring(ast.parse(text))
        except (SyntaxError, ValueError):
            doc = None
        lines = (doc or "").strip().splitlines()[:2]
    else:
        for line in text.splitlines()[:12]:
            stripped = line.strip()
            if stripped.startswith("#!") or not stripped:
                continue
            if stripped[:1] == "#" or stripped[:2] == "//":
                lines.append(stripped.lstrip("#/ ").strip())
            else:
                break
            if len(lines) == 2:
                break
    return " ".join(x for x in lines if x)[:200]


def dotnet_summary(root: Path, projects: list[str]) -> dict[str, Any]:
    """Which test library the projects use and roughly how many tests they hold."""
    libraries: list[str] = []
    tests = 0
    for rel in projects:
        try:
            header = (root / rel).read_text(encoding="utf-8", errors="ignore")[:20_000].lower()
        except OSError:
            continue
        libraries += [label for key, label in FRAMEWORKS if key in header and label not in libraries]
        counted = 0
        for path in _files((root / rel).parent):
            if path.suffix == ".cs" and counted < 2000:
                try:
                    tests += len(TEST_METHOD.findall(path.read_text(encoding="utf-8", errors="ignore")))
                except OSError:
                    pass
                counted += 1
    return {"libraries": libraries, "tests": tests}


def humanize(stem: str) -> str:
    return re.sub(r"[-_.]+", " ", stem).strip().capitalize()


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
        docker = bool(TALKS_TO_DOCKER.search(text))
        risky = bool(LIVE_NAME.search(path.stem)) or bool(ADDRESS.search(text) and NETWORK.search(text))
        stem = path.stem.lower()
        kind = "server" if risky else "docker" if docker else "data" if re.match(r"(check|validate|verify)", stem) else "script"
        label, what = KINDS[kind]
        out.append({"id": _name_for(path, taken), "title": humanize(path.stem), "command": f"{RUNNERS[path.suffix.lower()]} {rel.as_posix()}",
                    "source": rel.as_posix(), "tier": "manual", "requires": ["docker"] if docker else [], "risky": risky,
                    "kind": kind, "kind_label": label, "what": what, "description": describe(path, text),
                    "why": f"{rel.as_posix()} is named like a test or check" + (" and uses Docker" if docker else ""),
                    "warning": "it looks like it connects to a server that may already be running" if risky else ""})
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
            projects = dotnet_test_projects(root)
            info = dotnet_summary(root, projects[:MAX_PROJECTS])
            library = " / ".join(info["libraries"]) or ".NET"
            main = {"id": "test_command", "command": command, "projects": projects[:MAX_PROJECTS], "tests": info["tests"],
                    "framework": library,
                    "why": f"{len(projects)} .NET test project(s) found, with no main test command set",
                    "what": f"Automated tests written with {library}. Tests like these call your code directly and check "
                            "the result, so they are usually unit tests: quick, and they need nothing else running. "
                            "This runs after every job, so a job can't be finished with broken tests."}
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
