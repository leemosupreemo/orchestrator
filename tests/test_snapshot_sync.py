"""Other computers see a job the way its own computer does: the snapshot carries the job page's state and the run going now,
it is sent within seconds of a change, and a job continued elsewhere says so on the computer it came from."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from orchestrator import account
from orchestrator.web import server as ui

ROOT = Path(__file__).resolve().parents[1]


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": self.state_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = Path(tempfile.mkdtemp())
        jobs = self.root / ".orchestrator" / "jobs"
        jobs.mkdir(parents=True)
        (jobs / "j1.json").write_text(json.dumps({
            "job_id": "j1", "title": "Vegetation", "status": "human-needed", "last_error": "git add failed",
            "passed_uncommitted": {"files": ["a.cs"]}, "imported_from_machine": "MacBook Air"}))

    def test_the_snapshot_carries_the_job_pages_state_and_the_live_run(self):
        snap = account.build_snapshot(root=self.root, live={"j1": {"label": "Fix attempt", "step": "Running tests"}})
        job = next(j for j in snap["recent_jobs"] if j["job_id"] == "j1")
        self.assertEqual(job["state"]["label"], "Blocked")  # not "Stopped", guessed from the status
        self.assertEqual(job["state"]["next"], {"action": "resume", "label": "Commit the work"})
        self.assertEqual(job["active_run"], {"label": "Fix attempt", "step": "Running tests"})
        self.assertEqual(job["imported_from_machine"], "MacBook Air")

    def test_live_runs_are_keyed_by_job(self):
        fake = SimpleNamespace(sessions=SimpleNamespace(list=lambda: [
            {"running": True, "job": "j1", "action": "debug", "progress": {"step": "Running tests"}},
            {"running": False, "job": "j2", "action": "resume"},
            {"running": True, "action": "test_suite"}]))
        self.assertEqual(ui.UIServer.live_job_runs(fake), {"j1": {"label": "Fix attempt", "step": "Running tests"}})

    def test_remote_progress_distinguishes_interrupted_builds_from_jobs_needing_attention(self):
        jobs = self.root / ".orchestrator" / "jobs"
        for job_id, record in {
            "partial": {"status": "human-needed", "ai_modified_files": ["a", "b", "c"]},
            "interrupted": {"status": "executing", "worker_pid": 0},
        }.items():
            (jobs / f"{job_id}.json").write_text(json.dumps({"job_id": job_id, "title": job_id, **record}))
        snapshot = account.build_snapshot(root=self.root)
        states = {job["job_id"]: job["state"] for job in snapshot["recent_jobs"]}
        self.assertEqual(states["partial"]["label"], "Paused")
        self.assertIs(states["partial"].get("in_progress"), False)
        self.assertEqual(states["interrupted"]["label"], "Paused")
        self.assertIs(states["interrupted"].get("in_progress"), True)


class SendOnChangeTests(unittest.TestCase):
    def setUp(self):
        self.snapshots = [{"recent_jobs": [{"job_id": "j1", "status": "executing"}]}]
        self.fake = SimpleNamespace(root=None, live_job_runs=lambda: {})
        patches = [mock.patch.object(account, "build_snapshot", side_effect=lambda **kw: self.snapshots[-1]),
                   mock.patch.object(account, "sync_snapshot")]
        self.build, self.sync = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def send(self, sent):
        return ui.UIServer.send_snapshot_if_changed(self.fake, {"machine_id": "m"}, sent)

    def test_a_snapshot_is_sent_once_per_change(self):
        first = self.send("")
        self.assertTrue(first)
        self.assertEqual(self.send(first), first)  # nothing changed: not sent again
        self.assertEqual(self.sync.call_count, 1)
        self.snapshots.append({"recent_jobs": [{"job_id": "j1", "status": "review-needed"}]})
        self.assertNotEqual(self.send(first), first)
        self.assertEqual(self.sync.call_count, 2)

    def test_a_failed_send_is_tried_again_next_time(self):
        self.sync.side_effect = account.AccountError("offline")
        self.assertEqual(self.send("old"), "old")
        self.sync.side_effect = None
        self.assertNotEqual(self.send("old"), "old")


class WiringTests(unittest.TestCase):
    def test_older_remote_jobs_keep_approval_and_progress_distinctions(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        start = app.index("const remoteState =")
        fallback = app[start:app.index("\n    };", start) + len("\n    };")]
        progress = "const jobInProgress = " + re.search(r'working: \["In progress", (.*)\],', app).group(1) + ";"
        jobs = [
            {"status": "planned", "type": "feature-plan", "approved": False},
            {"status": "planned", "type": "feature-plan", "approved": True},
            {"status": "planned", "type": "bug-fix"},
            {"status": "human-needed"},
        ]
        script = fallback + "\n" + progress + "\nconsole.log(JSON.stringify(JSON.parse(process.argv[1]).map(j => {const state = remoteState(j); return [state.label, jobInProgress({state})];})));"
        result = subprocess.run(["node", "-e", script, json.dumps(jobs)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [["Needs approval", False], ["Ready", False], ["Ready", False], ["Paused", False]])

    def test_changes_are_sent_within_seconds_and_the_page_uses_them(self):
        server = (ROOT / "orchestrator" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn("SNAPSHOT_CHECK_SECONDS = 10", server)
        self.assertIn('threading.Thread(target=snapshot_sync, daemon=True, name="snapshot-sync").start()', server)
        self.assertIn("sent = self.send_snapshot_if_changed(machine, sent)", server)
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("state: rj.state?.label ? { ...rj.state, next: null", app)  # another computer's job can't be acted on here
        self.assertIn("continued on ${esc(j.continued_on)}", app)
        self.assertIn("rj.imported_from_machine === homeRemoteSnaps.selfName", app)


if __name__ == "__main__":
    unittest.main()
