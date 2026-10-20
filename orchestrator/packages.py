"""Packages and plugins for one project: what it uses now, what's worth adding, and the person's own list.

Three things live here, none of which edits a manifest (that goes through a job, so it is planned, reviewed and tested like any change):

- `detect` reads the project's manifests (package.json, requirements.txt, pyproject.toml, Package.swift, Cargo.toml, go.mod,
  Gemfile, Podfile, build.gradle, Unity's Packages/manifest.json, pubspec.yaml) for what is installed;
- the record (`.orchestrator/packages.json`) holds what a model suggested after reading the product requirements and features
  (and searching the web), plus anything the person adds, edits or dismisses;
- `review_prompt` / `parse_suggestions` are the research step: specs in, a short list of packages out, each tied to a feature.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import secrets
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

ECOSYSTEMS = {
    "npm": "npm (JavaScript, TypeScript)", "pip": "pip (Python)", "swiftpm": "Swift Package Manager", "cocoapods": "CocoaPods",
    "cargo": "Cargo (Rust)", "go": "Go modules", "ruby": "RubyGems", "gradle": "Gradle (Java, Kotlin, Android)",
    "unity": "Unity packages", "dart": "pub (Dart, Flutter)",
}
ALIASES = {"javascript": "npm", "typescript": "npm", "node": "npm", "js": "npm", "yarn": "npm", "pnpm": "npm", "python": "pip", "pypi": "pip",
           "swift": "swiftpm", "spm": "swiftpm", "swift package manager": "swiftpm", "pods": "cocoapods", "rust": "cargo", "crates": "cargo",
           "golang": "go", "rubygems": "ruby", "gem": "ruby", "java": "gradle", "kotlin": "gradle", "android": "gradle", "maven": "gradle",
           "flutter": "dart", "pub": "dart", "unity package": "unity", "upm": "unity"}
STATUSES = ("suggested", "planned", "dismissed")
SKIP_DIRS = {"node_modules", ".git", ".build", "Pods", "venv", ".venv", "build", "dist", "target", "__pycache__", ".orchestrator", "Library", "Temp",
             "DerivedData", ".dart_tool", ".gradle", "vendor"}
NAME = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@._/:+-]{0,99}$")
MAX_SUGGESTIONS = 12


class PackageError(ValueError):
    pass


def _norm(ecosystem: str, name: str) -> str:
    """One spelling per package for matching (pip folds - _ .; swiftpm ignores .git and the owner; gradle keeps group:artifact)."""
    name = name.strip().lower()
    if ecosystem == "pip":
        return re.sub(r"[-_.]+", "-", name)
    if ecosystem == "swiftpm":
        return re.sub(r"\.git$", "", name.rstrip("/")).rsplit("/", 1)[-1]
    return name


# ---------------------------------------------------------------- reading manifests


def _item(name: str, version: str, ecosystem: str, manifest: str, kind: str = "runtime") -> dict[str, str]:
    return {"name": name.strip(), "version": str(version or "").strip()[:60], "ecosystem": ecosystem, "manifest": manifest, "kind": kind}


def _read(path: Path, limit: int = 400_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read(path))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(_read(path))
    except tomllib.TOMLDecodeError:
        return {}


def _package_json(path: Path, rel: str) -> list[dict[str, str]]:
    data = _json(path)
    out = []
    for key, kind in (("dependencies", "runtime"), ("devDependencies", "dev"), ("peerDependencies", "runtime"), ("optionalDependencies", "runtime")):
        for name, version in (data.get(key) or {}).items() if isinstance(data.get(key), dict) else []:
            out.append(_item(name, version, "npm", rel, kind))
    return out


PEP508 = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([<>=!~].*?)?\s*(?:;.*)?$")


def _pep508(spec: str, rel: str, kind: str) -> dict[str, str] | None:
    found = PEP508.match(spec.strip())
    return _item(found.group(1), (found.group(2) or "").replace(" ", ""), "pip", rel, kind) if found else None


def _requirements(path: Path, rel: str) -> list[dict[str, str]]:
    kind = "dev" if re.search(r"dev|test|lint", path.name, re.I) else "runtime"
    out = []
    for line in _read(path).splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "http", "git+", ".", "/")):
            continue
        found = _pep508(line, rel, kind)
        if found:
            out.append(found)
    return out


def _pyproject(path: Path, rel: str) -> list[dict[str, str]]:
    data, out = _toml(path), []
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    for spec in project.get("dependencies") or []:
        found = _pep508(str(spec), rel, "runtime")
        if found:
            out.append(found)
    for group in (project.get("optional-dependencies") or {}).values() if isinstance(project.get("optional-dependencies"), dict) else []:
        for spec in group or []:
            found = _pep508(str(spec), rel, "dev")
            if found:
                out.append(found)
    poetry = (data.get("tool") or {}).get("poetry") if isinstance(data.get("tool"), dict) else None
    if isinstance(poetry, dict):
        tables = [(poetry.get("dependencies"), "runtime"), (poetry.get("dev-dependencies"), "dev")]
        tables += [(g.get("dependencies"), "dev") for g in (poetry.get("group") or {}).values() if isinstance(g, dict)]
        for table, kind in tables:
            for name, spec in (table or {}).items() if isinstance(table, dict) else []:
                if name.lower() != "python":
                    out.append(_item(name, spec if isinstance(spec, str) else (spec or {}).get("version", "") if isinstance(spec, dict) else "", "pip", rel, kind))
    return out


SWIFT_PACKAGE = re.compile(r'\.package\(\s*(?:name:\s*"[^"]*"\s*,\s*)?url:\s*"([^"]+)"\s*,\s*([^)]*(?:\([^)]*\))?[^)]*)\)')


def _package_swift(path: Path, rel: str) -> list[dict[str, str]]:
    out = []
    for url, requirement in SWIFT_PACKAGE.findall(_read(path)):
        version = re.search(r'"([^"]+)"', requirement)
        label = re.sub(r"\s+", " ", requirement.strip())[:40]
        out.append(_item(re.sub(r"\.git$", "", url.rstrip("/")).rsplit("/", 1)[-1], version.group(1) if version and "branch" not in label else label, "swiftpm", rel))
    return out


def _cargo(path: Path, rel: str) -> list[dict[str, str]]:
    data, out = _toml(path), []
    for key, kind in (("dependencies", "runtime"), ("dev-dependencies", "dev"), ("build-dependencies", "dev")):
        table = data.get(key)
        for name, spec in table.items() if isinstance(table, dict) else []:
            version = spec if isinstance(spec, str) else spec.get("version", "") if isinstance(spec, dict) else ""
            out.append(_item(name, version, "cargo", rel, kind))
    return out


def _go_mod(path: Path, rel: str) -> list[dict[str, str]]:
    text, out = _read(path), []
    blocks = re.findall(r"require\s*\(([^)]*)\)", text, re.S) + re.findall(r"^require\s+(\S+\s+\S+.*)$", text, re.M)
    for block in blocks:
        for line in block.splitlines():
            found = re.match(r"^\s*(\S+)\s+(v\S+)(.*)$", line)
            if found and "// indirect" not in found.group(3):
                out.append(_item(found.group(1), found.group(2), "go", rel))
    return out


def _gemfile(path: Path, rel: str) -> list[dict[str, str]]:
    return [_item(m.group(1), (m.group(2) or "").strip(), "ruby", rel) for m in re.finditer(r"""^\s*gem\s+['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?""", _read(path), re.M)]


def _podfile(path: Path, rel: str) -> list[dict[str, str]]:
    return [_item(m.group(1), (m.group(2) or "").strip(), "cocoapods", rel) for m in re.finditer(r"""^\s*pod\s+['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?""", _read(path), re.M)]


def _gradle(path: Path, rel: str) -> list[dict[str, str]]:
    out = []
    for m in re.finditer(r"""^\s*(\w+)\s*\(?\s*['"]([^'":\s]+):([^'":\s]+)(?::([^'"\s]+))?['"]""", _read(path), re.M):
        configuration, group, artifact, version = m.groups()
        if configuration in ("implementation", "api", "compileOnly", "runtimeOnly", "kapt", "ksp", "annotationProcessor") or "est" in configuration:
            out.append(_item(f"{group}:{artifact}", version or "", "gradle", rel, "dev" if "est" in configuration else "runtime"))
    return out


def _unity(path: Path, rel: str) -> list[dict[str, str]]:
    deps = _json(path).get("dependencies")
    return [_item(name, version, "unity", rel) for name, version in deps.items()] if isinstance(deps, dict) else []


def _pubspec(path: Path, rel: str) -> list[dict[str, str]]:
    out, section = [], ""
    for line in _read(path).splitlines():
        if re.match(r"^(dependencies|dev_dependencies):\s*$", line):
            section = line.split(":")[0]
        elif line and not line.startswith((" ", "\t", "#")):
            section = ""
        elif section:
            found = re.match(r"^  ([A-Za-z0-9_]+):\s*(\S.*)?$", line)
            if found and found.group(1) != "flutter":
                out.append(_item(found.group(1), (found.group(2) or "").strip().strip("\"'"), "dart", rel, "dev" if section == "dev_dependencies" else "runtime"))
    return out


READERS = {"package.json": _package_json, "pyproject.toml": _pyproject, "Package.swift": _package_swift, "Cargo.toml": _cargo, "go.mod": _go_mod,
           "Gemfile": _gemfile, "Podfile": _podfile, "build.gradle": _gradle, "build.gradle.kts": _gradle, "pubspec.yaml": _pubspec}


def _manifests(root: Path, depth: int = 2) -> list[Path]:
    found: list[Path] = []

    def walk(folder: Path, level: int) -> None:
        try:
            children = sorted(folder.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir():
                if child.name not in SKIP_DIRS and not child.name.startswith(".") and level < depth:
                    walk(child, level + 1)
            elif (child.name in READERS or re.fullmatch(r"requirements[\w.-]*\.txt", child.name)
                  or (child.name == "manifest.json" and child.parent.name == "Packages")):
                found.append(child)

    walk(root, 0)
    return found


def detect(root: Path) -> list[dict[str, str]]:
    """What the project's manifests say it uses, one entry per package per manifest. Reads only; nothing is run."""
    root = Path(root)
    out: list[dict[str, str]] = []
    for path in _manifests(root):
        rel = str(path.relative_to(root))
        if path.name in READERS:
            out += READERS[path.name](path, rel)
        elif path.name == "manifest.json":
            out += _unity(path, rel)
        else:
            out += _requirements(path, rel)
    seen, unique = set(), []
    for item in out:
        key = (item["ecosystem"], _norm(item["ecosystem"], item["name"]), item["manifest"])
        if item["name"] and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:600]


# ---------------------------------------------------------------- the record


def store_path(runtime: Path) -> Path:
    return Path(runtime) / "packages.json"


def load(runtime: Path) -> dict[str, Any]:
    try:
        data = json.loads(store_path(runtime).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"packages": [p for p in data.get("packages", []) if isinstance(p, dict) and p.get("id")], "review": data.get("review") if isinstance(data.get("review"), dict) else None,
            "auto": data.get("auto", True) is not False,  # review again when the product requirements change
            "prd_hash": data.get("prd_hash") if isinstance(data.get("prd_hash"), str) else None,  # the requirements the last review (or the first look) read
            "prd_snapshot": data.get("prd_snapshot") if isinstance(data.get("prd_snapshot"), str) else "",  # their text, to measure how much has changed since
            "running": data.get("running") if isinstance(data.get("running"), str) else ""}  # when an automatic review started, while one is going


def save(runtime: Path, data: dict[str, Any]) -> None:
    path = store_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def prd_fingerprint(text: str) -> str:
    """A short fingerprint of the requirements that ignores whitespace, so only a real change counts."""
    return hashlib.sha1(re.sub(r"\s+", " ", text or "").strip().encode("utf-8")).hexdigest()[:16]


MIN_NEW_CHARS = 300  # an automatic review needs about a paragraph of new requirements: tweaks and one-line additions don't qualify on their own
SNAPSHOT_CHARS = 30_000


def new_content(old: str, new: str) -> int:
    """How much new text the requirements have gained since `old`, in characters: added or rewritten words, so deleting, moving or retyping
    the same thing counts for little. Small edits add up, because `old` stays the text the last review read."""
    before, after = old.split(), new.split()
    added = 0
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            added += len(" ".join(after[j1:j2]))
    return added


def is_substantial(old: str, new: str) -> bool:
    return new_content(old, new) >= MIN_NEW_CHARS


def set_auto(runtime: Path, enabled: bool) -> None:
    data = load(runtime)
    data["auto"] = bool(enabled)
    save(runtime, data)


def note_prd(runtime: Path, fingerprint: str, *, running: str = "", error: str = "", snapshot: str | None = None) -> None:
    """Remember which requirements have been looked at (`snapshot` is their text), and whether a review is going. `error` records why one couldn't finish."""
    data = load(runtime)
    data["prd_hash"], data["running"] = fingerprint, running
    if snapshot is not None:
        data["prd_snapshot"] = snapshot[:SNAPSHOT_CHARS]
    if error:
        data["review"] = {**(data["review"] or {}), "at": datetime.now().astimezone().isoformat(timespec="seconds"), "error": error}
    save(runtime, data)


REGISTRY_PAGES = {"npm": "https://www.npmjs.com/package/{n}", "pip": "https://pypi.org/project/{n}", "cargo": "https://crates.io/crates/{n}",
                  "ruby": "https://rubygems.org/gems/{n}", "dart": "https://pub.dev/packages/{n}", "cocoapods": "https://cocoapods.org/pods/{n}",
                  "unity": "https://openupm.com/packages/{n}", "go": "https://pkg.go.dev/{n}"}


def link_for(package: dict[str, Any]) -> str:
    """A page about the package: its own link when it has one, otherwise where its registry lists it."""
    if package.get("url"):
        return package["url"]
    ecosystem, name = package.get("ecosystem", ""), str(package.get("name") or "")
    if ecosystem in REGISTRY_PAGES:
        return REGISTRY_PAGES[ecosystem].format(n=name)
    if ecosystem == "gradle" and ":" in name:
        group, artifact = name.split(":", 1)
        return f"https://mvnrepository.com/artifact/{group}/{artifact}"
    if ecosystem == "swiftpm":
        return f"https://github.com/{name}" if re.fullmatch(r"[\w.-]+/[\w.-]+", name) else f"https://swiftpackageindex.com/search?query={name}"
    return ""


def suggested_count(runtime: Path) -> int:
    return sum(1 for p in load(runtime)["packages"] if p.get("status") == "suggested")


def ecosystem_of(value: Any) -> str:
    raw = str(value or "").strip().lower()
    raw = ALIASES.get(raw, raw)
    if raw not in ECOSYSTEMS:
        raise PackageError(f"Choose an ecosystem: {', '.join(ECOSYSTEMS)}.")
    return raw


def _text(value: Any, limit: int) -> str:
    return re.sub(r"[ \t]+", " ", str(value or "")).strip()[:limit]


def _url(value: Any) -> str:
    url = str(value or "").strip()
    return url[:500] if re.fullmatch(r"https://\S{4,500}", url) else ""


def clean(raw: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """A record from form-like input; fields left out keep their old value. Raises PackageError for what can't work."""
    old = existing or {}
    get = lambda key, default="": raw.get(key, old.get(key, default))
    name = _text(get("name"), 100)
    if not NAME.match(name):
        raise PackageError("Give the package's name as its registry or repository uses it, like lodash or pointfreeco/swift-snapshot-testing.")
    return {"id": old.get("id") or "", "name": name, "ecosystem": ecosystem_of(get("ecosystem")), "version": _text(get("version"), 40),
            "purpose": _text(get("purpose"), 300), "why": _text(get("why"), 400), "feature": _text(get("feature"), 60), "url": _url(get("url")),
            "notes": _text(get("notes"), 1000), "status": old.get("status", "planned"), "origin": old.get("origin", "manual"), "added": old.get("added", "")}


def _id_for(ecosystem: str, name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", f"{ecosystem}-{name}".lower()).strip("-")[:60] or "pkg"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def _find(packages: list[dict[str, Any]], package_id: str) -> dict[str, Any]:
    for p in packages:
        if p["id"] == package_id:
            return p
    raise PackageError("That package isn't in the list any more.")


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a["ecosystem"] == b["ecosystem"] and _norm(a["ecosystem"], a["name"]) == _norm(b["ecosystem"], b["name"])


REGISTRY_LINKS = (
    (r"(?:www\.)?npmjs\.com/package/((?:@[^/]+/)?[^/?#]+)", "npm"), (r"pypi\.org/project/([^/?#]+)", "pip"), (r"crates\.io/crates/([^/?#]+)", "cargo"),
    (r"rubygems\.org/gems/([^/?#]+)", "ruby"), (r"pub\.dev/packages/([^/?#]+)", "dart"), (r"cocoapods\.org/pods/([^/?#]+)", "cocoapods"),
    (r"openupm\.com/packages/([^/?#]+)", "unity"),
)


def from_url(url: Any) -> dict[str, str] | None:
    """The package a link points to: {name, ecosystem}. A registry page says which ecosystem; a GitHub link doesn't (ecosystem is empty).
    None when it isn't a link this knows."""
    text = re.sub(r"^https?://", "", str(url or "").strip()).split("#", 1)[0].rstrip("/")
    if not text:
        return None
    for pattern, ecosystem in REGISTRY_LINKS:
        found = re.match(pattern, text, re.I)
        if found:
            return {"name": found.group(1), "ecosystem": ecosystem}
    found = re.match(r"pkg\.go\.dev/([^?#@]+)", text, re.I)
    if found:
        return {"name": found.group(1), "ecosystem": "go"}
    found = re.match(r"(?:mvnrepository\.com/artifact|central\.sonatype\.com/artifact|search\.maven\.org/artifact)/([^/?#]+)/([^/?#]+)", text, re.I)
    if found:
        return {"name": f"{found.group(1)}:{found.group(2)}", "ecosystem": "gradle"}
    found = re.match(r"(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/.*)?$", text, re.I)
    if found:
        return {"name": f"{found.group(1)}/{found.group(2)}", "ecosystem": ""}
    return None


def add(runtime: Path, raw: dict[str, Any], project_ecosystems: list[str] | None = None) -> dict[str, Any]:
    """The person's own entry: planned, and theirs to edit. A link alone is enough: the name and ecosystem are read from it
    (a GitHub link doesn't say which package manager it's for, so it falls back to the project's, when it has just one)."""
    data = load(runtime)
    raw = dict(raw)
    derived = from_url(raw.get("url"))
    if derived:
        raw["name"] = _text(raw.get("name"), 100) or derived["name"]
        raw["ecosystem"] = _text(raw.get("ecosystem"), 40) or derived["ecosystem"]
        if not raw["ecosystem"] and len(set(project_ecosystems or [])) == 1:
            raw["ecosystem"] = (project_ecosystems or [""])[0]
        if not raw["ecosystem"]:
            raise PackageError("That's a GitHub link, which doesn't say which package manager it's for. Choose the ecosystem.")
    elif _text(raw.get("url"), 500) and not _text(raw.get("name"), 100):
        raise PackageError("I don't know that link. Paste a page from npm, PyPI, crates.io, pub.dev, RubyGems, CocoaPods, pkg.go.dev, Maven or GitHub, or type the name.")
    record = clean({**raw, "status": "planned"})
    record.update(status="planned", origin="manual", added=datetime.now().astimezone().isoformat(timespec="seconds"))
    if any(_same(p, record) for p in data["packages"]):
        raise PackageError(f"{record['name']} is already in the list.")
    record["id"] = _id_for(record["ecosystem"], record["name"], {p["id"] for p in data["packages"]})
    data["packages"].append(record)
    save(runtime, data)
    return record


def update(runtime: Path, package_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    data = load(runtime)
    current = _find(data["packages"], package_id)
    record = clean(raw, current)
    if any(p["id"] != package_id and _same(p, record) for p in data["packages"]):
        raise PackageError(f"{record['name']} is already in the list.")
    current.update({k: v for k, v in record.items() if k != "id"})
    save(runtime, data)
    return current


def set_status(runtime: Path, package_id: str, status: str) -> dict[str, Any]:
    if status not in STATUSES:
        raise PackageError("Unknown status.")
    data = load(runtime)
    current = _find(data["packages"], package_id)
    current["status"] = status
    save(runtime, data)
    return current


def remove(runtime: Path, package_id: str) -> dict[str, Any]:
    data = load(runtime)
    gone = _find(data["packages"], package_id)
    data["packages"] = [p for p in data["packages"] if p["id"] != package_id]
    save(runtime, data)
    return gone


def merge_suggestions(runtime: Path, suggestions: list[dict[str, Any]], review: dict[str, Any]) -> list[dict[str, Any]]:
    """Add a model's suggestions as 'suggested', skipping any the project already has or the person already decided on."""
    data = load(runtime)
    added = []
    for raw in suggestions:
        record = {**raw, "id": "", "status": "suggested", "origin": "ai", "added": review.get("at", "")}
        if any(_same(p, record) for p in data["packages"]):
            continue
        record["id"] = _id_for(record["ecosystem"], record["name"], {p["id"] for p in data["packages"]})
        data["packages"].append(record)
        added.append(record)
    review = dict(review)
    snapshot = review.pop("prd_text", None)  # the text the review read: kept to measure later changes, not shown
    data["review"] = review
    data["running"] = ""
    if review.get("prd_hash"):
        data["prd_hash"] = review["prd_hash"]
    if snapshot is not None:
        data["prd_snapshot"] = snapshot[:SNAPSHOT_CHARS]
    save(runtime, data)
    return added


def overview(root: Path, runtime: Path) -> dict[str, Any]:
    """Everything the Packages page shows: what's installed, and the record with each entry marked installed when a manifest has it."""
    installed = detect(root)
    have = {(i["ecosystem"], _norm(i["ecosystem"], i["name"])) for i in installed}
    data = load(runtime)
    packages = [{**p, "installed": (p["ecosystem"], _norm(p["ecosystem"], p["name"])) in have, "link": link_for(p)} for p in data["packages"]]
    return {"installed": installed, "packages": packages, "review": data["review"], "ecosystems": ECOSYSTEMS, "auto": data["auto"], "running": bool(data["running"])}


# ---------------------------------------------------------------- the research step


def review_prompt(prd: str, features: list[dict[str, Any]], installed: list[dict[str, str]], stack: str, *, web: bool,
                  decided: list[dict[str, Any]] | None = None) -> str:
    feature_lines = [f"- {f.get('name')}: {f.get('summary') or '(no summary)'}" + (f" [code: {', '.join(f['paths'][:4])}]" if f.get("paths") else "")
                     for f in features[:40]]
    have = [f"{i['ecosystem']}: {i['name']}" for i in installed[:120]]
    skip = [f"{p['ecosystem']}: {p['name']} ({p['status']})" for p in (decided or [])[:60]]
    return "\n".join([
        "You advise on which packages and libraries would genuinely help a software project, given what it is meant to do.",
        "Read the product requirements and the features, then suggest packages (libraries, SDKs, plugins) the project does not already use.",
        "",
        "Rules:",
        "- Suggest a package only for a need the requirements or a feature actually states. No speculative extras, no packages that merely look popular.",
        "- Prefer well maintained, widely used, permissively licensed packages that fit the project's language and platform. Say why in one sentence.",
        "- Tie each one to the feature it helps (use the feature's exact name from the list below), or leave feature empty for a project-wide need.",
        "- Never suggest anything listed under 'Already in the project' or 'Already decided'. Prefer the platform's own capabilities over a package when they are enough.",
        f"- At most {MAX_SUGGESTIONS} suggestions. Fewer is better; an empty list is a fine answer.",
        f"- ecosystem must be one of: {', '.join(ECOSYSTEMS)}.",
        "- name is as the registry or repository spells it (lodash, requests, pointfreeco/swift-snapshot-testing, com.squareup.okhttp3:okhttp).",
        "- url is the package's own repository, registry page or documentation (https only).",
        ("- Search the web to confirm each package still exists, is maintained, and what its current version is. Put the pages you used in sources."
         if web else "- You cannot search the web in this run, so go from what you know, say so in notes, and leave version empty rather than guess it."),
        "",
        f"Project stack: {stack or 'unknown'}",
        "",
        "Product requirements:", prd.strip()[:8000] or "(none written yet)",
        "", "Features:", *(feature_lines or ["(none defined)"]),
        "", "Already in the project:", *(have or ["(nothing detected)"]),
        *(["", "Already decided (do not suggest again):", *skip] if skip else []),
        "",
        "Answer with ONLY one JSON object and nothing else:",
        '{"packages": [{"name": "...", "ecosystem": "...", "purpose": "what it is for, a few words", "why": "one sentence on why this project needs it",',
        '  "feature": "exact feature name or empty", "url": "https://...", "version": "current version or empty"}],',
        ' "notes": "one or two sentences on what you looked at and anything the person should know",',
        ' "sources": ["https://pages you read"]}',
    ])


REMINDER = ('\n\nYour last answer could not be used. Reply with ONLY one JSON object, no prose and no code fence, shaped like '
            '{"packages": [{"name": "...", "ecosystem": "...", "purpose": "...", "why": "...", "feature": "", "url": "https://...", "version": ""}], '
            '"notes": "...", "sources": []}.')


def _object_from(reply: str) -> Any:
    text = reply.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise PackageError("The model didn't suggest any packages.")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        raise PackageError("The model's answer wasn't readable.")
    return value


def parse_suggestions(reply: str, features: list[dict[str, Any]], installed: list[dict[str, str]],
                      decided: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """{"packages": [...], "notes": str, "sources": [urls]} from a model's answer: valid ecosystems and names, features matched by name,
    nothing the project already has, no duplicates. An empty list is allowed. Raises PackageError if the answer isn't usable."""
    value = _object_from(reply)
    if not isinstance(value, dict) or not isinstance(value.get("packages", []), list):
        raise PackageError("The model's answer wasn't a list of packages.")
    by_name = {str(f.get("name") or "").strip().lower(): str(f.get("id") or "") for f in features}
    have = {(i["ecosystem"], _norm(i["ecosystem"], i["name"])) for i in installed}
    have |= {(p["ecosystem"], _norm(p["ecosystem"], p["name"])) for p in (decided or [])}
    out = []
    for item in value.get("packages", []):
        if not isinstance(item, dict):
            continue
        try:
            record = clean({"name": item.get("name"), "ecosystem": item.get("ecosystem"), "version": item.get("version"), "purpose": item.get("purpose"),
                            "why": item.get("why"), "url": item.get("url")})
        except PackageError:
            continue
        key = (record["ecosystem"], _norm(record["ecosystem"], record["name"]))
        if key in have:
            continue
        have.add(key)
        feature = str(item.get("feature") or "").strip().lower()
        record["feature"] = by_name.get(feature, "")
        out.append({k: record[k] for k in ("name", "ecosystem", "version", "purpose", "why", "feature", "url")})
    sources = [u for u in (_url(s) for s in value.get("sources", []) if isinstance(s, str)) if u][:12] if isinstance(value.get("sources"), list) else []
    return {"packages": out[:MAX_SUGGESTIONS], "notes": _text(value.get("notes"), 600), "sources": sources}
