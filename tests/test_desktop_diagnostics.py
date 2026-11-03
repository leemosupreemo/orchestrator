import json
import unittest

from orchestrator.desktop_diagnostics import diagnostics_snapshot

SECRETS = ["private-machine", "sk-live-123", "/Users/owner/secret-project", "owner@example.com", "Traceback", "rm -rf"]


def status(**overrides):
    value = {"agent": "running", "setup": "ready", "local_interface": "ready", "remote_access": "connected", "remote_enabled": True,
             "activity": {"runs": 1, "tasks": 0}, "local_origin": "http://127.0.0.1:8765",
             "project": {"name": "secret-project", "root": "/Users/owner/secret-project"},
             "pairing": {"state": "connected", "owner_email": "owner@example.com"},
             "runner": {"version": "0.1.0", "api_version": 2}, "update": "current", "legacy": "none", "last_error": "tunnel_failed"}
    value.update(overrides)
    return value


class DiagnosticsTests(unittest.TestCase):
    def test_keeps_actionable_typed_states(self):
        report = diagnostics_snapshot(status(), [{"name": "git", "state": "found"}, {"name": "cloudflared", "state": "missing"}])
        self.assertEqual(report["agent"], "running")
        self.assertEqual(report["remote_access"], "connected")
        self.assertEqual(report["pairing"], "connected")
        self.assertTrue(report["project_selected"])
        self.assertEqual(report["activity"], {"runs": 1, "tasks": 0})
        self.assertEqual(report["runner"], {"version": "0.1.0", "api_version": 2})
        self.assertEqual(report["last_error"], "tunnel_failed")
        self.assertEqual(report["checks"], [{"name": "git", "state": "found"}, {"name": "cloudflared", "state": "missing"}])

    def test_excludes_paths_accounts_and_injected_fields(self):
        hostile = status(agent="running; rm -rf", remote_access="sk-live-123", last_error="Traceback: private-machine",
                         runner={"version": "/Users/owner/secret-project", "api_version": "sk-live-123"},
                         activity={"runs": "private-machine", "tasks": -1}, transcript="sk-live-123", machine_secret="private-machine")
        checks = [{"name": "sk-live-123", "state": "found"}, {"name": "git", "state": "Traceback"}, "private-machine",
                  {"name": "gh", "state": "found", "path": "/Users/owner/secret-project"}]
        text = json.dumps(diagnostics_snapshot(hostile, checks))
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        report = json.loads(text)
        self.assertEqual(report["agent"], "unknown")
        self.assertEqual(report["remote_access"], "unknown")
        self.assertEqual(report["runner"]["version"], "unknown")
        self.assertIsNone(report["runner"]["api_version"])
        self.assertEqual(report["activity"], {"runs": None, "tasks": None})
        self.assertNotIn("transcript", report)
        self.assertEqual(report["checks"], [{"name": "gh", "state": "found"}])

    def test_missing_status_is_unknown_not_an_error(self):
        report = diagnostics_snapshot({}, [])
        self.assertEqual(report["agent"], "unknown")
        self.assertFalse(report["project_selected"])
        self.assertEqual(report["checks"], [])


if __name__ == "__main__":
    unittest.main()
