import json
import os
import subprocess
import sys
import time
import unittest
from dataclasses import asdict, replace
from threading import Barrier
from unittest.mock import patch

from orchestrator import verification_candidate as candidate
from orchestrator import test_commands, verification_assist
from orchestrator.test_commands import ExtraTestCommand
from orchestrator.verification import command_digest
from orchestrator.verification_attempts import AttemptKey, AttemptStatus
from orchestrator.verification_coordinator import run_remote_check
from orchestrator.verification_placement import WorkerProbe
from orchestrator.verification_transport import ProbeRequest, repository_identity, ssh_probe

from tests import test_verification_candidate as candidate_tests


class WorkerProbeTests(unittest.TestCase):
    setUp = candidate_tests.CandidateTests.setUp

    def call_worker(self, request, root=None):
        return self.call_wire(request.to_wire(), root=root)

    def call_wire(self, wire, root=None):
        result = subprocess.run(
            [sys.executable, "-m", "orchestrator.scripts.verification_worker", str(root or self.helper)],
            input=json.dumps(wire), text=True, capture_output=True, check=True, timeout=10,
            env={**os.environ, "ORCHESTRATOR_VERIFICATION_WORKER_DIR": str(self.root / "journal")},
        )
        return json.loads(result.stdout)

    def test_start_runs_approved_check_and_reconnect_returns_saved_result(self):
        config = self.helper / ".orchestrator" / "project.json"
        config.parent.mkdir()
        command = ExtraTestCommand("one", "cat changed", parallel_verify=True)
        config.write_text(json.dumps({"test_commands": [
            {"name": command.name, "command": command.command, "parallel_verify": True},
        ]}))
        (self.owner / "changed").write_text("verified candidate")
        snapshot = candidate.create(self.owner, self.base, ["changed"])
        with candidate.publish(self.owner, "origin", snapshot) as ref:
            probe = ProbeRequest(repository_identity(self.owner), ref, snapshot, {"one": command_digest(command)})
            attempt = AttemptKey("1" * 32, "one", snapshot.commit, "2" * 32, "3" * 32,
                                 probe.repository, command_digest(command))
            start = probe.to_wire() | {"operation": "start", "attempt": attempt.__dict__}
            self.assertEqual(self.call_wire(start)["status"], "reserved")
            status = start | {"operation": "status"}
            for _ in range(50):
                result = self.call_wire(status)
                if result["status"] == "finished":
                    break
                time.sleep(0.05)
            self.assertEqual(result["status"], "finished")
            self.assertEqual(result["result"]["status"], "passed")
            self.assertEqual(result["result"]["output"], "verified candidate")
            self.assertEqual(self.call_wire(start), result)
            next_attempt = replace(attempt, attempt_id="4" * 32)

            def rpc(machine, allowed, key, operation, **kwargs):
                wire = probe.to_wire() | {"operation": operation, "attempt": asdict(key)}
                return AttemptStatus(**self.call_wire(wire))

            coordinated = run_remote_check(
                self.root / "owner-attempt.json", {"name": "air"}, ["air"], key=next_attempt,
                request=probe, expected_toolchain=result["result"]["toolchain"], timeout=10,
                rpc=rpc, sleep=lambda delay: time.sleep(min(delay, 0.05)),
            )
            self.assertEqual(coordinated.status, "finished")
            self.assertEqual(coordinated.result["output"], "verified candidate")

    def test_assist_runs_the_pair_concurrently_and_falls_back_on_lost_helper(self):
        commands = [ExtraTestCommand(name, "true", parallel_verify=True) for name in ("first", "second")]
        digests = [command_digest(command) for command in commands]
        settings = self.root / "config"
        settings.mkdir()
        (settings / "machines.json").write_text(json.dumps({"machines": [
            {"name": "air", "enabled": True, "execution_mode": "ssh", "verification_helper": True},
        ]}))
        (settings / "verification-timings.json").write_text(json.dumps({
            "local": {digest: [60, 60, 60] for digest in digests},
            "air": {**{digest: [60, 60, 60] for digest in digests}, "setup": [5, 5, 5]},
        }))
        repository = repository_identity(self.owner)
        (self.owner / "changed").write_text("uncommitted job change")
        report = WorkerProbe(1, repository, 10 ** 10, {command.name: "matching" for command in commands}, True)
        for disconnected in (False, True):
            with self.subTest(disconnected=disconnected):
                barrier = Barrier(2, timeout=5)
                local_checks = []

                def local(command, root):
                    local_checks.append(command.name)
                    if command.name == "first":
                        barrier.wait()
                    return local_runner(command, root)

                def remote(path, machine, allowed, *, key, request, **kwargs):
                    barrier.wait()
                    self.assertEqual(key.check, "second")
                    self.assertNotEqual(request.candidate.commit, self.base)
                    if disconnected:
                        return AttemptStatus("unavailable")
                    return AttemptStatus("finished", result=local_runner(commands[1], self.owner))

                local_runner = test_commands.run_one
                with patch.object(verification_assist, "toolchain", return_value="matching"), \
                     patch.object(verification_assist, "ssh_probe", return_value=report), \
                     patch.object(verification_assist, "run_remote_check", side_effect=remote), \
                     patch.object(test_commands, "run_one", side_effect=local):
                    results = verification_assist.run(
                        commands, self.owner, self.root / "out", self.root,
                        {"job_id": "job", "allowed_machines": ["local", "air"]}, job_owned=True,
                    )
                self.assertTrue(test_commands.passed(results))
                self.assertEqual(local_checks, ["first", "second"] if disconnected else ["first"])
                self.assertEqual(results[1].get("machine"), None if disconnected else "air")

    def test_source_change_after_pair_invalidates_remote_evidence(self):
        commands = [ExtraTestCommand(name, "true", parallel_verify=True) for name in ("first", "second")]
        snapshot = candidate.worktree_candidate(self.owner)
        first, second = (test_commands.run_one(command, self.owner) for command in commands)
        second["machine"] = "air"
        trailing = ExtraTestCommand("trailing", "echo different > changed")
        with patch.object(verification_assist, "_pair", return_value=({"first": first, "second": second}, snapshot.tree)):
            results = verification_assist.run([*commands, trailing], self.owner, self.root / "out", self.root, {"job_id": "job"})
        self.assertFalse(test_commands.passed(results))
        self.assertEqual([result["status"] for result in results], ["blocked", "blocked", "passed"])
        self.assertIn("source changed", (self.root / "out/test_second.log").read_text())

    def test_preflight_checks_real_checkout_and_matching_project_config(self):
        config = self.helper / ".orchestrator" / "project.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"test_commands": [
            {"name": "one", "command": "false", "parallel_verify": True, "requires": ["path:changed"]},
            {"name": "absent", "command": "false", "parallel_verify": True, "requires": ["path:missing"]},
        ]}))
        checks = {
            "one": command_digest(ExtraTestCommand("one", "false", requires=("path:changed",), parallel_verify=True)),
            "absent": command_digest(ExtraTestCommand("absent", "false", requires=("path:missing",), parallel_verify=True)),
        }
        snapshot = candidate.create(self.owner, self.base, [])
        with candidate.publish(self.owner, "origin", snapshot) as ref:
            request = ProbeRequest(repository_identity(self.owner), ref, snapshot, checks)
            probe = self.call_worker(request)
            rejected = self.call_worker(ProbeRequest(request.repository, ref, snapshot, {"one": "0" * 64}))
            wrong_request = ProbeRequest("0" * 64, ref, snapshot, checks)
            wrong = self.call_worker(wrong_request)
        self.assertTrue(probe["candidate_available"])
        self.assertEqual(set(probe["toolchains"]), {"one"})
        self.assertGreater(probe["disk_free_bytes"], 0)
        self.assertFalse(wrong["candidate_available"])
        self.assertEqual(wrong["toolchains"], {})
        self.assertFalse(rejected["candidate_available"])
        self.assertEqual(candidate_tests.git(self.helper, "for-each-ref", candidate.REF_PREFIX), "")

    def test_ssh_respects_job_policy_and_validates_worker_response(self):
        snapshot = candidate.create(self.owner, self.base, [])
        request = ProbeRequest(repository_identity(self.owner), candidate.REF_PREFIX + "1" * 32, snapshot, {})
        machine = {"name": "air", "enabled": True, "verification_helper": True, "execution_mode": "ssh",
                   "repo_path": str(self.helper), "ssh_target": ["-bad", "mac-air"]}
        payload = {"protocol": 1, "repository": request.repository, "disk_free_bytes": 500,
                   "toolchains": {}, "candidate_available": True}
        with patch("orchestrator.verification_transport.subprocess.run") as run:
            self.assertIsNone(ssh_probe(machine, ["local"], request))
            run.assert_not_called()
            run.return_value.stdout = json.dumps(payload)
            self.assertTrue(ssh_probe(machine, ["air"], request).candidate_available)
            self.assertIn("mac-air", run.call_args.args[0])
            self.assertNotIn("-bad", run.call_args.args[0])
            run.return_value.stdout = json.dumps({**payload, "candidate_available": "yes"})
            self.assertIsNone(ssh_probe(machine, ["air"], request))
