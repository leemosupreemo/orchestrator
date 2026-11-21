"""Build-volume checks and opt-in removal of recognized build caches."""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import chain
from pathlib import Path

GIB = 1024 ** 3
_DISK_ERROR = re.compile(
    r"no space left on device|\benospc\b|\berrno 28\b|not enough (?:free )?(?:disk )?space|"
    r"insufficient (?:free )?disk space|low disk space|\bdisk (?:is )?full\b|"
    r"ran out of (?:free |disk )space|disk space exhausted",
    re.IGNORECASE,
)


def is_disk_error(message: str) -> bool:
    return bool(_DISK_ERROR.search(message))


def status(root: Path, runtime: Path | None = None, *, minimum_gib: int | None = None) -> dict:
    if minimum_gib is None:
        from orchestrator.run_check import MIN_DISK_GB

        minimum_gib = MIN_DISK_GB
    readings = []
    for label, path in (("Project", root), ("Orchestrator data", runtime or root / ".orchestrator"),
                        ("Temporary files", Path(tempfile.gettempdir()))):
        try:
            try:
                free = shutil.disk_usage(path).free
            except FileNotFoundError:
                free = shutil.disk_usage(path.parent).free
        except OSError:
            continue
        readings.append((free, label))
    free, label = min(readings, key=lambda reading: reading[0]) if readings else (None, "")
    required = minimum_gib * GIB
    return {"free_bytes": free, "required_bytes": required, "volume": label,
            "needed_bytes": max(0, required - free) if free is not None else None}


def low_disk_message(root: Path, runtime: Path | None = None) -> str:
    report = status(root, runtime)
    if not report["needed_bytes"]:
        return ""
    return (f"Not enough disk space on this computer. {report['volume']} has "
            f"{report['free_bytes'] / GIB:.1f} GB free. Builds need {report['required_bytes'] / GIB:g} GB. "
            f"Free at least {math.ceil(report['needed_bytes'] / GIB)} GB.")


def derived_data_root() -> Path:
    return Path(os.environ.get("XCODE_DERIVED_DATA_PATH", "~/Library/Developer/Xcode/DerivedData")).expanduser()


@dataclass(frozen=True)
class Cache:
    path: Path
    name: str
    tool: str


class CacheBusyError(RuntimeError):
    """A selected cache may still be in use."""


def _directories(root: Path) -> Iterator[Path]:
    try:
        entries = sorted(root.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return
    yield from (entry for entry in entries if not entry.is_symlink() and entry.is_dir())


def _xcode_caches() -> Iterator[Cache]:
    directory = derived_data_root()
    if directory.is_symlink():
        return
    for entry in _directories(directory):
        if entry.name in {"ModuleCache.noindex", "SDKStatCaches.noindex"} or (entry / "info.plist").is_file():
            yield Cache(entry, entry.name, "Xcode")


def _vscode_caches() -> Iterator[Cache]:
    support = Path.home() / "Library" / "Application Support"
    if support.is_symlink():
        return
    for name in ("Code", "Code - Insiders"):
        profile = support / name
        if profile.is_symlink():
            continue
        # Chromium and compiled-code caches. User profiles, history and
        # unsaved-file backups live beside them and are deliberately separate.
        for directory in ("Cache", "Code Cache", "CachedData", "GPUCache"):
            path = profile / directory
            if not path.is_symlink() and path.is_dir():
                yield Cache(path, f"{name}/{directory}", "VS Code")


def _git_generated(root: Path, path: Path) -> bool:
    """Only offer ignored workspace directories with no tracked paths beneath them."""
    relative = str(path.relative_to(root))
    options = {"cwd": root, "capture_output": True, "timeout": 3, "check": False}
    try:
        tracked = subprocess.run(["git", "ls-files", "--", f":(literal){relative}"], **options)
        if tracked.returncode or tracked.stdout:
            return False
        return subprocess.run(["git", "check-ignore", "-q", "--", relative], **options).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _workspace_caches(root: Path) -> Iterator[Cache]:
    children = (path for path in _directories(root)
                if not path.name.startswith(".") and not (path / ".git").exists())
    for project in (root, *children):
        candidates = []
        if (project / "ProjectSettings" / "ProjectVersion.txt").is_file() and (project / "Assets").is_dir():
            candidates.append((project / "Library", "Unity"))
        if any((project / name).is_file() for name in ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts")):
            candidates.append((project / ".gradle", "Gradle"))
        build = project / ".build"
        if (project / "Package.swift").is_file() and not build.is_symlink():
            # SwiftPM can keep dependency checkouts here. Only offer compiled configurations.
            targets = (path for path in _directories(build) if re.fullmatch(r"[\w.]+(?:-[\w.]+){2,}", path.name))
            candidates.extend((path / configuration, "SwiftPM")
                              for path in (build, *targets) for configuration in ("debug", "release"))
        for path, tool in candidates:
            if not path.is_symlink() and path.is_dir() and _git_generated(root, path):
                yield Cache(path, str(path.relative_to(root)), tool)


def _caches(root: Path) -> dict[str, Cache]:
    try:
        device = root.stat().st_dev
    except OSError:
        return {}
    candidates = {}
    for cache in chain(_xcode_caches(), _vscode_caches(), _workspace_caches(root)):
        path = cache.path
        try:
            meta = path.stat()
            if path.is_symlink() or meta.st_dev != device or path.is_mount():
                continue
            key = hashlib.sha256(f"{path}:{meta.st_dev}:{meta.st_ino}".encode()).hexdigest()[:24]
            candidates[key] = cache
        except OSError:
            continue
    return candidates


def cache_candidates(root: Path) -> list[dict]:
    candidates = _caches(root)
    if not candidates:
        return []
    sizes = {}
    try:
        measured = subprocess.run(["du", "-sk", *(str(cache.path) for cache in candidates.values())],
                                  capture_output=True, text=True, timeout=8, check=False)
        for row in measured.stdout.splitlines():
            size, _, path = row.partition("\t")
            if size.isdecimal():
                sizes[path] = int(size) * 1024
    except (OSError, subprocess.SubprocessError):
        pass
    return sorted(({"id": key, "name": cache.name, "tool": cache.tool, "bytes": sizes.get(str(cache.path))}
                   for key, cache in candidates.items()), key=lambda item: -(item["bytes"] or 0))


def clear_caches(root: Path, ids: list[str]) -> tuple[list[str], list[str]]:
    candidates = _caches(root)
    if not ids or any(key not in candidates for key in ids):
        raise ValueError("Select available build caches. Recheck storage if the list has changed.")
    selected = [candidates[key] for key in dict.fromkeys(ids)]
    busy = [tool for tool in sorted({cache.tool for cache in selected}) if tool_is_active(tool)]
    if busy:
        detail = " Close Android Studio or IntelliJ and stop Gradle with ./gradlew --stop." if "Gradle" in busy else ""
        raise CacheBusyError(f"Finish running {', '.join(busy)} before clearing these caches.{detail}")
    removed, errors = [], []
    for cache in selected:
        try:
            shutil.rmtree(cache.path)
            removed.append(cache.name)
        except OSError:
            errors.append(f"Couldn't remove {cache.name}.")
    return removed, errors


def tool_is_active(tool: str) -> bool:
    patterns = {
        "Xcode": ("-x", "Xcode|xcodebuild|XCBBuildService|swift-build|swift-frontend"),
        "SwiftPM": ("-x", "Xcode|xcodebuild|XCBBuildService|swift|swift-build|swift-test|swiftc|swift-frontend"),
        "Unity": ("-x", "Unity|UnityShaderCompiler|UnityPackageManager|Unity[.]Licensing[.]Client"),
        "Gradle": ("-f", r"org[.]gradle[.]launcher[.]|org[.]gradle[.]wrapper[.]GradleWrapperMain|/Android Studio[^/]*/Contents/MacOS/|/IntelliJ IDEA[^/]*/Contents/MacOS/"),
        "VS Code": ("-f", r"/Visual Studio Code( - Insiders)?[.]app/Contents/|/Code( - Insiders)?[.]app/Contents/"),
    }
    try:
        result = subprocess.run(["pgrep", *patterns[tool]], capture_output=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode != 1
