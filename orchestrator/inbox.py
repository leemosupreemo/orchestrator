"""Everything waiting on a person, in one ranked list.

Pure ranking over data the web server already has: job summaries (with their
`state`) and terminal runs. Failures rank above plain decisions, and items from
other projects come after the active project's, so the list answers "what do I
do next?" without opening each job.
"""
from __future__ import annotations

from typing import Any

TONE_RANK = {"failed": 0, "attention": 1}
MAX_OTHER_PER_PROJECT = 20


def _job_item(job: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    state = job.get("state") or {}
    return {"kind": "job", "id": f"job:{project['root']}:{job['id']}", "job_id": job["id"], "title": job.get("title") or job["id"],
            "label": state.get("label") or "Needs you", "reason": state.get("reason") or "", "tone": state.get("tone") or "attention",
            "next": state.get("next"), "feature": job.get("feature"), "updated": job.get("updated") or 0, "project": project}


def _run_item(run: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "run", "id": f"run:{run['id']}", "run_id": run["id"], "title": run.get("title") or run.get("action") or "Run",
            "label": "Waiting for input", "reason": run.get("last_line") or "This run stopped at a prompt.", "tone": "attention",
            "next": None, "updated": run.get("started") or 0, "project": project}


def _rank(item: dict[str, Any]) -> tuple:
    return (TONE_RANK.get(item["tone"], 2), -float(item["updated"] or 0))


def build(active: dict[str, Any], jobs: list[dict[str, Any]], runs: list[dict[str, Any]],
          others: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """`active`/each of `others`: {name, root}. `others` entries also carry `jobs`."""
    here = [_job_item(j, active) for j in jobs
            if (j.get("state") or {}).get("group") == "needs_you" and not j.get("active_run")]
    here += [_run_item(r, active) for r in runs if r.get("running") and r.get("waiting")]
    elsewhere = []
    for project in others or []:
        meta = {"name": project["name"], "root": project["root"]}
        mine = [_job_item(j, meta) for j in project.get("jobs", []) if (j.get("state") or {}).get("group") == "needs_you"]
        elsewhere += sorted(mine, key=_rank)[:MAX_OTHER_PER_PROJECT]
    here.sort(key=_rank)
    elsewhere.sort(key=lambda i: (i["project"]["name"].lower(),) + _rank(i))
    return {"here": here, "elsewhere": elsewhere, "count": len(here)}
