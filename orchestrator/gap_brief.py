"""A ready-made brief for "Add tests" on one untested file, so the test job starts from the file's own facts.

The Tests page lists, per platform, the biggest files no test mentions (orchestrator/platforms.py). Its "Add tests" used to open
a test job with only "Add tests for <path>" filled in, so the planner rediscovered everything. `brief` reads the file and the
tests around it, without running anything, and prefills the job:

  area        the product area the file belongs to (Features/Lobby/... -> Lobby), matched to an area the test case library uses
  summary     "Add tests for PlayerSlotsView (Lobby)"
  spec        what the file is (its types and functions, what kind of code), how to test that kind of code, the existing tests
              to copy (the closest test files and their framework), where the new tests go, the command that runs them,
              when it is done, and what not to do
  test_cases  library cases for the same area, or that name the file's types
  feature     the feature whose paths include the file

Pure apart from reading the project; callers pass the platform, the test cases and the features in.
"""
from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from orchestrator.platforms import MIN_NAME, SKIP_DIRS, SOURCE_LANGUAGES, TEST_FILE, TYPE_NAME, WORD, _is_test_path

MAX_READ = 400_000
MAX_SCAN = 6_000
MAX_EXAMPLES = 3
MAX_CASES = 8
MAX_FUNCTIONS = 12
MAX_CALLERS = 5
MAX_BULK = 40
GENERIC_DIRS = {"sources", "source", "src", "lib", "app", "components", "component", "views", "view", "ui", "core", "common",
                "shared", "utils", "util", "helpers", "internal", "main", "java", "kotlin", "swift", "scripts", "features", "modules"}

FUNCTION = {
    ".swift": re.compile(r"^\s*(?:@\w+\s+)*(?:(?:public|internal|private|fileprivate|open|static|class|mutating|override|final|nonisolated)\s+)*func\s+([A-Za-z_]\w*)", re.M),
    ".py": re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z]\w*)", re.M),
    ".cs": re.compile(r"^\s*(?:public|internal|protected)\s+(?:static\s+|virtual\s+|override\s+|async\s+)*[\w<>\[\],.?]+\s+([A-Z]\w*)\s*\(", re.M),
    ".kt": re.compile(r"^\s*(?:(?:public|internal|private|override|suspend)\s+)*fun\s+([A-Za-z_]\w*)", re.M),
    ".java": re.compile(r"^\s*(?:public|protected)\s+(?:static\s+)?[\w<>\[\],.?]+\s+([a-z]\w*)\s*\(", re.M),
    ".ts": re.compile(r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)|^\s*(?:public\s+|static\s+)*([a-z]\w*)\s*\([^)]*\)\s*[:{]", re.M),
    ".js": re.compile(r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)", re.M),
    ".go": re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Z]\w*)", re.M),
    ".rs": re.compile(r"^\s*pub\s+(?:async\s+)?fn\s+([a-z_]\w*)", re.M),
}
FUNCTION[".tsx"] = FUNCTION[".ts"]

FRAMEWORKS = [  # (marker in a test file, framework name)
    (re.compile(r"^\s*import\s+Testing\b", re.M), "Swift Testing (@Test, #expect)"),
    (re.compile(r"^\s*import\s+XCTest\b", re.M), "XCTest"),
    (re.compile(r"\bimport\s+pytest\b|^def\s+test_", re.M), "pytest"),
    (re.compile(r"\bunittest\.TestCase\b"), "unittest"),
    (re.compile(r"\bfrom\s+['\"]vitest['\"]"), "Vitest"),
    (re.compile(r"\b(?:describe|it|test)\s*\(\s*['\"]"), "Jest-style (describe/it)"),
    (re.compile(r"\[(?:Fact|Theory)\b"), "xUnit"),
    (re.compile(r"\[Test(?:Case)?\]"), "NUnit"),
    (re.compile(r"@Test\b"), "JUnit"),
    (re.compile(r"\bfunc\s+Test[A-Z]\w*\(t \*testing\.T\)"), "Go testing"),
    (re.compile(r"#\[test\]"), "Rust #[test]"),
]

# What kind of code a file is, and how to test that kind of code. First match wins.
ROLES: list[tuple[str, Any, str, list[str]]] = [
    ("visual effect", lambda p, t: bool(re.search(r"(Modifier|Transition|Effect|Animation|Sheen|Shimmer)s?\b", p)) or ": ViewModifier" in t,
     "A visual effect or modifier",
     ["Test the values it computes (durations, phases, offsets, opacity curves) by pulling that maths into small functions if needed.",
      "Don't test pixels or animations frame by frame. If nothing in it can be tested without rendering, say so in the plan instead of writing empty tests."]),
    ("view", lambda p, t: bool(re.search(r"(View|Screen|Cell|Sheet|Page|Component)\.(swift|tsx|jsx|kt|vue|svelte)$", p))
     or ": View" in t or "@Composable" in t or "React" in t,
     "A screen or UI component",
     ["Test the logic the view shows: computed labels, which slots or rows appear for which state, enabled and disabled actions, empty and full states.",
      "If that logic lives inside the view body, move it into a small view model or pure function first, keeping the change minimal, then test that.",
      "Use snapshot or UI tests only if the project already has them (see the examples below)."]),
    ("scene", lambda p, t: "Scene" in p or "SKScene" in t or "MonoBehaviour" in t,
     "Game scene or game-loop code",
     ["Test the state changes it triggers (who won, what celebration plays, when it ends) through functions that don't need the renderer.",
      "Don't start the real scene or engine in a unit test."]),
    ("service", lambda p, t: bool(re.search(r"/(Services?|Clients?|Networking|Api|Repositories|Stores?)/", p)) or "Log" in PurePosixPath(p).stem,
     "A service (logging, networking, storage or similar)",
     ["Test what it decides and formats (levels, filtering, message shape, retries, error mapping) with its outside dependency replaced by a fake.",
      "Never write to the real system log, network, keychain or disk from a test."]),
    ("model", lambda p, t: True,
     "Domain logic or a data model",
     ["Test its behaviour directly: creating values, equality and ordering, every case of its enums, edge values and invalid input.",
      "Cover the rules other code relies on, one test per rule, named after the rule."]),
]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:MAX_READ]
    except OSError:
        return ""


def area_of(path: str, known_areas: list[str] | None = None) -> str:
    """The product area a file belongs to: the folder after Features/ (or the nearest meaningful folder), matched to an area
    the test case library already uses when one has the same name."""
    parts = [p for p in PurePosixPath(path).parts[:-1]]
    lowered = [p.lower() for p in parts]
    guess = ""
    for marker in ("features", "feature", "modules", "screens", "scenes"):
        if marker in lowered and lowered.index(marker) + 1 < len(parts):
            guess = parts[lowered.index(marker) + 1]
            break
    if not guess:
        guess = next((p for p in reversed(parts[1:] or parts) if p.lower() not in GENERIC_DIRS), "") or (parts[-1] if parts else "General")
    for area in known_areas or []:
        if area.lower() == guess.lower():
            return area
    return guess


def describe_file(root: Path, path: str) -> dict[str, Any]:
    """What the file declares, from its text."""
    ext = PurePosixPath(path).suffix
    text = _read(root / path)
    types = list(dict.fromkeys(TYPE_NAME.get(ext, re.compile("$^")).findall(text)))[:8]
    pattern = FUNCTION.get(ext)
    functions = []
    if pattern:
        for match in pattern.finditer(text):
            name = next((g for g in match.groups() if g), "")
            if re.search(r"\b(?:private|fileprivate)\b", match.group(0)):
                continue  # a private helper isn't behaviour other code relies on
            if name and not name.startswith("_") and name not in functions and name not in ("body", "init", "main"):
                functions.append(name)
    imports = re.findall(r"^\s*import\s+([A-Za-z_][\w.]*)", text, re.M)[:10]
    role = next(r for r in ROLES if r[1](path, text))
    return {"path": path, "language": SOURCE_LANGUAGES.get(ext, ext.lstrip(".")), "lines": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
            "types": types, "functions": functions[:MAX_FUNCTIONS], "imports": imports, "role": role[0], "role_label": role[2], "approach": role[3]}


def _source_and_test_files(root: Path, paths: list[str]) -> tuple[list[str], list[str]]:
    """(source files, test files) under the platform's folders, or the whole project when it names none."""
    sources, tests, seen = [], [], 0
    for base in [root / p for p in paths] or [root]:
        if not base.is_dir():
            continue
        for folder, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(files):
                if PurePosixPath(name).suffix not in SOURCE_LANGUAGES:
                    continue
                seen += 1
                if seen > MAX_SCAN:
                    return sources, tests
                rel = (Path(folder) / name).relative_to(root).as_posix()
                (tests if _is_test_path(rel) or TEST_FILE.search(name) else sources).append(rel)
    return sources, tests


def _names(info: dict[str, Any]) -> set[str]:
    """The names a test or a caller would use for this file: its types and its file name, long enough to be specific."""
    return {n for n in info["types"] + [PurePosixPath(info["path"]).stem] if len(n) >= MIN_NAME}


def callers(root: Path, info: dict[str, Any], platform_paths: list[str]) -> list[dict[str, Any]]:
    """The other source files that use this file's types, most uses first: what the rest of the app relies on."""
    names = {n for n in info["types"] + [PurePosixPath(info["path"]).stem] if len(n) >= 4}  # "Card" is specific enough to a caller
    if not names:
        return []
    found, seen_in = [], {n: 0 for n in names}
    texts = []
    for rel in _source_and_test_files(root, platform_paths)[0]:
        if rel == info["path"]:
            continue
        words = WORD.findall(_read(root / rel))
        texts.append((rel, words))
        for n in names & set(words):
            seen_in[n] += 1
    # A name many files declare or use for their own purposes (CodingKeys, Content) says nothing about this file.
    specific = {n for n in names if seen_in[n] <= max(8, len(texts) // 10) or n == PurePosixPath(info["path"]).stem}
    for rel, words in texts:
        uses = sum(1 for w in words if w in specific)
        if uses:
            found.append({"path": rel, "uses": uses, "names": sorted(specific & set(words))})
    found.sort(key=lambda c: (-c["uses"], c["path"]))
    return found[:MAX_CALLERS]


def tested_by(root: Path, path: str, platform_paths: list[str]) -> list[str]:
    """The test files that mention this file's types or name: how the Tests page decides a file "has a test"."""
    if not (root / path).is_file():
        return []
    names = _names(describe_file(root, path))
    return [rel for rel in _source_and_test_files(root, platform_paths)[1] if names & set(WORD.findall(_read(root / rel)))]


def _test_files(root: Path, paths: list[str]) -> list[str]:
    """Test files under the platform's folders (or the whole project when it has none)."""
    out, seen = [], 0
    for base in [root / p for p in paths] or [root]:
        if not base.is_dir():
            continue
        for folder, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(files):
                if PurePosixPath(name).suffix not in SOURCE_LANGUAGES:
                    continue
                seen += 1
                if seen > MAX_SCAN:
                    return out
                rel = (Path(folder) / name).relative_to(root).as_posix()
                if _is_test_path(rel) or TEST_FILE.search(name):
                    out.append(rel)
    return out


def examples(root: Path, path: str, platform_paths: list[str]) -> list[dict[str, Any]]:
    """The existing test files closest to this file: ones that test its neighbours (types from the same folder), then ones whose
    path shares its folder names. Each with the framework it uses."""
    folder = root / PurePosixPath(path).parent
    neighbours: set[str] = set()
    if folder.is_dir():
        for sibling in sorted(folder.iterdir())[:60]:
            if sibling.is_file() and sibling.suffix in TYPE_NAME and sibling.name != PurePosixPath(path).name:
                neighbours |= set(TYPE_NAME[sibling.suffix].findall(_read(sibling))) | {sibling.stem}
    neighbours = {n for n in neighbours if len(n) >= 4}
    words = {p.lower() for p in PurePosixPath(path).parts[:-1] if p.lower() not in GENERIC_DIRS}
    scored = []
    for rel in _test_files(root, platform_paths):
        text = _read(root / rel)
        tokens = set(WORD.findall(text))
        score = 3 * len(tokens & neighbours) + sum(1 for w in words if w in rel.lower())
        if score:
            framework = next((name for marker, name in FRAMEWORKS if marker.search(text)), "")
            scored.append((score, rel, framework, sorted(tokens & neighbours)[:4]))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [{"path": rel, "framework": fw, "tests": names} for _, rel, fw, names in scored[:MAX_EXAMPLES]]


def test_folder(root: Path, path: str, platform_paths: list[str], found: list[dict[str, Any]]) -> str:
    """Where tests for this file belong: the test folder that mirrors its source folder (Thirteen/Services/Log.swift ->
    ThirteenTests/Unit/Services), else the folder its closest examples share."""
    folders = {str(PurePosixPath(t).parent) for t in _test_files(root, platform_paths)}
    parts = PurePosixPath(path).parts[1:-1]  # its folders below the app's own top folder
    for n in range(len(parts), 0, -1):
        tail = "/".join(parts[-n:]) if n == len(parts) else "/".join(parts[:n])
        hits = sorted((f for f in folders if f.endswith("/" + tail)), key=len)
        if hits:
            return hits[0]
    dirs = [PurePosixPath(e["path"]).parent.parts for e in found]
    if not dirs:
        return ""
    common = []
    for level in zip(*dirs):
        if len(set(level)) != 1:
            break
        common.append(level[0])
    return "/".join(common)


def matching_cases(cases: list[dict[str, Any]], area: str, info: dict[str, Any]) -> list[dict[str, Any]]:
    names = [n.lower() for n in info["types"] + [PurePosixPath(info["path"]).stem] if len(n) >= 4]
    out = []
    for case in cases:
        text = " ".join([str(case.get("title") or ""), *map(str, case.get("steps") or []), str(case.get("expected") or ""), *map(str, case.get("covers") or [])]).lower()
        if str(case.get("area") or "").lower() == area.lower() or any(n in text for n in names):
            out.append(case)
    return out[:MAX_CASES]


def feature_for(features: list[dict[str, Any]], path: str) -> dict[str, Any] | None:
    best = None
    for feature in features:
        for prefix in feature.get("paths") or []:
            prefix = str(prefix).strip().rstrip("/")
            if prefix and (path == prefix or path.startswith(prefix + "/")) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), feature)
    return best[1] if best else None


def brief(root: Path, path: str, *, platform: dict[str, Any] | None = None, test_command: str = "", cases: list[dict[str, Any]] | None = None,
          features: list[dict[str, Any]] | None = None, uncovered: int | None = None) -> dict[str, Any]:
    """Everything to prefill a test job for one untested file. `platform` is the Tests page's platform ({name, label, paths})."""
    clean = PurePosixPath(path).as_posix().lstrip("/")
    target = (root / clean).resolve()
    if ".." in PurePosixPath(clean).parts or not target.is_file() or root.resolve() not in target.parents:
        raise ValueError("That file isn't in this project.")
    cases = cases or []
    info = describe_file(root, clean)
    area = area_of(clean, sorted({str(c.get("area")) for c in cases if c.get("area")}))
    stem = PurePosixPath(clean).stem
    subject = stem if stem in info["types"] else info["types"][0] if info["types"] else stem  # the type the file is named after
    platform_paths = list((platform or {}).get("paths") or [])
    found = examples(root, clean, platform_paths)
    framework = next((e["framework"] for e in found if e["framework"]), "")
    where = test_folder(root, clean, platform_paths, found)
    picked = matching_cases(cases, area, info)
    feature = feature_for(features or [], clean)
    used_by = callers(root, info, platform_paths)

    lines = [f"FILE: {clean} ({info['lines']} lines of {info['language']}"
             + (f", {uncovered} of them never run by the tests" if uncovered else ", no test mentions it yet") + ")"]
    if platform:
        lines.append(f"PLATFORM: {platform.get('name', '')}" + (f" ({platform['label']})" if platform.get("label") else ""))
    lines.append(f"WHAT IT IS: {info['role_label']}."
                 + (f" Declares {', '.join(info['types'])}." if info["types"] else "")
                 + (f" Functions: {', '.join(info['functions'])}." if info["functions"] else "")
                 + (f" Imports {', '.join(info['imports'])}." if info["imports"] else ""))
    lines += ["", "HOW TO TEST IT:", *[f"- {a}" for a in info["approach"]]]
    if used_by:
        lines += ["", "USED BY (test first what these rely on):",
                  *[f"- {c['path']}: uses {', '.join(c['names'])} {c['uses']} time{'s' * (c['uses'] != 1)}" for c in used_by]]
    if found:
        lines += ["", "FOLLOW THESE EXISTING TESTS (same style, helpers and naming):",
                  *[f"- {e['path']}" + (f" ({e['framework']})" if e["framework"] else "") + (f": tests {', '.join(e['tests'])}" if e["tests"] else "") for e in found]]
    if where or framework:
        lines += ["", "PUT THE NEW TESTS:" + (f" in {where}/" if where else "") + (f"{',' if where else ''} using {framework}" if framework else "") + "."]
    if test_command:
        lines += ["", f"RUN WITH: {test_command}"]
    if feature:
        lines += ["", f"FEATURE: {feature.get('name', feature.get('id'))}" + (f": {feature['summary']}" if feature.get("summary") else "")]
    lines += ["", "DONE WHEN:",
              f"- Each public behaviour of {subject} has at least one test, named after the behaviour it checks.",
              "- Edge cases are covered: empty and full states, boundaries, invalid input.",
              f"- A test mentions {subject}, so the Tests page counts this file as tested.",
              "- The whole test suite passes.",
              "", "DON'T:",
              "- Change what the code does. Only add small seams (an injected dependency, an extracted pure function) when a test needs one.",
              "- Write tests that only check that code runs without asserting anything."]
    return {"type": "coverage", "path": clean, "area": area, "summary": f"Add tests for {subject} ({area})", "spec": "\n".join(lines),
            "test_cases": "\n".join(f"{c['id']}: {c.get('title', '')}" for c in picked), "feature": feature.get("id") if feature else "",
            "file": info, "examples": found, "framework": framework, "used_by": used_by, "targets": [clean],
            "platform": (platform or {}).get("name", "")}


def bulk(root: Path, paths: list[str], *, platform: dict[str, Any] | None = None, test_command: str = "",
         cases: list[dict[str, Any]] | None = None, features: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Briefs for many untested files at once: one combined job, and one per area. Files that can't be read are skipped."""
    briefs = []
    for path in paths[:MAX_BULK]:
        try:
            briefs.append(brief(root, path, platform=platform, test_command=test_command, cases=cases, features=features))
        except ValueError:
            continue
    if not briefs:
        raise ValueError("None of those files are in this project.")
    by_area: dict[str, list[dict[str, Any]]] = {}
    for b in briefs:
        by_area.setdefault(b["area"], []).append(b)
    name = (platform or {}).get("name", "")

    def combine(group: list[dict[str, Any]], title: str, area: str) -> dict[str, Any]:
        parts = [f"Add tests for these {len(group)} files" + (f" in {name}" if name else "") + ". Each has no test yet. Work through them one at a time; "
                 "each file is its own task, and the build and tests pass after each one."]
        if test_command:
            parts.append(f"RUN WITH: {test_command}")
        for n, b in enumerate(group, 1):
            body = b["spec"].split("\nRUN WITH:")[0].split("\nDONE WHEN:")[0].strip()
            parts += ["", f"=== {n}. {b['path']} ===", body]
        parts += ["", "DONE WHEN:", "- Every file above has tests for its public behaviour, and a test mentions each file's main type.",
                  "- The whole test suite passes.", "", "DON'T:",
                  "- Change what the code does. Only add small seams when a test needs one.", "- Write tests that assert nothing."]
        cases_text = "\n".join(dict.fromkeys(line for b in group for line in b["test_cases"].splitlines() if line))
        features_seen = {b["feature"] for b in group if b["feature"]}
        return {"type": "coverage", "area": area, "summary": title, "spec": "\n".join(parts), "test_cases": cases_text,
                "feature": features_seen.pop() if len(features_seen) == 1 else "", "targets": [b["path"] for b in group], "platform": name}

    everything = combine(briefs, f"Add tests for {len(briefs)} untested files" + (f" in {name}" if name else ""),
                         next(iter(by_area)) if len(by_area) == 1 else (name or "All areas"))
    areas = [combine(group, f"Add tests for {area} ({len(group)} file{'s' * (len(group) != 1)})", area) for area, group in sorted(by_area.items())]
    return {"all": everything, "areas": areas, "skipped": len(paths[:MAX_BULK]) - len(briefs), "over_limit": max(0, len(paths) - MAX_BULK)}
