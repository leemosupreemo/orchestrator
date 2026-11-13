"""Which test frameworks a project uses, grouped, and whether each group runs on every job or by hand.

`discover_test_suites` (scripts/dev_console.py) finds suites file by file. This groups them by framework and project
folder (xUnit in tests/Protocol.Tests, unittest in tests/tools, ...), says in plain words what each framework is, and
works out from project.json whether a group is part of the test run after every job (`test_command` or a per-job entry
in `test_commands`), runs by hand (a manual or nightly entry), or isn't set up. `plan` returns the project.json changes
that move a group between "every job" and "by hand".

Only groups with a command of their own can be moved: .NET test projects (xUnit, NUnit, MSTest), Python test folders
(unittest, pytest), Gradle modules (Android unit tests, JVM tests, Android instrumented tests) and JavaScript packages
(Jest, Vitest or the package's own test script). Other frameworks are listed, but they run through the project's main test command.

Pure functions: the caller passes the suites and the config as plain data.
"""
from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

MAX_NAMES_PER_SUITE = 300
MAX_FILES_IN_COMMAND = 40
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
    "Android unit tests": "JUnit tests that run on your computer through Gradle, with no phone or emulator. Usually unit tests.",
    "Android instrumented tests": "Tests that run on an emulator or a phone, so they are slow and one has to be running.",
    "Go testing": "Go's built-in testing package.",
    "Rust tests": "Rust's built-in test runner (cargo test).",
    "JavaScript tests": "JavaScript or TypeScript tests (Jest, Vitest or the package's own test script). Usually unit tests.",
}
LANGUAGE_FRAMEWORK = {"swift": "XCTest / Swift Testing", "kotlin": "JUnit", "java": "JUnit", "go": "Go testing",
                      "rust": "Rust tests", "javascript": "JavaScript tests", "typescript": "JavaScript tests"}
DOTNET_LIBRARIES = (("xunit", "xUnit"), ("nunit", "NUnit"), ("mstest", "MSTest"))


UNITTEST_ISOLATED = ("import sys, unittest; sys.path.insert(0, sys.argv[1]); "
                     "unittest.main(module=None, argv=['unittest'] + sys.argv[2:])")


def python_command(framework: str, folder: str, files: list[str], isolate: bool) -> str:
    """The command for a Python test folder. `unittest discover -s <folder>` and `pytest <folder>` also run every package
    and sub-folder inside it, which is what you want unless another group already covers those: then (`isolate`) the
    command names this folder's own files only. The unittest form keeps both the project root and the folder on the import
    path, as discover does, so tests that import the project's own modules still work."""
    names = sorted(files)
    stems = [f[:-3] for f in names if f.endswith(".py")]
    plain = (f"python3 -m pytest {shlex.quote(folder)}" if framework == "pytest"
             else f"python3 -m unittest discover -s {shlex.quote(folder)}")
    if not isolate or not names or len(names) > MAX_FILES_IN_COMMAND or not all(x.isidentifier() for x in stems):
        return plain
    if framework == "pytest":
        return "python3 -m pytest " + " ".join(shlex.quote(f"{folder}/{f}" if folder != "." else f) for f in names)
    return f"python3 -c {shlex.quote(UNITTEST_ISOLATED)} {shlex.quote(folder)} " + " ".join(stems)


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


SETTINGS_FILES = ("settings.gradle", "settings.gradle.kts")
BUILD_FILES = ("build.gradle", "build.gradle.kts")
NO_TEST_SCRIPT = re.compile(r"no test specified")
WATCHING = re.compile(r"--watch\b|\bwatch\b")


def _gradle_group(root: Path, rel: str) -> dict[str, Any] | None:
    """The Gradle module a test file belongs to, and the command that runs that module's tests of this kind.

    src/test is unit tests (testDebugUnitTest for an Android module, test for a plain JVM one) and src/androidTest is
    instrumented tests (connectedDebugAndroidTest, which needs an emulator or a phone). None when there's no Gradle build."""
    parts = Path(rel).parts
    index = next((i for i, part in enumerate(parts[:-1]) if part == "src" and parts[i + 1].startswith(("test", "androidTest"))), None)
    if index is None:
        return None
    kind = "instrumented" if parts[index + 1].startswith("androidTest") else "unit"
    module = Path(*parts[:index]) if index else Path(".")
    gradle_root = None
    folder = module
    while True:
        if any((root / folder / name).is_file() for name in SETTINGS_FILES):
            gradle_root = folder
            break
        if folder == Path(".") or folder.parent == folder:
            break
        folder = folder.parent
    module_build = next((root / module / n for n in BUILD_FILES if (root / module / n).is_file()), None)
    if gradle_root is None and module_build is None:
        return None
    gradle_root = gradle_root if gradle_root is not None else module
    try:
        build_text = module_build.read_text(encoding="utf-8", errors="ignore")[:200_000] if module_build else ""
    except OSError:
        build_text = ""
    android = "com.android." in build_text
    if kind == "instrumented" and not android:
        return None
    relative = module.relative_to(gradle_root) if gradle_root != Path(".") else module
    path = "" if relative == Path(".") else ":" + ":".join(relative.parts)
    task = ("testDebugUnitTest" if android else "test") if kind == "unit" else "connectedDebugAndroidTest"
    wrapper = (root / gradle_root / "gradlew").is_file()
    root_wrapper = (root / "gradlew").is_file()
    root_text = gradle_root.as_posix()
    if gradle_root == Path("."):
        executable = "./gradlew" if wrapper else "gradle"
        project_flag = ""
    elif wrapper:
        executable, project_flag = f"{shlex.quote(root_text)}/gradlew", f" -p {shlex.quote(root_text)}"
    else:
        executable, project_flag = ("./gradlew" if root_wrapper else "gradle"), f" -p {shlex.quote(root_text)}"
    return {"kind": kind, "android": android, "module": module.as_posix(), "gradle_root": root_text, "gradle_path": path, "task": task,
            "command": f"{executable}{project_flag} {path}:{task}" if path else f"{executable}{project_flag} {task}",
            "framework": "Android instrumented tests" if kind == "instrumented" else ("Android unit tests" if android else "JUnit")}


def _node_group(root: Path, rel: str) -> dict[str, Any] | None:
    """The JavaScript package a test file belongs to, and the command that runs its tests once (never a watcher)."""
    folder = Path(rel).parent
    while True:
        if (root / folder / "package.json").is_file():
            break
        if folder == Path(".") or folder.parent == folder:
            return None
        folder = folder.parent
    try:
        package = json.loads((root / folder / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    package = package if isinstance(package, dict) else {}
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
    script = str(scripts.get("test") or "")
    tool = "vitest" if "vitest" in deps or "vitest" in script else "jest" if "jest" in deps or "jest" in script else None
    usable = bool(script) and not NO_TEST_SCRIPT.search(script)
    if not usable and tool is None:
        return None
    folder_text = folder.as_posix()
    manager = next((m for m, lock in (("pnpm", "pnpm-lock.yaml"), ("yarn", "yarn.lock"), ("bun", "bun.lockb")) if (root / folder / lock).is_file() or (root / lock).is_file()), "npm")
    where = "" if folder == Path(".") else {"npm": " --prefix ", "pnpm": " --dir ", "yarn": " --cwd ", "bun": " --cwd "}[manager] + shlex.quote(folder_text)
    if usable and not (WATCHING.search(script) or script.strip() in ("vitest", "jest")):
        command = f"{manager}{where} test" if manager != "bun" else f"bun run{where} test"
        if "react-scripts test" in script:
            command = "CI=true " + command  # Create React App's runner otherwise waits for keys
    elif tool == "vitest":
        command = f"{manager}{where} exec -- vitest run" if manager == "npm" else f"npx{' --prefix ' + shlex.quote(folder_text) if where else ''} vitest run"
    else:
        command = f"{manager}{where} exec -- jest" if manager == "npm" else f"npx{' --prefix ' + shlex.quote(folder_text) if where else ''} jest"
    return {"folder": folder_text, "tool": tool, "manager": manager, "command": command}


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
        extra: dict[str, Any] = {}
        if language == "csharp" and suite.get("framework") == "dotnet" and (project := _csproj_for(root, rel)):
            framework, folder, runner = _dotnet_library(root, project), str(Path(project).parent.as_posix()), "dotnet"
            command = f'dotnet test "{project}" --nologo'
        elif language == "csharp":
            framework, folder = "Unity Test Framework", ""
        elif language in ("kotlin", "java") and (gradle := _gradle_group(root, rel)):
            framework, folder, runner, project, command = gradle["framework"], gradle["module"], "gradle", gradle["gradle_path"] or ".", gradle["command"]
            extra = {"gradle_kind": gradle["kind"], "gradle_root": gradle["gradle_root"], "gradle_task": gradle["task"], "android": gradle["android"]}
        elif language in ("javascript", "typescript") and (node := _node_group(root, rel)):
            framework, folder, runner, project, command = "JavaScript tests", node["folder"], "node", None, node["command"]
            extra = {"tool": node["tool"], "manager": node["manager"]}
        elif language == "python":
            framework = _python_library(Path(suite["path"]))
            folder = Path(rel).parent.as_posix()
            runner = "python"
            command = None  # set below, once the folder's files are known
        else:
            framework, folder = LANGUAGE_FRAMEWORK.get(str(language), f"{language} tests"), ""
        key = slug(f"{framework}-{folder}") if folder else slug(framework)
        group = groups.setdefault(key, {
            "id": key, "framework": framework, "info": FRAMEWORK_INFO.get(framework, ""), "language": language,
            "folder": folder, "project": project, "runner": runner, "command": command, "toggle": bool(command),
            "tests": 0, "suites": 0, "files": [], **extra})
        if runner == "python":
            name = Path(rel).name
            if name not in group["files"]:
                group["files"].append(name)
        group["tests"] += suite["test_count"]
        group["suites"] += 1
        per_suite[rel] = {"group": key, "framework": framework, "funcs": list(suite.get("test_funcs") or [])[:MAX_NAMES_PER_SUITE]}
    python_folders = [g["folder"] for g in groups.values() if g["runner"] == "python"]
    for group in groups.values():
        if group["runner"] == "python":
            folder = group["folder"]
            group["isolate"] = any(other != folder and (folder == "." or other.startswith(folder + "/")) for other in python_folders)
            group["command"] = python_command(group["framework"], folder, group["files"], group["isolate"])
            group["toggle"] = True
    out = sorted(groups.values(), key=lambda g: (-g["tests"], g["framework"], g["folder"]))  # the main framework first
    for group in out:
        group.update(state_of(group, config))
    return out, per_suite


GRADLE_UNQUALIFIED_TASKS = {"test", "check", "testDebugUnitTest"}


def _gradle_tasks(command: str) -> list[str]:
    """The task names on a Gradle command line (not its options, the executable or the -p folder)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    tasks, skip = [], False
    for token in tokens:
        if skip:
            skip = False
        elif token in ("-p", "--project-dir"):
            skip = True
        elif token.startswith("-") or token.endswith(("gradlew", "gradle")) or "=" in token:
            continue
        else:
            tasks.append(token)
    return tasks


def _matches(group: dict[str, Any], command: str) -> bool:
    """Whether one command line is the run of this group."""
    if not group.get("command"):
        return False
    if command.strip() == group["command"]:
        return True
    if group["runner"] == "dotnet":
        return "dotnet test" in command and (_token(command, group["project"]) or _token(command, group["folder"]))
    if group["runner"] == "gradle":
        if "gradle" not in command:
            return False
        path, task = group["project"] if group["project"] != "." else "", group.get("gradle_task")
        for given in _gradle_tasks(command):
            if given == f"{path}:{task}" or (given == task and not path):
                return True
            if group.get("gradle_kind") == "unit" and given in GRADLE_UNQUALIFIED_TASKS:  # plain `gradlew test` runs every module's
                return True
        return False
    if group["runner"] == "node":
        runs_tests = re.search(r"\b(npm|pnpm|yarn|bun)\b.*\btest\b|\bvitest\b|\bjest\b|react-scripts test", command)
        return bool(runs_tests) and (group["folder"] == "." or _token(command, group["folder"]))
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
