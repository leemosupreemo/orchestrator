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
        self.assertEqual(plist["ProgramArguments"], [sys.executable, "-m", "orchestrator", "ui", "--no-open", "--managed-connection"])
        self.assertEqual(plist["EnvironmentVariables"]["PATH"], "/opt/homebrew/bin:/usr/bin")  # so cloudflared, gh, claude… are found
        self.assertTrue(plist["RunAtLoad"])
        self.assertTrue(plist["KeepAlive"])
        self.assertEqual(plist["ThrottleInterval"], 30)
        self.assertEqual(Path(plist["StandardOutPath"]), self.state.resolve() / "logs" / "service.log")

    def test_keep_awake_wraps_the_command_in_caffeinate(self):
        plist = plistlib.loads(service.render_launchd(self.spec(keep_awake=True)))
        self.assertEqual(plist["ProgramArguments"][:2], ["/usr/bin/caffeinate", "-i"])
        self.assertEqual(plist["ProgramArguments"][2], sys.executable)

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

    def test_reinstall_adopts_existing_explicit_public_url(self):
        from orchestrator.connection_manager import ConnectionStore
        path = service.definition_path('Darwin')
        path.parent.mkdir(parents=True)
        path.write_bytes(plistlib.dumps({'ProgramArguments': ['/python', '-m', 'orchestrator', 'ui', '--public-url', 'https://machine.tail123.ts.net']}))
        service.install(self.spec(), system='Darwin', run=FakeRun())
        store = ConnectionStore()
        self.assertEqual(store.data['selected'], 'funnel')
        self.assertEqual(store.data['providers']['funnel']['url'], 'https://machine.tail123.ts.net')
        self.assertIn('--managed-connection', plistlib.loads(path.read_bytes())['ProgramArguments'])
        service.install(self.spec(), system='Darwin', run=FakeRun())
        self.assertEqual(ConnectionStore().data['selected'], 'funnel')

    def test_install_needs_a_project_to_open(self):
        with patch.object(service, "active_project_root", return_value=None):
            with self.assertRaises(service.ServiceError) as caught:
                service.install(self.spec(), system="Darwin", run=FakeRun())
        self.assertIn("no project", str(caught.exception))
        self.assertFalse((self.home / "LaunchAgents").exists())  # nothing half-installed

    def test_service_falls_back_when_active_project_has_no_configuration(self):
        self.state.mkdir(parents=True)
        (self.state / 'projects.json').write_text(json.dumps({'version': 1, 'active': 'Unconfigured', 'projects': [
            {'name': 'Demo', 'root': str(self.project)},
        ]}))
        with patch.object(service, 'active_project_root', return_value=self.home / 'unconfigured'):
            self.assertEqual(service.service_project(self.spec().workdir), self.project.resolve())

    def test_background_ui_can_start_after_active_project_configuration_is_removed(self):
        from orchestrator.web import server
        workdir = self.spec().workdir
        workdir.mkdir(parents=True)
        with patch.object(Path, 'cwd', return_value=workdir), \
             patch.object(server, 'find_project_root', return_value=self.home / 'unconfigured'), \
             patch.object(server, '_serve', return_value=0) as serve:
            self.assertEqual(server.main(['--no-open']), 0)
        self.assertEqual(serve.call_args.args[1], self.project)

    def test_background_ui_does_not_replace_an_explicit_project(self):
        from orchestrator.web import server
        workdir = self.spec().workdir
        workdir.mkdir(parents=True)
        (workdir.parent / 'projects.json').write_text(json.dumps({'version': 1, 'active': 'Demo', 'projects': [
            {'name': 'Demo', 'root': str(self.project)},
        ]}))
        missing = self.home / 'unconfigured'
        with patch.object(Path, 'cwd', return_value=workdir), \
             patch.dict(os.environ, {'ORCHESTRATOR_PROJECT_ROOT': str(missing)}), \
             patch.object(server, '_serve') as serve:
            self.assertEqual(server.main(['--no-open']), 1)
        serve.assert_not_called()

    def test_a_failing_launchctl_is_reported(self):
        run = FakeRun({"launchctl bootstrap": CompletedProcess([], 5, "", "Bootstrap failed: 5: Input/output error")})
        with patch("time.sleep"), self.assertRaises(service.ServiceError) as caught:
            service.install(self.spec(), system="Darwin", run=run)
        self.assertIn("Input/output error", str(caught.exception))

    def test_existing_named_cloudflare_token_is_adopted_privately(self):
        from orchestrator.connection_manager import ConnectionStore
        for flags in (['--tunnel-token','secret-token'],['--tunnel-token=secret-token']):
            path=service.definition_path('Darwin'); path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(plistlib.dumps({'ProgramArguments':['python','-m','orchestrator','ui','--tunnel']+flags}))
            service.install(self.spec(),system='Darwin',run=FakeRun())
            store=ConnectionStore()
            self.assertEqual(store.data['providers']['cloudflare']['token'],'secret-token')
            self.assertNotIn('secret-token',path.read_text())
            self.assertEqual(store.path.stat().st_mode & 0o777,0o600)

    def test_failed_provider_adoption_preserves_installed_definition(self):
        from orchestrator.connection_manager import ConnectionStore
        path=service.definition_path('Darwin'); path.parent.mkdir(parents=True)
        original=plistlib.dumps({'ProgramArguments':['python','--public-url','https://runner.example']})
        path.write_bytes(original)
        with patch.object(ConnectionStore,'adopt',side_effect=OSError('cannot persist')), self.assertRaises(OSError):
            service.install(self.spec(),system='Darwin',run=FakeRun())
        self.assertEqual(path.read_bytes(),original)

    def test_launchd_bootstrap_retries_only_transient_teardown_failure(self):
        class TeardownRun(FakeRun):
            def __call__(self, argv, **kwargs):
                result=super().__call__(argv, **kwargs)
                if argv[1] == 'bootstrap' and sum(call[1] == 'bootstrap' for call in self.calls) == 1:
                    return CompletedProcess(argv, 5, '', 'Input/output error')
                return result
        run=TeardownRun()
        with patch('time.sleep') as sleep:
            service.install(self.spec(), system='Darwin', run=run)
        self.assertEqual(sum(call[1] == 'bootstrap' for call in run.calls), 2)
        sleep.assert_called_once_with(1)

    def test_launchd_bootstrap_retry_is_bounded_and_other_errors_are_immediate(self):
        for code, attempts in ((5,3),(7,1)):
            run=FakeRun({'launchctl bootstrap': CompletedProcess([],code,'','bootstrap failure')})
            with patch('time.sleep') as sleep, self.assertRaises(service.ServiceError):
                service.install(self.spec(), system='Darwin', run=run)
            self.assertEqual(sum(call[1] == 'bootstrap' for call in run.calls), attempts)
            self.assertEqual(sleep.call_count, attempts-1)

    def test_only_a_mac_can_run_it_in_the_background_and_anything_else_is_told_so(self):
        """Orchestrator runs on a Mac. Linux used to get a systemd unit; it is refused like any other system now, and nothing is written."""
        for system in ("Linux", "Windows"):
            for action in (lambda: service.install(self.spec(), system=system, run=FakeRun()),
                           lambda: service.uninstall(system=system, run=FakeRun()),
                           lambda: service.status(system=system, run=FakeRun())):
                with self.subTest(system=system), self.assertRaises(service.ServiceError) as caught:
                    action()
                self.assertIn("runs on a Mac", str(caught.exception))
        self.assertFalse((self.home / "LaunchAgents").exists())
        self.assertFalse(hasattr(service, "render_systemd"))  # no systemd unit is built any more

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
