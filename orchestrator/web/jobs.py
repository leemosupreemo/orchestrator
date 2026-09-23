from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from .contract import Operation
from .menu import available_actions


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
Runner = Callable[..., int]


def create_job(root: Path, options: dict[str, Any], runner: Runner = None) -> dict[str, Any]:
    runner = runner or _run
    job_type = options.get("job_type")
    if job_type not in {"bug", "feature", "design", "coverage", "quick"}:
        raise ValueError("Invalid job type")
    prompt = options.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A job prompt is required")
    models = options.get("allowed_models")
    machines = options.get("allowed_machines")
    if not isinstance(models, list) or not models or not all(isinstance(x, str) and _ID.fullmatch(x) for x in models):
        raise ValueError("Select at least one allowed model")
    if not isinstance(machines, list) or not machines or not all(isinstance(x, str) and _ID.fullmatch(x) for x in machines):
        raise ValueError("Select at least one allowed machine")
    branch_mode = options.get("branch_mode", "new")
    if branch_mode not in {"new", "current", "manual"}:
        raise ValueError("Invalid branch mode")
    argv = [sys.executable, str(_SCRIPTS / "new_job.py"), job_type,
            "--allowed-models", ",".join(models), "--allowed-machines", ",".join(machines),
            "--branch-mode", branch_mode]
    for field, flag in (("preset", "--preset"), ("planner", "--planner"),
                        ("builder", "--builder"), ("reviewer", "--reviewer"),
                        ("spec_url", "--spec-file")):
        value = options.get(field)
        if value:
            if not isinstance(value, str):
                raise ValueError(f"Invalid {field}")
            argv.extend([flag, value])
    if options.get("stitch"):
        argv.append("--stitch")
    if options.get("yolo"):
        argv.append("--yolo")
    else:
        argv.append("--no-dispatch")
    before = {job["job_id"] for job in list_jobs(root)}
    with tempfile.TemporaryDirectory(prefix="orchestrator-web-job-") as temporary:
        payload_path = Path(temporary) / "input.json"
        payload_path.write_text(json.dumps(options), encoding="utf-8")
        payload_path.chmod(0o600)
        argv.extend(["--web-input-file", str(payload_path)])
        env = {**os.environ, "ORCHESTRATOR_PROJECT_ROOT": str(root)}
        code = runner(argv, cwd=root, env=env)
    if code != 0:
        raise RuntimeError(f"Job creation failed with exit code {code}")
    created = [job for job in list_jobs(root) if job["job_id"] not in before]
    if len(created) != 1:
        raise RuntimeError("Job creation did not produce exactly one new local job")
    return created[0]


def _job_path(root: Path, job_id: str) -> Path:
    if not _ID.fullmatch(job_id):
        raise ValueError("Invalid job ID")
    return root / ".orchestrator" / "jobs" / f"{job_id}.json"


def _read_job(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    job = json.loads(raw)
    if not isinstance(job, dict):
        raise ValueError("Job file must contain an object")
    job["revision"] = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")
    return job


def list_jobs(root: Path) -> list[dict[str, Any]]:
    directory = root / ".orchestrator" / "jobs"
    if not directory.exists():
        return []
    return [_read_job(path) for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)]


def get_job(root: Path, job_id: str) -> dict[str, Any]:
    path = _job_path(root, job_id)
    if not path.is_file():
        raise FileNotFoundError(job_id)
    return _read_job(path)


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> int:
    return subprocess.run(argv, cwd=cwd, env=env, check=False).returncode


def run_job_action(
    root: Path, job_id: str, operation: Operation, inputs: dict[str, Any],
    expected_revision: int, runner: Runner = _run,
) -> dict[str, Any]:
    current = get_job(root, job_id)
    if current["revision"] != expected_revision:
        raise ValueError("Job changed; refresh before applying this action")
    if operation not in {item.operation for item in available_actions(current)}:
        raise ValueError(f"{operation} is unavailable for this job state")
    path = _job_path(root, job_id)
    if operation == Operation.JOB_LINK_LOGS:
        log_paths = inputs.get("log_paths")
        if not isinstance(log_paths, list) or not all(isinstance(p, str) and p and not Path(p).is_absolute() and ".." not in Path(p).parts for p in log_paths):
            raise ValueError("Log paths must be relative to the project")
        current.pop("revision")
        current["last_manual_log_paths"] = log_paths
        path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    else:
        scripts = {
            Operation.JOB_SCHEDULE: ("schedule_job.py", []),
            Operation.JOB_EXECUTE: ("worker_run.py", []),
            Operation.JOB_RESUME: ("schedule_job.py", ["--resume"]),
            Operation.JOB_DELIVER: ("deliver_build.py", []),
            Operation.JOB_EXPORT: ("export_job.py", []),
        }
        if operation not in scripts:
            raise NotImplementedError(f"No local handler for {operation}")
        script, flags = scripts[operation]
        env = {**os.environ, "ORCHESTRATOR_PROJECT_ROOT": str(root)}
        code = runner([sys.executable, str(_SCRIPTS / script), str(path), *flags], cwd=root, env=env)
        if code != 0:
            raise RuntimeError(f"{operation} failed with exit code {code}")
    return get_job(root, job_id)
