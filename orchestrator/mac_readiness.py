"""Whether this Mac can run Orchestrator unattended: a Mac mini in a closet needs to log in by itself, stay awake,
come back after a power cut, have Xcode's license accepted and keep its login item approved.

Only reads settings; changing them needs an administrator and is the owner's call. Each check is a typed state so it
can travel in diagnostics and the heartbeat: "ok", a named problem, or "unknown" when the setting can't be read or
doesn't apply (a laptop has no restart-after-power-failure setting).
"""
from __future__ import annotations

import getpass
import json
import re
import subprocess
from pathlib import Path
from typing import Callable

Runner = Callable[..., subprocess.CompletedProcess]
CHECKS = ("auto_login", "sleep", "power_restart", "xcode_license", "login_item")
STATES = {"ok", "off", "on", "not_accepted", "needs_approval", "not_installed", "unknown"}
# The ones the hosted app warns about, with what fixes them.
FIXES = {
    ("auto_login", "off"): "Turn on automatic login for this user (System Settings › Users & Groups). FileVault must be off.",
    ("sleep", "on"): "Stop it sleeping: sudo pmset -a sleep 0 disksleep 0",
    ("power_restart", "off"): "Restart after a power cut: sudo pmset -a autorestart 1",
    ("xcode_license", "not_accepted"): "Accept Xcode's license: sudo xcodebuild -license accept",
    ("login_item", "needs_approval"): "Allow Orchestrator in System Settings › General › Login Items (once, over Screen Sharing).",
}


def _output(run: Runner, argv: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None


def _pmset_value(text: str, key: str) -> int | None:
    match = re.search(rf"^\s*{re.escape(key)}\s+(\d+)", text, re.MULTILINE)
    return int(match.group(1)) if match else None


def check(run: Runner = subprocess.run, user: str | None = None, control_dir: Path | None = None) -> dict[str, str]:
    user = user or getpass.getuser()
    result = {name: "unknown" for name in CHECKS}
    login = _output(run, ["/usr/bin/defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser"])
    if login is not None:
        if login.returncode == 0:
            result["auto_login"] = "ok" if login.stdout.strip() == user else "off"
        elif "does not exist" in (login.stderr or "") or "Could not find" in (login.stderr or ""):
            result["auto_login"] = "off"
    power = _output(run, ["/usr/bin/pmset", "-g"])
    if power is not None and power.returncode == 0:
        sleep = _pmset_value(power.stdout, "sleep")
        if sleep is not None:
            result["sleep"] = "ok" if sleep == 0 else "on"
        restart = _pmset_value(power.stdout, "autorestart")
        if restart is not None:
            result["power_restart"] = "ok" if restart else "off"
    developer = _output(run, ["/usr/bin/xcode-select", "-p"])
    if developer is not None:
        if developer.returncode != 0 or "Xcode" not in developer.stdout:
            result["xcode_license"] = "not_installed"
        else:
            license = _output(run, ["/usr/bin/xcodebuild", "-license", "check"])
            if license is not None:
                result["xcode_license"] = "ok" if license.returncode == 0 else "not_accepted"
    if control_dir is not None:
        try:
            registration = json.loads((control_dir / "background.json").read_text()).get("registration")
            result["login_item"] = {"enabled": "ok", "requiresApproval": "needs_approval"}.get(registration, "unknown")
        except (OSError, ValueError, AttributeError):
            pass
    return result


def clean(value) -> dict[str, str]:
    """A reported readiness dict reduced to known checks and states (for the control plane and diagnostics)."""
    if not isinstance(value, dict):
        return {}
    return {name: value[name] for name in CHECKS if value.get(name) in STATES}


def warnings(readiness: dict[str, str]) -> list[dict[str, str]]:
    return [{"check": name, "fix": FIXES[(name, state)]} for name, state in clean(readiness).items() if (name, state) in FIXES]
