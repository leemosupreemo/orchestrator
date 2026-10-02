#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from common import CONFIG_DIR, ROOT, now_iso, read_json, run, write_json, print_phase
from model_registry import get_model
from probe_machine import load_machines, probe_machine
from worker_tools import remote_env_prefix, remote_import_check_command


RESOURCE_PROFILES = {
    "reasoning",
    "implementation",
    "build_test",
    "review",
    "heavy_build",
    "light_fix",
}


def load_machines() -> list[dict[str, Any]]:
    config = read_json(CONFIG_DIR / "machines.json")
    return [machine for machine in config["machines"] if machine.get("enabled", True)]


def load_job(job_path: Path) -> dict[str, Any]:
    return read_json(job_path)


def synthetic_group_for_job(job: dict[str, Any]) -> dict[str, Any]:
    job_type = job.get("type", "feature-plan")
    if job_type == "feature-plan" and not job.get("approved"):
        kind = "planning"
        resource_profile = "reasoning"
    elif job_type == "bug-investigate":
        kind = "investigation"
        resource_profile = "reasoning"
    elif job_type == "quick-fix":
        kind = "implementation"
        resource_profile = "implementation"
    else:
        kind = "implementation"
        resource_profile = "implementation"

    title = job.get("title", "Untitled Job")
    return {
        "group_id": "MAIN",
        "title": title,
        "kind": kind,
        "resource_profile": resource_profile,
        "preferred_roles": ["worker"],
        "preferred_models": [job.get("builder", "gemini-3.1-pro-preview")],
        "depends_on": [],
        "tasks": [title],
        "status": "pending",
        "assigned_machine": None,
        "assigned_model": None,
        "branch": job.get("branch"),
        "worktree_path": None,
        "artifacts": {},
        "synthetic": True,
    }


def normalize_job(job: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(job)
    if "type" not in normalized:
        if normalized.get("source") == "manual" or "repro_steps" in normalized.get("plan", {}):
            normalized["type"] = "bug-fix"
        elif normalized.get("source") == "quick" or "quick" in normalized.get("job_id", ""):
            normalized["type"] = "quick-fix"
        else:
            normalized["type"] = "feature-plan"

    if not normalized.get("task_groups"):
        normalized["task_groups"] = [synthetic_group_for_job(normalized)]
    normalized.setdefault("dispatch_history", [])

    builder = normalized.get("builder", "gemini-3.1-pro-preview")
    for group in normalized["task_groups"]:
        group.setdefault("depends_on", [])
        group.setdefault("preferred_roles", ["worker"])
        group.setdefault("preferred_models", [builder])
        group.setdefault("status", "pending")
        group.setdefault("assigned_machine", None)
        group.setdefault("assigned_model", None)
        group.setdefault("branch", None)
        group.setdefault("worktree_path", None)
        group.setdefault("tasks", [])
        group.setdefault("artifacts", {})
        profile = group.get("resource_profile", "implementation")
        if profile not in RESOURCE_PROFILES:
            raise ValueError(f"Unsupported resource profile: {profile}")

    return normalized


def group_index(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {group["group_id"]: group for group in job["task_groups"]}


def ready_groups(job: dict[str, Any]) -> list[dict[str, Any]]:
    indexed = group_index(job)
    ready: list[dict[str, Any]] = []
    for group in job["task_groups"]:
        if group["status"] not in {"pending", "scheduled"}:
            continue
        if all(indexed[dependency]["status"] == "completed" for dependency in group.get("depends_on", [])):
            ready.append(group)
    return ready


def equivalent_model_names(model_name: str) -> set[str]:
    model = get_model(model_name)
    if not model:
        return {model_name}
    return {model.id, *model.aliases}


def model_names_overlap(left: str, right: str) -> bool:
    return bool(equivalent_model_names(left).intersection(equivalent_model_names(right)))


def machine_supports_model(machine: dict[str, Any], candidate: str) -> bool:
    return any(model_names_overlap(candidate, machine_model) for machine_model in machine.get("models", []))


def model_allowed(candidate: str, allowed_models: list[str] | None) -> bool:
    if not allowed_models:
        return True
    return any(model_names_overlap(candidate, allowed_model) for allowed_model in allowed_models)


def compatible_model(candidate: str, machine: dict[str, Any], allowed_models: list[str] | None) -> bool:
    return machine_supports_model(machine, candidate) and model_allowed(candidate, allowed_models)


def choose_model(group: dict[str, Any], machine: dict[str, Any], job: dict[str, Any]) -> str:
    allowed = job.get("allowed_models")

    for candidate in group.get("preferred_models", []):
        if compatible_model(candidate, machine, allowed):
            return candidate

    fallback_by_profile = {
        "reasoning": job.get("planner"),
        "review": job.get("reviewer"),
    }
    fallback = fallback_by_profile.get(group["resource_profile"], job.get("builder"))
    if fallback and compatible_model(fallback, machine, allowed):
        return fallback

    builder = job.get("builder")
    if builder and compatible_model(builder, machine, allowed):
        return builder

    if allowed:
        for candidate in allowed:
            if machine_supports_model(machine, candidate):
                return candidate
    else:
        machine_models = machine.get("models", [])
        if machine_models:
            return machine_models[0]

    machine_models_text = ", ".join(machine.get("models", [])) or "none"
    allowed_text = ", ".join(allowed) if allowed else "unrestricted"
    raise RuntimeError(
        f"No compatible models found on machine {machine['name']} "
        f"(machine models: {machine_models_text}; allowed models: {allowed_text})."
    )


def format_ineligible_machines(ineligible: list[tuple[dict[str, Any], str]]) -> str:
    if not ineligible:
        return ""
    lines = ["Ineligible machines:"]
    for machine, reason in ineligible:
        lines.append(f"  - {machine['name']}: {reason}")
    return "\n".join(lines)


STICKINESS_BONUS = 150.0  # Strong preference for remaining on previous machine to preserve state
MIN_DISK_GB = int(os.environ.get("ORCHESTRATOR_MIN_DISK_GB", "15"))  # Minimum required to start a build (Xcode-sized; lower it for small projects)
LOW_DISK_THRESHOLD = 30 # Threshold for applying penalties

def score_machine(machine: dict[str, Any], probe: dict[str, Any], group: dict[str, Any], previously_assigned: str | None = None) -> tuple[float, list[str]]:
    if not probe.get("reachable"):
        return -9999, ["Machine unreachable"]
    if not probe.get("repo_exists"):
        return -9999, ["Repo path missing on machine"]

    base_priority = float(machine.get("priority", 0))
    components = [f"{base_priority:+.1f} (base priority)"]
    score = base_priority
    
    # 0. Stickiness Bonus (Resuming)
    if previously_assigned and machine["name"] == previously_assigned:
        score += STICKINESS_BONUS
        components.append(f"{STICKINESS_BONUS:+.1f} (stickiness bonus: previously assigned)")

    profile = group["resource_profile"]
    tags = set(machine.get("tags", []))
    hw = probe.get("hw", {})
    disk_free = probe.get("disk_free_gb", 0)

    # 1. Disk Space Safety Check
    if profile in {"heavy_build", "build_test", "implementation"} and disk_free < MIN_DISK_GB:
        return -9999, [f"Insufficient disk space ({disk_free}GB free, need {MIN_DISK_GB}GB)"]

    # 2. Disk Space Penalty
    if disk_free < LOW_DISK_THRESHOLD:
        penalty = (LOW_DISK_THRESHOLD - disk_free) * 5
        score -= penalty
        components.append(f"-{penalty:.1f} (low disk space: {disk_free}GB)")

    # 3. Compute Power Bonus (Cores)
    logical_cores = hw.get("logical_cores", 4)
    if profile in {"heavy_build", "build_test", "implementation"}:
        # Favor machines with more cores for parallel compilation
        core_bonus = (logical_cores - 4) * 10
        if core_bonus != 0:
            score += core_bonus
            components.append(f"{core_bonus:+.1f} (compute capacity: {logical_cores} cores)")
    
    # 4. Processor Architecture Bonus
    cpu_model = hw.get("cpu_model", "").upper()
    if "M1" in cpu_model or "M2" in cpu_model or "M3" in cpu_model:
        score += 20
        components.append("+20.0 (Apple Silicon bonus)")
        if "PRO" in cpu_model or "MAX" in cpu_model or "ULTRA" in cpu_model:
            score += 30
            components.append("+30.0 (Pro/Max/Ultra tier bonus)")

    # 5. Memory & Current Load
    if profile in {"heavy_build", "build_test", "implementation"} and machine.get("supports_xcode"):
        score += 40
        components.append("+40.0 (xcode build capability)")
    if profile in {"reasoning", "review"} and "interactive" in tags:
        score += 20
        components.append("+20.0 (interactive reasoning bonus)")

    mem_bonus = min(probe.get("mem_free_mb", 0) / 1000, 20)
    score += mem_bonus
    components.append(f"{mem_bonus:+.1f} (free memory bonus)")
    
    if probe.get("active_ai_jobs", 0) > 0:
        penalty = probe["active_ai_jobs"] * 25
        score -= penalty
        components.append(f"-{penalty:.1f} ({probe['active_ai_jobs']} active AI jobs)")
        
    if probe.get("active_xcodebuild_count", 0) > 0:
        penalty = probe["active_xcodebuild_count"] * 30
        score -= penalty
        components.append(f"-{penalty:.1f} ({probe['active_xcodebuild_count']} active builds)")
        
    if probe.get("active_simulator_count", 0) > 0:
        penalty = probe["active_simulator_count"] * 15
        score -= penalty
        components.append(f"-{penalty:.1f} ({probe['active_simulator_count']} active simulators)")
        
    if probe.get("cpu_load_1m", 0.0) > 1.0:
        penalty = probe["cpu_load_1m"] * 5
        score -= penalty
        components.append(f"-{penalty:.1f} (high CPU load)")

    if "tailscale" in tags:
        score -= 5
        components.append("-5.0 (tailscale latency penalty)")

    preferred_roles = set(group.get("preferred_roles", []))
    if preferred_roles and not preferred_roles.intersection(machine.get("roles", [])):
        score -= 50
        components.append("-50.0 (missing preferred roles)")

    max_jobs = machine.get("max_concurrent_jobs")
    if max_jobs is not None and probe.get("active_ai_jobs", 0) >= max_jobs:
        score -= 100
        components.append("-100.0 (at max job capacity)")

    if machine.get("interactive_reserved") and profile not in {"reasoning", "review"}:
        score -= 100
        components.append("-100.0 (reservation penalty for heavy task)")

    return score, components


def choose_machine_and_model(job: dict[str, Any], group: dict[str, Any], machines: list[dict[str, Any]], previously_assigned: str | None = None) -> tuple[dict[str, Any], dict[str, Any], str]:
    candidates: list[tuple[float, dict[str, Any], dict[str, Any], str, list[str]]] = []
    ineligible: list[tuple[dict[str, Any], str]] = []
    
    # Restrict to allowed machines if specified in job
    allowed_names = job.get("allowed_machines")
    
    for machine in machines:
        if allowed_names and machine["name"] not in allowed_names:
            ineligible.append((machine, "Not in allowed set for this job"))
            continue
            
        probe = probe_machine(machine)
        try:
            model = choose_model(group, machine, job)
        except RuntimeError as exc:
            ineligible.append((machine, str(exc)))
            continue
            
        # Dependency check for the selected model
        from model_registry import get_model
        m_meta = get_model(model)
        if m_meta:
            missing_bins = [b for b in m_meta.required_clis if not probe.get("binaries", {}).get(b, True)]
            if missing_bins:
                ineligible.append((machine, f"Missing required CLIs: {', '.join(missing_bins)}"))
                continue

        score, components = score_machine(machine, probe, group, previously_assigned=previously_assigned)
        if score <= -9999:
            ineligible.append((machine, components[0] if components else "Machine rejected by scheduler"))
            continue
        candidates.append((score, machine, probe, model, components))

    # Print Ineligible machines
    if ineligible:
        print("\n--- Ineligible Machines ---")
        for machine, reason in ineligible:
            print(f"  ❌ {machine['name']:15} | {reason}")
    
    if not candidates:
        details = format_ineligible_machines(ineligible)
        message = "No eligible machines available for scheduling."
        if details:
            message = f"{message}\n{details}"
        raise RuntimeError(message)

    candidates.sort(key=lambda candidate: candidate[0], reverse=True)

    print("\n--- Scheduling Scoreboard ---")
    for i, (score, machine, probe, model, components) in enumerate(candidates):
        marker = "🏆 WINNER" if i == 0 else f"#{i+1}"
        status_line = f"  {marker:9} | {machine['name']:15}: {score:6.1f} pts"
        if score < -500:
            status_line += f" (Reason: {components[0]})"
        print(status_line)
        for comp in components:
            print(f"      {comp}")
            
    if ineligible:
        print("\n  [Ineligible Machines]")
        for machine, reason in ineligible:
            print(f"  - {machine['name']:15}: {reason}")

    score, machine, probe, model, _ = candidates[0]
    print(f"\nDispatching to {machine['name']} using {model}")
    
    return machine, probe, model


def assign_group(job: dict[str, Any], group_id: str, machine: dict[str, Any], probe: dict[str, Any], model: str) -> dict[str, Any]:
    for group in job["task_groups"]:
        if group["group_id"] != group_id:
            continue
        group["assigned_machine"] = machine["name"]
        group["assigned_model"] = model
        group["status"] = "scheduled"
        assignment = {
            "group_id": group_id,
            "machine": machine["name"],
            "model": model,
            "resource_profile": group["resource_profile"],
            "scheduled_at": now_iso(),
            "probe": probe,
        }
        job["assigned_machine"] = machine["name"]
        job["assigned_model"] = model
        job["updated_at"] = now_iso()
        job["dispatch_history"].append(assignment)
        return assignment

    raise KeyError(f"Unknown group id: {group_id}")


def schedule_ready_group(
    job: dict[str, Any],
    machines: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str, dict[str, Any]]:
    groups = ready_groups(job)

    if not groups:
        raise RuntimeError("No ready task groups available for scheduling.")
    if len(job["task_groups"]) > 1:
        raise RuntimeError("Phase 1 scheduler supports single-group jobs only.")

    group = groups[0]
    prev_machine = group.get("assigned_machine") or job.get("assigned_machine")
    machine, probe, model = choose_machine_and_model(job, group, machines, previously_assigned=prev_machine)
    assignment = assign_group(job, group["group_id"], machine, probe, model)
    return group, machine, probe, model, assignment


def get_local_git_state() -> dict[str, Any]:
    try:
        branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ROOT), text=True).strip()
        head_hash = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
        status = subprocess.check_output(["git", "status", "--porcelain"], cwd=str(ROOT), text=True).strip()
        return {"branch": branch, "hash": head_hash, "dirty": len(status) > 0}
    except:
        return {"branch": "unknown", "hash": "unknown", "dirty": False}

def sync_code_to_remote(machine: dict[str, Any], local_state: dict[str, Any], remote_probe: dict[str, Any]) -> bool:
    """Ensures remote machine is synced with local code state."""
    ssh_target = remote_probe.get("active_ssh_target") or machine.get("ssh_target")
    if isinstance(ssh_target, list):
        ssh_target = ssh_target[0]
    
    remote_repo = machine["repo_path"]
    
    remote_branch = remote_probe.get("git_branch")
    remote_hash = remote_probe.get("git_head_hash")
    
    if remote_branch == local_state["branch"] and remote_hash == local_state["hash"] and not local_state["dirty"]:
        print(f"      - {machine['name']} is already perfectly in sync.")
        return True

    print(f"      - Synchronizing code to {machine['name']}...")
    
    # 1. If local is dirty, we use rsync to mirror the changes (fastest for uncommitted work)
    if local_state["dirty"]:
        print(f"      - Local changes detected. Using rsync to mirror state...")
        # Exclude common large folders
        excludes = ["--exclude", ".git/", "--exclude", "build/", "--exclude", "DerivedData/", "--exclude", "node_modules/", "--exclude", ".aider*"]
        cmd = ["rsync", "-avz", "--delete"] + excludes + [f"{ROOT}/", f"{ssh_target}:{remote_repo}/"]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            print(f"      - rsync successful.")
            return True
        except Exception as e:
            print(f"      - rsync failed: {e}. Falling back to git...")

    # 2. Git-based sync (Push/Pull)
    print(f"      - Pushing local branch '{local_state['branch']}' to origin...")
    try:
        subprocess.run(["git", "push", "origin", local_state["branch"]], check=True, capture_output=True)
        print(f"      - Pulling on {machine['name']}...")
        remote_cmd = f"cd '{remote_repo}' && git fetch origin && git checkout -f '{local_state['branch']}' && git pull origin '{local_state['branch']}'"
        subprocess.run(["ssh", ssh_target, remote_cmd], check=True, capture_output=True)
        print(f"      - Git sync successful.")
        return True
    except Exception as e:
        print(f"      - Git sync failed: {e}")
        return False


def verify_remote_worker_package(machine: dict[str, Any], ssh_target: str) -> bool:
    print(f"      - Checking orchestrator package on {machine['name']}...")
    result = run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", ssh_target, remote_import_check_command(machine)],
        cwd=ROOT,
        check=False,
    )
    if result.returncode == 0:
        return True

    print(f"\n❌ CRITICAL: Orchestrator package is not available on {machine['name']}.")
    print(f"   Run: orchestrator worker-install --machine {machine['name']}")
    return False


def remote_runtime_dir(machine: dict[str, Any]) -> str:
    return machine.get("orchestrator_runtime_dir") or ".orchestrator"

def dispatch_job(job_path: Path, machine: dict[str, Any], remote_probe: dict[str, Any], dry_run: bool, resume: bool = False) -> None:
    if dry_run:
        return

    if machine["execution_mode"] == "local":
        from worker_run import execute_job
        execute_job(job_path, resume=resume)
        return

    remote_repo = machine["repo_path"]
    job_basename = job_path.name
    ssh_target = remote_probe.get("active_ssh_target") or machine.get("ssh_target")
    if isinstance(ssh_target, list):
        ssh_target = ssh_target[0]

    if not verify_remote_worker_package(machine, ssh_target):
        sys.exit(1)

    # Sync code after validating the worker can run the package.
    print_phase("scheduling", subtext=f"syncing code to {machine['name']}")
    local_state = get_local_git_state()
    success = sync_code_to_remote(machine, local_state, remote_probe)
    
    if not success:
        print(f"\n❌ CRITICAL: Could not synchronize code to {machine['name']}.")
        print("   Aborting dispatch to prevent execution on inconsistent state.")
        sys.exit(1)

    runtime_dir = remote_runtime_dir(machine)
    remote_inbox = f"{runtime_dir}/jobs/inbox"

    print(f"      - Creating remote inbox directory on {machine['name']}...")
    run(["ssh", ssh_target, f"mkdir -p '{remote_repo}/{remote_inbox}'"], cwd=ROOT)
    
    print(f"      - Uploading job file {job_basename} to {machine['name']}...")
    run(["scp", str(job_path), f"{ssh_target}:{remote_repo}/{remote_inbox}/"], cwd=ROOT)
    
    print(f"      - Triggering remote worker run on {machine['name']}...")
    # Use -t for pseudo-tty to see colored output/real-time logs as they happen
    remote_job_path = f"{remote_repo}/{remote_inbox}/{job_basename}"
    resume_flag = " --resume" if resume else ""
    try:
        run(
            [
                "ssh",
                "-t", # Force TTY for real-time streaming and color
                ssh_target,
                f"cd '{remote_repo}' && {remote_env_prefix(machine)} python3 -m orchestrator.scripts.worker_run '{remote_inbox}/{job_basename}'{resume_flag}",
            ],
            cwd=ROOT,
            capture=False # Don't buffer output; stream it directly to dev console
        )
    finally:
        print(f"      - Downloading updated job state from {machine['name']}...")
        sync_result = run(["scp", f"{ssh_target}:{remote_job_path}", str(job_path)], cwd=ROOT, check=False)
        if sync_result.returncode != 0:
            print(f"      - Warning: Could not download updated job state from {machine['name']}.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_file")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Resume on the previously assigned machine")
    args = parser.parse_args()

    print_phase("scheduling")
    job_path = Path(args.job_file)
    job = normalize_job(load_job(job_path))
    machines = load_machines()
    
    group, machine, _probe, _model, assignment = schedule_ready_group(job, machines)
    
    if not args.dry_run:
        write_json(job_path, job)

    action = "Would schedule" if args.dry_run else "Scheduled"
    print(
        f"{action} {job['job_id']} group {group['group_id']} "
        f"to {assignment['machine']} using {assignment['model']}"
    )

    dispatch_job(job_path, machine, _probe, dry_run=args.dry_run, resume=args.resume)


if __name__ == "__main__":
    main()
