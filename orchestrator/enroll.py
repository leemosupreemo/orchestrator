"""`orchestrator enroll`: set up a Mac nobody is sitting at, such as a Mac mini in a closet, over SSH.

The hosted app's Add a Mac gives a one-time token instead of a pairing code, since nobody can read a code off that
Mac's screen. Enrolling sets up the project first (so a typo doesn't use up the token), redeems the token, then asks
the app to register its login items and start its agent. The agent runs as the logged-in user, because builds,
simulators and keychain signing need a user session, so the Mac needs automatic login.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from orchestrator import account
from orchestrator.project_config import remember_project
from orchestrator.project_setup import FIELDS, apply_project_setup, inspect_project

CONTROL_SOCKET = Path.home() / "Library/Application Support/Orchestrator/control/agent.sock"
GIT_URL = re.compile(r"^(?:[a-z+]+://|[\w.-]+@[\w.-]+:)\S+$")


def containing_app(executable: Path) -> Path | None:
    """The Orchestrator.app around this runtime, or None for a pipx or source install."""
    for parent in executable.parents:
        if parent.suffix == ".app" and (parent / "Contents/Info.plist").is_file():
            return parent
    return None


def _console_user() -> str:
    try:
        return subprocess.run(["/usr/bin/stat", "-f", "%Su", "/dev/console"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def _agent_ready(timeout: float) -> bool:
    """Waits for the app's agent to answer on its control socket with a project and an account."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(3)
                client.connect(str(CONTROL_SOCKET))
                client.sendall(json.dumps({"version": 1, "request_id": "enroll", "command": "status", "params": {}}).encode() + b"\n")
                with client.makefile("rb") as stream:
                    reply = json.loads(stream.readline())
            if reply.get("ok") and reply["result"].get("setup") == "ready":
                return True
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(1)
    return False


def _registration(since: float, timeout: float) -> str:
    """The login-item state the menu app recorded after `since` ("enabled", "requiresApproval", ...), or ""."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            record = json.loads((CONTROL_SOCKET.parent / "background.json").read_text())
            if float(record["at"]) >= since:
                return "pending" if record.get("pending") else str(record["registration"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(1)
    return ""


@dataclass
class System:
    """What enrolling touches outside Python; tests replace all of it."""
    console_user: Callable[[], str] = _console_user
    current_user: Callable[[], str] = getpass.getuser
    is_root: Callable[[], bool] = lambda: os.geteuid() == 0
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    redeem: Callable[[dict], dict] = lambda body: account.call("enroll/redeem", body)
    agent_ready: Callable[[float], bool] = _agent_ready
    registration: Callable[[float, float], str] = _registration
    app: Path | None = field(default_factory=lambda: containing_app(Path(sys.executable).resolve()))
    projects_dir: Path = field(default_factory=lambda: Path.home() / "Projects")


class EnrollError(Exception):
    pass


def read_token(token: str | None, from_stdin: bool) -> str:
    if from_stdin:
        token = sys.stdin.readline().strip()
    if not token:
        raise ValueError("Pass the token from Add a Mac with --token, or --token-stdin to read it from standard input.")
    return token


def _project(spec: str, models: list[str], system: System) -> Path:
    """The project folder, cloned if `spec` is a git URL, and configured if it wasn't already."""
    if GIT_URL.match(spec):
        name = re.sub(r"\.git$", "", spec.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]) or "project"
        root = system.projects_dir / name
        if root.exists():
            raise EnrollError(f"{root} already exists. Pass that folder with --project instead, or move it.")
        root.parent.mkdir(parents=True, exist_ok=True)
        result = system.run(["git", "clone", spec, str(root)], capture_output=True, text=True,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if result.returncode != 0:
            raise EnrollError(f"Couldn't clone {spec}: {(result.stderr or '').strip().splitlines()[-1:] or 'git failed'}. "
                              "Check this Mac's git access (an SSH key, or `gh auth login`).")
    else:
        root = Path(spec).expanduser()
        if not root.is_dir():
            raise EnrollError(f"There's no folder at {root}.")
    found = inspect_project(root)
    if found["configured"]:
        remember_project(Path(found["root"]), active=True)
        return Path(found["root"])
    values = {key: value for key, value in found["inferred"].items() if key in FIELDS and value}
    applied = apply_project_setup(root, {**values, "models": models})
    if not applied["ok"]:
        problems = "\n".join(f"    - {error}" for error in applied["errors"])
        raise EnrollError(f"{root} needs a little setup Orchestrator couldn't work out:\n{problems}\n"
                          f"  The folder was kept. Finish with `orchestrator wizard` in it, then run this command again with --project {root}.")
    return Path(applied["root"])


def enroll(token: str, project: str, system: System | None = None, models: list[str] | None = None,
           name: str | None = None, out: Callable[[str], object] = sys.stdout.write) -> int:
    system = system or System()

    def say(line: str) -> None:
        out(line + "\n")

    try:
        if system.is_root():
            raise EnrollError("Run this as the Mac user Orchestrator should work as, not as root.")
        user = system.current_user()
        if system.console_user() != user:
            raise EnrollError(f"{user} isn't logged in on this Mac's screen. Orchestrator runs in that login session, "
                              "so turn on automatic login for this user (System Settings › Users & Groups), restart, "
                              "then run this again.")
        if system.app is not None and str(system.app).startswith("/Volumes/"):
            raise EnrollError("Copy Orchestrator to Applications first; it can't run from the disk image.")
        root = _project(project, models or ["codex"], system)
        say(f"  ✓ Project: {root}")
        machine = account.load_machine()
        if machine:
            say(f"  ✓ Already connected to {machine.get('owner_email', 'an account')}; the enrollment token wasn't used.")
        else:
            try:
                redeemed = system.redeem({"token": token, "name": name or account.default_name(), "os": account.os_label(),
                                          "version": account.package_version()})
            except account.AccountError as exc:
                raise EnrollError(str(exc)) from None
            machine = {"machine_id": redeemed["machine_id"], "machine_secret": redeemed["machine_secret"],
                       "owner_email": str(redeemed.get("owner_email") or "").lower(), "name": name or account.default_name(),
                       "control_url": account.control_url(), "paired_at": time.time()}
            account.save_machine(machine)
            say(f"  ✓ Connected to {machine['owner_email']}")
        if system.app is None:
            say("  Orchestrator isn't installed as the app here, so start it in the background with "
                "`orchestrator service install`.")
            return 0
        started = time.time()
        system.run(["/usr/bin/open", "-g", "-j", "-a", str(system.app), "--args", "--register-background"],
                   capture_output=True, text=True)
        approval = ("connect once with Screen Sharing and allow Orchestrator in System Settings › General › Login Items, "
                    "then run this again.")
        if not system.agent_ready(60):
            raise EnrollError("The app didn't start its background agent. macOS may be asking for approval: " + approval)
        registration = system.registration(started, 30)
        if registration == "pending":
            say("  ✓ Running in the background. Starting at login is turned on once its current work finishes.")
        elif registration != "enabled":
            raise EnrollError("Orchestrator is running now, but it won't start again after a restart until macOS "
                              "approves it: " + approval)
        else:
            say("  ✓ Running in the background, and starting at login")
        say(f"  Open it from {account.HOSTED_APP_URL}")
        return 0
    except EnrollError as exc:
        say(f"  ✗ {exc}")
        return 1
