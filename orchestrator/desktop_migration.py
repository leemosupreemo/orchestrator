"""Find an Orchestrator that `orchestrator service install` set up, and hand its job to the desktop app.

Only that exact service (com.orchestrator.ui in ~/Library/LaunchAgents) is ever touched, and only while it isn't
running: older versions can't say whether work is in progress, so a running one is left for the person to stop when
its work finishes. Pairing, credentials and projects stay where they are, because the app uses the same state.
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import stat
import subprocess
import time
from pathlib import Path
from typing import Callable

from orchestrator.service import LABEL

Runner = Callable[..., subprocess.CompletedProcess]


def _definition(launch_agents: Path | None) -> Path:
    return (launch_agents or Path.home() / "Library" / "LaunchAgents") / f"{LABEL}.plist"


def _read_ours(path: Path) -> bytes | None:
    """The definition's bytes if it is a plain file of ours that runs `orchestrator ui`; otherwise None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > 65536:
            return None
        with os.fdopen(os.dup(fd), "rb") as stream:
            content = stream.read()
    finally:
        os.close(fd)
    try:
        data = plistlib.loads(content)
    except Exception:
        return None
    argv = data.get("ProgramArguments") if isinstance(data, dict) else None
    if data.get("Label") != LABEL or not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        return None
    if not any(argv[i:i + 3] == ["-m", "orchestrator", "ui"] for i in range(len(argv))):
        return None
    return content


def _running(run: Runner) -> bool:
    result = run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    return result.returncode == 0 and "state = running" in (result.stdout or "")


def _manual_server(state_dir: Path) -> bool:
    """True while an `orchestrator ui` started from a terminal holds this user's instance lease."""
    try:
        fd = os.open(state_dir / "ui-instance.lock", os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                return json.loads(os.pread(fd, 4096, 0) or b"{}").get("kind") == "cli"
            except (ValueError, AttributeError):
                return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def inspect_legacy(state_dir: Path, run: Runner = subprocess.run, launch_agents: Path | None = None) -> dict:
    """{state: none | foreign | running | stopped, manual_server}. Carries no paths, arguments or process IDs."""
    path = _definition(launch_agents)
    manual = _manual_server(state_dir)
    if not path.exists() and not path.is_symlink():
        return {"state": "none", "manual_server": manual}
    if _read_ours(path) is None:
        return {"state": "foreign", "manual_server": manual}
    return {"state": "running" if _running(run) else "stopped", "manual_server": manual}


def migrate_legacy(state_dir: Path, consent: bool, run: Runner = subprocess.run, launch_agents: Path | None = None) -> dict:
    """Back up and remove a stopped legacy service. Never stops a running process or touches anything else."""
    found = inspect_legacy(state_dir, run, launch_agents)
    if not consent:
        return {"migrated": False, "state": found["state"], "reason": "consent_required"}
    if found["state"] != "stopped":
        return {"migrated": False, "state": found["state"], "reason": found["state"]}
    path = _definition(launch_agents)
    content = _read_ours(path)
    # Ask launchd again right before removal: KeepAlive may have restarted it since the person looked.
    if content is None or _running(run):
        return {"migrated": False, "state": "running" if content else "foreign", "reason": "changed"}
    backups = state_dir / "backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    backup = backups / f"{LABEL}-{time.strftime('%Y%m%d-%H%M%S')}.plist"
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    path.unlink()
    return {"migrated": True, "state": "none", "backup": str(backup)}
