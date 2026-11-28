"""Durability and one owner per job.

Each finished task's commit is pushed to the job's own branch, so a computer that dies mid-job loses at most the task in
progress. Continue here moves a job: the computer it left stops working on it ("Last worked on …") until it's brought back.
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
        self.assertIn('print(f"      - Task {label} committed ({sha[:7]}).")\n    backup_push(job)', source)
        self.assertIn('commit_job_files(ai_modified + ai_untracked, commit_msg)\n            backup_push(job)', source)


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
        self.assertEqual((state["group"], state["label"], state["next"]), ("done", "Last worked on MacBook Air", None))
        with mock.patch.object(worker, "read_json", return_value=job), mock.patch.object(worker, "remove_task_views"), \
             mock.patch.object(worker, "prepare_git_branch") as prepare:
            worker.execute_job(self.path, resume=True)
        prepare.assert_not_called()  # nothing touched git or the AI here

    def test_home_lists_a_moved_job_among_the_jobs_not_completed(self):
        # it isn't finished: it lives on another computer. Home shows that computer's row in its place (its real state), and
        # the moved copy itself stays among the jobs when that row isn't there (the other computer never synced)
        app = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('const isFinished = (j) => j.state.group === "done" && !j.moved_to;', app)
        self.assertIn("const seen = new Set(jobs.filter((j) => !j.moved_to).concat(archivedJobs).map((j) => j.id));", app)
        self.assertIn("jobs.filter((j) => !(j.moved_to && ownedElsewhere.has(j.id))).concat(remoteJobs)", app)

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

    def test_the_heartbeats_moves_mark_older_copies_only(self):
        newer = self.root / ".orchestrator" / "jobs" / "j2.json"
        newer.write_text(json.dumps({"job_id": "j2", "status": "executing", "imported_at": 500.0}))  # brought back here since
        marked = ui.apply_moved_jobs({"j1": {"to": "MacBook Air", "imported_at": 100.0},
                                      "j2": {"to": "MacBook Air", "imported_at": 100.0},
                                      "missing": {"to": "MacBook Air", "imported_at": 100.0},
                                      "../bad": {"to": "x", "imported_at": 1.0}}, roots=[self.root])
        self.assertEqual(marked, ["j1"])
        self.assertEqual(json.loads(self.path.read_text())["moved_to"], "MacBook Air")
        self.assertNotIn("moved_to", json.loads(newer.read_text()))
        self.assertEqual(ui.apply_moved_jobs({"j1": {"to": "MacBook Air", "imported_at": 100.0}}, roots=[self.root]), [])

    def test_continue_here_restores_the_jobs_history_from_the_snapshot(self):
        from orchestrator import account
        source = {"job_id": "j9", "title": "Trees", "status": "debugging", "iteration": 6, "debug_phase": "propose",
                  "allowed_models": ["claude-sonnet-5"], "task_commits": {"0": "abc123"},
                  "clarification_history": [{"question": "Which zone?", "answer": "Camelot"}],
                  "debug_history": [{"hypothesis": "wind too strong", "result": {"tests_ok": False}}],
                  "plan": {"tasks": [{"title": "Sway"}], "risks": ["perf"]}, "worker_pid": 123, "blocker": {"kind": "git_lock"}}
        raw = {"job_id": "j9", "title": "Trees", "status": "debugging", "tasks": [{"title": "Sway"}], "tasks_count": 1,
               "record": account.job_record(source)}
        with mock.patch("shutil.which", return_value=None):
            ui.import_remote_job(self.root, {"job": raw, "source_machine_name": "Studio Mac"})
        job = json.loads((self.root / ".orchestrator" / "jobs" / "j9.json").read_text())
        for key in ("iteration", "debug_phase", "allowed_models", "task_commits", "clarification_history", "debug_history"):
            self.assertEqual(job[key], source[key], key)
        self.assertEqual(job["plan"]["risks"], ["perf"])
        self.assertNotIn("worker_pid", job)  # this computer's state never travels
        self.assertNotIn("blocker", job)
        self.assertIsNone(account.job_record({"status": "completed", "iteration": 3}))

    def test_taking_back_deduplicates_resolved_debug_history_and_preserves_iteration_order(self):
        from orchestrator import account
        hyp1 = "The earlier tuning addressed overall cover, but its mixed-view measurements can improve even when flowers remain hard to distinguish."
        hyp3 = "The static side is fine; the previous agent did not finish the per-view comparison."
        # Local older copy (moved away after iter 1 crashed leaving result='pending')
        self.path.write_text(json.dumps({
            "job_id": "j1", "title": "Camelot", "status": "debugging", "moved_to": "Mac", "owner_epoch": 6,
            "debug_history": [{"iteration": 1, "model": "gpt-6.2-astra", "hypothesis": hyp1, "action": "investigate", "result": "pending"}],
        }))
        # Remote newer copy from Mac (iter 1 resolved + iter 3 added)
        remote_source = {
            "job_id": "j1", "title": "Camelot", "status": "debugging", "iteration": 3, "owner_epoch": 7,
            "debug_history": [
                {"iteration": 1, "model": "gpt-6.2-astra", "hypothesis": hyp1, "action": "investigate",
                 "result": {"build_ok": False, "tests_ok": False, "notes": "The fix attempt changed no files"}},
                {"iteration": 3, "model": "claude-sonnet-5", "hypothesis": hyp3, "action": "investigate", "result": "pending"},
            ],
        }
        raw = {"job_id": "j1", "title": "Camelot", "status": "debugging", "owner_epoch": 7, "record": account.job_record(remote_source)}
        with mock.patch("shutil.which", return_value=None):
            ui.import_remote_job(self.root, {"job": raw, "source_machine_name": "Mac"})
        merged = json.loads(self.path.read_text())["debug_history"]
        self.assertEqual([e["iteration"] for e in merged], [1, 3])
        self.assertIsInstance(merged[0]["result"], dict)
        self.assertEqual(merged[1]["result"], "pending")

    def test_taking_back_deduplicates_clarification_history_and_prefers_answered(self):
        from orchestrator import account
        q = "Which zone manifest should take precedence when README disagrees?"
        self.path.write_text(json.dumps({
            "job_id": "j1", "title": "Camelot", "status": "debugging", "moved_to": "Mac", "owner_epoch": 2,
            "clarification_history": [{"timestamp": "2026-11-21T08:38:00", "asker": "planner", "question": q, "answer": "Use manifest"}],
        }))
        remote_source = {
            "job_id": "j1", "title": "Camelot", "status": "debugging", "owner_epoch": 3,
            "clarification_history": [{"timestamp": "2026-11-21T08:38:00", "asker": "planner", "question": q, "answer": ""}],
        }
        raw = {"job_id": "j1", "title": "Camelot", "status": "debugging", "owner_epoch": 3, "record": account.job_record(remote_source)}
        with mock.patch("shutil.which", return_value=None):
            ui.import_remote_job(self.root, {"job": raw, "source_machine_name": "Mac"})
        merged = json.loads(self.path.read_text())["clarification_history"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["answer"], "Use manifest")

    def test_the_page_moves_the_older_copy_and_refuses_while_the_other_computer_works(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Number(rj.imported_at || 0) > Number(localImportedAt.get(jid) || 0)", app)
        self.assertIn("api(`jobs/${encodeURIComponent(j.id)}/moved`", app)
        # a run going there must not silently fork a second copy: offer switching to that machine or stopping that run first
        self.assertIn("const busy = rj.active_run ? `On ${machineLabel} now", app)
        self.assertIn("data-switch-machine", app)
        self.assertIn("data-stop-and-take-over", app)
        self.assertIn("async function stopRemoteRunAndTakeOver(", app)
        server = (ROOT / "orchestrator" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn("This job moved to {moved} and is worked on there now.", server)


if __name__ == "__main__":
    unittest.main()
