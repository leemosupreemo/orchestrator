from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from orchestrator import cli, service  # noqa: E402


class FakeRun:
    """Records the commands the service module would run, and answers them."""

    def __init__(self, answers=None):
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        for prefix, result in self.answers.items():
            if " ".join(argv).startswith(prefix):
                return result
        return CompletedProcess(argv, 0, "", "")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.state = base / "state"
        self.project = base / "project"
        (self.project / ".orchestrator").mkdir(parents=True)
        (self.project / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "Demo"}))
        self.patches = [
            patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(self.state), "PATH": "/opt/homebrew/bin:/usr/bin"}),
            patch.object(service, "launch_agents_dir", return_value=self.home / "LaunchAgents"),
            patch.object(service, "systemd_user_dir", return_value=self.home / "systemd"),
            patch.object(service, "active_project_root", return_value=self.project),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.addCleanup(self.tmp.cleanup)

    def spec(self, **kwargs):
        return service.make_spec(**kwargs)

    def test_the_launchd_agent_runs_the_ui_with_the_callers_path(self):
        spec = self.spec()
        plist = plistlib.loads(service.render_launchd(spec))
        self.assertEqual(plist["Label"], "com.orchestrator.ui")
        self.assertEqual(plist["ProgramArguments"], [sys.executable, "-m", "orchestrator", "ui", "--no-open", "--tunnel"])
        self.assertEqual(plist["EnvironmentVariables"]["PATH"], "/opt/homebrew/bin:/usr/bin")  # so cloudflared, gh, claude… are found
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["ThrottleInterval"], 30)
        self.assertEqual(Path(plist["StandardOutPath"]), self.state.resolve() / "logs" / "service.log")

    def test_keep_awake_wraps_the_command_in_caffeinate(self):
        plist = plistlib.loads(service.render_launchd(self.spec(keep_awake=True)))
        self.assertEqual(plist["ProgramArguments"][:2], ["/usr/bin/caffeinate", "-i"])
        self.assertEqual(plist["ProgramArguments"][2], sys.executable)

    def test_the_systemd_unit_quotes_paths_and_restarts(self):
        unit = service.render_systemd(service.Spec(python="/opt/my python/bin/python3", workdir=Path("/home/me/.orchestrator/service"),
                                                   log=Path("/home/me/.orchestrator/logs/service.log"), path="/usr/bin:/bin"))
        self.assertIn('ExecStart="/opt/my python/bin/python3" "-m" "orchestrator" "ui" "--no-open" "--tunnel"', unit)
        self.assertIn('Environment="PATH=/usr/bin:/bin"', unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("WantedBy=default.target", unit)

    def test_install_on_macos_writes_the_agent_and_loads_it(self):
        run = FakeRun()
        notes = service.install(self.spec(), system="Darwin", run=run)
        path = self.home / "LaunchAgents" / "com.orchestrator.ui.plist"
        self.assertTrue(path.is_file())
        domain = f"gui/{os.getuid()}"
        self.assertEqual(run.calls, [["launchctl", "bootout", f"{domain}/com.orchestrator.ui"],
                                     ["launchctl", "bootstrap", domain, str(path)]])
        self.assertTrue(any(self.project.name in n for n in notes))
        self.assertEqual(oct((self.state.resolve() / "logs" / "service.log").stat().st_mode & 0o777), "0o600")  # holds the access address

    def test_install_on_linux_enables_the_unit(self):
        run = FakeRun()
        notes = service.install(self.spec(), system="Linux", run=run)
        self.assertTrue((self.home / "systemd" / "orchestrator-ui.service").is_file())
        self.assertEqual(run.calls, [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "orchestrator-ui.service"]])
        self.assertTrue(any("enable-linger" in n for n in notes))

    def test_install_needs_a_project_to_open(self):
        with patch.object(service, "active_project_root", return_value=None):
            with self.assertRaises(service.ServiceError) as caught:
                service.install(self.spec(), system="Darwin", run=FakeRun())
        self.assertIn("no project", str(caught.exception))
        self.assertFalse((self.home / "LaunchAgents").exists())  # nothing half-installed

    def test_a_failing_launchctl_is_reported(self):
        run = FakeRun({"launchctl bootstrap": CompletedProcess([], 5, "", "Bootstrap failed: 5: Input/output error")})
        with self.assertRaises(service.ServiceError) as caught:
            service.install(self.spec(), system="Darwin", run=run)
        self.assertIn("Input/output error", str(caught.exception))

    def test_other_systems_are_told_plainly(self):
        with self.assertRaises(service.ServiceError) as caught:
            service.install(self.spec(), system="Windows", run=FakeRun())
        self.assertIn("Windows", str(caught.exception))

    def test_uninstall_stops_and_removes(self):
        run = FakeRun()
        self.assertFalse(service.uninstall(system="Darwin", run=run))  # nothing there
        service.install(self.spec(), system="Darwin", run=run)
        run.calls.clear()
        self.assertTrue(service.uninstall(system="Darwin", run=run))
        self.assertEqual(run.calls, [["launchctl", "bootout", f"gui/{os.getuid()}/com.orchestrator.ui"]])
        self.assertFalse((self.home / "LaunchAgents" / "com.orchestrator.ui.plist").exists())

    def test_status(self):
        self.assertEqual(service.status(system="Darwin", run=FakeRun())["installed"], False)
        service.install(self.spec(), system="Darwin", run=FakeRun())
        running = FakeRun({"launchctl print": CompletedProcess([], 0, "com.orchestrator.ui = {\n\tstate = running\n\tpid = 4242\n}", "")})
        info = service.status(system="Darwin", run=running)
        self.assertEqual((info["installed"], info["running"]), (True, True))
        self.assertIn("4242", info["detail"])
        waiting = FakeRun({"launchctl print": CompletedProcess([], 0, "state = waiting", "")})
        self.assertFalse(service.status(system="Darwin", run=waiting)["running"])
        unloaded = FakeRun({"launchctl print": CompletedProcess([], 113, "", "Could not find service")})
        self.assertIn("not loaded", service.status(system="Darwin", run=unloaded)["detail"])

    def test_the_cli_exposes_service_commands(self):
        with patch.object(cli, "service_command", return_value=0) as command:
            self.assertEqual(cli.main(["service", "install", "--keep-awake"]), 0)
            command.assert_called_with("install", True)
            self.assertEqual(cli.main(["service", "status"]), 0)
            command.assert_called_with("status", False)


if __name__ == "__main__":
    unittest.main()
