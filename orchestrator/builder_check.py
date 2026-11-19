"""Checks on what a builder run actually did, so a job doesn't carry on from a claim the disk doesn't back up.

A builder model can report that its writes were blocked (the AI tool's permission mode refused them) and still hand back a
test command for a test file it never wrote. The build then passed, "NO TESTS RAN" counted as a failure, and a resumed job
reused that same summary forever. These helpers are pure so they can be tested without a model or a live checkout:

    writes_blocked(text)            the builder says its writes were refused / nothing was changed
    tree_fingerprint(root)          a cheap fingerprint of the working tree's changes, to see whether a run changed anything
    is_ignored_tool_path(path)      whether a path belongs to local agent/orchestrator state (.claude/, .orchestrator/, .worktrees/)
    missing_test_targets(cmd, root) test files a test command names that don't exist
    source_only_test_files(cmd, root) Python test files in `cmd` that only assert substrings on source text
    no_tests_ran(output)            a test run that found no tests at all
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
import shlex
import subprocess
from pathlib import Path

_BLOCKED = [re.compile(p) for p in (
    r"\b(?:writes?|edits?|file (?:writes?|edits?|changes?))\b[^.\n]{0,80}\b(?:were|was|are|is|got|been)\s+(?:all\s+)?(?:blocked|denied|refused|declined|rejected)\b",
    r"\b(?:blocked|denied|refused|prevented)\s+by\s+(?:the\s+)?(?:session'?s?\s+)?permission",
    r"\bno files?\s+(?:on disk\s+)?(?:was|were|has|have)\s+(?:been\s+)?(?:changed|modified|written|created|edited)\b",
    r"\bnothing\s+(?:was|has been)\s+(?:written|changed|applied|saved)\b",
    r"\bonce\s+write\s+permissions?\s+(?:is|are)\s+granted\b",
    r"\b(?:could not|couldn't|unable to|cannot|can't)\s+write\s+(?:any\s+)?files?\b",
)]


def writes_blocked(text: str) -> bool:
    """Whether a builder's report says its file writes were refused, or that no file was changed."""
    flat = re.sub(r"\s+", " ", (text or "").lower().replace("’", "'"))
    return any(p.search(flat) for p in _BLOCKED)


_IGNORED = (".orchestrator/", ".claude/", ".worktrees/")


def is_ignored_tool_path(path: str) -> bool:
    """Whether `path` lives inside orchestrator or AI tool scratch directories that must not count as project changes."""
    norm = (path or "").strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return any(norm.startswith(prefix) or norm == prefix.rstrip("/") for prefix in _IGNORED)


def tree_fingerprint(root: Path) -> str | None:
    """A hash of every changed or untracked path (git status) with its size and modification time. It moves when the builder edits
    a file that was already modified, which the status line alone would not show. None when git can't say."""
    try:
        status = subprocess.run(["git", "status", "--porcelain=v1", "-uall"], cwd=root, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if status.returncode != 0:
        return None
    digest = hashlib.sha1()
    for line in sorted(status.stdout.splitlines()):
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if not path or path.startswith(_IGNORED):
            continue
        try:
            info = (Path(root) / path).stat()
            digest.update(f"{line}|{info.st_size}|{info.st_mtime_ns}\n".encode())
        except OSError:
            digest.update(f"{line}|gone\n".encode())
    return digest.hexdigest()


def changed(before: str | None, after: str | None) -> bool:
    """Whether the working tree moved between two fingerprints. Unknown counts as not moved: the builder's own report decides then."""
    return before is not None and after is not None and before != after


_SKIP_DIRS = {".git", "node_modules", "Library", "Pods", "build", "dist", "venv", ".venv", "__pycache__"}
_GLOB = re.compile(r"[*?\[\$`]")


def _found(base: Path, pattern: str) -> bool:
    if not base.is_dir():
        return False
    if not _GLOB.search(pattern) and (base / pattern).is_file():
        return True
    for folder, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        if any(Path(f).match(pattern) for f in files):
            return True
    return False


def _command_target_spec(command: str) -> tuple[tuple[str, str | None] | None, list[str]]:
    """Parse `(discover_dir, discover_pattern)` or explicit `*.py` file args from a Python test command."""
    try:
        parts = shlex.split(command or "")
    except ValueError:
        return None, []
    if not parts or "cd" in parts or any("$" in p for p in parts):
        return None, []
    if "discover" in parts:
        value = lambda flag: parts[parts.index(flag) + 1] if flag in parts and parts.index(flag) + 1 < len(parts) else None
        return (value("-s") or value("--start-directory") or ".", value("-p") or value("--pattern")), []
    targets: list[str] = []
    skip = False
    for part in parts[1:]:
        if skip:
            skip = False
            continue
        if part in ("-c", "-m", "-k", "-p", "-s", "-t", "--rootdir", "--config-file"):
            skip = True
            continue
        target = part.split("::")[0]
        if target.endswith(".py") and not part.startswith("-") and not _GLOB.search(target):
            targets.append(target)
    return None, targets


def missing_test_targets(command: str, root: Path) -> list[str]:
    """Test files `command` names that aren't in `root`: `unittest discover -s DIR -p PATTERN` matching nothing, or a `*.py` file argument
    that doesn't exist. Commands that change directory first, or use shell variables, can't be checked and report nothing."""
    discover, targets = _command_target_spec(command)
    root = Path(root)
    if discover is not None:
        start, pattern = discover
        if pattern and not _found(root / start, pattern):
            return [f"{pattern} (in {start})"]
        return []
    return [target for target in targets if not (root / target).exists()]


_RAN_SOME = re.compile(r"\bRan\s+[1-9]\d*\s+tests?\b|\b[1-9]\d*\s+(?:passed|failed)\b|\bCollected\s+[1-9]\d*\s+items?\b", re.I)
_RAN_NONE = re.compile(r"\bNO TESTS RAN\b|\bRan\s+0\s+tests?\b|\bno tests ran\b|\bcollected\s+0\s+items?\b|\bNo tests found\b", re.I)


def no_tests_ran(output: str) -> bool:
    """A test run that found nothing to run (unittest, pytest and Jest all say so in their own words), with no test run beside it."""
    text = output or ""
    return bool(_RAN_NONE.search(text)) and not _RAN_SOME.search(text)


_TEXT_TEST_STDLIB = frozenset({"__future__", "glob", "json", "os", "pathlib", "pytest", "re", "sys", "typing", "unittest"})
_FILE_READ_CALLS = frozenset({"open", "read_text", "read_bytes"})
_STRING_ASSERT_CALLS = frozenset({"assertIn", "assertNotIn", "assertRegex", "assertNotRegex", "assertTrue", "assertFalse"})
_EXEC_CALLS = frozenset({"subprocess", "system", "popen", "exec", "eval", "compile"})


def _targeted_python_files(command: str, root: Path) -> list[Path]:
    """Existing `.py` test files referenced by `command` (via `unittest discover` or direct arguments)."""
    discover, targets = _command_target_spec(command)
    base = Path(root)
    if discover is not None:
        start_rel, pattern = discover
        start = base / start_rel
        pat = pattern or "test*.py"
        if not start.is_dir():
            return []
        found: list[Path] = []
        for folder, dirs, files in os.walk(start):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in sorted(files):
                if Path(fname).match(pat):
                    found.append(Path(folder) / fname)
        return found
    return [base / target for target in targets if (base / target).is_file()]


def _is_source_text_only_file(path: Path) -> bool:
    """True when a Python test file imports only stdlib/test modules and asserts on text read from disk without running code."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return False

    imports: set[str] = set()
    calls: set[str] = set()
    test_defs = 0
    has_assert = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            test_defs += 1
        elif isinstance(node, ast.Assert):
            has_assert = True
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
                if isinstance(node.func.value, ast.Name):
                    calls.add(node.func.value.id)

    if not test_defs or not imports or not imports.issubset(_TEXT_TEST_STDLIB):
        return False
    if calls & _EXEC_CALLS:
        return False
    return bool(calls & _FILE_READ_CALLS) and (has_assert or bool(calls & _STRING_ASSERT_CALLS))


def source_only_test_files(command: str, root: Path) -> list[str]:
    """Targeted Python test files in `command` that only read files and assert substrings/regexes rather than running code."""
    base = Path(root)
    flagged: list[str] = []
    for path in _targeted_python_files(command, base):
        if _is_source_text_only_file(path):
            try:
                flagged.append(path.relative_to(base).as_posix())
            except ValueError:
                flagged.append(str(path))
    return flagged
