from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator import ux_native as native
from orchestrator import ux_review as ux

PNG = b"\x89PNG\r\n\x1a\nfake"


def done(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


class FakeAdb:
    """Answers the adb calls capture_android makes, and remembers them."""

    def __init__(self, devices="List of devices attached\nemulator-5554\tdevice\n", install=0, screenshot=PNG):
        self.calls, self.devices, self.install, self.screenshot = [], devices, install, screenshot

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        args = argv[1:]
        if args[:2] == ["-s", "emulator-5554"]:
            args = args[2:]
        if args == ["devices"]:
            return done(self.devices)
        if args[:1] == ["install"]:
            return done(returncode=self.install, stderr="INSTALL_FAILED_OLDER_SDK" if self.install else "")
        if args[:2] == ["exec-out", "screencap"]:
            return done(self.screenshot, returncode=0 if self.screenshot else 1)
        return done()

    def shell(self, *words):
        return [c for c in self.calls if c[-len(words):] == list(words)] if words else []


class AndroidTests(unittest.TestCase):
    CFG = {"package": "com.example.app", "apk": "", "avd": "", "build_command": "", "screens": ["myapp://settings"]}

    def capture(self, run, cfg=None, dark_mode=True, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "screens"
            result = native.capture_android(root, {**self.CFG, **(cfg or {})}, out, dark_mode=dark_mode, log=lambda _: None, run=run,
                                            sleep=lambda _: None, adb="/sdk/adb", **kwargs)
            return result, sorted(p.name for p in out.glob("*.png")) if out.exists() else []

    def test_captures_launch_and_each_link_in_light_and_dark(self):
        adb = FakeAdb()
        (shots, problem), files = self.capture(adb)
        self.assertEqual(problem, "")
        self.assertEqual([(s["route"], s["dark"]) for s in shots], [
            ("Android, after launch", False), ("myapp://settings", False), ("Android, after launch", True), ("myapp://settings", True)])
        self.assertEqual(files, ["android-launch-dark.png", "android-launch-light.png", "android-myapp-settings-dark.png", "android-myapp-settings-light.png"])
        self.assertTrue(any(c[-3:] == ["-d", "myapp://settings", "com.example.app"] for c in adb.calls))  # the link is opened in the app
        self.assertEqual(adb.calls[-1][-4:], ["cmd", "uimode", "night", "auto"])  # night mode is handed back last

    def test_light_only_when_dark_mode_is_off(self):
        (shots, _), files = self.capture(FakeAdb(), dark_mode=False)
        self.assertEqual([s["dark"] for s in shots], [False, False])
        self.assertTrue(all("light" in f for f in files))

    def test_says_what_is_missing_instead_of_failing_quietly(self):
        (shots, problem), _ = self.capture(FakeAdb(devices="List of devices attached\n"))
        self.assertEqual(shots, [])
        self.assertIn("No Android emulator or device is running", problem)
        (shots, problem), _ = self.capture(FakeAdb(devices="List of devices attached\n"), cfg={"avd": "Pixel_8"}, popen=MagicMock())
        self.assertIn("emulator program wasn't found", problem)  # no emulator binary next to the fake adb
        with tempfile.TemporaryDirectory() as tmp, patch.object(native, "find_adb", return_value=None):
            self.assertIn("adb wasn't found", native.capture_android(Path(tmp), self.CFG, Path(tmp), run=FakeAdb(), log=lambda _: None)[1])

    def test_install_and_build_problems_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app.apk").write_bytes(b"x")
            shots, problem = native.capture_android(root, {**self.CFG, "apk": "app.apk"}, root / "s", log=lambda _: None, run=FakeAdb(install=1),
                                                    sleep=lambda _: None, adb="/sdk/adb")
            self.assertEqual(shots, [])
            self.assertIn("Installing the Android build failed: INSTALL_FAILED_OLDER_SDK", problem)
            self.assertIn("wasn't found at missing.apk", native.capture_android(root, {**self.CFG, "apk": "missing.apk"}, root / "s", log=lambda _: None,
                                                                                run=FakeAdb(), sleep=lambda _: None, adb="/sdk/adb")[1])

    def test_a_screenshot_that_is_not_a_png_counts_as_missing(self):
        (shots, problem), _ = self.capture(FakeAdb(screenshot=b"error: closed"), dark_mode=False)
        self.assertEqual(shots, [])
        self.assertIn("Missing screenshots", problem)

    def test_picks_a_booted_device_preferring_emulators(self):
        self.assertEqual(native.running_device("List of devices attached\nR58M\tdevice\nemulator-5556\tdevice\n"), "emulator-5556")
        self.assertEqual(native.running_device("List of devices attached\nR58M\toffline\n"), None)
        self.assertEqual(native.running_device("List of devices attached\n\n"), None)


class MacTests(unittest.TestCase):
    def run_capture(self, responses, app_exists=True):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if isinstance(argv, list) and argv[0] in responses:
                return responses[argv[0]](argv)
            return done()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if app_exists:
                (root / "build" / "My App.app").mkdir(parents=True)
            out = root / "screens"
            result = native.capture_macos(root, {"app": "build/My App.app", "build_command": ""}, out, log=lambda _: None, run=run, sleep=lambda _: None)
            return result, calls, sorted(p.name for p in out.glob("*.png")) if out.exists() else []

    def shot(self, argv):
        Path(argv[-1]).write_bytes(PNG)
        return done()

    def test_launches_finds_the_window_captures_it_and_quits_the_app(self):
        (shots, problem), calls, files = self.run_capture({"pgrep": lambda a: done("123\n"), "osascript": lambda a: done("4567\n"), "screencapture": self.shot})
        self.assertEqual(problem, "")
        self.assertEqual(shots, [{"file": "macos-my-app-light.png", "route": "My App window", "width": 0, "dark": False}])
        self.assertEqual(files, ["macos-my-app-light.png"])
        capture = next(c for c in calls if c[0] == "screencapture")
        self.assertEqual(capture[:5], ["screencapture", "-x", "-o", "-l", "4567"])
        self.assertEqual(calls[-1][0], "pkill")  # the app is closed afterwards
        self.assertIn("[123]", next(c for c in calls if c[0] == "osascript")[-1])

    def test_reports_each_way_it_can_fail(self):
        _, problem = self.run_capture({}, app_exists=False)[0]
        self.assertIn("wasn't found at build/My App.app", problem)
        self.assertIn("isn't running after launch", self.run_capture({"pgrep": lambda a: done("")})[0][1])
        self.assertIn("no visible window", self.run_capture({"pgrep": lambda a: done("9"), "osascript": lambda a: done("")})[0][1])
        self.assertIn("Screen Recording", self.run_capture({"pgrep": lambda a: done("9"), "osascript": lambda a: done("5"),
                                                           "screencapture": lambda a: done(returncode=1)})[0][1])


class SettingsTests(unittest.TestCase):
    def test_native_settings_are_validated_and_count_as_screens(self):
        cfg = ux.settings({"ui_review": {"android": {"package": "com.example.app", "screens": ["myapp://a"]}, "macos": {"app": "build/X.app"},
                                         "simulator_screens": ["myapp://s"]}})
        self.assertEqual(cfg["android"]["screens"], ["myapp://a"])
        self.assertEqual(cfg["macos"]["app"], "build/X.app")
        self.assertEqual(cfg["simulator_screens"], ["myapp://s"])
        self.assertTrue(ux.has_screens(cfg))
        self.assertFalse(ux.has_screens(ux.settings({})))
        self.assertTrue(ux.has_screens(ux.settings({"ui_review": {"macos": {"app": "X.app"}}})))
        for bad in ({"android": {"package": "nope"}}, {"android": {"package": "com.x.app", "screens": ["not a link"]}},
                    {"macos": {"app": "X"}}, {"simulator_screens": ["x"]}, {"android": "yes"}):
            with self.assertRaises(ux.ConfigError, msg=bad):
                ux.settings({"ui_review": bad})


if __name__ == "__main__":
    unittest.main()
