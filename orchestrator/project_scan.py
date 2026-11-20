"""What the repository itself says about the project, for Ask AI.

A model asked about a project whose product requirements aren't written still has to know what the project is. It runs in an
empty folder and can't open files, so the context carries the answer: what the project is built with, how its folders are
laid out, and the start of its main source files. Three small, bounded sections:

    stack_and_manifests   language mix, detected stack, and the facts in package.json / pyproject / Package.swift / Gradle ...
    structure             the folders and how many files each holds, plus the files at the top
    key_files             the first lines of a few entry points (main, app, index, server ...)

The scan never reads secrets (keys, certificates, .env files, credentials) or noise (dependencies, build output), redacts
anything token-shaped in the code it quotes, is bounded in files and time so a huge repository costs about a second, and is
remembered for two minutes so a conversation doesn't rescan on every question.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

SKIP_DIRS = {".git", ".orchestrator", ".worktrees", ".claude", ".idea", ".vscode", "node_modules", "build", "dist", "target", "out", "venv", ".venv",
             "env", "__pycache__", ".pytest_cache", ".mypy_cache", "Pods", "Carthage", "DerivedData", ".build", ".gradle", ".dart_tool", ".next",
             ".nuxt", "coverage", "Library", "Temp", "Logs", "obj", "UserSettings", "vendor", ".terraform", "site-packages", "bower_components"}
SECRET_NAME = re.compile(
    r"(^|/)(\.env(\..*)?|\.npmrc|\.netrc|\.pypirc|id_[a-z0-9]+|.*\.(pem|key|p8|p12|pfx|jks|keystore|crt|cer|mobileprovision|keychain)|"
    r".*(secret|credential|password|passwd|private[-_]?key|token)s?(\.[a-z0-9]+)?|google-services\.json|GoogleService-Info\.plist|"
    r"service[-_]?account.*\.json|.*\.tfvars|\.htpasswd)$", re.I)
SECRET_TEXT = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "<private key removed>"),
    (re.compile(r"(?i)((?:api[_-]?key|secret|token|passw(?:or)?d|authorization|bearer|client[_-]?secret|access[_-]?key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]{8,}"), r"\1<redacted>"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b"), "<redacted>"),
]
LANGUAGES = {".swift": "Swift", ".m": "Objective-C", ".mm": "Objective-C", ".kt": "Kotlin", ".java": "Java", ".ts": "TypeScript", ".tsx": "TypeScript",
             ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".py": "Python", ".cs": "C#", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
             ".php": "PHP", ".c": "C", ".h": "C/C++", ".cpp": "C++", ".dart": "Dart", ".vue": "Vue", ".svelte": "Svelte", ".html": "HTML", ".css": "CSS",
             ".scss": "CSS", ".sql": "SQL", ".sh": "Shell", ".lua": "Lua", ".gd": "GDScript"}
SOURCE_SUFFIXES = {s for s, name in LANGUAGES.items() if name not in ("HTML", "CSS", "SQL")} | {".xml", ".gradle", ".kts", ".plist"}
ENTRY_STEMS = {"main": 10, "app": 9, "index": 8, "server": 8, "__main__": 8, "cli": 7, "application": 7, "appdelegate": 7, "mainactivity": 9,
               "contentview": 6, "program": 8, "manage": 6, "routes": 5, "router": 5, "page": 4, "layout": 3, "androidmanifest": 6, "scenedelegate": 4}
NOT_KEY = re.compile(r"(^|/)(tests?|spec|specs|__tests__|mocks?|fixtures?|generated|node_modules|migrations|locale|i18n|assets|public/vendor)(/|$)|"
                     r"(\.min\.|\.test\.|\.spec\.|_test\.|\.generated\.|\.g\.|package-lock|yarn\.lock|pnpm-lock|\.lock$)", re.I)
MAX_FILES = 25_000
MAX_SECONDS = 3.0
MAX_FILE_BYTES = 200_000
CACHE_SECONDS = 120
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def redact(text: str) -> str:
    for pattern, replacement in SECRET_TEXT:
        text = pattern.sub(replacement, text)
    return text


def _walk(root: Path, now: float | None = None) -> tuple[list[str], bool]:
    """Relative paths of the project's own files (no dependencies, build output or secrets), and whether the scan stopped early."""
    files: list[str] = []
    started = now if now is not None else time.monotonic()
    root_text = str(root)
    for folder, dirs, names in os.walk(root_text):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and not os.path.islink(os.path.join(folder, d)))
        rel_folder = os.path.relpath(folder, root_text)
        for name in sorted(names):
            rel = name if rel_folder == "." else f"{rel_folder}/{name}"
            if name.startswith(".DS_Store") or SECRET_NAME.search(rel):
                continue
            files.append(rel)
            if len(files) >= MAX_FILES:  # checked per file: a single folder can hold more than the limit
                return files, True
        if time.monotonic() - started > MAX_SECONDS:
            return files, True
    return files, False


def languages(files: list[str]) -> list[tuple[str, int]]:
    counts = Counter(LANGUAGES[Path(f).suffix.lower()] for f in files if Path(f).suffix.lower() in LANGUAGES)
    return counts.most_common(6)


def structure(files: list[str], limit: int, truncated: bool = False) -> str:
    """The folders (two levels) with how many files each holds, and the files at the top."""
    top_files = [f for f in files if "/" not in f]
    by_dir: dict[str, Counter] = {}
    totals: Counter = Counter()
    for f in files:
        parts = f.split("/")
        if len(parts) < 2:
            continue
        totals[parts[0]] += 1
        by_dir.setdefault(parts[0], Counter())[parts[1] if len(parts) > 2 else ""] += 1
    lines = []
    if top_files:
        shown = top_files[:30]
        lines.append("Top level files: " + ", ".join(shown) + (f" and {len(top_files) - len(shown)} more" if len(top_files) > len(shown) else ""))
    for folder, count in sorted(totals.items(), key=lambda x: (-x[1], x[0])):
        subs = [(k, v) for k, v in by_dir[folder].most_common(8) if k]
        extra = f": " + ", ".join(f"{k}/ ({v})" for k, v in subs) if subs else ""
        lines.append(f"{folder}/ ({count} file{'s' if count != 1 else ''}){extra}")
    text = "\n".join(lines)
    if truncated:
        text += "\n(a very large project: this listing stops early)"
    return text[:limit].rsplit("\n", 1)[0] + "\n…(trimmed)" if len(text) > limit else text


def _read(path: Path, n: int = 60_000) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES * 2:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")[:n]
    except OSError:
        return ""


def _names(text: str, pattern: Any, limit: int = 15) -> list[str]:
    seen: list[str] = []
    for name in (pattern.findall(text) if hasattr(pattern, "findall") else re.findall(pattern, text)):
        if name not in seen:
            seen.append(name)
    return seen[:limit]


RE_TOML_NAME = re.compile(r'^name\s*=\s*"([^"]+)"', re.M)
RE_TOML_DESCRIPTION = re.compile(r'^description\s*=\s*"([^"]+)"', re.M)
RE_SWIFT_NAME = re.compile(r'name:\s*"([^"]+)"')
RE_SWIFT_TARGET = re.compile(r'\.(?:target|executableTarget|testTarget)\(\s*name:\s*"([^"]+)"')
RE_POD = re.compile(r"""pod\s+['"]([^'"]+)['"]""")
RE_GRADLE_FILE = re.compile(r"(^|/)(settings|build)\.gradle(\.kts)?$")
RE_GRADLE_PLUGIN = re.compile(r"""(?:id|apply plugin:?)\s*[("']([\w.\-]+)[)"']""")
RE_APPLICATION_ID = re.compile(r'applicationId\s*=?\s*"([^"]+)"')
RE_INCLUDE = re.compile(r"""include\(?[\s"']+:?([\w\-]+)""")
RE_PUB_NAME = re.compile(r"^name:\s*(\S+)", re.M)
RE_PUB_DESCRIPTION = re.compile(r"^description:\s*(.+)$", re.M)
RE_REQUIREMENT_END = re.compile(r"[=<>~! \[;]")


def _joined(*parts: str) -> str:
    return "; ".join(p for p in parts if p)


def manifests(root: Path, files: list[str]) -> str:
    """What the build files say about the project: its name, what it is for, what it depends on, how it is built and tested."""
    present = set(files)
    out: list[str] = []
    if "package.json" in present:
        try:
            pkg = json.loads(_read(root / "package.json"))
        except ValueError:
            pkg = {}
        if isinstance(pkg, dict):
            deps = sorted(pkg.get("dependencies") or {})
            scripts = sorted(pkg.get("scripts") or {})
            out.append("package.json: " + _joined(
                f"name {pkg['name']}" if pkg.get("name") else "", f"\"{pkg['description']}\"" if pkg.get("description") else "",
                "scripts " + ", ".join(scripts[:12]) if scripts else "",
                "dependencies " + ", ".join(deps[:18]) + (f" and {len(deps) - 18} more" if len(deps) > 18 else "") if deps else "",
                f"{len(pkg.get('devDependencies') or {})} dev dependencies" if pkg.get("devDependencies") else ""))
    if "pyproject.toml" in present:
        text = _read(root / "pyproject.toml", 8_000)
        name, description = RE_TOML_NAME.search(text), RE_TOML_DESCRIPTION.search(text)
        out.append("pyproject.toml: " + _joined(f"name {name[1]}" if name else "", f"\"{description[1]}\"" if description else ""))
    if "requirements.txt" in present:
        lines = [l.strip() for l in _read(root / "requirements.txt", 4_000).splitlines() if l.strip() and not l.startswith(("#", "-"))]
        out.append("requirements.txt: " + ", ".join(RE_REQUIREMENT_END.split(l)[0] for l in lines[:18]))
    if "Package.swift" in present:
        text = _read(root / "Package.swift", 8_000)
        name, targets = RE_SWIFT_NAME.search(text), _names(text, RE_SWIFT_TARGET)
        out.append("Package.swift: " + _joined(f"name {name[1]}" if name else "", "targets " + ", ".join(targets) if targets else ""))
    xcode = sorted({f.split(".xcodeproj/")[0].rsplit("/", 1)[-1] for f in files if ".xcodeproj/" in f})
    if xcode:
        out.append("Xcode project: " + ", ".join(xcode[:4]))
    if "Podfile" in present:
        pods = _names(_read(root / "Podfile", 6_000), RE_POD)
        if pods:
            out.append("Podfile pods: " + ", ".join(pods))
    gradle_files = [f for f in files if RE_GRADLE_FILE.search(f)][:6]
    if gradle_files:
        text = "\n".join(_read(root / g, 8_000) for g in gradle_files)
        plugins, modules, app_id = _names(text, RE_GRADLE_PLUGIN), _names(text, RE_INCLUDE), RE_APPLICATION_ID.search(text)
        out.append("Gradle: " + _joined("plugins " + ", ".join(plugins) if plugins else "", f"application id {app_id[1]}" if app_id else "",
                                         "modules " + ", ".join(modules) if modules else ""))
    if "Cargo.toml" in present:
        text = _read(root / "Cargo.toml", 6_000)
        name, description = RE_TOML_NAME.search(text), RE_TOML_DESCRIPTION.search(text)
        out.append("Cargo.toml: " + _joined(f"name {name[1]}" if name else "", f"\"{description[1]}\"" if description else ""))
    if "go.mod" in present:
        out.append("go.mod: " + (_read(root / "go.mod", 2_000).splitlines() or [""])[0])
    if "pubspec.yaml" in present:
        text = _read(root / "pubspec.yaml", 4_000)
        name, description = RE_PUB_NAME.search(text), RE_PUB_DESCRIPTION.search(text)
        out.append("pubspec.yaml: " + _joined(f"name {name[1]}" if name else "", f"\"{description[1].strip()}\"" if description else ""))
    if "Dockerfile" in present or any(f.endswith(("docker-compose.yml", "compose.yaml", "docker-compose.yaml")) for f in files):
        out.append("Runs in Docker (a Dockerfile or compose file is present).")
    return "\n".join(x.rstrip("; ") for x in out if x.strip() and not x.rstrip().endswith(":"))  # an entry with no facts is left out


def stack_and_manifests(root: Path, files: list[str]) -> str:
    lines = []
    mix = languages(files)
    if mix:
        lines.append("Languages by file count: " + ", ".join(f"{name} ({count})" for name, count in mix))
    try:
        from orchestrator.stack_detection import detect_project_stack
        stack = detect_project_stack(root)
        if stack and stack.display_name and stack.display_name != "Generic / Other":
            lines.append(f"Detected stack: {stack.display_name}" + (f"; build `{stack.build_command}`" if stack.build_command else "") + (f"; test `{stack.test_command}`" if stack.test_command else ""))
    except Exception:  # a project this can't read shouldn't stop the question
        pass
    facts = manifests(root, files)
    if facts:
        lines.append(facts)
    return "\n".join(lines)


def _key_score(rel: str) -> int:
    name = Path(rel).name
    stem = Path(rel).stem.lower()
    suffix = Path(rel).suffix.lower()
    if suffix not in SOURCE_SUFFIXES or NOT_KEY.search(rel):
        return 0
    score = ENTRY_STEMS.get(stem, 0)
    if not score and name.endswith(("App.swift", "Application.kt", "Application.java", "Controller.swift", "ViewModel.swift", "Activity.kt")):
        score = 4
    if not score:
        return 0
    depth = rel.count("/")
    return max(1, score * 10 - depth * 3)


def key_files(root: Path, files: list[str], count: int, chars_each: int) -> str:
    """The first lines of the likeliest entry points, so a model can see what the code is and how it starts."""
    ranked = sorted((f for f in files if _key_score(f)), key=lambda f: (-_key_score(f), f))[:count]
    blocks = []
    for rel in ranked:
        try:
            if (root / rel).stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        text = redact(_read(root / rel, chars_each * 3)).strip()
        if not text or "\x00" in text:
            continue
        lines, used = [], 0
        for line in text.splitlines():
            if used + len(line) + 1 > chars_each:
                lines.append("…")
                break
            lines.append(line)
            used += len(line) + 1
        blocks.append(f"#### {rel}\n```\n" + "\n".join(lines) + "\n```")
    return "\n\n".join(blocks)


SIZES = {"free": {"structure": 2_500, "files": 3, "chars": 1_500}, "balanced": {"structure": 5_000, "files": 8, "chars": 3_000},
         "complex": {"structure": 8_000, "files": 12, "chars": 4_000}}


def scan(root: Path, tier: str = "balanced", now: float | None = None) -> dict[str, str]:
    """{"What it's built with": ..., "Project structure": ..., "Key source files": ...}: each may be empty. Cached for two minutes."""
    clock = now if now is not None else time.time()
    key = f"{root}|{tier}"
    cached = _CACHE.get(key)
    if cached and clock - cached[0] < CACHE_SECONDS:
        return cached[1]
    size = SIZES.get(tier, SIZES["balanced"])
    files, truncated = _walk(Path(root))
    result = {"What it's built with": stack_and_manifests(Path(root), files),
              "Project structure": structure(files, size["structure"], truncated),
              "Key source files (the start of each)": key_files(Path(root), files, size["files"], size["chars"])}
    _CACHE[key] = (clock, result)
    return result
