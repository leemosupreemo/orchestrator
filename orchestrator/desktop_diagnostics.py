"""What the desktop app may export for support: typed states, counts, versions and tool presence.

Every field is copied through an allowlist. Free text (errors, log lines, paths, accounts) is never included, so a
value that doesn't match its expected form becomes "unknown" instead of being cleaned up after the fact.
"""
from __future__ import annotations

import re

STATES = {
    "agent": {"running", "starting"},
    "setup": {"needs_project", "needs_pairing", "ready"},
    "local_interface": {"ready", "starting"},
    "remote_access": {"off", "connecting", "connected", "unavailable", "waiting_for_legacy"},
    "update": {"current", "installing"},
    "pairing": {"not_connected", "starting", "waiting", "connected", "expired", "cancelled", "failed"},
    "legacy": {"none", "foreign", "running", "stopped"},
    "last_error": {"", "pairing_failed", "tunnel_failed", "heartbeat_failed"},
}
CHECK_NAMES = {"git", "gh", "xcodebuild", "cloudflared", "claude", "codex", "opencode"}
CHECK_STATES = {"found", "missing"}
VERSION = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,31}$")


def _state(value, allowed):
    return value if isinstance(value, str) and value in allowed else "unknown"


def _count(value):
    return value if type(value) is int and 0 <= value < 1_000_000 else None


def diagnostics_snapshot(status: dict, checks: list) -> dict:
    status = status if isinstance(status, dict) else {}
    activity = status.get("activity") if isinstance(status.get("activity"), dict) else {}
    runner = status.get("runner") if isinstance(status.get("runner"), dict) else {}
    pairing = status.get("pairing") if isinstance(status.get("pairing"), dict) else {}
    version = runner.get("version")
    report = {"schema": 1, **{key: _state(status.get(key), allowed) for key, allowed in STATES.items() if key != "pairing"},
              "pairing": _state(pairing.get("state"), STATES["pairing"]),
              "remote_enabled": status.get("remote_enabled") if type(status.get("remote_enabled")) is bool else None,
              "project_selected": isinstance(status.get("project"), dict),
              "activity": {"runs": _count(activity.get("runs")), "tasks": _count(activity.get("tasks"))},
              "runner": {"version": version if isinstance(version, str) and VERSION.match(version) else "unknown",
                         "api_version": _count(runner.get("api_version"))},
              "checks": []}
    for check in checks[:20] if isinstance(checks, list) else []:
        if isinstance(check, dict) and check.get("name") in CHECK_NAMES and check.get("state") in CHECK_STATES:
            report["checks"].append({"name": check["name"], "state": check["state"]})
    return report
