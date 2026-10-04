"""Why a job can't start yet: the checks the scheduler would fail, found before anyone approves it.

Pure: takes the job, the machines config and the free disk space of this computer, returns a list of blockers,
each with plain words and where to fix it. It mirrors the scheduler's rules (allowed machines, allowed models a
machine can run, the free-space floor) so the page and the scheduler agree.
"""
from __future__ import annotations

import os
from typing import Any, Callable

# Minimum free space to start a build (Xcode-sized; lower it for small projects). The scheduler uses the same number.
MIN_DISK_GB = int(os.environ.get("ORCHESTRATOR_MIN_DISK_GB", "15"))


def equivalent_model_names(model_name: str) -> set[str]:
    from orchestrator.scripts.model_registry import get_model
    model = get_model(model_name)
    return {model.id, *model.aliases} if model else {model_name}


def model_names_overlap(left: str, right: str) -> bool:
    """Two model names that mean the same model (an id and its alias)."""
    return bool(equivalent_model_names(left) & equivalent_model_names(right))

SELF_CHECKOUT_TEXT = ("This project is the folder Orchestrator itself runs from. A job here would switch its branch and "
                      "change Orchestrator's own code while it runs. Run jobs on Orchestrator from a separate copy "
                      "(`git worktree add ../orchestrator-jobs`), or set the job to make no git changes.")


def runs_from(root: Any) -> bool:
    """Is `root` the checkout this Orchestrator package is running from? Jobs there would rewrite its own code."""
    from pathlib import Path
    import orchestrator
    try:
        return Path(orchestrator.__file__).resolve().parents[1] == Path(root).resolve()
    except (OSError, IndexError):
        return False


def switches_branches(job: dict[str, Any]) -> bool:
    return str(job.get("branch_mode") or "new") != "manual"


# Statuses where the job is about to be (or waiting to be) scheduled.
CHECKED_STATUSES = {"planned", "scheduled"}


def blockers(job: dict[str, Any], machines: list[dict[str, Any]], disk_free_gb: float | None, min_disk_gb: int,
             models_overlap: Callable[[str, str], bool]) -> list[dict[str, str]]:
    if job.get("status") not in CHECKED_STATUSES:
        return []
    enabled = [m for m in machines if m.get("enabled", True)]
    if not enabled:
        return [{"id": "no-machine", "text": "No machine is set up, so nothing can run this job.", "fix": "Add a machine", "route": "#/config/fleet"}]

    allowed_names = job.get("allowed_machines") or []
    usable = [m for m in enabled if not allowed_names or m.get("name") in allowed_names]
    if not usable:
        return [{"id": "machine-not-allowed", "text": f"This job is limited to {', '.join(allowed_names)}, which isn't set up or is switched off.",
                 "fix": "Open Machines", "route": "#/config/fleet"}]

    allowed_models = job.get("allowed_models") or []
    if allowed_models:
        usable = [m for m in usable if any(models_overlap(a, mm) for a in allowed_models for mm in m.get("models", []))]
        if not usable:
            return [{"id": "model-not-on-machine", "text": "None of the models this job may use is enabled on a machine that can run it.",
                     "fix": "Choose models", "route": "#/config/models"}]
    elif not any(m.get("models") for m in usable):
        return [{"id": "no-model", "text": "No model is selected on any machine, so nothing can plan or build.", "fix": "Choose models", "route": "#/config/models"}]

    found = []
    local = [m for m in usable if m.get("execution_mode", "local") == "local"]
    if disk_free_gb is not None and local and len(local) == len(usable) and disk_free_gb < min_disk_gb:
        found.append({"id": "low-disk", "text": f"Only {disk_free_gb:.0f} GB is free on this computer and builds want {min_disk_gb} GB. Free some space, or lower the limit with ORCHESTRATOR_MIN_DISK_GB if this project builds small.",
                      "fix": "", "route": ""})
    return found
