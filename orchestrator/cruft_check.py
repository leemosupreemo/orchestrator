"""Is the change carrying weight it doesn't need?

scope_check asks whether a job stayed inside its plan. This asks what that check can't see from file names alone: code
the change added that the project already has (a new `format_date` beside an existing one), code nothing uses, the same
block pasted twice, and leftovers (debug statements, commented-out code). It reads only the lines the change added, so
existing debt is never blamed on the job.

Pure over parsed data: the web server supplies the git diff and answers two questions about the repository (where is
this name already defined, how often is it mentioned). Findings are prompts to look, not verdicts. Every check is a
heuristic that errs toward staying quiet: only names that read like real helpers are looked up, only top-level
definitions are judged, and anything can be accepted, which sticks to the job.
"""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any, Callable

from orchestrator.scope_check import _ignored, _is_test

SOURCE = (".py", ".js", ".jsx", ".ts", ".tsx", ".swift", ".go", ".rs", ".kt")
MAX_NAMES = 40       # names looked up per job: each is a git call
MAX_FINDINGS = 30
BLOCK_LINES = 5      # consecutive added lines that make a pasted block
EXEMPT = {"lambda_handler"}  # called by a runtime, not by this repository

_DEF = re.compile(r"^(?:(?:export|default|pub(?:\([^)]*\))?|public|open|final|internal|async)\s+)*"
                  r"(?:def|class|func|function|fn|fun|struct|enum|protocol|actor|interface)\s+(\w+)")
_ARROW = re.compile(r"^(?:export\s+)?(?:const|let)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)\s*(?::[^=]+)?=>|function\b|\w+\s*=>)")
_DEBUG = re.compile(r"\bconsole\.(?:log|debug)\(|^\s*debugger\b|\bpdb\.set_trace\(|^\s*breakpoint\(\)|\b(?:FIXME|XXX|HACK)\b")
_COMMENTED_CODE = re.compile(r"^\s*(?:#|//)\s*(?:def |class |return\b|import |from \w+ import |\w+\s*=\s*\S|[\w.]+\(.*\)\s*;?\s*$)")
_TRIVIAL = re.compile(r"^[\s{}()\[\];,]*$|^\s*(?:#|//|import |from |end\b|else\b|\}\s*else)")

Added = dict[str, list[tuple[int, str]]]


def parse_added(diff: str) -> Added:
    """The lines a `git diff -U0` added, by file: [(line number, text)]. Deleted files and binaries add nothing."""
    out: Added = defaultdict(list)
    path, line = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            path = path[2:] if path.startswith("b/") else None if path == "/dev/null" else path
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line = int(m.group(1)) if m else 0
        elif raw.startswith("+") and path:
            out[path].append((line, raw[1:]))
            line += 1
    return dict(out)


SOURCE_GLOBS = [f"*{ext}" for ext in SOURCE]  # `git grep` pathspecs


def definition_pattern(name: str = "") -> str:
    """A `git grep -E` pattern for a top-level definition line: of `name` if given, else of any name. Only lines that start
    in column 0 match, so a method that happens to share a name is not a duplicate of a function."""
    return (r"^(export |default |pub(\([^)]*\))? |public |open |final |internal |async )*"
            r"(def|class|func|function|fn|fun|struct|enum|protocol|actor|interface|const|let)[[:space:]]+"
            + (re.escape(name) if name else "[A-Za-z_]"))


def existing_source(path: str) -> bool:
    """Code that counts as already in the project: source outside test folders. A module that is merely named test_probe.py
    is still the project's own code; only a file under tests/, __tests__/ and the like is a test."""
    folders = [p.lower() for p in PurePosixPath(path).parts[:-1]]
    return path.endswith(SOURCE) and not _ignored(path) and not any(
        p in ("test", "tests", "__tests__", "spec", "specs") or p.endswith("tests") for p in folders)


def reviewable(path: str) -> bool:
    """Source code worth judging: not a test, a lockfile or one of Orchestrator's own files."""
    return path.endswith(SOURCE) and not _ignored(path) and not _is_test(path)


def definition_name(line: str) -> str | None:
    """The name a top-level definition line introduces (`def format_date(`, `export const f = () =>`), else None."""
    m = _DEF.match(line) or _ARROW.match(line)
    return m.group(1) if m else None


def _compound(name: str) -> bool:
    """A name that reads like a specific helper (`format_date`, `ScoreService`), not a word every class has."""
    return len(name) >= 8 and not name.startswith(("test", "__")) and ("_" in name or bool(re.search(r"[a-z][A-Z]", name)))


def _definitions(lines: list[tuple[int, str]]) -> list[tuple[int, str, bool]]:
    """Top-level definitions in added lines: (line, name, decorated)."""
    by_line = dict(lines)
    found = []
    for n, text in lines:
        name = definition_name(text)
        if name and _compound(name) and name not in EXEMPT:
            found.append((n, name, by_line.get(n - 1, "").lstrip().startswith("@")))
    return found


def _finding(kind: str, severity: str, title: str, detail: str, path: str, line: int, key: str) -> dict[str, Any]:
    return {"id": f"{kind}:{path}:{key}", "kind": kind, "severity": severity, "title": title, "detail": detail,
            "files": [path], "line": line}


def _pasted_blocks(added: Added) -> list[dict[str, Any]]:
    """The same run of lines added in two places: copy and paste that wants a shared function."""
    seen: dict[tuple[str, ...], tuple[str, int]] = {}
    runs: dict[tuple[str, str], tuple[dict[str, Any], int]] = {}  # (this file, the file it copies) -> its latest finding and last line
    out: list[dict[str, Any]] = []
    for path, lines in sorted(added.items()):
        if not reviewable(path):
            continue
        for i in range(len(lines) - BLOCK_LINES + 1):
            window = lines[i:i + BLOCK_LINES]
            if window[-1][0] - window[0][0] != BLOCK_LINES - 1:  # not consecutive in the file
                continue
            norm = tuple(re.sub(r"\s+", " ", t).strip() for _, t in window)
            if sum(1 for t in norm if len(t) > 12 and not _TRIVIAL.match(t)) < BLOCK_LINES - 1:
                continue
            first = seen.setdefault(norm, (path, window[0][0]))
            if first == (path, window[0][0]) or (first[0] == path and abs(first[1] - window[0][0]) < BLOCK_LINES):
                continue
            end = window[-1][0]
            run = runs.get((path, first[0]))
            if run and window[0][0] <= run[1]:  # the same pasted block, still going: one finding, longer
                run[0]["detail"] = run[0]["detail"].replace(f"-{run[1]} ", f"-{end} ", 1)
                runs[(path, first[0])] = (run[0], end)
                continue
            found = _finding("pasted_block", "medium", "The same code was added twice",
                             f"Lines {window[0][0]}-{end} repeat {first[0]}:{first[1]}. Move it into one function and call it from both.",
                             path, window[0][0], f"{window[0][0]}")
            runs[(path, first[0])] = (found, end)
            out.append(found)
    return out


def evaluate(added: Added, defined_elsewhere: Callable[[str], list[str]], mentions: Callable[[str], int],
             accepted: list[str] | None = None) -> dict[str, Any]:
    """`defined_elsewhere(name)`: paths that already define `name`. `mentions(name)`: how many lines in the project,
    this change included, mention it."""
    findings: list[dict[str, Any]] = []
    asked = 0
    for path, lines in sorted(added.items()):
        if not reviewable(path):
            continue
        for n, name, decorated in _definitions(lines):
            if asked >= MAX_NAMES:
                break
            asked += 1
            elsewhere = [p for p in defined_elsewhere(name) if p != path]
            if elsewhere:
                findings.append(_finding("duplicate", "medium", f"`{name}` already exists",
                                         f"It is already defined in {', '.join(elsewhere[:3])}. Call that one instead of adding a second.",
                                         path, n, name))
            elif not decorated and mentions(name) <= 1:
                findings.append(_finding("unused", "low", f"`{name}` is never used",
                                         "Nothing else in the project mentions it. Remove it, or wire it in if it was meant to be called.",
                                         path, n, name))
        debug = [n for n, t in lines if _DEBUG.search(t)]
        if debug:
            findings.append(_finding("leftover", "low", "Debug leftovers", f"Debug statements or FIXME markers added at line {debug[0]}"
                                     + (f" and {len(debug) - 1} more" if len(debug) > 1 else "") + ". Remove them before merging.", path, debug[0], "debug"))
        commented = [n for n, t in lines if _COMMENTED_CODE.match(t)]
        if len(commented) >= 2:
            findings.append(_finding("leftover", "low", "Commented-out code", f"{len(commented)} lines of code were added as comments, starting at line "
                                     f"{commented[0]}. Delete them; Git keeps the history.", path, commented[0], "commented"))
    findings += _pasted_blocks(added)

    accepted_set = set(accepted or [])
    open_findings = [f for f in findings if f["id"] not in accepted_set][:MAX_FINDINGS]
    order = {"high": 0, "medium": 1, "low": 2}
    open_findings.sort(key=lambda f: (order[f["severity"]], f["files"][0], f["line"]))
    return {"files": len([p for p in added if reviewable(p)]), "findings": open_findings, "clear": not open_findings,
            "accepted": len(findings) - len([f for f in findings if f["id"] not in accepted_set])}


def fix_request(findings: list[dict[str, Any]], title: str) -> str:
    """A job description asking for the findings to be fixed without adding anything."""
    lines = [f"Remove the unnecessary code from the change for \"{title}\". Fix each item below by deleting it, reusing what the project "
             "already has, or merging the repeated code into one function. Do not add features or new files."]
    for f in findings:
        lines.append(f"\n{f['title']} ({f['files'][0]}:{f['line']}): {f['detail']}")
    return "\n".join(lines)
