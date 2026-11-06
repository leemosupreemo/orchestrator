from __future__ import annotations

import json
import base64
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from .contract import Operation, validate_command
from .jobs import create_job, list_jobs, list_archived_jobs, run_job_action, _job_path
from .projection import project_job, redact_data, redact_text
from .tools import run_tool, run_app_tests, run_refresh, _find_test_plans, _read, _NAME


class Transport(Protocol):
    def pending(self) -> list[str]: ...
    def interrupted(self) -> list[str]: ...
    def claim(self, command_id: str) -> dict[str, Any] | None: ...
    def finish(self, command_id: str, status: str, result: dict[str, Any]) -> None: ...
    def publish_job(self, project_id: str, job: dict[str, Any]) -> None: ...
    def publish_archive(self, project_id: str, job: dict[str, Any]) -> None: ...
    def active_ids(self, project_id: str) -> set[str]: ...
    def remove_job(self, project_id: str, job_id: str) -> None: ...
    def archived_ids(self, project_id: str) -> set[str]: ...
    def remove_archive(self, project_id: str, job_id: str) -> None: ...
    def heartbeat(self, projects: list[str], test_plans: dict[str, list[str]] | None = None) -> None: ...
    def fetch_artifact(self, project_id: str, artifact_id: str) -> tuple[str, bytes]: ...
    def progress(self, command_id: str, output: str) -> None: ...


class Worker:
    def __init__(self, projects: dict[str, Path], transport: Transport, owner_email: str):
        self.projects = {key: Path(root).resolve() for key, root in projects.items()}
        self.transport = transport
        self.owner_email = owner_email
        self._locks = {key: threading.Lock() for key in projects}
        self._published_active: dict[tuple[str, str], int] = {}
        self._published_archived: dict[tuple[str, str], int] = {}
        self._known_active_ids: dict[str, set[str]] = {}
        self._known_archived_ids: dict[str, set[str]] = {}
        self._last_test_plans: dict[str, list[str]] = {}

    def process(self, command_id: str) -> None:
        raw = self.transport.claim(command_id)
        if raw is None:
            return
        try:
            if raw.get("requested_by") != self.owner_email:
                raise ValueError("Operator is not authorized")
            command = validate_command({key: raw[key] for key in
                                        ("project_id", "operation", "job_id", "inputs", "expected_revision") if key in raw})
            root = self.projects.get(command.project_id)
            if root is None:
                if command.operation == Operation.TOOL_PROJECT and self.projects:
                    root = next(iter(self.projects.values()))
                else:
                    raise ValueError("Project is not configured on this worker")
            def runner(argv: list[str], *, cwd: Path, env: dict[str, str]) -> int:
                process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, bufsize=1)
                output = ""
                last_publish = 0.0
                assert process.stdout is not None
                for line in process.stdout:
                    output = (output + redact_text(line))[-20000:]
                    if time.monotonic() - last_publish > 1.0:
                        self.transport.progress(command_id, output)
                        last_publish = time.monotonic()
                code = process.wait()
                self.transport.progress(command_id, output)
                return code
            with self._locks[command.project_id]:
                if command.operation == Operation.JOB_CREATE:
                    inputs = dict(command.inputs)
                    if inputs.get("artifact_id"):
                        name, data = self.transport.fetch_artifact(command.project_id, inputs["artifact_id"])
                        with tempfile.TemporaryDirectory(prefix="orchestrator-web-spec-") as temporary:
                            safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", name or "spec.md")[:128]
                            source = Path(temporary) / safe_name
                            source.write_bytes(data)
                            inputs["_trusted_source_file"] = str(source)
                            inputs["_trusted_file_name"] = name
                            job = create_job(root, inputs, runner=runner)
                    else:
                        job = create_job(root, inputs, runner=runner)
                    result = {"job_id": job["job_id"]}
                    self.transport.publish_job(command.project_id, project_job(job))
                elif command.operation.value.startswith("job."):
                    inputs = dict(command.inputs)
                    if command.operation == Operation.JOB_ATTACH_REFERENCE and inputs.get("artifact_id"):
                        name, data = self.transport.fetch_artifact(command.project_id, inputs["artifact_id"])
                        with tempfile.TemporaryDirectory(prefix="orchestrator-web-reference-") as temporary:
                            source = Path(temporary) / "upload"
                            source.write_bytes(data)
                            inputs["_trusted_source_file"] = str(source)
                            inputs["_trusted_file_name"] = name
                            job = run_job_action(root, command.job_id, command.operation,
                                                 inputs, command.expected_revision, runner=runner)
                    else:
                        job = run_job_action(root, command.job_id, command.operation,
                                             inputs, command.expected_revision, runner=runner)
                    result = {"job_id": job["job_id"], "revision": job["revision"]}
                    if "_web_result" in job:
                        result.update(job["_web_result"])
                    if _job_path(root, job["job_id"]).is_file():
                        self.transport.publish_job(command.project_id, project_job(job))
                    else:
                        self.transport.publish_archive(command.project_id, project_job(job))
                        self.transport.remove_job(command.project_id, job["job_id"])
                elif command.operation == Operation.TESTS_RUN:
                    result = run_app_tests(root, command.inputs,
                                           progress=lambda output: self.transport.progress(command_id, redact_text(output)))
                elif command.operation == Operation.PROJECT_REFRESH:
                    result = run_refresh(root)
                    self.reconcile()
                elif command.operation == Operation.TOOL_PROJECT and command.inputs.get("action") == "add":
                    new_id = str(command.inputs.get("project_id", "")).strip()
                    raw_path = str(command.inputs.get("path", "")).strip()
                    new_path = Path(raw_path).expanduser().resolve()
                    if not new_id or not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", new_id):
                        raise ValueError("Project ID must contain only letters, numbers, dashes, and underscores (1-64 chars)")
                    if not new_path.is_dir():
                        raise ValueError(f"Directory not found on Mac worker: {raw_path}")
                    self.projects[new_id] = new_path
                    if new_id not in self._locks:
                        self._locks[new_id] = threading.Lock()
                    _save_persistent_projects(self.projects)
                    self.reconcile()
                    result = {"registered": new_id, "path": str(new_path), "projects": sorted(self.projects.keys())}
                else:
                    result = run_tool(root, command.operation, command.inputs,
                                      progress=lambda output: self.transport.progress(command_id, redact_text(output)))
            status = "failed" if isinstance(result, dict) and (result.get("exit_code", 0) != 0 or
                      isinstance(result.get("sync"), dict) and result["sync"].get("success") is False) else "succeeded"
            self.transport.finish(command_id, status, redact_data(result))
        except Exception as exc:
            self.transport.finish(command_id, "failed", {"error": redact_text(str(exc))})

    def reconcile(self, sync_github: bool = False) -> None:
        test_plans: dict[str, list[str]] = {}
        for project_id, root in self.projects.items():
            if sync_github:
                try:
                    run_refresh(root)
                except Exception:
                    pass
            active = list_jobs(root)
            active_ids = {job["job_id"] for job in active}
            for job in active:
                key = (project_id, job["job_id"])
                rev = job.get("revision", 0)
                if key not in self._published_active or self._published_active[key] != rev:
                    self.transport.publish_job(project_id, project_job(job))
                    self._published_active[key] = rev

            archived = list_archived_jobs(root)
            archived_ids = {job["job_id"] for job in archived}
            for job in archived:
                key = (project_id, job["job_id"])
                rev = job.get("revision", 0)
                if key not in self._published_archived or self._published_archived[key] != rev:
                    self.transport.publish_archive(project_id, project_job(job))
                    self._published_archived[key] = rev

            prev_active = self._known_active_ids.get(project_id)
            if prev_active is None or prev_active != active_ids:
                try:
                    for old_id in self.transport.active_ids(project_id) - active_ids:
                        self.transport.remove_job(project_id, old_id)
                        self._published_active.pop((project_id, old_id), None)
                except Exception:
                    pass
                self._known_active_ids[project_id] = active_ids

            prev_archived = self._known_archived_ids.get(project_id)
            if prev_archived is None or prev_archived != archived_ids:
                try:
                    for old_id in self.transport.archived_ids(project_id) - archived_ids:
                        self.transport.remove_archive(project_id, old_id)
                        self._published_archived.pop((project_id, old_id), None)
                except Exception:
                    pass
                self._known_archived_ids[project_id] = archived_ids

            try:
                config = _read(root / ".orchestrator" / "project.json", {})
                target = config.get("test_target")
                safe_target = target if isinstance(target, str) and _NAME.fullmatch(target) else None
                plans = _find_test_plans(root, safe_target)
                test_plans[project_id] = [p.name for p in plans]
            except Exception:
                test_plans[project_id] = []
        self._last_test_plans = test_plans
        self.transport.heartbeat(sorted(self.projects), test_plans)

    def recover(self) -> None:
        for command_id in self.transport.interrupted():
            self.transport.finish(command_id, "failed", {
                "error": "Worker stopped during this operation. Review local project state before retrying."})

    def run_forever(self, poll_seconds: float = 3.0, github_sync_seconds: float = 60.0) -> None:
        try:
            self.recover()
        except Exception:
            pass
        def heartbeat_loop() -> None:
            while True:
                try:
                    self.transport.heartbeat(sorted(self.projects), self._last_test_plans or None)
                except Exception:
                    pass
                time.sleep(15)
        threading.Thread(target=heartbeat_loop, daemon=True).start()
        next_reconcile = 0.0
        next_github_sync = 0.0
        backoff = 0
        while True:
            try:
                now = time.monotonic()
                if now >= next_reconcile:
                    sync_github = now >= next_github_sync
                    self.reconcile(sync_github=sync_github)
                    next_reconcile = now + 15.0
                    if sync_github:
                        next_github_sync = now + github_sync_seconds
                for command_id in self.transport.pending():
                    self.process(command_id)
                backoff = 0
                time.sleep(poll_seconds)
            except Exception:
                sleep_time = min(60.0, 5.0 * (2 ** min(backoff, 4)))
                backoff += 1
                time.sleep(sleep_time)


class FirebaseTransport:
    def __init__(self, project_id: str):
        import firebase_admin
        from firebase_admin import firestore
        try:
            firebase_admin.get_app()
        except ValueError:
            firebase_admin.initialize_app(options={"projectId": project_id})
        self.db = firestore.client()
        self.firestore = firestore

    def pending(self) -> list[str]:
        query = self.db.collection("commands").where("status", "==", "queued").limit(20)
        return [doc.id for doc in query.stream()]

    def interrupted(self) -> list[str]:
        query = self.db.collection("commands").where("status", "==", "running")
        return [doc.id for doc in query.stream()]

    def claim(self, command_id: str) -> dict[str, Any] | None:
        ref = self.db.collection("commands").document(command_id)
        transaction = self.db.transaction()
        @self.firestore.transactional
        def claim_in_transaction(tx):
            snapshot = ref.get(transaction=tx)
            if not snapshot.exists or snapshot.get("status") != "queued":
                return None
            tx.update(ref, {"status": "running", "started_at": self.firestore.SERVER_TIMESTAMP})
            return snapshot.to_dict()
        return claim_in_transaction(transaction)

    def finish(self, command_id: str, status: str, result: dict[str, Any]) -> None:
        self.db.collection("commands").document(command_id).update({
            "status": status, "result": result, "finished_at": self.firestore.SERVER_TIMESTAMP})

    def progress(self, command_id: str, output: str) -> None:
        self.db.collection("commands").document(command_id).update({"output": output})

    def publish_job(self, project_id: str, job: dict[str, Any]) -> None:
        self.db.collection("projects").document(project_id).collection("jobs").document(job["job_id"]).set(job)

    def publish_archive(self, project_id: str, job: dict[str, Any]) -> None:
        self.db.collection("projects").document(project_id).collection("archives").document(job["job_id"]).set(job)

    def active_ids(self, project_id: str) -> set[str]:
        return {doc.id for doc in self.db.collection("projects").document(project_id).collection("jobs").stream()}

    def remove_job(self, project_id: str, job_id: str) -> None:
        self.db.collection("projects").document(project_id).collection("jobs").document(job_id).delete()

    def archived_ids(self, project_id: str) -> set[str]:
        return {doc.id for doc in self.db.collection("projects").document(project_id).collection("archives").stream()}

    def remove_archive(self, project_id: str, job_id: str) -> None:
        self.db.collection("projects").document(project_id).collection("archives").document(job_id).delete()

    def heartbeat(self, projects: list[str], test_plans: dict[str, list[str]] | None = None) -> None:
        data: dict[str, Any] = {
            "project_ids": projects,
            "last_seen": self.firestore.SERVER_TIMESTAMP
        }
        if test_plans is not None:
            data["test_plans"] = test_plans
        self.db.collection("workers").document("mac").set(data, merge=True)

    def fetch_artifact(self, project_id: str, artifact_id: str) -> tuple[str, bytes]:
        if not artifact_id or len(artifact_id) > 128 or not all(c.isalnum() or c in "_-" for c in artifact_id):
            raise ValueError("Invalid artifact ID")
        ref = self.db.collection("projects").document(project_id).collection("artifacts").document(artifact_id)
        metadata = ref.get().to_dict()
        ct = str(metadata.get("content_type", ""))
        if not (ct in {"image/png", "image/jpeg", "image/webp", "application/pdf", "application/json", "application/x-yaml"} or ct.startswith("text/")):
            raise ValueError("Unsupported reference artifact")
        count, size = metadata.get("chunk_count"), metadata.get("size")
        if type(count) is not int or not 1 <= count <= 90 or type(size) is not int or not 1 <= size <= 10 * 1024 * 1024:
            raise ValueError("Invalid reference size")
        pieces = []
        for index in range(count):
            chunk = ref.collection("chunks").document(f"{index:04d}").get().to_dict()
            if not chunk or not isinstance(chunk.get("data"), str):
                raise ValueError("Reference upload is incomplete")
            pieces.append(base64.b64decode(chunk["data"], validate=True))
        data = b"".join(pieces)
        if len(data) != size:
            raise ValueError("Reference size does not match metadata")
        return str(metadata.get("name", "reference")), data


def _projects_config_path() -> Path:
    return Path.home() / ".config" / "orchestrator" / "projects.json"


def _load_projects() -> dict[str, Path]:
    projects: dict[str, Path] = {}
    cfg = _projects_config_path()
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str):
                        p = Path(v).expanduser().resolve()
                        if p.is_dir():
                            projects[k] = p
        except Exception:
            pass
    project_json = os.environ.get("ORCHESTRATOR_WEB_PROJECTS")
    if project_json:
        try:
            data = json.loads(project_json)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(k, str) and isinstance(v, str):
                        p = Path(v).expanduser().resolve()
                        if p.is_dir():
                            projects[k] = p
        except Exception:
            pass
    return projects


def _save_persistent_projects(projects: dict[str, Path]) -> None:
    try:
        cfg = _projects_config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({k: str(v) for k, v in projects.items()}, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    firebase_project = os.environ.get("ORCHESTRATOR_FIREBASE_PROJECT")
    owner_email = os.environ.get("ORCHESTRATOR_WEB_OWNER_EMAIL")
    if not firebase_project or not owner_email:
        raise SystemExit("Set ORCHESTRATOR_FIREBASE_PROJECT and ORCHESTRATOR_WEB_OWNER_EMAIL")
    projects = _load_projects()
    if not projects:
        raise SystemExit("No projects configured in ORCHESTRATOR_WEB_PROJECTS or ~/.config/orchestrator/projects.json")
    Worker(projects, FirebaseTransport(firebase_project), owner_email).run_forever()


if __name__ == "__main__":
    main()
