"""Work out how a project sends builds to testers from what it already has, so setup is a confirmation, not a form.

A project that has shipped before leaves its answers around: a GoogleService-Info.plist or google-services.json, a
.firebaserc, distribution receipts and logs, an AGENTS.md or README that names the Firebase project, app and tester
group, an .ipa or .apk from an earlier build, and a script that makes them. `propose` reads those (it runs nothing) and
returns the settings to use, each value with where it was found, and what is still missing for the person to fill in.

`state` says where the project stands:
  "configured"  delivery is already set up (nothing to propose)
  "xcode"       an Xcode project: the wizard's signing setup is the way, under Advanced
  "proposal"    settings found or guessed for Android, a Unity/own-script build, or a web preview
  "none"        nothing recognisable

Automatic delivery is never proposed on: sending a build is an explicit act.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
from pathlib import Path
from typing import Any

from orchestrator import platforms as platform_view

APP_ID = re.compile(r"\b1:\d{8,}:(ios|android|web):[0-9a-f]{12,}\b")
DOC_PROJECT = [re.compile(r"(?i)firebase\s+project[:\s]+[`'\"]?([a-z][a-z0-9-]{4,29})\b"), re.compile(r"\bproject\s+[`'\"]([a-z][a-z0-9-]{4,29})[`'\"]")]
DOC_GROUP = [re.compile(r"(?i)tester\s+group[:\s]+[`'\"]?([a-z0-9][a-z0-9_-]{2,40})\b"), re.compile(r"\bgroups?\s+[`'\"]([a-z0-9][a-z0-9_-]{2,40})[`'\"]")]
SKIP_DIRS = {".git", "node_modules", "bin", "obj", "Library", "Temp", "Logs", "PackageCache", ".venv", "venv", "build", "dist", ".orchestrator",
             "DerivedData", "Pods", "__pycache__", "TestResults", ".build", ".gradle", "target", "Carthage", ".worktrees", ".claude"}
ARTIFACT_SUFFIXES = (".ipa", ".apk", ".aab")
SCRIPT_NAME = re.compile(r"(?i)^(package|build|release|distribute|ship|make)[-_]?(ios|android|mobile|apk|ipa)|^(ios|android)[-_]?(build|package|release)")
DOC_FILES = ("AGENTS.md", "CLAUDE.md", "README.md")
MAX_DOCS = 40
MAX_BYTES = 150_000


def _read(path: Path, limit: int = MAX_BYTES) -> str:
    try:
        with path.open(encoding="utf-8", errors="ignore") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _walk(root: Path, max_depth: int = 5):
    base = len(root.parts)
    for folder, dirs, files in os.walk(root):
        depth = len(Path(folder).parts) - base
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and (depth < max_depth) and not (Path(folder) / d / ".git").exists())
        for name in sorted(files):
            yield Path(folder) / name


def firebase_facts(root: Path) -> dict[str, list[tuple[str, str]]]:
    """Firebase project ids, app ids and tester groups found in the project, each as (value, where it was found), best first:
    receipts and logs of real uploads, then the app's own Firebase files, then what the docs say."""
    found: dict[str, list[tuple[str, str]]] = {"project": [], "app": [], "group": []}

    def add(kind: str, value: str | None, source: str) -> None:
        if value and all(v != value for v, _ in found[kind]):
            found[kind].append((value, source))

    receipts = sorted((root / ".local" / "distribution-receipts").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:5] \
        if (root / ".local" / "distribution-receipts").is_dir() else []
    for path in receipts:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            source = f"a past upload receipt ({path.name})"
            add("project", data.get("firebase_project"), source)
            add("app", data.get("firebase_app_id"), source)
            add("group", data.get("tester_group"), source)
    local = root / ".local"
    for path in sorted(local.glob("*firebase*.log"))[:3] if local.is_dir() else []:
        text = _read(path, 20_000)
        for label, kind in (("Firebase project", "project"), ("Firebase App ID", "app"), ("Tester group", "group")):
            match = re.search(rf"{label}:\s*(\S+)", text)
            if match:
                add(kind, match.group(1), f"a past build log ({path.name})")
    order = {"GoogleService-Info.plist": 0, "google-services.json": 1, ".firebaserc": 2}
    for path in sorted((p for p in _walk(root, 4) if p.name in order), key=lambda p: (order[p.name], p.as_posix())):
        if path.name == "GoogleService-Info.plist":
            try:
                data = plistlib.loads(path.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                continue
            rel = path.relative_to(root).as_posix()
            add("project", data.get("PROJECT_ID"), rel)
            add("app", data.get("GOOGLE_APP_ID"), rel)
        elif path.name == "google-services.json":
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rel = path.relative_to(root).as_posix()
                add("project", (data.get("project_info") or {}).get("project_id"), rel)
                add("app", ((data.get("client") or [{}])[0].get("client_info") or {}).get("mobilesdk_app_id"), rel)
            except (OSError, ValueError, IndexError, AttributeError):
                continue
        elif path.name == ".firebaserc":
            try:
                add("project", ((json.loads(path.read_text(encoding="utf-8")).get("projects") or {}).get("default")), ".firebaserc")
            except (OSError, ValueError, AttributeError):
                continue
    docs = [root / name for name in DOC_FILES if (root / name).is_file()]
    docs += sorted((root / "docs").rglob("*.md"))[:MAX_DOCS] if (root / "docs").is_dir() else []
    for path in docs:
        text = _read(path)
        rel = path.relative_to(root).as_posix()
        for match in APP_ID.finditer(text):
            add("app", match.group(0), rel)
        for pattern in DOC_PROJECT:
            for match in pattern.finditer(text):
                add("project", match.group(1), rel)
        for pattern in DOC_GROUP:
            for match in pattern.finditer(text):
                add("group", match.group(1), rel)
    return found


def existing_artifact(root: Path) -> Path | None:
    """The newest .ipa, .apk or .aab already in the project (not in dependency folders)."""
    best: tuple[float, Path] | None = None
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS - {".local"} and d != "node_modules" and not (Path(folder) / d / ".git").exists()]
        for name in files:
            if name.lower().endswith(ARTIFACT_SUFFIXES):
                path = Path(folder) / name
                try:
                    stamp = path.stat().st_mtime
                except OSError:
                    continue
                if best is None or stamp > best[0]:
                    best = (stamp, path)
    return best[1] if best else None


def artifact_pattern(root: Path, artifact: Path) -> str:
    """A glob that matches this artifact and the ones later builds will write: version and build numbers become `*`."""
    parts = list(artifact.relative_to(root).parts)
    out = [re.sub(r"\d+(?:[.\d]*\d)?", "*", part) for part in parts[:-1]] + [f"*{artifact.suffix}"]
    return "/".join(re.sub(r"\*+", "*", part) for part in out)


def build_script(root: Path, hint: str | None) -> str | None:
    """The project's own script that builds the app (`tools/package_ios.sh`): named like a build or package step for a phone."""
    candidates = []
    for folder in ("tools", "scripts", "script", "ci", "bin", "."):
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if path.is_file() and path.suffix in (".sh", ".py") and SCRIPT_NAME.search(path.stem):
                rank = 0 if hint and hint in path.stem.lower() else 1
                candidates.append((rank, path))
    if not candidates:
        return None
    path = sorted(candidates, key=lambda c: c[0])[0][1]
    rel = path.relative_to(root).as_posix()
    return f"python3 {rel}" if path.suffix == ".py" else f"bash {rel}"


OUT_DIR = re.compile(r"(\.?[\w./-]*release[\w./-]*?(?:<[\w-]+>|\$\{?\w+\}?)[\w./-]*)")


def script_output_pattern(root: Path, command: str, suffix: str) -> str | None:
    """Where a build script says its output goes (`.local/release-<version>/ios/`), as a glob for the file it makes.
    The version in the path becomes `*`. None when the script doesn't say."""
    parts = command.split()
    path = root / parts[-1] if parts else None
    if not path or not path.is_file():
        return None
    text = _read(path, 30_000)
    for match in OUT_DIR.finditer(text):
        folder = re.sub(r"<[\w-]+>|\$\{?\w+\}?", "*", match.group(1)).strip("/")
        folder = re.sub(r"\*+", "*", folder)
        if folder and not folder.startswith(("/", "~")) and ".." not in folder.split("/"):
            return f"{folder.rstrip('/')}/*{suffix}"
    return None


def _policy_note(root: Path) -> str | None:
    for name in ("AGENTS.md", "CLAUDE.md"):
        text = _read(root / name)
        if re.search(r"(?is)firebase.{0,400}explicit|explicit.{0,400}firebase", text):
            return f"{name} says Firebase uploads must be explicitly requested, so automatic delivery is left off. Each time you press Send build is one request."
    return None


def propose(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """The delivery setup for this project, from what it already contains. See the module doc for `state`."""
    platform = str(config.get("delivery_platform") or "ios").lower()
    if platform in ("android", "web", "file") and config.get("delivery_platform"):
        return {"state": "configured", "platform": platform, "fields": {}, "found": [], "missing": [], "notes": []}
    if config.get("xcode_project") or config.get("xcode_workspace") or any(root.glob("*.xcodeproj")):
        if config.get("distribution_script_path"):
            return {"state": "configured", "platform": "ios", "fields": {}, "found": [], "missing": [], "notes": []}
        return {"state": "xcode", "platform": "ios", "fields": {}, "found": [], "missing": [], "notes": []}
    facts = firebase_facts(root)
    found: list[dict[str, str]] = []
    notes: list[str] = []
    unity = any(platform_view._is_unity(p) for p in [root] + [c for c in root.iterdir() if c.is_dir() and c.name not in SKIP_DIRS]) if root.is_dir() else False
    gradle = any((root / n).exists() for n in ("gradlew", "build.gradle", "build.gradle.kts"))
    hosting = False
    try:
        hosting = bool(json.loads((root / "firebase.json").read_text(encoding="utf-8")).get("hosting"))
    except (OSError, ValueError, AttributeError):
        pass
    artifact = existing_artifact(root)
    fields: dict[str, Any] = {}
    if unity or (artifact and not gradle):
        fields["delivery_platform"] = "file"
        hint = "ios" if (artifact and artifact.suffix == ".ipa") or any(":ios:" in v for v, _ in facts["app"]) else "android" if gradle else None
        script = build_script(root, hint)
        if hint is None and script:  # the script's own name says which phone it builds for
            stem = Path(script.split()[-1]).stem.lower()
            hint = "ios" if "ios" in stem or "ipa" in stem else "android" if "android" in stem or "apk" in stem else None
        if script:
            fields["delivery_build_command"] = script
            found.append({"label": "Build script", "value": script, "source": "named like a build step"})
        suffix = artifact.suffix if artifact else (".ipa" if hint == "ios" else ".apk" if hint == "android" else "")
        from_script = script_output_pattern(root, script, suffix) if script and suffix else None
        if from_script:
            fields["delivery_artifact"] = from_script
            found.append({"label": "File it makes", "value": from_script, "source": "where the build script says it writes"})
        elif artifact:
            pattern = artifact_pattern(root, artifact)
            fields["delivery_artifact"] = pattern
            found.append({"label": "File it makes", "value": pattern, "source": f"an earlier build ({artifact.relative_to(root).as_posix()})"})
    elif gradle:
        fields.update({"delivery_platform": "android", "delivery_build_command": "./gradlew assembleDebug" if (root / "gradlew").exists() else "gradle assembleDebug",
                       "delivery_artifact": "**/build/outputs/apk/debug/*.apk"})
        found.append({"label": "Build", "value": fields["delivery_build_command"], "source": "a Gradle project"})
    elif hosting:
        fields["delivery_platform"] = "web"
        found.append({"label": "Web preview", "value": "Firebase Hosting preview link", "source": "firebase.json"})
    else:
        return {"state": "none", "platform": None, "fields": {}, "found": [], "missing": [], "notes": []}
    wanted = ":ios:" if fields.get("delivery_artifact", "").endswith(".ipa") else ":android:" if fields["delivery_platform"] in ("android",) or fields.get("delivery_artifact", "").endswith((".apk", ".aab")) else ""
    if fields["delivery_platform"] != "web":
        app = next(((v, s) for v, s in facts["app"] if not wanted or wanted in v), None)
        if app:
            fields["firebase_app_id"] = app[0]
            found.append({"label": "Firebase app ID", "value": app[0], "source": app[1]})
    if facts["project"]:
        fields["firebase_project_id"] = facts["project"][0][0]
        found.append({"label": "Firebase project", "value": facts["project"][0][0], "source": facts["project"][0][1]})
    if facts["group"] and fields["delivery_platform"] != "web":
        fields["firebase_groups"] = facts["group"][0][0]
        found.append({"label": "Tester group", "value": facts["group"][0][0], "source": facts["group"][0][1]})
    missing = []
    if fields["delivery_platform"] != "web" and not fields.get("firebase_app_id"):
        missing.append("Firebase app ID")
    if fields["delivery_platform"] in ("file", "android") and not fields.get("delivery_artifact"):
        missing.append("the file your build makes")
    policy = _policy_note(root)
    if policy:
        notes.append(policy)
    return {"state": "proposal", "platform": fields["delivery_platform"], "fields": fields, "found": found, "missing": missing, "notes": notes}
