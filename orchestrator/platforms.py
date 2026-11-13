"""Which parts of a project have tests, by platform (client, server, shared code, tooling), and the biggest gaps.

A project's code is split into platforms from its folders: a Unity or Xcode app is the client, `server/`, `functions/`
and `api/` are the server, a `Protocol` or `Shared` library is shared, `tools/` and `scripts/` are tooling, and another
repository cloned inside is its own platform. `platforms` in .orchestrator/project.json overrides all of that:

    "platforms": [{"name": "Game client", "kind": "client", "paths": ["DaocUnity"]},
                  {"name": "Game server", "kind": "server", "paths": ["OpenDAoC-Core"]}]

For each platform this answers: how much code is there, how much of it does a test refer to, how many tests point at it,
and which files are the biggest ones nothing tests. "Has a test" means a test file mentions one of the file's types (or
its name) — it is not line coverage, which needs the language's coverage tool and isn't available for every language.
Nothing is run: this only reads files.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

SOURCE_LANGUAGES = {".cs": "C#", ".swift": "Swift", ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
                    ".kt": "Kotlin", ".java": "Java", ".go": "Go", ".rs": "Rust", ".gd": "GDScript"}
SKIP_DIRS = {".git", "node_modules", "bin", "obj", "Library", "Temp", "Logs", "PackageCache", ".venv", "venv", "build", "dist",
             ".orchestrator", "DerivedData", "Pods", "__pycache__", "TestResults", ".build", ".gradle", "target", "Carthage"}
THIRD_PARTY = {"plugins", "thirdparty", "third_party", "external", "vendor", "generated", "packages"}
TEST_DIRS = {"tests", "test", "__tests__", "unittests", "uitests", "integrationtests", "spec", "specs"}
CONFIG_FILE = re.compile(r"^(?:\.eslintrc|eslint\.config|jest\.config|vite\.config|webpack\.config|babel\.config|rollup\.config|tailwind\.config|postcss\.config|karma\.conf|setup\.py$|conftest\.py$)")
TEST_FILE = re.compile(r"(?:^test_|_test\.|Tests?\.[a-z]+$|Spec\.[a-z]+$|\.(?:test|spec)\.[jt]sx?$)")
TYPE_NAME = {
    ".cs": re.compile(r"\b(?:class|struct|interface|enum|record)\s+([A-Za-z_]\w*)"),
    ".swift": re.compile(r"\b(?:class|struct|enum|protocol|actor)\s+([A-Za-z_]\w*)"),
    ".py": re.compile(r"^class\s+([A-Za-z_]\w*)", re.M),
    ".kt": re.compile(r"\b(?:class|interface|object)\s+([A-Za-z_]\w*)"),
    ".java": re.compile(r"\b(?:class|interface|enum)\s+([A-Za-z_]\w*)"),
    ".ts": re.compile(r"export\s+(?:default\s+)?(?:abstract\s+)?(?:class|function|const|interface)\s+([A-Za-z_]\w*)"),
    ".js": re.compile(r"(?:export|module\.exports\.)\s*(?:default\s+)?(?:class|function|const)\s+([A-Za-z_]\w*)"),
}
TYPE_NAME[".tsx"] = TYPE_NAME[".ts"]
WORD = re.compile(r"[A-Za-z_]\w{3,}")
PY_IMPORT = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w., ]+))", re.M)
MIN_NAME = 5
MAX_FILES_PER_NAME = 3  # a name shared by more files than this (Zone, Item, Manager...) says nothing about any one of them
MAX_FILES = 20_000
GAP_FILES = 5

KIND_RULES = (  # (kind, folder names, in order of priority)
    ("server", {"server", "servers", "backend", "api", "functions", "cloud", "services", "service", "gameserver"}),
    ("shared", {"shared", "common", "core", "protocol", "lib", "libs", "models", "sdk"}),
    ("tooling", {"tools", "scripts", "script", "ci", "build", "devtools", "ai", "automation"}),
    ("client", {"client", "app", "apps", "web", "frontend", "ios", "android", "mobile", "ui"}),
)
KIND_LABEL = {"client": "Client", "server": "Server", "shared": "Shared code", "tooling": "Tooling", "other": "Other", "separate": "Separate repository"}


def _words(name: str) -> set[str]:
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])", name) if w}


def _kind_of(name: str) -> str | None:
    words = _words(name)
    for kind, keys in KIND_RULES:
        if words & keys:
            return kind
    return None


def _separate_repo_role(folder: Path) -> tuple[str, str]:
    """(kind, label) for another repository cloned inside: a server, a client, or just a separate repository."""
    try:
        children = [c for c in folder.iterdir() if not c.name.startswith(".")]
    except OSError:
        return "separate", ""
    names = {c.name.lower() for c in children}
    if any(n == "gameserver" or "server" in n and (folder / n).is_dir() for n in names) or any(n.startswith(("docker-compose", "compose")) for n in names):
        return "server", "server"
    if any(n in {"client", "app", "ios", "android"} for n in names):
        return "client", "client"
    return "separate", ""


def _is_unity(folder: Path) -> bool:
    return (folder / "ProjectSettings" / "ProjectVersion.txt").is_file() and (folder / "Assets").is_dir()


def parse(raw: Any, root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """The platforms in a `platforms` value (project.json or the editor), and what is wrong with it. Each needs a unique
    name, a kind, and one or more folders or files that exist inside the project."""
    if raw in (None, []):
        return [], []
    if not isinstance(raw, list):
        return [], ["platforms must be a list."]
    clean: list[dict[str, Any]] = []
    errors: list[str] = []
    names: set[str] = set()
    owners: dict[str, str] = {}  # folder -> the platform that has it
    for index, item in enumerate(raw, 1):
        label = f"platforms[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be an object.")
            continue
        name = str(item.get("name") or "").strip()
        label = f'platform "{name}"' if name else label
        problems = []
        if not name:
            problems.append("needs a name")
        elif len(name) > 60:
            problems.append("name is longer than 60 characters")
        elif name.lower() in names:
            problems.append("name is used twice")
        kind = item.get("kind") or "other"
        if kind not in KIND_LABEL:
            problems.append(f"kind must be one of {', '.join(KIND_LABEL)}")
        paths = item.get("paths")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
            problems.append("needs at least one folder")
            paths = []
        cleaned = []
        for path in paths:
            path = path.strip().strip("/") or "."
            parts = Path(path).parts
            if Path(path).is_absolute() or ".." in parts:
                problems.append(f'"{path}" must be a folder inside the project')
            elif path != "." and not (root / path).exists():
                problems.append(f'"{path}" doesn\'t exist in the project')
            elif path in owners:
                problems.append(f'"{path}" already belongs to "{owners[path]}"')
            else:
                cleaned.append(path)
        if problems:
            errors += [f"{label}: {problem}." for problem in problems]
            continue
        names.add(name.lower())
        owners.update({path: name for path in cleaned})
        clean.append({"name": name, "kind": kind, "paths": cleaned})
    return clean, errors


def detect(root: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    """The platforms: from `platforms` in project.json, or worked out from the folders. Each is {name, kind, paths, detected}."""
    configured = config.get("platforms")
    if isinstance(configured, list) and configured:
        out = []
        for item in configured:
            if isinstance(item, dict) and item.get("name") and isinstance(item.get("paths"), list):
                kind = item.get("kind") if item.get("kind") in KIND_LABEL else "other"
                out.append({"name": str(item["name"]), "kind": kind, "paths": [str(p).strip("/") or "." for p in item["paths"]], "detected": False})
        if out:
            return out
    platforms: dict[str, dict[str, Any]] = {}

    def add(name: str, kind: str, path: str) -> None:
        platforms.setdefault(name, {"name": name, "kind": kind, "paths": [], "detected": True})["paths"].append(path)

    try:
        tops = sorted(p for p in root.iterdir() if p.is_dir() and p.name not in SKIP_DIRS and not p.name.startswith(".")
                      and not p.name.endswith((".xcodeproj", ".xcworkspace", ".xcassets", ".lproj", ".bundle", ".framework", ".app")))
    except OSError:
        return []
    xcode_targets = {p.stem for p in root.glob("*.xcodeproj")}
    for folder in tops:
        name = folder.name
        if (folder / ".git").exists():
            kind, role = _separate_repo_role(folder)
            add(f"{name} ({role + ', ' if role else ''}separate repository)", kind, name)
        elif _is_unity(folder):
            add("Client (Unity)", "client", name)
        elif name in xcode_targets or name == config.get("scheme") or name == config.get("project_name"):
            add("Client (iOS app)", "client", name)
        elif name.lower() in TEST_DIRS or name.lower().endswith(("tests", "uitests")):
            continue  # tests are not a platform
        elif (kind := _kind_of(name)):
            add({"client": "Client", "server": "Server", "shared": "Shared code", "tooling": "Tooling"}[kind], kind, name)
        else:
            add(name, "other", name)
    if _is_unity(root):
        platforms = {"Client (Unity)": {"name": "Client (Unity)", "kind": "client", "paths": ["."], "detected": True}}
    return list(platforms.values())


def _platform_of(rel: str, platforms: list[dict[str, Any]]) -> int | None:
    best, best_len = None, -1
    for i, platform in enumerate(platforms):
        for path in platform["paths"]:
            if path == "." or rel == path or rel.startswith(path + "/"):
                if len(path) > best_len:
                    best, best_len = i, len(path)
    return best


def _is_test_path(rel: str) -> bool:
    parts = [p.lower() for p in rel.split("/")]
    return bool(set(parts[:-1]) & TEST_DIRS) or any(p.endswith(("tests", "uitests")) for p in parts[:-1]) or bool(TEST_FILE.search(parts[-1]))


PROJECT_REFERENCE = re.compile(r'<ProjectReference\s+Include="([^"]+)"', re.I)


def _referenced_platform(root: Path, rel: str, platforms: list[dict[str, Any]]) -> int | None:
    """For a C# test file: the platform of the project its test project references (Foo.Tests -> Foo)."""
    folder = (root / rel).parent
    while root in folder.parents or folder == root:
        projects = sorted(folder.glob("*.csproj"))
        if projects:
            try:
                text = projects[0].read_text(encoding="utf-8", errors="ignore")
            except OSError:
                return None
            votes: dict[int, int] = {}
            for ref in PROJECT_REFERENCE.findall(text):
                target = os.path.normpath(folder / ref.replace("\\", "/"))
                try:
                    owner = _platform_of(Path(target).relative_to(root).as_posix(), platforms)
                except ValueError:
                    continue
                if owner is not None:
                    votes[owner] = votes.get(owner, 0) + 1
            return max(votes, key=votes.get) if votes else None
        if folder == root:
            return None
        folder = folder.parent
    return None


def _lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def analyze(root: Path, config: dict[str, Any], suites: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The per-platform picture. `suites` (from discover_test_suites) give each test file's test count; without them tests aren't counted."""
    platforms = detect(root, config)
    if not platforms:
        return {"platforms": [], "note": "No source folders found."}
    suite_files = {Path(str(s.get("rel_path") or "")).as_posix(): s for s in suites or []}
    sources: list[dict[str, Any]] = []
    test_tokens: set[str] = set()
    test_files: dict[str, set[str]] = {}
    seen = 0
    for folder, dirs, files in os.walk(root):
        rel_folder = Path(folder).relative_to(root).as_posix()
        # another repository is its own platform: look inside it only when a platform's path points there
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            ext = Path(name).suffix
            if ext not in SOURCE_LANGUAGES:
                continue
            seen += 1
            if seen > MAX_FILES:
                break
            rel = f"{rel_folder}/{name}" if rel_folder != "." else name
            try:
                text = (Path(folder) / name).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if rel in suite_files or _is_test_path(rel):
                tokens = set(WORD.findall(text))
                if ext == ".py":  # an import names a module exactly, however short its name is
                    for match in PY_IMPORT.finditer(text):
                        for dotted in (match.group(1) or match.group(2)).replace(" ", "").split(","):
                            tokens.update(part for part in dotted.split(".") if len(part) >= 2)
                test_tokens |= tokens
                test_files[rel] = tokens
                continue
            if set(p.lower() for p in rel.split("/")[:-1]) & THIRD_PARTY or CONFIG_FILE.search(name):
                continue  # third-party code and tool configuration are not what the project's tests are for
            names = {Path(name).stem} | set(TYPE_NAME.get(ext, re.compile("$^")).findall(text))
            keep = {n for n in names if len(n) >= MIN_NAME}
            if ext == ".py" and len(Path(name).stem) >= 2:
                keep.add(Path(name).stem)  # a Python module is imported by its file name
            sources.append({"path": rel, "ext": ext, "lines": _lines(text), "names": keep})
    rows = [{**p, "files": 0, "lines": 0, "tested_files": 0, "tested_lines": 0, "tests": 0, "gaps": [], "languages": {}} for p in platforms]
    by_name: dict[str, list[int]] = {}
    for i, source in enumerate(sources):
        for n in source["names"]:
            by_name.setdefault(n, []).append(i)
    referenced: set[int] = set()
    credit: dict[int, int] = {}  # platform index -> tests whose files point at it
    specific = {n: files for n, files in by_name.items() if len(files) <= MAX_FILES_PER_NAME}
    for rel, tokens in test_files.items():
        votes: dict[int, int] = {}
        for token in tokens & specific.keys():
            files = specific[token]
            referenced.update(files)
            owners = {_platform_of(sources[i]["path"], platforms) for i in files} - {None}
            if len(owners) == 1:
                owner = owners.pop()
                votes[owner] = votes.get(owner, 0) + 1
        if rel not in suite_files:
            continue
        home = _platform_of(rel, platforms)  # a test inside a platform's own folders belongs to it
        owner = home if home is not None else _referenced_platform(root, rel, platforms)
        if owner is None and votes:
            owner = max(votes, key=votes.get)
        if owner is not None:
            credit[owner] = credit.get(owner, 0) + int(suite_files[rel].get("test_count") or 0)
    for i, source in enumerate(sources):
        owner = _platform_of(source["path"], platforms)
        if owner is None:
            continue
        row = rows[owner]
        row["files"] += 1
        row["lines"] += source["lines"]
        language = SOURCE_LANGUAGES[source["ext"]]
        row["languages"][language] = row["languages"].get(language, 0) + source["lines"]
        if i in referenced:
            row["tested_files"] += 1
            row["tested_lines"] += source["lines"]
        else:
            row["gaps"].append({"path": source["path"], "lines": source["lines"]})
    out = []
    for index, row in enumerate(rows):
        if not row["files"]:
            continue
        row["tests"] = credit.get(index, 0)
        row["gaps"] = sorted(row["gaps"], key=lambda g: -g["lines"])
        row["untested_files"] = len(row["gaps"])
        row["gaps"] = row["gaps"][:GAP_FILES]
        row["percent"] = round(100 * row["tested_lines"] / row["lines"]) if row["lines"] else 0
        row["languages"] = [{"name": k, "lines": v} for k, v in sorted(row["languages"].items(), key=lambda kv: -kv[1])]
        row["label"] = KIND_LABEL.get(row["kind"], "Other")
        out.append(row)
    order = {"client": 0, "server": 1, "shared": 2, "tooling": 3, "separate": 4, "other": 5}
    out.sort(key=lambda r: (order.get(r["kind"], 9), -r["lines"]))
    return {"platforms": out, "note": "A file has a test when a test file mentions one of its types or its name. That is not line coverage."}
