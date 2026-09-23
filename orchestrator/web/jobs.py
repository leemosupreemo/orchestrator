from __future__ import annotations

import hashlib
import json
import os
import re
import ipaddress
import socket
import subprocess
import sys
import tempfile
import shutil
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from pathlib import Path
from typing import Any, Callable

from .contract import Operation
from .menu import available_actions


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
Runner = Callable[..., int]


def _validate_public_https_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Specification URL must be public HTTPS")
    if parsed.hostname.lower() == "localhost":
        raise ValueError("Specification URL must use a public host")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise ValueError("Specification URL must use a domain name")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Specification host could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(entry[4][0]).is_global for entry in addresses):
        raise ValueError("Specification host must resolve to public IP addresses")


def _fetch_public_spec(url: str) -> str:
    _validate_public_https_url(url)
    class SafeRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            _validate_public_https_url(newurl)
            return super().redirect_request(request, fp, code, msg, headers, newurl)
    opener = build_opener(SafeRedirect())
    with opener.open(Request(url, headers={"User-Agent": "Orchestrator Web"}), timeout=10) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("Specification URL exceeds 1 MB")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Specification URL must contain UTF-8 text") from exc


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
    spec_url = options.get("spec_url")
    if spec_url:
        if not isinstance(spec_url, str):
            raise ValueError("Invalid specification source")
        parsed = urlparse(spec_url)
        if parsed.scheme:
            _validate_public_https_url(spec_url)
        elif not _safe_relative_path(root, spec_url).is_file():
            raise ValueError("Specification file must be inside the selected project")
    argv = [sys.executable, str(_SCRIPTS / "new_job.py"), job_type,
            "--allowed-models", ",".join(models), "--allowed-machines", ",".join(machines),
            "--branch-mode", branch_mode]
    for field, flag in (("preset", "--preset"), ("planner", "--planner"),
                        ("builder", "--builder"), ("reviewer", "--reviewer")):
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
        if spec_url:
            if urlparse(spec_url).scheme:
                source_path = Path(temporary) / "external-spec.md"
                source_path.write_text(_fetch_public_spec(spec_url), encoding="utf-8")
                source_path.chmod(0o600)
            else:
                source_path = _safe_relative_path(root, spec_url)
            argv.extend(["--spec-file", str(source_path)])
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


def list_archived_jobs(root: Path) -> list[dict[str, Any]]:
    directory = root / ".orchestrator" / "jobs" / "archive"
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


def _save_job(path: Path, job: dict[str, Any]) -> None:
    data = {key: value for key, value in job.items() if key != "revision" and not key.startswith("_web_")}
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.stem}-", suffix=".json", delete=False,
                                     encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _archive_job(root: Path, path: Path, job: dict[str, Any], status: str) -> dict[str, Any]:
    job["status"] = status
    job["completed_at"] = datetime.now(timezone.utc).isoformat()
    _save_job(path, job)
    archive = root / ".orchestrator" / "jobs" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    path.replace(archive / path.name)
    return job


def _run_checked(argv: list[str], root: Path, runner: Runner) -> None:
    code = runner(argv, cwd=root, env={**os.environ, "ORCHESTRATOR_PROJECT_ROOT": str(root)})
    if code != 0:
        raise RuntimeError(f"Operation failed with exit code {code}")


def _run_script(name: str, args: list[str], root: Path, runner: Runner) -> None:
    _run_checked([sys.executable, str(_SCRIPTS / name), *args], root, runner)


def _safe_relative_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value:
        raise ValueError("Path must stay inside the project")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Path must stay inside the project")
    return resolved


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
        paths = inputs.get("log_paths")
        if not isinstance(paths, list) or any(not isinstance(p, str) or not _safe_relative_path(root, p) for p in paths):
            raise ValueError("Log paths must be project-relative files")
        current["last_manual_log_paths"] = paths
        _save_job(path, current)
    elif operation == Operation.JOB_MODELS:
        models = inputs.get("allowed_models")
        if not isinstance(models, list) or not models or any(not isinstance(x, str) or not _ID.fullmatch(x) for x in models):
            raise ValueError("Select valid model IDs")
        current["allowed_models"] = models
        _save_job(path, current)
    elif operation == Operation.JOB_ATTACH_REFERENCE:
        url = inputs.get("source_url")
        source_file = inputs.get("_trusted_source_file")
        if source_file:
            source = Path(source_file)
            if not source.is_file() or source.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("Uploaded reference is missing or too large")
            name = re.sub(r"[^A-Za-z0-9._-]", "-", str(inputs.get("_trusted_file_name", "reference")))[:128]
            if Path(name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".txt"}:
                raise ValueError("Unsupported uploaded reference type")
            destination_dir = root / ".orchestrator" / "output" / job_id / "references"
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / f"{len(current.get('reference_artifacts', []))+1}-{name}"
            shutil.copyfile(source, destination)
            artifact = {"type": "file_reference", "path": str(destination.relative_to(root)),
                        "note": str(inputs.get("description", ""))[:1000]}
        else:
            if not isinstance(url, str) or urlparse(url).scheme not in {"https", "http"} or not urlparse(url).netloc:
                raise ValueError("Supply an HTTP(S) reference URL or uploaded file")
            artifact = {"type": "url_reference", "url": url, "note": str(inputs.get("description", ""))[:1000]}
        current.setdefault("reference_artifacts", []).append(artifact)
        _save_job(path, current)
    elif operation in {Operation.JOB_REVISE, Operation.JOB_ITERATE}:
        feedback = inputs.get("feedback")
        if not isinstance(feedback, str) or not feedback.strip():
            raise ValueError("Feedback is required")
        kind = {"bug-fix": "bug", "bug-investigate": "bug", "feature-plan": "feature",
                "test-audit": "coverage", "feature-design": "design"}.get(current.get("type"), "feature")
        args = [kind, "--no-dispatch", "--update", str(path), "--feedback", feedback]
        args.extend(["--allowed-models", ",".join(current.get("allowed_models") or ["codex"])])
        args.extend(["--allowed-machines", ",".join(current.get("allowed_machines") or ["local"])])
        if current.get("design_spec") or kind == "design":
            args.append("--stitch")
        _run_script("new_job.py", args, root, runner)
    elif operation == Operation.JOB_APPROVE_SUBTASKS:
        if current.get("status") == "designing":
            design = current.get("plan", {})
            current["design_spec"] = design
            current["status"] = "planned"
            current["type"] = "feature-plan"
            current["raw_input"] = str(current.get("raw_input", "")) + "\n\nApproved design: " + json.dumps(design)
            _save_job(path, current)
            _run_script("new_job.py", ["feature", "--no-dispatch", "--update", str(path),
                                       "--allowed-models", ",".join(current.get("allowed_models") or ["codex"]),
                                       "--allowed-machines", ",".join(current.get("allowed_machines") or ["local"])], root, runner)
        else:
            current["approved"] = True
            _save_job(path, current)
            _run_script("schedule_job.py", [str(path)], root, runner)
    elif operation == Operation.JOB_DEBUG:
        args = [str(path)]
        feedback = str(inputs.get("feedback") or "").strip()
        if inputs.get("notes"):
            feedback += "\nVerification notes: " + str(inputs["notes"])
        if inputs.get("bug_reproduced"):
            feedback += "\nBug was reproduced in the latest verification."
        if feedback:
            args.extend(["--feedback", feedback.strip()])
        if inputs.get("max_iterations"):
            count = inputs["max_iterations"]
            if type(count) is not int or not 1 <= count <= 20:
                raise ValueError("Iterations must be between 1 and 20")
            args.extend(["--max-iterations", str(count)])
        _run_script("debug_job.py", args, root, runner)
    elif operation == Operation.JOB_REOPEN_BUG:
        if current.get("type") not in {"bug-fix", "bug-investigate"}:
            raise ValueError("This is not a bug job")
        feedback = inputs.get("feedback")
        if not isinstance(feedback, str) or not feedback.strip():
            raise ValueError("Describe what is still happening")
        current["status"] = "debugging"
        current["debug_phase"] = "propose"
        _save_job(path, current)
        _run_script("debug_job.py", [str(path), "--feedback", feedback], root, runner)
    elif operation == Operation.JOB_BRIEF:
        directory = root / ".orchestrator" / "output" / job_id
        files = {}
        for name in ("brief.md", "builder_summary.md"):
            item = directory / name
            if item.is_file():
                files[name] = item.read_text(encoding="utf-8", errors="replace")[:20000]
        current["_web_result"] = {"files": files, "plan": current.get("plan", {})}
        return current
    elif operation == Operation.JOB_FILES:
        current["_web_result"] = {"modified": current.get("ai_modified_files", []),
                                  "untracked": current.get("ai_untracked_files", [])}
        return current
    elif operation == Operation.JOB_GITHUB:
        number = current.get("pr_number") or current.get("issue_number")
        kind = "pull" if current.get("pr_number") else "issues"
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root,
                                capture_output=True, text=True, check=False)
        match = re.search(r"github\.com[:/]([^/]+/[^/.]+)", remote.stdout)
        if not number or not match:
            raise ValueError("No GitHub PR or issue URL is available")
        current["_web_result"] = {"url": f"https://github.com/{match.group(1)}/{kind}/{int(number)}"}
        return current
    elif operation == Operation.JOB_ASK_AI:
        question = inputs.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("Question is required")
        sys.path.insert(0, str(_SCRIPTS))
        from llm import run_llm
        model = inputs.get("model") or current.get("builder") or "codex"
        context = json.dumps({key: current.get(key) for key in ("title", "status", "plan", "branch", "ai_modified_files")}, default=str)
        output, actual_model, session_id = run_llm(model, f"Answer the user's question about this job without changing code.\nJob: {context[:30000]}\nQuestion: {question}", allowed_models=current.get("allowed_models"))
        current.setdefault("llm_sessions", []).append({"id": session_id, "model": actual_model})
        _save_job(path, current)
        current["_web_result"] = {"answer": output[:20000], "model": actual_model}
    elif operation == Operation.JOB_CLOSE_ISSUE:
        number = current.get("issue_number")
        if not isinstance(number, int):
            raise ValueError("No issue number is linked to this job")
        _run_checked(["gh", "issue", "close", str(number)], root, runner)
        return _archive_job(root, path, current, "completed")
    elif operation == Operation.JOB_MERGE:
        number = current.get("pr_number")
        if not isinstance(number, int):
            raise ValueError("No PR number is linked to this job")
        view = subprocess.run(["gh", "pr", "view", str(number), "--json", "isDraft"], cwd=root,
                              capture_output=True, text=True, check=False)
        if view.returncode != 0 or json.loads(view.stdout).get("isDraft"):
            raise ValueError("PR must exist and be ready for review")
        _run_checked(["gh", "pr", "merge", str(number), "--merge", "--delete-branch"], root, runner)
        return _archive_job(root, path, current, "completed")
    elif operation == Operation.JOB_DISCARD:
        for name in current.get("ai_modified_files", []):
            target = _safe_relative_path(root, name)
            if target.exists():
                _run_checked(["git", "checkout", "--", str(target.relative_to(root))], root, runner)
        for name in current.get("ai_untracked_files", []):
            target = _safe_relative_path(root, name)
            if target.is_file():
                target.unlink()
        return _archive_job(root, path, current, "discarded")
    elif operation == Operation.JOB_RERUN:
        branch_mode = inputs.get("branch_mode", "new")
        if branch_mode not in {"new", "current", "manual"}:
            raise ValueError("Invalid branch mode")
        for name in current.get("ai_modified_files", []):
            target = _safe_relative_path(root, name)
            if target.exists():
                _run_checked(["git", "checkout", "--", str(target.relative_to(root))], root, runner)
        for name in current.get("ai_untracked_files", []):
            target = _safe_relative_path(root, name)
            if target.is_file():
                target.unlink()
        current.update({"branch_mode": branch_mode, "status": "planned", "pr_number": None})
        if branch_mode == "new":
            current["branch"] = None
        _save_job(path, current)
        _run_script("schedule_job.py", [str(path)], root, runner)
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
        _run_script(script, [str(path), *flags], root, runner)
    result = get_job(root, job_id)
    if "_web_result" in current:
        result["_web_result"] = current["_web_result"]
    return result
