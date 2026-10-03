"""Build the plan: run a chosen set of features in dependency order, independent ones in parallel.

A *plan run* lists features. Each tick of the server's background loop asks `decide` what to do next:

- a feature is **ready** when everything it depends on is done; ready features get a feature job (planned, verified
  and built like any other), as many at once as the machines can take;
- with `auto_approve`, a plan that is ready for approval (no open question, no architect concerns) is approved,
  exactly as the Approve button would; otherwise the person approves it;
- a feature whose job needs attention holds back only the features that depend on it;
- the run is finished when every feature in it is done. Pausing stops new starts; nothing running is stopped.

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


def start(runtime: Path, feature_ids: list[str], features: list[dict[str, Any]], auto_approve: bool,
          now: float | None = None) -> dict[str, Any]:
    known = {f["id"]: f for f in features}
    chosen = [fid for fid in dict.fromkeys(feature_ids) if fid in known]
    if not chosen:
        raise PlanRunError("Choose at least one feature to build.")
    existing = load(runtime)
    if existing and not existing.get("finished") and not existing.get("stopped"):
        raise PlanRunError("A plan is already being built. Stop it first to start another.")
    run = {"id": secrets.token_hex(4), "features": chosen, "auto_approve": bool(auto_approve), "paused": False,
           "stopped": False, "finished": False, "started": now if now is not None else time.time(), "starting": {}, "failed_start": {}}
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


def _job_for(feature_id: str, run: dict[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any] | None:
    mine = [j for j in jobs if j.get("feature") == feature_id and j.get("plan_run") == run["id"]]
    return max(mine, key=lambda j: j.get("updated") or 0) if mine else None


def _done(feature: dict[str, Any] | None, job: dict[str, Any] | None) -> bool:
    return bool(feature and feature.get("status") == "complete") or bool(job and (job.get("state") or {}).get("group") == "done")


def decide(run: dict[str, Any], features: list[dict[str, Any]], jobs: list[dict[str, Any]], slots: int,
           starting_alive: dict[str, bool] | None = None, now: float | None = None) -> dict[str, Any]:
    """What to do now: {"start": [feature ids], "approve": [job ids], "rows": [...], "finished": bool}.

    `starting_alive` says, for features whose job is still being planned, whether that planning run is still going."""
    now = time.time() if now is None else now
    by_id = {f["id"]: f for f in features}
    starting = run.get("starting") or {}
    starting_alive = starting_alive or {}
    status: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    approve: list[str] = []
    busy = 0
    for fid in run["features"]:
        feature, job = by_id.get(fid), _job_for(fid, run, jobs)
        if feature is None:
            status[fid] = "done"  # deleted since: nothing to wait for
            continue
        if _done(feature, job):
            status[fid] = "done"
        elif job:
            state = job.get("state") or {}
            status[fid] = "working" if state.get("group") == "working" else "attention"
            busy += state.get("group") == "working"
            if (run.get("auto_approve") and state.get("label") == "Approve plan"
                    and (state.get("next") or {}).get("action") == "approve"):
                approve.append(job["id"])
        elif fid in starting:
            alive = starting_alive.get(fid, True) and now - float(starting[fid].get("at", now)) < STARTING_TIMEOUT
            status[fid] = "planning" if alive else "failed_start"
            busy += alive
        elif fid in (run.get("failed_start") or {}):
            status[fid] = "failed_start"
        else:
            status[fid] = "pending"

    def blocker(fid: str) -> list[str]:
        """The not-yet-done features this one waits on (dependencies outside the run count once done or complete)."""
        out = []
        for dep in (by_id.get(fid) or {}).get("depends_on") or []:
            if dep in status:
                if status[dep] != "done":
                    out.append(dep)
            elif dep in by_id and by_id[dep].get("status") != "complete":
                out.append(dep)
        return out

    start: list[str] = []
    free = 0 if run.get("paused") or run.get("stopped") else max(0, slots - busy)
    for fid in run["features"]:
        if fid not in by_id:
            continue
        name = by_id[fid]["name"]
        waits = blocker(fid)
        job = _job_for(fid, run, jobs)
        row = {"feature": fid, "name": name, "state": status[fid], "job": job["id"] if job else None, "waiting_on": waits,
               "detail": ""}
        if status[fid] == "pending":
            if waits:
                stuck = [d for d in waits if status.get(d) in ("attention", "failed_start")]
                row["state"] = "waiting"
                row["detail"] = ("Waiting on " + ", ".join(by_id[d]["name"] for d in waits if d in by_id)
                                 + (" (it needs attention)" if stuck else ""))
            elif free > 0:
                start.append(fid)
                free -= 1
                row["state"], row["detail"] = "planning", "Starting"
            else:
                row["state"] = "ready"
                row["detail"] = "Paused" if run.get("paused") else "Ready: waiting for a free machine"
        elif status[fid] == "attention" and job:
            row["detail"] = (job.get("state") or {}).get("label") or "Needs you"
        elif status[fid] == "failed_start":
            row["detail"] = "Planning didn't finish. Open its run, then resume to try again."
        elif status[fid] == "working" and job:
            row["detail"] = (job.get("state") or {}).get("label") or "Working"
        elif status[fid] == "planning":
            row["detail"] = "Planning"
        rows.append(row)
    finished = all(s == "done" for s in status.values())
    return {"start": start, "approve": approve, "rows": rows, "finished": finished}
