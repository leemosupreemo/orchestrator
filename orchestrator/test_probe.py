"""Ask a test framework what its tests are, instead of guessing from the files.

Reading source files finds most tests, but misses ones that expand when they run (a parameterized .NET test with
several inputs is one method and many tests: DAoC-Mobile's Protocol tests are 179 by the scan and 389 really), and it
can't tell whether a group can run at all here (a project may need something prepared first). Each framework has a
cheap way to list its tests without running them, and that step also fails when something is missing:

    .NET     dotnet test <project> --list-tests            (builds the project, runs no tests)
    pytest   python3 -m pytest <folder> --collect-only -q
    unittest the loader's discover(), which imports the test files and reports the ones it couldn't import
    Gradle   the group's own command with --dry-run        (Gradle plans the tasks and runs none: it fails when the SDK, JDK or
                                                            build is missing). The tests aren't counted, so the file scan's count stays.
    JS       Node and the installed packages are there    (the tests aren't run or counted)

`probe` runs that for a group and returns {status, tests, names, reason}: status is "ok", "needs-setup" (with the reason the
tool gave) or "error" (it timed out or the output wasn't understood). Results are cached in
<runtime>/output/test-probes.json, and the Tests page shows them. Nothing runs unless someone asks for a check.

The parsers are pure; the runner is injected.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from orchestrator import test_commands

CACHE = Path("output") / "test-probes.json"
TIMEOUT = 600
MAX_NAMES = 2000
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
Run = Callable[[str, Path, int], "tuple[int, str, bool]"]  # command, cwd, timeout -> (exit code, output, timed out)

UNITTEST_SNIPPET = (
    "import json, sys, unittest\n"
    "def walk(s):\n"
    "    for t in s:\n"
    "        yield from walk(t) if isinstance(t, unittest.TestSuite) else [t]\n"
    "names, errors = [], []\n"
    "folder, mods = sys.argv[1], sys.argv[2:]\n"
    "sys.path.insert(0, folder)\n"
    "loader = unittest.TestLoader()\n"
    "suite = loader.loadTestsFromNames(mods) if mods else loader.discover(folder)\n"
    "for t in walk(suite):\n"
    "    if t.__class__.__name__ == '_FailedTest':\n"
    "        errors.append(str(getattr(t, '_exception', t.id())).strip().splitlines()[-1][:300])\n"
    "    else:\n"
    "        names.append(t.id())\n"
    "print(json.dumps({'names': names, 'errors': errors}))\n"
)


def clean(text: str) -> str:
    return ANSI.sub("", text or "")


def _first_error(output: str) -> str:
    """The reason a tool gave for failing: its first error line, without the file path in front."""
    for line in clean(output).splitlines():
        match = re.search(r"\berror\b[^:]*:\s*(.+)$", line, re.I) or re.search(r"^E\s+(\w*Error.*)$", line)
        if match and match.group(1).strip():
            return re.sub(r"\s*\[[^\]]*\.(?:csproj|sln|props)\]\s*$", "", match.group(1).strip())[:300]  # without the trailing [project path]
    lines = [x.strip() for x in clean(output).splitlines() if x.strip()]
    return lines[-1][:300] if lines else "the tool failed without saying why"


def parse_dotnet(output: str) -> list[str] | None:
    """Test names from `dotnet test --list-tests`, or None when the output has no list."""
    lines = clean(output).splitlines()
    for i, line in enumerate(lines):
        if "The following Tests are available" in line:
            return [x.strip() for x in lines[i + 1:] if x.strip()]
    return None


def parse_pytest(output: str) -> list[str] | None:
    """Test ids from `pytest --collect-only -q` (`path::Class::test`), or None when nothing was collected."""
    names = [x.strip() for x in clean(output).splitlines() if "::" in x and not x.startswith((" ", "E", "ERROR"))]
    return names or None


def _gradle_reason(output: str) -> str:
    """Gradle's own explanation: the line under "* What went wrong:", which names a missing SDK, JDK or task directly."""
    lines = [x.rstrip() for x in clean(output).splitlines()]
    for i, line in enumerate(lines):
        if line.strip().startswith("* What went wrong"):
            for follow in lines[i + 1:]:
                if follow.strip():
                    return follow.strip()[:300]
    return _first_error(output)


def _result(status: str, started: float, now: float, **extra: Any) -> dict[str, Any]:
    names = extra.pop("names", None)
    return {"status": status, "seconds": round(now - started, 1), "tests": len(names) if names is not None else None,
            "names": (names or [])[:MAX_NAMES], **extra}


def probe(group: dict[str, Any], root: Path, *, run: Run = test_commands.execute, which: Callable[[str], str | None] = shutil.which,
          clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Check one group with its framework's own listing. Groups for other frameworks come back as "unsupported"."""
    started = clock()
    runner = group.get("runner")
    if runner == "gradle":
        if group["command"].startswith("gradle") and not which("gradle"):
            return _result("needs-setup", started, clock(), reason="Gradle isn't installed, and this project has no gradlew to use instead")
        code, output, timed_out = run(f"{group['command']} --dry-run", root, TIMEOUT)
        if timed_out:
            return _result("error", started, clock(), reason=f"stopped after {TIMEOUT}s")
        if code == 0:
            return _result("ok", started, clock(), how="gradle --dry-run: the build is set up, no tests were run")
        return _result("needs-setup", started, clock(), reason=_gradle_reason(output))
    if runner == "node":
        folder = root / (group.get("folder") or ".")
        if not which("node"):
            return _result("needs-setup", started, clock(), reason="Node.js isn't installed or isn't on PATH")
        if not ((folder / "node_modules").is_dir() or (root / "node_modules").is_dir()):
            return _result("needs-setup", started, clock(), reason="The packages aren't installed here. Run npm install (or your package manager's install) first.")
        return _result("ok", started, clock(), how="Node.js and the packages are here; the tests weren't run")
    if runner == "dotnet":
        if not which("dotnet"):
            return _result("needs-setup", started, clock(), reason="dotnet isn't installed or isn't on PATH")
        code, output, timed_out = run(f'dotnet test "{group["project"]}" --list-tests --nologo', root, TIMEOUT)
        names = parse_dotnet(output) if code == 0 else None
        if timed_out:
            return _result("error", started, clock(), reason=f"stopped after {TIMEOUT}s")
        if names is not None:
            return _result("ok", started, clock(), names=names, how="dotnet test --list-tests")
        return _result("needs-setup", started, clock(), reason=_first_error(output))
    if runner == "python" and group.get("framework") == "pytest":
        files = (group.get("files") or []) if group.get("isolate") else []
        targets = " ".join(shlex.quote(f"{group['folder']}/{f}" if group["folder"] != "." else f) for f in files) or shlex.quote(group["folder"])
        code, output, timed_out = run(f"python3 -m pytest {targets} --collect-only -q -p no:cacheprovider", root, TIMEOUT)
        if timed_out:
            return _result("error", started, clock(), reason=f"stopped after {TIMEOUT}s")
        if "No module named pytest" in output:
            return _result("needs-setup", started, clock(), reason="pytest isn't installed (pip install pytest)")
        names = parse_pytest(output)
        if names is not None and code in (0, 5):
            return _result("ok", started, clock(), names=names, how="pytest --collect-only")
        return _result("needs-setup", started, clock(), reason=_first_error(output))
    if runner == "python":
        files = group.get("files") or []
        stems = [f[:-3] for f in files if f.endswith(".py")]
        mods = " ".join(stems) if group.get("isolate") and stems and all(x.isidentifier() for x in stems) and len(stems) <= 40 else ""
        command = f"python3 -c {shlex.quote(UNITTEST_SNIPPET)} {shlex.quote(group['folder'])} {mods}".strip()  # what the group's command runs
        code, output, timed_out = run(command, root, 120)
        if timed_out:
            return _result("error", started, clock(), reason="stopped after 120s")
        try:
            data = json.loads(clean(output).strip().splitlines()[-1])
        except (ValueError, IndexError):
            return _result("needs-setup" if code else "error", started, clock(), reason=_first_error(output))
        if data["errors"]:
            return _result("needs-setup", started, clock(), reason=f"{len(data['errors'])} test file(s) can't be imported: {data['errors'][0]}",
                           names=data["names"], how="unittest")
        return _result("ok", started, clock(), names=data["names"], how="unittest")
    return {"status": "unsupported", "seconds": 0.0, "tests": None, "names": [],
            "reason": "This framework's tests are counted by reading the files."}


def read_cache(runtime_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((runtime_dir / CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(runtime_dir: Path, results: dict[str, dict[str, Any]]) -> None:
    path = runtime_dir / CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**read_cache(runtime_dir), **results}
    path.write_text(json.dumps(merged, indent=1), encoding="utf-8")


def overlay(groups: list[dict[str, Any]], cache: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The groups with their probe result. When the framework counted the tests, that count replaces the file scan's
    estimate (kept as `estimated`), and `exact` says so."""
    out = []
    for group in groups:
        result = cache.get(group["id"])
        counted = bool(result and result.get("status") == "ok" and result.get("tests") is not None)
        out.append({**group, "probe": result, "exact": counted, "estimated": group["tests"],
                    **({"tests": result["tests"]} if counted else {})})
    return out
