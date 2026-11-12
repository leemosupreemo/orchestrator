"""Which test frameworks a project uses, grouped, and whether each group runs on every job or by hand.

`discover_test_suites` (scripts/dev_console.py) finds suites file by file. This groups them by framework and project
folder (xUnit in tests/Protocol.Tests, unittest in tests/tools, ...), says in plain words what each framework is, and
works out from project.json whether a group is part of the test run after every job (`test_command` or a per-job entry
in `test_commands`), runs by hand (a manual or nightly entry), or isn't set up. `plan` returns the project.json changes
that move a group between "every job" and "by hand".

Only groups with a command of their own can be moved: .NET test projects (xUnit, NUnit, MSTest) and Python test folders
(unittest, pytest). Other frameworks are listed, but they run through the project's main test command.

Pure functions: the caller passes the suites and the config as plain data.
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

MAX_NAMES_PER_SUITE = 300
NO_AUTOMATIC_TESTS = 'echo "No automatic tests are selected. Choose them on the Tests page."'

FRAMEWORK_INFO = {
    "xUnit": "A C# testing library. Tests call your code directly, so they are usually unit tests.",
    "NUnit": "A C# testing library. Tests call your code directly, so they are usually unit tests.",
    "MSTest": "Microsoft's C# testing library. Tests call your code directly, so they are usually unit tests.",
    ".NET tests": "C# tests run with dotnet test. Tests like these are usually unit tests.",
    "unittest": "Python's built-in testing library. Tests are usually unit tests.",
    "pytest": "A popular Python testing library. Tests are usually unit tests.",
    "Unity Test Framework": "Unity's own test runner. Tests run inside the Unity Editor, so they are slower to start.",
    "XCTest / Swift Testing": "Apple's testing libraries. They run through Xcode.",
    "JUnit": "The standard Java and Kotlin testing library.",
    "Go testing": "Go's built-in testing package.",
    "Rust tests": "Rust's built-in test runner (cargo test).",
    "JavaScript tests": "JavaScript or TypeScript tests (Jest, Vitest or similar).",
}
LANGUAGE_FRAMEWORK = {"swift": "XCTest / Swift Testing", "kotlin": "JUnit", "java": "JUnit", "go": "Go testing",
                      "rust": "Rust tests", "javascript": "JavaScript tests", "typescript": "JavaScript tests"}
DOTNET_LIBRARIES = (("xunit", "xUnit"), ("nunit", "NUnit"), ("mstest", "MSTest"))


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "tests"


def split_chain(command: str) -> list[str]:
    """A command line as its `&&`-joined parts."""
    return [part.strip() for part in re.split(r"\s+&&\s+", (command or "").strip()) if part.strip()]


def _token(text: str, path: str) -> bool:
    """`path` appears in `text` as a whole word: followed by a space, a quote or the end, not by `/` or more name."""
    return bool(re.search(r"(?<![\w./-])" + re.escape(path) + r"""(?=\s|$|["'])""", text))


def _csproj_for(root: Path, rel: str) -> str | None:
    folder = (root / rel).parent
    while True:
        projects = sorted(folder.glob("*.csproj"))
        if projects:
            return projects[0].relative_to(root).as_posix()
        if folder == root or root not in folder.parents:
            return None
        folder = folder.parent


def _dotnet_library(root: Path, project: str) -> str:
    try:
        header = (root / project).read_text(encoding="utf-8", errors="ignore")[:20_000].lower()
    except OSError:
        return ".NET tests"
    return next((label for key, label in DOTNET_LIBRARIES if key in header), ".NET tests")


def _python_library(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")[:50_000]
    except OSError:
        return "unittest"
    if re.search(r"^\s*(import|from)\s+pytest\b", text, re.M):
        return "pytest"
    return "unittest"


def outside_nested_repos(root: Path, suites: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The suites that belong to this project: not those in another repository cloned inside it (a submodule, a vendored
    server), whose tests are that project's own."""
    nested: dict[Path, bool] = {}

    def inside(folder: Path) -> bool:
        if folder == root or root not in folder.parents:
            return False
        if folder not in nested:
            nested[folder] = (folder / ".git").exists() or inside(folder.parent)
        return nested[folder]

    return [s for s in suites if not inside((root / str(s.get("rel_path") or "")).parent)]


def build(root: Path, suites: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """(groups, extras by suite path): the groups with their mode, and each suite's group, framework and test names."""
    groups: dict[str, dict[str, Any]] = {}
    per_suite: dict[str, dict[str, Any]] = {}
    for suite in suites:
        if not suite.get("test_count"):
            continue
        rel = Path(str(suite.get("rel_path") or "")).as_posix()
        language = suite.get("language")
        runner = project = None
        command = None
        if language == "csharp" and suite.get("framework") == "dotnet" and (project := _csproj_for(root, rel)):
            framework, folder, runner = _dotnet_library(root, project), str(Path(project).parent.as_posix()), "dotnet"
            command = f'dotnet test "{project}" --nologo'
        elif language == "csharp":
            framework, folder = "Unity Test Framework", ""
        elif language == "python":
            framework = _python_library(Path(suite["path"]))
            folder = Path(rel).parent.as_posix()
            runner = "python"
            command = f"python3 -m pytest {shlex.quote(folder)}" if framework == "pytest" \
                else f"python3 -m unittest discover -s {shlex.quote(folder)}"
        else:
            framework, folder = LANGUAGE_FRAMEWORK.get(str(language), f"{language} tests"), ""
        key = slug(f"{framework}-{folder}") if folder else slug(framework)
        group = groups.setdefault(key, {
            "id": key, "framework": framework, "info": FRAMEWORK_INFO.get(framework, ""), "language": language,
            "folder": folder, "project": project, "runner": runner, "command": command, "toggle": bool(command),
            "tests": 0, "suites": 0})
        group["tests"] += suite["test_count"]
        group["suites"] += 1
        per_suite[rel] = {"group": key, "framework": framework, "funcs": list(suite.get("test_funcs") or [])[:MAX_NAMES_PER_SUITE]}
    out = sorted(groups.values(), key=lambda g: (not g["toggle"], g["framework"], g["folder"]))
    for group in out:
        group.update(state_of(group, config))
    return out, per_suite


def _matches(group: dict[str, Any], command: str) -> bool:
    """Whether one command line is the run of this group."""
    if not group.get("command"):
        return False
    if command.strip() == group["command"]:
        return True
    if group["runner"] == "dotnet":
        return "dotnet test" in command and (_token(command, group["project"]) or _token(command, group["folder"]))
    return ("unittest" in command or "pytest" in command) and _token(command, group["folder"])


def state_of(group: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """`mode`: per-job, manual, nightly or off (not in project.json); `where`: main or extra; `extra`: the entry's name."""
    if not group["toggle"]:
        return {"mode": None, "where": None, "extra": None}
    if any(_matches(group, part) for part in split_chain(str(config.get("test_command") or ""))):
        return {"mode": "per-job", "where": "main", "extra": None}
    for entry in config.get("test_commands") or []:
        if isinstance(entry, dict) and _matches(group, str(entry.get("command") or "")):
            tier = entry.get("tier") or "per-job"
            return {"mode": tier, "where": "extra", "extra": entry.get("name")}
    return {"mode": "off", "where": None, "extra": None}


def _unique_name(base: str, items: list[dict[str, Any]]) -> str:
    taken = {i.get("name") for i in items}
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def plan(config: dict[str, Any], group: dict[str, Any], mode: str) -> dict[str, Any]:
    """The project.json changes that make `group` run on every job (`per-job`) or by hand (`manual`). Empty if nothing
    changes. A group inside the main test command moves out of it into its own entry, and the other parts stay."""
    if not group.get("toggle") or mode not in ("per-job", "manual"):
        return {}
    main = str(config.get("test_command") or "").strip()
    parts = split_chain(main)
    items = [dict(e) for e in config.get("test_commands") or [] if isinstance(e, dict)]
    in_main = next((i for i, p in enumerate(parts) if _matches(group, p)), None)
    in_extra = next((i for i, e in enumerate(items) if _matches(group, str(e.get("command") or ""))), None)
    new_main = main
    if mode == "per-job":
        if in_main is not None:
            return {}
        plain = in_extra is not None and set(items[in_extra]) <= {"name", "command", "tier"}  # nothing a main command would lose
        if in_extra is not None and plain and (not main or main == NO_AUTOMATIC_TESTS):
            new_main = str(items.pop(in_extra)["command"])  # takes the empty main command's place
        elif in_extra is not None:
            items[in_extra].pop("tier", None)
        elif not main or main == NO_AUTOMATIC_TESTS:
            new_main = group["command"]
        else:
            items.append({"name": _unique_name(group["id"], items), "command": group["command"]})
    else:
        if in_main is not None:
            moved = parts.pop(in_main)
            new_main = " && ".join(parts) or NO_AUTOMATIC_TESTS
            if in_extra is None:
                items.append({"name": _unique_name(group["id"], items), "command": moved, "tier": "manual"})
            else:
                items[in_extra]["tier"] = "manual"
        elif in_extra is not None:
            items[in_extra]["tier"] = "manual"
        else:
            items.append({"name": _unique_name(group["id"], items), "command": group["command"], "tier": "manual"})
    changes: dict[str, Any] = {}
    if new_main != main:
        changes["test_command"] = new_main
    if items != [e for e in config.get("test_commands") or [] if isinstance(e, dict)]:
        changes["test_commands"] = items or None
    return changes


def plan_all(config: dict[str, Any], groups: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    """`plan` for every group that can be moved, one after another so each sees the last one's changes."""
    current = dict(config)
    total: dict[str, Any] = {}
    for group in groups:
        if not group.get("toggle"):
            continue
        fresh = state_of(group, current)
        changes = plan(current, {**group, **fresh}, mode)
        for key, value in changes.items():
            current[key] = value
            total[key] = value
    return total
