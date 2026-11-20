"""Jobs shared through the project's repository (phase 2 of docs/state-sync.md): two computers and a real bare remote."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator import job_sync, state_ref  # noqa: E402
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


class SharedJobsApiTests(ServerTestCase):
    def test_the_web_app_can_ask_for_shared_jobs(self):
        res, data = self.request("GET", "/api/shared-jobs")
        self.assertEqual((res.status, data), (200, {"jobs": [], "synced_at": 0}))

    def test_home_reads_shared_jobs_first_and_the_product_page_asks_about_jobs_separately(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn('const snaps = [...sharedItems, ...homeRemoteSnaps.snaps];', app)
        self.assertIn('await api("shared-jobs")', app)
        for text in ("Also share this project's jobs (their plans, your answers and fix history)", "Share jobs too", "Stop sharing jobs",
                     'data-prd-share-scope="jobs"'):
            self.assertIn(text, app)


if __name__ == "__main__":
    unittest.main()
