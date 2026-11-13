"""Measured line coverage per platform, from the tools that can measure it.

`platforms.py` says which files a test mentions. This runs the tests under a coverage tool and reports which lines they
really executed:

    .NET     dotnet test <project> --collect:"XPlat Code Coverage"   (the project needs the coverlet.collector package)
    Python   coverage run -m unittest / pytest, then coverage json    (needs the coverage package)
    Android  Gradle unit tests with the Android Gradle Plugin's coverage report (enableUnitTestCoverage), or the jacoco plugin
    JS       Jest or Vitest with an lcov report (Vitest needs a coverage provider such as @vitest/coverage-v8)

Other languages have no per-file measurement here yet, so they show nothing rather than a guess.

A measurement covers the files the tests *load*: coverlet reports every class of an assembly the tests reference, and
coverage.py every file under the project, so a platform's figure is "x% of the lines in the N files measured", shown
beside how many files the platform has. Running this runs the tests, so it only happens when someone asks.

Results are cached in <runtime>/output/line-coverage.json (per file: lines covered, lines that can be covered).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from orchestrator import platforms as platform_view, test_commands, test_groups

CACHE = Path("output") / "line-coverage.json"
TIMEOUT = 1800
Run = Callable[[str, Path, int], "tuple[int, str, bool]"]
FileLines = dict[str, tuple[set[int], set[int]]]  # path -> (covered lines, all coverable lines)


def parse_cobertura(xml_text: str, root: Path) -> FileLines:
    """Per-file lines from a Cobertura report. Paths come back relative to the project; files outside it are dropped."""
    if "<!DOCTYPE" in xml_text[:2000].upper() or "<!ENTITY" in xml_text.upper():
        return {}
    try:
        tree = ET.fromstring(xml_text.lstrip("﻿"))
    except ET.ParseError:
        return {}
    sources = [Path(s.text.strip()) for s in tree.iter("source") if s.text]
    out: FileLines = {}
    for cls in tree.iter("class"):
        filename = cls.get("filename") or ""
        candidates = [Path(filename)] if Path(filename).is_absolute() else [s / filename for s in sources] + [root / filename]
        rel = None
        for candidate in candidates:
            try:
                rel = candidate.resolve().relative_to(root.resolve()).as_posix()
                break
            except (OSError, ValueError):
                continue
        if rel is None:
            continue
        covered, every = out.setdefault(rel, (set(), set()))
        for line in cls.iter("line"):
            number = int(line.get("number", "0"))
            every.add(number)
            if int(line.get("hits", "0")) > 0:
                covered.add(number)
    return out


def parse_coverage_json(text: str, root: Path) -> FileLines:
    """Per-file lines from `coverage json`."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    out: FileLines = {}
    for name, info in (data.get("files") or {}).items():
        path = Path(name)
        try:
            rel = (path.resolve() if path.is_absolute() else (root / path).resolve()).relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            continue
        executed, missing = set(info.get("executed_lines") or []), set(info.get("missing_lines") or [])
        out[rel] = (executed, executed | missing)
    return out


def parse_lcov(text: str, base: Path, root: Path) -> FileLines:
    """Per-file lines from an lcov report. Paths are relative to the folder the tool ran in; files outside the project and
    anything in node_modules are dropped."""
    out: FileLines = {}
    current: tuple[set[int], set[int]] | None = None
    for line in text.splitlines():
        if line.startswith("SF:"):
            raw = Path(line[3:].strip())
            path = raw if raw.is_absolute() else base / raw
            try:
                rel = path.resolve().relative_to(root.resolve()).as_posix()
            except (OSError, ValueError):
                current = None
                continue
            current = None if "node_modules" in Path(rel).parts else out.setdefault(rel, (set(), set()))
        elif line.startswith("DA:") and current is not None:
            number, _, rest = line[3:].partition(",")
            hits = rest.split(",")[0]
            if number.isdigit() and hits.lstrip("-").isdigit():
                current[1].add(int(number))
                if int(hits) > 0:
                    current[0].add(int(number))
        elif line.startswith("end_of_record"):
            current = None
    return {path: lines for path, lines in out.items() if lines[1]}


def _source_index(module: Path, root: Path) -> dict[str, str]:
    """`com/example/Foo.kt` -> that file's path in the project, for the source sets under <module>/src (main first)."""
    index: dict[str, str] = {}
    src = module / "src"
    try:
        sets = sorted((p for p in src.iterdir() if p.is_dir()), key=lambda p: (p.name != "main", p.name))
    except OSError:
        return index
    for source_set in sets:
        for language in ("java", "kotlin"):
            folder = source_set / language
            if not folder.is_dir():
                continue
            for path in list(folder.rglob("*"))[:20_000]:
                if path.is_file():
                    index.setdefault(path.relative_to(folder).as_posix(), path.resolve().relative_to(root.resolve()).as_posix())
    return index


def parse_jacoco(xml_text: str, root: Path, module: Path) -> FileLines:
    """Per-file lines from a JaCoCo XML report: a line is covered when any of its instructions ran. JaCoCo names a file by its
    package and name, so each is looked up in the module's source folders."""
    if "<!ENTITY" in xml_text.upper() or re.search(r"<!DOCTYPE[^>]*\[", xml_text[:4000]):
        return {}  # an internal DTD subset is how entity tricks are written; a real report has only the plain DOCTYPE line
    cleaned = re.sub(r"<!DOCTYPE[^>]*>", "", xml_text, count=1)
    try:
        tree = ET.fromstring(cleaned.lstrip("\ufeff"))
    except ET.ParseError:
        return {}
    index = _source_index(module, root)
    out: FileLines = {}
    for package in tree.iter("package"):
        folder = (package.get("name") or "").strip("/")
        for source in package.iter("sourcefile"):
            key = f"{folder}/{source.get('name')}" if folder else str(source.get("name"))
            rel = index.get(key)
            if rel is None:
                continue
            covered, every = out.setdefault(rel, (set(), set()))
            for line in source.iter("line"):
                number = int(line.get("nr", "0"))
                if number <= 0:
                    continue
                every.add(number)
                if int(line.get("ci", "0")) > 0:
                    covered.add(number)
    return {path: lines for path, lines in out.items() if lines[1]}


def merge(into: FileLines, other: FileLines) -> None:
    """Add one group's lines to the total: a line any group executed counts as covered."""
    for path, (covered, every) in other.items():
        have_covered, have_every = into.setdefault(path, (set(), set()))
        have_covered |= covered
        have_every |= every


def measure_group(group: dict[str, Any], root: Path, run: Run = test_commands.execute,
                  which: Callable[[str], str | None] = shutil.which) -> dict[str, Any]:
    """Run one test group under a coverage tool. {status: ok | needs-setup | error | unsupported, files, reason}."""
    runner = group.get("runner")
    if runner not in ("dotnet", "python", "gradle", "node"):
        return {"status": "unsupported", "reason": "No coverage tool is wired up for this framework yet."}
    with tempfile.TemporaryDirectory(prefix="orchestrator-linecov-") as tmp:
        out = Path(tmp)
        if runner == "gradle":
            return _measure_gradle(group, root, run, out)
        if runner == "node":
            return _measure_node(group, root, run, out)
        if runner == "dotnet":
            if not which("dotnet"):
                return {"status": "needs-setup", "reason": "dotnet isn't installed or isn't on PATH"}
            code, output, timed_out = run(f'dotnet test "{group["project"]}" --nologo --collect:"XPlat Code Coverage" --results-directory {shlex.quote(str(out))}',
                                          root, TIMEOUT)
            reports = sorted(out.rglob("coverage.cobertura.xml"))
            if timed_out:
                return {"status": "error", "reason": f"stopped after {TIMEOUT // 60} minutes"}
            if not reports:
                reason = ("coverlet.collector isn't in this test project. Add it with: dotnet add package coverlet.collector"
                          if code == 0 else _reason_from(output))
                return {"status": "needs-setup", "reason": reason}
            files: FileLines = {}
            for report in reports:
                merge(files, parse_cobertura(report.read_text(encoding="utf-8", errors="ignore"), root))
            return {"status": "ok", "files": files, "tests_passed": code == 0}
        # Python: coverage.py around the same command the group runs
        probe = run("python3 -m coverage --version", root, 60)
        if probe[0] != 0:
            return {"status": "needs-setup", "reason": "coverage isn't installed (pip install coverage)"}
        data = out / "group.cov"
        base = f"python3 -m coverage run --data-file={shlex.quote(str(data))} --source=."
        if group.get("isolate") and group.get("files"):
            script = out / "isolated.py"
            script.write_text(test_groups.UNITTEST_ISOLATED + "\n", encoding="utf-8")  # the same runner the group's own command uses
            stems = " ".join(f[:-3] for f in sorted(group["files"]) if f.endswith(".py"))
            command = f"{base} {shlex.quote(str(script))} {shlex.quote(group['folder'])} {stems}"
        elif group.get("framework") == "pytest":
            command = f"{base} -m pytest {shlex.quote(group['folder'])}"
        else:
            command = f"{base} -m unittest discover -s {shlex.quote(group['folder'])}"
        code, output, timed_out = run(command, root, TIMEOUT)
        if timed_out:
            return {"status": "error", "reason": f"stopped after {TIMEOUT // 60} minutes"}
        report = out / "group.json"
        run(f"python3 -m coverage json --data-file={shlex.quote(str(data))} -o {shlex.quote(str(report))} -q", root, 300)
        if not report.is_file():
            return {"status": "error", "reason": "the tests ran but no coverage data was written"}
        return {"status": "ok", "files": parse_coverage_json(report.read_text(encoding="utf-8", errors="ignore"), root), "tests_passed": code == 0}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:400_000]
    except OSError:
        return ""


def _measure_gradle(group: dict[str, Any], root: Path, run: Run, out: Path) -> dict[str, Any]:
    """Unit tests of one Gradle module, with the coverage report its build already knows how to write."""
    if group.get("gradle_kind") != "unit":
        return {"status": "unsupported", "reason": "Tests that run on a phone or emulator aren't measured for coverage here."}
    module = root / group["folder"]
    build_text = _read(module / "build.gradle") + _read(module / "build.gradle.kts")
    path = group["project"] if group.get("project") not in (None, ".") else ""
    base = group["command"].rsplit(" ", 1)[0]
    if group.get("android") and "enableUnitTestCoverage" in build_text:
        task, reports = "createDebugUnitTestCoverageReport", module / "build" / "reports" / "coverage"
    elif not group.get("android") and "jacoco" in build_text:
        task, reports = "jacocoTestReport", module / "build" / "reports" / "jacoco"
    else:
        how = "set enableUnitTestCoverage = true in the debug build type" if group.get("android") else "apply the jacoco plugin"
        return {"status": "needs-setup", "reason": f"This module's build doesn't write a coverage report yet. In its build.gradle, {how}, then measure again."}
    if group.get("android"):
        command = f"{base} {path}:{task}"
    else:
        command = f"{base} {path}:test {path}:{task}" if path else f"{base} test {task}"
    code, output, timed_out = run(command, root, TIMEOUT)
    if timed_out:
        return {"status": "error", "reason": f"stopped after {TIMEOUT // 60} minutes"}
    files: FileLines = {}
    for report in (sorted(reports.rglob("*.xml"))[:30] if reports.is_dir() else []):
        merge(files, parse_jacoco(_read(report), root, module))
    if not files:
        return {"status": "needs-setup", "reason": _reason_from(output) if code else "the coverage report wasn't written or had no source files from this module"}
    return {"status": "ok", "files": files, "tests_passed": code == 0}


def _measure_node(group: dict[str, Any], root: Path, run: Run, out: Path) -> dict[str, Any]:
    """Jest or Vitest run once with an lcov report. Nothing is downloaded: the tool has to be installed in the project."""
    tool = group.get("tool")
    if tool not in ("jest", "vitest"):
        return {"status": "unsupported", "reason": "Coverage is measured for Jest and Vitest. This package's test tool isn't one of them."}
    folder_rel = group.get("folder") or "."
    folder = root / folder_rel
    if not ((folder / "node_modules").is_dir() or (root / "node_modules").is_dir()):
        return {"status": "needs-setup", "reason": "The packages aren't installed here. Run npm install (or your package manager's install) first."}
    try:
        package = json.loads(_read(folder / "package.json") or "{}")
    except ValueError:
        package = {}
    deps = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
    if tool == "vitest" and not any(d in deps for d in ("@vitest/coverage-v8", "@vitest/coverage-istanbul")):
        return {"status": "needs-setup", "reason": "Vitest needs a coverage provider. Add one with npm i -D @vitest/coverage-v8, then measure again."}
    where = f"cd {shlex.quote(folder_rel)} && " if folder_rel != "." else ""
    destination = shlex.quote(str(out))
    if tool == "vitest":
        command = f"{where}npx --no-install vitest run --coverage --coverage.reporter=lcov --coverage.reportsDirectory={destination}"
    else:
        command = f"{where}npx --no-install jest --coverage --coverageReporters=lcov --coverageDirectory={destination}"
    code, output, timed_out = run(command, root, TIMEOUT)
    if timed_out:
        return {"status": "error", "reason": f"stopped after {TIMEOUT // 60} minutes"}
    report = next(iter(sorted(out.rglob("lcov.info"))), None)
    if report is None:
        return {"status": "needs-setup", "reason": _reason_from(output) if code else "the tests ran but no coverage report was written"}
    return {"status": "ok", "files": parse_lcov(_read(report), folder, root), "tests_passed": code == 0}


def _reason_from(output: str) -> str:
    from orchestrator import test_probe
    return test_probe._first_error(output)


def measure(groups: list[dict[str, Any]], root: Path, *, blocked: set[str] | None = None, run: Run = test_commands.execute,
            which: Callable[[str], str | None] = shutil.which, log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    """Measure every group a tool can measure, skipping the ones a check found can't run here, and combine the files."""
    total: FileLines = {}
    notes: dict[str, dict[str, Any]] = {}
    for group in groups:
        if not group.get("toggle"):
            continue
        label = f"{group['framework']} {group.get('folder', '')}".strip()
        if blocked and group["id"] in blocked:
            notes[group["id"]] = {"status": "skipped", "reason": "a check found it can't run here yet"}
            log(f"Skipping {label}: it can't run here yet")
            continue
        log(f"Measuring {label} …")
        result = measure_group(group, root, run, which)
        notes[group["id"]] = {k: v for k, v in result.items() if k != "files"}
        notes[group["id"]]["label"] = label
        if result["status"] == "ok":
            merge(total, result["files"])
            log(f"  {len(result['files'])} files measured" + ("" if result.get("tests_passed") else " (some tests failed, so coverage may be understated)"))
        else:
            log(f"  {result.get('reason')}")
    return {"at": time.time(), "files": {p: [len(c), len(e)] for p, (c, e) in total.items()}, "groups": notes}


def read_cache(runtime_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((runtime_dir / CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(runtime_dir: Path, measurement: dict[str, Any]) -> None:
    path = runtime_dir / CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(measurement), encoding="utf-8")


def apply(analysis: dict[str, Any], measurement: dict[str, Any]) -> dict[str, Any]:
    """The platform analysis with each platform's measured line coverage: lines covered out of lines that can be covered,
    over the files the tests loaded. A platform nothing measured gets none."""
    files = measurement.get("files") or {}
    if not files:
        return {**analysis, "measured_at": None}
    platforms = analysis["platforms"]
    spec = [{"paths": p["paths"]} for p in platforms]
    totals: dict[int, list[int]] = {}
    per_file: dict[int, list[dict[str, Any]]] = {}
    for path, (covered, every) in files.items():
        if platform_view._is_test_path(path):
            continue
        owner = platform_view._platform_of(path, spec)
        if owner is None:
            continue
        row = totals.setdefault(owner, [0, 0, 0])
        row[0] += covered
        row[1] += every
        row[2] += 1
        if every > covered:
            per_file.setdefault(owner, []).append({"path": path, "uncovered": every - covered, "lines": every})
    out = []
    for index, platform in enumerate(platforms):
        row = totals.get(index)
        measured = None
        if row and row[1]:
            gaps = sorted(per_file.get(index, []), key=lambda g: -g["uncovered"])[:platform_view.GAP_FILES]
            measured = {"covered": row[0], "lines": row[1], "files": row[2], "percent": round(100 * row[0] / row[1]), "gaps": gaps}
        out.append({**platform, "measured": measured})
    return {**analysis, "platforms": out, "measured_at": measurement.get("at"), "measured_groups": measurement.get("groups") or {}}
