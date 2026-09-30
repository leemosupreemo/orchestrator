#!/usr/bin/env python3
"""Test case library: the project's catalogue of what must be tested, kept in
the repo next to the code (default `docs/test-cases/`, override with
`test_case_library` in .orchestrator/project.json).

Planners must emit `test_cases` for every feature, bug and coverage job, one
or more per acceptance criterion. Each case gets a stable id `TC-<issue>-<nn>`
and is stored as one JSON file, `<library>/<area>/<id>.json`, so parallel jobs
never conflict and cases are reviewed in the job's PR.

A case is *covered* when its id appears in a test file (the builder puts it in
a comment on the test that implements it); that marker, not anything the model
says, is the source of truth. Cases of type `manual` are checks a person runs
on a device and are reported separately.

    orchestrator testcases list [--area A] [--status covered|planned|unassigned|manual] [--json]
    orchestrator testcases show TC-141-01
    orchestrator testcases check <job.json>     # exit 1 if an automated case has no test
    orchestrator testcases manual [--area A]    # printable manual checklist

This module has no orchestrator imports so the web UI can use it directly.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_LIBRARY = "docs/test-cases"
CASE_TYPES = ("unit", "integration", "ui", "manual")
PRIORITIES = ("high", "medium", "low")
CASE_ID_RE = re.compile(r"\bTC-\d+-\d{2,}\b")
# Where test code lives, by convention across the stacks the orchestrator supports.
TEST_FILE_RE = re.compile(
    r"(Tests?\.swift|Spec\.swift|^test_.*\.py|_test\.py|_test\.go|\.test\.[jt]sx?|\.spec\.[jt]sx?|Tests?\.kt|Test\.java|_test\.rs)$"
)
TEST_DIR_HINTS = ("test", "tests", "spec", "specs", "__tests__")
SKIP_DIRS = {".git", ".build", ".swiftpm", "DerivedData", "node_modules", "Pods", "build", "dist", "target",
             ".orchestrator", "vendor", "__pycache__"}


class TestCaseError(ValueError):
    pass


# --------------------------------------------------------------------------- normalizing planner output


def _text(value: Any) -> str:
    return str(value or "").strip()


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [_text(v) for v in (value or []) if _text(v)]


def slug(area: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", area.lower()).strip("-") or "general"


def normalize_cases(raw: Any, issue_number: Any, source_job: str | None = None,
                    existing: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """Validates planner-emitted cases and gives each a stable id.

    Ids already present (a re-plan that kept a case) are kept; new cases are
    numbered after the highest existing `TC-<issue>-NN`.
    """
    if not isinstance(raw, list):
        raise TestCaseError("test_cases must be a list")
    prefix = f"TC-{issue_number}-"
    taken = [c["id"] for c in existing if str(c.get("id", "")).startswith(prefix)]
    taken += [str(c.get("id")) for c in raw if isinstance(c, dict) and str(c.get("id", "")).startswith(prefix)]
    counter = max([int(i.rsplit("-", 1)[1]) for i in taken if i.rsplit("-", 1)[1].isdigit()] or [0])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    cases = []
    for item in raw:
        if not isinstance(item, dict):
            raise TestCaseError("each test case must be an object")
        title = _text(item.get("title"))
        expected = _text(item.get("expected"))
        if not title or not expected:
            raise TestCaseError("each test case needs a title and an expected result")
        case_type = _text(item.get("type")).lower() or "unit"
        if case_type not in CASE_TYPES:
            case_type = "integration" if case_type in ("e2e", "end-to-end", "functional") else "unit"
        priority = _text(item.get("priority")).lower()
        case_id = _text(item.get("id"))
        if not case_id.startswith(prefix):
            counter += 1
            case_id = f"{prefix}{counter:02d}"
        task = item.get("task")
        cases.append({
            "id": case_id,
            "area": _text(item.get("area")) or "General",
            "title": title,
            "type": case_type,
            "priority": priority if priority in PRIORITIES else "medium",
            "preconditions": _list(item.get("preconditions")),
            "steps": _list(item.get("steps")),
            "expected": expected,
            "covers": _list(item.get("covers")),
            # Unit/integration test(s) or test group (suite, test plan) that cover this case.
            "tests": _list(item.get("tests")) if case_type != "manual" else [],
            "task": task if isinstance(task, int) and task > 0 else None,
            "source_job": source_job or item.get("source_job"),
            "created": item.get("created") or now,
            "updated": now,
        })
    return cases


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def acceptance_criteria(plan: dict[str, Any]) -> list[str]:
    criteria = _list(plan.get("acceptance_criteria"))
    for task in plan.get("tasks") or []:
        if isinstance(task, dict):
            criteria += _list(task.get("acceptance_criteria"))
    seen, unique = set(), []
    for c in criteria:
        if _norm(c) not in seen:
            seen.add(_norm(c))
            unique.append(c)
    return unique


def uncovered_criteria(plan: dict[str, Any]) -> list[str]:
    """Acceptance criteria that no test case claims to cover.

    Matching is on normalized text, allowing a case to quote part of a
    criterion or vice versa, since planners paraphrase slightly."""
    covers = [_norm(c) for case in plan.get("test_cases") or [] if isinstance(case, dict) for c in _list(case.get("covers"))]
    missing = []
    for criterion in acceptance_criteria(plan):
        n = _norm(criterion)
        if not any(n == c or (len(c) >= 12 and (c in n or n in c)) for c in covers):
            missing.append(criterion)
    return missing


def plan_problems(plan: dict[str, Any]) -> list[str]:
    """Why a plan's test cases aren't acceptable yet; empty when they are."""
    raw = plan.get("test_cases")
    if not isinstance(raw, list) or not raw:
        return ["The plan has no `test_cases`. Every feature, bug fix and coverage job must list them."]
    try:
        normalize_cases(raw, 0)
    except TestCaseError as exc:
        return [f"`test_cases` is malformed: {exc}."]
    problems = []
    uncovered = uncovered_criteria(plan)
    if uncovered:
        problems.append("These acceptance criteria are not covered by any test case (list them in a case's `covers`):\n"
                        + "\n".join(f"- {c}" for c in uncovered))
    unassigned = [c.get("title") for c in raw if isinstance(c, dict)
                  and _text(c.get("type")).lower() != "manual" and not _list(c.get("tests"))]
    if unassigned:
        problems.append("These automated test cases have no `tests` assigned (name the suite, test, or test plan "
                        "that will cover each one):\n" + "\n".join(f"- {t}" for t in unassigned))
    if all(c.get("type") == "manual" for c in raw if isinstance(c, dict)):
        problems.append("Every test case is `manual`; at least the core behaviour must have an automated case.")
    return problems


def replan_request(problems: list[str]) -> str:
    return ("### TEST CASES REQUIRED ###\nYour plan was rejected by the orchestrator's test-case check:\n"
            + "\n".join(problems)
            + "\n\nReturn the complete plan JSON again with a `test_cases` array that fixes this, "
              "keeping everything else that was right. Schema:\n" + TEST_CASE_SCHEMA + "\n")


# --------------------------------------------------------------------------- library on disk


def library_dir(root: Path) -> Path:
    config_path = root / ".orchestrator" / "project.json"
    try:
        configured = json.loads(config_path.read_text(encoding="utf-8")).get("test_case_library")
    except (OSError, json.JSONDecodeError):
        configured = None
    return root / (configured or DEFAULT_LIBRARY)


def case_path(root: Path, case: dict[str, Any]) -> Path:
    return library_dir(root) / slug(case["area"]) / f"{case['id']}.json"


def load_library(root: Path) -> list[dict[str, Any]]:
    base = library_dir(root)
    if not base.is_dir():
        return []
    cases = []
    for path in sorted(base.rglob("TC-*.json")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(case, dict) and case.get("id"):
            case["_file"] = str(path.relative_to(root))
            cases.append(case)
    return cases


def write_cases(root: Path, cases: list[dict[str, Any]]) -> list[Path]:
    """Writes each case to its own file, removing a stale copy if its area moved."""
    existing = {c["id"]: root / c["_file"] for c in load_library(root)}
    written = []
    for case in cases:
        path = case_path(root, case)
        old = existing.get(case["id"])
        if old and old != path and old.exists():
            old.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in case.items() if not k.startswith("_")}
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
        written.append(path)
    readme = library_dir(root) / "README.md"
    if not readme.exists():
        readme.write_text(LIBRARY_README, encoding="utf-8")
        written.append(readme)
    return written


LIBRARY_README = """# Test case library

What this project must be tested for, one JSON file per case under `<area>/<id>.json`.
Cases are written by the orchestrator's planner for every feature, bug fix and
coverage job and reviewed in that job's pull request.

- `type`: `unit`, `integration`, `ui` (automated) or `manual` (a person checks it on a device).
- A case is **covered** when its id (for example `TC-141-01`) appears in a comment in
  the test that implements it. The orchestrator checks this after every build and sends
  the job back to be fixed if an automated case has no test.

Browse it with `orchestrator testcases list`, or on the Tests page of `orchestrator ui`.
"""


# --------------------------------------------------------------------------- coverage
#
# Each automated case names the test(s) or test group that cover it in `tests`:
# a suite ("LobbyRejoinTests"), one test ("LobbyRejoinTests/testKeepsSeat" or
# "test_keeps_seat"), or a test plan / group ("Multiplayer"). Status:
#   covered    - an assigned test exists in the code, or a test carries the case id
#   planned    - tests are assigned but none of them exist yet
#   unassigned - no test assigned and none carries the id  (called out everywhere)
#   manual     - a person checks it on a device

STATUSES = ("covered", "planned", "unassigned", "manual")
_SUITE_RE = re.compile(r"^\s*(?:@Suite[^\n]*\n\s*)?(?:final\s+|public\s+|private\s+|internal\s+)*(?:class|struct)\s+([A-Za-z_]\w*)", re.M)
_METHOD_RES = (
    re.compile(r"\bfunc\s+(test\w*)\s*\("),                    # XCTest
    re.compile(r"@Test\b[^\n]*\n?\s*(?:@\w+[^\n]*\n\s*)*func\s+(\w+)\s*\("),  # Swift Testing
    re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(", re.M),  # pytest / unittest
    re.compile(r"\bfunc\s+(Test\w+)\s*\(\s*t\s+\*testing"),    # Go
    re.compile(r"\b(?:it|test)\(\s*['\"]([^'\"]+)['\"]"),       # JS/TS
    re.compile(r"#\[test\]\s*(?:#\[[^\]]*\]\s*)*fn\s+(\w+)"),   # Rust
)


def _is_test_file(path: Path, root: Path) -> bool:
    if TEST_FILE_RE.search(path.name):
        return True
    parts = {p.lower() for p in path.relative_to(root).parts[:-1]}
    return bool(parts & set(TEST_DIR_HINTS)) or any(p.endswith("tests") for p in parts)


def scan_tests(root: Path) -> dict[str, Any]:
    """One pass over the test code: case-id markers, suites, test methods and test plans."""
    library = library_dir(root)
    markers: dict[str, list[str]] = {}
    suites: dict[str, str] = {}          # suite name -> file
    methods: dict[str, str] = {}         # "Suite/method" and bare "method" -> file
    plans: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                       and not d.endswith((".xcodeproj", ".xcworkspace", ".xcassets"))]
        here = Path(dirpath)
        if here == library or library in here.parents:
            continue
        for name in filenames:
            path = here / name
            if path.suffix == ".xctestplan":
                plans.add(path.stem)
                continue
            if path.suffix not in (".swift", ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".kt", ".java", ".rs", ".m"):
                continue
            if not _is_test_file(path, root):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel = str(path.relative_to(root))
            if "TC-" in text:
                for lineno, line in enumerate(text.splitlines(), 1):
                    for case_id in CASE_ID_RE.findall(line):
                        markers.setdefault(case_id, []).append(f"{rel}:{lineno}")
            file_suites = _SUITE_RE.findall(text) or [path.stem]
            for suite in file_suites:
                suites.setdefault(suite, rel)
            suites.setdefault(path.stem, rel)
            for pattern in _METHOD_RES:
                for method in pattern.findall(text):
                    methods.setdefault(method, rel)
                    for suite in file_suites:
                        methods.setdefault(f"{suite}/{method}", rel)
    return {"markers": markers, "suites": suites, "methods": methods, "plans": plans}


def resolve_test_ref(ref: str, index: dict[str, Any]) -> str | None:
    """Where an assigned test reference exists, or None. Accepts `Target/Suite/method`,
    `Suite/method`, `Suite`, `method`, a test plan name, or a file path."""
    ref = ref.strip().strip("`")
    if not ref:
        return None
    if ref in index["plans"]:
        return f"test plan {ref}"
    parts = [p for p in re.split(r"[/.:]{1,2}", ref) if p and p not in ("swift", "py", "ts", "js")]
    candidates = [ref, "/".join(parts[-2:]), parts[-1] if parts else ref]
    for c in candidates:
        if c in index["methods"]:
            return index["methods"][c]
        if c in index["suites"]:
            return index["suites"][c]
    return None


def case_status(case: dict[str, Any], index: dict[str, Any]) -> dict[str, Any]:
    if case.get("type") == "manual":
        return {"status": "manual", "tests": [], "assigned": case.get("tests") or []}
    assigned = [t for t in case.get("tests") or [] if str(t).strip()]
    found = [f"{ref} → {where}" for ref in assigned if (where := resolve_test_ref(str(ref), index))]
    marked = index["markers"].get(case["id"], [])
    if found or marked:
        return {"status": "covered", "tests": found + marked, "assigned": assigned}
    return {"status": "planned" if assigned else "unassigned", "tests": [], "assigned": assigned}


def coverage(root: Path, cases: list[dict[str, Any]], index: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    index = scan_tests(root) if index is None else index
    return {case["id"]: case_status(case, index) for case in cases}


def summarize(cases: list[dict[str, Any]], cov: dict[str, dict[str, Any]]) -> dict[str, Any]:
    counts = {s: sum(1 for c in cases if cov[c["id"]]["status"] == s) for s in STATUSES}
    automated = len(cases) - counts["manual"]
    return {**counts, "total": len(cases), "automated": automated,
            "covered_pct": round(100.0 * counts["covered"] / automated, 1) if automated else None}


def cases_by_test(cases: list[dict[str, Any]], cov: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Suite / test plan name -> ids of the cases it covers (for the Tests page)."""
    mapping: dict[str, list[str]] = {}
    for case in cases:
        for ref in cov[case["id"]]["assigned"]:
            parts = [p for p in re.split(r"[/.:]{1,2}", str(ref)) if p]
            group = parts[-2] if len(parts) >= 2 else (parts[0] if parts else str(ref))
            mapping.setdefault(group, []).append(case["id"])
    return mapping


def job_cases(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in (job.get("plan") or {}).get("test_cases") or [] if isinstance(c, dict) and c.get("id")]


def due_cases(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Cases that should have tests by now: all of them, except in a multi-task
    feature where only finished tasks' cases (and untasked ones at the end) count."""
    cases = job_cases(job)
    tasks = (job.get("plan") or {}).get("tasks") or []
    if job.get("type") != "feature-plan" or not tasks:
        return cases
    done = {i + 1 for i in job.get("completed_task_indices") or []}
    all_done = len(done) >= len(tasks)
    return [c for c in cases if (c.get("task") in done) or (c.get("task") is None and all_done)]


def missing_tests(root: Path, job: dict[str, Any]) -> list[dict[str, Any]]:
    """Due automated cases that aren't covered, each with its status attached."""
    due = [c for c in due_cases(job) if c.get("type") != "manual"]
    if not due:
        return []
    cov = coverage(root, due)
    return [dict(c, _status=cov[c["id"]]["status"]) for c in due if cov[c["id"]]["status"] != "covered"]


def missing_tests_feedback(missing: list[dict[str, Any]]) -> str:
    lines = ["These planned test cases have no test in the code yet. Write the assigned test for each (or, if none "
             "is assigned, add one to the suite where it fits), check the expected result, and put the case id in "
             "a comment on the test (for example `// TC-141-01`):"]
    for c in missing:
        where = f" — write: {', '.join(c.get('tests') or [])}" if c.get("tests") else " — no test assigned; choose a suite"
        lines.append(f"- {c['id']} ({c['type']}): {c['title']} — expected: {c['expected']}{where}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- prompt text

TEST_CASE_SCHEMA = """  "test_cases": [
    {
      "id": "string (OPTIONAL: keep the id when revising an existing case; omit for new cases)",
      "area": "string (feature area, e.g. Lobby, Checkout; reuse an existing area name when one fits)",
      "title": "string (what is being verified, one line)",
      "type": "unit|integration|ui|manual",
      "priority": "high|medium|low",
      "preconditions": ["string"],
      "steps": ["string"],
      "expected": "string (the observable result that makes this pass)",
      "covers": ["string (the acceptance criterion text this case verifies, copied verbatim)"],
      "tests": ["string (REQUIRED unless manual: the unit/integration test or test group that covers it — a suite like LobbyRejoinTests, a test like LobbyRejoinTests/testKeepsSeat, or a test plan like Multiplayer; existing or to be written, following the project's naming)"],
      "task": "integer (OPTIONAL, feature plans only: 1-based number of the task that implements it)"
    }
  ]"""

PLANNER_INSTRUCTIONS = f"""
### TEST CASES (MANDATORY — enforced by the orchestrator)
Your plan JSON MUST include a `test_cases` array, added alongside the other fields:
{TEST_CASE_SCHEMA}

Rules:
- Every acceptance criterion must be covered by at least one case; put the criterion's exact text in `covers`.
- Cover the happy path, edge cases (empty, limits, timing, concurrency) and failure/error paths. For a bug, include a
  regression case that fails before the fix and passes after.
- Prefer automated types (`unit`, `integration`, `ui`) that fit the project's existing test framework. Use `manual`
  only for what cannot be automated (real devices, hardware, subjective visuals); manual cases need clear steps.
- Tie every automated case to real unit or integration tests via `tests`: reuse an existing suite or test when it
  already covers the behaviour, otherwise name the new test the builder should write (in the suite it belongs to).
  Unit cases go in unit suites; integration cases in integration suites/test plans.
- Keep cases concrete and independently checkable; the builder implements or extends the assigned tests.
- Plans without adequate test cases are sent back to you.
"""

VERIFIER_INSTRUCTIONS = """
### TEST CASE REVIEW
The plan's `test_cases` define what will be tested. Check that they cover every acceptance criterion, include edge and
failure cases, are realistic for this codebase's test setup, and aren't `manual` when they could be automated. Raise
`concerns` (with the missing cases in `suggested_additions`) when coverage is inadequate.
"""



def prompt_block(cases: list[dict[str, Any]]) -> str:
    if not cases:
        return ""
    lines = ["Test cases to implement (from the test case library):",
             "Write an automated test for every case not marked manual, and put its id in a comment on that test "
             "(e.g. `// TC-141-01` or `# TC-141-01`). The orchestrator checks for these ids after the build.", ""]
    for c in cases:
        lines.append(f"- {c['id']} [{c['type']}, {c['priority']}] {c['title']}")
        if c.get("preconditions"):
            lines.append(f"  Given: {'; '.join(c['preconditions'])}")
        if c.get("steps"):
            lines.append(f"  Steps: {'; '.join(c['steps'])}")
        lines.append(f"  Expected: {c['expected']}")
        if c.get("tests"):
            lines.append(f"  Covered by: {', '.join(c['tests'])}")
    return "\n".join(lines) + "\n"


def issue_section(cases: list[dict[str, Any]]) -> list[str]:
    if not cases:
        return []
    lines = ["", "## Test Cases"]
    for c in cases:
        tests = f" — _tests:_ {', '.join(c['tests'])}" if c.get("tests") else (" — ⚠️ _no test assigned_" if c["type"] != "manual" else "")
        lines.append(f"- **{c['id']}** ({c['type']}, {c['priority']}) {c['title']} — _expected:_ {c['expected']}{tests}")
    return lines


def library_index(root: Path, limit: int = 150) -> str:
    """Compact list of existing cases for the planner, so it extends rather than duplicates."""
    cases = load_library(root)
    if not cases:
        return ""
    lines = ["Existing test case library (reuse or extend; don't duplicate):"]
    for c in cases[-limit:]:
        lines.append(f"- {c['id']} [{c.get('area')}/{c.get('type')}] {c.get('title')}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- CLI


def _root() -> Path:
    from orchestrator.project_config import find_project_root

    return find_project_root()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator testcases", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    list_p = sub.add_parser("list", help="List the library with coverage")
    list_p.add_argument("--area")
    list_p.add_argument("--status", choices=list(STATUSES))
    list_p.add_argument("--json", action="store_true")
    sub.add_parser("show", help="Show one case").add_argument("id")
    sub.add_parser("check", help="Exit 1 if a job's automated cases lack tests").add_argument("job_file")
    manual_p = sub.add_parser("manual", help="Print the manual checklist")
    manual_p.add_argument("--area")
    args = parser.parse_args(argv)

    root = _root()
    if args.action == "check":
        job = json.loads(Path(args.job_file).read_text(encoding="utf-8"))
        missing = missing_tests(root, job)
        if missing:
            print(missing_tests_feedback(missing))
            return 1
        print(f"All {len(job_cases(job))} test cases for this job are covered or manual.")
        return 0

    cases = load_library(root)
    if getattr(args, "area", None):
        cases = [c for c in cases if slug(c.get("area", "")) == slug(args.area)]
    if args.action == "show":
        case = next((c for c in cases if c["id"] == args.id), None)
        if not case:
            print(f"No test case {args.id}.")
            return 1
        case["coverage"] = coverage(root, [case])[case["id"]]
        print(json.dumps(case, indent=2))
        return 0
    if args.action == "manual":
        for c in (c for c in cases if c.get("type") == "manual"):
            print(f"[ ] {c['id']}  {c['title']}")
            for step in c.get("steps", []):
                print(f"      - {step}")
            print(f"      Expect: {c['expected']}\n")
        return 0

    cov = coverage(root, cases)
    rows = [dict(c, coverage=cov[c["id"]]) for c in cases if not args.status or cov[c["id"]]["status"] == args.status]
    if args.json:
        print(json.dumps(rows))
        return 0
    for c in rows:
        tests = ", ".join(c["coverage"]["assigned"]) or ("—" if c.get("type") == "manual" else "NO TEST ASSIGNED")
        print(f"{c['id']:12} {c['coverage']['status']:10} {c.get('area', ''):16} {c['title']}  [{tests}]")
    totals = summarize(cases, cov)
    print(f"\n{totals['total']} cases: {totals['covered']} covered, {totals['planned']} with a test still to write, "
          f"{totals['unassigned']} with no test assigned, {totals['manual']} manual")
    if totals["unassigned"]:
        print(f"\033[93m⚠ {totals['unassigned']} automated case(s) have no unit/integration test assigned:\033[0m")
        for c in cases:
            if cov[c["id"]]["status"] == "unassigned":
                print(f"  - {c['id']} {c['title']}")
    return 1 if totals["unassigned"] and args.status is None else 0


if __name__ == "__main__":
    raise SystemExit(main())
