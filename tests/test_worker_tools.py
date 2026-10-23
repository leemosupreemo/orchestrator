from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import worker_tools  # noqa: E402


class WorkerToolsTests(unittest.TestCase):
    def test_remote_import_check_uses_configured_package_path(self) -> None:
        machine = {
            "name": "mac2",
            "execution_mode": "ssh",
            "ssh_target": "worker-host",
            "repo_path": "/Users/me/App",
            "orchestrator_package_path": "/opt/orchestrator",
        }

        command = worker_tools.remote_import_check_command(machine)

        self.assertIn("cd /Users/me/App", command)
        self.assertIn("PYTHONPATH=/opt/orchestrator:$PYTHONPATH", command)
        self.assertIn("import orchestrator.scripts.worker_run", command)

    @patch("worker_tools.subprocess.run")
    def test_check_remote_reports_missing_package(self, mock_run) -> None:
        mock_run.return_value = MagicMock(returncode=1, stderr="No module named orchestrator", stdout="")
        machine = {
            "name": "mac2",
            "execution_mode": "ssh",
            "ssh_target": "worker-host",
            "repo_path": "/Users/me/App",
        }

        result = worker_tools.check_remote(machine)

        self.assertEqual(result, 1)
        args = mock_run.call_args.args[0]
        self.assertEqual(args[:4], ["ssh", "-o", "BatchMode=yes", "-o"])
        self.assertIn("import orchestrator.scripts.worker_run", args[-1])
        self.assertIn("command -v xcodebuild", args[-1])

    @patch("worker_tools.check_remote", return_value=0)
    @patch("worker_tools.subprocess.run")
    def test_install_remote_syncs_package_then_checks_worker(self, mock_run, mock_check_remote) -> None:
        mock_run.return_value = MagicMock(returncode=0)
        machine = {
            "name": "mac2",
            "execution_mode": "ssh",
            "ssh_target": "worker-host",
            "repo_path": "/Users/me/App",
            "orchestrator_package_path": "/remote/package",
        }

        result = worker_tools.install_remote(machine)

        self.assertEqual(result, 0)
        mkdir_args = mock_run.call_args_list[0].args[0]
        rsync_args = mock_run.call_args_list[1].args[0]
        self.assertEqual(mkdir_args, ["ssh", "worker-host", "mkdir -p /remote/package"])
        self.assertEqual(rsync_args[0], "rsync")
        self.assertIn("--delete", rsync_args)
        self.assertEqual(rsync_args[-1], "worker-host:/remote/package/orchestrator/")
        mock_check_remote.assert_called_once_with(machine)


class WorkerCheckSummaryTests(unittest.TestCase):
    def test_summary_line_counts_ready_machines_and_names_the_others(self):
        with patch("worker_tools.sys.stdout.isatty", return_value=False):
            self.assertEqual(worker_tools.summary_line(2, 2, []), "✅ 2 of 2 machines ready")
            self.assertEqual(worker_tools.summary_line(1, 1, []), "✅ 1 of 1 machine ready")
            self.assertEqual(worker_tools.summary_line(2, 3, ["mini"]), "⚠️ 2 of 3 machines ready. Not ready: mini")
            self.assertEqual(worker_tools.summary_line(0, 2, ["a", "b"]), "❌ 0 of 2 machines ready. Not ready: a, b")
            self.assertIn("No machines are enabled", worker_tools.summary_line(0, 0, []))

    def test_summary_is_coloured_only_on_a_terminal(self):
        with patch("worker_tools.sys.stdout.isatty", return_value=True), patch.dict("os.environ", {}, clear=True):
            self.assertTrue(worker_tools.summary_line(1, 1, []).startswith("\033[92m"))
            self.assertTrue(worker_tools.summary_line(1, 2, ["x"]).startswith("\033[93m"))
            self.assertTrue(worker_tools.summary_line(0, 2, ["x", "y"]).startswith("\033[91m"))
        with patch("worker_tools.sys.stdout.isatty", return_value=True), patch.dict("os.environ", {"NO_COLOR": "1"}):
            self.assertNotIn("\033", worker_tools.summary_line(1, 1, []))

    def test_check_prints_the_summary_and_fails_when_a_machine_is_not_ready(self):
        machines = [{"name": "here", "execution_mode": "local", "repo_path": "/r"}, {"name": "far", "execution_mode": "ssh", "repo_path": "/r"}]
        out = io.StringIO()
        with patch("worker_tools.selected_machines", return_value=machines), patch("worker_tools.check_local", return_value=0), \
                patch("worker_tools.check_remote", return_value=1), patch("sys.argv", ["worker_tools.py", "check"]), redirect_stdout(out):
            code = worker_tools.main()
        self.assertEqual(code, 1)
        self.assertTrue(out.getvalue().rstrip().endswith("1 of 2 machines ready. Not ready: far"))
        out = io.StringIO()
        with patch("worker_tools.selected_machines", return_value=[]), patch("sys.argv", ["worker_tools.py", "check"]), redirect_stdout(out):
            self.assertEqual(worker_tools.main(), 0)
        self.assertIn("No machines are enabled", out.getvalue())


if __name__ == "__main__":
    unittest.main()

