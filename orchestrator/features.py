"""Features: the product-level things jobs add up to.

A feature groups jobs, owns a slice of the codebase (`paths`), and has a status
that is a statement about now, not forever: "complete" records when it was last
judged done and reopens the moment new work is attached. Stored in
`<runtime>/features.json`; jobs point at a feature with `job["feature"]`.

Overlap is what keeps features discrete: two features claiming the same paths,
or two in-flight jobs on different features touching the same file.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import time
from pathlib import Path, PurePosixPath
from typing import Any

from orchestrator import analytics

STATUSES = ("planned", "in-progress", "complete")
DONE_GROUPS = {"done"}


class FeatureError(ValueError):
    pass


def store_path(runtime: Path) -> Path:
    return runtime / "features.json"


def load(runtime: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(store_path(runtime).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [f for f in data.get("features", []) if isinstance(f, dict) and f.get("id")] if isinstance(data, dict) else []


def save(runtime: Path, features: list[dict[str, Any]]) -> None:
    path = store_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"features": features}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]


def _paths(raw: Any) -> list[str]:
    items = raw.splitlines() if isinstance(raw, str) else raw if isinstance(raw, list) else []
    out = []
    for item in items:
        p = str(item).strip().lstrip("./")
        if p and p not in out:
            out.append(p)
    return out


def _deps(raw: Any, features: list[dict[str, Any]], self_id: str | None) -> list[str]:
    ids = raw.splitlines() if isinstance(raw, str) else raw if isinstance(raw, list) else []
    known = {f["id"] for f in features}
    out = []
    for item in ids:
        d = str(item).strip()
        if not d or d in out:
            continue
        if d == self_id:
            raise FeatureError("A feature can't depend on itself")
        if d not in known:
            raise FeatureError(f"Unknown feature: {d}")
        out.append(d)
    return out


def _would_cycle(features: list[dict[str, Any]], feature_id: str, deps: list[str]) -> bool:
    """True if making `feature_id` depend on `deps` lets a dependency lead back to it."""
    graph = {f["id"]: list(f.get("depends_on") or []) for f in features}
    graph[feature_id] = deps
    seen, stack = set(), list(deps)
    while stack:
        node = stack.pop()
        if node == feature_id:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(graph.get(node, []))
    return False


def layers(features: list[dict[str, Any]]) -> dict[str, int]:
    """Depth of each feature: 0 has no dependencies, otherwise 1 + its deepest dependency.
    Dependencies on missing features are ignored."""
    graph = {f["id"]: [d for d in f.get("depends_on") or [] if d != f["id"]] for f in features}
    depth: dict[str, int] = {}

    def visit(node: str, trail: frozenset[str]) -> int:
        if node in depth:
            return depth[node]
        deps = [d for d in graph[node] if d in graph and d not in trail]  # `trail` guards hand-edited cycles
        depth[node] = 1 + max((visit(d, trail | {node}) for d in deps), default=-1)
        return depth[node]

    for node in graph:
        visit(node, frozenset())
    return depth


def get(features: list[dict[str, Any]], feature_id: str) -> dict[str, Any]:
    for f in features:
        if f["id"] == feature_id:
            return f
    raise FeatureError("Feature not found")


def create(runtime: Path, name: str, summary: str = "", paths: Any = None, depends_on: Any = None) -> dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise FeatureError("A feature needs a name")
    if len(name) > 80:
        raise FeatureError("Keep the name under 80 characters")
    features = load(runtime)
    base = _slug(name) or "feature"
    taken = {f["id"] for f in features}
    fid, n = base, 2
    while fid in taken:
        fid, n = f"{base}-{n}", n + 1
    feature = {"id": fid, "name": name, "summary": (summary or "").strip()[:500], "paths": _paths(paths),
               "depends_on": _deps(depends_on, features, None), "status": "planned", "created": time.time(), "completed_at": None, "reopened": False}
    save(runtime, features + [feature])
    return feature


def update(runtime: Path, feature_id: str, **fields: Any) -> dict[str, Any]:
    features = load(runtime)
    feature = get(features, feature_id)
    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if not name:
            raise FeatureError("A feature needs a name")
        feature["name"] = name[:80]
    if "summary" in fields:
        feature["summary"] = str(fields["summary"] or "").strip()[:500]
    if "paths" in fields:
        feature["paths"] = _paths(fields["paths"])
    if "depends_on" in fields:
        deps = _deps(fields["depends_on"], features, feature_id)
        if _would_cycle(features, feature_id, deps):
            raise FeatureError("That would make features depend on each other in a circle")
        feature["depends_on"] = deps
    save(runtime, features)
    return feature


def set_status(runtime: Path, feature_id: str, status: str) -> dict[str, Any]:
    if status not in STATUSES:
        raise FeatureError(f"Status must be one of: {', '.join(STATUSES)}")
    features = load(runtime)
    feature = get(features, feature_id)
    feature["status"] = status
    if status == "complete":
        feature["completed_at"], feature["reopened"] = time.time(), False
    save(runtime, features)
    return feature


def delete(runtime: Path, feature_id: str) -> None:
    features = load(runtime)
    get(features, feature_id)
    save(runtime, [{**f, "depends_on": [d for d in f.get("depends_on") or [] if d != feature_id]}
                   for f in features if f["id"] != feature_id])


def note_work_attached(runtime: Path, feature_id: str, job_group: str) -> None:
    """A job landing on a feature: planned -> in-progress; complete -> reopened (in-progress)."""
    features = load(runtime)
    feature = get(features, feature_id)
    if job_group in DONE_GROUPS:
        return
    if feature["status"] == "complete":
        feature["status"], feature["reopened"] = "in-progress", True
    elif feature["status"] == "planned":
        feature["status"] = "in-progress"
    save(runtime, features)


def _kpi_op(runtime: Path, feature_id: str, op) -> dict[str, Any]:
    features = load(runtime)
    feature = get(features, feature_id)
    try:
        result = op(feature)
    except analytics.AnalyticsError as exc:
        raise FeatureError(str(exc))
    save(runtime, features)
    return result


def kpi_add(runtime: Path, feature_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    return _kpi_op(runtime, feature_id, lambda f: analytics.add_kpi(f, raw))


def kpi_update(runtime: Path, feature_id: str, kpi_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    def op(f: dict[str, Any]) -> dict[str, Any]:
        old = analytics.find_kpi(f, kpi_id)
        f["kpis"] = [analytics.clean_kpi(raw, old) if k["id"] == kpi_id else k for k in f["kpis"]]
        return analytics.find_kpi(f, kpi_id)
    return _kpi_op(runtime, feature_id, op)


def kpi_delete(runtime: Path, feature_id: str, kpi_id: str) -> None:
    def op(f: dict[str, Any]) -> None:
        analytics.find_kpi(f, kpi_id)
        f["kpis"] = [k for k in f["kpis"] if k["id"] != kpi_id]
    _kpi_op(runtime, feature_id, op)


def kpi_measure(runtime: Path, feature_id: str, kpi_id: str, value: Any, note: str = "", decision: str = "") -> dict[str, Any]:
    return _kpi_op(runtime, feature_id, lambda f: analytics.log_measurement(analytics.find_kpi(f, kpi_id), value, note, decision))


def rollup(features: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each feature with its job counts. `jobs` are web job summaries (need `feature`, `state`)."""
    out = []
    depth = layers(features)
    status = {f["id"]: f.get("status") for f in features}
    for f in features:
        mine = [j for j in jobs if j.get("feature") == f["id"]]
        deps = [d for d in f.get("depends_on") or [] if d in status]
        groups = [(j.get("state") or {}).get("group") for j in mine]
        waiting = [] if f.get("status") == "complete" else [d for d in deps if status[d] != "complete"]
        kpis = [{**k, "status": analytics.status(k)} for k in f.get("kpis") or []]
        out.append({**f, "kpis": kpis, "depends_on": deps, "layer": depth[f["id"]], "waiting_on": waiting, "jobs_total": len(mine), "jobs_done": groups.count("done"),
                    "jobs_working": groups.count("working"), "jobs_need_you": groups.count("needs_you"),
                    "job_ids": [j["id"] for j in mine]})
    return out


def _covers(a: str, b: str) -> bool:
    """True if path pattern `a` and `b` could name the same files."""
    if a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    pa, pb = PurePosixPath(a.rstrip("/*")), PurePosixPath(b.rstrip("/*"))
    return pa == pb or pa in pb.parents or pb in pa.parents


def overlaps(features: list[dict[str, Any]], jobs: list[dict[str, Any]],
             job_files: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
    """Pairs of features that step on each other, with why.

    `job_files` maps job id -> files that job touches (only in-flight jobs count)."""
    found: dict[tuple[str, str], list[str]] = {}

    def add(a: str, b: str, reason: str) -> None:
        key = tuple(sorted((a, b)))
        if reason not in found.setdefault(key, []):
            found[key].append(reason)

    for i, a in enumerate(features):
        for b in features[i + 1:]:
            for pa in a.get("paths", []):
                for pb in b.get("paths", []):
                    if _covers(pa, pb):
                        add(a["id"], b["id"], f"both claim {pa if pa == pb else f'{pa} and {pb}'}")
    by_file: dict[str, set[str]] = {}
    for j in jobs:
        if (j.get("state") or {}).get("group") == "done" or not j.get("feature"):
            continue
        for path in (job_files or {}).get(j["id"], []):
            by_file.setdefault(path, set()).add(j["feature"])
    for path, owners in by_file.items():
        ids = sorted(owners)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                add(a, b, f"in-flight jobs both change {path}")
    names = {f["id"]: f["name"] for f in features}
    return [{"features": list(key), "names": [names.get(k, k) for k in key], "reasons": reasons}
            for key, reasons in sorted(found.items())]
