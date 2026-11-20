"""Tell the builder what the project already has before it writes anything.

The cheapest cruft to remove is the cruft never written. A coding agent that hasn't searched the project writes a new
`format_date` next to the one that exists. This lists the project's own top-level definitions that share words with the
task (its title, description and acceptance criteria) and puts the closest few in the builder's prompt, so reuse is the
first thing it considers. It is plain search over names, with no index to keep or model call to pay for: one `git grep`.

The list is a guess, and the prompt says so. A wrong entry costs the builder a glance; a missing one costs nothing it
wasn't already losing. Whether it helps is measured, not assumed: the job records what was suggested, and the job page
reports how many of those names the finished change actually uses.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from orchestrator import cruft_check

LIMIT = 8            # suggestions in the prompt: past this it is noise, not help
PER_FILE = 3         # from any one file, so one big module can't fill the list
STOP = {"add", "adds", "added", "new", "create", "make", "makes", "support", "implement", "feature", "update", "with", "from",
        "that", "this", "when", "then", "than", "should", "must", "have", "will", "into", "each", "every", "also", "your",
        "user", "users", "data", "code", "file", "files", "test", "tests", "function", "class", "page", "item", "items",
        "list", "value", "values", "show", "shows", "allow", "allows", "using", "used", "uses", "name", "type", "task",
        "job", "work", "works", "need", "needs", "only", "same", "other", "after", "before", "about", "there"}

Definition = tuple[str, int, str]   # path, line, name


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "er"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[:-len(suffix)]
            break
    if word.endswith("s") and len(word) > 4:
        word = word[:-1]
    return word[:-1] if word.endswith("e") and len(word) > 4 else word


def tokens(text: str) -> set[str]:
    """The meaningful words in text, with names split at underscores and capitals (`rankPlayers` -> rank, player)."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    words = (w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced))
    return {_stem(w) for w in words if len(w) >= 4 and w not in STOP and not w.isdigit()}


def task_text(job: dict[str, Any]) -> str:
    """What the builder was asked for, as one string."""
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    parts = [job.get("title"), plan.get("summary"), plan.get("description"), job.get("requirements")]
    for item in plan.get("acceptance_criteria") or []:
        parts.append(item.get("text") if isinstance(item, dict) else item)
    return " ".join(str(p) for p in parts if p)


def index(root: Path) -> list[Definition]:
    """Every top-level definition in the project's source (new, uncommitted files included), tests and lockfiles left out."""
    try:
        res = subprocess.run(["git", "grep", "-n", "-I", "--untracked", "-E", cruft_check.definition_pattern(), "--",
                              *cruft_check.SOURCE_GLOBS], cwd=root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in res.stdout.splitlines():
        path, _, rest = line.partition(":")
        number, _, text = rest.partition(":")
        name = cruft_check.definition_name(text) if number.isdigit() else None
        if name and cruft_check.existing_source(path):
            found.append((path, int(number), name))
    return found


def rank(defs: list[Definition], text: str, likely_files: list[str] | None = None) -> list[dict[str, Any]]:
    """The definitions whose names share words with the task, best first. A definition in a file the plan already means to
    change counts for more: that is the code the builder is about to be standing in."""
    wanted = tokens(text)
    planned = [p.strip().lstrip("./") for p in likely_files or [] if p and p.strip()]
    scored = []
    for path, line, name in defs:
        shared = len(tokens(name) & wanted)
        if not shared:
            continue
        in_plan = any(path == p or path.startswith(p.rstrip("/") + "/") for p in planned)
        scored.append((-(2 * shared + (1 if in_plan else 0)), path, line, name))
    scored.sort()
    out, per_file = [], {}
    for _, path, line, name in scored:
        if per_file.get(path, 0) >= PER_FILE:
            continue
        per_file[path] = per_file.get(path, 0) + 1
        out.append({"name": name, "path": path, "line": line})
        if len(out) == LIMIT:
            break
    return out


def merge(known: list[dict[str, Any]] | None, new: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """A job's suggestions across its tasks: each task's list added to the job's, every definition once."""
    seen = {(s.get("path"), s.get("name")) for s in known or []}
    return list(known or []) + [s for s in new or [] if (s.get("path"), s.get("name")) not in seen]


def candidates(root: Path, job: dict[str, Any]) -> list[dict[str, Any]]:
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    return rank(index(root), task_text(job), [str(p) for p in plan.get("likely_files") or []])


def prompt_block(found: list[dict[str, Any]], root: Path | None = None, likely_files: list[str] | None = None) -> str:
    """The section for the builder's prompt; empty when there is nothing worth saying."""
    existing = [p for p in likely_files or [] if root and (root / p).is_file()]
    if not found and not existing:
        return ""
    lines = ["### EXISTING CODE TO REUSE",
             "Matched by words from this task against the project's own definitions. It is a guess: ignore what doesn't apply. "
             "Before you write a new helper, class or dependency, read the closest entry and call or extend it. "
             "If you add something new anyway, say why in your summary."]
    lines += [f"- `{f['name']}` ({f['path']}:{f['line']})" for f in found]
    if existing:
        lines.append(f"These files the plan names already exist: {', '.join(existing[:8])}. Extend them rather than adding parallel ones.")
    return "\n".join(lines) + "\n\n"
