"""Several jobs at once: one works in a project folder at a time, and a new job that looks like an open one is asked about."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from orchestrator import folder_lock, job_overlap  # noqa: E402
from tests.test_web_ui import ServerTestCase, UI_HEADERS, ui  # noqa: E402

HOLD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from orchestrator import folder_lock
    with folder_lock.hold(sys.argv[2], job=sys.argv[3], title=sys.argv[4]):
        print("held", flush=True)
        time.sleep(float(sys.argv[5]))
""")


class FolderLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def holder_process(self, job="j1", title="Add login page", seconds="30"):
        proc = subprocess.Popen([sys.executable, "-c", HOLD, str(PACKAGE_ROOT), str(self.root), job, title, seconds],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertEqual(proc.stdout.readline().strip(), "held")
        return proc

    def test_a_second_job_waits_and_is_told_which_job_it_waits_for(self):
        self.assertIsNone(folder_lock.holder(self.root))
        proc = self.holder_process()
        self.assertEqual(folder_lock.holder(self.root)["title"], "Add login page")
        waited = []
        with self.assertRaises(TimeoutError):
            with folder_lock.hold(self.root, job="j2", title="Dark mode", on_wait=waited.append, poll=0.05, timeout=0.3):
                self.fail("got the folder while another job had it")
        self.assertEqual([w["job"] for w in waited], ["j1"])  # told once, not every poll
        proc.kill()
        proc.wait()
        with folder_lock.hold(self.root, job="j2", title="Dark mode", poll=0.05, timeout=5):
            self.assertEqual(folder_lock.holder(self.root)["job"], "j2")
        self.assertIsNone(folder_lock.holder(self.root))

    def test_a_crashed_job_never_leaves_the_folder_locked(self):
        proc = self.holder_process()
        proc.kill()  # no clean-up runs
        proc.wait()
        self.assertIsNone(folder_lock.holder(self.root))

    def test_the_waiting_job_starts_by_itself_when_the_folder_is_free(self):
        self.holder_process(seconds="0.4")
        started = time.monotonic()
        with folder_lock.hold(self.root, job="j2", poll=0.05, timeout=10):
            self.assertGreater(time.monotonic() - started, 0.2)

    def test_a_jobs_own_children_already_have_the_folder(self):
        with folder_lock.hold(self.root, job="j1", title="Parent"):
            self.assertEqual(os.environ[folder_lock.ENV], str(self.root.resolve()))
            # Run fix starting the worker again inside the same job: no waiting for its own parent.
            code = subprocess.run([sys.executable, "-c", HOLD, str(PACKAGE_ROOT), str(self.root), "j1", "Child", "0"],
                                  capture_output=True, text=True, timeout=10)
            self.assertEqual(code.stdout.strip(), "held")
        self.assertNotIn(folder_lock.ENV, os.environ)


class OverlapTests(unittest.TestCase):
    def test_the_same_work_matches_and_different_work_does_not(self):
        same = [("Add login page", "Add a login page with Google sign-in"), ("Add dark mode", "Add dark mode toggle to settings"),
                ("Show the score on the game over screen", "Game over screen shows final score"), ("Add login page", "add LOGIN pages")]
        different = [("Fix login bug", "Add login page"), ("Fix score bug", "Add score display"), ("Add", "Fix"), ("", "Add login page")]
        for a, b in same:
            self.assertGreater(job_overlap.similarity(a, b), 0, (a, b))
        for a, b in different:
            self.assertEqual(job_overlap.similarity(a, b), 0, (a, b))

    def test_only_open_jobs_count_closest_first(self):
        jobs = [{"id": "done", "title": "Add login page", "state": {"group": "done"}},
                {"id": "far", "title": "Login page with Google sign-in and Apple", "state": {"group": "working", "label": "Building"}},
                {"id": "near", "title": "Add the login page", "state": {"group": "needs_you", "label": "Approve plan"}},
                {"id": "other", "title": "Dark mode", "state": {"group": "working"}}]
        found = job_overlap.similar_jobs("Add login page", jobs)
        self.assertEqual([m["id"] for m in found], ["near", "far"])
        self.assertEqual(found[0]["state"], "Approve plan")


class ServerTests(ServerTestCase):
    def write_job(self, job_id, title, status="planned"):
        jobs = ui.jobs_dir(self.root)
        jobs.mkdir(parents=True, exist_ok=True)
        (jobs / f"{job_id}.json").write_text(json.dumps({"job_id": job_id, "title": title, "status": status, "type": "feature"}))

    def test_a_new_job_like_an_open_one_is_asked_about_and_can_be_created_anyway(self):
        self.write_job("20260101-feature-1", "Add a login page")
        started = []
        fake = lambda *a, **k: started.append(a) or type("S", (), {"id": "r1", "summary": lambda self: {"id": "r1"}, "job_id": None, "result_job": None, "running": False, "exit_code": 0})()
        with patch.object(self.server.sessions, "start", fake):
            res, data = self.request("POST", "/api/runs", body={"action": "new_job", "params": {"type": "feature", "summary": "Login page"}}, headers=UI_HEADERS)
            self.assertEqual(res.status, 409)
            self.assertIn("Add a login page", data["error"])
            self.assertEqual(data["duplicates"][0]["id"], "20260101-feature-1")
            self.assertEqual(started, [])
            res, data = self.request("POST", "/api/runs", body={"action": "new_job", "params": {"type": "feature", "summary": "Dark mode"}}, headers=UI_HEADERS)
            self.assertNotEqual(res.status, 409)  # different work: no question
            started.clear()
            self.request("POST", "/api/runs", body={"action": "new_job", "params": {"type": "feature", "summary": "Login page", "allow_duplicate": True}}, headers=UI_HEADERS)
            self.assertEqual(len(started), 1)
            self.assertNotIn("allow_duplicate", " ".join(started[0][2]))  # not passed on to the job

    def test_branch_switches_are_refused_while_a_job_has_the_folder(self):
        with folder_lock.hold(self.root, job="j1", title="Add login page"):
            res, data = self.request("POST", "/api/runs", body={"action": "git_checkout", "params": {"branch": "main"}}, headers=UI_HEADERS)
        self.assertEqual(res.status, 409)
        self.assertIn("Add login page", data["error"])

    def test_a_waiting_job_says_what_it_waits_for(self):
        state = ui.job_state({"status": "planned", "waiting_for": {"title": "Add login page"}, "worker_pid": os.getpid(), "worker_host": ui.socket.gethostname()})
        self.assertEqual(state["label"], "Waiting")
        self.assertIn("Add login page", state["reason"])
        stale = ui.job_state({"status": "planned", "waiting_for": {"title": "Add login page"}, "worker_pid": 0})
        self.assertNotEqual(stale["label"], "Waiting")  # its worker was stopped: it isn't waiting any more


class WorkerTests(unittest.TestCase):
    def test_the_worker_holds_the_folder_for_the_whole_run(self):
        src = (PACKAGE_ROOT / "orchestrator" / "scripts" / "worker_run.py").read_text()
        main = src[src.index("def main() -> None:"):]
        self.assertIn("with hold_project_folder(job_path):\n        execute_job(job_path, resume=args.resume)", main)

    def test_the_page_asks_about_duplicates_for_every_way_of_creating_a_job(self):
        app = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        api = app[app.index("async function api("):app.index("async function confirmDuplicateJob")]
        self.assertIn('res.status === 409 && Array.isArray(data.duplicates) && path === "runs" && body?.action === "new_job"', api)
        self.assertIn("allow_duplicate: true", api)

    def test_the_guide_explains_it(self):
        guide = (PACKAGE_ROOT / "docs" / "user-guide.md").read_text()
        section = guide[guide.index("## Running Several Jobs"):]
        for bit in ("One job at a time in a project folder", "**Waiting**", "Create anyway", "Jobs that change the same code"):
            self.assertIn(bit, section)


if __name__ == "__main__":
    unittest.main()
