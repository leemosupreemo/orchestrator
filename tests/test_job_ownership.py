"""Durability and one owner per job.

Each finished task's commit is pushed to the job's own branch, so a computer that dies mid-job loses at most the task in
progress. Continue here moves a job: the computer it left stops working on it ("Moved to …") until it's brought back.
Git runs only in throwaway repositories here, never in this checkout."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestrator.web import server as ui

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator" / "scripts"))
import worker_run as worker  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class BackupPushTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.remote, self.repo = tmp / "remote.git", tmp / "repo"
        git(tmp, "init", "-q", "--bare", str(self.remote))
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / "a.txt").write_text("a")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "first")
        git(self.repo, "checkout", "-q", "-b", "job/grass")
        (self.repo / "b.txt").write_text("b")
        git(self.repo, "add", "b.txt")
        git(self.repo, "commit", "-q", "-m", "task 1")
        self.patches = [mock.patch.object(worker, "ROOT", self.repo),
                        mock.patch.object(worker, "PROJECT_CONFIG", SimpleNamespace(base_branch="main", git_remote=str(self.remote)))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_the_jobs_branch_is_pushed_after_its_work_is_committed(self):
        job = {"branch": "job/grass", "base_branch": "main", "branch_mode": "new"}
        self.assertTrue(worker.backup_push(job))
        self.assertEqual(git(self.remote, "rev-parse", "job/grass"), git(self.repo, "rev-parse", "HEAD"))
        self.assertIn("backed_up_at", job)

    def test_the_base_branch_and_manual_mode_are_never_pushed(self):
        for job in ({"branch": "main", "base_branch": "main"}, {"branch": "master", "base_branch": "develop"},
                    {"branch": "job/grass", "base_branch": "main", "branch_mode": "manual"}):
            self.assertFalse(worker.backup_push(job), job)
        self.assertEqual(git(self.remote, "branch", "--list"), "")

    def test_a_failed_push_is_recorded_and_never_stops_the_job(self):
        job = {"branch": "job/grass", "base_branch": "main"}
        git(self.repo, "remote", "set-url", "origin", str(self.remote.parent / "missing.git"))
        self.assertFalse(worker.backup_push(job))
        self.assertTrue(job["backup_error"])

    def test_each_task_and_the_final_commit_are_backed_up(self):
        source = (ROOT / "orchestrator" / "scripts" / "worker_run.py").read_text(encoding="utf-8")
        self.assertIn('print(f"      - Task {index + 1} committed ({sha[:7]}).")\n    backup_push(job)', source)
        self.assertIn('_git_commit_step(f"git commit -m {shlex.quote(commit_msg)}")\n            backup_push(job)', source)


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / ".orchestrator" / "jobs").mkdir(parents=True)
        self.path = self.root / ".orchestrator" / "jobs" / "j1.json"
        self.path.write_text(json.dumps({"job_id": "j1", "title": "Grass", "status": "human-needed", "last_error": "x"}))

    def test_a_job_continued_elsewhere_is_moved_and_never_run_here(self):
        self.assertTrue(ui.mark_job_moved(self.root, "j1", "MacBook Air"))
        self.assertFalse(ui.mark_job_moved(self.root, "j1", "MacBook Air"))  # already
        job = json.loads(self.path.read_text())
        state = ui.job_state(job)
        self.assertEqual((state["group"], state["label"], state["next"]), ("done", "Moved to MacBook Air", None))
        with mock.patch.object(worker, "read_json", return_value=job), mock.patch.object(worker, "remove_task_views"), \
             mock.patch.object(worker, "prepare_git_branch") as prepare:
            worker.execute_job(self.path, resume=True)
        prepare.assert_not_called()  # nothing touched git or the AI here

    def test_continuing_it_back_takes_the_newer_copy(self):
        ui.mark_job_moved(self.root, "j1", "MacBook Air")
        with mock.patch("shutil.which", return_value=None):  # no git fetch in a test
            res = ui.import_remote_job(self.root, {"job": {"job_id": "j1", "title": "Grass", "status": "review-needed", "pr_number": 17},
                                                   "source_machine_name": "MacBook Air"})
        job = json.loads(self.path.read_text())
        self.assertFalse(res["already_existed"])
        self.assertNotIn("moved_to", job)
        self.assertEqual((job["status"], job["pr_number"], job["imported_from_machine"]), ("review-needed", 17, "MacBook Air"))
        self.assertGreater(job["imported_at"], 0)

    def handler(self):
        handler = ui.UIHandler.__new__(ui.UIHandler)
        handler.server = mock.Mock()
        handler.server.sessions.running_job_ids.return_value = set()
        handler.server._publishing_jobs = set()
        handler.server._repository_lock = threading.Lock()
        handler._audit = mock.Mock()
        handler._require_owner = mock.Mock()
        return handler

    def test_the_server_refuses_to_run_a_moved_job_but_still_deletes_it(self):
        ui.mark_job_moved(self.root, "j1", "MacBook Air")
        handler = self.handler()
        for action in ("resume", "debug"):
            with self.assertRaises(ui.UIError) as err:
                handler._launch_run(action, {"job": "j1"}, self.root)
            self.assertIn("moved to MacBook Air", str(err.exception))
        handler.server.sessions.start.assert_not_called()
        handler._launch_run("delete_job", {"job": "j1"}, self.root)  # removing the stale copy is still allowed
        handler.server.sessions.start.assert_called_once()

    def test_the_page_moves_the_older_copy_and_refuses_while_the_other_computer_works(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Number(rj.imported_at || 0) > Number(localImportedAt.get(jid) || 0)", app)
        self.assertIn("api(`jobs/${encodeURIComponent(j.id)}/moved`", app)
        self.assertIn("if (rj.active_run) { // one owner per job", app)
        server = (ROOT / "orchestrator" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn("This job moved to {moved} and is worked on there now.", server)


if __name__ == "__main__":
    unittest.main()
