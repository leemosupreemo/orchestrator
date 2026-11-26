"""Jobs shared through the project's repository (phase 2 of docs/state-sync.md): two computers and a real bare remote."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator import account, job_sync, model_outcomes, state_ref  # noqa: E402
from tests.test_web_ui import ServerTestCase  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class TwoComputers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.remote = base / "remote.git"
        git(base, "init", "-q", "--bare", str(self.remote))
        self.root = {}
        for name in ("studio", "laptop"):
            root = base / name
            root.mkdir()
            git(root, "init", "-q", "-b", "main")
            git(root, "remote", "add", "origin", str(self.remote))
            (root / ".orchestrator" / "jobs" / "archive").mkdir(parents=True)
            state_ref.set_consent(root, True, "jobs")
            self.root[name] = root

    def job(self, name, job_id="job-1", **fields):
        job = {"job_id": job_id, "title": "Make vegetation realistic", "type": "quick-fix", "status": "human-needed",
               "owner_epoch": 1, "updated_at": "2026-11-20T10:00:00", "plan": {"summary": "Denser grass", "tasks": []}, **fields}
        (self.root[name] / ".orchestrator" / "jobs" / f"{job_id}.json").write_text(json.dumps(job))
        return job

    def read_job(self, name, job_id="job-1"):
        for folder in ("jobs", "jobs/archive"):
            path = self.root[name] / ".orchestrator" / folder / f"{job_id}.json"
            if path.exists():
                return json.loads(path.read_text()), folder
        return None, None

    def sync(self, name):
        return job_sync.sync_project(self.root[name], name=name.capitalize())

    def remote_entry(self, job_id="job-1"):
        return json.loads(git(self.remote, "show", f"{state_ref.REF}:jobs/{job_id}.json"))

    def remote_head(self):
        return git(self.remote, "rev-parse", state_ref.REF)


class JobSyncTests(TwoComputers):
    def test_outcomes_survive_a_job_move_through_the_repository(self):
        from orchestrator.web.server import import_remote_job

        job = self.job("studio")
        model_outcomes.record(job, "planner", "chosen", "fallback", "plan_gate_passed")
        self.job("studio", **{model_outcomes.FIELD: job[model_outcomes.FIELD]})
        self.sync("studio")
        entry = self.remote_entry()
        account_entry = account._job_entry(job, "job-1", self.root["studio"], None)
        self.assertNotIn(model_outcomes.FIELD, account_entry)
        self.assertNotIn(model_outcomes.FIELD, account_entry.get("record") or {})
        import_remote_job(self.root["laptop"], {"job": entry, "source_machine_name": "Studio"})
        imported, _ = self.read_job("laptop")
        self.assertEqual(imported[model_outcomes.FIELD], job[model_outcomes.FIELD])

    def test_a_job_is_shared_and_the_other_computer_lists_it_without_taking_it(self):
        self.job("studio")
        self.assertEqual(self.sync("studio")["outcome"], "pushed")
        entry = self.remote_entry()
        self.assertEqual((entry["machine"], entry["title"], entry["summary"]), ("Studio", "Make vegetation realistic", "Denser grass"))
        self.assertEqual(self.sync("laptop")["outcome"], "idle")         # nothing of its own to share
        listed = job_sync.shared_jobs(self.root["laptop"], "Laptop")
        self.assertEqual([j["job_id"] for j in listed["jobs"]], ["job-1"])
        self.assertGreater(listed["synced_at"], 0)
        self.assertEqual(job_sync.shared_jobs(self.root["studio"], "Studio")["jobs"], [])  # never your own

    def test_continue_here_raises_ownership_and_the_old_copy_is_marked_moved_not_overwritten(self):
        self.job("studio")
        self.sync("studio")
        self.job("laptop", owner_epoch=2, imported_from_machine="Studio", imported_at=1795000000)  # Continue here
        self.assertEqual(self.sync("laptop")["outcome"], "pushed")
        self.assertEqual(self.remote_entry()["machine"], "Laptop")
        result = self.sync("studio")
        self.assertEqual(result["moved"], ["job-1"])
        self.assertEqual(self.read_job("studio")[0]["moved_to"], "Laptop")
        self.assertEqual(self.remote_entry()["machine"], "Laptop")   # the stale copy didn't push over the owner's

    def test_a_lower_ownership_number_never_overwrites_the_owners_copy(self):
        self.job("laptop", owner_epoch=3)
        self.sync("laptop")
        self.job("studio", owner_epoch=1, status="debugging", updated_at="2026-11-20T12:00:00")  # newer, but not the owner
        self.sync("studio")
        self.assertEqual((self.remote_entry()["machine"], self.remote_entry()["status"]), ("Laptop", "human-needed"))

    def test_the_owner_s_own_later_changes_replace_its_copy(self):
        self.job("studio")
        self.sync("studio")
        self.job("studio", status="review-needed", updated_at="2026-11-20T11:00:00")
        self.assertEqual(self.sync("studio")["outcome"], "pushed")
        self.assertEqual(self.remote_entry()["status"], "review-needed")

    def test_changes_that_say_nothing_new_are_not_pushed(self):
        self.job("studio")
        self.sync("studio")
        head = self.remote_head()
        self.job("studio", updated_at="2026-11-20T10:05:00")   # touched, nothing else
        self.assertEqual(self.sync("studio")["outcome"], "idle")
        self.assertEqual(self.remote_head(), head)

    def test_archiving_on_one_computer_archives_the_copy_on_the_other(self):
        self.job("studio")
        self.sync("studio")
        self.job("laptop", owner_epoch=0, imported_from_machine="Studio")   # a copy it holds, never continued
        archived = self.read_job("studio")[0] | {"status": "archived", "restore_status": "human-needed", "archived_at": 1795100000.0}
        (self.root["studio"] / ".orchestrator" / "jobs" / "job-1.json").unlink()
        (self.root["studio"] / ".orchestrator" / "jobs" / "archive" / "job-1.json").write_text(json.dumps(archived))
        self.sync("studio")
        events = json.loads(git(self.remote, "show", f"{state_ref.REF}:{job_sync.EVENTS}"))
        self.assertTrue(events["job-1"]["archived"])
        self.assertEqual(self.sync("laptop")["archived"], ["job-1"])
        self.assertEqual(self.read_job("laptop")[1], "jobs/archive")

    def test_jobs_are_only_shared_after_their_own_yes(self):
        state_ref.set_consent(self.root["studio"], False, "jobs")
        state_ref.set_consent(self.root["studio"], True, "product")   # yes to the requirements is not yes to jobs
        self.job("studio")
        self.assertEqual(self.sync("studio")["outcome"], "off")
        self.assertEqual(job_sync.shared_jobs(self.root["studio"], "Studio"), {"jobs": [], "synced_at": 0})
        self.assertEqual(subprocess.run(["git", "rev-parse", "--verify", "-q", state_ref.REF], cwd=self.remote).returncode, 1)

    def test_jobs_and_product_share_one_ref_without_disturbing_each_other(self):
        from orchestrator import product_sync
        from orchestrator.prd import Prd, replace_section, template
        state_ref.set_consent(self.root["studio"], True, "product")
        Prd(self.root["studio"], self.root["studio"] / ".orchestrator").write(replace_section(template(), "pitch", "Sells shoes."), "you")
        self.job("studio")
        self.assertEqual(product_sync.sync_project(self.root["studio"], name="Studio"), "pushed")
        self.assertEqual(self.sync("studio")["outcome"], "pushed")
        tree = git(self.remote, "ls-tree", "-r", "--name-only", state_ref.REF).split()
        self.assertEqual(sorted(tree), ["jobs/job-1.json", "product/prd.md"])  # no archive events yet, so no events file

    def test_task_scratch_files_are_not_shared_and_state_change_from_building_to_paused_pushes(self):
        from unittest.mock import patch
        from orchestrator.web import server as web_server
        self.job("studio", job_id="job-1", status="executing", worker_pid=999999)
        self.job("studio", job_id="job-1_task_1", status="executing", worker_pid=999999)
        with patch.object(web_server, "worker_alive", return_value=True):
            self.assertEqual(self.sync("studio")["outcome"], "pushed")
            self.assertEqual(self.remote_entry("job-1")["state"]["label"], "Building")
        tree = git(self.remote, "ls-tree", "-r", "--name-only", state_ref.REF).split()
        self.assertEqual(tree, ["jobs/job-1.json"])  # _task_1 scratch file is never shared
        with patch.object(web_server, "worker_alive", return_value=False):
            self.assertEqual(self.sync("studio")["outcome"], "pushed")
            self.assertEqual(self.remote_entry("job-1")["state"]["label"], "Paused")

    def test_full_job_context_brief_verification_and_runs_sync_through_repo_not_firestore(self):
        from orchestrator import job_runs
        from orchestrator.web import server as web_server

        studio_rt = self.root["studio"] / ".orchestrator"
        laptop_rt = self.root["laptop"] / ".orchestrator"
        brief_path = studio_rt / "output" / "job-1" / "brief.md"
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        brief_path.write_text("# Feature brief\n\n## Summary\nDenser grass in Camelot Hills.\n", encoding="utf-8")

        job = self.job(
            "studio",
            raw_input="Make Camelot Hills grass denser",
            clarification_asker="architect",
            clarification_asked_at="2026-11-26T12:00:00Z",
            clarification_run_id="20261126-120000-abc123",
            human_clarification_question="Architect raised concerns: Split helper.",
            verification={"status": "concerns", "comments": "Split helper.", "suggested_additions": ["Add dotnet test"]},
            spec={"summary": "Denser flora", "assumptions": ["Camelot Hills only"]},
            risk_assessment={"level": "low"},
            blocker={"kind": "env", "title": "Unity missing", "detail": "Install Unity"},
        )
        job_runs.record(studio_rt, {
            "id": "20261126-120000-abc123",
            "action": "new_job",
            "title": "Plan feature",
            "job": "job-1",
            "started": 1795000000.0,
            "ended": 1795000042.0,
            "exit_code": 0,
            "stopped": False,
            "result": {"headline": "Plan updated — architect review needs attention", "tone": "warn"},
            "machine": "Studio",
        })

        self.assertEqual(self.sync("studio")["outcome"], "pushed")
        entry = self.remote_entry("job-1")

        # Rich fields travel in refs/orchestrator/state, NOT Firestore account._job_entry
        account_entry = account._job_entry(job, "job-1", self.root["studio"], None)
        self.assertNotIn("extra", account_entry)
        self.assertNotIn("brief_md", account_entry)
        self.assertNotIn("runs", account_entry)
        self.assertEqual(entry["verification"]["suggested_additions"], ["Add dotnet test"])
        self.assertEqual(entry["extra"]["raw_input"], "Make Camelot Hills grass denser")
        self.assertEqual(entry["extra"]["clarification_asker"], "architect")
        self.assertIn("Denser grass in Camelot Hills.", entry["brief_md"])
        self.assertEqual(len(entry["runs"]), 1)
        self.assertEqual(entry["runs"][0]["machine"], "Studio")

        # Laptop imports from shared-jobs and keeps full context, brief, and synced run history
        web_server.import_remote_job(self.root["laptop"], {"job": entry, "source_machine_name": "Studio"})
        imported, _ = self.read_job("laptop")
        self.assertEqual(imported["raw_input"], "Make Camelot Hills grass denser")
        self.assertEqual(imported["verification"]["suggested_additions"], ["Add dotnet test"])
        self.assertEqual(imported["clarification_asker"], "architect")
        self.assertEqual(imported["clarification_run_id"], "20261126-120000-abc123")
        self.assertEqual(imported["spec"]["assumptions"], ["Camelot Hills only"])
        self.assertEqual(imported["blocker"]["title"], "Unity missing")
        self.assertIn("Denser grass in Camelot Hills.", (laptop_rt / "output" / "job-1" / "brief.md").read_text(encoding="utf-8"))

        laptop_runs = job_runs.for_job(laptop_rt, "job-1", [])
        self.assertEqual(len(laptop_runs), 1)
        self.assertEqual(laptop_runs[0]["id"], "20261126-120000-abc123")
        self.assertEqual(laptop_runs[0]["machine"], "Studio")
        self.assertEqual(laptop_runs[0]["status"], "succeeded")

        history = {r["id"]: r for r in web_server.run_history(self.root["laptop"])}
        self.assertIn("20261126-120000-abc123", history)
        self.assertTrue(history["20261126-120000-abc123"]["remote_log"])
        self.assertEqual(history["20261126-120000-abc123"]["exit_code"], 0)
        self.assertEqual(history["20261126-120000-abc123"]["machine"], "Studio")

        # Laptop adds a second run and pushes; Studio merges both runs on sync
        job_runs.record(laptop_rt, {
            "id": "20261126-121000-def456",
            "action": "schedule",
            "title": "Build",
            "job": "job-1",
            "started": 1795000600.0,
            "ended": 1795000720.0,
            "exit_code": 0,
            "stopped": False,
            "result": {"headline": "Built cleanly", "tone": "ok"},
            "machine": "Laptop",
        })
        self.assertEqual(self.sync("laptop")["outcome"], "pushed")
        self.assertEqual({r["id"] for r in self.remote_entry("job-1")["runs"]},
                         {"20261126-120000-abc123", "20261126-121000-def456"})
        self.sync("studio")
        self.assertEqual({r["id"] for r in job_runs.for_job(studio_rt, "job-1", [])},
                         {"20261126-120000-abc123", "20261126-121000-def456"})

    def test_multi_hop_move_updates_moved_to_on_original_computer(self):
        self.job("studio", owner_epoch=1)
        self.sync("studio")
        self.job("laptop", owner_epoch=2, imported_from_machine="Studio", imported_at=1795000000)
        self.sync("laptop")
        self.assertEqual(self.sync("studio")["moved"], ["job-1"])
        self.assertEqual(self.read_job("studio")[0]["moved_to"], "Laptop")

        # A third computer takes over with epoch 3: Studio's moved_to should update from Laptop to Mini
        mini = Path(self.tmp.name) / "mini"
        mini.mkdir()
        git(mini, "init", "-q", "-b", "main")
        git(mini, "remote", "add", "origin", str(self.remote))
        (mini / ".orchestrator" / "jobs" / "archive").mkdir(parents=True)
        state_ref.set_consent(mini, True, "jobs")
        (mini / ".orchestrator" / "jobs" / "job-1.json").write_text(json.dumps({
            "job_id": "job-1", "title": "Make vegetation realistic", "type": "quick-fix", "status": "human-needed",
            "owner_epoch": 3, "imported_from_machine": "Laptop", "imported_at": 1795000500,
            "updated_at": "2026-11-20T12:00:00", "plan": {"summary": "Denser grass", "tasks": []},
        }))
        self.assertEqual(job_sync.sync_project(mini, name="Mini")["outcome"], "pushed")
        self.assertEqual(self.sync("studio")["moved"], ["job-1"])
        self.assertEqual(self.read_job("studio")[0]["moved_to"], "Mini")


class SharedJobsApiTests(ServerTestCase):
    def test_the_web_app_can_ask_for_shared_jobs(self):
        res, data = self.request("GET", "/api/shared-jobs")
        self.assertEqual((res.status, data), (200, {"jobs": [], "synced_at": 0}))

    def test_home_reads_shared_jobs_first(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn('const snaps = [...sharedItems, ...homeRemoteSnaps.snaps];', app)
        self.assertIn('await api("shared-jobs")', app)
        self.assertNotIn("Stop sharing jobs", app)


if __name__ == "__main__":
    unittest.main()
