"""Jobs shared through the project's own repository (refs/orchestrator/state), phase 2 of docs/state-sync.md.

Each computer writes its jobs to the state ref as `jobs/<id>.json`: the same entry the account receives today
(`account._job_entry`: summary, state, plan and, for work still to do, its history), plus which computer wrote it. In phase 2
the account still receives them too, so computers on older versions keep working; the web app reads this copy first.

Rules, the same ones the control plane uses:
- **Ownership.** The copy with the higher ownership number (`owner_epoch`, raised by Continue here) wins; on a tie, the newer one.
  A computer never overwrites a winning copy, and when another computer's copy of one of its jobs wins, its own is marked
  moved (`apply_moved_jobs`), as the heartbeat's `moved_jobs` does.
- **Archive and restore.** `job-events.json` keeps the newest event per job; each computer applies the ones newer than its own
  (`apply_job_archives`), as the heartbeat's `job_archives` does.

On by default; the person can stop sharing this project's jobs separately from its product requirements (jobs carry their
answers and plans).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from orchestrator import account, state_ref, sync_health

CHECK_SECONDS = 60
JOBS_DIR = "jobs/"
EVENTS = "job-events.json"
IGNORED_IN_COMPARISON = ("updated_at", "machine", "active_run", "state")   # change by the minute without saying anything new


def _canonical(entry: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in entry.items() if k not in IGNORED_IN_COMPARISON}, sort_keys=True, default=str)


def _rank(entry: dict[str, Any]) -> tuple[int, str]:
    return (account.job_epoch(entry), str(entry.get("updated_at") or ""))


def local_entries(root: Path, name: str, now: float | None = None) -> dict[str, dict[str, Any]]:
    """This computer's jobs as they would be shared: active ones, and those archived in the last 30 days."""
    now = time.time() if now is None else now
    jobs = Path(root) / ".orchestrator" / "jobs"
    paths = sorted(jobs.glob("*.json")) + [p for p in sorted((jobs / "archive").glob("*.json")) if now - p.stat().st_mtime < account.ARCHIVE_WINDOW]
    repo = account.project_repo(Path(root))
    out: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(job, dict) or not job.get("job_id"):
            continue
        entry = account._job_entry(job, path.stem, Path(root), repo, None)
        entry.pop("active_run", None)
        entry.update(job_id=path.stem, machine=name)
        out[path.stem] = entry
    return out


def _remote_entry(root: Path, head: str | None, job_id: str) -> dict[str, Any] | None:
    raw = state_ref.read(root, head, f"{JOBS_DIR}{job_id}.json")
    try:
        data = json.loads(raw) if raw is not None else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _remote_events(root: Path, head: str | None) -> dict[str, dict[str, Any]]:
    raw = state_ref.read(root, head, EVENTS)
    try:
        data = json.loads(raw) if raw is not None else {}
    except ValueError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _merged_events(mine: dict[str, dict[str, Any]], theirs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = dict(theirs)
    for job_id, event in mine.items():
        if float(event.get("at") or 0) > float((out.get(job_id) or {}).get("at") or 0):
            out[job_id] = event
    return out


def sync_project(root: Path, *, name: str, running: set[str] | None = None) -> dict[str, Any]:
    """One round for one project: push this computer's winning copies and newer events, apply other computers' moves and
    archives here. Returns {"outcome": off | no remote | idle | pushed | busy, "moved": [...], "archived": [...]}."""
    root = Path(root)
    if not state_ref.sharing(root, "jobs"):
        return {"outcome": "off", "moved": [], "archived": []}
    if not state_ref.remote_url(root):
        return {"outcome": "no remote", "moved": [], "archived": []}
    from orchestrator.web.server import apply_job_archives, apply_moved_jobs  # the same steps the heartbeat's replies take
    moved: list[str] = []
    archived: list[str] = []
    for _ in range(3):
        head = state_ref.fetch(root)
        mine = local_entries(root, name)
        changes: dict[str, bytes | None] = {}
        moves: dict[str, dict[str, Any]] = {}
        for job_id, entry in mine.items():
            theirs = _remote_entry(root, head, job_id)
            if theirs is None or (_rank(entry) >= _rank(theirs) and _canonical(entry) != _canonical(theirs)
                                  and (_rank(entry) > _rank(theirs) or theirs.get("machine") == name)):
                changes[f"{JOBS_DIR}{job_id}.json"] = json.dumps(entry, sort_keys=True, default=str).encode("utf-8")
            elif account.job_epoch(theirs) > account.job_epoch(entry) and theirs.get("machine") not in ("", name) and not entry.get("moved_to"):
                moves[job_id] = {"to": theirs["machine"], "epoch": account.job_epoch(theirs)}
        events = _merged_events(account.job_archive_events([root]), _remote_events(root, head))
        if events != _remote_events(root, head):
            changes[EVENTS] = json.dumps(events, sort_keys=True).encode("utf-8")
        if moves:
            moved += apply_moved_jobs(moves, roots=[root])
        archived += apply_job_archives(events, roots=[root], running=running or set())
        if not changes:
            return {"outcome": "idle", "moved": moved, "archived": archived}
        sha = state_ref.commit(root, head, changes, "Jobs", name)
        if state_ref.push(root, sha):
            return {"outcome": "pushed", "moved": moved, "archived": archived}
    return {"outcome": "busy", "moved": moved, "archived": archived}


def shared_jobs(root: Path, name: str) -> dict[str, Any]:
    """Other computers' jobs for this project, from the last fetched shared state, for the web app: {"jobs": [...],
    "synced_at": when that state was written}. Nothing when this project's jobs aren't shared."""
    root = Path(root)
    if not state_ref.sharing(root, "jobs"):
        return {"jobs": [], "synced_at": 0}
    head = state_ref.fetched(root)
    out = []
    for path in state_ref.files(root, head, JOBS_DIR):
        entry = _remote_entry(root, head, path.removeprefix(JOBS_DIR).removesuffix(".json"))
        if entry and entry.get("machine") and entry.get("machine") != name:
            out.append(entry)
    return {"jobs": out, "synced_at": state_ref.committed_at(root, head)}


def run_round(root: Path | None, running: set[str] | None = None) -> int:
    from orchestrator import product_sync
    name = product_sync.computer_name()
    results = []
    for path in product_sync.project_roots(root):
        try:
            result = sync_project(path, name=name, running=running)
            results.append(result["outcome"])
            if result["moved"] or result["archived"]:
                print(f"  From another computer: moved {', '.join(result['moved']) or 'none'}; archived or restored "
                      f"{', '.join(result['archived']) or 'none'}", flush=True)
        except Exception as exc:  # offline, no rights: that project waits, the others go on
            results.append(f"error: {exc}")
    failed = [r.removeprefix("error: ") for r in results if r.startswith("error:")]
    if any(r not in ("off", "no remote") for r in results):
        sync_health.record("shared_jobs", not failed, failed[0] if failed else "")
    return CHECK_SECONDS
