"""Epics: the breakdown of a product into epic-level pieces the person approves one by one.

A breakdown (from the product requirements, or imported from Jira) is a list of epics, each *suggested*, *approved* or
*declined*. Approving an epic creates its feature (so the planner, the plan run and the feature pages work as before)
and a waiting job labelled Epic: a job record with status "backlog" that does nothing until the person plans it. Planning
it runs the usual feature job, which takes the place of the waiting one (`attach_job`).

An epic can also be linked to a Jira epic (`jira`), either created from it or imported into it.

Stored in `<runtime>/epics.json`. Everything here is local file work; the model and Jira calls are made by the caller.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator import feature_map, features as store

STATUSES = ("suggested", "approved", "declined")
JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


class EpicError(ValueError):
    pass


def store_path(runtime: Path) -> Path:
    return runtime / "epics.json"


def load(runtime: Path) -> dict[str, Any]:
    try:
        data = json.loads(store_path(runtime).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    epics = [e for e in data.get("epics", []) if isinstance(e, dict) and e.get("id") and e.get("status") in STATUSES]
    return {"breakdown": data.get("breakdown") if isinstance(data.get("breakdown"), dict) else None, "epics": epics}


def save(runtime: Path, data: dict[str, Any]) -> None:
    path = store_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"breakdown": data.get("breakdown"), "epics": data["epics"]}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _find(epics: list[dict[str, Any]], epic_id: str) -> dict[str, Any]:
    for epic in epics:
        if epic["id"] == epic_id:
            return epic
    raise EpicError("That epic no longer exists")


def get(runtime: Path, epic_id: str) -> dict[str, Any]:
    return _find(load(runtime)["epics"], epic_id)


def for_feature(runtime: Path, feature_id: str) -> dict[str, Any] | None:
    return next((e for e in load(runtime)["epics"] if e.get("feature") == feature_id), None)


# --------------------------------------------------------------------------- suggestions


def add_suggestions(runtime: Path, proposal: dict[str, Any]) -> dict[str, Any]:
    """Put a model's breakdown in the list. Earlier suggestions that were never decided are replaced; approved and
    declined epics stay, and anything already there under the same name isn't suggested twice."""
    data = load(runtime)
    kept = [e for e in data["epics"] if e["status"] != "suggested"]
    taken = {e["name"].strip().lower() for e in kept}
    added = []
    for f in proposal.get("features") or []:
        if f["name"].strip().lower() in taken:
            continue
        added.append({"id": "ep-" + secrets.token_hex(4), "name": f["name"], "summary": f.get("summary", ""),
                      "requirements": f.get("requirements") or [], "stories": f.get("stories") or [], "slice": f.get("slice", ""),
                      "paths": f.get("paths") or [], "depends_on": f.get("depends_on") or [], "serves": f.get("serves", ""),
                      "layer": f.get("layer", 0), "status": "suggested", "source": "prd", "feature": "", "job": "", "jira": None,
                      "created": time.time()})
    if not added:
        raise EpicError("Every suggested epic is already in the list.")
    data["epics"] = kept + added
    data["breakdown"] = {k: proposal.get(k) for k in ("technique", "technique_name", "rationale", "open_questions", "detail", "warnings")} | {"at": time.time()}
    save(runtime, data)
    return data


def _candidates(epics: list[dict[str, Any]], ids: set[str]) -> list[dict[str, Any]]:
    """The epics to approve, plus every undecided epic they build on (an epic can't start before what it needs)."""
    by_name = {e["name"].strip().lower(): e for e in epics if e["status"] != "approved"}
    picked = {e["id"]: e for e in epics if e["id"] in ids and e["status"] != "approved"}
    todo = list(picked.values())
    while todo:
        for dep in todo.pop().get("depends_on") or []:
            needed = by_name.get(dep.strip().lower())
            if needed and needed["id"] not in picked:
                picked[needed["id"]] = needed
                todo.append(needed)
    return list(picked.values())


def _as_feature(epic: dict[str, Any]) -> dict[str, Any]:
    return {k: epic.get(k) for k in ("name", "summary", "requirements", "stories", "slice", "paths", "depends_on", "serves")}


def approve(runtime: Path, ids: list[str]) -> list[dict[str, Any]]:
    """Approve epics: each becomes a feature and a waiting Epic job. Returns the epics approved, prerequisites included."""
    data = load(runtime)
    picked = _candidates(data["epics"], set(ids))
    if not picked:
        raise EpicError("Choose at least one epic that isn't approved yet.")
    existing = store.load(runtime)
    try:
        checked = feature_map.validate([_as_feature(e) for e in picked], existing)["features"]
    except feature_map.FeatureMapError as exc:
        raise EpicError(str(exc))
    missing = {e["name"] for e in picked} - {f["name"] for f in checked}
    if missing:
        raise EpicError(f"{', '.join(sorted(missing))} already exists as a feature, so it can't be approved again. Rename or decline it.")
    try:
        created = feature_map.accept(runtime, checked, [f["name"] for f in checked])
    except (feature_map.FeatureMapError, store.FeatureError) as exc:
        raise EpicError(str(exc))
    by_name = {f["name"].strip().lower(): f for f in created}
    for epic in picked:
        feature = by_name[epic["name"].strip().lower()]
        epic.update(status="approved", feature=feature["id"], approved_at=time.time())
        epic["job"] = make_backlog_job(runtime, epic)
    save(runtime, data)
    return picked


def decline(runtime: Path, ids: list[str]) -> None:
    data = load(runtime)
    for epic in data["epics"]:
        if epic["id"] in ids and epic["status"] == "suggested":
            epic["status"] = "declined"
    save(runtime, data)


def restore(runtime: Path, ids: list[str]) -> None:
    data = load(runtime)
    for epic in data["epics"]:
        if epic["id"] in ids and epic["status"] == "declined":
            epic["status"] = "suggested"
    save(runtime, data)


# --------------------------------------------------------------------------- the waiting job


def brief(epic: dict[str, Any], heading: bool = True) -> str:
    """What the job is told about the epic: its summary, requirements and stories, and the Jira epic it came from."""
    lines = [f"# Epic: {epic['name']}", ""] if heading else []
    if epic.get("summary"):
        lines += [epic["summary"], ""]
    if epic.get("slice"):
        lines += [f"Release slice: {epic['slice']}", ""]
    if epic.get("requirements"):
        lines += ["## Requirements", *[f"- {r}" for r in epic["requirements"]], ""]
    if epic.get("stories"):
        lines += ["## User stories", *[f"- {s}" for s in epic["stories"]], ""]
    if epic.get("depends_on"):
        lines += [f"Builds on: {', '.join(epic['depends_on'])}", ""]
    if (epic.get("jira") or {}).get("url"):
        lines += [f"Jira epic: {epic['jira']['key']} ({epic['jira']['url']})", ""]
    return "\n".join(lines).strip() + "\n"


def jobs_dir(runtime: Path) -> Path:
    return runtime / "jobs"


def _job_link(epic: dict[str, Any]) -> list[dict[str, str]]:
    jira = epic.get("jira") or {}
    return [{"provider": "jira", "ref": jira["key"], "title": epic["name"], "url": jira.get("url", ""), "detail": ""}] if jira.get("key") else []


def make_backlog_job(runtime: Path, epic: dict[str, Any]) -> str:
    now = datetime.now()
    slug = re.sub(r"[^a-z0-9]+", "-", epic["name"].lower()).strip("-")[:30] or "epic"
    folder = jobs_dir(runtime)
    folder.mkdir(parents=True, exist_ok=True)
    job_id, n = f"{now:%Y%m%d-%H%M%S}-epic-{slug}", 2
    while (folder / f"{job_id}.json").exists():
        job_id, n = f"{now:%Y%m%d-%H%M%S}-epic-{slug}-{n}", n + 1
    job = {"job_id": job_id, "type": "epic", "title": epic["name"], "summary": epic.get("summary", ""), "status": "backlog",
           "epic": epic["id"], "feature": epic.get("feature") or None, "description": brief(epic), "external_links": _job_link(epic),
           "created_at": now.isoformat(timespec="seconds"), "updated_at": now.isoformat(timespec="seconds")}
    path = folder / f"{job_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return job_id


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def attach_job(runtime: Path, epic_id: str, job_id: str) -> None:
    """The epic was planned as `job_id`: that job is now the epic's, and the waiting one is removed."""
    data = load(runtime)
    try:
        epic = _find(data["epics"], epic_id)
    except EpicError:
        return
    old = epic.get("job")
    if old and old != job_id:
        path = jobs_dir(runtime) / f"{old}.json"
        if _read(path).get("status") == "backlog":
            path.unlink(missing_ok=True)
    epic["job"] = job_id
    save(runtime, data)
    _put_link(jobs_dir(runtime) / f"{job_id}.json", epic)  # the planned job carries the Jira link too, so its PR and merge are noted there


# --------------------------------------------------------------------------- Jira


def _put_link(path: Path, epic: dict[str, Any]) -> None:
    """Make sure the job file at `path` links to the epic's Jira epic (and to nothing else Jira under the same key twice)."""
    job = _read(path)
    wanted = _job_link(epic)
    if not job or not wanted:
        return
    links = [l for l in job.get("external_links") or [] if isinstance(l, dict) and not (l.get("provider") == "jira" and l.get("ref") == wanted[0]["ref"])]
    job["external_links"] = links + wanted
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def link_jira(runtime: Path, epic_id: str, key: str, url: str) -> None:
    """Record the Jira epic an epic is linked to, on the epic and on its job (so the job's merge updates reach the epic)."""
    if not JIRA_KEY.match(key):
        raise EpicError("That isn't a Jira key.")
    data = load(runtime)
    epic = _find(data["epics"], epic_id)
    epic["jira"] = {"key": key, "url": url}
    save(runtime, data)
    if epic.get("job"):
        _put_link(jobs_dir(runtime) / f"{epic['job']}.json", epic)


def import_issues(runtime: Path, issues: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Jira epics become approved epics here, each with a feature and a waiting job. Ones already imported are skipped."""
    data = load(runtime)
    have = {(e.get("jira") or {}).get("key") for e in data["epics"]}
    made = []
    for issue in issues:
        key = str(issue.get("key") or "")
        if not JIRA_KEY.match(key) or key in have:
            continue
        name = " ".join(str(issue.get("summary") or key).split())[:80] or key
        summary = " ".join(str(issue.get("description") or "").split())[:500]
        feature = store.create(runtime, name, summary)
        epic = {"id": "ep-" + secrets.token_hex(4), "name": name, "summary": summary, "requirements": [], "stories": [], "slice": "",
                "paths": [], "depends_on": [], "serves": "", "layer": 0, "status": "approved", "source": "jira", "feature": feature["id"],
                "job": "", "jira": {"key": key, "url": str(issue.get("url") or "")}, "created": time.time(), "approved_at": time.time()}
        epic["job"] = make_backlog_job(runtime, epic)
        data["epics"].append(epic)
        have.add(key)
        made.append(epic)
    if not made:
        raise EpicError("Those Jira epics are already here.")
    save(runtime, data)
    return made
