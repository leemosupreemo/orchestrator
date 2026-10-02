from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import analytics as A  # noqa: E402


class KpiTests(unittest.TestCase):
    def feature(self):
        return {"id": "lobby", "name": "Lobby"}

    def test_add_validates_and_dedupes_ids(self):
        f = self.feature()
        a = A.add_kpi(f, {"name": "Seat claim rate", "event": "lobby_seat_claimed", "target": "60", "unit": "%"})
        b = A.add_kpi(f, {"name": "Seat claim rate", "event": "lobby_seat_claimed_v2"})
        self.assertEqual((a["id"], b["id"], a["target"], b["target"], b["direction"]), ("seat-claim-rate", "seat-claim-rate-2", 60.0, None, "up"))

    def test_rejects_bad_input(self):
        f = self.feature()
        for bad in ({"name": "", "event": "ok_event"}, {"name": "x", "event": "Bad Event"}, {"name": "x", "event": "e"},
                    {"name": "x", "event": "ok_event", "target": "lots"}, {"name": "x", "event": "ok_event", "direction": "sideways"}):
            with self.assertRaises(A.AnalyticsError, msg=str(bad)):
                A.add_kpi(f, bad)
        self.assertNotIn("kpis", {k for k, v in f.items() if v})

    def test_update_keeps_id_and_measurements(self):
        f = self.feature()
        k = A.add_kpi(f, {"name": "Rate", "event": "rate_event", "target": 5})
        A.log_measurement(k, 3)
        updated = A.clean_kpi({"target": 9}, k)
        self.assertEqual((updated["id"], updated["target"], len(updated["measurements"]), updated["event"]), (k["id"], 9.0, 1, "rate_event"))


class MeasurementTests(unittest.TestCase):
    def kpi(self, direction="up", target=10):
        return {"id": "k", "name": "K", "event": "k_event", "direction": direction, "target": target, "measurements": []}

    def test_log_validates_and_caps(self):
        k = self.kpi()
        with self.assertRaises(A.AnalyticsError):
            A.log_measurement(k, "abc")
        with self.assertRaises(A.AnalyticsError):
            A.log_measurement(k, 1, decision="ship it")
        for i in range(A.MAX_MEASUREMENTS + 5):
            A.log_measurement(k, i)
        self.assertEqual(len(k["measurements"]), A.MAX_MEASUREMENTS)
        self.assertEqual(k["measurements"][-1]["value"], A.MAX_MEASUREMENTS + 4)

    def test_status_up_and_down(self):
        up = self.kpi("up", 10)
        self.assertEqual(A.status(up)["state"], "no-data")
        A.log_measurement(up, 8)
        self.assertEqual(A.status(up)["state"], "behind")
        A.log_measurement(up, 12)
        s = A.status(up)
        self.assertEqual((s["state"], s["trend"]), ("on-track", "better"))
        down = self.kpi("down", 2)
        A.log_measurement(down, 3)
        A.log_measurement(down, 1)
        s = A.status(down)
        self.assertEqual((s["state"], s["trend"]), ("on-track", "better"))
        A.log_measurement(down, 5)
        self.assertEqual((A.status(down)["state"], A.status(down)["trend"]), ("behind", "worse"))

    def test_no_target_and_flat(self):
        k = self.kpi(target=None)
        A.log_measurement(k, 4)
        A.log_measurement(k, 4)
        s = A.status(k)
        self.assertEqual((s["state"], s["trend"]), ("no-target", "flat"))


class PlanTests(unittest.TestCase):
    FEATURES = [{"id": "lobby", "name": "Lobby", "kpis": [{"name": "Seat claims", "event": "seat_claimed", "direction": "up", "target": 60.0, "unit": "%"},
                                                           {"name": "Errors", "event": "seat_error", "direction": "down", "target": None, "unit": ""}]},
                {"id": "chat", "name": "Chat"}]

    def test_tracking_plan_lists_events_by_feature(self):
        text = A.tracking_plan(self.FEATURES)
        self.assertIn("## Lobby", text)
        self.assertIn("| Seat claims | `seat_claimed` | at least 60 % |", text)
        self.assertIn("| Errors | `seat_error` | — |", text)
        self.assertNotIn("Chat", text)
        self.assertIn("No KPIs defined yet.", A.tracking_plan([{"id": "x", "name": "X"}]))

    def test_planner_context_names_the_events_and_provider(self):
        text = A.instrumentation_context(self.FEATURES[0], "Mixpanel")
        self.assertIn("`seat_claimed`", text)
        self.assertIn("Mixpanel integration", text)
        self.assertIn("at most", A.instrumentation_context({"kpis": [{"name": "E", "event": "e_ev", "direction": "down", "target": 2.0, "unit": ""}]}, None))
        self.assertEqual(A.instrumentation_context(self.FEATURES[1], "Mixpanel"), "")


class Resp:
    def __init__(self, body="", status=200):
        self.status, self._body = status, body

    def read(self):
        return self._body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ProviderTests(unittest.TestCase):
    def run_test(self, provider, response, host=None):
        seen = []

        def opener(req, timeout):
            seen.append((req.full_url, json.loads(req.data)))
            if isinstance(response, Exception):
                raise response
            return response

        A.send_test(provider, "KEY123", host or A.PROVIDERS[provider]["hosts"]["us"], opener, now=1700000000)
        return seen

    def test_each_provider_sends_its_documented_test_event(self):
        url, body = self.run_test("mixpanel", Resp('{"status":1,"error":null}'))[0]
        self.assertEqual(url, "https://api.mixpanel.com/track?verbose=1")
        self.assertEqual((body[0]["properties"]["token"], body[0]["event"]), ("KEY123", A.TEST_EVENT))
        url, body = self.run_test("amplitude", Resp('{"code":200}'))[0]
        self.assertEqual((url, body["api_key"], body["events"][0]["event_type"]), ("https://api2.amplitude.com/2/httpapi", "KEY123", A.TEST_EVENT))
        url, body = self.run_test("posthog", Resp('{"status":1}'), "https://eu.i.posthog.com/")[0]
        self.assertEqual((url, body["api_key"], body["event"]), ("https://eu.i.posthog.com/capture/", "KEY123", A.TEST_EVENT))

    def test_mixpanel_rejects_a_bad_token_even_on_http_200(self):
        with self.assertRaisesRegex(A.AnalyticsError, "token"):
            self.run_test("mixpanel", Resp('{"status":0,"error":"token, missing or empty"}'))

    def test_http_and_network_failures_are_explained(self):
        with self.assertRaisesRegex(A.AnalyticsError, "rejected the key \\(401\\)"):
            self.run_test("amplitude", urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"")))
        with self.assertRaisesRegex(A.AnalyticsError, "reach"):
            self.run_test("posthog", urllib.error.URLError("dns"))

    def test_input_checks(self):
        with self.assertRaises(A.AnalyticsError):
            A.send_test("nope", "k", "https://x", lambda *a, **k: None)
        with self.assertRaises(A.AnalyticsError):
            A.send_test("mixpanel", "", "https://api.mixpanel.com", lambda *a, **k: None)
        with self.assertRaises(A.AnalyticsError):
            A.send_test("mixpanel", "k", "http://insecure", lambda *a, **k: None)

    def test_public_provider_list_has_no_secrets_or_hosts_with_credentials(self):
        ids = [p["id"] for p in A.public_providers()]
        self.assertEqual(ids, ["mixpanel", "amplitude", "posthog"])


if __name__ == "__main__":
    unittest.main()
