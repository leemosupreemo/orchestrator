"""Owner commands travel over authenticated heartbeats without leaking runner data."""
from tests.test_control_plane import PlaneCase


class ConnectionControlPlaneTests(PlaneCase):
    def setUp(self):
        super().setUp()
        self.mid, self.secret = self.pair()
        self.auth = f"Machine {self.mid}:{self.secret}"
        self.heartbeat()

    def heartbeat(self, **extra):
        return self.call("POST", "/cp/machine/heartbeat", {
            "endpoint": "https://runner.example.com", "api_version": 4, **extra,
        }, machine_auth=self.auth)

    def request(self, action="repair", user="alice-token", **extra):
        return self.call("POST", "/cp/machines/repair-connection", {
            "machine_id": self.mid, "action": action, **extra,
        }, user=user)

    def view(self):
        return self.call("GET", "/cp/machines", user="alice-token")[1]["machines"][0]

    def test_owner_only_versioned_actions_and_server_generated_ids(self):
        self.assertEqual(self.request(user=None)[0], 401)
        self.assertEqual(self.request(user="bob-token")[0], 404)
        self.assertEqual(self.request("execute_shell")[0], 400)
        self.assertEqual(self.request(["repair"])[0], 400)
        self.heartbeat(api_version=3)
        status, reply = self.request()
        self.assertEqual(status, 409)
        self.assertIn("update", reply["error"].lower())
        self.heartbeat()
        status, reply = self.request(id="attacker-chosen")
        self.assertEqual(status, 200)
        self.assertNotEqual(reply["connection_command"]["id"], "attacker-chosen")

    def test_commands_repeat_until_matching_ack_and_coalesce_after_completion(self):
        status, requested = self.request()
        self.assertEqual(status, 200)
        command = requested["connection_command"]
        self.assertEqual(set(command), {"id", "action", "created", "expires"})
        self.assertEqual(command["expires"] - command["created"], 900)
        self.assertTrue(self.view()["connection_command_pending"])
        self.assertEqual(self.request()[1]["connection_command"], command)
        # A second button cannot replace an in-flight command, even a different action.
        self.assertEqual(self.request("setup_funnel")[1]["connection_command"], command)
        for ack in (None, "wrong", "☃", "a" * 10000, {"id": command["id"]}, [command["id"]]):
            self.assertEqual(self.heartbeat(connection_ack=ack)[1]["connection_command"], command)
        self.assertNotIn("connection_command", self.heartbeat(connection_ack=command["id"])[1])
        self.assertFalse(self.view()["connection_command_pending"])
        self.assertNotIn("connection_command", self.heartbeat()[1])
        self.assertEqual(self.request()[0], 429)
        self.clock.t += 61
        replacement = self.request("setup_funnel")[1]["connection_command"]
        self.assertNotEqual(command["id"], replacement["id"])
        # An old ack must not suppress a newer command.
        self.assertEqual(self.heartbeat(connection_ack=command["id"])[1]["connection_command"], replacement)

    def test_expired_commands_are_never_delivered(self):
        status, reply = self.request()
        self.assertEqual(status, 200)
        command = reply["connection_command"]
        self.clock.t = command["expires"]
        self.assertFalse(self.view()["connection_command_pending"])
        self.assertNotIn("connection_command", self.heartbeat()[1])
        self.assertNotEqual(self.request()[1]["connection_command"]["id"], command["id"])

    def test_legacy_reset_is_one_shot_but_new_runners_receive_managed_repair(self):
        route = "/cp/machines/reset-tunnel"
        body = {"machine_id": self.mid}
        self.assertEqual(self.call("POST", route, body, user="alice-token")[0], 200)
        response = self.heartbeat()[1]
        self.assertFalse(response["reset_tunnel"])
        self.assertEqual(response["connection_command"]["action"], "repair")
        self.heartbeat(connection_ack=response["connection_command"]["id"], api_version=3)
        self.assertEqual(self.call("POST", route, body, user="alice-token")[0], 200)
        self.assertTrue(self.heartbeat(api_version=3)[1]["reset_tunnel"])
        self.assertFalse(self.heartbeat(api_version=3)[1]["reset_tunnel"])

    def test_known_connection_failure_is_independent_of_machine_online(self):
        for state in ("connected", "checking", "repairing", "failed", "needs_setup"):
            self.heartbeat(connection={"state": state, "provider": "funnel"})
            view = self.view()
            self.assertTrue(view["online"])
            self.assertEqual(view["reachable"], state not in {"failed", "needs_setup"})
            self.assertEqual(view["connection"]["state"], state)
        # Missing and invalid reports cannot erase known failures.
        self.heartbeat()
        self.heartbeat(connection={"state": "invented"})
        self.assertFalse(self.view()["reachable"])

    def test_connection_reports_are_allowlisted_bounded_and_do_not_store_private_objects(self):
        safe = "https://login.tailscale.com/f/funnel?node=n123"
        self.heartbeat(connection={
            "state": "needs_setup", "provider": "funnel", "endpoint": "https://runner.example.com",
            "checked_at": self.clock.t, "detail": "a\x00" * 1000, "setup_url": safe,
            "token": "SECRET", "project": {"private": True}, "credentials": {"password": "SECRET"},
        })
        connection = self.view()["connection"]
        self.assertEqual(connection["setup_url"], safe)
        self.assertEqual(connection["endpoint"], "https://runner.example.com")
        self.assertLessEqual(len(connection["detail"]), 400)
        self.assertNotIn("\x00", connection["detail"])
        self.assertNotIn("SECRET", str(self.store.get("machines", self.mid)))
        self.assertNotIn(self.secret, str(self.view()))
        self.assertEqual(set(connection), {"state", "provider", "endpoint", "checked_at", "detail", "setup_url"})

    def test_unsafe_setup_links_and_malformed_metadata_are_dropped(self):
        unsafe = ["http://login.tailscale.com/f/funnel?node=a", "https://login.tailscale.com.evil.test/f/funnel?node=a",
                  "https://u:p@login.tailscale.com/f/funnel?node=a", "https://login.tailscale.com/f/funnel?node=a&token=secret",
                  "https://login.tailscale.com/f/funnel?node=", "https://login.tailscale.com/f/funnel?node=a#evil",
                  "https://login.tailscale.com/f/funnel?node=a\nevil", {"private": "secret"}]
        for url in unsafe:
            with self.subTest(url=url):
                self.heartbeat(connection={"state": "needs_setup", "setup_url": url,
                                           "provider": ["funnel"], "checked_at": float("nan"),
                                           "endpoint": "https://u:p@evil.test/path", "detail": {"token": "secret"}})
                self.assertEqual(self.view()["connection"], {"state": "needs_setup"})

    def test_timestamp_bounds_do_not_allow_nonfinite_or_unbounded_values(self):
        for checked in (float("inf"), float("-inf"), 10**500, -1, True, self.clock.t + 301, "today"):
            with self.subTest(checked=checked):
                self.heartbeat(connection={"state": "connected", "checked_at": checked})
                self.assertNotIn("checked_at", self.view()["connection"])

    def test_queued_legacy_reset_migrates_when_runner_upgrades(self):
        self.heartbeat(api_version=3)
        self.call("POST", "/cp/machines/reset-tunnel", {"machine_id": self.mid}, user="alice-token")
        reply = self.heartbeat()[1]
        self.assertFalse(reply["reset_tunnel"])
        self.assertEqual(reply["connection_command"]["action"], "repair")
        self.assertEqual(self.heartbeat()[1]["connection_command"], reply["connection_command"])
