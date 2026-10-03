"""Anti gold-plating: did the change stay inside what was planned?

Compares a job's plan (the files it expected to touch, how many tasks) with the
diff it actually produced, and reports work nobody asked for: files outside the
plan, new files, dependency or build changes, code outside the feature's own
paths, and a change far bigger than the plan justified. Pure over parsed data;
the web server supplies the git diff. Findings are prompts to look, not verdicts:
anything can be accepted as in scope, and acceptance sticks to the job.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath
from typing import Any

LINES_PER_TASK = 400
MANIFESTS = {"package.json", "Package.swift", "Podfile", "Gemfile", "go.mod", "Cargo.toml", "pyproject.toml", "setup.py",
             "setup.cfg", "build.gradle", "build.gradle.kts", "pom.xml", "project.pbxproj"}
IGNORED = ("*.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Package.resolved", "Podfile.lock", "go.sum",
           ".orchestrator/*", "docs/test-cases/*", "*.xcuserstate", ".gitignore")
TEST_NAME = re.compile(r"(^test_|_test$|Tests?$|Spec$|\.test$|\.spec$)")


def parse_numstat(numstat: str, name_status: str) -> list[dict[str, Any]]:
    """Merge `git diff --numstat` and `--name-status --no-renames` output into one list."""
    status = {}
    for line in name_status.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            status[parts[1].strip()] = parts[0].strip()[:1]
    out = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        out.append({"path": path.strip(), "status": status.get(path.strip(), "M"),
                    "added": int(added) if added.isdigit() else 0, "deleted": int(deleted) if deleted.isdigit() else 0})
    return out


def _stem(path: str) -> str:
    name = PurePosixPath(path).stem
    return TEST_NAME.sub("", name).strip("_.-").lower()


def _matches(path: str, pattern: str) -> bool:
    pattern = pattern.strip().lstrip("./")
    if not pattern:
        return False
    if path == pattern or fnmatch.fnmatch(path, pattern):
        return True
    base = pattern.rstrip("/*")
    return path.startswith(base + "/")


def _ignored(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(PurePosixPath(path).name, pat) for pat in IGNORED)


def _is_test(path: str) -> bool:
    parts = [p.lower() for p in PurePosixPath(path).parts]
    return bool(TEST_NAME.search(PurePosixPath(path).stem)) or any(p in ("test", "tests", "__tests__", "spec", "specs") or p.endswith("tests") for p in parts[:-1])


def planned(path: str, plan_files: list[str]) -> bool:
    if any(_matches(path, p) for p in plan_files):
        return True
    if _is_test(path):  # a test is in scope when it exercises a planned file
        stem = _stem(path)
        return bool(stem) and any(stem == _stem(p) or (len(stem) > 3 and stem in _stem(p)) for p in plan_files if not _is_test(p))
    return False


def evaluate(plan_files: list[str], tasks: int, changed: list[dict[str, Any]], owned_paths: list[str] | None = None,
             accepted: list[str] | None = None) -> dict[str, Any]:
    accepted_set = set(accepted or [])
    plan_files = [p for p in plan_files if p]
    files = [c for c in changed if not _ignored(c["path"])]
    findings: list[dict[str, Any]] = []

    def add(kind: str, severity: str, title: str, detail: str, paths: list[str]) -> None:
        paths = [p for p in paths if p not in accepted_set]
        if paths:
            findings.append({"id": kind, "kind": kind, "severity": severity, "title": title, "detail": detail, "files": paths})

    manifests = [c["path"] for c in files if PurePosixPath(c["path"]).name in MANIFESTS and not planned(c["path"], plan_files)]
    add("manifest", "high", "Dependencies or build settings changed", "Nothing in the plan called for this. New dependencies are the most common way a small change grows.", manifests)

    unplanned = [c for c in files if c["path"] not in manifests and not planned(c["path"], plan_files)]
    if plan_files:
        new = [c["path"] for c in unplanned if c["status"] == "A"]
        add("new_files", "medium", "New files the plan didn't mention", "Each new file is something to maintain. Keep it only if the task needs it.", new)
        add("outside_plan", "medium", "Changes outside the planned files", "These existing files weren't in the plan. Check each change is needed for the task.",
            [c["path"] for c in unplanned if c["status"] != "A"])
    else:
        findings.append({"id": "no_plan_files", "kind": "no_plan_files", "severity": "low", "title": "The plan doesn't list the files it expects to change",
                         "detail": "Without that, scope can't be checked file by file; only size is.", "files": []})

    if owned_paths:
        outside = [c["path"] for c in files if not _is_test(c["path"]) and not any(_matches(c["path"], p) for p in owned_paths)
                   and not planned(c["path"], plan_files)]
        add("outside_feature", "medium", "Code outside this feature's own area", "These paths belong to the rest of the product, not this feature.", outside)

    total = sum(c["added"] for c in files)
    budget = LINES_PER_TASK * max(tasks, 1)
    if total > budget:
        findings.append({"id": "oversized", "kind": "oversized", "severity": "medium", "title": "Bigger than the plan justifies",
                         "detail": f"{total} lines added for {max(tasks, 1)} task(s); about {budget} is typical. Look for extras: abstractions, options, or tidying nobody asked for.",
                         "files": []})

    flagged = {p for f in findings for p in f["files"]}
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    return {"files": len(files), "added": total, "in_scope": len([c for c in files if c["path"] not in flagged]),
            "flagged": len(flagged), "findings": findings, "clear": not findings}


def trim_request(findings: list[dict[str, Any]], title: str) -> str:
    """A job description asking for the extras to be removed or justified."""
    lines = [f"Trim the change for \"{title}\" back to its plan. Revert or remove each item below unless the task genuinely needs it; "
             "where it does, say why in the commit message. Do not add anything new."]
    for f in findings:
        lines.append(f"\n{f['title']}: {f['detail']}")
        lines += [f"- {p}" for p in f["files"][:20]]
    return "\n".join(lines)
