"""Runs packaging/macos/install-mac.sh against fake system tools, so nothing is downloaded, mounted or installed."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "packaging/macos/install-mac.sh"
HAS_SH_TOOLS = all(shutil.which(tool) for tool in ("plutil", "shasum"))

FAKES = {
    "uname": 'case "$1" in -s) echo "${FAKE_SYSTEM:-Darwin}";; -m) echo "${FAKE_ARCH:-arm64}";; esac',
    "id": 'case "$1" in -u) echo "${FAKE_UID:-501}";; -un) echo builder;; esac',
    "stat": 'echo "${FAKE_CONSOLE:-builder}"',
    # curl -fsSL --proto =https -o OUT URL: serve from $FAKE_WEB/<last path part>
    "curl": 'out="$5"; url="$6"; echo "$url" >> "$FAKE_LOG"; cp "$FAKE_WEB/${url##*/}" "$out"',
    "hdiutil": 'echo "hdiutil $*" >> "$FAKE_LOG"; if [ "$1" = attach ]; then mkdir -p "$6"; cp -R "$FAKE_WEB/Orchestrator.app" "$6/"; fi',
    "codesign": 'exit "${FAKE_CODESIGN:-0}"',
    "spctl": 'exit "${FAKE_SPCTL:-0}"',
    "ditto": 'echo "ditto $*" >> "$FAKE_LOG"; cp -R "$1" "$2"',
}


@unittest.skipUnless(HAS_SH_TOOLS, "needs macOS plutil and shasum")
class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-install-")
        base = self.base = Path(self.tmp.name)
        self.bin, self.web, self.home, self.apps = base / "bin", base / "web", base / "home", base / "Applications"
        for folder in (self.bin, self.web, self.home, self.apps):
            folder.mkdir()
        for name, body in FAKES.items():
            (self.bin / name).write_text("#!/bin/sh\n" + body + "\n")
            (self.bin / name).chmod(0o755)
        self.log = base / "log"
        self.log.touch()
        app = self.web / "Orchestrator.app/Contents/Resources/runtime/bin"
        app.mkdir(parents=True)
        (app / "orchestrator").write_text('#!/bin/sh\necho "enroll-args: $*" >> "$FAKE_LOG"\nexit "${FAKE_ENROLL:-0}"\n')
        (app / "orchestrator").chmod(0o755)
        (self.web / "Orchestrator-0.2.0-arm64.dmg").write_bytes(b"arm image")
        (self.web / "Orchestrator-0.2.0-x86_64.dmg").write_bytes(b"intel image")
        self.release()

    def tearDown(self):
        self.tmp.cleanup()

    def release(self, **overrides):
        record = {"version": "0.2.0"}
        for arch in ("arm64", "x86_64"):
            name = f"Orchestrator-0.2.0-{arch}.dmg"
            record[arch] = {"url": f"https://downloads.example.com/{name}",
                            "sha256": hashlib.sha256((self.web / name).read_bytes()).hexdigest()}
        record.update(overrides)
        (self.web / "macos.json").write_text(json.dumps(record))

    def install(self, *args, **env):
        environment = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "TMPDIR": str(self.base),
                       "FAKE_LOG": str(self.log), "FAKE_WEB": str(self.web), "ORCHESTRATOR_APPLICATIONS": str(self.apps),
                       "ORCHESTRATOR_RELEASES_URL": "https://releases.example.com/macos.json", **env}
        result = subprocess.run(["/bin/sh", str(SCRIPT), *args], env=environment, capture_output=True, text=True, timeout=60)
        return result.returncode, result.stdout + result.stderr, self.log.read_text()

    def installed(self):
        return (self.apps / "Orchestrator.app").exists()

    def test_installs_the_matching_build_and_passes_arguments_to_enroll(self):
        code, output, log = self.install("--token", "enroll_x", "--project", "git@github.com:me/app.git")
        self.assertEqual(code, 0, output)
        self.assertIn("Orchestrator-0.2.0-arm64.dmg", log)
        self.assertNotIn("x86_64", log)
        self.assertTrue(self.installed())
        self.assertIn("enroll-args: enroll --token enroll_x --project git@github.com:me/app.git", log)
        self.assertEqual([p for p in self.base.iterdir() if p.name.startswith("orchestrator-install.")], [])

    def test_intel_mac_gets_the_intel_build(self):
        code, output, log = self.install("--token", "t", FAKE_ARCH="x86_64")
        self.assertEqual(code, 0, output)
        self.assertIn("Orchestrator-0.2.0-x86_64.dmg", log)

    def test_root_other_systems_and_no_console_session_are_refused_before_downloading(self):
        for env, message in (({"FAKE_UID": "0"}, "not as root"), ({"FAKE_SYSTEM": "Linux"}, "Mac app"),
                             ({"FAKE_CONSOLE": "someone"}, "automatic login")):
            with self.subTest(env=env):
                code, output, log = self.install(**env)
                self.assertEqual(code, 1)
                self.assertIn(message, output)
                self.assertEqual(log, "")

    def test_checksum_mismatch_installs_and_runs_nothing(self):
        self.release(arm64={"url": "https://downloads.example.com/Orchestrator-0.2.0-arm64.dmg", "sha256": "0" * 64})
        code, output, log = self.install()
        self.assertEqual(code, 1)
        self.assertIn("checksum", output)
        self.assertFalse(self.installed())
        self.assertNotIn("enroll-args", log)
        self.assertNotIn("hdiutil attach", log)

    def test_bad_signature_or_notarization_installs_and_runs_nothing(self):
        for env, message in (({"FAKE_CODESIGN": "1"}, "signature"), ({"FAKE_SPCTL": "3"}, "notarized")):
            with self.subTest(env=env):
                code, output, log = self.install(**env)
                self.assertEqual(code, 1)
                self.assertIn(message, output)
                self.assertFalse(self.installed())
                self.assertNotIn("enroll-args", log)

    def test_non_https_download_is_refused(self):
        self.release(arm64={"url": "http://downloads.example.com/Orchestrator-0.2.0-arm64.dmg", "sha256": "0" * 64})
        code, output, _ = self.install()
        self.assertEqual(code, 1)
        self.assertIn("https://", output)

    def test_existing_install_is_kept_and_nothing_is_downloaded(self):
        shutil.copytree(self.web / "Orchestrator.app", self.apps / "Orchestrator.app")
        code, output, log = self.install("--token", "t")
        self.assertEqual(code, 0, output)
        self.assertIn("already installed", output)
        self.assertNotIn("downloads.example.com", log)
        self.assertIn("enroll-args", log)

    def test_unwritable_applications_falls_back_to_home(self):
        self.apps.chmod(0o555)
        try:
            code, output, _ = self.install("--token", "t")
        finally:
            self.apps.chmod(0o755)
        self.assertEqual(code, 0, output)
        self.assertTrue((self.home / "Applications/Orchestrator.app").exists())

    def test_enroll_failure_is_the_script_result(self):
        code, _, _ = self.install("--token", "t", FAKE_ENROLL="1")
        self.assertEqual(code, 1)

    def test_posix_shell_syntax(self):
        self.assertEqual(subprocess.run(["/bin/sh", "-n", str(SCRIPT)]).returncode, 0)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck isn't installed")
    def test_shellcheck(self):
        self.assertEqual(subprocess.run(["shellcheck", "-s", "sh", str(SCRIPT)]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
