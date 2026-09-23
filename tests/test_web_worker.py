from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.web.jobs import get_job
from orchestrator.web.projection import project_job, redact_text
from orchestrator.web.worker import Worker


class FakeTransport:
    def __init__(self, commands=None):
        self.commands = commands or {}
        self.results = {}
        self.jobs = {}
        self.archives = {}
        self.heartbeats = []

    def claim(self, command_id):
        command = self.commands.get(command_id)
        if not command or command.get("status") != "queued":
            return None
        command["status"] = "running"
        return dict(command)

    def finish(self, command_id, status, result):
        self.results[command_id] = {"status": status, "result": result}

    def publish_job(self, project_id, job):
        self.jobs[(project_id, job["job_id"])] = job

    def publish_archive(self, project_id, job):
        self.archives[(project_id, job["job_id"])] = job

    def active_ids(self, project_id):
        return {job_id for item_project, job_id in self.jobs if item_project == project_id}

    def remove_job(self, project_id, job_id):
        self.jobs.pop((project_id, job_id), None)

    def archived_ids(self, project_id):
        return {job_id for item_project, job_id in self.archives if item_project == project_id}

    def remove_archive(self, project_id, job_id):
        self.archives.pop((project_id, job_id), None)

    def heartbeat(self, projects):
        self.heartbeats.append(projects)

    def interrupted(self):
        return [key for key, command in self.commands.items() if command.get("status") == "running"]

    def fetch_artifact(self, project_id, artifact_id):
        return "lobby.txt", b"Reference content"


class WebWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        directory = self.root / ".orchestrator" / "jobs"
        directory.mkdir(parents=True)
        self.path = directory / "job-1.json"
        self.path.write_text(json.dumps({"job_id": "job-1", "title": "Lobby", "type": "bug",
                                         "status": "planned", "raw_input": "API_KEY=verysecret"}))

    def tearDown(self):
        self.temp.cleanup()

    def command(self):
        return {"status": "queued", "requested_by": "owner", "project_id": "test",
                "operation": "job.link_logs", "job_id": "job-1", "inputs": {"log_paths": ["logs/a.log"]},
                "expected_revision": get_job(self.root, "job-1")["revision"]}

    def test_worker_executes_once_and_publishes_canonical_job(self):
        transport = FakeTransport({"cmd-1": self.command()})
        worker = Worker({"test": self.root}, transport, owner_email="owner")
        worker.process("cmd-1")
        worker.process("cmd-1")
        self.assertEqual(json.loads(self.path.read_text())["last_manual_log_paths"], ["logs/a.log"])
        self.assertEqual(transport.results["cmd-1"]["status"], "succeeded")
        self.assertNotIn("raw_input", transport.jobs[("test", "job-1")])

    def test_unprovisioned_user_cannot_execute(self):
        command = self.command()
        command["requested_by"] = "stranger"
        transport = FakeTransport({"cmd-1": command})
        Worker({"test": self.root}, transport, owner_email="owner").process("cmd-1")
        self.assertEqual(transport.results["cmd-1"]["status"], "failed")
        self.assertNotIn("last_manual_log_paths", json.loads(self.path.read_text()))

    def test_stale_revision_fails_without_mutation(self):
        command = self.command()
        command["expected_revision"] = 0
        transport = FakeTransport({"cmd-1": command})
        Worker({"test": self.root}, transport, owner_email="owner").process("cmd-1")
        self.assertEqual(transport.results["cmd-1"]["status"], "failed")
        self.assertNotIn("last_manual_log_paths", json.loads(self.path.read_text()))

    def test_reconcile_publishes_cli_changes_and_heartbeat(self):
        transport = FakeTransport()
        worker = Worker({"test": self.root}, transport, owner_email="owner")
        worker.reconcile()
        self.assertIn(("test", "job-1"), transport.jobs)
        self.assertEqual(transport.heartbeats[-1], ["test"])

    def test_reconcile_moves_cli_archived_job_out_of_active_projection(self):
        transport = FakeTransport()
        worker = Worker({"test": self.root}, transport, owner_email="owner")
        worker.reconcile()
        archive = self.path.parent / "archive"
        archive.mkdir()
        self.path.replace(archive / self.path.name)
        worker.reconcile()
        self.assertNotIn(("test", "job-1"), transport.jobs)
        self.assertIn(("test", "job-1"), transport.archives)
        (archive / self.path.name).replace(self.path)
        worker.reconcile()
        self.assertIn(("test", "job-1"), transport.jobs)
        self.assertNotIn(("test", "job-1"), transport.archives)

    def test_failed_app_test_command_is_reported_failed(self):
        command = {"status": "queued", "requested_by": "owner", "project_id": "test",
                   "operation": "tests.run", "inputs": {"action": "visual"}}
        transport = FakeTransport({"cmd-1": command})
        with patch("orchestrator.web.worker.run_app_tests", return_value={"exit_code": 1, "output": "Xcode test failed"}):
            Worker({"test": self.root}, transport, owner_email="owner").process("cmd-1")
        self.assertEqual(transport.results["cmd-1"]["status"], "failed")

    def test_projection_redacts_secret_patterns(self):
        projection = project_job(get_job(self.root, "job-1"))
        self.assertNotIn("verysecret", json.dumps(projection))
        self.assertIn("job.schedule", {item["operation"] for item in projection["actions"]})
        self.assertEqual(redact_text("Bearer topsecret123"), "Bearer [REDACTED]")

    def test_recovery_marks_interrupted_command_for_review_without_replaying(self):
        command = self.command()
        command["status"] = "running"
        transport = FakeTransport({"cmd-1": command})
        Worker({"test": self.root}, transport, owner_email="owner").recover()
        self.assertEqual(transport.results["cmd-1"]["status"], "failed")
        self.assertIn("review", transport.results["cmd-1"]["result"]["error"].lower())
        self.assertNotIn("last_manual_log_paths", json.loads(self.path.read_text()))

    def test_uploaded_reference_reaches_canonical_job(self):
        command = self.command()
        command["operation"] = "job.attach_reference"
        command["inputs"] = {"artifact_id": "upload-1", "description": "Lobby layout"}
        transport = FakeTransport({"cmd-1": command})
        Worker({"test": self.root}, transport, owner_email="owner").process("cmd-1")
        self.assertEqual(transport.results["cmd-1"]["status"], "succeeded")
        job = json.loads(self.path.read_text())
        file_path = self.root / job["reference_artifacts"][0]["path"]
        self.assertEqual(file_path.read_bytes(), b"Reference content")


if __name__ == "__main__":
    unittest.main()
