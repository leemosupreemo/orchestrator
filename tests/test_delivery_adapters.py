from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import delivery_adapters as da  # noqa: E402
from orchestrator.config_validation import validate_project_config  # noqa: E402
from orchestrator.project_config import load_project_config  # noqa: E402
from test_config_validation import make_config  # noqa: E402

FIREBASE = lambda name: "/usr/local/bin/firebase" if name == "firebase" else None  # noqa: E731


def cfg(root: Path, **kw):
    base = dict(root=root, delivery_platform="android", delivery_build_command=None, delivery_artifact="out/*.apk",
                firebase_app_id="1:123:android:abc", firebase_project_id=None, firebase_hosting_target=None,
                firebase_testers=None, firebase_groups=None)
    base.update(kw)
    return SimpleNamespace(**base)


class PureHelperTests(unittest.TestCase):
    def test_platform_defaults_to_ios_and_is_case_insensitive(self):
        self.assertEqual(da.platform_of(SimpleNamespace()), "ios")
        self.assertEqual(da.platform_of(SimpleNamespace(delivery_platform=" Android ")), "android")

    def test_recipients_prefer_the_job_then_the_project_then_a_default_group(self):
        config = SimpleNamespace(firebase_testers="p@x.com", firebase_groups=None)
        self.assertEqual(da.recipients(config, {"groups": "qa"}), ("p@x.com", "qa"))
        self.assertEqual(da.recipients(config, {}), ("p@x.com", None))
        self.assertEqual(da.recipients(SimpleNamespace(), {}), (None, "internal-testers"))

    def test_channel_ids_are_safe(self):
        self.assertEqual(da.channel_id("Job_42 / fix!"), "job-42-fix")
        self.assertEqual(da.channel_id(""), "preview")
        self.assertLessEqual(len(da.channel_id("x" * 100)), 40)

    def test_find_artifact_takes_the_newest_apk_or_aab_inside_the_project_and_ignores_stale_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            (root / "out").mkdir(parents=True)
            root = root.resolve()  # macOS temp dirs sit behind a /var -> /private/var link
            old, new, junk = root / "out/old.apk", root / "out/new.aab", root / "out/notes.txt"
            for f in (old, new, junk):
                f.write_text("x")
            os.utime(old, (1000, 1000))
            self.assertEqual(da.find_artifact(root, "out/*"), new)
            self.assertEqual(da.find_artifact(root, "out/*", since=time.time() - 60), new)
            os.utime(new, (1000, 1000))
            self.assertIsNone(da.find_artifact(root, "out/*", since=time.time() - 60))
            self.assertIsNone(da.find_artifact(root, "../*"))

    def test_preview_url_from_json_or_text(self):
        out = 'i  hosting: ...\n{"status":"success","result":{"my-site":{"site":"my-site","url":"https://my-site--job-1-abc.web.app","expireTime":"2026-11-19"}}}'
        self.assertEqual(da._preview_url(out), "https://my-site--job-1-abc.web.app")
        self.assertEqual(da._preview_url("Channel URL (my-site): https://my-site--job-1-abc.web.app [expires 2026]"),
                         "https://my-site--job-1-abc.web.app")
        self.assertIsNone(da._preview_url("nothing here"))
        self.assertIsNone(da._preview_url('{"status":"error"}'))


class ValidationTests(unittest.TestCase):
    def test_android_needs_an_app_id_and_an_artifact_inside_the_project(self):
        self.assertEqual(da.validate_platform(cfg(Path("."))), [])
        errors = da.validate_platform(cfg(Path("."), firebase_app_id=None, delivery_artifact=None))
        self.assertEqual(len(errors), 2)
        self.assertIn("inside the project", da.validate_platform(cfg(Path("."), delivery_artifact="/abs/app.apk"))[0])
        self.assertIn("inside the project", da.validate_platform(cfg(Path("."), delivery_artifact="../app.apk"))[0])

    def test_web_needs_firebase_json_and_an_unknown_platform_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = cfg(Path(tmp), delivery_platform="web")
            self.assertIn("firebase.json", da.validate_platform(config)[0])
            (Path(tmp) / "firebase.json").write_text("{}")
            self.assertEqual(da.validate_platform(config), [])
        self.assertIn("must be one of", da.validate_platform(cfg(Path("."), delivery_platform="steam"))[0])


class CommandTests(unittest.TestCase):
    def test_android_command_line(self):
        config = cfg(Path("."), firebase_project_id="proj", firebase_testers="a@x.com")
        argv = da.ADAPTERS["android"].command(config, {"groups": "qa"}, "notes", Path("/p/app.apk"))
        self.assertEqual(argv, ["firebase", "appdistribution:distribute", "/p/app.apk", "--app", "1:123:android:abc",
                                "--release-notes", "notes", "--groups", "qa", "--testers", "a@x.com", "--project", "proj"])

    def test_web_command_line(self):
        config = cfg(Path("."), delivery_platform="web", firebase_hosting_target="app", firebase_project_id="proj")
        argv = da.ADAPTERS["web"].command(config, {"job_id": "Job_7"}, "", None)
        self.assertEqual(argv, ["firebase", "hosting:channel:deploy", "job-7", "--expires", "7d", "--only", "app",
                                "--project", "proj", "--json"])


class DeliverTests(unittest.TestCase):
    def deliver(self, config, outputs, job=None):
        calls = []

        def run(argv, cwd, timeout):
            calls.append((argv, cwd, timeout))
            return outputs[len(calls) - 1]

        result = da.deliver(config, job or {"job_id": "job-1"}, "notes", run=run, now=time.time, which=FIREBASE)
        return result, calls

    def test_android_builds_finds_the_fresh_apk_uploads_and_returns_a_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "out").mkdir()
            apk = root / "out/app.apk"
            apk.write_bytes(b"apk")
            os.utime(apk, (time.time() + 5, time.time() + 5))  # "written by the build"
            config = cfg(root, delivery_build_command="make apk", firebase_groups="qa")
            (receipt, log), calls = self.deliver(config, [(0, "built"), (0, "uploaded https://console.firebase.google.com/project/p/appdistribution/x")])
            self.assertEqual([c[0][0] for c in calls], ["/bin/bash", "firebase"])
            self.assertEqual(calls[1][0][2], str(apk.resolve()))
            self.assertEqual((receipt["status"], receipt["platform"], receipt["groups"]), ("delivered", "android", "qa"))
            self.assertEqual(receipt["sha256"], da.sha256(apk))
            self.assertTrue(receipt["console_url"].startswith("https://console.firebase.google.com/"))
            self.assertNotIn("notes", log)  # release notes aren't echoed into the log

    def test_a_failed_build_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            (receipt, log), calls = self.deliver(cfg(Path(tmp), delivery_build_command="make"), [(2, "boom")])
            self.assertEqual((receipt, len(calls)), ({}, 1))
            self.assertIn("nothing was sent", log)

    def test_no_artifact_or_a_stale_one_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (receipt, log), calls = self.deliver(cfg(root), [])
            self.assertEqual((receipt, calls), ({}, []))
            self.assertIn("Set delivery_build_command", log)
            (root / "out").mkdir()
            (root / "out/app.apk").write_text("x")
            os.utime(root / "out/app.apk", (1000, 1000))
            (receipt, log), calls = self.deliver(cfg(root, delivery_build_command="true"), [(0, "")])
            self.assertEqual(receipt, {})
            self.assertIn("was written by the build", log)

    def test_a_failed_upload_gives_no_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "out").mkdir()
            (root / "out/app.apk").write_text("x")
            (receipt, log), _ = self.deliver(cfg(root), [(1, "HTTP Error: 404")])
            self.assertEqual(receipt, {})
            self.assertIn("Firebase failed (exit 1)", log)

    def test_web_receipt_carries_the_preview_url_and_no_url_means_unconfirmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = cfg(Path(tmp), delivery_platform="web")
            (receipt, _), _ = self.deliver(config, [(0, '{"result":{"s":{"url":"https://s--job-1-x.web.app"}}}')])
            self.assertEqual((receipt["platform"], receipt["preview_url"]), ("web", "https://s--job-1-x.web.app"))
            (receipt, log), _ = self.deliver(config, [(0, "ok")])
            self.assertEqual(receipt, {})
            self.assertIn("doesn't show the build was delivered", log)

    def test_a_missing_firebase_cli_says_how_to_install_it(self):
        receipt, log = da.deliver(cfg(Path(".")), {}, "n", run=lambda *a: (0, ""), now=time.time, which=lambda n: None)
        self.assertEqual(receipt, {})
        self.assertIn("npm install -g firebase-tools", log)


class ConfigIntegrationTests(unittest.TestCase):
    def test_an_android_project_with_firebase_needs_no_scheme_plist_or_distribution_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp), xcode_project=None, scheme=None, test_target="", build_command="make",
                                 test_command="make test", firebase_distribution=True, delivery_provider="firebase",
                                 delivery_platform="android", delivery_artifact="out/*.apk", firebase_app_id="1:1:android:a")
            self.assertEqual(validate_project_config(config), [])
            self.assertFalse(config.uses_xcode)
            config = make_config(Path(tmp), xcode_project=None, scheme=None, test_target="", build_command="make",
                                 test_command="make test", firebase_distribution=True, delivery_platform="android")
            self.assertEqual(len(validate_project_config(config)), 2)
            self.assertEqual(len(config.validate_distribution_config()), 2)

    def test_ios_is_unchanged_and_an_unknown_platform_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = validate_project_config(make_config(Path(tmp), xcode_project=None, scheme="S", build_command="x",
                                                         test_command="y", firebase_distribution=True))
            self.assertTrue(any("distribution_script_path" in e for e in errors))
            self.assertTrue(any("firebase_plist_path" in e for e in errors))
            errors = validate_project_config(make_config(Path(tmp), xcode_project=None, scheme="S", build_command="x",
                                                         test_command="y", delivery_platform="steam"))
            self.assertEqual(len(errors), 1)
            self.assertIn("must be one of", errors[0])

    def test_project_json_fields_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".orchestrator").mkdir()
            (root / ".orchestrator/project.json").write_text(json.dumps({
                "project_name": "G", "build_command": "x", "test_command": "y", "delivery_platform": "Web",
                "delivery_build_command": "npm run build", "firebase_project_id": "p", "firebase_hosting_target": "app"}))
            loaded = load_project_config(root)
            self.assertEqual((loaded.delivery_platform, loaded.delivery_build_command, loaded.firebase_project_id,
                              loaded.firebase_hosting_target), ("web", "npm run build", "p", "app"))



class DeliverScriptTests(unittest.TestCase):
    """scripts/deliver_build.py hands non-iOS projects to the adapter and writes the receipt."""

    def run_script(self, receipt, errors=None, branch="job-branch"):
        from unittest.mock import patch
        sys.path.insert(0, str(PACKAGE_ROOT / "orchestrator" / "scripts"))
        import deliver_build
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "rt"
            job = Path(tmp) / "job.json"
            job.write_text(json.dumps({"job_id": "job-1", "title": "T", "branch": "job-branch"}))
            config = SimpleNamespace(delivery_platform="android", runtime_dir=runtime,
                                     validate_distribution_config=lambda: errors or [])
            with patch.object(deliver_build, "PROJECT_CONFIG", config), \
                 patch.object(deliver_build.delivery_adapters, "deliver", return_value=(receipt, "log text")) as deliver, \
                 patch.object(deliver_build.subprocess, "check_output", return_value=branch.encode()), \
                 patch.object(deliver_build, "send_final_notification") as notify, \
                 patch.object(deliver_build, "print_header"):
                with self.assertRaises(SystemExit) as raised:
                    deliver_build.deliver_build(job)
                written = runtime / "output" / "delivery" / "job-1.json"
                return (raised.exception.code, json.loads(written.read_text()) if written.exists() else None,
                        deliver.called, notify.call_args.args[-1] if notify.called else None,
                        (runtime / "logs" / "distribution_job-1.log").exists())

    def test_a_confirmed_delivery_writes_the_receipt_notifies_and_exits_0(self):
        code, receipt, _, notified, logged = self.run_script({"status": "delivered", "platform": "android", "groups": "qa"})
        self.assertEqual((code, notified, logged), (0, True, True))
        self.assertEqual((receipt["job_id"], receipt["branch"], receipt["platform"]), ("job-1", "job-branch", "android"))

    def test_an_unconfirmed_delivery_exits_1_without_a_receipt(self):
        code, receipt, _, notified, logged = self.run_script({})
        self.assertEqual((code, receipt, notified, logged), (1, None, False, True))

    def test_bad_config_or_the_wrong_branch_stops_before_building(self):
        code, receipt, called, *_ = self.run_script({"status": "delivered"}, errors=["firebase_app_id missing"])
        self.assertEqual((code, receipt, called), (1, None, False))
        code, receipt, called, *_ = self.run_script({"status": "delivered"}, branch="other")
        self.assertEqual((code, receipt, called), (1, None, False))


if __name__ == "__main__":
    unittest.main()
