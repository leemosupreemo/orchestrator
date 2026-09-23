from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.web.jobs import get_job
from orchestrator.web.projection import project_job, redact_text
from orchestrator.web.worker import Worker


class FakeTransport:
    def __init__(self, commands=None):
        self.commands = commands or {}
        self.results = {}
        self.jobs = {}
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

    def heartbeat(self, projects):
        self.heartbeats.append(projects)

    def interrupted(self):
        return [key for key, command in self.commands.items() if command.get("status") == "running"]


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

    def test_projection_redacts_secret_patterns(self):
        self.assertNotIn("verysecret", json.dumps(project_job(get_job(self.root, "job-1"))))
        self.assertEqual(redact_text("Bearer topsecret123"), "Bearer [REDACTED]")

    def test_recovery_marks_interrupted_command_for_review_without_replaying(self):
        command = self.command()
        command["status"] = "running"
        transport = FakeTransport({"cmd-1": command})
        Worker({"test": self.root}, transport, owner_email="owner").recover()
        self.assertEqual(transport.results["cmd-1"]["status"], "failed")
        self.assertIn("review", transport.results["cmd-1"]["result"]["error"].lower())
        self.assertNotIn("last_manual_log_paths", json.loads(self.path.read_text()))


if __name__ == "__main__":
    unittest.main()
