"""Tell linked tickets, cards and issues what happened to a job.

Called at two moments: a pull request was opened for the job, and the job's
pull request was merged. For each item the job is linked to (job["external_links"])
it can post a comment and, for a merge, move the item along (Jira transition,
Trello list, Sentry resolve). What happens is controlled per app in
settings.json under "integration_options"; see integrations.WRITEBACK_DEFAULTS.

Best effort by design: a failure is recorded on the job and never raised, so a
flaky Jira can't break a merge. Each (app, item, event, PR) is only done once.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from orchestrator import integrations as ig

EVENTS = ("pr_opened", "merged")


def _done_before(log: list[dict[str, Any]], provider: str, ref: str, event: str, key: str) -> bool:
    return any(e.get("ok") and e.get("provider") == provider and e.get("ref") == ref
               and e.get("event") == event and e.get("key") == key for e in log)


def _message(job: dict[str, Any], event: str, pr_url: str | None, pr_number: Any) -> str:
    title = job.get("title") or job.get("job_id") or "this job"
    pr = f"PR #{pr_number}" if pr_number else "a pull request"
    if event == "pr_opened":
        return f"Orchestrator opened {pr} for \"{title}\"" + (f": {pr_url}" if pr_url else ".")
    return f"Orchestrator merged {pr} for \"{title}\"" + (f": {pr_url}" if pr_url else ".")


def writeback(job: dict[str, Any], event: str, settings: dict[str, Any], *,
              pr_url: str | None = None, pr_number: Any = None) -> list[dict[str, Any]]:
    """Run the enabled actions; appends to and returns the new entries of job["integration_log"]."""
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}")
    links = [l for l in job.get("external_links") or [] if isinstance(l, dict)]
    saved = settings.get("integrations") if isinstance(settings.get("integrations"), dict) else {}
    options = settings.get("integration_options") if isinstance(settings.get("integration_options"), dict) else {}
    log = job.setdefault("integration_log", [])
    key = pr_url or str(pr_number or "")
    text = _message(job, event, pr_url, pr_number)
    new: list[dict[str, Any]] = []

    for link in links:
        provider_id, ref = link.get("provider", ""), link.get("ref", "")
        cls = ig.PROVIDERS.get(provider_id)
        if not cls or not cls.can_write or provider_id not in saved or not ref:
            continue
        opts = ig.writeback_options(options.get(provider_id))
        provider = cls(saved[provider_id])
        steps: list[tuple[str, Any]] = []
        if event == "pr_opened" and opts["comment_pr"]:
            steps.append(("comment", lambda p=provider, r=ref: p.comment(r, text)))
        if event == "merged":
            if opts["comment_merge"]:
                steps.append(("comment", lambda p=provider, r=ref: p.comment(r, text)))
            if opts["move_on_merge"]:
                steps.append(("move", lambda p=provider, r=ref, t=opts["target"]: p.move(r, t)))
        for action, run in steps:
            marker = f"{event}:{action}"
            if _done_before(log, provider_id, ref, marker, key):
                continue
            entry = {"t": datetime.now().isoformat(timespec="seconds"), "provider": provider_id, "ref": ref,
                     "event": marker, "key": key}
            try:
                entry.update(ok=True, message=run())
            except ig.IntegrationError as exc:
                entry.update(ok=False, message=str(exc))
            except Exception as exc:  # never let a sync problem break the job flow
                entry.update(ok=False, message=f"{type(exc).__name__}: {exc}")
            log.append(entry)
            new.append(entry)
    return new


def notify_job_event(job: dict[str, Any], event: str, settings_path: Path, job_path: Path | None = None, *,
                     pr_url: str | None = None, pr_number: Any = None) -> list[dict[str, Any]]:
    """Script-side entry point: reads settings.json, runs writeback, persists the log.
    `job` is updated in place so the caller's later writes keep the log."""
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        return []
    if not job.get("external_links") or not settings.get("integrations"):
        return []
    new = writeback(job, event, settings, pr_url=pr_url, pr_number=pr_number)
    if new and job_path is not None and job_path.exists():
        try:  # persist now, touching only the log, in case the caller doesn't write soon
            on_disk = json.loads(job_path.read_text(encoding="utf-8"))
            on_disk["integration_log"] = job["integration_log"]
            job_path.write_text(json.dumps(on_disk, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass
    for entry in new:
        print(f"      - {ig.PROVIDERS[entry['provider']].name}: {entry['message']}" + ("" if entry["ok"] else "  (not synced)"))
    return new
