"""Keeping Orchestrator itself up to date on this computer.

Each computer checks the branch it was installed from (CHECK_SECONDS) and sorts what's new into one of two kinds:

- **Minor**: fixes and improvements that need nothing from you. Installed by itself once no run is going (auto_minor, on
  by default), then the UI restarts. Jobs carry on where they were.
- **Breaking**: a change other computers or saved data depend on (a new sync format, a migration), named in
  orchestrator/release.json by raising `breaking` and saying why in `breaking_changes`. Waits for you to choose Update
  (auto_breaking, off by default), because it's worth knowing when it happens, and your other computers want it too.

Settings and the last check live in ~/.orchestrator/updates.json, per computer.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from orchestrator.project_config import PACKAGE_ROOT, user_state_dir

CHECK_SECONDS = 1800
RELEASE_FILE = "orchestrator/release.json"
DEFAULTS = {"auto_minor": True, "auto_breaking": False}
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


def _file() -> Path:
    return user_state_dir() / "updates.json"


def load() -> dict[str, Any]:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **(data if isinstance(data, dict) else {})}


def save(data: dict[str, Any]) -> None:
    try:
        _file().parent.mkdir(parents=True, exist_ok=True)
        _file().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def set_options(auto_minor: bool | None = None, auto_breaking: bool | None = None) -> dict[str, Any]:
    data = load()
    if auto_minor is not None:
        data["auto_minor"] = bool(auto_minor)
    if auto_breaking is not None:
        data["auto_breaking"] = bool(auto_breaking)
    save(data)
    return data


def _git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **_ENV})


def _release(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def install_root() -> Path:
    return PACKAGE_ROOT.parent


def install_kind(root: Path | None = None) -> str:
    """git (a checkout, edited in place), packaged (the Mac app, which updates itself), or pip (pipx or pip)."""
    try:
        from orchestrator.desktop_runtime import is_packaged_install
        if is_packaged_install():
            return "packaged"
    except Exception:
        pass
    root = root or install_root()
    return "git" if (root / ".git").exists() else "pip"


def check(root: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """What's installed, what's newest on the branch it came from, and whether the difference is minor or breaking.
    Records the answer (load()["last_check"]) and returns it."""
    now = time.time() if now is None else now
    root = root or install_root()
    kind = install_kind(root)
    result: dict[str, Any] = {"at": now, "install": kind, "update": "none"}
    try:
        if kind == "git":
            result.update(_check_git(root))
        elif kind == "packaged":
            result["note"] = "The Orchestrator app updates itself: use Check for Updates in its menu."
        else:
            result.update(_check_pip())
    except (OSError, subprocess.SubprocessError) as exc:
        result.update(update="unknown", error=str(exc)[:300])
    data = load()
    data["last_check"] = result
    save(data)
    return result


def _check_git(root: Path) -> dict[str, Any]:
    branch = _git(root, "branch", "--show-current").stdout.strip()
    if not branch:
        return {"update": "unknown", "error": "This copy of Orchestrator isn't on a branch, so it can't tell what's newer."}
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    local = _release((root / RELEASE_FILE).read_text(encoding="utf-8")) if (root / RELEASE_FILE).is_file() else {}
    out: dict[str, Any] = {"branch": branch, "installed": head[:12], "installed_breaking": int(local.get("breaking") or 0)}
    fetched = _git(root, "fetch", "-q", "origin", branch, timeout=120)
    if fetched.returncode != 0:
        return {**out, "update": "unknown", "error": (fetched.stderr or fetched.stdout).strip()[-300:] or "Couldn't reach origin."}
    latest = _git(root, "rev-parse", f"origin/{branch}").stdout.strip()
    behind = int(_git(root, "rev-list", "--count", f"HEAD..origin/{branch}").stdout.strip() or 0)
    ahead = int(_git(root, "rev-list", "--count", f"origin/{branch}..HEAD").stdout.strip() or 0)
    out.update(latest=latest[:12], behind=behind, ahead=ahead)
    if not behind:
        return {**out, "update": "none"}
    remote = _release(_git(root, "show", f"origin/{branch}:{RELEASE_FILE}").stdout)
    level = int(remote.get("breaking") or 0)
    out["latest_breaking"] = level
    out["changes"] = [s for s in _git(root, "log", "--format=%s", f"HEAD..origin/{branch}", "-n", "20").stdout.splitlines() if s]
    out["breaking_changes"] = [c.get("summary") for c in remote.get("breaking_changes") or []
                               if isinstance(c, dict) and int(c.get("level") or 0) > out["installed_breaking"] and c.get("summary")]
    out["update"] = "breaking" if level > out["installed_breaking"] else "minor"
    if ahead:
        out["blocked"] = f"This copy has {ahead} commit{'s' if ahead != 1 else ''} of its own that aren't on origin/{branch}; push or drop them first."
    elif _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        out["blocked"] = "This copy of Orchestrator has edits of its own; commit or set them aside first."
    return out


def _check_pip() -> dict[str, Any]:
    from orchestrator import account
    spec = account.INSTALL_SPEC  # git+https://…@branch
    url, _, branch = spec.removeprefix("git+").rpartition("@")
    installed = ""
    try:
        from importlib.metadata import distribution
        direct = json.loads(distribution("orchestrator").read_text("direct_url.json") or "{}")
        installed = str((direct.get("vcs_info") or {}).get("commit_id") or "")
    except Exception:
        pass
    res = subprocess.run(["git", "ls-remote", url, f"refs/heads/{branch}"], capture_output=True, text=True, timeout=60,
                         env={**os.environ, **_ENV})
    latest = res.stdout.split()[0] if res.returncode == 0 and res.stdout.split() else ""
    if not latest:
        return {"branch": branch, "update": "unknown", "error": (res.stderr or "Couldn't reach the repository.").strip()[-300:]}
    out = {"branch": branch, "installed": installed[:12], "latest": latest[:12]}
    if installed and installed == latest:
        return {**out, "update": "none"}
    # Without a checkout, release.json at the newest commit can't be read cheaply: say so, and treat it as minor.
    return {**out, "update": "minor", "note": "Installed with pipx, so whether it's breaking can't be told before installing."}


def apply(root: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Install the newest version on the branch. Returns {ok, message, restart}; the caller restarts the UI when ok."""
    now = time.time() if now is None else now
    root = root or install_root()
    kind = install_kind(root)
    if kind == "packaged":
        return {"ok": False, "message": "The Orchestrator app updates itself: use Check for Updates in its menu."}
    try:
        if kind == "git":
            status = _check_git(root)
            if status.get("update") in ("none",):
                return {"ok": True, "message": "Already up to date.", "restart": False}
            if status.get("blocked"):
                return {"ok": False, "message": status["blocked"]}
            before = _git(root, "rev-parse", "HEAD").stdout.strip()
            merged = _git(root, "merge", "--ff-only", f"origin/{status['branch']}")
            if merged.returncode != 0:
                return {"ok": False, "message": (merged.stderr or merged.stdout).strip()[-300:]}
            changed = _git(root, "diff", "--name-only", before, "HEAD").stdout.split()
            if "pyproject.toml" in changed:  # new or changed dependencies
                pip = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", "."], cwd=str(root),
                                     capture_output=True, text=True, timeout=600)
                if pip.returncode != 0:
                    return {"ok": False, "message": "Updated the code, but installing its dependencies failed: "
                            + (pip.stderr or pip.stdout).strip()[-300:]}
            message = f"Updated to {status.get('latest')} ({status.get('update')})."
        else:
            from orchestrator import account
            pipx = shutil.which("pipx")
            argv = [pipx, "install", "--force", account.INSTALL_SPEC] if pipx else \
                [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", account.INSTALL_SPEC]
            res = subprocess.run(argv, capture_output=True, text=True, timeout=900)
            if res.returncode != 0:
                return {"ok": False, "message": (res.stderr or res.stdout).strip()[-300:]}
            message = "Updated to the newest version."
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "message": str(exc)[:300]}
    data = load()
    history = [h for h in data.get("history") or [] if isinstance(h, dict)][-19:]
    data["history"] = history + [{"at": now, "message": message}]
    data["last_check"] = {**(data.get("last_check") or {}), "update": "none"}
    save(data)
    return {"ok": True, "message": message, "restart": True}


def due(data: dict[str, Any], now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now - float((data.get("last_check") or {}).get("at") or 0) >= CHECK_SECONDS


def should_install(status: dict[str, Any], data: dict[str, Any]) -> bool:
    """Install on its own? Minor ones when auto_minor is on, breaking ones only when auto_breaking is."""
    if status.get("blocked") or status.get("error"):
        return False
    return (status.get("update") == "minor" and data.get("auto_minor", True)) or \
        (status.get("update") == "breaking" and data.get("auto_breaking", False))
