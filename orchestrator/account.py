"""This computer's link to an Orchestrator account: pairing (`orchestrator connect`), the heartbeat that tells the
hosted app where to reach it, and checking the one-time tickets the hosted app signs people in with.

The control plane (cloud/functions/control_plane.py) only learns the computer's name, OS, version and web address.
Code, model subscriptions and integration tokens never leave the computer.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import os
import platform
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from orchestrator.project_config import user_state_dir

FIREBASE_PROJECT_ID = "swift-orch-web-20260923"
HOSTED_APP_URL = f"https://{FIREBASE_PROJECT_ID}.web.app"
HOSTED_ORIGINS = (HOSTED_APP_URL, f"https://{FIREBASE_PROJECT_ID}.firebaseapp.com")
CONTROL_URL_ENV = "ORCHESTRATOR_CONTROL_URL"
HEARTBEAT_SECONDS = 60
# What this computer's server can do for the hosted app. Bumped when the hosted page starts relying on something older
# computers lack; the page compares it with the one it needs (REQUIRED_RUNNER_API in account.js) and says to update.
API_VERSION = 4
CAPABILITIES = {"preview_remote": True}
INSTALL_SPEC = "git+https://github.com/leemosupreemo/orchestrator.git@web-ui"  # what the install command on the hosted app installs


class AccountError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def control_url() -> str:
    return (os.environ.get(CONTROL_URL_ENV) or f"{HOSTED_APP_URL}/cp").rstrip("/")


def machine_path() -> Path:
    return user_state_dir() / "machine.json"


def load_machine() -> dict[str, Any] | None:
    try:
        data = json.loads(machine_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("machine_id") and data.get("machine_secret"):
        return data
    return None


def save_machine(data: dict[str, Any]) -> None:
    path = machine_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def forget_machine() -> None:
    try:
        machine_path().unlink()
    except FileNotFoundError:
        pass


def default_name() -> str:
    name = socket.gethostname().split(".")[0]
    return name.replace("-", " ") if name else "My computer"


def os_label() -> str:
    system = platform.system()
    return {"Darwin": "macOS"}.get(system, system) + (f" {platform.mac_ver()[0]}" if system == "Darwin" and platform.mac_ver()[0] else "")


def package_version() -> str:
    try:
        from importlib.metadata import version
        return version("orchestrator")
    except Exception:
        return "dev"


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def call(path: str, body: dict[str, Any] | None = None, machine: dict[str, Any] | None = None, timeout: float = 15) -> dict[str, Any]:
    """POST JSON to the control plane. Raises AccountError with the control plane's message and status."""
    headers = {"Content-Type": "application/json"}
    if machine:
        headers["Authorization"] = f"Machine {machine['machine_id']}:{machine['machine_secret']}"
    req = urllib.request.Request(f"{control_url()}/{path.lstrip('/')}", data=json.dumps(body or {}).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error") or exc.reason
        except Exception:
            message = str(exc.reason)
        raise AccountError(str(message), exc.code)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise AccountError(f"Couldn't reach Orchestrator's servers ({getattr(exc, 'reason', exc)}).")


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def start_pairing(name: str | None = None) -> dict:
    name = name or default_name()
    return {**call("pair/start", {"name": name, "os": os_label(), "version": package_version()}), "name": name}


def poll_pairing(pairing: dict) -> dict:
    return call("pair/poll", {"code": pairing["code"], "poll_secret": pairing["poll_secret"]})


def finish_pairing(pairing: dict, claimed: dict) -> dict:
    if claimed.get("status") != "claimed":
        raise AccountError("Pairing hasn't completed.")
    machine = {"machine_id": claimed["machine_id"], "machine_secret": claimed["machine_secret"],
               "owner_email": str(claimed.get("owner_email") or "").lower(), "name": pairing["name"],
               "control_url": control_url(), "paired_at": time.time()}
    save_machine(machine)
    return machine


def connect(name: str | None = None, out: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
            poll_every: float = 3.0) -> dict[str, Any]:
    """Pair this computer with an account: show a code, wait for someone signed in to confirm it, keep the secret."""
    started = start_pairing(name)
    code = started["code"]
    out("")
    out(f"  Your code: {format_code(code)}")
    out(f"  Open {HOSTED_APP_URL}/#/connect?code={code} and sign in to add this computer to your account.")
    out(f"  (Or sign in at {HOSTED_APP_URL} and enter the code.) It expires in {started['expires_in'] // 60} minutes.")
    out("")
    deadline = time.time() + started["expires_in"] + 30
    while time.time() < deadline:
        sleep(poll_every)
        try:
            result = poll_pairing(started)
        except AccountError as exc:
            if exc.status in (404, 410):
                raise
            continue  # a dropped connection: keep waiting
        if result.get("status") == "claimed":
            return finish_pairing(started, result)
    raise AccountError("The pairing code expired. Run `orchestrator connect` again.", 410)


def valid_connection_command(command: Any, now: float | None = None) -> bool:
    """A delayed heartbeat cannot execute expired or unbounded provider actions."""
    if not isinstance(command, dict) or command.get("action") not in ("repair", "setup_funnel"):
        return False
    command_id = command.get("id")
    if not isinstance(command_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", command_id):
        return False
    created, expires = command.get("created"), command.get("expires")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in (created, expires)):
        return False
    now = time.time() if now is None else now
    return 0 <= created <= now + 30 and now < expires and 0 < expires - created <= 900


def heartbeat(machine: dict[str, Any], endpoint: str, running: int = 0, update_state: str | None = None,
              readiness: dict[str, str] | None = None, connection: dict[str, Any] | None = None,
              connection_ack: str | None = None, update_control: int = 0, update_ack: str | None = None,
              state_heads: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Tell the control plane this computer is up, where browsers reach it, and how many runs are going (so it can say
    so if the computer disappears mid-run). The app also reports its update progress, and the reply may ask it to update.
    Forgets the pairing if the account removed it."""
    body = {"endpoint": endpoint, "version": package_version(), "os": os_label(), "api_version": API_VERSION,
            "running": running, "packaged": bool(os.environ.get("ORCHESTRATOR_PACKAGED_APP")),
            "capabilities": dict(CAPABILITIES),
            "update_control": 1 if update_control == 1 and os.environ.get("ORCHESTRATOR_PACKAGED_APP") else 0}
    if update_ack:
        body["update_ack"] = update_ack
    if update_state:
        body["update_state"] = update_state
    if readiness:
        body["readiness"] = readiness
    body["connection"] = connection if connection is not None else {}
    if connection_ack:
        body["connection_ack"] = connection_ack
    if state_heads:
        body["state_heads"] = state_heads  # which shared-state commit this computer last pushed, per project (docs/state-sync.md)
    try:
        return call("machine/heartbeat", body, machine=machine)
    except AccountError as exc:
        if exc.status == 410:
            forget_machine()
        raise


def report_update(machine: dict[str, Any], operation_id: str, state: str) -> dict[str, Any]:
    return call("machine/update-progress", {"operation_id": operation_id, "state": state}, machine=machine)


def send_events(machine: dict[str, Any], events: list[dict[str, Any]], project: str) -> None:
    """Push what the UI server noticed to the owner's phones and browsers: [{key, title, body, path}] (notifier.Tracker)."""
    if events:
        call("machine/events", {"events": [{"key": e["key"], "title": f"{e['title']} · {project}", "body": e["body"],
                                            "path": e["path"]} for e in events]}, machine=machine)


def _normalize_repo(value: str | None) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    if text.endswith(".git"):
        text = text[:-4]
    if ":" in text and "@" in text.split(":", 1)[0]:
        text = text.split(":", 1)[1]
    elif "://" in text:
        text = text.split("://", 1)[1].split("/", 1)[-1]
    parts = [p for p in text.split("/") if p]
    return "/".join(parts[-2:]).lower() if len(parts) >= 2 else text.lower()


def _project_json_remote(root: Path) -> str:
    try:
        return str(json.loads((root / ".orchestrator" / "project.json").read_text(encoding="utf-8")).get("git_remote") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _origin_url(root: Path) -> str:
    try:
        res = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, capture_output=True, text=True, timeout=2)
        return res.stdout if res.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


_origin_repo_cache: dict[tuple[str, int], str] = {}


def project_repo(root: Path, github_repo: str | None = None) -> str:
    """The normalized `owner/name` for a project on any git host, so computers can tell whether they hold the same repo.
    Order: the GitHub repo already detected, project.json's git_remote, then the origin URL. The origin lookup runs
    git, and this is called on every state poll, so its answer is kept until project.json or .git/config changes."""
    repo = _normalize_repo(github_repo) or _normalize_repo(_project_json_remote(root))
    if repo:
        return repo
    try:
        stamp = (str(root), (root / ".git" / "config").stat().st_mtime_ns)
    except OSError:  # a linked worktree or no repo: nothing cheap to key on, so ask git each time
        return _normalize_repo(_origin_url(root))
    if stamp not in _origin_repo_cache:
        if len(_origin_repo_cache) >= 256:  # every .git/config edit makes a new key; old ones are never asked for again
            _origin_repo_cache.clear()
        _origin_repo_cache[stamp] = _normalize_repo(_origin_url(root))
    return _origin_repo_cache[stamp]


# Archiving and restoring travel between the owner's computers: each sends its recent ones with its snapshot, the account
# keeps the newest per job, and every computer applies them to its own copy (server.apply_job_archives). Without this, a job
# copied to another computer (a project download, Continue here) comes back there after being archived here.
ARCHIVE_WINDOW = 30 * 86400  # older archives have long since been seen everywhere
MAX_ARCHIVE_EVENTS = 200


def _stamp(value: Any) -> float:
    """An epoch time from a job field: a number, or an ISO date (completed_at); 0 when there's none."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value:
        from datetime import datetime
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def job_archive_events(roots: list[Path], now: float | None = None) -> dict[str, dict[str, Any]]:
    """{job id: {archived, at}} for jobs put away (the archive folder) or brought back (restored_at) lately, newest first."""
    now = time.time() if now is None else now
    events: dict[str, dict[str, Any]] = {}
    for root in roots:
        jobs = root / ".orchestrator" / "jobs"
        candidates = [(p, True) for p in (jobs / "archive").glob("*.json")] + [(p, False) for p in jobs.glob("*.json")]
        for path, archived in candidates:
            if now - path.stat().st_mtime > ARCHIVE_WINDOW and not archived:
                continue  # an untouched active job has no recent restore
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(job, dict):
                continue
            at = _stamp(job.get("archived_at")) or _stamp(job.get("completed_at")) if archived else _stamp(job.get("restored_at"))
            if not at or now - at > ARCHIVE_WINDOW:
                continue
            if at > float((events.get(path.stem) or {}).get("at") or 0):
                events[path.stem] = {"archived": archived, "at": round(at, 3)}
    newest = sorted(events.items(), key=lambda kv: kv[1]["at"], reverse=True)[:MAX_ARCHIVE_EVENTS]
    return dict(newest)


def job_epoch(job: dict[str, Any]) -> int:
    """A job's ownership number: Continue here raises it, and the copy with the highest one owns the job (the account's
    job_owners; a lower copy is told it moved). Jobs from before the number: made here 1, continued here 2, and a copy an
    old project download brought over (it names where it came from but was never continued) 0, so it never wins."""
    value = job.get("owner_epoch")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if job.get("imported_at"):
        return 2
    return 0 if job.get("imported_from_machine") else 1


def _snapshot_job_progress(jdata: dict[str, Any]) -> list[int]:
    done = jdata.get("completed_task_indices") or jdata.get("completed_tasks") or []
    return sorted({i for i in done if isinstance(i, int)}) if isinstance(done, list) else []


def _job_state(job: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """What the job page on this computer says the job is waiting on (label, reason, next step), so another computer shows
    the same instead of guessing from the bare status."""
    try:
        from orchestrator.web.server import job_state
        state = job_state(job, root)
    except Exception:
        return None
    nxt = state.get("next") or None
    return {"group": state.get("group"), "tone": state.get("tone"), "label": state.get("label"),
            "reason": str(state.get("reason") or "")[:600],
            "next": {"action": nxt.get("action"), "label": nxt.get("label")} if isinstance(nxt, dict) else None}


# The rest of an unfinished job's record, beyond the summary fields: with these, Continue here on another computer picks the
# job up with its history (answers, fix attempts, models, task commits) rather than a summary. Trimmed to stay small.
RECORD_FIELDS = ("clarification_history", "debug_history", "allowed_models", "allowed_machines", "planner", "builder", "reviewer",
                 "task_commits", "iteration", "max_iterations", "debug_phase", "debug_pause_reason", "feature", "epic",
                 "test_command_override", "ai_modified_files", "ai_untracked_files", "wip_backup")
MAX_RECORD_BYTES = 30_000
PLAN_FIELDS = ("risks", "constraints", "assumptions", "likely_files", "acceptance_criteria", "test_cases")


def _trimmed(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, list):
        return [_trimmed(v, depth + 1) for v in value[-10:]] if depth < 4 else []
    if isinstance(value, dict):
        return {str(k)[:80]: _trimmed(v, depth + 1) for k, v in list(value.items())[:40]} if depth < 4 else {}
    return value if isinstance(value, (int, float, bool)) or value is None else str(value)[:200]


def job_record(job: dict[str, Any]) -> dict[str, Any] | None:
    """The backup of an unfinished job's record carried in the snapshot (None for a finished job)."""
    if job.get("status") in ("completed", "archived"):
        return None
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    record = {k: _trimmed(job[k]) for k in RECORD_FIELDS if job.get(k) not in (None, [], {}, "")}
    extra = {k: _trimmed(plan[k]) for k in PLAN_FIELDS if plan.get(k) not in (None, [], {}, "")}
    if extra:
        record["plan_extra"] = extra
    # The whole snapshot is one database document (1 MB at most, up to 100 jobs): the bulkiest history goes first.
    for bulky in ("debug_history", "plan_extra", "clarification_history", "ai_modified_files", "ai_untracked_files"):
        if len(json.dumps(record, default=str)) <= MAX_RECORD_BYTES:
            break
        record.pop(bulky, None)
    return record or None


def _job_entry(jdata: dict[str, Any], stem: str, proj_root: Path, repo: str | None,
               live: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """What the account keeps of one job: its summary, state, and (for work still to do) its whole plan and history."""
    plan_obj = jdata.get("plan") or {}
    tasks_raw = plan_obj.get("tasks") or []
    done_indices = _snapshot_job_progress(jdata)
    verification = jdata.get("verification")
    if not isinstance(verification, dict):
        verification = {}
    return {
        "job_id": jdata.get("job_id"),
        "project": proj_root.name,
        "repo": repo,
        "title": jdata.get("title") or plan_obj.get("title") or jdata.get("job_id"),
        "type": jdata.get("type", "generic"),
        "status": jdata.get("status", "planned"),
        "branch": jdata.get("branch"),
        "base_branch": jdata.get("base_branch"),
        "branch_mode": jdata.get("branch_mode"),
        "last_error": jdata.get("last_error"),
        "yolo": bool(jdata.get("yolo")),
        "requirements": jdata.get("requirements"),
        "created_at": jdata.get("created_at"),
        "updated_at": jdata.get("updated_at"),
        "issue_number": jdata.get("issue_number"),
        "pr_number": jdata.get("pr_number"),
        "summary": plan_obj.get("summary") or "",
        "code_host": jdata.get("code_host"),
        "approved": bool(jdata.get("approved")),
        "human_clarification_question": jdata.get("human_clarification_question"),
        "test_status": jdata.get("test_status"),
        "verification": {"status": verification.get("status"),
                         "comments": str(verification.get("comments") or "")[:2000]} if verification else None,
        "state": _job_state(jdata, proj_root),
        "active_run": (live or {}).get(stem) or (live or {}).get(str(jdata.get("job_id") or "")),
        "blocker": {k: jdata["blocker"].get(k) for k in ("kind", "title", "detail")} if isinstance(jdata.get("blocker"), dict) else None,
        "imported_from_machine": jdata.get("imported_from_machine"),
        "imported_at": jdata.get("imported_at"),
        "record": job_record(jdata),
        "moved_to": jdata.get("moved_to"),
        "owner_epoch": job_epoch(jdata),
        "tasks_count": len(tasks_raw),
        "tasks_done": len(done_indices),
        "completed_task_indices": done_indices,
        # The whole plan travels for work still to do, so "Continue on this computer" resumes with
        # every task; a finished job only needs its counts.
        "tasks": [] if jdata.get("status") == "completed" else [
            {k: v[:4000] if isinstance(v, str) else v for k, v in t.items()}  # very long text trimmed
            for t in tasks_raw if isinstance(t, dict)],
    }


# Every job, one record each on the account (control plane account_jobs), so another computer can pick up any of them while
# this one is offline, not only the newest 20 the snapshot carries. Only the jobs that changed since the last send go, so a
# send stays small; what was sent is remembered here and only after the account took it.
MAX_JOB_UPDATES = 60


def _jobs_sent_file() -> Path:
    return user_state_dir() / "account_jobs_sent.json"


def _entry_hash(entry: dict[str, Any]) -> str:
    stable = {k: v for k, v in entry.items() if k != "active_run"}  # a live run's step changes by the second
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()[:20]


def _jobs_sent() -> dict[str, str]:
    try:
        data = json.loads(_jobs_sent_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def job_updates(roots: list[Path], repo_by_root: dict[str, str | None] | None = None,
                live: dict[str, dict[str, Any]] | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """The jobs whose record changed since the account last took them: every job still in a project, and those put away
    in the last 30 days (so the account's copy says archived). Oldest change first is fine: the rest go with the next send."""
    now = time.time() if now is None else now
    sent = _jobs_sent()
    out: list[dict[str, Any]] = []
    for root in roots:
        repo = project_repo(root, (repo_by_root or {}).get(str(root)))
        jobs = root / ".orchestrator" / "jobs"
        paths = sorted(jobs.glob("*.json"), reverse=True) + [p for p in sorted((jobs / "archive").glob("*.json"), reverse=True)
                                                              if now - p.stat().st_mtime < ARCHIVE_WINDOW]
        for path in paths:
            if re.search(r"_task_\d+$", path.stem):  # scratch copy the worker makes per task; not a job
                continue
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(job, dict) or not job.get("job_id"):
                continue
            entry = _job_entry(job, path.stem, root, repo, live)
            entry["job_id"] = path.stem  # the id every computer uses for it (its file name)
            digest = _entry_hash(entry)
            if sent.get(path.stem) != digest:
                out.append({**entry, "_digest": digest})
            if len(out) >= MAX_JOB_UPDATES:
                return out
    return out


def mark_jobs_sent(updates: list[dict[str, Any]]) -> None:
    """The account took these: don't send them again until they change."""
    if not updates:
        return
    sent = _jobs_sent()
    sent.update({u["job_id"]: u["_digest"] for u in updates if u.get("job_id") and u.get("_digest")})
    try:
        _jobs_sent_file().parent.mkdir(parents=True, exist_ok=True)
        _jobs_sent_file().write_text(json.dumps(sent), encoding="utf-8")
    except OSError:
        pass


def build_snapshot(root: Path | None = None, live: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Assemble lightweight offline view of recent projects and jobs for the hosted app. `live`: job id -> its run going now
    ({"label": "Fix attempt", "step": "..."}), so other computers see progress, not only the status."""
    from orchestrator.project_config import find_project_root, load_recent_projects

    recent_data = load_recent_projects(prune_missing=False)
    repo_by_root: dict[str, str | None] = {}
    projects_list = []
    jobs_list = []
    roots_to_check = []
    if root:
        roots_to_check.append(root)
    for p in recent_data.get("projects", []):
        r_str = p.get("root")
        if r_str:
            r_path = Path(r_str).expanduser()
            repo_by_root[str(r_path)] = p.get("github_repo")
            if r_path not in roots_to_check and r_path.is_dir():
                roots_to_check.append(r_path)

    if not roots_to_check:
        try:
            curr_root = find_project_root()
            if curr_root.is_dir():
                roots_to_check.append(curr_root)
        except Exception:
            pass

    for proj_root in roots_to_check[:10]:
        repo = project_repo(proj_root, repo_by_root.get(str(proj_root)))
        jobs_dir = proj_root / ".orchestrator" / "jobs"
        proj_jobs = []
        if jobs_dir.is_dir():
            for jpath in sorted(jobs_dir.glob("*.json"), reverse=True)[:20]:
                if re.search(r"_task_\d+$", jpath.stem):  # scratch copy the worker makes per task; not a job
                    continue
                try:
                    jdata = json.loads(jpath.read_text(encoding="utf-8"))
                    if isinstance(jdata, dict) and jdata.get("job_id"):
                        proj_jobs.append(_job_entry(jdata, jpath.stem, proj_root, repo, live))
                except Exception:
                    continue
        latest_job = proj_jobs[0] if proj_jobs else None
        projects_list.append({
            "name": proj_root.name,
            "root": str(proj_root),
            "repo": repo,
            "jobs_count": len(proj_jobs),
            "latest_job": latest_job,
        })
        jobs_list.extend(proj_jobs)

    jobs_list.sort(key=lambda j: str(j.get("updated_at") or j.get("created_at") or ""), reverse=True)
    return {
        "projects": projects_list,
        "recent_jobs": jobs_list[:50],
        "job_archives": job_archive_events(roots_to_check[:10]),
        "job_updates": job_updates(roots_to_check[:10], repo_by_root, live),
        "metadata": {"os": os_label(), "version": package_version()},
    }


def sync_snapshot(machine: dict[str, Any], snapshot: dict[str, Any] | None = None, root: Path | None = None,
                  live: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Sync offline project & job metadata to the control plane for browsing when this computer is offline."""
    payload = snapshot if snapshot is not None else build_snapshot(root=root, live=live)
    reply = call("machine/sync", payload, machine=machine)
    mark_jobs_sent(payload.get("job_updates") or [])
    return reply


def disconnect() -> bool:
    """Remove this computer from its account (best effort online) and forget the pairing here."""
    machine = load_machine()
    if machine is None:
        return False
    try:
        call("machine/leave", machine=machine)
    except AccountError as exc:
        if exc.status not in (401, 410):
            forget_machine()
            raise AccountError(f"Forgot the pairing here, but couldn't tell the account ({exc}). Remove it in the web app too.")
    forget_machine()
    return True


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_ticket(ticket: str, machine: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """The claims of a ticket the control plane signed for this computer, or AccountError. Replay is the caller's job (nonce)."""
    try:
        payload, signature = ticket.split(".", 1)
        expected = hmac.new(machine["machine_secret"].encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(signature), expected):
            raise ValueError("signature")
        claims = json.loads(_unb64(payload))
    except (ValueError, UnicodeError, json.JSONDecodeError, KeyError):
        raise AccountError("That sign-in link isn't valid for this computer.", 401)
    if claims.get("v") != 1 or claims.get("mid") != machine["machine_id"]:
        raise AccountError("That sign-in link is for a different computer.", 401)
    if float(claims.get("exp", 0)) < (time.time() if now is None else now):
        raise AccountError("That sign-in link expired. Sign in again.", 401)
    if not claims.get("email") or not claims.get("nonce"):
        raise AccountError("That sign-in link isn't valid for this computer.", 401)
    return claims
