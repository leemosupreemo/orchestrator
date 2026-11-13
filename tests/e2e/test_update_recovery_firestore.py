"""Real Firestore transaction integration; all traffic stays in a local emulator.

Run: ORCHESTRATOR_E2E_UPDATE_FIRESTORE=1 python3 -m unittest tests.e2e.test_update_recovery_firestore -v
Requires Java, google-cloud-firestore, and the cached Firebase Firestore emulator jar.
"""
from __future__ import annotations

import concurrent.futures
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "cloud" / "functions"))


@unittest.skipUnless(os.environ.get("ORCHESTRATOR_E2E_UPDATE_FIRESTORE") == "1", "set ORCHESTRATOR_E2E_UPDATE_FIRESTORE=1")
class FirestoreUpdateRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import firestore
        from firestore_store import FirestoreStore
        import control_plane

        jar = Path.home() / ".cache/firebase/emulators/cloud-firestore-emulator-v1.20.4.jar"
        if not jar.is_file() or not shutil.which("java"):
            raise RuntimeError("Opt-in Firestore test requires Java and cached Firestore emulator v1.20.4")
        temporary = tempfile.TemporaryDirectory(prefix="orchestrator-update-firestore-")
        cls.addClassCleanup(temporary.cleanup)
        log_path = Path(temporary.name) / "emulator.log"
        log = log_path.open("w")
        cls.addClassCleanup(log.close)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen([
            "java", "-jar", str(jar), "--host", "127.0.0.1", "--port", str(port),
            "--project_id", "demo-orchestrator-update", "--single_project_mode", "--single_project_mode_error"],
            cwd=temporary.name, stdout=log, stderr=subprocess.STDOUT)

        def stop():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        cls.addClassCleanup(stop)
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f"Firestore emulator failed to start: {log_path.read_text()}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        environment = patch.dict(os.environ, {"FIRESTORE_EMULATOR_HOST": f"127.0.0.1:{port}"})
        environment.start()
        cls.addClassCleanup(environment.stop)
        cls.client = firestore.Client(project="demo-orchestrator-update", credentials=AnonymousCredentials())
        cls.addClassCleanup(cls.client.close)
        cls.store = FirestoreStore(cls.client)
        cls.control_plane = control_plane

    def setUp(self):
        self.now = 1_000_000.0
        self.plane = self.control_plane.ControlPlane(self.store, now=lambda: self.now)
        owner = "owner-" + os.urandom(8).hex()
        self.user = {"uid": owner, "email": "owner@example.test", "email_verified": True}
        self.mid, _, secret = self.plane._create_machine(owner, "owner@example.test", {
            "name": "Emulator Mac", "os": "macOS", "version": "test"}, "m_" + os.urandom(8).hex())
        self.auth = f"Machine {self.mid}:{secret}"
        self.plane.heartbeat(self.auth, {"packaged": True, "update_control": 1})

    def start(self):
        return self.plane.request_update_recovery(self.user, {"machine_id": self.mid})["update_operation"]

    def report(self, op, state):
        return self.plane.update_progress(self.auth, {"operation_id": op["id"], "state": state})

    def test_concurrent_requests_join_and_terminal_ack_retains_public_result(self):
        barrier = threading.Barrier(4)
        def request():
            barrier.wait()
            return self.start()
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            operations = list(pool.map(lambda _: request(), range(4)))
        op = operations[0]
        self.assertTrue(all(other == op for other in operations))
        self.assertNotIn("expire_at", self.store.get("machine_updates", self.mid))
        self.assertTrue(self.report(op, "current")["accepted"])
        self.assertEqual(self.plane.heartbeat(self.auth, {})["update_operation"]["state"], "current")
        self.assertNotIn("update_operation", self.plane.heartbeat(self.auth, {"update_ack": op["id"]}))
        self.assertEqual(self.plane.list_machines(self.user)["machines"][-1]["update_operation"]["state"], "current")

    def test_real_transactions_serialize_cancel_and_install(self):
        for _ in range(4):
            op = self.start()
            barrier = threading.Barrier(2)
            def cancel():
                barrier.wait()
                try:
                    self.plane.cancel_update(self.user, {"machine_id": self.mid, "operation_id": op["id"]})
                    return True
                except self.control_plane.ControlError as error:
                    self.assertEqual(error.status, 409)
                    return False
            def install():
                barrier.wait()
                return self.report(op, "updating")["accepted"]
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                cancellation, installation = pool.submit(cancel), pool.submit(install)
                cancelled, installed = cancellation.result(), installation.result()
            self.assertNotEqual(cancelled, installed)
            actual = self.store.get("machine_updates", self.mid)
            self.assertEqual(actual["state"], "updating" if installed else "cancelling")
            if cancelled:
                self.assertFalse(self.report(op, "updating")["accepted"])
                self.assertTrue(self.report(op, "cancelled")["accepted"])
            else:
                self.assertTrue(self.report(op, "current")["accepted"])

    def test_install_survives_expiry_and_stale_progress_cannot_replace_new_operation(self):
        op = self.start()
        self.assertTrue(self.report(op, "updating")["accepted"])
        self.now += 1801
        self.assertEqual(self.start()["id"], op["id"])
        self.assertEqual(self.plane.heartbeat(self.auth, {})["update_operation"]["state"], "updating")
        with self.assertRaises(self.control_plane.ControlError) as conflict:
            self.plane.cancel_update(self.user, {"machine_id": self.mid, "operation_id": op["id"]})
        self.assertEqual(conflict.exception.status, 409)
        self.assertTrue(self.report(op, "current")["accepted"])
        self.plane.heartbeat(self.auth, {"packaged": True, "update_control": 1})
        new = self.start()
        self.assertNotEqual(new["id"], op["id"])
        self.assertFalse(self.report(op, "updating")["accepted"])
        self.now += 1801
        expired = self.report(new, "updating")
        self.assertFalse(expired["accepted"])
        self.assertEqual(expired["update_operation"]["state"], "expired")
