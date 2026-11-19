"""Loose MECE (Mutually Exclusive, Collectively Exhaustive) coordination for parallel jobs.

1. Mutually Exclusive (ME) — Soft Lanes:
   - `load_active_peers` / `lane_context` tell planners and builders what active peers own.
   - `classify_launch_overlap` tiers `parallel_jobs.decide()` into `hard` (auto-wait),
     `soft` (run in parallel with an advisory), or `disjoint`.
   - `peer_file_map` feeds active peer files into `scope_check.evaluate()` to flag mid-run drift.

2. Collectively Exhaustive (CE) — Scoped Sibling Coverage:
   - For splintered parent jobs and Features with requirements, maps each parent requirement
     to sibling jobs and `deferred` items, quietly surfacing gaps or duplicate claims.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from orchestrator import job_overlap

INACTIVE_STATUSES = {"completed", "archived", "discarded", "decomposed"}
RUNNING_STATUSES = {"executing", "debugging", "verifying", "planning", "in_progress"}


def _clean_text(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _clip(text: Any, limit: int = 140) -> str:
    cleaned = _clean_text(text)
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


def _is_inactive(job: dict[str, Any]) -> bool:
    return str(job.get("status") or "") in INACTIVE_STATUSES or bool(job.get("archived")) or bool(job.get("moved_to"))


def _is_running(job: dict[str, Any]) -> bool:
    from orchestrator.parallel_jobs import _alive

    pid = job.get("worker_pid")
    if isinstance(pid, int) and pid > 0:
        return _alive(job) and not job.get("waiting_for")
    return str(job.get("status") or "") in RUNNING_STATUSES and not job.get("waiting_for")


def _is_open_peer(job_id: str, job: dict[str, Any], current_job_id: str | None = None) -> bool:
    if not isinstance(job, dict) or _is_inactive(job):
        return False
    if current_job_id and (
        job_id == current_job_id
        or job_id.startswith(f"{current_job_id}_task")
        or current_job_id.startswith(f"{job_id}_task")
    ):
        return False
    return True


def _directories(files: set[str]) -> set[str]:
    return {p for f in files if (p := str(PurePosixPath(f).parent)) and p != "."}


def _job_summary_text(job: dict[str, Any]) -> str:
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    return _clip(plan.get("summary") or job.get("raw_input") or job.get("title") or "")


def _same_group(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    return bool(
        (a.get("feature") and a.get("feature") == b.get("feature"))
        or (a.get("parent_job_id") and a.get("parent_job_id") == b.get("parent_job_id"))
    )


def active_peers(
    jobs: list[tuple[str, dict[str, Any]]],
    current_job_id: str | None = None,
    current_job: dict[str, Any] | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Collect active/open peer jobs so planners and builders know which lanes are claimed."""
    peers: list[dict[str, Any]] = []
    for peer_id, peer in jobs:
        if not _is_open_peer(peer_id, peer, current_job_id):
            continue
        files = sorted(job_overlap.planned_files(peer))
        title = _clip(peer.get("title") or peer_id, 90)
        summary = _job_summary_text(peer)
        if not (files or title or summary):
            continue
        peers.append({
            "id": peer_id,
            "title": title,
            "status": str(peer.get("status") or ""),
            "running": _is_running(peer),
            "same_group": _same_group(current_job, peer),
            "summary": summary,
            "files": files,
            "_updated": str(peer.get("updated_at") or peer.get("created_at") or ""),
        })
    peers.sort(key=lambda p: p["_updated"], reverse=True)
    peers.sort(key=lambda p: (not p["running"], not p["same_group"], not bool(p["files"])))
    for p in peers:
        p.pop("_updated", None)
    return peers[: max(0, limit)]


def load_active_peers(
    jobs_dir: Path,
    current_job_id: str | None = None,
    current_job: dict[str, Any] | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Read job files in `jobs_dir` and return active peer lanes (fail-safe on unreadable files)."""
    if not jobs_dir or not jobs_dir.is_dir():
        return []
    loaded: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            loaded.append((path.stem, data))
    return active_peers(loaded, current_job_id=current_job_id, current_job=current_job, limit=limit)


def lane_context(peers: list[dict[str, Any]], role: str = "builder") -> str:
    """Compact MECE soft-lane block for planner or builder prompts; empty when no peers exist."""
    if not peers:
        return ""
    is_planner = role == "planner"
    lines = [
        "### ACTIVE PARALLEL WORK (SOFT MECE LANES)",
        (
            "Other jobs are currently open or building in this repository. Keep this plan mutually "
            "exclusive with their claimed files and responsibilities where practical:"
            if is_planner
            else "Other jobs are currently open or building alongside this one. Stay inside your own lane:"
        ),
    ]
    file_label = "Owned files" if is_planner else "Hands-off files"
    for p in peers:
        state = "running" if p.get("running") else (p.get("status") or "open")
        files = p.get("files") or []
        shown = ", ".join(files[:8]) + (f", +{len(files) - 8} more" if len(files) > 8 else "")
        detail = f" — {p['summary']}" if p.get("summary") and p["summary"].lower() != str(p.get("title") or "").lower() else ""
        lane = f" | {file_label}: {shown}" if shown else ""
        lines.append(f'- [{state}] "{p["title"]}" ({p["id"]}){detail}{lane}')

    lines.append("Guidelines:")
    if is_planner:
        lines.extend([
            "- Avoid overlapping their owned files or duplicating helpers/logic they are already building.",
            "- If you must touch a shared integration boundary (routes, exports, manifest, schema), keep the touchpoint minimal and note it in `assumptions` or `risks`.",
            "- If this job is a sibling under the same feature or parent plan, stay strictly inside your assigned acceptance criteria and do not absorb a sibling's scope.",
        ])
    else:
        lines.extend([
            "- Do not refactor, clean up, or opportunistically edit files owned by an active peer job.",
            "- If you must touch a shared integration file (e.g., route table, exports, manifest), keep the edit strictly additive and minimal.",
            "- Do not duplicate work a sibling job is already building.",
        ])
    return "\n".join(lines) + "\n"


def classify_launch_overlap(
    job_id: str,
    job: dict[str, Any],
    running: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Classify overlap between `job` and running jobs into `hard`, `soft`, or `disjoint`."""
    mine = job_overlap.planned_files(job)
    my_dirs = _directories(mine)
    my_title = str(job.get("title") or "")
    my_words = job_overlap.words(f"{my_title} {_job_summary_text(job)}")
    advisories: list[dict[str, Any]] = []

    for other_id, other in running:
        if not isinstance(other, dict) or other_id == job_id:
            continue
        other_title = str(other.get("title") or other_id)
        theirs = job_overlap.planned_files(other)

        shared = sorted(mine & theirs)
        if shared:
            return {
                "tier": "hard",
                "wait_reason": f"It changes the same files as “{other_title}” ({', '.join(shared[:2])}), so it waits for that job.",
                "advisories": [],
            }

        if not mine:
            blind_reason = (
                f"“{other_title}” is also running without a file list" if not theirs else
                f"belongs to the same work as “{other_title}”" if _same_group(job, other) else
                f"looks closely related to “{other_title}”" if my_title and job_overlap.similarity(my_title, other_title) > 0 else
                ""
            )
            if blind_reason:
                return {
                    "tier": "hard",
                    "wait_reason": f"It has no planned files yet and {blind_reason}, so it waits to avoid overlapping changes.",
                    "advisories": [],
                }

        shared_dirs = sorted(my_dirs & _directories(theirs))
        same_group = _same_group(job, other)
        other_words = job_overlap.words(f"{other_title} {_job_summary_text(other)}")
        shared_words = sorted(my_words & job_overlap._aligned(my_words, other_words)) if (my_words and other_words) else []

        reasons = []
        if shared_dirs:
            reasons.append(f"edits nearby files in {', '.join(shared_dirs[:2])}")
        if same_group:
            reasons.append("sibling job in the same feature/plan")
        if len(shared_words) >= 2 and not reasons:
            reasons.append(f"related topic ({', '.join(shared_words[:3])})")

        if reasons:
            advisories.append({
                "peer_id": other_id,
                "peer_title": other_title,
                "shared_dirs": shared_dirs[:5],
                "shared_words": shared_words[:6],
                "same_group": same_group,
                "reason": "; ".join(reasons),
            })

    return {"tier": "soft" if advisories else "disjoint", "wait_reason": "", "advisories": advisories}


def peer_file_map(
    job_id: str,
    job: dict[str, Any],
    others: list[tuple[str, dict[str, Any]]],
) -> dict[str, list[str]]:
    """Map each file claimed or modified by active peer jobs -> list of peer job titles."""
    if _is_inactive(job):
        return {}
    my_branch = str(job.get("branch") or "")
    owners: dict[str, list[str]] = {}
    for other_id, other in others:
        if not _is_open_peer(other_id, other, job_id):
            continue
        if my_branch and str(other.get("branch") or "") == my_branch:
            continue
        label = str(other.get("title") or other_id)
        for path in sorted(job_overlap.planned_files(other)):
            bucket = owners.setdefault(path, [])
            if label not in bucket:
                bucket.append(label)
    return owners


# ---- Collectively Exhaustive (CE) & Duplicate Ownership across Sibling Jobs ----

def _extract_text_list(items: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(items, list):
        return out
    for item in items:
        if isinstance(item, list):
            for sub in _extract_text_list(item):
                if sub.lower() not in seen:
                    seen.add(sub.lower())
                    out.append(sub)
            continue
        raw = (item.get("text") or item.get("title") or item.get("description") or item.get("criterion") or "") if isinstance(item, dict) else item
        text = _clean_text(raw)
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out


def _job_corpus_items(job: dict[str, Any]) -> list[str]:
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    items = [t for raw in (job.get("title"), plan.get("title"), plan.get("summary"), job.get("raw_input")) if (t := _clean_text(raw))]
    items.extend(_extract_text_list(plan.get("acceptance_criteria")))
    for task in plan.get("tasks") or []:
        if isinstance(task, dict):
            items.extend(t for field in ("title", "description") if (t := _clean_text(task.get(field))))
            items.extend(_extract_text_list(task.get("acceptance_criteria")))
    return items


def _norm_alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _requirement_matches_items(requirement: str, items: list[str]) -> bool:
    req_norm = _norm_alnum(requirement)
    if not req_norm or not items:
        return False
    req_words = job_overlap.words(requirement)

    for item in items:
        item_norm = _norm_alnum(item)
        if not item_norm:
            continue
        if req_norm in item_norm or (len(item_norm) >= 12 and item_norm in req_norm):
            return True
        if not req_words:
            continue
        shared = req_words & job_overlap._aligned(req_words, job_overlap.words(item))
        if (len(req_words) == 1 and shared) or (len(shared) >= 2 and (len(shared) / len(req_words)) >= 0.6):
            return True

    if len(req_words) >= 2:
        combined_words = set().union(*(job_overlap.words(item) for item in items))
        shared = req_words & job_overlap._aligned(req_words, combined_words)
        min_ratio = 1.0 if len(req_words) == 2 else 0.75
        return len(shared) >= 2 and (len(shared) / len(req_words)) >= min_ratio
    return False


def evaluate_sibling_coverage(
    requirements: list[str],
    siblings: list[tuple[str, dict[str, Any]]],
    *,
    scope_kind: str,
    scope_id: str,
    deferred: list[str] | None = None,
) -> dict[str, Any]:
    """Check whether sibling jobs collectively cover parent requirements without silent gaps or overlap."""
    reqs = _extract_text_list(requirements)
    active_siblings = [
        (sid, sjob)
        for sid, sjob in siblings
        if isinstance(sjob, dict)
        and str(sjob.get("status") or "") not in {"discarded", "archived"}
        and not sjob.get("archived")
        and not sjob.get("moved_to")
    ]
    raw_deferred: list[Any] = list(deferred or [])
    for _, sjob in active_siblings:
        plan = sjob.get("plan") if isinstance(sjob.get("plan"), dict) else {}
        if isinstance(plan.get("deferred"), list):
            raw_deferred.extend(plan["deferred"])
    deferred_notes = _extract_text_list(raw_deferred)

    corpora = {sid: _job_corpus_items(sjob) for sid, sjob in active_siblings}
    covered, overlapping, deferred_hits, gaps = [], [], [], []

    for req in reqs:
        matched = [
            {"id": sid, "title": str(sjob.get("title") or sid), "status": (st := str(sjob.get("status") or "")), "done": st == "completed"}
            for sid, sjob in active_siblings
            if _requirement_matches_items(req, corpora[sid])
        ]
        if len(matched) == 1:
            covered.append({"requirement": req, "jobs": matched})
        elif len(matched) >= 2:
            overlapping.append({"requirement": req, "jobs": matched})
        elif (def_note := next((n for n in deferred_notes if _requirement_matches_items(req, [n])), None)):
            deferred_hits.append({"requirement": req, "deferred_note": def_note})
        else:
            gaps.append(req)

    parts = []
    if gaps:
        parts.append(f"{len(gaps)} requirement{'s' if len(gaps) != 1 else ''} not covered by any sibling job yet")
    if overlapping:
        parts.append(f"{len(overlapping)} requirement{'s' if len(overlapping) != 1 else ''} claimed by multiple sibling jobs")

    return {
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "total_requirements": len(reqs),
        "sibling_count": len(active_siblings),
        "covered": covered,
        "overlapping": overlapping,
        "deferred": deferred_hits,
        "gaps": gaps,
        "mece_ok": not gaps and not overlapping,
        "summary": "; ".join(parts) + "." if parts else "",
    }


def evaluate_parent_coverage(
    parent_id: str,
    parent_job: dict[str, Any],
    jobs: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any] | None:
    """Evaluate CE/ME coverage of a splintered parent job's acceptance criteria across its sub-jobs."""
    plan = parent_job.get("plan") if isinstance(parent_job.get("plan"), dict) else {}
    raw_reqs: list[Any] = list(plan.get("acceptance_criteria") or [])
    for task in plan.get("tasks") or []:
        if isinstance(task, dict) and isinstance(task.get("acceptance_criteria"), list):
            raw_reqs.extend(task["acceptance_criteria"])
    reqs = _extract_text_list(raw_reqs)
    if not reqs:
        return None
    sub_ids = set(parent_job.get("subtask_job_ids") or [])
    siblings = [
        (jid, j) for jid, j in jobs
        if jid != parent_id and isinstance(j, dict) and (j.get("parent_job_id") == parent_id or jid in sub_ids)
    ]
    if not siblings:
        return None
    return evaluate_sibling_coverage(
        reqs, siblings, scope_kind="parent_job", scope_id=parent_id,
        deferred=plan.get("deferred") if isinstance(plan.get("deferred"), list) else [],
    )


def evaluate_feature_coverage(
    feature: dict[str, Any],
    jobs: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any] | None:
    """Evaluate CE/ME coverage of a Feature's requirements across its attached jobs."""
    fid = str(feature.get("id") or "")
    reqs = _extract_text_list(feature.get("requirements"))
    if not fid or not reqs:
        return None
    siblings = [(jid, j) for jid, j in jobs if isinstance(j, dict) and j.get("feature") == fid]
    return evaluate_sibling_coverage(reqs, siblings, scope_kind="feature", scope_id=fid) if siblings else None


def job_mece_summary(
    job_id: str,
    job: dict[str, Any],
    jobs: list[tuple[str, dict[str, Any]]],
    features: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Combined loose-MECE summary for a job: soft parallel advisories + scoped sibling CE coverage."""
    running_peers = [(jid, j) for jid, j in jobs if jid != job_id and isinstance(j, dict) and _is_running(j)]
    launch = classify_launch_overlap(job_id, job, running_peers)
    advisories = launch["advisories"]

    coverage: dict[str, Any] | None = None
    by_id = dict(jobs)
    if job.get("subtask_job_ids") or job.get("status") == "decomposed":
        coverage = evaluate_parent_coverage(job_id, job, jobs)
    elif job.get("parent_job_id") and job["parent_job_id"] in by_id:
        coverage = evaluate_parent_coverage(job["parent_job_id"], by_id[job["parent_job_id"]], jobs)
    elif job.get("feature") and features:
        if (feat := next((f for f in features if isinstance(f, dict) and f.get("id") == job["feature"]), None)):
            coverage = evaluate_feature_coverage(feat, jobs)

    if not advisories and not coverage:
        return None
    return {"tier": launch["tier"], "advisories": advisories, "coverage": coverage}
