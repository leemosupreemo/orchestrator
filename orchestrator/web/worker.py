from __future__ import annotations

import json
import base64
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from .contract import Operation, validate_command
from .jobs import create_job, list_jobs, list_archived_jobs, run_job_action
from .projection import project_job, redact_data, redact_text
from .tools import run_tool, run_app_tests, run_refresh


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
    def heartbeat(self, projects: list[str]) -> None: ...
    def fetch_artifact(self, project_id: str, artifact_id: str) -> tuple[str, bytes]: ...
    def progress(self, command_id: str, output: str) -> None: ...


class Worker:
    def __init__(self, projects: dict[str, Path], transport: Transport, owner_email: str):
        self.projects = {key: Path(root).resolve() for key, root in projects.items()}
        self.transport = transport
        self.owner_email = owner_email
        self._locks = {key: threading.Lock() for key in projects}

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
                    job = create_job(root, command.inputs, runner=runner)
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
                    if (root / ".orchestrator" / "jobs" / f"{job['job_id']}.json").exists():
                        self.transport.publish_job(command.project_id, project_job(job))
                    else:
                        self.transport.publish_archive(command.project_id, project_job(job))
                        self.transport.remove_job(command.project_id, job["job_id"])
                elif command.operation == Operation.TESTS_RUN:
                    result = run_app_tests(root, command.inputs)
                elif command.operation == Operation.PROJECT_REFRESH:
                    result = run_refresh(root)
                    self.reconcile()
                else:
                    result = run_tool(root, command.operation, command.inputs)
            status = "failed" if isinstance(result, dict) and result.get("exit_code", 0) != 0 else "succeeded"
            self.transport.finish(command_id, status, redact_data(result))
        except Exception as exc:
            self.transport.finish(command_id, "failed", {"error": redact_text(str(exc))})

    def reconcile(self) -> None:
        for project_id, root in self.projects.items():
            active = list_jobs(root)
            for job in active:
                self.transport.publish_job(project_id, project_job(job))
            archived = list_archived_jobs(root)
            for job in archived:
                self.transport.publish_archive(project_id, project_job(job))
            for old_id in self.transport.active_ids(project_id) - {job["job_id"] for job in active}:
                self.transport.remove_job(project_id, old_id)
            for old_id in self.transport.archived_ids(project_id) - {job["job_id"] for job in archived}:
                self.transport.remove_archive(project_id, old_id)
        self.transport.heartbeat(sorted(self.projects))

    def recover(self) -> None:
        for command_id in self.transport.interrupted():
            self.transport.finish(command_id, "failed", {
                "error": "Worker stopped during this operation. Review local project state before retrying."})

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        self.recover()
        def heartbeat_loop() -> None:
            while True:
                try:
                    self.transport.heartbeat(sorted(self.projects))
                except Exception:
                    pass
                time.sleep(10)
        threading.Thread(target=heartbeat_loop, daemon=True).start()
        next_reconcile = 0.0
        while True:
            if time.monotonic() >= next_reconcile:
                self.reconcile()
                next_reconcile = time.monotonic() + 15
            for command_id in self.transport.pending():
                self.process(command_id)
            time.sleep(poll_seconds)


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

    def heartbeat(self, projects: list[str]) -> None:
        self.db.collection("workers").document("mac").set({
            "project_ids": projects, "last_seen": self.firestore.SERVER_TIMESTAMP})

    def fetch_artifact(self, project_id: str, artifact_id: str) -> tuple[str, bytes]:
        if not artifact_id or len(artifact_id) > 128 or not all(c.isalnum() or c in "_-" for c in artifact_id):
            raise ValueError("Invalid artifact ID")
        ref = self.db.collection("projects").document(project_id).collection("artifacts").document(artifact_id)
        metadata = ref.get().to_dict()
        if not metadata or metadata.get("content_type") not in {"image/png", "image/jpeg", "image/webp", "application/pdf", "text/plain"}:
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


def main() -> None:
    firebase_project = os.environ.get("ORCHESTRATOR_FIREBASE_PROJECT")
    owner_email = os.environ.get("ORCHESTRATOR_WEB_OWNER_EMAIL")
    project_json = os.environ.get("ORCHESTRATOR_WEB_PROJECTS")
    if not firebase_project or not owner_email or not project_json:
        raise SystemExit("Set ORCHESTRATOR_FIREBASE_PROJECT, ORCHESTRATOR_WEB_OWNER_EMAIL, and ORCHESTRATOR_WEB_PROJECTS")
    projects = json.loads(project_json)
    if not isinstance(projects, dict) or not projects:
        raise SystemExit("ORCHESTRATOR_WEB_PROJECTS must map project IDs to local paths")
    Worker({key: Path(value) for key, value in projects.items()},
           FirebaseTransport(firebase_project), owner_email).run_forever()


if __name__ == "__main__":
    main()
