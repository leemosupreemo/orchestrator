from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from .contract import Operation, validate_command
from .jobs import create_job, list_jobs, run_job_action
from .projection import project_job, redact_data, redact_text
from .tools import run_tool


class Transport(Protocol):
    def pending(self) -> list[str]: ...
    def interrupted(self) -> list[str]: ...
    def claim(self, command_id: str) -> dict[str, Any] | None: ...
    def finish(self, command_id: str, status: str, result: dict[str, Any]) -> None: ...
    def publish_job(self, project_id: str, job: dict[str, Any]) -> None: ...
    def heartbeat(self, projects: list[str]) -> None: ...


class Worker:
    def __init__(self, projects: dict[str, Path], transport: Transport, owner_uid: str):
        self.projects = {key: Path(root).resolve() for key, root in projects.items()}
        self.transport = transport
        self.owner_uid = owner_uid
        self._locks = {key: threading.Lock() for key in projects}

    def process(self, command_id: str) -> None:
        raw = self.transport.claim(command_id)
        if raw is None:
            return
        try:
            if raw.get("requested_by") != self.owner_uid:
                raise ValueError("Operator is not authorized")
            command = validate_command({key: raw[key] for key in
                                        ("project_id", "operation", "job_id", "inputs", "expected_revision") if key in raw})
            root = self.projects.get(command.project_id)
            if root is None:
                raise ValueError("Project is not configured on this worker")
            with self._locks[command.project_id]:
                if command.operation == Operation.JOB_CREATE:
                    job = create_job(root, command.inputs)
                    result = {"job_id": job["job_id"]}
                    self.transport.publish_job(command.project_id, project_job(job))
                elif command.operation.value.startswith("job."):
                    job = run_job_action(root, command.job_id, command.operation,
                                         command.inputs, command.expected_revision)
                    result = {"job_id": job["job_id"], "revision": job["revision"]}
                    self.transport.publish_job(command.project_id, project_job(job))
                elif command.operation == Operation.TESTS_RUN:
                    result = run_tool(root, Operation.TOOL_SELF_TESTS, {})
                elif command.operation == Operation.PROJECT_REFRESH:
                    self.reconcile()
                    result = {"refreshed": True}
                else:
                    result = run_tool(root, command.operation, command.inputs)
            self.transport.finish(command_id, "succeeded", redact_data(result))
        except Exception as exc:
            self.transport.finish(command_id, "failed", {"error": redact_text(str(exc))})

    def reconcile(self) -> None:
        for project_id, root in self.projects.items():
            for job in list_jobs(root):
                self.transport.publish_job(project_id, project_job(job))
        self.transport.heartbeat(sorted(self.projects))

    def recover(self) -> None:
        for command_id in self.transport.interrupted():
            self.transport.finish(command_id, "failed", {
                "error": "Worker stopped during this operation. Review local project state before retrying."})

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        self.recover()
        while True:
            self.reconcile()
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

    def publish_job(self, project_id: str, job: dict[str, Any]) -> None:
        self.db.collection("projects").document(project_id).collection("jobs").document(job["job_id"]).set(job)

    def heartbeat(self, projects: list[str]) -> None:
        self.db.collection("workers").document("mac").set({
            "project_ids": projects, "last_seen": self.firestore.SERVER_TIMESTAMP})


def main() -> None:
    firebase_project = os.environ.get("ORCHESTRATOR_FIREBASE_PROJECT")
    owner_uid = os.environ.get("ORCHESTRATOR_WEB_OWNER_UID")
    project_json = os.environ.get("ORCHESTRATOR_WEB_PROJECTS")
    if not firebase_project or not owner_uid or not project_json:
        raise SystemExit("Set ORCHESTRATOR_FIREBASE_PROJECT, ORCHESTRATOR_WEB_OWNER_UID, and ORCHESTRATOR_WEB_PROJECTS")
    projects = json.loads(project_json)
    if not isinstance(projects, dict) or not projects:
        raise SystemExit("ORCHESTRATOR_WEB_PROJECTS must map project IDs to local paths")
    Worker({key: Path(value) for key, value in projects.items()},
           FirebaseTransport(firebase_project), owner_uid).run_forever()


if __name__ == "__main__":
    main()
