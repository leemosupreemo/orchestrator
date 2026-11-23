"""Is this computer keeping the others up to date?

Each way this computer shares work (the account heartbeat, the job records it sends, its jobs' branches and unfinished
work on origin) records its last success and failure here. A failure that lasts past GRACE_SECONDS is a problem worth
showing, with what to do about it: the computer that can't sync is the one that has to fix it, and the others only see
stale information, so it has to say so itself.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from orchestrator.project_config import user_state_dir

GRACE_SECONDS = 600  # brief blips (a laptop waking, a Wi-Fi hop) don't nag
STALE_FAILURE_SECONDS = 900  # if no failure was recorded recently (e.g. laptop slept), wait for a fresh check before warning
CHANNELS = {
    "account": "Checking in with your account",
    "jobs": "Sending job updates to your account",
    "branches": "Backing up job branches to GitHub",
    "unfinished": "Backing up unfinished work to GitHub",
    "product": "Sharing your product requirements through your repository",
    "shared_jobs": "Sharing jobs through your repository",
}
_lock = threading.Lock()


def _file() -> Path:
    return user_state_dir() / "sync_health.json"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(channel: str, ok: bool, error: str = "", now: float | None = None) -> None:
    """One attempt on `channel`. A success clears the failure; a failure keeps when failing began."""
    if channel not in CHANNELS:
        return
    now = time.time() if now is None else now
    with _lock:
        data = _load()
        entry = dict(data.get(channel) or {})
        if ok:
            if entry.get("ok_at") and not entry.get("failing_since") and now - float(entry["ok_at"]) < 60:
                return  # nothing new worth a write
            entry.update(ok_at=now)
            entry.pop("failing_since", None)
            entry.pop("failed_at", None)
            entry.pop("error", None)
        else:
            prev_failed_at = entry.get("failed_at")
            if prev_failed_at and now - float(prev_failed_at) > STALE_FAILURE_SECONDS:
                entry["failing_since"] = now  # prior failure was before sleep/idle: start a fresh grace window
            else:
                entry.setdefault("failing_since", now)
            entry.update(failed_at=now, error=_clean(error))
        data[channel] = entry
        try:
            _file().parent.mkdir(parents=True, exist_ok=True)
            _file().write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass


def clear_failures() -> None:
    """Drop prior-session failure markers on launch so stale errors don't show before the fresh round runs."""
    with _lock:
        data = _load()
        changed = False
        for entry in data.values():
            if isinstance(entry, dict) and any(k in entry for k in ("failing_since", "failed_at", "error")):
                entry.pop("failing_since", None)
                entry.pop("failed_at", None)
                entry.pop("error", None)
                changed = True
        if changed:
            try:
                _file().parent.mkdir(parents=True, exist_ok=True)
                _file().write_text(json.dumps(data), encoding="utf-8")
            except OSError:
                pass


def _clean(error: str) -> str:
    text = re.sub(r"(https?://)[^/@\s]+@", r"\1", str(error or "")).strip()  # never a token from a remote URL
    return text.splitlines()[-1][:300] if text else "Unknown error"


def advice(error: str) -> str:
    """What to do, in words, for the usual causes."""
    e = error.lower()
    if any(w in e for w in ("could not resolve host", "network is unreachable", "timed out", "connection refused",
                            "nodename nor servname", "temporary failure in name resolution", "urlopen error", "offline")):
        return "This computer seems to be offline. It catches up by itself once it's back online."
    if any(w in e for w in ("401", "410", "removed from its account", "sign in", "unauthorized")):
        return "This computer isn't signed in to your account any more. Add it again under Your computers (`orchestrator connect`)."
    if any(w in e for w in ("403", "permission", "denied", "authentication failed", "could not read username")):
        return "GitHub refused the push. Check this computer's GitHub sign-in (Git setup) and that it may push to this repository."
    if "rejected" in e or "non-fast-forward" in e:
        return "GitHub has newer commits on that branch. Continue the job here or pull the branch, then it backs up again."
    return "It keeps retrying. If this lasts, check this computer's connection and Git setup."


def problems(now: float | None = None) -> list[dict[str, Any]]:
    """Channels failing for longer than the grace period, each with since when, the error and what to do."""
    now = time.time() if now is None else now
    out = []
    for channel, entry in _load().items():
        since = (entry or {}).get("failing_since")
        failed_at = (entry or {}).get("failed_at")
        if failed_at and now - float(failed_at) > STALE_FAILURE_SECONDS:
            continue
        if channel in CHANNELS and since and now - float(since) >= GRACE_SECONDS:
            error = str(entry.get("error") or "")
            out.append({"channel": channel, "label": CHANNELS[channel], "since": float(since),
                        "last_ok": entry.get("ok_at"), "error": error, "advice": advice(error)})
    return sorted(out, key=lambda p: p["since"])


def overview(now: float | None = None) -> dict[str, Any]:
    """Every channel's state, for the details under the warning."""
    data = _load()
    return {"problems": problems(now), "channels": [
        {"channel": c, "label": label, "last_ok": (data.get(c) or {}).get("ok_at"),
         "failing_since": (data.get(c) or {}).get("failing_since"), "error": (data.get(c) or {}).get("error")}
        for c, label in CHANNELS.items()]}
