"""Test counts from the formats .NET and most other runners write: `dotnet test` console output, TRX and JUnit XML.

Every reader returns the same shape `parse_test_output` in scripts/common.py does (total_run, passed_count,
failed_count, failing_tests) plus skipped_count, so a job's summary and the Tests page treat them alike.
`read_results` takes a path or glob inside the project, so a command that writes `TestResults/*.trx` can point at it.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 50 * 1024 * 1024
FAILING_NAMES_KEPT = 200

_DOTNET_SUMMARY = re.compile(
    r"^(?:Passed|Failed)!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+),\s+Skipped:\s+(\d+),\s+Total:\s+(\d+)", re.M)
_DOTNET_FAILED_TEST = re.compile(r"^\s+Failed\s+(\S+?)(?:\(.*\))?\s+\[[^\]]*\]\s*$", re.M)


def _empty() -> dict[str, Any]:
    return {"total_run": 0, "passed_count": 0, "failed_count": 0, "skipped_count": 0, "failing_tests": []}


def _short(name: str) -> str:
    """`Ns.Class.Method(args)` as `Class.Method`, the way the Tests page names tests."""
    name = re.sub(r"\(.*\)$", "", name.strip())
    return ".".join(name.split(".")[-2:])


def _add_failure(failing: list[str], name: str) -> None:
    short = _short(name)
    if short and short not in failing and len(failing) < FAILING_NAMES_KEPT:
        failing.append(short)


def parse_dotnet_output(text: str) -> dict[str, Any] | None:
    """`dotnet test` console output, summed over every test project it ran. None when it isn't dotnet output."""
    summaries = _DOTNET_SUMMARY.findall(text or "")
    if not summaries:
        return None
    result = _empty()
    for failed, passed, skipped, total in summaries:
        result["failed_count"] += int(failed)
        result["skipped_count"] += int(skipped)
        result["total_run"] += int(total)
    result["passed_count"] = max(0, result["total_run"] - result["failed_count"] - result["skipped_count"])
    for match in _DOTNET_FAILED_TEST.finditer(text):
        _add_failure(result["failing_tests"], match.group(1))
    return result


def _parse_xml(text: str) -> ET.Element | None:
    if "<!DOCTYPE" in text[:2000].upper() or "<!ENTITY" in text.upper():
        return None  # runners don't write these, and entity expansion is how XML files get abused
    try:
        return ET.fromstring(text.lstrip("﻿"))
    except ET.ParseError:
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_trx(text: str) -> dict[str, Any] | None:
    """A Visual Studio TRX file (what `dotnet test --logger trx` writes)."""
    root = _parse_xml(text)
    if root is None or _local(root.tag) != "TestRun":
        return None
    result = _empty()
    for node in root.iter():
        if _local(node.tag) != "UnitTestResult":
            continue
        outcome = node.get("outcome", "")
        result["total_run"] += 1
        if outcome == "Passed":
            result["passed_count"] += 1
        elif outcome in ("NotExecuted", "Inconclusive", "Pending"):
            result["skipped_count"] += 1
        else:
            result["failed_count"] += 1
            _add_failure(result["failing_tests"], node.get("testName", ""))
    return result


def parse_junit(text: str) -> dict[str, Any] | None:
    """JUnit XML (`<testsuite>` or `<testsuites>`: pytest --junitxml, Gradle, Jest, many others)."""
    root = _parse_xml(text)
    if root is None or _local(root.tag) not in ("testsuite", "testsuites"):
        return None
    result = _empty()
    for case in (n for n in root.iter() if _local(n.tag) == "testcase"):
        children = {_local(c.tag) for c in case}
        result["total_run"] += 1
        if children & {"failure", "error"}:
            result["failed_count"] += 1
            name = f'{case.get("classname", "")}.{case.get("name", "")}'.strip(".")
            _add_failure(result["failing_tests"], name)
        elif "skipped" in children:
            result["skipped_count"] += 1
        else:
            result["passed_count"] += 1
    return result


def parse_file(path: Path) -> dict[str, Any] | None:
    """One results file, by what's inside it (TRX or JUnit). None when it's unreadable, too big or neither."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_trx(text) or parse_junit(text)


def combine(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = _empty()
    for r in results:
        for key in ("total_run", "passed_count", "failed_count", "skipped_count"):
            total[key] += r.get(key, 0)
        for name in r.get("failing_tests", []):
            if name not in total["failing_tests"] and len(total["failing_tests"]) < FAILING_NAMES_KEPT:
                total["failing_tests"].append(name)
    return total


def read_results(root: Path, pattern: str) -> dict[str, Any] | None:
    """The combined counts of every results file matching `pattern` (a path or glob inside `root`). Every match
    counts, old ones too, so point it at a folder the command clears. None when nothing matched or was readable."""
    root = root.resolve()
    parsed = []
    for path in sorted(root.glob(pattern)):
        try:
            inside = path.resolve().is_relative_to(root)
        except (OSError, ValueError):
            inside = False
        if inside and path.is_file() and (one := parse_file(path)):
            parsed.append(one)
    return combine(parsed) if parsed else None
