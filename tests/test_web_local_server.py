from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.web.local_server import (
    LocalCommandRunner,
    discover_local_projects,
    get_static_dir,
)


class LocalServerTests(unittest.TestCase):
    def test_discover_local_projects_explicit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orch-proj-") as tmp:
            p = Path(tmp)
            projects = discover_local_projects(p)
            self.assertIn(p.name, projects)
            self.assertEqual(projects[p.name], p.resolve())

    def test_local_command_runner_job_flow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orch-proj-") as tmp:
            p = Path(tmp)
            runtime = p / ".orchestrator"
            runtime.mkdir()
            (runtime / "project.json").write_text(json.dumps({"project_name": "TestProj", "base_branch": "main"}))
            (runtime / "config").mkdir()
            (runtime / "config" / "machines.json").write_text(json.dumps({"machines": [{"name": "mac1", "repo_path": str(p)}]}))
            (runtime / "config" / "settings.json").write_text(json.dumps({}))

            runner = LocalCommandRunner({"test": p})

            # 1. Store artifact
            runner.store_artifact("art-123", "spec.md", b"# Test Spec\nDo something.")
            self.assertIn("art-123", runner.artifacts)

            # 2. Add job.create command
            cmd = runner.add_command({
                "operation": "job.create",
                "project_id": "test",
                "inputs": {
                    "title": "Local Test Job",
                    "plan": "Test plan content",
                    "artifact_id": "art-123",
                }
            })
            self.assertIn("id", cmd)
            self.assertIn(cmd["status"], ["queued", "running", "succeeded"])

    def test_get_static_dir_fallback(self) -> None:
        static_dir = get_static_dir()
        self.assertTrue(static_dir.is_dir() or str(static_dir).endswith("dist"))
