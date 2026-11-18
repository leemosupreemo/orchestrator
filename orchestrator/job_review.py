"""Final human sign-off and delivery evidence for a job's current revision.

Reading this state never starts a build, uploads anything, or changes a job.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def source_revision(root: Path, job: dict[str, Any]) -> str:
    branch = str(job.get("branch") or "")
    if not branch or branch.startswith("-"):
        return ""
    try:
        result = subprocess.run(["git", "rev-parse", "--verify", f"{branch}^{{commit}}"], cwd=root,
                                capture_output=True, text=True, timeout=5)
        value = result.stdout.strip()
        return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", value) else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
        return value if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username else None
    except ValueError:
        return None


def source_changes(root: Path, job: dict[str, Any]) -> str:
    """Fingerprint uncommitted code only in the checkout that actually holds this job's branch."""
    checkout = Path(job.get("worktree") or root)
    try:
        branch = subprocess.run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=checkout,
                                capture_output=True, text=True, timeout=5)
        if branch.returncode != 0 or branch.stdout.strip() != job.get("branch"):
            return ""
        diff = subprocess.run(["git", "diff", "--binary", "HEAD", "--", ".", ":(exclude).orchestrator"],
                              cwd=checkout, capture_output=True, timeout=5)
        return hashlib.sha256(diff.stdout).hexdigest() if diff.stdout else ""
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def _owned_files(root: Path, job: dict[str, Any]) -> dict[str, str]:
    checkout = Path(job.get("worktree") or root).resolve()
    names = set(job.get("ai_modified_files") or []) | set(job.get("ai_untracked_files") or []) | set(job.get("output_digests") or {})
    values = {}
    for name in sorted(str(n) for n in names):
        path = (checkout / name).resolve()
        if not path.is_relative_to(checkout):
            continue
        try:
            with path.open("rb") as stream:
                values[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError:
            values[name] = "missing"
    return values


def _unexpired(record: dict[str, Any], *, require_expiry=False) -> bool:
    try:
        if record.get("expires"):
            expires = datetime.fromisoformat(str(record["expires"]).replace("Z", "+00:00"))
        elif require_expiry:
            built = datetime.fromisoformat(str(record.get("built_at") or "").replace("Z", "+00:00"))
            expires = built.astimezone() + timedelta(days=7)
        else:
            return True
        return expires.tzinfo is not None and expires > datetime.now(timezone.utc)
    except ValueError:
        return False


def _matches_commit(record: dict[str, Any], commit: str) -> bool:
    saved = str(record.get("commit") or "")
    return bool(commit and re.fullmatch(r"[0-9a-f]{7,64}", saved) and commit.startswith(saved))


def _preview_current(record: dict[str, Any], job: dict[str, Any], commit: str) -> bool:
    if record.get("branch") != job.get("branch") or not _matches_commit(record, commit) or not _safe_url(record.get("url")):
        return False
    return _unexpired(record)


def review_state(root: Path, job: dict[str, Any]) -> dict[str, Any]:
    runtime = root / ".orchestrator"
    config = _read(runtime / "project.json")
    commit = source_revision(root, job)
    # Non-git records still need a stable review token, so an intervening job update invalidates the dialog.
    fingerprint = {k: job.get(k) for k in ("job_id", "branch", "status", "updated_at", "plan", "completed_tasks", "completed_task_indices")}
    dirty = source_changes(root, job)
    applicable = job.get("output_scope") != "tests-only" and job.get("type") not in {
        "design", "feature-design", "coverage", "test-audit", "bug-investigate",
    }
    configured = bool(config.get("firebase_distribution") or config.get("firebase_app_id")
                      or config.get("delivery_build_command") or config.get("delivery_artifact")
                      or config.get("delivery_provider") or config.get("distribution_script_path") or config.get("firebase_plist_path")
                      or config.get("delivery_platform") == "web" and (root / "firebase.json").is_file())
    required = applicable and configured
    delivery = {"status": "unavailable", "required": required, "reason": "App delivery is not configured for this project.",
                "url": None, "version": None, "build": None, "built_at": None}
    if not applicable:
        delivery["reason"] = "This job produces documents, tests, or an investigation rather than an app build."
    elif configured:
        delivery.update(status="pending", reason="Deliver this job's current build before approving it.")
    job_id = str(job.get("job_id") or "")
    receipt = _read(runtime / "output" / "delivery" / f"{job_id}.json") if job_id and Path(job_id).name == job_id else {}
    if applicable and receipt:
        if receipt.get("status") == "delivered":
            if (receipt.get("job_id") == job_id and receipt.get("branch") == job.get("branch") and _matches_commit(receipt, commit)
                    and _unexpired(receipt, require_expiry=receipt.get("platform") == "web")):
                delivery.update(status="delivered", reason="This job's current build was delivered for review.",
                                url=_safe_url(receipt.get("preview_url")) or _safe_url(receipt.get("console_url")),
                                version=receipt.get("version"), build=receipt.get("build"), built_at=receipt.get("built_at"))
            elif configured:
                delivery.update(status="stale", reason="The saved delivery does not match this job's current code. Deliver it again.")
        elif configured and receipt.get("status") == "failed":
            delivery.update(status="failed", reason="The last delivery attempt failed. Retry delivery before approving.")
        elif configured and receipt.get("status") == "delivering":
            delivery.update(status="pending", reason="Delivery has not yet confirmed that this build reached its destination.")
    # Existing hosted web previews are also reviewable evidence when they identify this branch and commit.
    # A failed explicit delivery retry stays failed; an older preview cannot conceal that failure.
    if applicable and not receipt and delivery["status"] not in {"delivered", "failed"}:
        for path in sorted((runtime / "output" / "previews").glob("*.json")):
            preview = _read(path)
            if _preview_current(preview, job, commit):
                delivery.update(status="delivered", reason="A preview of this job's current code is available for review.",
                                url=_safe_url(preview.get("url")), built_at=preview.get("at"))
                break
    if required and dirty and delivery["status"] == "delivered":
        delivery.update(status="stale", reason="This job has uncommitted changes. Commit and deliver the updated code before approving.")
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    tasks = plan.get("tasks") or job.get("tasks") or []
    completed = job.get("completed_tasks") or job.get("completed_task_indices") or []
    finished = len({int(i) for i in completed if str(i).isdigit() and 0 <= int(i) < len(tasks)}) if isinstance(completed, list) else 0
    ready = job.get("status") == "review-needed" and (not tasks or finished >= len(tasks))
    can_approve = ready and (not required or delivery["status"] == "delivered")
    reason = ("Review the work, then explicitly approve completion." if can_approve else
              delivery["reason"] if ready else "The job must finish its tasks and verification before final approval.")
    revision = hashlib.sha256(json.dumps({"commit": commit, "job": fingerprint, "files": _owned_files(root, job),
                                         "changes": dirty, "delivery": delivery}, sort_keys=True, default=str).encode()).hexdigest()
    return {"ready": ready, "can_approve": can_approve, "reason": reason, "revision": revision,
            "approval": job.get("manual_review") if isinstance(job.get("manual_review"), dict) else None,
            "delivery": delivery}


def approve(root: Path, job: dict[str, Any], *, reviewed: bool, expected_revision: str | None = None,
            reviewer: str = "CLI") -> dict[str, Any]:
    if reviewed is not True:
        raise ValueError("Confirm that you reviewed the work before approving completion.")
    state = review_state(root, job)
    if expected_revision is not None and expected_revision != state["revision"]:
        raise ValueError("The job changed since you opened review. Reopen it and review the current code.")
    if not state["can_approve"]:
        raise ValueError(state["reason"])
    return {"reviewed_by": reviewer, "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "revision": state["revision"], "commit": source_revision(root, job), "delivery": state["delivery"]}


def project_root(job_path: Path) -> Path:
    """The primary project owns .orchestrator/jobs, even for jobs building in a separate checkout."""
    parent = job_path.resolve().parent
    if parent.name == "archive":
        parent = parent.parent
    return parent.parent.parent if parent.name == "jobs" and parent.parent.name == ".orchestrator" else parent
