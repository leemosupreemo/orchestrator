"""Keep the bundled web UI in step with its source.

Uses only the standard library because setup.py also loads this file by path
during package builds, before the orchestrator package is importable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

DIST_DIR = Path(__file__).resolve().parent / "dist"
WEB_SOURCE_DIR = Path(__file__).resolve().parents[2] / "web"
_SOURCE_FILES = ("index.html", "package.json", "package-lock.json", "tsconfig.json", ".env.local")
_INSTALL_COMMAND = 'pipx install --force "git+https://github.com/leemosupreemo/orchestrator.git"'


class UIUnavailable(RuntimeError):
    """The web UI has no usable build and cannot be built here."""


def _has_source(web_dir: Path) -> bool:
    return (web_dir / "package.json").is_file() and (web_dir / "src" / "main.tsx").is_file()


def _newest_source_mtime(web_dir: Path) -> float:
    paths = [web_dir / name for name in _SOURCE_FILES]
    paths.extend(path for path in (web_dir / "src").rglob("*") if path.is_file())
    return max((path.stat().st_mtime for path in paths if path.is_file()), default=0.0)


def needs_build(web_dir: Path = WEB_SOURCE_DIR, dist_dir: Path = DIST_DIR) -> bool:
    if not _has_source(web_dir):
        return False
    index = dist_dir / "index.html"
    return not index.is_file() or _newest_source_mtime(web_dir) > index.stat().st_mtime


def _dependencies_current(web_dir: Path) -> bool:
    installed = web_dir / "node_modules" / ".package-lock.json"
    lock = web_dir / "package-lock.json"
    return installed.is_file() and (not lock.is_file() or installed.stat().st_mtime >= lock.stat().st_mtime)


def build_ui(web_dir: Path = WEB_SOURCE_DIR) -> bool:
    npm = shutil.which("npm")
    if npm is None:
        return False
    commands = [[npm, "run", "build"]]
    if not _dependencies_current(web_dir):
        commands.insert(0, [npm, "ci"])
    try:
        for command in commands:
            subprocess.run(command, cwd=web_dir, check=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def ensure_ui_built(web_dir: Path = WEB_SOURCE_DIR, dist_dir: Path = DIST_DIR) -> None:
    """Rebuild a missing or stale UI, keeping any older build if that fails."""
    has_build = (dist_dir / "index.html").is_file()
    if not _has_source(web_dir):
        if not has_build:
            raise UIUnavailable(
                "This install doesn't include the web UI. Install Node.js, then reinstall:\n"
                f"  {_INSTALL_COMMAND}"
            )
        return
    if not needs_build(web_dir, dist_dir):
        return
    print("Building the web UI...", flush=True)
    if build_ui(web_dir) and (dist_dir / "index.html").is_file():
        return
    has_npm = shutil.which("npm") is not None
    reason = "The web UI build failed" if has_npm else "Node.js is needed to build the web UI"
    if not has_build:
        fix = f"Check the output above, or run: npm --prefix {web_dir} ci && npm --prefix {web_dir} run build" if has_npm else "Install Node.js, then try again."
        raise UIUnavailable(f"{reason}. {fix}")
    print(f"Warning: {reason}, so this is showing an older version of the UI.", flush=True)
