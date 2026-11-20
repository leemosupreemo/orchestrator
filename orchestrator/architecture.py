"""The project's architecture: which language, frameworks, structure and packages it is built with, and why.

Without this step a new project's first feature job picks the stack as a side effect of five small tasks, with no
alternatives weighed and nothing written down, and an imported project's architecture is something every AI re-guesses.
Here a model proposes or describes it, the person approves it, and the approved decision steers every later job.

Four kinds of proposal (`MODES`):

  initial    a new project with no code: two or three candidate stacks in plain words, each with a fit score for the
             product described, simple pros and cons, and a recommendation
  describe   an imported project with code: what its architecture is now, read from an evidence digest of the repository
             (manifests, packages, folders, languages, entry points), and where it diverges from the product requirements
  tweak      an addition or adjustment as the product grows (a package, a layer, a storage choice); the stack stays
  overhaul   a rare rethink: candidates again, the current architecture among them

Whatever the model calls it, `change_kind` decides from the content: a change to the primary language, framework, chosen
candidate or the dominant system shape is an overhaul; anything else is a tweak. Approving bumps the version to match
(an overhaul 1.3 -> 2.0, a tweak 1.3 -> 1.4) and keeps a short history.

Architecture is described with a fixed catalog of styles in three categories (`STYLES`): system shape, code organisation and
UI pattern. Real projects are mixtures, so each proposal carries a weighted profile, every category summing to 100
("80% app with its own backend, 20% hosted backend services").

Everything is tied to the product requirements: each decision names the requirements it serves, `prd_links` says how the
architecture meets each core need, `prd_gaps` lists needs it doesn't meet yet, and the requirements' fingerprint is kept at
approval so a later change to them can prompt a review (`revisit_reasons` and `prd_changed` only flag; nothing changes a
decision without approval).

The step is soft and can be skipped, but there is always a current architecture once anyone has looked: skipping keeps the
recommended option (or, with no proposal, an inference from the repository) as a *working assumption* (`status: provisional`,
versions 0.x). Jobs follow a working assumption more loosely than an approved decision, and approving any proposal (or the
assumption as it is) makes it version 1.0. Either kind keeps changing as the product does.

Files:

  proposal   `.orchestrator/architecture-proposal.json`   the latest model answer, not yet approved (runtime, untracked)
  decision   `docs/architecture.json`                      the approved record, tracked so every worktree sees it
             `docs/architecture.md`                        the same decision written for people and every AI prompt

Pure functions apart from reading the repository: callers pass the model in and decide when to commit.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

RECORD_PATH = "docs/architecture.json"
DOC_PATH = "docs/architecture.md"
PROPOSAL_FILE = "architecture-proposal.json"
MAX_PRD_CHARS = 12_000
MAX_FEEDBACK_CHARS = 4_000
MAX_HISTORY = 20
REVIEW_BUDGET_SECONDS = 420

MODES = ("initial", "describe", "tweak", "overhaul")
STATUSES = ("approved", "provisional")  # provisional: a working assumption, kept when the step is skipped, until someone approves one

# The languages stack detection, the builder and the test runner know how to drive (see stack_detection.DetectedStack).
LANGUAGES = ("swift", "kotlin", "java", "typescript", "javascript", "python", "go", "rust", "csharp", "dart", "ruby", "generic")

# The catalog every proposal is described with. Categories are independent: one style mix per category, each summing to 100.
CATEGORIES: dict[str, dict[str, str]] = {
    "shape": {"title": "System shape", "question": "What runs where"},
    "code": {"title": "Code organisation", "question": "How the code is divided up"},
    "ui": {"title": "UI pattern", "question": "How screens get and change their data"},
}
STYLES: dict[str, dict[str, str]] = {
    # system shape
    "client_only": {"category": "shape", "name": "App only", "plain": "Everything runs on the device or in the browser; no server of your own."},
    "client_server": {"category": "shape", "name": "App with its own backend", "plain": "An app talks to a server you write and run."},
    "hosted_backend": {"category": "shape", "name": "App with hosted backend services", "plain": "An app uses ready-made services (Firebase, Supabase, CloudKit) instead of your own server."},
    "serverless": {"category": "shape", "name": "Serverless functions", "plain": "Small pieces of server code that run on demand in the cloud."},
    "monolith": {"category": "shape", "name": "Single server app", "plain": "One server program that does everything, including its web pages."},
    "services": {"category": "shape", "name": "Several services", "plain": "Separate server programs that each own one job and talk over the network."},
    "static_site": {"category": "shape", "name": "Static site", "plain": "Pages built ahead of time and served as files."},
    "tool_or_library": {"category": "shape", "name": "Tool or library", "plain": "A command-line tool or a package other code uses."},
    # code organisation
    "layered": {"category": "code", "name": "Layers", "plain": "Screens, logic and data access each live in their own layer."},
    "clean": {"category": "code", "name": "Clean core", "plain": "Business rules sit in the middle and don't depend on frameworks or the database."},
    "feature_modules": {"category": "code", "name": "Feature modules", "plain": "Code is grouped by feature, each folder holding everything that feature needs."},
    "framework_conventions": {"category": "code", "name": "Framework conventions", "plain": "The framework decides where things go (Rails, Django, Next.js)."},
    "flat": {"category": "code", "name": "Flat / scripts", "plain": "A few files without much structure; fine while it is small."},
    # UI pattern
    "mvvm": {"category": "ui", "name": "View models (MVVM)", "plain": "Each screen has a helper object holding its data and actions."},
    "mvc": {"category": "ui", "name": "Controllers (MVC)", "plain": "Controllers sit between screens and data."},
    "single_state": {"category": "ui", "name": "Single state store", "plain": "All app state lives in one place and changes through named actions (Redux, TCA, MVI)."},
    "component_state": {"category": "ui", "name": "Component state", "plain": "Each component keeps its own state and passes data down (React hooks, SwiftUI @State)."},
    "server_rendered": {"category": "ui", "name": "Server-rendered pages", "plain": "The server builds each page; the browser mostly displays it."},
    "no_ui": {"category": "ui", "name": "No user interface", "plain": "Nothing for people to look at: an API, a tool or a library."},
}

TOOLS = {
    "xcodebuild": "Xcode (iOS / macOS apps)", "swift": "Swift toolchain", "node": "Node.js", "npm": "npm", "pnpm": "pnpm",
    "bun": "Bun", "python3": "Python 3", "java": "Java runtime", "gradle": "Gradle", "go": "Go", "cargo": "Rust (cargo)",
    "flutter": "Flutter", "dotnet": ".NET", "docker": "Docker",
}

LANGUAGE_BY_SUFFIX = {".swift": "Swift", ".kt": "Kotlin", ".kts": "Kotlin", ".java": "Java", ".ts": "TypeScript", ".tsx": "TypeScript",
                      ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".py": "Python", ".go": "Go", ".rs": "Rust",
                      ".cs": "C#", ".dart": "Dart", ".rb": "Ruby", ".m": "Objective-C", ".mm": "Objective-C", ".c": "C", ".cc": "C++",
                      ".cpp": "C++", ".vue": "Vue", ".svelte": "Svelte"}
SOURCE_SUFFIXES = set(LANGUAGE_BY_SUFFIX)
MANIFESTS = ("package.json", "Package.swift", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod", "pubspec.yaml",
             "Gemfile", "settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts", "Podfile")
SKIP_DIRS = {".git", ".orchestrator", "docs", "node_modules", ".build", "build", "dist", "Pods", "DerivedData", ".venv", "venv",
             "__pycache__", ".worktrees", "target", "vendor", "Library", "Temp", "obj"}
ENTRY_NAMES = re.compile(r"^(main|index|app|server|App|AppDelegate|MainActivity|manage|wsgi|asgi|routes?|urls|Program|lib)\.[A-Za-z]+$|App\.swift$")

REMINDER = ("\n\nYour last answer could not be read. Answer with ONLY the JSON object described above: no prose before or after it, "
            "every brace and quote balanced, `chosen` equal to the `name` of one of the candidates, and `stack.language` one of the "
            "allowed languages.")


class ArchitectureError(ValueError):
    pass


# --------------------------------------------------------------------------- where things are


def record_path(root: Path) -> Path:
    return root / RECORD_PATH


def doc_path(root: Path) -> Path:
    return root / DOC_PATH


def proposal_path(runtime: Path) -> Path:
    return runtime / PROPOSAL_FILE


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_decision(root: Path) -> dict[str, Any] | None:
    """The current architecture: an approved decision, or a working assumption ("provisional": the person skipped the step, or it
    was inferred) that jobs follow more loosely. None when nothing is recorded; a record that can't be read counts as none."""
    data = _read_json(record_path(root))
    return data if data and data.get("status") in STATUSES and data.get("stack") else None


def is_approved(record: dict[str, Any] | None) -> bool:
    return bool(record and record.get("status") == "approved")


def load_proposal(runtime: Path) -> dict[str, Any] | None:
    return _read_json(proposal_path(runtime))


def save_proposal(runtime: Path, proposal: dict[str, Any]) -> None:
    _write_json(proposal_path(runtime), proposal)


def clear_proposal(runtime: Path) -> None:
    try:
        proposal_path(runtime).unlink()
    except OSError:
        pass


def prd_fingerprint(prd_text: str) -> str:
    """Whitespace-insensitive, so re-saving the requirements unchanged doesn't count as a change."""
    return hashlib.sha256(re.sub(r"\s+", " ", prd_text or "").strip().encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- what the project already is


def _walk(root: Path, limit: int):
    seen = 0
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            seen += 1
            if seen > limit:
                return
            yield Path(current) / name


def is_codeless(root: Path, limit: int = 4000) -> bool:
    """True while the repository has no source files or package manifests outside docs: a project that hasn't been scaffolded."""
    if any((root / name).is_file() for name in MANIFESTS):
        return False
    if any(root.glob("*.xcodeproj")) or any(root.glob("*.xcworkspace")):
        return False
    count = 0
    for path in _walk(root, limit + 1):
        count += 1
        if path.suffix.lower() in SOURCE_SUFFIXES or count > limit:
            return False
    return True


def needs_decision(root: Path) -> bool:
    """A new project that would otherwise have its stack chosen silently by the first job."""
    return load_decision(root) is None and is_codeless(root)


def default_mode(root: Path) -> str:
    """What a first proposal should be: choose for an empty project, describe for one that already has code."""
    return "initial" if is_codeless(root) else "describe"


def evidence(root: Path, limit: int = 20_000) -> dict[str, Any]:
    """A digest of the repository for describing an existing project's architecture: what a person would skim first. The model
    that reads it can't open the repository itself, so this is everything it knows about the code."""
    languages: Counter[str] = Counter()
    folders: Counter[str] = Counter()
    entries: list[str] = []
    total = 0
    for path in _walk(root, limit):
        rel = path.relative_to(root)
        total += 1
        lang = LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
        if lang:
            languages[lang] += 1
            top = "/".join(rel.parts[:2]) if len(rel.parts) > 2 else (rel.parts[0] if len(rel.parts) > 1 else ".")
            folders[top] += 1
            if ENTRY_NAMES.search(path.name) and len(entries) < 15:
                entries.append(str(rel))
    manifests = [name for name in MANIFESTS if (root / name).is_file()]
    manifests += [str(p.relative_to(root)) for p in sorted(root.glob("*/package.json"))[:5]]
    manifests += [p.name for p in sorted(root.glob("*.xcodeproj"))[:2]] + [p.name for p in sorted(root.glob("*.xcworkspace"))[:2]]
    try:
        from orchestrator import packages
        installed = [f"{p['ecosystem']}: {p['name']}" for p in packages.detect(root)][:80]
    except Exception:
        installed = []
    try:
        from orchestrator.stack_detection import detect_project_stack
        stack = detect_project_stack(root).display_name
    except Exception:
        stack = ""
    notes = ""
    for name in ("AGENTS.md", "CLAUDE.md", "README.md"):
        try:
            text = (root / name).read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if text:
            notes += f"\n--- {name} (start) ---\n{text[:2500]}\n"
    return {"stack": stack, "files": total, "languages": dict(languages.most_common(8)), "folders": dict(folders.most_common(25)),
            "manifests": manifests, "packages": installed, "entry_points": entries, "notes": notes.strip()}


def evidence_text(ev: dict[str, Any]) -> str:
    lines = [f"Detected stack: {ev.get('stack') or 'unknown'}", f"Files scanned: {ev.get('files', 0)}",
             "Source files by language: " + (", ".join(f"{k} {v}" for k, v in (ev.get("languages") or {}).items()) or "none"),
             "Manifests: " + (", ".join(ev.get("manifests") or []) or "none"),
             "Folders holding source (files per folder):", *[f"- {k}: {v}" for k, v in (ev.get("folders") or {}).items()],
             "Entry points: " + (", ".join(ev.get("entry_points") or []) or "none found"),
             "Packages in use:", *([f"- {p}" for p in (ev.get("packages") or [])] or ["- none detected"])]
    if ev.get("notes"):
        lines += ["Project notes:", ev["notes"]]
    return "\n".join(lines)


def platforms_from_prd(text: str, known: list[str]) -> tuple[list[str], str]:
    """The platforms and technology preferences the new-project wizard wrote into the pitch ("Built for: ...", "Technology
    preferences: ..."). Platforms come back empty when none were chosen, which asks the model to recommend them."""
    built = re.search(r"^Built for:\s*(.+)$", text or "", re.M)
    prefs = re.search(r"^Technology preferences:\s*(.+)$", text or "", re.M)
    chosen = [p for p in known if built and p.lower() in built.group(1).lower()]
    return chosen, (prefs.group(1).strip() if prefs else "")


def available_tools(which: Any = shutil.which) -> list[str]:
    return [label for tool, label in TOOLS.items() if which(tool)]


# --------------------------------------------------------------------------- asking a model


def _catalog_text() -> str:
    out = []
    for cat, meta in CATEGORIES.items():
        out.append(f"{cat} ({meta['title']}: {meta['question']}):")
        out += [f"  - {sid}: {s['name']}. {s['plain']}" for sid, s in STYLES.items() if s["category"] == cat]
    return "\n".join(out)


MODE_BRIEF = {
    "initial": [
        "You are a senior software architect choosing the technical foundation for a NEW project that has no code yet.",
        "Propose 2 or 3 genuinely different candidates and recommend the one that best fits the product described.",
    ],
    "describe": [
        "You are a senior software architect describing the architecture of an EXISTING project from a digest of its repository.",
        "Describe what is there now, honestly, as the chosen candidate named \"Current architecture\". Don't redesign it. You may add "
        "at most one alternative candidate only if the product requirements clearly call for a different direction.",
        "Where the code and the product requirements disagree or the code doesn't yet support a requirement, list it in `prd_gaps`.",
    ],
    "tweak": [
        "You are a senior software architect making a SMALL addition or adjustment to an approved architecture as the product grows.",
        "Keep the primary language, framework and system shape. Change only what the person's note requires; keep everything else "
        "exactly as it is. Return the full updated architecture with a single candidate: the current one, adjusted.",
    ],
    "overhaul": [
        "You are a senior software architect reconsidering an approved architecture from the ground up. This is rare and costly.",
        "Propose 2 or 3 candidates and include the current architecture as one of them, so staying put is a real option. Recommend a "
        "change only when the product requirements clearly justify the cost of rebuilding; say what moving would cost in `cons`.",
    ],
}


def proposal_prompt(prd_text: str, platforms: list[str], preferences: str, tools: list[str], platform_needs: dict[str, list[str]],
                    *, web: bool, mode: str = "initial", previous: dict[str, Any] | None = None, feedback: str = "",
                    repo_evidence: str = "") -> str:
    if mode not in MODES:
        raise ArchitectureError(f"Unknown kind of proposal: {mode}.")
    needs = [f"- {p}: {'; '.join(platform_needs.get(p, []))}" for p in platforms if platform_needs.get(p)]
    lines = [
        *MODE_BRIEF[mode],
        "",
        "Cover, for this specific product:",
        "- the language and framework for each platform, and whether platforms share code;",
        "- its structure: layers or modules and what each is responsible for, and the top-level folders;",
        "- how state, data storage, networking or the backend, and sign-in work, when the product needs them;",
        "- the few packages that are clearly needed (prefer the platform's own capabilities when they are enough);",
        "- build and test commands that work once the first slice exists.",
        "",
        "Explain it for someone who isn't an engineer:",
        "- Each candidate gets `best_for` (one short plain sentence: the kind of product it suits), 2 to 4 `pros` and 2 to 4 `cons` in "
        "everyday words (\"works offline\", \"one codebase for iPhone and Android\", \"you pay for a server\"), and `fit`, 0 to 100, for how "
        "well it fits THIS product's requirements.",
        "- Describe each candidate with a weighted `profile` using ONLY the style ids below. Real architectures are mixtures: for each "
        "category give the styles that apply with weights summing to 100 (e.g. {\"shape\": {\"client_server\": 80, \"hosted_backend\": 20}}).",
        f"- Each candidate gets a very simple `diagram`: at most {MAX_NODES} boxes and {MAX_EDGES} arrows. Each node has an `id`, a short "
        f"`label` (2 to 4 words) and a `kind` from: {', '.join(DIAGRAM_KINDS)} (user = who uses it, app = what they open, service = code "
        "that runs behind it, data = where data is kept, external = someone else's service). Edges go `from` → `to` with a 1 to 3 word "
        "`label` (\"syncs\", \"signs in\"). Show only the main pieces." + (" For the current architecture, draw your best approximation "
                                                                             "from the repository digest." if mode == "describe" else ""),
        "",
        "Tie it to the product requirements:",
        "- Each decision lists in `serves` the requirements it exists for, quoted briefly from the requirements below.",
        "- `prd_links` maps each core need in the requirements to how the architecture meets it.",
        "- `prd_gaps` lists needs the architecture doesn't meet yet (empty when none).",
        "- If there are no requirements yet, choose conservatively and say what you assumed in `open_questions`.",
        "",
        "Rules:",
        "- Prefer what this Mac can already build and test (listed below). If the best choice needs a missing tool, say so in risks.",
        "- Prefer boring, well-supported, actively maintained technology a small team can keep running. Nothing just because it's fashionable.",
        "- Every decision gets the alternatives you rejected and a one-sentence reason.",
        "- `revisit_when` lists concrete product changes that would make you choose differently (\"offline use becomes required\").",
        "- Keep it readable in two minutes: at most 6 decisions, 6 layers, 8 packages, 3 risks, 3 open questions.",
        "- If platforms were not chosen, recommend which to build for, with reasons, in `summary` and the first decision." if not platforms and mode == "initial" else "",
        ("- Search the web to check that the frameworks and packages you name are current and maintained. Put the pages in `sources`."
         if web else "- You cannot search the web in this run; go from what you know and leave versions out rather than guess them."),
        f"- `stack.language` must be one of: {', '.join(LANGUAGES)} (the primary language, used to build and test the project).",
        "",
        "Style catalog (use these ids in every `profile`):",
        _catalog_text(),
        "",
        f"Platforms chosen: {', '.join(platforms) if platforms else 'not decided'}",
        f"Technology preferences from the person: {preferences.strip() or '(none given)'}",
        f"Tools installed on this Mac: {', '.join(tools) if tools else '(could not tell)'}",
    ]
    if needs:
        lines += ["What each platform needs to be built, tested and shipped by Orchestrator:", *needs]
    if repo_evidence:
        lines += ["", "Repository digest (you can't open the repository; this is what is known about the code):", repo_evidence[:12_000]]
    lines += ["", "Product requirements:", (prd_text.strip()[:MAX_PRD_CHARS] or "(none written yet)")]
    if previous:
        lines += ["", "The current architecture:" if mode in ("tweak", "overhaul") else "Your previous proposal:",
                  json.dumps(_strip_meta(previous), indent=2, ensure_ascii=False)[:8000]]
    if feedback.strip():
        lines += ["", "The person's note (follow it unless it is unsafe or impossible; if so, say why in `notes`):", feedback.strip()[:MAX_FEEDBACK_CHARS]]
    lines += [
        "",
        "Answer with ONLY one JSON object and nothing else:",
        json.dumps({
            "summary": "two or three plain sentences: what this is and why it fits this product",
            "candidates": [{"name": "short name, e.g. Native SwiftUI + CloudKit", "best_for": "plain sentence", "fit": 85,
                            "pros": ["..."], "cons": ["..."], "profile": {"shape": {"hosted_backend": 100}, "code": {"layered": 70, "feature_modules": 30}, "ui": {"mvvm": 100}},
                            "diagram": {"nodes": [{"id": "player", "label": "Player", "kind": "user"}, {"id": "app", "label": "iPhone app", "kind": "app"},
                                                  {"id": "cloud", "label": "CloudKit", "kind": "external"}],
                                        "edges": [{"from": "player", "to": "app", "label": "plays"}, {"from": "app", "to": "cloud", "label": "syncs"}]}}],
            "chosen": "the name of one candidate",
            "stack": {"language": "swift", "framework": "SwiftUI", "build_command": "...", "test_command": "..."},
            "platforms": [{"platform": "iOS app", "language": "Swift", "framework": "SwiftUI", "notes": "..."}],
            "layers": [{"name": "...", "responsibility": "..."}],
            "structure": ["Sources/App/: entry point and screens"],
            "packages": [{"name": "...", "ecosystem": "...", "purpose": "..."}],
            "decisions": [{"topic": "Persistence", "choice": "...", "alternatives": ["..."], "reason": "...", "serves": ["requirement"]}],
            "prd_links": [{"requirement": "a core need, briefly quoted", "how": "how the architecture meets it"}],
            "prd_gaps": ["..."],
            "risks": ["..."], "open_questions": ["..."], "revisit_when": ["..."], "notes": "", "sources": ["https://..."],
        }, indent=2),
    ]
    return "\n".join(line for line in lines if line is not None)


META_KEYS = ("status", "at", "approved_at", "approved_by", "model", "searched", "feedback_history", "prd_hash", "history",
             "version", "revision", "kind", "mode", "note", "changes")


def _strip_meta(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k not in META_KEYS}


def _json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ArchitectureError("The model's answer had no architecture in it.")
    try:
        data = json.loads(candidate[start:end + 1])
    except ValueError:
        raise ArchitectureError("The model's answer couldn't be read as an architecture.") from None
    if not isinstance(data, dict):
        raise ArchitectureError("The model's answer couldn't be read as an architecture.")
    return data


def _text(value: Any, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _texts(value: Any, n: int, limit: int = 300) -> list[str]:
    if not isinstance(value, list):
        return []
    return [t for t in (_text(v, limit) for v in value) if t][:n]


def _items(value: Any, keys: dict[str, int], n: int, list_keys: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        item: dict[str, Any] = {k: _text(raw.get(k), limit) for k, limit in keys.items()}
        for k in list_keys:
            item[k] = _texts(raw.get(k), 6, 200)
        if any(item.get(k) for k in keys):
            out.append(item)
    return out[:n]


def normalize_profile(raw: Any) -> dict[str, dict[str, int]]:
    """Only catalog styles, each in its own category, weights rounded to whole percents summing to exactly 100 per category."""
    if not isinstance(raw, dict):
        return {}
    by_cat: dict[str, dict[str, float]] = {}
    for key, value in raw.items():
        # Accept both {"shape": {"client_server": 80}} and a flat {"client_server": 80}.
        pairs = value.items() if isinstance(value, dict) else [(key, value)]
        for sid, weight in pairs:
            style = STYLES.get(str(sid))
            try:
                w = float(weight)
            except (TypeError, ValueError):
                continue
            if style and w > 0:
                cat = by_cat.setdefault(style["category"], {})
                cat[str(sid)] = cat.get(str(sid), 0.0) + w
    out: dict[str, dict[str, int]] = {}
    for cat in CATEGORIES:
        weights = by_cat.get(cat)
        if not weights:
            continue
        total = sum(weights.values())
        shares = {sid: w * 100 / total for sid, w in weights.items()}
        rounded = {sid: int(s) for sid, s in shares.items()}
        # Largest remainders get the leftover points, so the category adds up to 100.
        for sid in sorted(shares, key=lambda s: shares[s] - rounded[s], reverse=True)[:100 - sum(rounded.values())]:
            rounded[sid] += 1
        out[cat] = dict(sorted(((s, w) for s, w in rounded.items() if w > 0), key=lambda kv: -kv[1]))
    return out


# A deliberately simple diagram: boxes in columns by kind (who uses it, what they use, what runs behind it, where data lives,
# outside services) joined by labelled arrows. Enough to see the shape of an option at a glance, not a full system drawing.
DIAGRAM_KINDS = ("user", "app", "service", "data", "external")
MAX_NODES = 10
MAX_EDGES = 14


def normalize_diagram(raw: Any) -> dict[str, list[dict[str, str]]]:
    if not isinstance(raw, dict):
        return {"nodes": [], "edges": []}
    nodes: list[dict[str, str]] = []
    ids: set[str] = set()
    for item in raw.get("nodes") if isinstance(raw.get("nodes"), list) else []:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label") or item.get("id"), 40)
        node_id = re.sub(r"[^a-z0-9_]", "", _text(item.get("id") or label, 40).lower().replace(" ", "_"))[:30]
        if not label or not node_id or node_id in ids:
            continue
        kind = _text(item.get("kind"), 20).lower()
        nodes.append({"id": node_id, "label": label, "kind": kind if kind in DIAGRAM_KINDS else "service"})
        ids.add(node_id)
        if len(nodes) == MAX_NODES:
            break
    edges: list[dict[str, str]] = []
    for item in raw.get("edges") if isinstance(raw.get("edges"), list) else []:
        if not isinstance(item, dict):
            continue
        a = re.sub(r"[^a-z0-9_]", "", _text(item.get("from"), 40).lower().replace(" ", "_"))
        b = re.sub(r"[^a-z0-9_]", "", _text(item.get("to"), 40).lower().replace(" ", "_"))
        if a in ids and b in ids and a != b:
            edges.append({"from": a, "to": b, "label": _text(item.get("label"), 30)})
        if len(edges) == MAX_EDGES:
            break
    return {"nodes": nodes, "edges": edges}


def mermaid(diagram: dict[str, Any] | None) -> str:
    """The diagram as a Mermaid flowchart, for docs/architecture.md (GitHub and most editors draw it)."""
    if not diagram or not diagram.get("nodes"):
        return ""
    shape = {"user": ("([", "])"), "data": ("[(", ")]"), "external": ("{{", "}}")}
    clean = lambda s: re.sub(r'["\[\]{}()|<>]', "", s)
    lines = ["```mermaid", "flowchart LR"]
    for n in diagram["nodes"]:
        open_, close = shape.get(n["kind"], ("[", "]"))
        lines.append(f'  {n["id"]}{open_}"{clean(n["label"])}"{close}')
    for e in diagram.get("edges") or []:
        lines.append(f'  {e["from"]} -->|"{clean(e["label"])}"| {e["to"]}' if e.get("label") else f'  {e["from"]} --> {e["to"]}')
    lines.append("```")
    return "\n".join(lines)


def dominant(profile: dict[str, dict[str, int]], category: str) -> str:
    weights = (profile or {}).get(category) or {}
    return max(weights, key=lambda s: weights[s]) if weights else ""


def profile_line(profile: dict[str, dict[str, int]], category: str) -> str:
    """"80% App with its own backend, 20% App with hosted backend services"."""
    return ", ".join(f"{w}% {STYLES[s]['name']}" for s, w in ((profile or {}).get(category) or {}).items() if s in STYLES)


def parse_proposal(reply: str, mode: str = "initial") -> dict[str, Any]:
    """The model's answer, checked and trimmed to what the page and prompts use. Raises ArchitectureError if it isn't usable."""
    data = _json_object(reply)
    candidates = []
    for raw in data.get("candidates") if isinstance(data.get("candidates"), list) else []:
        if not isinstance(raw, dict) or not _text(raw.get("name")):
            continue
        try:
            fit = max(0, min(100, int(round(float(raw.get("fit"))))))
        except (TypeError, ValueError):
            fit = None
        candidates.append({"name": _text(raw.get("name"), 80), "best_for": _text(raw.get("best_for") or raw.get("summary"), 300),
                           "fit": fit, "pros": _texts(raw.get("pros"), 5, 200), "cons": _texts(raw.get("cons"), 5, 200),
                           "profile": normalize_profile(raw.get("profile")), "diagram": normalize_diagram(raw.get("diagram"))})
    candidates = candidates[:3]
    if not candidates:
        raise ArchitectureError("The model didn't propose any candidate architectures.")
    chosen = _text(data.get("chosen"), 80)
    names = [c["name"] for c in candidates]
    if chosen not in names:
        match = [n for n in names if chosen and (chosen.lower() in n.lower() or n.lower() in chosen.lower())]
        if not match and len(candidates) == 1:
            match = names
        if not match:
            raise ArchitectureError("The model didn't say which candidate it chose.")
        chosen = match[0]
    raw_stack = data.get("stack") if isinstance(data.get("stack"), dict) else {}
    language = _text(raw_stack.get("language"), 30).lower()
    language = {"ts": "typescript", "js": "javascript", "c#": "csharp", "objective-c": "swift", "py": "python"}.get(language, language)
    if language not in LANGUAGES:
        raise ArchitectureError(f"The model chose a primary language Orchestrator can't build ({language or 'none'}).")
    stack = {"language": language, "framework": _text(raw_stack.get("framework"), 60),
             "build_command": _text(raw_stack.get("build_command"), 200), "test_command": _text(raw_stack.get("test_command"), 200)}
    chosen_candidate = next(c for c in candidates if c["name"] == chosen)
    return {
        "mode": mode if mode in MODES else "initial",
        "summary": _text(data.get("summary"), 800),
        "candidates": candidates,
        "chosen": chosen,
        "profile": chosen_candidate["profile"] or normalize_profile(data.get("profile")),
        "diagram": chosen_candidate["diagram"],
        "stack": stack,
        "platforms": _items(data.get("platforms"), {"platform": 60, "language": 40, "framework": 60, "notes": 300}, 6),
        "layers": _items(data.get("layers"), {"name": 60, "responsibility": 300}, 6),
        "structure": _texts(data.get("structure"), 12, 200),
        "packages": _items(data.get("packages"), {"name": 120, "ecosystem": 30, "purpose": 200}, 8),
        "decisions": _items(data.get("decisions"), {"topic": 60, "choice": 200, "reason": 400}, 6, ("alternatives", "serves")),
        "prd_links": _items(data.get("prd_links"), {"requirement": 200, "how": 300}, 10),
        "prd_gaps": _texts(data.get("prd_gaps"), 6),
        "risks": _texts(data.get("risks"), 3),
        "open_questions": _texts(data.get("open_questions"), 3),
        "revisit_when": _texts(data.get("revisit_when"), 5),
        "notes": _text(data.get("notes"), 600),
        "sources": [s for s in _texts(data.get("sources"), 8, 300) if s.startswith("https://")],
    }


# --------------------------------------------------------------------------- how big a change is


def change_kind(current: dict[str, Any] | None, proposal: dict[str, Any]) -> str:
    """"initial" with nothing approved yet; otherwise "overhaul" when the foundations move (primary language, framework, chosen
    candidate, or the dominant system shape) and "tweak" for everything else. Judged from the content, not the model's label.
    A different chosen option always differs in language or framework; the same stack under a new name ("Current
    architecture") is the same choice."""
    if not current:
        return "initial"
    a, b = current.get("stack") or {}, proposal.get("stack") or {}
    if a.get("language") != b.get("language") or _text(a.get("framework")).lower() != _text(b.get("framework")).lower():
        return "overhaul"
    shape_a, shape_b = dominant(current.get("profile") or {}, "shape"), dominant(proposal.get("profile") or {}, "shape")
    if shape_a and shape_b and shape_a != shape_b:
        return "overhaul"
    return "tweak"


def what_changed(current: dict[str, Any] | None, proposal: dict[str, Any]) -> list[str]:
    """Plain lines saying what an approval would change, for the person deciding."""
    if not current:
        return []
    out = []
    a, b = current.get("stack") or {}, proposal.get("stack") or {}
    for key, label in (("language", "Primary language"), ("framework", "Framework"), ("build_command", "Build command"), ("test_command", "Test command")):
        if (a.get(key) or "") != (b.get(key) or ""):
            out.append(f"{label}: {a.get(key) or '(none)'} → {b.get(key) or '(none)'}")
    for cat, meta in CATEGORIES.items():
        before, after = profile_line(current.get("profile") or {}, cat), profile_line(proposal.get("profile") or {}, cat)
        if before != after and (before or after):
            out.append(f"{meta['title']}: {before or '(not described)'} → {after or '(not described)'}")
    for key, label, name_key in (("packages", "Package", "name"), ("layers", "Layer", "name"), ("decisions", "Decision", "topic")):
        old = {i.get(name_key) for i in current.get(key) or []}
        new = {i.get(name_key) for i in proposal.get(key) or []}
        out += [f"{label} added: {n}" for n in sorted(new - old) if n]
        out += [f"{label} removed: {n}" for n in sorted(old - new) if n]
    old_choices = {d.get("topic"): d.get("choice") for d in current.get("decisions") or []}
    out += [f"Decision changed: {d['topic']}: {old_choices[d['topic']]} → {d['choice']}"
            for d in proposal.get("decisions") or [] if d.get("topic") in old_choices and old_choices[d["topic"]] != d.get("choice")]
    return out[:20]


def next_version(current: dict[str, Any] | None, kind: str) -> str:
    if not current:
        return "1.0"
    try:
        major, minor = (int(x) for x in str(current.get("version") or "1.0").split(".", 1))
    except ValueError:
        major, minor = 1, 0
    return f"{major + 1}.0" if kind == "overhaul" else f"{major}.{minor + 1}"


# --------------------------------------------------------------------------- approving it


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def edit_proposal(proposal: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """The person's direct edits before approving: the chosen candidate, the stack's commands, and the summary."""
    out = dict(proposal)
    if changes.get("chosen") not in (None, ""):
        candidates = proposal.get("candidates", [])
        chosen = _text(changes["chosen"], 80)
        match = next((c for c in candidates if c["name"] == chosen), None)
        if match is None:
            raise ArchitectureError("Pick one of the proposed candidates, or ask for changes to get a different one.")
        out["chosen"] = chosen
        if match.get("profile"):
            out["profile"] = match["profile"]
    stack = dict(proposal.get("stack") or {})
    for key in ("framework", "build_command", "test_command"):
        if key in changes and changes[key] is not None:
            stack[key] = _text(changes[key], 200)
    if changes.get("language") not in (None, ""):
        language = _text(changes["language"], 30).lower()
        if language not in LANGUAGES:
            raise ArchitectureError(f"Language must be one of: {', '.join(LANGUAGES)}.")
        stack["language"] = language
    out["stack"] = stack
    if "summary" in changes and changes["summary"] is not None:
        out["summary"] = _text(changes["summary"], 800)
    return out


def _save_record(root: Path, record: dict[str, Any]) -> None:
    _write_json(record_path(root), record)
    doc_path(root).parent.mkdir(parents=True, exist_ok=True)
    doc_path(root).write_text(render_markdown(record), encoding="utf-8")


def approve(root: Path, runtime: Path, proposal: dict[str, Any], *, prd_text: str = "", by: str = "you") -> dict[str, Any]:
    """Write the approved decision (JSON and markdown) into the repository and clear the proposal. Returns the record.
    Approving over a working assumption makes it version 1.0. Committing is the caller's choice, so a job's worktree sees it."""
    if not proposal or not proposal.get("stack") or not proposal.get("chosen"):
        raise ArchitectureError("There's no proposal to approve yet.")
    current = load_decision(root)
    provisional = bool(current and current.get("status") == "provisional")
    kind = "approved" if provisional else change_kind(current, proposal)
    origin = "described" if proposal.get("mode") == "describe" else "proposed"
    version = "1.0" if provisional else next_version(current, kind)
    history = list((current or {}).get("history") or [])
    history.append({"version": version, "kind": "described" if (kind == "initial" and origin == "described") else kind, "at": _now(),
                    "summary": _text(proposal.get("summary"), 300), "note": _text(proposal.get("note"), 300),
                    "changes": what_changed(current, proposal)[:10]})
    keep_origin = current and not provisional
    record = {**_strip_meta(proposal), "status": "approved", "origin": current.get("origin", origin) if keep_origin else origin,
              "approved_at": _now(), "approved_by": by, "version": version, "prd_hash": prd_fingerprint(prd_text) if prd_text else "",
              "history": history[-MAX_HISTORY:]}
    _save_record(root, record)
    clear_proposal(runtime)
    return record


def approve_current(root: Path, runtime: Path, *, prd_text: str = "", by: str = "you") -> dict[str, Any]:
    """Approve the working assumption as it stands, with no new proposal."""
    current = load_decision(root)
    if not current or current.get("status") != "provisional":
        raise ArchitectureError("There's no working assumption to approve.")
    return approve(root, runtime, {**current, "mode": "describe" if current.get("origin") in ("described", "inferred") else "initial",
                                   "note": "Approved the working assumption as it was"}, prd_text=prd_text, by=by)


# --------------------------------------------------------------------------- skipping it (soft: there is always a working assumption)


def infer_record(root: Path) -> dict[str, Any]:
    """A working assumption read from the repository without a model: the detected stack, its languages and packages.
    For an empty project this is whatever the product requirements hint at, often just "not decided"."""
    ev = evidence(root)
    try:
        from orchestrator.stack_detection import detect_project_stack
        detected = detect_project_stack(root)
    except Exception:
        detected = None
    language = (detected.language if detected else "generic") or "generic"
    language = language if language in LANGUAGES else "generic"
    name = detected.display_name.replace(" (from architecture)", "") if detected else "Not decided"
    if language == "generic" and not ev["languages"]:
        name = "Not decided"
    langs = ", ".join(f"{k} ({v} files)" for k, v in ev["languages"].items())
    summary = (f"Inferred from the code: {langs}." if langs else
               "Nothing is built yet. This is a placeholder until an architecture is chosen; jobs may pick reasonable defaults and say so.")
    return {"summary": summary, "chosen": name, "candidates": [{"name": name, "best_for": "", "fit": None, "pros": [], "cons": [], "profile": {},
                                                                  "diagram": {"nodes": [], "edges": []}}],
            "profile": {}, "diagram": {"nodes": [], "edges": []},
            "stack": {"language": language, "framework": (detected.framework or "") if detected else "",
                      "build_command": (detected.build_command or "") if detected else "", "test_command": (detected.test_command or "") if detected else ""},
            "platforms": [], "layers": [], "structure": [f"{k}/" for k in list(ev["folders"])[:8] if k != "."],
            "packages": [{"name": p.split(": ", 1)[-1], "ecosystem": p.split(": ", 1)[0], "purpose": ""} for p in ev["packages"][:8]],
            "decisions": [], "prd_links": [], "prd_gaps": [], "risks": [], "open_questions": [], "revisit_when": [], "notes": "", "sources": [],
            "fingerprint": _code_fingerprint(ev)}


def _code_fingerprint(ev: dict[str, Any]) -> str:
    """What an inferred assumption was read from, so a later look can tell the code has moved on since."""
    return hashlib.sha256(json.dumps([ev.get("stack"), ev.get("languages"), sorted(ev.get("manifests") or [])], sort_keys=True).encode()).hexdigest()[:12]


def skip(root: Path, runtime: Path, *, prd_text: str = "") -> dict[str, Any]:
    """Decide later, without losing track. A waiting proposal's recommended option becomes the working assumption; with no
    proposal, one is inferred from the repository (or refreshed, when the last one was inferred). Never replaces an approved
    decision: skipping a change to one just discards the proposal."""
    current = load_decision(root)
    if current and current.get("status") == "approved":
        clear_proposal(runtime)
        return current
    proposal = load_proposal(runtime)
    base = {**proposal, "origin": "described" if proposal.get("mode") == "describe" else "proposed"} if proposal and proposal.get("stack") \
        else {**infer_record(root), "origin": "inferred"}
    try:
        major, minor = (int(x) for x in str((current or {}).get("version") or "0.0").split(".", 1))
    except ValueError:
        major, minor = 0, 0
    version = f"0.{minor + 1}" if major == 0 else "0.1"
    history = list((current or {}).get("history") or [])
    history.append({"version": version, "kind": "working assumption", "at": _now(), "summary": _text(base.get("summary"), 300),
                    "note": "Recommended option kept without approval" if base["origin"] != "inferred" else "Inferred from the repository",
                    "changes": what_changed(current, base)[:10]})
    record = {**_strip_meta(base), "origin": base["origin"], "status": "provisional", "recorded_at": _now(), "version": version,
              "prd_hash": prd_fingerprint(prd_text) if prd_text else "", "history": history[-MAX_HISTORY:]}
    _save_record(root, record)
    clear_proposal(runtime)
    return record


def inferred_is_stale(root: Path, record: dict[str, Any] | None) -> bool:
    """An inferred working assumption whose code has changed since (a project scaffolded after skipping, a new language)."""
    if not record or record.get("origin") != "inferred" or record.get("status") != "provisional":
        return False
    return record.get("fingerprint") != _code_fingerprint(evidence(root))


# --------------------------------------------------------------------------- reading it back


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items if i)


def render_markdown(record: dict[str, Any], name: str = "") -> str:
    stack = record.get("stack") or {}
    out = [f"# {name + ': ' if name else ''}Architecture", "",
           "_The approved technical foundation. Every AI that plans, builds or reviews work here reads this first. "
           "Change it from the Architecture page, not by hand, so the record and this page stay in step._", ""]
    if record.get("status") == "provisional":
        how = "inferred from the repository" if record.get("origin") == "inferred" else "the recommended option, kept without approval"
        out += [f"**Working assumption, not approved** ({how}), version {record.get('version', '0.1')}, {str(record.get('recorded_at', ''))[:10]}. "
                "Jobs follow it unless a request needs otherwise. Approve it, or choose another, on the Product page.", ""]
    elif record.get("approved_at"):
        how = "Described from the existing code" if record.get("origin") == "described" else "Approved"
        out += [f"{how}, version {record.get('version', '1.0')}, {record['approved_at'][:10]}.", ""]
    if record.get("summary"):
        out += ["## Summary", "", record["summary"], ""]
    out += ["## Stack", "", f"- **Chosen:** {record.get('chosen', '')}", f"- **Primary language:** {stack.get('language', '')}"]
    if stack.get("framework"):
        out.append(f"- **Framework:** {stack['framework']}")
    if stack.get("build_command"):
        out.append(f"- **Build:** `{stack['build_command']}`")
    if stack.get("test_command"):
        out.append(f"- **Test:** `{stack['test_command']}`")
    out.append("")
    if (record.get("diagram") or {}).get("nodes"):
        out += ["## Diagram", "", mermaid(record["diagram"]), ""]
    profile = record.get("profile") or {}
    if profile:
        out += ["## Style profile", "", *[f"- **{meta['title']}:** {profile_line(profile, cat)}" for cat, meta in CATEGORIES.items() if profile.get(cat)], ""]
    if record.get("prd_links") or record.get("prd_gaps"):
        out += ["## How it serves the product requirements", ""]
        out += [f"- **{l['requirement']}:** {l['how']}" for l in record.get("prd_links") or []]
        out += [f"- **Not covered yet:** {g}" for g in record.get("prd_gaps") or []]
        out.append("")
    if record.get("platforms"):
        out += ["## Platforms", "", *[f"- **{p['platform']}:** {' / '.join(x for x in (p.get('language'), p.get('framework')) if x)}"
                                      + (f". {p['notes']}" if p.get("notes") else "") for p in record["platforms"]], ""]
    if record.get("layers"):
        out += ["## Layers", "", *[f"{n}. **{l['name']}:** {l['responsibility']}" for n, l in enumerate(record["layers"], 1)], ""]
    if record.get("structure"):
        out += ["## Folder structure", "", _bullets(record["structure"]), ""]
    if record.get("packages"):
        out += ["## Packages", "", *[f"- `{p['name']}` ({p.get('ecosystem') or 'package'}): {p.get('purpose', '')}" for p in record["packages"]], ""]
    if record.get("decisions"):
        out += ["## Decisions", ""]
        for d in record["decisions"]:
            out += [f"### {d['topic']}", "", f"**Choice:** {d['choice']}", ""]
            if d.get("alternatives"):
                out += [f"**Considered:** {', '.join(d['alternatives'])}", ""]
            if d.get("reason"):
                out += [f"**Why:** {d['reason']}", ""]
            if d.get("serves"):
                out += [f"**Serves:** {'; '.join(d['serves'])}", ""]
    if record.get("candidates"):
        out += ["## Candidates considered", ""]
        for c in record["candidates"]:
            mark = " (chosen)" if c["name"] == record.get("chosen") else ""
            fit = f" Fit {c['fit']}/100." if c.get("fit") is not None else ""
            out += [f"- **{c['name']}{mark}:** {c.get('best_for', '')}{fit}"]
            if c.get("pros"):
                out.append(f"  - Good: {'; '.join(c['pros'])}")
            if c.get("cons"):
                out.append(f"  - Watch out: {'; '.join(c['cons'])}")
        out.append("")
    for key, title in (("risks", "Risks"), ("open_questions", "Open questions"), ("revisit_when", "Revisit this when")):
        if record.get(key):
            out += [f"## {title}", "", _bullets(record[key]), ""]
    if record.get("history"):
        out += ["## History", ""]
        for h in reversed(record["history"]):
            out.append(f"- **{h.get('version')}** ({h.get('kind')}, {str(h.get('at', ''))[:10]}): {h.get('note') or h.get('summary') or ''}".rstrip(": "))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


RULES = {
    "planner": ("Plan within these decisions: use this language, framework, structure and these packages. Small additions that fit "
                "(a package, a module inside an existing layer) are fine; say so in `assumptions`. If the request needs a decision to change, "
                "say so in `risks`, name the decision, and don't change it silently."),
    "verifier": ("Treat these decisions as constraints. Raise a concern when the plan quietly adds a framework, swaps a decision, or "
                 "ignores the agreed structure, unless the plan says why the decision must change."),
    "builder": ("Build with this language, framework and structure. Don't add a framework or a package that duplicates one listed here; "
                "if a decision is in your way, stop and say so instead of working around it."),
}

PROVISIONAL_RULES = {
    "planner": ("This is a working assumption, not an approved decision. Follow it by default so jobs stay consistent. If the request "
                "fits clearly better with a different choice, plan the better choice and say so in `assumptions` and `risks`."),
    "verifier": ("This is a working assumption, not an approved decision. Raise a concern only when the plan departs from it without saying why."),
    "builder": ("This is a working assumption. Follow it unless the approved plan says otherwise; don't add a second framework for the same job."),
}

UNDECIDED = ("\n\n## Architecture (not decided yet)\nThis is a new project with no code and no approved architecture. "
             "Don't pick a stack silently. Plan only what the request needs, state every language, framework and package you assume "
             "in `assumptions`, and say in `risks` that the architecture should be decided on the Architecture page first.\n")


def context_block(root: Path, audience: str = "planner") -> str:
    """The approved decision as prompt context with the rule for who reads it; a short warning for an undecided new project."""
    record = load_decision(root)
    if record is None:
        return UNDECIDED if audience == "planner" and is_codeless(root) else ""
    stack = record.get("stack") or {}
    lines = [f"- Chosen: {record.get('chosen', '')} (version {record.get('version', '1.0')})",
             f"- Primary language: {stack.get('language', '')}" + (f", framework: {stack['framework']}" if stack.get("framework") else "")]
    for cat, meta in CATEGORIES.items():
        if (record.get("profile") or {}).get(cat):
            lines.append(f"- {meta['title']}: {profile_line(record['profile'], cat)}")
    for p in record.get("platforms", []):
        lines.append(f"- {p['platform']}: {' / '.join(x for x in (p.get('language'), p.get('framework')) if x)}")
    for l in record.get("layers", []):
        lines.append(f"- Layer {l['name']}: {l['responsibility']}")
    if record.get("structure"):
        lines.append("- Folders: " + "; ".join(record["structure"]))
    if record.get("packages"):
        lines.append("- Packages: " + ", ".join(p["name"] for p in record["packages"]))
    for d in record.get("decisions", []):
        lines.append(f"- {d['topic']}: {d['choice']}" + (f" (not {', '.join(d['alternatives'])})" if d.get("alternatives") else ""))
    if record.get("prd_gaps") and audience == "planner":
        lines.append("- Product needs the architecture doesn't cover yet: " + "; ".join(record["prd_gaps"]))
    if is_approved(record):
        return f"\n\n## Architecture (approved, `{DOC_PATH}`)\n{RULES.get(audience, RULES['planner'])}\n\n" + "\n".join(lines) + "\n"
    if record.get("origin") == "inferred" and stack.get("language") == "generic":
        return UNDECIDED if audience == "planner" and is_codeless(root) else ""  # a placeholder says nothing worth following
    return (f"\n\n## Architecture (working assumption, `{DOC_PATH}`)\n{PROVISIONAL_RULES.get(audience, PROVISIONAL_RULES['planner'])}\n\n"
            + "\n".join(lines) + "\n")


def detected_stack_fields(root: Path) -> dict[str, Any] | None:
    """What stack detection needs from the decision, for a repository that has no manifests yet."""
    record = load_decision(root)
    if record is None or record.get("origin") == "inferred" or record["stack"].get("language") == "generic":
        return None
    stack = record["stack"]
    framework = stack.get("framework") or None
    label = {"typescript": "TypeScript", "javascript": "JavaScript", "csharp": "C#"}.get(stack["language"], stack["language"].capitalize())
    return {"language": stack["language"], "framework": framework,
            "display_name": f"{label}{f' ({framework})' if framework else ''} (from architecture)",
            "build_command": stack.get("build_command") or None, "test_command": stack.get("test_command") or None,
            "manifest_file": RECORD_PATH}


def revisit_reasons(record: dict[str, Any] | None, change_text: str) -> list[str]:
    """Which of the decision's own `revisit_when` triggers a change (new requirements, a job's request) seems to touch.
    A cheap word-overlap check meant to prompt a person, never to change anything."""
    if not record or not change_text.strip():
        return []
    words = set(re.findall(r"[a-z]{4,}", change_text.lower()))
    stop = {"when", "becomes", "required", "needed", "need", "with", "that", "this", "than", "more", "from", "into", "have", "users"}
    hits = []
    for trigger in record.get("revisit_when", []):
        key = {w for w in re.findall(r"[a-z]{4,}", trigger.lower()) if w not in stop}
        if key and len(key & words) >= max(1, min(2, len(key))):
            hits.append(trigger)
    return hits


def prd_changed(record: dict[str, Any] | None, prd_text: str) -> bool:
    """The product requirements have changed since this decision was approved (only for decisions that recorded them)."""
    return bool(record and record.get("prd_hash") and prd_text.strip() and record["prd_hash"] != prd_fingerprint(prd_text))


def summary(root: Path, prd_text: str = "") -> dict[str, Any]:
    """The short version for Home: what it is, its style mix, and whether anything wants attention."""
    record = load_decision(root)
    if record is None:
        return {"state": "undecided" if is_codeless(root) else "undescribed"}
    stack = record.get("stack") or {}
    return {"state": "approved" if is_approved(record) else "provisional", "chosen": record.get("chosen", ""),
            "version": record.get("version", "1.0"), "origin": record.get("origin", "proposed"), "stale": inferred_is_stale(root, record),
            "language": stack.get("language", ""), "framework": stack.get("framework", ""),
            "profile": {cat: [{"style": s, "name": STYLES[s]["name"], "weight": w} for s, w in (record.get("profile") or {}).get(cat, {}).items() if s in STYLES]
                        for cat in CATEGORIES},
            "prd_gaps": len(record.get("prd_gaps") or []), "prd_changed": prd_changed(record, prd_text),
            "revisit": revisit_reasons(record, prd_text)}
