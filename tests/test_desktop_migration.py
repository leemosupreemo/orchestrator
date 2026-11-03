import fcntl
import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import desktop_migration as migration
from orchestrator.runtime_control import InstanceLease


def legacy_plist(**overrides) -> bytes:
    data = {"Label": "com.orchestrator.ui", "ProgramArguments": ["/usr/bin/caffeinate", "-i", "/Users/x/.local/pipx/venvs/o/bin/python",
            "-m", "orchestrator", "ui", "--no-open", "--tunnel"], "KeepAlive": True, "RunAtLoad": True}
    data.update(overrides)
    return plistlib.dumps(data)


class Launchctl:
    """Records every launchctl call; `print` answers with the configured state."""

    def __init__(self, state="running"):
        self.state, self.calls = state, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ["launchctl", "print"]:
            if self.state == "unloaded":
                return subprocess.CompletedProcess(argv, 113, "", "Could not find service")
            return subprocess.CompletedProcess(argv, 0, f"state = {self.state}\n\tpid = 4242\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def mutations(self):
        return [call for call in self.calls if call[:2] != ["launchctl", "print"]]


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-mig-")
        self.base = Path(self.tmp.name).resolve()
        self.state = self.base / "state"
        self.state.mkdir()
        self.agents = self.base / "LaunchAgents"
        self.agents.mkdir()
        self.plist = self.agents / "com.orchestrator.ui.plist"
        (self.state / "machine.json").write_text('{"machine_secret": "keep-me"}')

    def tearDown(self):
        self.tmp.cleanup()

    def inspect(self, run):
        return migration.inspect_legacy(self.state, run, launch_agents=self.agents)

    def migrate(self, run, consent=True):
        return migration.migrate_legacy(self.state, consent, run, launch_agents=self.agents)

    def test_no_legacy_service(self):
        run = Launchctl()
        self.assertEqual(self.inspect(run)["state"], "none")
        self.assertFalse(self.migrate(run)["migrated"])
        self.assertEqual(run.mutations, [])

    def test_running_legacy_service_is_never_stopped(self):
        self.plist.write_bytes(legacy_plist())
        run = Launchctl("running")
        self.assertEqual(self.inspect(run)["state"], "running")
        result = self.migrate(run)
        self.assertFalse(result["migrated"])
        self.assertEqual(result["state"], "running")
        self.assertEqual(run.mutations, [])
        self.assertTrue(self.plist.exists())

    def test_without_consent_nothing_changes(self):
        self.plist.write_bytes(legacy_plist())
        run = Launchctl("not running")
        result = self.migrate(run, consent=False)
        self.assertFalse(result["migrated"])
        self.assertEqual(result["reason"], "consent_required")
        self.assertEqual(run.mutations, [])
        self.assertTrue(self.plist.exists())

    def test_foreign_definitions_are_never_touched(self):
        cases = [legacy_plist(Label="com.example.other"), legacy_plist(ProgramArguments=["/bin/sh", "-c", "rm -rf ~"]), b"not a plist"]
        for content in cases:
            self.plist.write_bytes(content)
            run = Launchctl("not running")
            self.assertEqual(self.inspect(run)["state"], "foreign")
            self.assertFalse(self.migrate(run)["migrated"])
            self.assertEqual(run.mutations, [])
            self.assertEqual(self.plist.read_bytes(), content)

    def test_symlinked_definition_is_foreign(self):
        target = self.base / "elsewhere.plist"
        target.write_bytes(legacy_plist())
        self.plist.symlink_to(target)
        run = Launchctl("not running")
        self.assertEqual(self.inspect(run)["state"], "foreign")
        self.assertFalse(self.migrate(run)["migrated"])
        self.assertTrue(target.exists() and self.plist.is_symlink())

    def test_idle_legacy_service_migrates_with_backup_and_keeps_state(self):
        original = legacy_plist()
        self.plist.write_bytes(original)
        run = Launchctl("not running")
        result = self.migrate(run)
        self.assertTrue(result["migrated"])
        self.assertEqual(run.mutations, [["launchctl", "bootout", f"gui/{os.getuid()}/com.orchestrator.ui"]])
        self.assertFalse(self.plist.exists())
        backup = Path(result["backup"])
        self.assertEqual(backup.read_bytes(), original)
        self.assertTrue(backup.is_relative_to(self.state))
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / "machine.json").read_text(), '{"machine_secret": "keep-me"}')
        self.assertEqual(self.inspect(Launchctl("unloaded"))["state"], "none")

    def test_unloaded_definition_migrates_without_stopping_anything(self):
        self.plist.write_bytes(legacy_plist())
        run = Launchctl("unloaded")
        self.assertEqual(self.inspect(run)["state"], "stopped")
        self.assertTrue(self.migrate(run)["migrated"])

    def test_service_started_again_before_removal_is_left_alone(self):
        self.plist.write_bytes(legacy_plist())
        run = Launchctl("not running")
        self.assertEqual(self.inspect(run)["state"], "stopped")
        run.state = "running"  # launchd's KeepAlive restarted it after the person looked
        self.assertFalse(self.migrate(run)["migrated"])
        self.assertEqual(run.mutations, [])

    def test_manual_cli_server_is_reported_but_never_stopped(self):
        lease = InstanceLease.acquire(self.state / "ui-instance.lock", kind="cli")
        try:
            run = Launchctl()
            result = self.inspect(run)
            self.assertEqual(result["state"], "none")
            self.assertTrue(result["manual_server"])
            self.assertEqual(run.mutations, [])
        finally:
            lease.close()
        self.assertFalse(self.inspect(Launchctl())["manual_server"])

    def test_desktop_agent_own_lease_is_not_a_manual_server(self):
        lease = InstanceLease.acquire(self.state / "ui-instance.lock", kind="desktop")
        try:
            self.assertFalse(self.inspect(Launchctl())["manual_server"])
        finally:
            lease.close()

    def test_inspect_result_carries_no_paths_or_arguments(self):
        self.plist.write_bytes(legacy_plist())
        text = json.dumps(self.inspect(Launchctl("running")))
        self.assertNotIn("/", text)
        self.assertNotIn("4242", text)


if __name__ == "__main__":
    unittest.main()
