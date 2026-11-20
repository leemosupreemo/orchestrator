"""The content-free nudge: a computer that pushed shared state says which commit with its heartbeat (a project key and a hash),
and the account's other computers fetch at once instead of on their next minute (docs/state-sync.md)."""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cloud" / "functions"))
import control_plane as cp  # noqa: E402
from orchestrator import account, product_sync, state_ref  # noqa: E402
from orchestrator.prd import Prd, replace_section, template  # noqa: E402
from tests import test_control_plane as plane_tests  # noqa: E402
from tests.test_web_ui import ServerTestCase  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class NudgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        remote = base / "remote.git"
        git(base, "init", "-q", "--bare", str(remote))
        self.root = {}
        for name in ("studio", "laptop"):
            root = base / name
            root.mkdir()
            git(root, "init", "-q", "-b", "main")
            git(root, "remote", "add", "origin", str(remote))
            (root / ".orchestrator").mkdir()
            state_ref.set_consent(root, True)
            self.root[name] = root

    def push_from_studio(self, pitch="Sells shoes."):
        Prd(self.root["studio"], self.root["studio"] / ".orchestrator").write(replace_section(template(), "pitch", pitch), "you")
        self.assertEqual(product_sync.sync_project(self.root["studio"], name="Studio"), "pushed")

    def test_both_computers_name_the_project_the_same_and_only_a_pushing_computer_reports(self):
        self.assertEqual(state_ref.project_key(self.root["studio"]), state_ref.project_key(self.root["laptop"]))
        self.assertRegex(state_ref.project_key(self.root["studio"]), r"^[0-9a-f]{32}$")
        self.assertEqual(state_ref.local_heads([self.root["studio"]]), {})  # nothing pushed yet
        self.push_from_studio()
        heads = state_ref.local_heads([self.root["studio"]])
        self.assertEqual(list(heads.values())[0]["sha"], git(self.root["studio"], "rev-parse", state_ref.TRACKING_REF))
        for scope in state_ref.SCOPES:
            state_ref.set_consent(self.root["studio"], False, scope)
        self.assertEqual(state_ref.local_heads([self.root["studio"]]), {})  # stopped sharing everything: nothing reported

    def test_a_computer_is_behind_until_it_has_the_newest_commit(self):
        self.push_from_studio()
        heads = {k: v["sha"] for k, v in state_ref.local_heads([self.root["studio"]]).items()}
        laptop = [self.root["laptop"]]
        self.assertTrue(state_ref.behind(laptop, heads))
        product_sync.sync_project(self.root["laptop"], name="Laptop")
        self.assertFalse(state_ref.behind(laptop, heads))
        self.assertFalse(state_ref.behind([self.root["studio"]], heads))  # its own push
        self.assertFalse(state_ref.behind(laptop, None))
        for scope in state_ref.SCOPES:
            state_ref.set_consent(self.root["laptop"], False, scope)
        self.assertFalse(state_ref.behind(laptop, {state_ref.project_key(self.root["laptop"]): "f" * 40}))  # not shared here

    def test_the_heads_go_through_the_real_control_plane_and_reach_the_other_computer(self):
        plane = cp.ControlPlane(cp.MemoryStore())
        case = plane_tests.PlaneCase("setUp")
        case.plane, case.assertEqual = plane, self.assertEqual
        machines = {name: dict(zip(("machine_id", "machine_secret"), case.pair(name=name))) for name in ("Studio", "Laptop")}

        def call(path, body=None, machine=None):
            status, reply = plane.handle("POST", f"/cp/{path}", {"authorization": f"Machine {machine['machine_id']}:{machine['machine_secret']}"},
                                         body or {}, plane_tests.verify_user)
            if status != 200:
                raise account.AccountError(reply.get("error", "error"), status)
            return reply

        with patch.object(account, "call", side_effect=call):
            self.push_from_studio()
            account.heartbeat(machines["Studio"], "https://studio.example.com", state_heads=state_ref.local_heads([self.root["studio"]]))
            reply = account.heartbeat(machines["Laptop"], "https://laptop.example.com", state_heads=state_ref.local_heads([self.root["laptop"]]))
        self.assertTrue(state_ref.behind([self.root["laptop"]], reply["state_heads"]))
        stored = json.dumps(plane.store.data["state_heads"])
        self.assertNotIn("Sells shoes", stored)                 # no content, only a key and a commit hash
        self.assertNotIn("remote", stored)                       # not even the repository's name


class HangingGitTests(unittest.TestCase):
    def test_a_git_that_hangs_or_is_missing_comes_back_as_a_failure_not_an_exception(self):
        with patch.object(state_ref.subprocess, "run", side_effect=subprocess.TimeoutExpired(["git"], 10)):
            res = state_ref._git(Path("."), "remote", "get-url", "origin", timeout=10)
            self.assertEqual((res.returncode, res.stderr), (124, b"git remote timed out after 10 seconds"))
            self.assertEqual(state_ref.remote_url(Path(".")), "")       # read as "no remote" for this round
        with patch.object(state_ref.subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertEqual(state_ref._git(Path("."), "status").returncode, 127)


class SyncThreadSurvivalTests(ServerTestCase):
    def test_a_round_that_raises_does_not_stop_the_sync_thread(self):
        rounds = []

        def run_round(root):
            rounds.append(1)
            if len(rounds) == 1:
                raise subprocess.TimeoutExpired(["git", "remote"], 10)   # what killed the thread on the real Mac
            self.server._stopping.set()
            return 0.01

        with patch.object(product_sync, "run_round", side_effect=run_round), patch("orchestrator.job_sync.run_round", return_value=0.01), \
                patch.object(product_sync, "CHECK_SECONDS", 0.01), patch("orchestrator.sync_health.record") as record:
            thread = self.server.start_state_sync()
            thread.join(5)
        self.assertGreaterEqual(len(rounds), 2)   # it tried again after the failure
        self.assertFalse(thread.is_alive())
        self.assertIn("Sharing stopped for a round", record.call_args_list[0].args[2])

class ServerNudgeTests(ServerTestCase):
    def test_a_heartbeat_reply_with_a_newer_commit_wakes_the_sync_at_once(self):
        rounds, woken = [], threading.Event()

        def run_round(root):
            rounds.append(time.time())
            woken.set()
            return 60  # the normal wait: only the nudge can bring the next round sooner

        with patch.object(product_sync, "run_round", side_effect=run_round), patch("orchestrator.job_sync.run_round", return_value=60), \
                patch.object(product_sync, "CHECK_SECONDS", 60):
            self.server.start_state_sync()
            beats = []

            def heartbeat(machine, endpoint, **kwargs):
                beats.append(kwargs)
                if len(beats) > 1:
                    self.server._stopping.set()
                return {"state_heads": {"a" * 32: "2" * 40}}

            with patch.object(account, "load_machine", return_value={"machine_id": "one"}), patch.object(account, "heartbeat", side_effect=heartbeat), \
                    patch.object(account, "sync_snapshot"), patch.object(state_ref, "behind", return_value=True):
                thread = self.server.start_heartbeat(lambda: "https://x.example.com", interval=.01)
                self.assertTrue(woken.wait(5), "the nudge didn't wake the sync")
                thread.join(3)
            self.server.wake_state_sync()  # let the sync thread see the stop
        self.assertIn("state_heads", beats[0])
        self.assertGreaterEqual(len(rounds), 1)


if __name__ == "__main__":
    unittest.main()
