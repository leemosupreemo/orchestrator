"""What's live, what testers have, and what's on the way.

Everything is read from places the orchestrator already writes: git (the base
branch is "live", the latest tag is the last release), delivery receipts
(`<runtime>/output/delivery/<job>.json`, written after each tester build) and,
when `gh` is signed in, the project's recent GitHub Actions runs. `git` and `gh`
are injected so this stays testable and the web server owns the subprocesses.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

GitFn = Callable[..., str]
GhFn = Callable[[list[str]], "str | None"]

CI_FIELDS = "name,status,conclusion,url,createdAt,headBranch,displayTitle,event"
MAX_RECEIPTS = 20


def live(git: GitFn, base: str) -> dict[str, Any] | None:
    """The base branch as production: its newest commit, the last tag, and how much is unreleased."""
    if not git("rev-parse", "--verify", "--quiet", base):
        return None
    commit = git("log", "-1", "--format=%h%x09%s%x09%cr", base).split("\t")
    tag = git("describe", "--tags", "--abbrev=0", base) or None
    unreleased = git("rev-list", "--count", f"{tag}..{base}") if tag else git("rev-list", "--count", base)
    return {"branch": base, "commit": commit[0] if commit else "", "subject": commit[1] if len(commit) > 1 else "",
            "when": commit[2] if len(commit) > 2 else "", "tag": tag,
            "unreleased": int(unreleased) if unreleased.isdigit() else None}


def receipts(runtime: Path) -> list[dict[str, Any]]:
    """Tester builds, newest first."""
    folder = runtime / "output" / "delivery"
    out = []
    for path in folder.glob("*.json") if folder.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("status") != "delivered":
            continue
        out.append({"job_id": data.get("job_id") or path.stem, "title": data.get("title") or "", "branch": data.get("branch") or "",
                    "version": data.get("version"), "build": data.get("build"), "provider": data.get("provider") or "firebase",
                    "recipients": data.get("groups") or data.get("testers") or "", "built_at": data.get("built_at"),
                    "delivered": path.stat().st_mtime})
    return sorted(out, key=lambda r: r["delivered"], reverse=True)[:MAX_RECEIPTS]


def pipeline(gh: GhFn, base: str, limit: int = 6) -> dict[str, Any]:
    """Recent GitHub Actions runs. `available` is False when gh is missing or not signed in."""
    raw = gh(["run", "list", "--limit", str(limit), "--json", CI_FIELDS])
    if raw is None:
        return {"available": False, "runs": []}
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return {"available": False, "runs": []}
    runs = []
    for r in rows if isinstance(rows, list) else []:
        done = r.get("status") == "completed"
        outcome = r.get("conclusion") if done else r.get("status")
        tone = {"success": "done", "failure": "failed", "cancelled": "attention", "timed_out": "failed"}.get(outcome, "working")
        runs.append({"name": r.get("name") or "", "title": r.get("displayTitle") or "", "branch": r.get("headBranch") or "",
                     "outcome": outcome or "unknown", "tone": tone, "url": r.get("url"), "created": r.get("createdAt"),
                     "on_base": r.get("headBranch") == base})
    return {"available": True, "runs": runs}


def ready_to_ship(jobs: list[dict[str, Any]], shipped_jobs: set[str]) -> list[dict[str, Any]]:
    """Jobs with a branch that finished building but haven't gone to testers or into production."""
    out = []
    for j in jobs:
        state = j.get("state") or {}
        if not j.get("branch") or j.get("status") != "review-needed" or j["id"] in shipped_jobs:
            continue
        out.append({"id": j["id"], "title": j.get("title") or j["id"], "branch": j["branch"], "pr_number": j.get("pr_number"),
                    "next": state.get("next"), "feature": j.get("feature")})
    return out


def overview(git: GitFn, gh: GhFn, runtime: Path, base: str, jobs: list[dict[str, Any]], firebase: dict[str, Any],
             ci_configured: bool) -> dict[str, Any]:
    sent = receipts(runtime)
    return {"live": live(git, base), "testers": {"latest": sent[0] if sent else None, "builds": sent, **firebase},
            "ready": ready_to_ship(jobs, {r["job_id"] for r in sent}), "pipeline": pipeline(gh, base),
            "xcode_cloud": ci_configured}
