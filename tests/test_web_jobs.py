from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from orchestrator.web.contract import Operation
from orchestrator.web.jobs import create_job, get_job, list_jobs, run_job_action, _validate_public_https_url


class WebJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.jobs = self.root / ".orchestrator" / "jobs"
        self.jobs.mkdir(parents=True)
        self.path = self.jobs / "job-1.json"
        self.path.write_text(json.dumps({"job_id": "job-1", "status": "planned", "type": "bug", "title": "Fix lobby"}))

    def tearDown(self):
        self.temp.cleanup()

    def test_job_list_and_read_use_local_files(self):
        self.assertEqual([job["job_id"] for job in list_jobs(self.root)], ["job-1"])
        self.assertEqual(get_job(self.root, "job-1")["title"], "Fix lobby")
        with self.assertRaises(ValueError):
            get_job(self.root, "../secrets")

    def test_link_logs_updates_canonical_file(self):
        revision = get_job(self.root, "job-1")["revision"]
        result = run_job_action(self.root, "job-1", Operation.JOB_LINK_LOGS,
                                {"log_paths": ["logs/failure.log"]}, revision)
        self.assertEqual(result["last_manual_log_paths"], ["logs/failure.log"])
        self.assertEqual(json.loads(self.path.read_text())["last_manual_log_paths"], ["logs/failure.log"])

    def test_stale_revision_does_not_change_job(self):
        before = self.path.read_text()
        with self.assertRaises(ValueError):
            run_job_action(self.root, "job-1", Operation.JOB_LINK_LOGS,
                           {"log_paths": ["logs/failure.log"]}, -1)
        self.assertEqual(self.path.read_text(), before)

    def test_invalid_action_does_not_launch_script(self):
        called = []
        with self.assertRaises(ValueError):
            run_job_action(self.root, "job-1", Operation.JOB_MERGE, {},
                           get_job(self.root, "job-1")["revision"], runner=lambda *args: called.append(args))
        self.assertEqual(called, [])

    def test_schedule_uses_existing_script(self):
        called = []
        def runner(argv, *, cwd, env):
            called.append((argv, cwd, env["ORCHESTRATOR_PROJECT_ROOT"]))
            return 0
        run_job_action(self.root, "job-1", Operation.JOB_SCHEDULE, {},
                       get_job(self.root, "job-1")["revision"], runner=runner)
        self.assertEqual(Path(called[0][0][1]).name, "schedule_job.py")
        self.assertEqual(called[0][1], self.root)

    def test_revise_passes_feedback_to_real_planner(self):
        called = []
        def runner(argv, *, cwd, env):
            called.append(argv)
            return 0
        run_job_action(self.root, "job-1", Operation.JOB_REVISE,
                       {"feedback": "Make the retry visible"}, get_job(self.root, "job-1")["revision"], runner=runner)
        self.assertEqual(Path(called[0][1]).name, "new_job.py")
        self.assertIn("Make the retry visible", called[0])

    def test_model_override_writes_canonical_job(self):
        result = run_job_action(self.root, "job-1", Operation.JOB_MODELS,
                                {"allowed_models": ["codex"]}, get_job(self.root, "job-1")["revision"])
        self.assertEqual(result["allowed_models"], ["codex"])

    def test_url_reference_is_persisted(self):
        result = run_job_action(self.root, "job-1", Operation.JOB_ATTACH_REFERENCE,
                                {"source_url": "https://example.com/mockup", "description": "Lobby"},
                                get_job(self.root, "job-1")["revision"])
        self.assertEqual(result["reference_artifacts"][0]["url"], "https://example.com/mockup")

    def test_create_job_supplies_noninteractive_input_to_existing_planner(self):
        payloads = []
        def runner(argv, *, cwd, env):
            index = argv.index("--web-input-file")
            payloads.append(json.loads(Path(argv[index + 1]).read_text()))
            (self.jobs / "job-2.json").write_text(json.dumps({"job_id": "job-2", "status": "planned", "type": "feature-plan"}))
            return 0
        job = create_job(self.root, {"job_type": "feature", "prompt": "Add a lobby view",
                                     "allowed_models": ["codex"], "allowed_machines": ["local"],
                                     "branch_mode": "new", "yolo": False}, runner=runner)
        self.assertEqual(job["job_id"], "job-2")
        self.assertEqual(payloads[0]["prompt"], "Add a lobby view")

    def test_create_job_rejects_missing_prompt_without_launching(self):
        with self.assertRaises(ValueError):
            create_job(self.root, {"job_type": "feature", "prompt": ""}, runner=lambda *args, **kwargs: self.fail("launched"))

    def test_create_job_rejects_spec_outside_project(self):
        base = {"job_type": "feature", "prompt": "Add a lobby", "allowed_models": ["codex"],
                "allowed_machines": ["local"]}
        for source in ("/etc/hosts", "../outside.md", "http://127.0.0.1/private"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                create_job(self.root, {**base, "spec_url": source}, runner=lambda *args, **kwargs: self.fail("launched"))

    def test_public_spec_is_fetched_to_private_file_before_planning(self):
        captured = []
        def runner(argv, *, cwd, env):
            path = Path(argv[argv.index("--spec-file") + 1])
            captured.append(path.read_text())
            (self.jobs / "job-2.json").write_text(json.dumps({"job_id": "job-2", "status": "planned"}))
            return 0
        options = {"job_type": "feature", "prompt": "Add a lobby", "allowed_models": ["codex"],
                   "allowed_machines": ["local"], "spec_url": "https://example.com/spec"}
        with patch.dict(create_job.__globals__, {"_validate_public_https_url": lambda _: None,
                                                 "_fetch_public_spec": lambda _: "Approved spec"}):
            create_job(self.root, options, runner=runner)
        self.assertEqual(captured, ["Approved spec"])

    def test_private_dns_spec_host_is_rejected(self):
        with patch("orchestrator.web.jobs.socket.getaddrinfo", return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(ValueError):
                _validate_public_https_url("https://internal.example/spec")


if __name__ == "__main__":
    unittest.main()
