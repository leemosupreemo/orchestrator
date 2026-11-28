"""A project can supply its own capture command (`ui_review.capture`) for screens Orchestrator can't take itself.

Found while trying to review a Unity game's vegetation: the simulator check only knows Xcode schemes, so a project built any
other way had no way to put screenshots on a job's UX review. These tests run real commands through real bash."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator/scripts"))
from orchestrator import ux_native as native  # noqa: E402
from orchestrator import ux_review as ux  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nfake"
# Writes what the contract says: into $ORCHESTRATOR_CAPTURE_DIR, with the environment it was given.
WRITER = (
    "import os, sys, json, pathlib\n"
    "d = pathlib.Path(os.environ['ORCHESTRATOR_CAPTURE_DIR'])\n"
    "(d / 'eye-level.png').write_bytes(b'\\x89PNG\\r\\n\\x1a\\nfake')\n"
    "(d / 'sub').mkdir(); (d / 'sub' / 'wide_dark.png').write_bytes(b'\\x89PNG\\r\\n\\x1a\\nfake')\n"
    "(d / 'stats.txt').write_text('not an image')\n"
    "(d / 'captions.json').write_text(json.dumps({'eye-level.png': 'Eye level, CamelotHills'}))\n"
    "(d / 'env.txt').write_text(os.environ['ORCHESTRATOR_CAPTURE_JOB'] + '|' + os.environ['ORCHESTRATOR_CAPTURE_DARK'] + '|' + os.getcwd())\n"
)


def cfg(command, timeout=60):
    return {"command": command, "timeout": timeout}


class SettingsTests(unittest.TestCase):
    def test_the_command_is_validated_and_counts_as_screens(self):
        self.assertEqual(ux.settings({"ui_review": {"capture": {"command": "make shots"}}})["capture"], {"command": "make shots", "timeout": 900})
        self.assertEqual(ux.settings({"ui_review": {"capture": {"command": "x", "timeout": 120}}})["capture"]["timeout"], 120)
        self.assertTrue(ux.has_screens(ux.settings({"ui_review": {"capture": {"command": "x"}}})))
        self.assertIsNone(ux.settings({})["capture"])
        self.assertIsNone(ux.settings({"ui_review": {"capture": {}}})["capture"])
        for bad in ("make shots", {"timeout": 60}, {"command": " "}, {"command": "x" * 2001}, {"command": "x", "timeout": 5},
                    {"command": "x", "timeout": 9999}, {"command": "x", "timeout": "60"}, {"command": "x", "timeout": True}, {"command": 7}):
            with self.assertRaises(ux.ConfigError, msg=bad):
                ux.settings({"ui_review": {"capture": bad}})


class CheckConfigTests(unittest.TestCase):
    def test_check_config_reports_a_bad_capture_entry_and_accepts_a_good_one(self):
        from orchestrator import config_validation
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project.json"
            project.write_text(json.dumps({"ui_review": {"capture": {"command": "make shots", "timeout": 1}}}))
            self.assertEqual(config_validation._ui_review_errors(project),
                             ["ui_review: ui_review.capture.timeout is the seconds it may run, between 10 and 3600."])
            project.write_text(json.dumps({"ui_review": {"capture": {"command": "make shots", "timeout": 600}}}))
            self.assertEqual(config_validation._ui_review_errors(project), [])


class CaptureCommandTests(unittest.TestCase):
    def capture(self, command, timeout=60, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            project, out = Path(tmp) / "project", Path(tmp) / "screens"
            project.mkdir()
            shots, problem = native.capture_command(project, cfg(command, timeout), out, log=lambda line: None, **kwargs)
            files = sorted(p.name for p in out.glob("*")) if out.exists() else []
            return shots, problem, files, out, project

    def test_images_the_command_writes_become_screens_with_captions_and_dark_flags(self):
        shots, problem, files, out, project = self.capture(f"{sys.executable} -c \"{WRITER}\"", job_id="job-7", dark_mode=True)
        self.assertEqual(problem, "")
        self.assertEqual([(s["route"], s["dark"], s["width"]) for s in shots], [("Eye level, CamelotHills", False, 0), ("wide dark", True, 0)])
        self.assertEqual(files, [s["file"] for s in shots])  # only the two images, copied under safe names
        self.assertTrue(all(f.startswith("project-") and f.endswith(".png") for f in files))

    def test_the_command_runs_in_the_project_folder_with_the_job_and_dark_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve() / "project"
            project.mkdir()
            seen = {}

            def fake(command, root, env, timeout):
                seen.update(root=root, job=env["ORCHESTRATOR_CAPTURE_JOB"], dark=env["ORCHESTRATOR_CAPTURE_DARK"], timeout=timeout)
                (Path(env["ORCHESTRATOR_CAPTURE_DIR"]) / "a.png").write_bytes(PNG)
                return 0, ""

            for dark, want in ((True, "1"), (False, "0")):
                native.capture_command(project, cfg("x", 90), Path(tmp) / "out", job_id="j1", dark_mode=dark, log=lambda l: None, run=fake)
                self.assertEqual((seen["root"], seen["job"], seen["dark"], seen["timeout"]), (project, "j1", want, 90))

    def test_a_failing_command_says_why_and_its_images_are_not_used(self):
        shots, problem, files, *_ = self.capture(f"{sys.executable} -c \"{WRITER}\"; echo 'Unity license check failed' >&2; exit 3")
        self.assertEqual((shots, files), ([], []))
        self.assertEqual(problem, "The project's capture command failed (exit 3): Unity license check failed.")

    def test_a_command_that_writes_nothing_says_so(self):
        shots, problem, *_ = self.capture("true")
        self.assertEqual(shots, [])
        self.assertIn("wrote no images to $ORCHESTRATOR_CAPTURE_DIR", problem)

    def test_a_command_that_cannot_start_is_reported(self):
        def broken(*args):
            raise OSError("bash is missing")
        shots, problem, *_ = self.capture("x", run=broken)
        self.assertEqual((shots, problem), ([], "The project's capture command couldn't start: bash is missing."))

    def test_a_command_that_runs_out_of_time_is_stopped_with_everything_it_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child.pid"
            command = f"sleep 300 & echo $! > {marker}; wait"  # a helper process, as a game engine has
            started = time.time()
            shots, problem, *_ = self.capture(command, timeout=1)
            self.assertLess(time.time() - started, 20)
            self.assertEqual(shots, [])
            self.assertIn("didn't finish in 1 minute and was stopped", problem)
            pid = int(marker.read_text())
            time.sleep(0.3)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_too_many_empty_or_oversized_images_are_left_out_and_counted(self):
        def fake(command, root, env, timeout):
            folder = Path(env["ORCHESTRATOR_CAPTURE_DIR"])
            for n in range(native.CAPTURE_MAX_IMAGES + 3):
                (folder / f"s{n:02d}.png").write_bytes(PNG)
            (folder / "empty.png").write_bytes(b"")
            return 0, ""

        with tempfile.TemporaryDirectory() as tmp:
            shots, problem = native.capture_command(Path(tmp), cfg("x"), Path(tmp) / "out", log=lambda l: None, run=fake)
        self.assertEqual(len(shots), native.CAPTURE_MAX_IMAGES)
        self.assertIn("4 images from the project's capture command were left out", problem)


class RunnerWiringTests(unittest.TestCase):
    def test_the_review_uses_the_command_instead_of_guessing_devices(self):
        import ux_review_run as runner
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen = {}

            def fake(project, config, out_dir, **kwargs):
                seen.update(config=config, job_id=kwargs["job_id"], dark=kwargs["dark_mode"])
                return [{"file": "project-01-a.png", "route": "A", "width": 0, "dark": False}], "one image was left out"

            settings = ux.settings({"ui_review": {"capture": {"command": "make shots"}, "dark_mode": True}})
            with patch.object(runner, "ROOT", root), patch.object(runner.native, "capture_command", side_effect=fake), \
                    patch.object(runner, "preview_web_target", return_value=({**settings, "url": ""}, "")), \
                    patch.object(runner, "can_auto_preview", return_value=False):
                shots, limits = runner.capture(settings, root / "screens", dark=None, job_id="j9")
        self.assertEqual([s["file"] for s in shots], ["project-01-a.png"])
        self.assertEqual(limits, ["one image was left out"])  # and no "No screens configured" note
        self.assertEqual((seen["config"], seen["job_id"], seen["dark"]), ({"command": "make shots", "timeout": 900}, "j9", True))

    def test_a_configured_command_stops_devices_being_picked_for_the_project(self):
        import ux_review_run as runner
        settings = ux.settings({"ui_review": {"capture": {"command": "make shots"}}})
        self.assertEqual(runner.with_detected_devices(settings, {}), (settings, ""))


if __name__ == "__main__":
    unittest.main()
