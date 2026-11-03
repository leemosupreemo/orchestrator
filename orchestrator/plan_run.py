"""Build the plan: run a chosen set of features in dependency order, independent ones in parallel.

A *plan run* lists features. Each tick of the server's background loop asks `decide` what to do next:

- a feature is **ready** when everything it depends on is done; ready features get a feature job (planned, verified
  and built like any other), as many at once as the machines can take;
- with `auto_approve`, a plan that is ready for approval (no open question, no architect concerns) is approved,
  exactly as the Approve button would; otherwise the person approves it;
- a feature whose job needs attention holds back only the features that depend on it;
- the run is finished when every feature in it is done. Pausing stops new starts and Stop removes the run; neither
  stops anything already running.

`decide` is pure over the run, the features and job summaries, so the rules are easy to read and test. The run is
kept in `<runtime>/plan-run.json`.
"""
from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

STARTING_TIMEOUT = 30 * 60  # a planning run that hasn't produced its job in this long is treated as failed


class PlanRunError(ValueError):
    pass


def path(runtime: Path) -> Path:
    return runtime / "plan-run.json"


def load(runtime: Path) -> dict[str, Any] | None:
    try:
        run = json.loads(path(runtime).read_text())
    except (OSError, ValueError):
        return None
    return run if isinstance(run, dict) and isinstance(run.get("features"), list) else None


def save(runtime: Path, run: dict[str, Any]) -> None:
    target = path(runtime)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(run, indent=2))
    temporary.replace(target)


def clear(runtime: Path) -> None:
    try:
        path(runtime).unlink()
    except FileNotFoundError:
        pass


def start(runtime: Path, feature_ids: list[str], features: list[dict[str, Any]], auto_approve: bool) -> dict[str, Any]:
    known = {f["id"] for f in features}
    chosen = [fid for fid in dict.fromkeys(feature_ids) if fid in known]
    if not chosen:
        raise PlanRunError("Choose at least one feature to build.")
    existing = load(runtime)
    if existing and not existing.get("finished"):
        raise PlanRunError("A plan is already being built. Stop it first to start another.")
    run = {"id": secrets.token_hex(4), "features": chosen, "auto_approve": bool(auto_approve), "paused": False,
           "finished": False, "starting": {}}
    save(runtime, run)
    return run


def capacity(machines: list[dict[str, Any]]) -> int:
    """How many jobs can build at once: the enabled machines' concurrent-job allowances (at least one)."""
    total = 0
    for machine in machines:
        if machine.get("enabled", True):
            try:
                total += max(1, int(machine.get("max_concurrent_jobs") or 1))
            except (TypeError, ValueError):
                total += 1
    return max(1, total)


def job_for(feature_id: str, run: dict[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The job this run created for a feature (the latest, if it was replanned)."""
    mine = [j for j in jobs if j.get("feature") == feature_id and j.get("plan_run") == run["id"]]
    return max(mine, key=lambda j: j.get("updated") or 0) if mine else None


def _status(feature: dict[str, Any], job: dict[str, Any] | None, planning: dict[str, Any] | None, alive: bool, now: float) -> str:
    if feature.get("status") == "complete" or (job and (job.get("state") or {}).get("group") == "done"):
        return "done"
    if job:
        return "working" if (job.get("state") or {}).get("group") == "working" else "attention"
    if planning is not None:
        return "planning" if alive and now - float(planning.get("at", now)) < STARTING_TIMEOUT else "failed_start"
    return "pending"


DETAIL = {"failed_start": "Planning didn't finish. Open its run, then resume to try again.", "planning": "Planning"}


def decide(run: dict[str, Any], features: list[dict[str, Any]], jobs: list[dict[str, Any]], slots: int,
           starting_alive: dict[str, bool] | None = None, now: float | None = None, can_start: bool = True) -> dict[str, Any]:
    """What to do now: {"start": [feature ids], "approve": [job ids], "rows": [...], "finished": bool}.

    `starting_alive` says, for features whose job is still being planned, whether that planning run is still going.
    With `can_start` off (a page showing the run) nothing is started or approved; the rows are the same."""
    now = time.time() if now is None else now
    by_id = {f["id"]: f for f in features}
    starting, alive = run.get("starting") or {}, starting_alive or {}
    members = [fid for fid in run["features"] if fid in by_id]  # a feature deleted since has nothing left to wait for
    job_of = {fid: job_for(fid, run, jobs) for fid in members}
    status = {fid: _status(by_id[fid], job_of[fid], starting.get(fid), alive.get(fid, True), now) for fid in members}
    busy = sum(s in ("working", "planning") for s in status.values())
    approve = [job_of[fid]["id"] for fid in members if can_start and run.get("auto_approve") and job_of[fid]
               and status[fid] == "attention" and (job_of[fid].get("state") or {}).get("label") == "Approve plan"
               and ((job_of[fid].get("state") or {}).get("next") or {}).get("action") == "approve"]

    def waits_on(fid: str) -> list[str]:
        """Not-yet-done features this one builds on (one outside the run counts once it is complete)."""
        return [d for d in by_id[fid].get("depends_on") or []
                if (status[d] != "done" if d in status else d in by_id and by_id[d].get("status") != "complete")]

    start: list[str] = []
    free = max(0, slots - busy) if can_start and not run.get("paused") else 0
    rows: list[dict[str, Any]] = []
    for fid in members:
        job, state = job_of[fid], status[fid]
        detail = ((job.get("state") or {}).get("label") or "") if job else DETAIL.get(state, "")
        waits = waits_on(fid)
        if state == "pending" and waits:
            stuck = any(status.get(d) in ("attention", "failed_start") for d in waits)
            state = "waiting"
            detail = "Waiting on " + ", ".join(by_id[d]["name"] for d in waits) + (" (it needs attention)" if stuck else "")
        elif state == "pending" and free > 0:
            start.append(fid)
            free -= 1
            state, detail = "planning", "Starting"
        elif state == "pending":
            state = "ready"
            detail = "Paused" if run.get("paused") else "Ready: waiting for a free machine" if can_start else "Ready"
        rows.append({"feature": fid, "name": by_id[fid]["name"], "state": state, "job": job["id"] if job else None,
                     "waiting_on": waits, "detail": detail})
    return {"start": start, "approve": approve, "rows": rows, "finished": all(s == "done" for s in status.values())}
