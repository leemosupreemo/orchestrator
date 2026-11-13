import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import mac_readiness as readiness

LAPTOP_PMSET = """System-wide power settings:
Currently in use:
 standby              1
 disksleep            10
 sleep                0 (sleep prevented by caffeinate, powerd)
 displaysleep         0
"""
MINI_PMSET = """Currently in use:
 autorestart          0
 disksleep            10
 sleep                1
"""


class Mac:
    """Answers the read-only commands the checks run, and records anything else (which must never happen)."""

    def __init__(self, auto_login="builder", pmset=LAPTOP_PMSET, xcode="/Applications/Xcode.app/Contents/Developer", license_ok=True):
        self.auto_login, self.pmset, self.xcode, self.license_ok = auto_login, pmset, xcode, license_ok
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "/usr/bin/defaults":
            if self.auto_login is None:
                return subprocess.CompletedProcess(argv, 1, "", "Could not find key 'autoLoginUser'")
            return subprocess.CompletedProcess(argv, 0, self.auto_login + "\n", "")
        if argv[0] == "/usr/bin/pmset":
            return subprocess.CompletedProcess(argv, 0, self.pmset, "")
        if argv[0] == "/usr/bin/xcode-select":
            return subprocess.CompletedProcess(argv, 0 if self.xcode else 2, (self.xcode or "") + "\n", "")
        if argv[:2] == ["/usr/bin/xcodebuild", "-license"]:
            return subprocess.CompletedProcess(argv, 0 if self.license_ok else 1, "", "")
        raise AssertionError(f"unexpected command {argv}")


class ReadinessTests(unittest.TestCase):
    def test_a_ready_closet_mac(self):
        mac = Mac(pmset=" sleep 0\n autorestart 1\n")
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "background.json").write_text(json.dumps({"registration": "enabled"}))
            result = readiness.check(mac, user="builder", control_dir=Path(folder))
        self.assertEqual(result, {name: "ok" for name in readiness.CHECKS})
        self.assertEqual(readiness.warnings(result), [])

    def test_problems_are_named_with_their_fixes(self):
        mac = Mac(auto_login=None, pmset=MINI_PMSET, license_ok=False)
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "background.json").write_text(json.dumps({"registration": "requiresApproval"}))
            result = readiness.check(mac, user="builder", control_dir=Path(folder))
        self.assertEqual(result, {"auto_login": "off", "sleep": "on", "power_restart": "off", "xcode_license": "not_accepted",
                                  "login_item": "needs_approval"})
        fixes = {w["check"]: w["fix"] for w in readiness.warnings(result)}
        self.assertEqual(set(fixes), set(readiness.CHECKS))
        self.assertIn("autorestart 1", fixes["power_restart"])

    def test_automatic_login_for_someone_else_is_off(self):
        self.assertEqual(readiness.check(Mac(auto_login="admin"), user="builder")["auto_login"], "off")

    def test_settings_that_dont_apply_or_cant_be_read_are_unknown_not_warnings(self):
        def broken(argv, **kwargs):
            raise OSError("missing")
        result = readiness.check(broken, user="builder", control_dir=Path("/nonexistent"))
        self.assertEqual(set(result.values()), {"unknown"})
        laptop = readiness.check(Mac(), user="builder")
        self.assertEqual(laptop["power_restart"], "unknown")  # laptops have no autorestart setting
        self.assertEqual(laptop["sleep"], "ok")
        self.assertEqual(readiness.warnings(laptop), [])

    def test_no_xcode_is_not_a_license_problem(self):
        result = readiness.check(Mac(xcode="/Library/Developer/CommandLineTools"), user="builder")
        self.assertEqual(result["xcode_license"], "not_installed")
        self.assertEqual(readiness.warnings(result), [])

    def test_only_reads_settings(self):
        mac = Mac()
        readiness.check(mac, user="builder")
        for argv in mac.calls:
            self.assertNotIn("sudo", argv)
            self.assertNotIn("write", argv)
            self.assertNotIn("accept", argv)
            self.assertNotEqual(argv[:2], ["/usr/bin/pmset", "-a"])

    def test_clean_keeps_known_checks_and_states_only(self):
        self.assertEqual(readiness.clean({"sleep": "on", "auto_login": "<script>", "secret": "ok"}), {"sleep": "on"})
        self.assertEqual(readiness.clean("nope"), {})


if __name__ == "__main__":
    unittest.main()
