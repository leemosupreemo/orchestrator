"""Run Design and Tests without the production builder, debugger or git pipeline."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from common import ROOT, OUTPUT_DIR, PROMPTS_DIR, get_repo_state, now_iso, read_json, write_json, write_text
from llm import extract_json_block, run_llm
from model_router import ModelRole
from orchestrator import job_scope
from orchestrator.project_config import PROJECT_CONFIG
from reference_artifacts import attach_reference_artifact, reference_context


def execute_nonproduction_job(job_path: Path) -> None:
    job = read_json(job_path)
    design = job.get("type") in job_scope.DESIGN_TYPES
    job.update(status="executing", worker_pid=os.getpid(), updated_at=now_iso())
    write_json(job_path, job)
    out = OUTPUT_DIR / job["job_id"]
    out.mkdir(parents=True, exist_ok=True)
    try:
        before = get_repo_state()
        dirty = set(before["modified"]) | set(before["untracked"])
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, text=True)
        if untracked.returncode == 0:
            dirty.update(untracked.stdout.rstrip("\0").split("\0"))
        owned = job.get("output_digests") or {}
        for path, digest in owned.items():
            target = ROOT / path
            if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
                dirty.discard(path)
        template = "builder_design.md" if design else "builder_coverage.md"
        prompt = (PROMPTS_DIR / template).read_text(encoding="utf-8")
        prompt += f"\nJob: {job['title']}\nPlan:\n{json.dumps(job.get('plan', {}), indent=2)}\n{reference_context(job)}"
        from orchestrator import prd
        prompt += prd.context_block(ROOT, "builder")
        output, model, session = run_llm(job["builder"], prompt, cwd=ROOT, timeout=1200,
                                       allowed_models=job.get("allowed_models"), role=ModelRole.BUILDER,
                                       protected_paths=[ROOT])
        result = json.loads(extract_json_block(output))
        if result.get("clarification_needed"):
            job["human_clarification_question"] = result["clarification_needed"]
            raise ValueError(result["clarification_needed"])
        proposals = result.get("files")
        if isinstance(proposals, list) and not design:
            conflicts = [p.get("path") for p in proposals if isinstance(p, dict) and p.get("path") in dirty]
            if conflicts:
                raise ValueError("Test outputs overlap existing local changes: " + ", ".join(conflicts))
        written = job_scope.write_files(ROOT, job["type"], job["job_id"], proposals)
        job["actual_builder_used"] = model
        job.setdefault("llm_sessions", []).append({"id": session, "model": model})
        job["builder_summary"] = str(result.get("summary") or "")
        tracked_result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True)
        tracked = set(tracked_result.stdout.rstrip("\0").split("\0")) if tracked_result.returncode == 0 else set()
        job["ai_modified_files"] = sorted(set(job.get("ai_modified_files") or []) | {path for path in written if path in tracked})
        job["ai_untracked_files"] = sorted(set(job.get("ai_untracked_files") or []) | {path for path in written if path not in tracked})
        job["output_digests"] = {**owned, **{path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in written}}
        job["output_scope"] = "design-mockups" if design else "tests-only"
        write_text(out / "builder_summary.md", job["builder_summary"] + "\n\nFiles:\n" + "\n".join(f"- {path}" for path in written))
        if design:
            for path in written:
                # References open in the job's sandboxed visual preview.
                if path.endswith((".html", ".svg")):
                    ref = attach_reference_artifact(job, path, note="Generated visual mockup")
                    ref["role"] = "design"
                    job.setdefault("reference_artifacts", []).append(ref)
            job["design_artifacts"] = written
        else:
            command = PROJECT_CONFIG.test_command or PROJECT_CONFIG.stack.test_command
            if not command:
                from common import extract_commands
                command = extract_commands()[1]
            job["test_validation"] = {"command": command, "passed": False}
            with tempfile.TemporaryDirectory(prefix="orchestrator-coverage-") as scratch:
                # Build caches and test fixtures may change in this disposable copy.
                # Only validated test proposals above are ever copied to the real project.
                work = Path(scratch).resolve() / "project"
                def ignore(directory, names):
                    return set(names) & {".git", ".orchestrator", ".build", "target", "node_modules", ".venv", "venv", "__pycache__"} if Path(directory) == ROOT else set()
                shutil.copytree(ROOT, work, symlinks=True, ignore=ignore)
                for dependency in ("node_modules", ".venv", "venv"):
                    if (ROOT / dependency).is_dir():
                        (work / dependency).symlink_to(ROOT / dependency, target_is_directory=True)
                config = ROOT / ".orchestrator/project.json"
                if config.is_file():
                    (work / ".orchestrator").mkdir(exist_ok=True)
                    shutil.copy2(config, work / ".orchestrator/project.json")
                isolated_command = job_scope.protect_command(command.replace(str(ROOT), str(work)), [ROOT])
                env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "COVERAGE_FILE": str(Path(scratch) / ".coverage"), "TMPDIR": scratch,
                       "ORCHESTRATOR_PROJECT_ROOT": str(work), "ORCHESTRATOR_RUNTIME_DIR": str(work / ".orchestrator"),
                       "AI_RUNTIME_DIR": str(work / ".orchestrator")}
                env.pop("ORCHESTRATOR_CONFIG", None)
                if env.get("PYTHONPATH"):
                    env["PYTHONPATH"] = os.pathsep.join(str(work / Path(part).relative_to(ROOT)) if part and Path(part).is_relative_to(ROOT) else part
                                                          for part in env["PYTHONPATH"].split(os.pathsep))
                validation = subprocess.run(isolated_command, shell=True, cwd=work, env=env,
                                            capture_output=True, text=True, timeout=1200)
                reports = [Path(scratch) / ".coverage", work / "coverage.xml", work / "lcov.info", work / "coverage/lcov.info"]
                for report in reports:
                    if report.is_file():
                        (out / "coverage").mkdir(exist_ok=True)
                        shutil.copy2(report, out / "coverage" / report.name)
            write_text(out / "test.log", validation.stdout + validation.stderr)
            job["test_validation"]["passed"] = validation.returncode == 0
            if validation.returncode:
                raise ValueError("Test validation failed. Review test.log; production fixes require a separate Bug fix job.")
        job.update(status="review-needed", last_error=None, human_clarification_question=None)
    except Exception as exc:
        job.update(status="human-needed", last_error=str(exc))
        print(f"Design/Tests job stopped: {exc}")
    finally:
        job.update(worker_pid=None, updated_at=now_iso())
        write_json(job_path, job)
