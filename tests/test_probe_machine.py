from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import probe_machine  # noqa: E402


class ProbeMachineTests(unittest.TestCase):
    @patch("probe_machine._provider_auth_ready", side_effect=lambda cli, home: cli == "claude")
    def test_llm_tool_readiness_reports_installed_and_authenticated_providers(self, _auth):
        status = probe_machine.llm_provider_status({"claude": True, "codex": True, "opencode": False}, Path("/tmp/worker-home"))
        self.assertEqual(status["claude"], {"installed": True, "ready": True})
        self.assertEqual(status["codex"], {"installed": True, "ready": False})
        self.assertEqual(status["opencode"], {"installed": False, "ready": False})

    @patch("probe_machine.count_matching_processes", return_value=0)
    @patch("probe_machine.check_stale_processes", return_value=[])
    @patch("probe_machine.detect_codex_integration", return_value=False)
    @patch("probe_machine.probe_disk_space", return_value=(0, 0))
    @patch("probe_machine.probe_hardware_specs", return_value={"cpu_model": "M4", "logical_cores": 10, "physical_cores": 10, "total_mem_gb": 32})
    @patch("probe_machine.probe_mem_free_mb", return_value=(8192, "normal"))
    @patch("shutil.which", return_value="/usr/bin/tool")
    def test_probe_local_missing_repo_still_reports_tools(
        self,
        _mock_which,
        _mock_mem,
        _mock_hw,
        _mock_disk,
        _mock_codex,
        _mock_stale,
        _mock_processes,
    ):
        payload = probe_machine.probe_local("mac2", "/path/that/does/not/exist")

        self.assertTrue(payload["reachable"])
        self.assertFalse(payload["repo_path_ok"])
        self.assertEqual(payload["repo_path"], "/path/that/does/not/exist")
        self.assertIn("Configured repo path does not exist", payload["repo_warning"])
        self.assertTrue(payload["binaries"]["xcodebuild"])

    @patch("probe_machine.subprocess.run")
    def test_probe_remote_does_not_cd_into_repo_before_probe(self, mock_run):
        remote_payload = {
            "machine": "mac2",
            "reachable": True,
            "repo_path": "/missing/repo",
            "repo_path_ok": False,
            "binaries": {"xcodebuild": True},
        }

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "ssh" and "python3 /tmp/orchestrator_probe_mac2/probe_machine.py" in cmd[-1]:
                return MagicMock(returncode=0, stdout=json.dumps(remote_payload), stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = fake_run
        machine = {
            "name": "mac2",
            "execution_mode": "ssh",
            "ssh_target": "worker-host",
            "repo_path": "/missing/repo",
        }

        payload = probe_machine.probe_remote(machine)

        self.assertTrue(payload["reachable"])
        self.assertFalse(payload["repo_path_ok"])
        ssh_probe_cmd = next(
            call.args[0][-1]
            for call in mock_run.call_args_list
            if call.args[0][0] == "ssh" and "probe_machine.py --probe-local" in call.args[0][-1]
        )
        self.assertNotIn("cd /missing/repo", ssh_probe_cmd)
        self.assertIn("--repo-path /missing/repo", ssh_probe_cmd)

    def test_probe_local_script_runs_without_common_dependency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_script = Path(temp_dir) / "probe_machine.py"
            shutil.copy2(SCRIPTS_DIR / "probe_machine.py", temp_script)

            result = subprocess.run(
                [
                    sys.executable,
                    str(temp_script),
                    "--probe-local",
                    "--machine-name",
                    "mac2",
                    "--repo-path",
                    "/path/that/does/not/exist",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["reachable"])
        self.assertFalse(payload["repo_path_ok"])
        self.assertEqual(payload["repo_path"], "/path/that/does/not/exist")


if __name__ == "__main__":
    unittest.main()
