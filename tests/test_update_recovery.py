"""Owner-only update recovery and atomic cancellation/install arbitration."""
import concurrent.futures

from tests.test_control_plane import PlaneCase


class UpdateRecoveryTests(PlaneCase):
    def setUp(self):
        super().setUp()
        self.mid, self.secret = self.pair()
        self.auth = f"Machine {self.mid}:{self.secret}"
        self.heartbeat(packaged=True, update_control=1, capabilities={"preview_remote": True})

    def heartbeat(self, **body):
        fields = {"packaged": True, "update_control": 1, **body}
        return self.call("POST", "/cp/machine/heartbeat", fields, machine_auth=self.auth)

    def start(self, user="alice-token"):
        return self.call("POST", "/cp/machines/update-recovery", {"machine_id": self.mid}, user=user)

    def cancel(self, op):
        return self.call("POST", "/cp/machines/cancel-update", {"machine_id": self.mid, "operation_id": op["id"]}, user="alice-token")

    def progress(self, op, state):
        return self.call("POST", "/cp/machine/update-progress", {"operation_id": op["id"], "state": state}, machine_auth=self.auth)

    def operation(self):
        status, result = self.start()
        self.assertEqual(status, 200, result)
        return result["update_operation"]

    def test_ownership_auth_and_supported_installer(self):
        self.assertEqual(self.start(user=None)[0], 401)
        self.assertEqual(self.start(user="bob-token")[0], 404)
        for fields in ({"packaged": True, "update_control": 0}, {"packaged": False, "update_control": 1}, {"packaged": True, "update_control": True}):
            self.heartbeat(**fields)
            self.assertEqual(self.start()[0], 409)

    def test_dedup_delivery_ack_and_safe_public_metadata(self):
        op = self.operation()
        self.assertEqual(op, self.operation())
        self.assertEqual(op["expires"] - op["created"], 1800)
        for _ in range(2):
            self.assertEqual(self.heartbeat()[1]["update_operation"], op)
        view = self.call("GET", "/cp/machines", user="alice-token")[1]["machines"][0]
        self.assertEqual(view["update_operation"], op)
        self.heartbeat(packaged=True, update_control=1, capabilities={"preview_remote": "secret", "private": True})
        view = self.call("GET", "/cp/machines", user="alice-token")[1]["machines"][0]
        self.assertEqual(view["capabilities"], {})
        self.assertTrue(self.progress(op, "current")[1]["accepted"])
        self.heartbeat(update_ack=op["id"])
        self.assertNotIn("update_operation", self.heartbeat()[1])
        self.assertEqual(self.call("GET", "/cp/machines", user="alice-token")[1]["machines"][0]["update_operation"]["state"], "current")

    def test_cancel_is_pending_until_ack_and_late_progress_cannot_override(self):
        op = self.operation()
        self.assertEqual(self.cancel(op)[1]["update_operation"]["state"], "cancelling")
        self.assertEqual(self.heartbeat()[1]["update_operation"]["action"], "cancel")
        for state in ("checking", "updating", "current", "failed"):
            self.assertFalse(self.progress(op, state)[1]["accepted"])
        self.assertTrue(self.progress(op, "cancelled")[1]["accepted"])
        self.assertFalse(self.progress(op, "current")[1]["accepted"])

    def test_expiry_stale_ids_and_monotonic_progress(self):
        op = self.operation()
        self.assertTrue(self.progress(op, "downloading")[1]["accepted"])
        self.assertFalse(self.progress(op, "checking")[1]["accepted"])
        self.assertFalse(self.progress({"id": "stale"}, "updating")[1]["accepted"])
        self.clock.t += 1801
        result = self.progress(op, "updating")[1]
        self.assertFalse(result["accepted"])
        self.assertEqual(result["update_operation"]["state"], "expired")
        self.assertNotEqual(self.operation()["id"], op["id"])
        self.assertEqual(self.cancel(op)[0], 409)

    def test_install_claim_does_not_expire_or_cancel(self):
        op = self.operation()
        self.assertTrue(self.progress(op, "updating")[1]["accepted"])
        self.clock.t += 1801
        self.assertEqual(self.cancel(op)[0], 409)
        self.assertEqual(self.operation()["id"], op["id"])
        self.assertEqual(self.heartbeat()[1]["update_operation"]["state"], "updating")
        self.assertTrue(self.progress(op, "current")[1]["accepted"])
        self.assertFalse(self.progress(op, "updating")[1]["accepted"])

    def test_cancel_install_concurrent_exactly_one_wins(self):
        for _ in range(30):
            op = self.operation()
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                cancel = pool.submit(self.cancel, op)
                claim = pool.submit(self.progress, op, "updating")
                cancelled, installed = cancel.result(), claim.result()
            self.assertNotEqual(cancelled[0] == 200, installed[1]["accepted"])
            self.progress(op, "current" if installed[1]["accepted"] else "cancelled")

    def test_progress_requires_matching_machine_auth_and_known_state(self):
        op = self.operation()
        payload = {"operation_id": op["id"], "state": "checking"}
        self.assertEqual(self.call("POST", "/cp/machine/update-progress", payload, user="alice-token")[0], 401)
        self.assertEqual(self.call("POST", "/cp/machine/update-progress", payload, machine_auth=f"Machine {self.mid}:wrong")[0], 401)
        other, secret = self.pair()
        status, result = self.call("POST", "/cp/machine/update-progress", payload, machine_auth=f"Machine {other}:{secret}")
        self.assertEqual(status, 200)
        self.assertFalse(result["accepted"])
        self.assertEqual(self.progress(op, "secret path /home/key")[0], 400)
        self.assertEqual(self.cancel({"id": "stale"})[0], 409)

    def test_old_terminal_progress_and_ack_do_not_affect_new_operation(self):
        old = self.operation()
        self.progress(old, "failed")
        new = self.operation()
        self.assertNotEqual(old["id"], new["id"])
        self.assertFalse(self.progress(old, "current")[1]["accepted"])
        self.assertEqual(self.heartbeat(update_ack=old["id"])[1]["update_operation"], new)
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            operations = list(pool.map(lambda _: self.operation(), range(20)))
        self.assertTrue(all(op["id"] == new["id"] for op in operations))

    def test_omitted_capabilities_remove_support_and_malformed_types_are_dropped(self):
        self.call("POST", "/cp/machine/heartbeat", {"packaged": True}, machine_auth=self.auth)
        self.assertEqual(self.start()[0], 409)
        for value in (True, "1", 1.0, {"secret": True}):
            self.heartbeat(update_control=value, capabilities=["preview_remote"])
            self.assertEqual(self.start()[0], 409)
        self.heartbeat(capabilities={"preview_remote": False, "other": True})
        view = self.call("GET", "/cp/machines", user="alice-token")[1]["machines"][0]
        self.assertEqual(view["capabilities"], {"preview_remote": False})
