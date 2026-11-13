"""Extra test commands that run after a job's main tests pass.

`test_command` stays the project's main test run. `test_commands` in `.orchestrator/project.json` adds more, for a
project whose tests live in several places (a game's protocol library, its server, its client):

    "test_commands": [
      {"name": "protocol", "command": "dotnet test tests/Daoc.Protocol.Tests -c Release", "timeout": 600},
      {"name": "server", "command": "python3 tools/test-profile-server.py --skip-baseline", "tier": "manual",
       "requires": ["docker", "container:daoc-two-faction-server", "path:.local/staging"]}
    ]

Each entry: `name` (unique), `command`, optional `cwd` (inside the project, default "."), `timeout` in seconds
(default 30 minutes), `retries` (default 0: a command that passes after failing is reported as flaky), `tier`,
`requires` and `results` (a TRX or JUnit file, or a glob of them inside the project, for the test counts; without it
the counts come from `dotnet test` output when the command prints it). Only the "per-job" tier (the default) runs with a job; "nightly" and "manual" are for people and
schedules. A command whose `requires` aren't met is *blocked*, not failed: it says what is missing and the job goes on.
`requires` entries: `docker` (the daemon answers), `container:<name>` (a Docker container that is running),
`command:<name>` (on PATH), `path:<file or folder in the project>`, `env:<NAME>` (set and not empty).

`backend_test_command` is the same thing for one command, named "backend".

Pure helpers with injected effects so they can be tested without Docker or a real shell; the pipeline in
scripts/run_build_and_tests.py supplies the project root and output folder.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from orchestrator import test_results

TIERS = ("per-job", "nightly", "manual")
DEFAULT_TIMEOUT = 30 * 60
TIMEOUT_EXIT_CODE = 124  # what `timeout(1)` uses
KILL_GRACE_SECONDS = 10
REQUIREMENT_KINDS = ("docker", "container", "command", "path", "env")


@dataclass(frozen=True)
class ExtraTestCommand:
    name: str
    command: str
    cwd: str = "."
    timeout: int = DEFAULT_TIMEOUT
    retries: int = 0
    tier: str = "per-job"
    requires: tuple[str, ...] = ()
    results: str = ""


def _requirement_error(requirement: str) -> str | None:
    kind, _, value = requirement.partition(":")
    if kind not in REQUIREMENT_KINDS:
        return f'unknown requirement "{requirement}" (use docker, container:<name>, command:<name>, path:<path> or env:<NAME>)'
    if kind == "docker" and value:
        return 'requirement "docker" takes no value'
    if kind != "docker" and not value:
        return f'requirement "{requirement}" needs a value, like {kind}:<value>'
    return None


def parse(raw: Any, root: Path | None = None) -> tuple[list[ExtraTestCommand], list[str]]:
    """The commands in a `test_commands` value, and what is wrong with it. Entries with errors are left out."""
    if raw in (None, [], ()):
        return [], []
    if not isinstance(raw, (list, tuple)):
        return [], ["test_commands must be a list."]
    commands: list[ExtraTestCommand] = []
    errors: list[str] = []
    names: set[str] = set()
    for index, entry in enumerate(raw, 1):
        label = f"test_commands[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{label} must be an object.")
            continue
        name = str(entry.get("name") or "").strip()
        command = str(entry.get("command") or "").strip()
        label = f'test_commands "{name}"' if name else label
        problems: list[str] = []
        if not name:
            problems.append("needs a name")
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            problems.append("name may only use letters, digits, '.', '_' and '-'")
        elif name in names:
            problems.append("name is used twice")
        if not command:
            problems.append("needs a command")
        cwd = str(entry.get("cwd") or ".")
        if root is not None and not _inside(root, cwd):
            problems.append(f'cwd "{cwd}" is outside the project')
        timeout, retries = entry.get("timeout", DEFAULT_TIMEOUT), entry.get("retries", 0)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            problems.append("timeout must be a whole number of seconds above 0")
        if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 5:
            problems.append("retries must be a whole number from 0 to 5")
        tier = str(entry.get("tier") or "per-job")
        if tier not in TIERS:
            problems.append(f'tier must be one of {", ".join(TIERS)}')
        requires = entry.get("requires") or []
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            problems.append("requires must be a list of strings")
            requires = []
        problems += [error for r in requires if (error := _requirement_error(r))]
        results = entry.get("results") or ""
        if not isinstance(results, str):
            problems.append("results must be a path or glob of TRX/JUnit files")
            results = ""
        elif results and (Path(results).is_absolute() or ".." in Path(results).parts):
            problems.append(f'results "{results}" must be inside the project')
        if problems:
            errors += [f"{label}: {problem}." for problem in problems]
            continue
        names.add(name)
        commands.append(ExtraTestCommand(name, command, cwd, timeout, retries, tier, tuple(requires), results))
    return commands, errors


def from_config(config: Any) -> tuple[list[ExtraTestCommand], list[str]]:
    """Everything the project configures beyond `test_command`: `test_commands`, then `backend_test_command`."""
    root = getattr(config, "root", None)
    commands, errors = parse(getattr(config, "test_commands", None), root)
    backend = (getattr(config, "backend_test_command", None) or "").strip()
    if backend:
        if any(c.name == "backend" for c in commands):
            errors.append('backend_test_command is also a test_commands entry named "backend"; rename the entry.')
        else:
            commands.append(ExtraTestCommand("backend", backend))
    return commands, errors


def _inside(root: Path, cwd: str) -> bool:
    try:
        return (root / cwd).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def docker_ready() -> bool:
    """Whether a Docker daemon answers (installed but not running is the usual reason it doesn't)."""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def container_running(name: str) -> bool:
    """Whether a Docker container with this name is running."""
    if not shutil.which("docker"):
        return False
    try:
        done = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", name],
                              capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and done.stdout.strip() == "true"


def unmet_requirements(requires: tuple[str, ...] | list[str], root: Path, *,
                       which: Callable[[str], str | None] = shutil.which,
                       docker: Callable[[], bool] = docker_ready,
                       container: Callable[[str], bool] = container_running,
                       environ: dict[str, str] | None = None) -> list[str]:
    """What is missing, in words a person can act on. Empty when the command can run."""
    environ = os.environ if environ is None else environ
    missing: list[str] = []
    for requirement in requires:
        kind, _, value = requirement.partition(":")
        if kind == "docker" and not docker():
            missing.append("Docker isn't running (start Docker Desktop)")
        elif kind == "container" and not container(value):
            missing.append(f"the {value} container isn't running (start the server it belongs to)")
        elif kind == "command" and not which(value):
            missing.append(f"{value} isn't installed or isn't on PATH")
        elif kind == "path" and not (root / value).exists():
            missing.append(f"{value} doesn't exist (a local file this test needs)")
        elif kind == "env" and not environ.get(value):
            missing.append(f"{value} isn't set")
    return missing


def execute(command: str, cwd: Path, timeout: int) -> tuple[int, str, bool]:
    """Run a shell command, output and errors together. On timeout the whole process group gets SIGTERM, then
    SIGKILL after a grace period, so a test runner's children (Unity, dotnet, docker clients) don't linger."""
    process = subprocess.Popen(command, cwd=str(cwd), shell=True, executable="/bin/bash", stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, output or "", False
    except subprocess.TimeoutExpired:
        for sig, wait in ((signal.SIGTERM, KILL_GRACE_SECONDS), (signal.SIGKILL, None)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            try:
                output, _ = process.communicate(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            output = ""
        return TIMEOUT_EXIT_CODE, output or "", True


def run_one(extra: ExtraTestCommand, root: Path, *, run: Callable[[str, Path, int], tuple[int, str, bool]] = execute,
            requirements: Callable[[tuple[str, ...], Path], list[str]] | None = None,
            clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """One command's result: status "passed", "failed", "timeout" or "blocked", with its output (the last attempt's)."""
    result: dict[str, Any] = {"name": extra.name, "command": extra.command, "tier": extra.tier, "attempts": 0,
                              "flaky": False, "seconds": 0.0, "output": ""}
    missing = (requirements or (lambda r, p: unmet_requirements(r, p)))(extra.requires, root)
    if missing:
        return {**result, "status": "blocked", "reason": "; ".join(missing)}
    started = clock()
    status, failed_before = "failed", False
    for attempt in range(1, extra.retries + 2):
        code, output, timed_out = run(extra.command, root / extra.cwd, extra.timeout)
        result.update(attempts=attempt, output=output, exit_code=code)
        if timed_out:
            status = "timeout"
        elif code == 0:
            status, result["flaky"] = "passed", failed_before
            break
        else:
            status = "failed"
        failed_before = True
    result.update(status=status, seconds=round(clock() - started, 1))
    counts = (test_results.read_results(root, extra.results) if extra.results else None) \
        or test_results.parse_dotnet_output(result["output"])
    if counts:
        result["tests"] = {k: counts[k] for k in ("total_run", "passed_count", "failed_count", "skipped_count")}
        result["failing_tests"] = counts["failing_tests"]
    if status == "timeout":
        result["reason"] = f"stopped after {extra.timeout}s"
    return result


def run_all(commands: list[ExtraTestCommand], root: Path, out_dir: Path, *, tier: str = "per-job",
            run_command: Callable[..., dict[str, Any]] = run_one) -> list[dict[str, Any]]:
    """Run the commands of one tier in order, write a log per command and `test_commands.json`, and return the
    results. A failure doesn't stop the rest: every command reports."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for extra in (c for c in commands if c.tier == tier):
        result = run_command(extra, root)
        log = out_dir / f"test_{extra.name}.log"
        log.write_text(result["output"] or result.get("reason", ""), encoding="utf-8")
        result["log"] = log.name
        results.append(result)
    (out_dir / "test_commands.json").write_text(
        json.dumps([{k: v for k, v in r.items() if k != "output"} for r in results], indent=2), encoding="utf-8")
    return results


def passed(results: list[dict[str, Any]]) -> bool:
    """Blocked commands don't fail a job: they couldn't run here, which is not the code's fault."""
    return all(r["status"] in ("passed", "blocked") for r in results)


def summary(results: list[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        detail = {"passed": f'passed in {r["seconds"]}s' + (f' (flaky: {r["attempts"]} attempts)' if r["flaky"] else ""),
                  "failed": f'FAILED (exit {r.get("exit_code")}) in {r["seconds"]}s',
                  "timeout": f'TIMED OUT, {r.get("reason", "")}',
                  "blocked": f'blocked: {r.get("reason", "")}'}[r["status"]]
        tests = r.get("tests")
        if tests:
            detail += f' ({tests["total_run"]} tests, {tests["failed_count"]} failed, {tests["skipped_count"]} skipped)'
        lines.append(f'  {r["name"]}: {detail}')
    return "\n".join(lines)


def failure_report(results: list[dict[str, Any]], tail_lines: int = 200) -> str:
    """What went wrong, for `test.log` and the failure analysis: each failed command and the end of its output."""
    parts = []
    for r in results:
        if r["status"] in ("failed", "timeout"):
            tail = "\n".join((r["output"] or "").splitlines()[-tail_lines:])
            parts.append(f'=== {r["name"]} ({r["status"]}): {r["command"]}\n{tail}')
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------------------------- for the web UI

LAST_RESULT_FIELDS = ("status", "seconds", "reason", "flaky", "attempts", "exit_code", "tests", "failing_tests", "log")
RESULT_FILES_SCANNED = 60


def latest_results(runtime_dir: Path) -> dict[str, dict[str, Any]]:
    """The newest result of each command, from jobs' `test_commands.json` and `run-tests` runs. `at` is when it was
    written and `job` the job it ran for (empty for a run by hand)."""
    output = runtime_dir / "output"
    files = []
    for pattern in ("*/test_commands.json", "manual-tests/*/test_commands.json"):
        files += list(output.glob(pattern)) if output.is_dir() else []
    newest: dict[str, dict[str, Any]] = {}
    try:
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return {}
    for path in files[:RESULT_FILES_SCANNED]:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            at = path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            continue
        folder = path.parent
        job = "" if folder.parent.name == "manual-tests" else folder.name
        for row in rows if isinstance(rows, list) else []:
            name = row.get("name") if isinstance(row, dict) else None
            if isinstance(name, str) and name not in newest:
                newest[name] = {**{k: row[k] for k in LAST_RESULT_FIELDS if k in row}, "at": at, "job": job}
    return newest


def view(config: Any, results: dict[str, dict[str, Any]] | None = None, *,
         requirements: Callable[[tuple[str, ...], Path], list[str]] | None = None) -> list[dict[str, Any]]:
    """What the Tests page shows for each configured command: its settings, what is missing right now, its latest result.
    `editable` is False for `backend_test_command`, which lives in project.json under its own key."""
    root = Path(getattr(config, "root", "."))
    check = requirements or (lambda r, p: unmet_requirements(r, p))
    commands, _ = from_config(config)
    backend = (getattr(config, "backend_test_command", None) or "").strip()
    rows = []
    for c in commands:
        rows.append({"name": c.name, "command": c.command, "cwd": c.cwd, "timeout": c.timeout, "retries": c.retries,
                     "tier": c.tier, "requires": list(c.requires), "results": c.results,
                     "unmet": check(c.requires, root) if c.requires else [],
                     "editable": not (backend and c.name == "backend" and c.command == backend),
                     "last": (results or {}).get(c.name)})
    return rows


def entry_from_form(body: dict[str, Any]) -> dict[str, Any]:
    """A `test_commands` entry from the web form: text fields, with defaults left out so project.json stays tidy."""
    def text(key: str) -> str:
        return str(body.get(key) or "").strip()

    def whole(key: str, default: int) -> Any:
        raw = text(key)
        if not raw:
            return default
        return int(raw) if re.fullmatch(r"\d+", raw) else raw  # a bad value is reported by parse(), in the form's words

    entry: dict[str, Any] = {"name": text("name"), "command": text("command")}
    if text("cwd") not in ("", "."):
        entry["cwd"] = text("cwd")
    if (timeout := whole("timeout", DEFAULT_TIMEOUT)) != DEFAULT_TIMEOUT:
        entry["timeout"] = timeout
    if (retries := whole("retries", 0)) != 0:
        entry["retries"] = retries
    if text("tier") not in ("", "per-job"):
        entry["tier"] = text("tier")
    requires = [line.strip() for line in str(body.get("requires") or "").replace(",", "\n").splitlines() if line.strip()]
    if requires:
        entry["requires"] = requires
    if text("results"):
        entry["results"] = text("results")
    return entry


def with_entry(raw: Any, entry: dict[str, Any], original: str | None, root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """The `test_commands` list with `entry` added, or replacing `original`, and what is wrong with the result."""
    items = [dict(x) for x in raw] if isinstance(raw, list) else []
    index = next((i for i, x in enumerate(items) if original and x.get("name") == original), None)
    if index is None:
        items.append(entry)
    else:
        items[index] = entry
    return items, parse(items, root)[1]


def without_entry(raw: Any, name: str) -> list[dict[str, Any]]:
    return [dict(x) for x in raw if isinstance(x, dict) and x.get("name") != name] if isinstance(raw, list) else []
