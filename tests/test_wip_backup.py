"""Unfinished work backed up to a hidden ref and brought back on another computer. Throwaway repositories with a local
bare remote only, never this checkout."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import wip_backup


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class WipBackupTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.origin = base / "origin.git"
        git(base, "init", "-q", "--bare", str(self.origin))
        self.pro = base / "pro"
        git(base, "clone", "-q", str(self.origin), str(self.pro))
        for repo in (self.pro,):
            git(repo, "config", "user.email", "t@example.com")
            git(repo, "config", "user.name", "T")
        (self.pro / "keep.txt").write_text("one\n")
        (self.pro / "old.txt").write_text("old\n")
        git(self.pro, "add", ".")
        git(self.pro, "commit", "-q", "-m", "start")
        git(self.pro, "push", "-q", "origin", "HEAD:main")
        git(self.pro, "checkout", "-q", "-b", "ai/job-1")
        git(self.pro, "push", "-q", "origin", "ai/job-1")
        self.air = base / "air"
        git(base, "clone", "-q", "-b", "main", str(self.origin), str(self.air))
        self.job = {"branch": "ai/job-1", "status": "human-needed",
                    "ai_modified_files": ["keep.txt", "old.txt"], "ai_untracked_files": ["new.txt"]}

    def test_unfinished_work_goes_to_a_hidden_ref_and_nothing_here_changes(self):
        (self.pro / "keep.txt").write_text("two\n")
        (self.pro / "new.txt").write_text("brand new\n")
        (self.pro / "old.txt").unlink()
        status_before = git(self.pro, "status", "--porcelain")
        record = wip_backup.backup_job(self.pro, "job-1", self.job, machine="Pro", now=1000.0)
        self.assertEqual(record["files"], ["keep.txt", "new.txt", "old.txt"])
        self.assertEqual(git(self.pro, "status", "--porcelain"), status_before)  # working tree and index untouched
        self.assertEqual(git(self.pro, "log", "-1", "--format=%s"), "start")    # the branch didn't move
        self.assertIn(record["commit"], git(self.origin, "show-ref"))
        self.job["wip_backup"] = record
        self.assertIsNone(wip_backup.backup_job(self.pro, "job-1", self.job, now=1001.0))  # unchanged: not sent again

        restored = wip_backup.restore(self.air, "job-1", self.job)  # the Air is on main: it switches to the job's branch
        self.assertEqual(git(self.air, "branch", "--show-current"), "ai/job-1")
        self.assertEqual(restored, ["keep.txt", "new.txt", "old.txt"])
        self.assertEqual((self.air / "keep.txt").read_text(), "two\n")
        self.assertEqual((self.air / "new.txt").read_text(), "brand new\n")
        self.assertFalse((self.air / "old.txt").exists())
        self.assertEqual(git(self.air, "diff", "--cached", "--name-only"), "")  # uncommitted again, as on the Pro

    def test_the_repositorys_pre_push_hook_neither_runs_nor_blocks_a_backup(self):
        hook = self.pro / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\ntouch hook-ran\nexit 1\n")  # a test run that fails, as husky's often does mid-work
        hook.chmod(0o755)
        (self.pro / "keep.txt").write_text("two\n")
        record = wip_backup.backup_job(self.pro, "job-1", self.job, machine="Pro", now=1000.0)
        self.assertIn("commit", record, record)
        self.assertFalse((self.pro / "hook-ran").exists())

    def test_restore_never_overwrites_changes_here(self):
        (self.pro / "keep.txt").write_text("two\n")
        self.job["wip_backup"] = wip_backup.backup_job(self.pro, "job-1", self.job, now=1000.0)
        git(self.air, "checkout", "-q", "-b", "ai/job-1", "origin/ai/job-1")
        (self.air / "keep.txt").write_text("mine\n")
        with self.assertRaisesRegex(wip_backup.WipConflictError, "already have changes here") as ctx:
            wip_backup.restore(self.air, "job-1", self.job)
        self.assertEqual(ctx.exception.files, ["keep.txt"])
        self.assertEqual((self.air / "keep.txt").read_text(), "mine\n")

    def test_restore_succeeds_when_local_dirty_files_already_match_backup(self):
        (self.pro / "keep.txt").write_text("two\n")
        (self.pro / "new.txt").write_text("brand new\n")
        (self.pro / "old.txt").unlink()
        self.job["wip_backup"] = wip_backup.backup_job(self.pro, "job-1", self.job, machine="Pro", now=1000.0)

        git(self.air, "checkout", "-q", "-b", "ai/job-1", "origin/ai/job-1")
        (self.air / "keep.txt").write_text("two\n")
        (self.air / "new.txt").write_text("brand new\n")
        (self.air / "old.txt").unlink()

        restored = wip_backup.restore(self.air, "job-1", self.job)
        self.assertEqual(restored, ["keep.txt", "new.txt", "old.txt"])
        self.assertEqual((self.air / "keep.txt").read_text(), "two\n")
        self.assertEqual((self.air / "new.txt").read_text(), "brand new\n")
        self.assertFalse((self.air / "old.txt").exists())

    def test_restore_reports_only_differing_files_and_supports_explicit_overwrite(self):
        (self.pro / "keep.txt").write_text("two\n")
        (self.pro / "new.txt").write_text("from pro\n")
        (self.pro / "old.txt").unlink()
        self.job["wip_backup"] = wip_backup.backup_job(self.pro, "job-1", self.job, machine="Pro", now=1000.0)

        git(self.air, "checkout", "-q", "-b", "ai/job-1", "origin/ai/job-1")
        (self.air / "keep.txt").write_text("two\n")        # identical to backup: must not conflict
        (self.air / "new.txt").write_text("from air\n")    # different untracked file: conflicts
        (self.air / "old.txt").write_text("edited here\n") # locally edited but deleted in backup: conflicts

        with self.assertRaises(wip_backup.WipConflictError) as ctx:
            wip_backup.restore(self.air, "job-1", self.job)
        self.assertEqual(ctx.exception.files, ["new.txt", "old.txt"])
        self.assertEqual((self.air / "new.txt").read_text(), "from air\n")
        self.assertEqual((self.air / "old.txt").read_text(), "edited here\n")

        restored = wip_backup.restore(self.air, "job-1", self.job, overwrite=True)
        self.assertEqual(restored, ["keep.txt", "new.txt", "old.txt"])
        self.assertEqual((self.air / "keep.txt").read_text(), "two\n")
        self.assertEqual((self.air / "new.txt").read_text(), "from pro\n")
        self.assertFalse((self.air / "old.txt").exists())
        self.assertEqual(git(self.air, "diff", "--cached", "--name-only"), "")

    def test_restore_wip_endpoint_returns_409_conflicts_and_accepts_overwrite(self):
        import json
        from http import HTTPStatus
        from unittest.mock import MagicMock
        from orchestrator.web import server as ui

        (self.pro / "keep.txt").write_text("from pro\n")
        backup = wip_backup.backup_job(self.pro, "job-1", self.job, machine="Pro", now=1000.0)
        jobs_dir = self.air / ".orchestrator" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        job_path = jobs_dir / "job-1.json"
        job_path.write_text(json.dumps({
            "job_id": "job-1", "status": "human-needed", "branch": "ai/job-1",
            "imported_from_machine": "Pro", "wip_backup": backup,
            "ai_modified_files": ["keep.txt"],
        }))
        git(self.air, "checkout", "-q", "-b", "ai/job-1", "origin/ai/job-1")
        (self.air / "keep.txt").write_text("from air\n")

        def call_restore(body):
            handler = ui.UIHandler.__new__(ui.UIHandler)
            handler.server = MagicMock()
            handler.server.bootstrap_enabled = False
            handler.server.root = self.air
            handler.server.sessions.running_job_ids.return_value = set()
            handler._principal = {"role": "owner", "token": "t", "kind": "token"}
            handler._body = MagicMock(return_value=body)
            captured = []
            handler._json = lambda payload, status=HTTPStatus.OK: captured.append((status, payload))
            handler._api("POST", "/api/jobs/job-1/restore-wip", {})
            return captured[0]

        status, payload = call_restore({})
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(payload["conflicts"], ["keep.txt"])
        self.assertIn("already have changes here", payload["error"])
        self.assertEqual((self.air / "keep.txt").read_text(), "from air\n")

        status, payload = call_restore({"overwrite": True})
        self.assertEqual(status, HTTPStatus.OK)
        self.assertEqual(payload, {"ok": True, "files": ["keep.txt"]})
        self.assertEqual((self.air / "keep.txt").read_text(), "from pro\n")
        saved = json.loads(job_path.read_text())
        self.assertEqual(saved["wip_restored"]["commit"], backup["commit"])

    def test_nothing_is_backed_up_off_the_jobs_branch_or_for_a_finished_job(self):
        (self.pro / "keep.txt").write_text("two\n")
        self.assertIsNone(wip_backup.backup_job(self.pro, "job-1", {**self.job, "status": "completed"}))
        self.assertIsNone(wip_backup.backup_job(self.pro, "job-1", {**self.job, "moved_to": "Air"}))
        self.assertIsNone(wip_backup.backup_job(self.pro, "job-1", {**self.job, "branch": "ai/other"}))

    def test_a_failed_push_is_retried_later_not_every_heartbeat(self):
        git(self.pro, "remote", "set-url", "origin", "/nonexistent/repo.git")
        (self.pro / "keep.txt").write_text("two\n")
        failed = wip_backup.backup_job(self.pro, "job-1", self.job, now=1000.0)
        self.assertIn("error", failed)
        self.job["wip_backup"] = failed
        self.assertIsNone(wip_backup.backup_job(self.pro, "job-1", self.job, now=1000.0 + 60))
        self.assertIn("error", wip_backup.backup_job(self.pro, "job-1", self.job, now=1000.0 + wip_backup.RETRY_SECONDS + 1))


    def test_work_never_uploaded_is_kept_on_a_side_branch_when_the_job_moves(self):
        (self.pro / "keep.txt").write_text("committed but not pushed\n")
        git(self.pro, "commit", "-q", "-am", "local only")
        (self.pro / "new.txt").write_text("not even committed\n")
        kept = wip_backup.keep_stranded(self.pro, "job-1", self.job, "Leemos MacBook Pro")
        self.assertEqual((kept["branch"], kept["commits"], kept["wip_files"]), ("ai/job-1-from-leemos-macbook-pro", 1, ["new.txt"]))
        refs = git(self.origin, "show-ref")
        self.assertIn("refs/heads/ai/job-1-from-leemos-macbook-pro", refs)
        self.assertIn("refs/orchestrator/wip/job-1-from-leemos-macbook-pro", refs)
        self.assertNotIn(git(self.pro, "rev-parse", "HEAD"), git(self.origin, "rev-parse", "ai/job-1"))  # the job's branch untouched
        self.assertTrue((self.pro / "new.txt").exists())

    def test_nothing_to_keep_when_everything_was_uploaded(self):
        self.assertIsNone(wip_backup.keep_stranded(self.pro, "job-1", self.job, "Pro"))



    def test_picking_a_job_up_brings_its_unfinished_work_in_one_step(self):
        import json
        from orchestrator.web import server as ui
        (self.pro / "keep.txt").write_text("two\n")
        backup = wip_backup.backup_job(self.pro, "job-1", self.job, machine="Pro", now=1000.0)
        raw = {"job_id": "job-1", "status": "human-needed", "branch": "ai/job-1", "owner_epoch": 1,
               "record": {"wip_backup": backup, "ai_modified_files": ["keep.txt"]}}
        # the endpoint's own steps, run directly: import, then restore
        result = ui.import_remote_job(self.air, {"job": raw, "source_machine_name": "Pro"})
        job = json.loads((self.air / ".orchestrator" / "jobs" / "job-1.json").read_text())
        self.assertEqual(job["owner_epoch"], 2)
        self.assertEqual(job["wip_backup"]["commit"], backup["commit"])
        self.assertEqual(wip_backup.restore(self.air, result["id"], job), ["keep.txt"])
        self.assertEqual((self.air / "keep.txt").read_text(), "two\n")


@unittest.skipUnless(__import__("shutil").which("node"), "node is required")
class NoticeTests(unittest.TestCase):
    def test_the_job_and_remote_pages_say_what_waits_where(self):
        import json
        app = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        grab = lambda name, end: app[app.index(f"function {name}("):app.index(end, app.index(f"function {name}("))]
        script = f"""
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
const ago = () => "2h ago"; const plural = (n, w) => `${{n}} ${{w}}${{n === 1 ? "" : "s"}}`;
{grab("wipRestoreNote", "document.addEventListener")}
{grab("remoteFreshnessHtml", "function strandedNote(")}
{grab("strandedNote", "pages[")}
const wip = {{ commit: "a".repeat(40), files: ["a", "b"], machine: "Pro", at: 1 }};
console.log(JSON.stringify({{
  offer: wipRestoreNote({{ job_id: "j", imported_from_machine: "Pro", wip_backup: wip }}, true),
  done: wipRestoreNote({{ job_id: "j", imported_from_machine: "Pro", wip_backup: wip, wip_restored: {{ commit: wip.commit }} }}, true),
  mine: wipRestoreNote({{ job_id: "j", wip_backup: wip }}, true),
  offline: remoteFreshnessHtml({{ online: false }}, "Pro", 5, wip),
  online: remoteFreshnessHtml({{ online: true }}, "Pro", 5, wip),
  stranded: strandedNote({{ stranded: {{ branch: "ai/x-from-pro", commits: 2, wip_commit: "c", wip_files: ["f"] }} }}),
}}));
"""
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertIn("Pro left 2 unfinished files", out["offer"])
        self.assertIn(">Bring the unfinished work here</button>", out["offer"])
        self.assertEqual(out["done"], "")
        self.assertEqual(out["mine"], "")  # this computer's own backup: nothing to bring
        self.assertIn("Pro is offline. Last synced from Pro 2h ago", out["offline"])
        self.assertIn("come with Continue here", out["offline"])
        self.assertNotIn("offline", out["online"])
        self.assertIn("2 commits on branch", out["stranded"])
        self.assertIn('formDialog(\n        "Replace local changes?"', app)
        self.assertIn('"Replace local files"', app)
        self.assertIn('body: { overwrite: true }', app)


if __name__ == "__main__":
    unittest.main()
