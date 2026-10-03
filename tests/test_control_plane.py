from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "cloud" / "functions"))
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import control_plane as cp  # noqa: E402
from orchestrator import account  # noqa: E402
from orchestrator.web import server as ui  # noqa: E402

ALICE = {"uid": "u-alice", "email": "Alice@Example.com", "email_verified": True}
BOB = {"uid": "u-bob", "email": "bob@example.com", "email_verified": True}
USERS = {"alice-token": ALICE, "bob-token": BOB}


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def verify_user(token: str) -> dict:
    if token not in USERS:
        raise ValueError("bad token")
    return USERS[token]


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = cp.MemoryStore()
        self.plane = cp.ControlPlane(self.store, now=self.clock)

    def call(self, method, path, body=None, user=None, machine_auth=None):
        headers = {}
        if user:
            headers["authorization"] = f"Bearer {user}"
        if machine_auth:
            headers["authorization"] = machine_auth
        return self.plane.handle(method, path, headers, body or {}, verify_user)

    def pair(self, user="alice-token", name="Studio Mac"):
        status, started = self.call("POST", "/cp/pair/start", {"name": name, "os": "macOS 26", "version": "0.1.0"})
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/cp/pair/claim", {"code": started["code"]}, user=user)
        self.assertEqual(status, 200)
        status, polled = self.call("POST", "/cp/pair/poll", {"code": started["code"], "poll_secret": started["poll_secret"]})
        self.assertEqual(polled["status"], "claimed")
        return polled["machine_id"], polled["machine_secret"]

    def beat(self, machine_id, secret, endpoint="https://studio.run.example.com"):
        return self.call("POST", "/cp/machine/heartbeat", {"endpoint": endpoint}, machine_auth=f"Machine {machine_id}:{secret}")

    def test_pairing_hands_over_the_secret_once(self):
        status, started = self.call("POST", "/cp/pair/start", {"name": "Studio Mac"})
        self.assertEqual(len(started["code"]), 8)
        self.assertTrue(set(started["code"]) <= set(cp.CODE_ALPHABET))
        poll = {"code": started["code"], "poll_secret": started["poll_secret"]}
        self.assertEqual(self.call("POST", "/cp/pair/poll", poll), (200, {"status": "waiting"}))
        status, preview = self.call("POST", "/cp/pair/preview", {"code": started["code"].lower()[:4] + "-" + started["code"][4:]}, user="alice-token")
        self.assertEqual((status, preview["name"]), (200, "Studio Mac"))
        status, claimed = self.call("POST", "/cp/pair/claim", {"code": started["code"]}, user="alice-token")
        self.assertEqual(claimed["machine"]["name"], "Studio Mac")
        status, polled = self.call("POST", "/cp/pair/poll", poll)
        self.assertEqual((polled["status"], polled["owner_email"]), ("claimed", "alice@example.com"))
        self.assertEqual(self.call("POST", "/cp/pair/poll", poll)[0], 404)  # gone after hand-over

    def test_polling_needs_the_poll_secret(self):
        _, started = self.call("POST", "/cp/pair/start", {})
        self.assertEqual(self.call("POST", "/cp/pair/poll", {"code": started["code"], "poll_secret": "guess"})[0], 404)

    def test_codes_expire_and_cannot_be_claimed_twice(self):
        _, started = self.call("POST", "/cp/pair/start", {})
        self.clock.t += cp.PAIRING_TTL + 1
        self.assertEqual(self.call("POST", "/cp/pair/claim", {"code": started["code"]}, user="alice-token")[0], 404)
        self.assertEqual(self.call("POST", "/cp/pair/poll", {"code": started["code"], "poll_secret": started["poll_secret"]})[0], 410)
        _, started = self.call("POST", "/cp/pair/start", {})
        self.assertEqual(self.call("POST", "/cp/pair/claim", {"code": started["code"]}, user="alice-token")[0], 200)
        self.assertEqual(self.call("POST", "/cp/pair/claim", {"code": started["code"]}, user="bob-token")[0], 404)

    def test_person_routes_need_a_signed_in_verified_user(self):
        _, started = self.call("POST", "/cp/pair/start", {})
        self.assertEqual(self.call("POST", "/cp/pair/claim", {"code": started["code"]})[0], 401)
        self.assertEqual(self.call("POST", "/cp/pair/claim", {"code": started["code"]}, user="forged")[0], 401)
        USERS["unverified"] = {"uid": "u3", "email": "x@example.com", "email_verified": False}
        self.addCleanup(USERS.pop, "unverified")
        self.assertEqual(self.call("POST", "/cp/pair/claim", {"code": started["code"]}, user="unverified")[0], 403)

    def test_machines_are_private_to_their_owner(self):
        machine_id, _ = self.pair()
        _, mine = self.call("GET", "/cp/machines", user="alice-token")
        self.assertEqual([m["id"] for m in mine["machines"]], [machine_id])
        _, theirs = self.call("GET", "/cp/machines", user="bob-token")
        self.assertEqual(theirs["machines"], [])
        self.assertEqual(self.call("POST", "/cp/machine/ticket", {"machine_id": machine_id}, user="bob-token")[0], 404)
        self.assertEqual(self.call("POST", "/cp/machines/remove", {"machine_id": machine_id}, user="bob-token")[0], 404)

    def test_heartbeat_marks_online_and_records_the_address(self):
        machine_id, secret = self.pair()
        _, listed = self.call("GET", "/cp/machines", user="alice-token")
        self.assertFalse(listed["machines"][0]["online"])
        status, beat = self.beat(machine_id, secret)
        self.assertEqual((status, beat["owner_email"]), (200, "alice@example.com"))
        _, listed = self.call("GET", "/cp/machines", user="alice-token")
        self.assertTrue(listed["machines"][0]["reachable"])
        self.assertEqual(listed["machines"][0]["endpoint"], "https://studio.run.example.com")
        self.clock.t += cp.ONLINE_WINDOW + 1
        _, listed = self.call("GET", "/cp/machines", user="alice-token")
        self.assertFalse(listed["machines"][0]["online"])

    def test_heartbeat_refuses_bad_secrets_and_plain_http(self):
        machine_id, secret = self.pair()
        self.assertEqual(self.beat(machine_id, "wrong")[0], 401)
        self.assertEqual(self.beat(machine_id, secret, endpoint="http://100.64.0.1:8765")[0], 400)
        self.assertEqual(self.beat(machine_id, secret, endpoint="https://a.example.com/path")[0], 400)
        self.assertEqual(self.beat(machine_id, secret, endpoint="")[0], 200)

    def test_removed_machines_are_told_so(self):
        machine_id, secret = self.pair()
        self.assertEqual(self.call("POST", "/cp/machines/remove", {"machine_id": machine_id}, user="alice-token")[0], 200)
        self.assertEqual(self.beat(machine_id, secret)[0], 410)

    def test_a_machine_can_leave(self):
        machine_id, secret = self.pair()
        self.assertEqual(self.call("POST", "/cp/machine/leave", machine_auth=f"Machine {machine_id}:{secret}")[0], 200)
        _, listed = self.call("GET", "/cp/machines", user="alice-token")
        self.assertEqual(listed["machines"], [])

    def test_tickets_need_the_machine_online_and_reachable(self):
        machine_id, secret = self.pair()
        status, data = self.call("POST", "/cp/machine/ticket", {"machine_id": machine_id}, user="alice-token")
        self.assertEqual(status, 409)
        self.assertIn("offline", data["error"])
        self.beat(machine_id, secret, endpoint="")
        status, data = self.call("POST", "/cp/machine/ticket", {"machine_id": machine_id}, user="alice-token")
        self.assertEqual(status, 409)
        self.assertIn("can't be reached", data["error"])

    def test_tickets_verify_only_on_their_machine_and_expire(self):
        machine_id, secret = self.pair()
        other_id, other_secret = self.pair(name="Laptop")
        self.beat(machine_id, secret)
        _, issued = self.call("POST", "/cp/machine/ticket", {"machine_id": machine_id}, user="alice-token")
        self.assertEqual(issued["endpoint"], "https://studio.run.example.com")
        here = {"machine_id": machine_id, "machine_secret": secret}
        claims = account.verify_ticket(issued["ticket"], here, now=self.clock.t)
        self.assertEqual((claims["email"], claims["uid"]), ("alice@example.com", "u-alice"))
        with self.assertRaises(account.AccountError):  # another computer's secret
            account.verify_ticket(issued["ticket"], {"machine_id": other_id, "machine_secret": other_secret}, now=self.clock.t)
        with self.assertRaises(account.AccountError):  # right secret, wrong id
            account.verify_ticket(issued["ticket"], {"machine_id": other_id, "machine_secret": secret}, now=self.clock.t)
        with self.assertRaises(account.AccountError):
            account.verify_ticket(issued["ticket"], here, now=self.clock.t + cp.TICKET_TTL + 1)
        payload, sig = issued["ticket"].split(".")
        with self.assertRaises(account.AccountError):  # tampered
            account.verify_ticket(payload[:-2] + "AA." + sig, here, now=self.clock.t)

    def test_unknown_routes(self):
        self.assertEqual(self.call("GET", "/cp/nope")[0], 404)


class _PlaneHandler(BaseHTTPRequestHandler):
    plane: cp.ControlPlane

    def log_message(self, *args):
        return

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        headers = {k.lower(): v for k, v in self.headers.items()}
        status, payload = self.plane.handle(method, self.path, headers, body, verify_user)
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self._handle("POST")

    def do_GET(self):
        self._handle("GET")


class EndToEndTests(unittest.TestCase):
    """The runner's side (orchestrator/account.py and the UI server) against the real control plane over HTTP."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.plane = cp.ControlPlane(cp.MemoryStore())
        handler = type("H", (_PlaneHandler,), {"plane": self.plane})
        self.cp_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.cp_server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.env = patch.dict(os.environ, {
            "ORCHESTRATOR_USER_STATE_DIR": str(base / "state"),
            account.CONTROL_URL_ENV: f"http://127.0.0.1:{self.cp_server.server_address[1]}/cp",
        })
        self.env.start()
        self.root = base / "project"
        (self.root / ".orchestrator").mkdir(parents=True)
        (self.root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "Demo"}))

    def tearDown(self):
        self.cp_server.shutdown()
        self.cp_server.server_close()
        self.env.stop()
        self.tmp.cleanup()

    def connect_as(self, user_token="alice-token"):
        printed: list[str] = []

        def sleep(_):  # the person confirms the code while the computer waits
            code = next(line.split(": ")[1].replace("-", "") for line in printed if "Your code" in line)
            if self.plane.store.get("pairings", code)["status"] == "waiting":
                self.plane.handle("POST", "/cp/pair/claim", {"authorization": f"Bearer {user_token}"}, {"code": code}, verify_user)

        return account.connect("Studio Mac", out=printed.append, sleep=sleep), printed

    def test_connect_heartbeat_and_ticket_sign_in(self):
        machine, printed = self.connect_as()
        self.assertEqual(machine["owner_email"], "alice@example.com")
        self.assertEqual(account.load_machine()["machine_id"], machine["machine_id"])
        self.assertEqual(oct(account.machine_path().stat().st_mode & 0o777), "0o600")
        self.assertTrue(any("/#/connect?code=" in line for line in printed))

        account.heartbeat(machine, "https://studio.run.example.com")
        _, issued = self.plane.handle("POST", "/cp/machine/ticket", {"authorization": "Bearer alice-token"},
                                      {"machine_id": machine["machine_id"]}, verify_user)

        server = ui.UIServer(("127.0.0.1", 0), self.root, token="owner-token")
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(ui.allowed_auth_sources(self.root).get("alice@example.com"), "account")

        def auth(body):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("POST", "/api/auth", body=json.dumps(body),
                         headers={"Content-Type": "application/json", "X-Orchestrator-UI": "1"})
            res = conn.getresponse()
            try:
                return res.status, json.loads(res.read())
            finally:
                conn.close()

        status, data = auth({"ticket": issued["ticket"]})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["email"], "alice@example.com")
        self.assertTrue(data["token"].startswith("si_"))
        status, data = auth({"ticket": issued["ticket"]})  # replayed
        self.assertEqual(status, 401)
        self.assertIn("already used", data["error"])

    def test_heartbeat_forgets_a_removed_machine(self):
        machine, _ = self.connect_as()
        self.plane.handle("POST", "/cp/machines/remove", {"authorization": "Bearer alice-token"},
                          {"machine_id": machine["machine_id"]}, verify_user)
        with self.assertRaises(account.AccountError) as caught:
            account.heartbeat(machine, "")
        self.assertEqual(caught.exception.status, 410)
        self.assertIsNone(account.load_machine())

    def test_disconnect_removes_it_from_the_account(self):
        self.connect_as()
        self.assertTrue(account.disconnect())
        self.assertIsNone(account.load_machine())
        _, listed = self.plane.handle("GET", "/cp/machines", {"authorization": "Bearer alice-token"}, {}, verify_user)
        self.assertEqual(listed["machines"], [])
        self.assertFalse(account.disconnect())

    def test_ticket_sign_in_needs_a_paired_computer(self):
        server = ui.UIServer(("127.0.0.1", 0), self.root, token="owner-token")
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("POST", "/api/auth", body=json.dumps({"ticket": "x.y"}),
                     headers={"Content-Type": "application/json", "X-Orchestrator-UI": "1"})
        res = conn.getresponse()
        data = json.loads(res.read())
        conn.close()
        self.assertEqual(res.status, 409)
        self.assertIn("orchestrator connect", data["error"])


if __name__ == "__main__":
    unittest.main()
