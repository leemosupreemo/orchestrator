import importlib.util
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HERE = REPO / "packaging/macos"


def load(name):
    spec = importlib.util.spec_from_file_location(f"macos_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HERE))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(HERE))
    return module


release = load("release")
smoke = load("smoke")
HAS_TOOLS = bool(shutil.which("clang") and shutil.which("lipo"))
SIGNED = dict(identity="Developer ID Application: Example (TEAM123456)", notary_profile="orchestrator-notary",
              feed_url="https://example.com/appcast.xml", public_key="A" * 43 + "=")


def refuse_tools(*args, **kwargs):
    raise AssertionError("no signing, notarization or disk-image tool may run")


@unittest.skipUnless(HAS_TOOLS, "needs Apple build tools")
class ReleaseValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-rel-")
        self.base = Path(self.tmp.name)
        self.app = self.make_bundle()

    def tearDown(self):
        self.tmp.cleanup()

    def make_bundle(self, arch="arm64", binary_arch=None, python_version="0.1.0", minimum="13.0"):
        app = self.base / arch / "Orchestrator.app"
        contents = app / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        source = self.base / "main.c"
        source.write_text("int main(void) { return 0; }\n")
        for name in ("Orchestrator", "OrchestratorAgentLauncher"):
            subprocess.run(["clang", "-arch", binary_arch or arch, "-o", str(contents / "MacOS" / name), str(source)], check=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": "Orchestrator", "CFBundleShortVersionString": "0.1.0", "CFBundleVersion": "7",
            "LSMinimumSystemVersion": minimum, "OrchestratorArchitecture": arch, "OrchestratorDevelopmentBuild": True}))
        dist = contents / "Resources/runtime/lib/python3.12/site-packages/orchestrator-0.1.0.dist-info"
        dist.mkdir(parents=True)
        (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: orchestrator\nVersion: {python_version}\n")
        shutil.copy2(HERE / "dependencies.json", contents / "Resources/dependencies.json")
        return app

    def test_valid_development_bundle_facts(self):
        facts = release.validate_bundle(self.app)
        self.assertEqual(facts, {"version": "0.1.0", "build": 7, "architecture": "arm64"})

    def test_mismatched_native_and_python_versions_refused(self):
        shutil.rmtree(self.base / "arm64")
        with self.assertRaisesRegex(ValueError, "version"):
            release.validate_bundle(self.make_bundle(python_version="0.2.0"))

    def test_wrong_architecture_refused(self):
        shutil.rmtree(self.base / "arm64")
        with self.assertRaisesRegex(ValueError, "architecture"):
            release.validate_bundle(self.make_bundle(binary_arch="x86_64"))

    def test_unsupported_deployment_target_refused(self):
        shutil.rmtree(self.base / "arm64")
        with self.assertRaisesRegex(ValueError, "13.0"):
            release.validate_bundle(self.make_bundle(minimum="12.0"))

    def test_unverified_dependency_digest_refused(self):
        bundled = self.app / "Contents/Resources/dependencies.json"
        data = json.loads(bundled.read_text())
        data["arm64"]["cloudflared"]["sha256"] = "f" * 64
        bundled.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "dependencies"):
            release.validate_bundle(self.app)

    def test_signed_release_requires_every_input_before_running_tools(self):
        for missing in SIGNED:
            values = dict(SIGNED, **{missing: ""})
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                release.release(self.app, self.base / "out", development=False, run=refuse_tools, **values)
        self.assertFalse((self.base / "out").exists())

    def test_signed_release_refuses_non_developer_id_identity_and_bad_update_inputs(self):
        cases = [dict(identity="Apple Development: Example (TEAM123456)"), dict(identity="Apple Distribution: Example (TEAM123456)"),
                 dict(feed_url="http://example.com/appcast.xml"), dict(public_key="not-a-key")]
        for override in cases:
            with self.subTest(override=override), self.assertRaises(ValueError):
                release.release(self.app, self.base / "out", development=False, run=refuse_tools, **dict(SIGNED, **override))

    def test_release_never_modifies_the_input_bundle(self):
        before = (self.app / "Contents/Info.plist").read_bytes()
        calls = []

        def tools(argv, **kwargs):
            calls.append(list(map(str, argv)))
            if Path(calls[-1][0]).name == "hdiutil":
                Path(calls[-1][-1]).write_bytes(b"image")

        release.release(self.app, self.base / "out", development=False, run=tools, **SIGNED)
        record = json.loads((self.base / "out/Orchestrator-0.1.0-arm64.json").read_text())
        self.assertFalse(record["development"])
        self.assertEqual((self.app / "Contents/Info.plist").read_bytes(), before)
        tools = [Path(call[0]).name for call in calls]
        for tool in ("codesign", "hdiutil", "notarytool", "stapler", "spctl"):
            self.assertIn(tool, " ".join(" ".join(call) for call in calls), tool)
        self.assertLess(tools.index("hdiutil"), next(i for i, c in enumerate(calls) if "notarytool" in c))
        signed_app = [c for c in calls if c[0].endswith("codesign") and c[-1].endswith("Orchestrator.app")]
        self.assertTrue(signed_app and "runtime" in signed_app[0] and SIGNED["identity"] in signed_app[0])


class SmokeTests(unittest.TestCase):
    def test_file_named_orchestrator_that_is_not_an_executable_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / "Orchestrator.app"
            (app / "Contents/MacOS").mkdir(parents=True)
            script = app / "Contents/MacOS/Orchestrator"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "executable"):
                smoke.check_executable(app)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (REPO / ".github/workflows/macos-release.yml").read_text()

    def test_manual_only_and_never_publishes(self):
        self.assertIn("workflow_dispatch", self.workflow)
        for trigger in ("\n  push:", "\n  pull_request:", "\n  schedule:", "\n  release:"):
            self.assertNotIn(trigger, self.workflow)
        for publisher in ("gh release", "softprops/action-gh-release", "firebase deploy", "appcast"):
            self.assertNotIn(publisher, self.workflow)

    def test_artifacts_hold_only_disk_images_and_their_records(self):
        uploads = self.workflow.split("actions/upload-artifact")[1:]
        self.assertTrue(uploads)
        for upload in uploads:
            lines = upload.split("path: |\n", 1)[1].splitlines()
            paths = [line.strip() for line in lines[:next(i for i, line in enumerate(lines) if not line.startswith(" " * 12))]]
            self.assertTrue(paths)
            for path in paths:
                self.assertTrue(path.endswith((".dmg", ".json")), path)

    def test_signing_secrets_only_in_protected_signed_mode(self):
        self.assertIn("environment: ${{ inputs.mode == 'signed' && 'macos-release' || '' }}", self.workflow)
        self.assertIn("if: inputs.mode == 'signed'", self.workflow)

    def test_hosted_page_offers_no_download_until_real_releases_exist(self):
        account = (REPO / "orchestrator/web/static/account.js").read_text()
        self.assertNotIn(".dmg", account)


if __name__ == "__main__":
    unittest.main()
