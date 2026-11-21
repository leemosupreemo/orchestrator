"""Blockers: a job stopped by the machine (a git lock, a sign-in, a missing tool, a full disk) pauses instead of starting AI fix
attempts, Home shows each blocker once with its jobs, and once it's cleared they resume by themselves, at most once an hour."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestrator import blockers
from orchestrator.web import server as ui

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator" / "scripts"))


class DetectTests(unittest.TestCase):
    def test_machine_problems_are_recognised(self):
        cases = {
            "fatal: Unable to create '/p/.git/index.lock': File exists.": ("git_lock", "project", "git"),
            "Failed to authenticate. API Error: 401 OAuth access token has expired. Re-authenticate to continue. (claude)": ("sign_in", "machine", "claude"),
            "bash: xcbeautify: command not found": ("tool", "machine", "xcbeautify"),
            "error: No space left on device": ("disk_full", "machine", "disk"),
            "CoreSimulator is out of date for the selected Xcode": ("simulator", "machine", "xcode"),
        }
        for text, expected in cases.items():
            found = blockers.detect(text)
            self.assertEqual((found["kind"], found["scope"], found["key"]), expected, text)

    def test_a_problem_in_the_code_is_not_a_blocker(self):
        for text in ("AssertionError: 3 != 4", "error: cannot find 'GrassSystem' in scope", "Permission denied for user in test fixture", ""):
            self.assertIsNone(blockers.detect(text), text)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / ".git").mkdir()
        self.lock = self.root / ".git" / "index.lock"

    def test_a_git_lock_is_cleared_only_when_old_and_unused(self):
        self.lock.write_text("")
        blocker = {**blockers.detect("index.lock': File exists."), "detected_at": time.time()}
        self.assertFalse(blockers.is_cleared(blocker, self.root))
        self.assertFalse(blockers.clear_git_lock(self.root))  # just made: a git command may be using it
        old = time.time() - 60
        os.utime(self.lock, (old, old))
        with mock.patch.object(blockers, "lock_in_use", return_value=True):
            self.assertFalse(blockers.clear_git_lock(self.root))
        with mock.patch.object(blockers, "lock_in_use", return_value=False):
            self.assertTrue(blockers.clear_git_lock(self.root))
        self.assertTrue(blockers.is_cleared(blocker, self.root))

    def test_a_blocker_that_comes_back_after_an_automatic_resume_is_not_resumed_again_within_the_hour(self):
        job = {"blocker": {"kind": "git_lock", "key": "git", "auto_resumed_at": time.time()}}
        job["last_blocker"] = job.pop("blocker")  # what the worker does as the resumed run starts
        blockers.record(job, blockers.detect("index.lock': File exists."))
        self.assertFalse(blockers.due_for_auto_resume(job["blocker"]))
        other = {}
        blockers.record(other, blockers.detect("index.lock': File exists."))
        self.assertTrue(blockers.due_for_auto_resume(other["blocker"]))

    def test_home_groups_jobs_by_the_blocker_they_wait_on(self):
        jobs_dir = self.root / ".orchestrator" / "jobs"
        jobs_dir.mkdir(parents=True)
        lock = blockers.detect("index.lock': File exists.")
        for name, blocker in (("a", lock), ("b", lock), ("c", None)):
            job = {"status": "human-needed", "title": name.upper(), **({"blocker": {**lock, "detected_at": 1.0}} if blocker else {})}
            ui.write_json_file(jobs_dir / f"{name}.json", job)
        groups = ui.blocker_groups(self.root)
        self.assertEqual(len(groups), 1)
        self.assertEqual([j["title"] for j in groups[0]["jobs"]], ["A", "B"])
        self.assertTrue(groups[0]["cleared"])  # no lock file in this folder

    def test_the_job_page_says_what_blocks_it_checked_now(self):
        job = {"status": "human-needed", "blocker": {**blockers.detect("bash: zzz-missing-tool: command not found"), "detected_at": 1.0}}
        state = ui.job_state(job, self.root)
        self.assertEqual((state["label"], state["next"]["label"]), ("zzz-missing-tool isn't installed", "Try again"))
        self.assertIn("Install zzz-missing-tool", state["steps"][0])
        with mock.patch("shutil.which", return_value="/usr/local/bin/zzz-missing-tool"):
            cleared = ui.job_state(job, self.root)
        self.assertEqual((cleared["label"], cleared["next"]["action"]), ("Blocker cleared", "resume"))


class AutoResumeTests(unittest.TestCase):
    def test_cleared_jobs_resume_once_and_a_still_blocked_one_waits(self):
        root = Path(tempfile.mkdtemp())
        (root / ".git").mkdir()
        jobs_dir = root / ".orchestrator" / "jobs"
        jobs_dir.mkdir(parents=True)
        cleared = {**blockers.detect("index.lock': File exists."), "detected_at": 1.0}  # no lock file: cleared
        still = {**blockers.detect("bash: zzz-missing-tool: command not found"), "detected_at": 1.0}
        ui.write_json_file(jobs_dir / "a.json", {"status": "human-needed", "blocker": cleared})
        ui.write_json_file(jobs_dir / "b.json", {"status": "human-needed", "blocker": still})
        # the server's own loop does this (no browser needed); Home's refresh and Clear the lock call the same method
        started = []
        server = SimpleNamespace(
            sessions=SimpleNamespace(running_job_ids=lambda: set(),
                                     start=lambda key, title, argv, *a, **k: started.append((key, argv)) or SimpleNamespace()),
            _publishing_jobs=set(), child_env=lambda: {}, audit=mock.Mock())
        resume = lambda **kw: ui.UIServer.resume_cleared_jobs(server, root, **kw)
        self.assertEqual(resume(), ["a"])
        self.assertEqual([key for key, _ in started], ["resume"])
        self.assertTrue(any(str(a).endswith("a.json") for a in started[0][1]))
        server.audit.record.assert_called_once()
        self.assertEqual(resume(), [])  # already resumed this hour: not again
        self.assertEqual(resume(force=True), ["a"])  # Clear the lock skips the wait

    def test_the_servers_own_loop_resumes_them(self):
        source = (ROOT / "orchestrator" / "web" / "server.py").read_text(encoding="utf-8")
        loop = source[source.index("    def start_notifier("):source.index("    def _plan_inputs(")]
        self.assertIn("self.resume_cleared_jobs(self.root)", loop)

class WiringTests(unittest.TestCase):
    def test_the_worker_pauses_on_a_blocker_instead_of_starting_fix_attempts(self):
        source = (ROOT / "orchestrator" / "scripts" / "worker_run.py").read_text(encoding="utf-8")
        failed = source.index("        if not build_ok or not test_ok:\n            blocker = blocker_in_logs(job)")
        self.assertLess(failed, source.index('job["status"] = "debugging"', failed))
        self.assertIn("blockers.record(job, blocker)", source)
        self.assertIn('job["last_blocker"] = job.pop("blocker")', source)

    def test_a_blocker_in_the_build_log_is_found(self):
        import worker_run
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(worker_run, "OUTPUT_DIR", Path(tmp)):
            (Path(tmp) / "j1").mkdir()
            (Path(tmp) / "j1" / "build.log").write_text("Compiling...\nerror: No space left on device\n")
            self.assertEqual(worker_run.blocker_in_logs({"job_id": "j1"})["kind"], "disk_full")
            self.assertIsNone(worker_run.blocker_in_logs({"job_id": "missing"}))

    def test_pausing_on_a_blocker_records_it_and_starts_no_fix(self):
        import worker_run
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "j1.json"
            job = {"job_id": "j1", "title": "Grass", "status": "executing"}
            with mock.patch.object(worker_run, "update_issue_status"), mock.patch.object(worker_run, "send_notifications") as notify:
                worker_run.pause_on_blocker(path, job, blockers.detect("No space left on device"), 16)
            saved = json.loads(path.read_text())
        self.assertEqual((saved["status"], saved["blocker"]["kind"]), ("human-needed", "disk_full"))
        self.assertNotIn("debug_phase", saved)
        self.assertIn("Low disk space", notify.call_args[0][1])

    def test_a_run_starting_moves_the_blocker_to_last_blocker(self):
        import worker_run
        job = {"job_id": "j1", "status": "human-needed", "moved_to": None,
               "blocker": {"kind": "git_lock", "key": "git", "auto_resumed_at": 5.0}}
        written = []
        with mock.patch.object(worker_run, "read_json", return_value=job), mock.patch.object(worker_run, "remove_task_views"), \
             mock.patch.object(worker_run, "write_json", side_effect=lambda p, j: written.append(dict(j))), \
             mock.patch.object(worker_run, "prepare_git_branch", side_effect=RuntimeError("stop here")), \
             mock.patch.object(worker_run, "update_issue_status"), mock.patch.object(worker_run, "send_notifications"):
            try:
                worker_run.execute_job(Path("j1.json"), resume=True)
            except Exception:
                pass
        self.assertTrue(written)
        self.assertNotIn("blocker", written[0])
        self.assertEqual(written[0]["last_blocker"]["auto_resumed_at"], 5.0)

    def test_home_shows_blockers_and_resumes_cleared_jobs(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('api("blockers", { method: "POST", body: {} }).catch(() => api("blockers")', app)
        self.assertIn("HomeStatus.render({git: homeGit, jobs, runs: state.runs, blockers: homeBlockers?.blockers || []})", app)
        self.assertIn('api("blockers/clear-lock", { method: "POST", body: {} })', app)

    def test_disk_full_steps_report_needed_and_available_space(self):
        report = {"required_bytes": 15 * blockers.disk_space.GIB, "free_bytes": int(13.3 * blockers.disk_space.GIB), "volume": "Project", "needed_bytes": int(1.7 * blockers.disk_space.GIB)}
        with mock.patch.object(blockers.disk_space, "status", return_value=report):
            self.assertEqual(blockers.steps({"kind": "disk_full"}, Path(".")), ["15 GB space needed. 13.3 GB space available."])


if __name__ == "__main__":
    unittest.main()
