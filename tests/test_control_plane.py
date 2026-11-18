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


class PlaneCase(unittest.TestCase):
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


class MovedJobTests(PlaneCase):
    """Continue here moves a job: when one computer's snapshot says it imported a job from another, the account tells that
    other computer in its heartbeat reply, so it stops working on its copy even with no browser open."""

    def sync(self, machine_id, secret, jobs):
        return self.call("POST", "/cp/machine/sync", {"projects": [], "recent_jobs": jobs}, machine_auth=f"Machine {machine_id}:{secret}")

    def test_the_computer_a_job_left_hears_about_it_in_its_heartbeat(self):
        studio = self.pair(name="Studio Mac")
        air = self.pair(name="MacBook Air")
        self.assertNotIn("moved_jobs", self.beat(*studio)[1])
        self.sync(*air, [{"job_id": "j1", "imported_from_machine": "Studio Mac", "imported_at": 100.0},
                         {"job_id": "j2"},  # made here: not a move
                         {"job_id": "j3", "imported_from_machine": "Studio Mac", "imported_at": 50.0, "moved_to": "Studio Mac"}])
        status, reply = self.beat(*studio)
        self.assertEqual(status, 200)
        self.assertEqual(reply["moved_jobs"], {"j1": {"to": "MacBook Air", "imported_at": 100.0}})
        self.assertNotIn("moved_jobs", self.beat(*air)[1])

    def test_only_the_same_accounts_computers_and_the_newest_move_count(self):
        studio = self.pair(name="Studio Mac")
        air = self.pair(name="MacBook Air")
        other = self.pair(user="bob-token", name="Studio Mac")  # someone else's computer with the same name
        self.sync(*air, [{"job_id": "j1", "imported_from_machine": "Studio Mac", "imported_at": 100.0}])
        self.sync(*air, [{"job_id": "j1", "imported_from_machine": "Studio Mac", "imported_at": 90.0}])  # older: ignored
        self.assertEqual(self.beat(*studio)[1]["moved_jobs"]["j1"]["imported_at"], 100.0)
        self.assertNotIn("moved_jobs", self.beat(*other)[1])


class JobArchiveSyncTests(PlaneCase):
    """Archive and Restore travel to all the owner's computers: a job copied to another computer (a project download,
    Continue here) doesn't come back there after it was archived here."""

    def sync(self, machine_id, secret, archives):
        return self.call("POST", "/cp/machine/sync", {"projects": [], "recent_jobs": [], "job_archives": archives},
                         machine_auth=f"Machine {machine_id}:{secret}")

    def test_every_computer_of_the_account_hears_the_newest_archive_or_restore(self):
        pro, air = self.pair(name="MacBook Pro"), self.pair(name="MacBook Air")
        other = self.pair(user="bob-token", name="MacBook Air")
        self.assertNotIn("job_archives", self.beat(*air)[1])
        self.sync(*pro, {"j1": {"archived": True, "at": 900_000.0}, "bad": {"archived": "yes", "at": 1.0},
                         "future": {"archived": True, "at": 9_999_999.0}})
        self.assertEqual(self.beat(*air)[1]["job_archives"], {"j1": {"archived": True, "at": 900_000.0}})
        self.assertEqual(self.beat(*pro)[1]["job_archives"], {"j1": {"archived": True, "at": 900_000.0}})
        self.assertNotIn("job_archives", self.beat(*other)[1])
        self.sync(*air, {"j1": {"archived": False, "at": 950_000.0}})  # restored on the Air later: that wins
        self.sync(*pro, {"j1": {"archived": True, "at": 900_000.0}})   # the Pro's older archive, sent again: ignored
        self.assertEqual(self.beat(*pro)[1]["job_archives"], {"j1": {"archived": False, "at": 950_000.0}})


class JobOwnerTests(PlaneCase):
    """One owner per job, by ownership number (the fencing token): Continue here raises it; a copy with a lower or equal
    number elsewhere hears it moved, even when its computer was offline during the takeover and syncs late."""

    def sync(self, machine_id, secret, jobs):
        return self.call("POST", "/cp/machine/sync", {"projects": [], "recent_jobs": jobs}, machine_auth=f"Machine {machine_id}:{secret}")

    def test_a_takeover_tells_the_old_owner_even_if_it_was_offline(self):
        pro, air = self.pair(name="MacBook Pro"), self.pair(name="MacBook Air")
        self.sync(*pro, [{"job_id": "j1", "owner_epoch": 1}])
        self.assertNotIn("j1", self.beat(*pro)[1].get("moved_jobs", {}))
        self.sync(*air, [{"job_id": "j1", "owner_epoch": 2}])  # Continue here on the Air while the Pro is away
        self.assertEqual(self.beat(*pro)[1]["moved_jobs"]["j1"], {"to": "MacBook Air", "epoch": 2})
        self.sync(*pro, [{"job_id": "j1", "owner_epoch": 1}])  # the Pro comes back with its old copy: still not the owner
        self.assertEqual(self.store_doc("job_owners")["jobs"]["j1"]["name"], "MacBook Air")
        self.assertNotIn("j1", self.beat(*air)[1].get("moved_jobs", {}))

    def test_a_copy_from_an_old_download_loses_to_the_original(self):
        pro, air = self.pair(name="MacBook Pro"), self.pair(name="MacBook Air")
        self.sync(*air, [{"job_id": "j1", "owner_epoch": 0}])  # the Air syncs first, with its never-continued copy
        self.sync(*pro, [{"job_id": "j1", "owner_epoch": 1}])
        self.assertEqual(self.beat(*air)[1]["moved_jobs"]["j1"], {"to": "MacBook Pro", "epoch": 1})
        self.assertNotIn("j1", self.beat(*pro)[1].get("moved_jobs", {}))

    def test_two_copies_with_the_same_number_keep_the_first_owner(self):
        pro, air = self.pair(name="MacBook Pro"), self.pair(name="MacBook Air")
        self.sync(*pro, [{"job_id": "j1", "owner_epoch": 1}])
        self.sync(*air, [{"job_id": "j1", "owner_epoch": 1}, {"job_id": "gone", "owner_epoch": 1, "moved_to": "MacBook Pro"}])
        self.assertEqual(self.beat(*air)[1]["moved_jobs"], {"j1": {"to": "MacBook Pro", "epoch": 1}})

    def store_doc(self, collection):
        docs = self.plane.store.data.get(collection, {})
        self.assertEqual(len(docs), 1)
        return next(iter(docs.values()))


class JobOwnerApplyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.jobs = self.root / ".orchestrator" / "jobs"
        self.jobs.mkdir(parents=True)

    def job(self, name, **fields):
        (self.jobs / f"{name}.json").write_text(json.dumps({"job_id": name, "status": "review-needed", **fields}))

    def test_only_a_copy_at_or_behind_the_owners_number_is_marked_moved(self):
        self.job("behind")
        self.job("ahead", owner_epoch=3)  # continued back here since: the old notice doesn't apply
        self.job("legacy", imported_from_machine="MacBook Pro")
        marked = ui.apply_moved_jobs({"behind": {"to": "Air", "epoch": 2}, "ahead": {"to": "Air", "epoch": 2},
                                      "legacy": {"to": "MacBook Pro", "epoch": 1}}, roots=[self.root])
        self.assertEqual(sorted(marked), ["behind", "legacy"])

    def test_the_number_rules(self):
        self.assertEqual(account.job_epoch({}), 1)
        self.assertEqual(account.job_epoch({"imported_from_machine": "Pro"}), 0)
        self.assertEqual(account.job_epoch({"imported_from_machine": "Pro", "imported_at": 5.0}), 2)
        self.assertEqual(account.job_epoch({"owner_epoch": 7}), 7)


class JobArchiveApplyTests(unittest.TestCase):
    """What a computer does with the account's archives: its own copy follows the newest, never a running one, never a
    job merged here."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.jobs = self.root / ".orchestrator" / "jobs"
        (self.jobs / "archive").mkdir(parents=True)

    def job(self, name, folder="", **fields):
        path = (self.jobs / folder if folder else self.jobs) / f"{name}.json"
        path.write_text(json.dumps({"job_id": name, "status": "review-needed", **fields}))
        return path

    def test_a_copy_here_is_archived_and_restored_like_the_other_computers(self):
        self.job("j1")
        self.job("busy")
        self.job("restored-later", restored_at=2000.0)
        self.assertEqual(ui.apply_job_archives({"j1": {"archived": True, "at": 1000.0}, "busy": {"archived": True, "at": 1000.0},
                                                "restored-later": {"archived": True, "at": 1000.0}},
                                               roots=[self.root], running={"busy"}), ["j1"])
        archived = json.loads((self.jobs / "archive" / "j1.json").read_text())
        self.assertEqual((archived["status"], archived["restore_status"], archived["archived_at"]), ("archived", "review-needed", 1000.0))
        self.assertTrue((self.jobs / "busy.json").is_file())  # a run going: left for the next heartbeat
        self.assertEqual(ui.apply_job_archives({"j1": {"archived": False, "at": 3000.0}}, roots=[self.root]), ["j1"])
        restored = json.loads((self.jobs / "j1.json").read_text())
        self.assertEqual((restored["status"], restored["restored_at"]), ("review-needed", 3000.0))
        self.assertNotIn("archived_at", restored)

    def test_a_job_merged_here_never_comes_back(self):
        self.job("merged", "archive", status="completed", completed_at="2026-11-01T10:00:00")
        self.assertEqual(ui.apply_job_archives({"merged": {"archived": False, "at": 5000.0}}, roots=[self.root]), [])
        self.assertFalse((self.jobs / "merged.json").exists())

    def test_the_snapshot_lists_recent_archives_and_restores(self):
        now = 10_000_000.0
        self.job("put-away", "archive", status="archived", archived_at=now - 60)
        self.job("old", "archive", status="archived", archived_at=now - account.ARCHIVE_WINDOW - 1)
        self.job("legacy", "archive", status="archived", completed_at="1970-04-26T17:46:00")  # no archived_at: completed_at
        self.job("back", restored_at=now - 30)
        self.job("plain")
        events = account.job_archive_events([self.root], now=now)
        self.assertEqual(events["put-away"], {"archived": True, "at": now - 60})
        self.assertEqual(events["back"], {"archived": False, "at": now - 30})
        self.assertNotIn("old", events)
        self.assertNotIn("plain", events)
        self.assertIn("job_archives", account.build_snapshot(root=self.root))


class RateLimitTests(PlaneCase):
    def start(self, ip):
        return self.plane.handle("POST", "/cp/pair/start", {"fastly-client-ip": ip}, {}, verify_user)[0]

    def test_one_address_gets_a_limited_number_of_pairing_codes_per_hour(self):
        limit = cp.PAIR_START_LIMIT[0]
        self.assertEqual([self.start("203.0.113.5") for _ in range(limit)], [200] * limit)
        self.assertEqual(self.start("203.0.113.5"), 429)
        self.assertEqual(self.start("203.0.113.6"), 200)  # someone else is unaffected
        self.clock.t += cp.PAIR_START_LIMIT[1]
        self.assertEqual(self.start("203.0.113.5"), 200)  # the next window starts fresh

    def test_the_forwarded_address_is_used_when_the_hosting_header_is_missing(self):
        for _ in range(cp.PAIR_START_LIMIT[0]):
            self.plane.handle("POST", "/cp/pair/start", {"x-forwarded-for": "198.51.100.9, 10.0.0.1"}, {}, verify_user)
        self.assertEqual(self.plane.handle("POST", "/cp/pair/start", {"x-forwarded-for": "198.51.100.9"}, {}, verify_user)[0], 429)


class ControlPlaneTests(PlaneCase):
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

    def test_heartbeat_records_the_api_version(self):
        machine_id, secret = self.pair()
        _, listed = self.call("GET", "/cp/machines", user="alice-token")
        self.assertEqual(listed["machines"][0]["api_version"], 0)  # never reported one
        self.call("POST", "/cp/machine/heartbeat", {"endpoint": "", "api_version": 2}, machine_auth=f"Machine {machine_id}:{secret}")
        _, listed = self.call("GET", "/cp/machines", user="alice-token")
        self.assertEqual(listed["machines"][0]["api_version"], 2)
        self.call("POST", "/cp/machine/heartbeat", {"endpoint": "", "api_version": "junk"}, machine_auth=f"Machine {machine_id}:{secret}")
        _, listed = self.call("GET", "/cp/machines", user="alice-token")
        self.assertEqual(listed["machines"][0]["api_version"], 0)

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


class EnrollmentTests(PlaneCase):
    def setUp(self):
        super().setUp()
        self.pusher = FakePusher()
        self.plane = cp.ControlPlane(self.store, now=self.clock, pusher=self.pusher)

    def create(self, user="alice-token"):
        return self.call("POST", "/cp/enroll/create", {}, user=user)

    def redeem(self, token, name="Closet mini"):
        return self.call("POST", "/cp/enroll/redeem", {"token": token, "name": name, "os": "macOS 26", "version": "0.1.0"})

    def test_create_needs_a_signed_in_owner(self):
        self.assertEqual(self.call("POST", "/cp/enroll/create", {})[0], 401)
        status, created = self.create()
        self.assertEqual(status, 200)
        self.assertTrue(created["token"].startswith("enroll_"))
        self.assertGreaterEqual(len(created["token"]), len("enroll_") + 22)
        self.assertEqual(created["expires_in"], cp.ENROLLMENT_TTL)

    def test_only_the_hash_is_stored(self):
        _, created = self.create()
        stored = json.dumps(self.store.data.get("enrollments", {}))
        self.assertNotIn(created["token"], stored)
        self.assertNotIn(created["token"][len("enroll_"):], stored)

    def test_redeem_creates_a_machine_like_pairing_and_works_once(self):
        _, created = self.create()
        status, redeemed = self.redeem(created["token"])
        self.assertEqual(status, 200)
        self.assertEqual(redeemed["owner_email"], "alice@example.com")
        self.assertRegex(redeemed["machine_id"], r"^m_[0-9a-f]{16}$")
        status, _ = self.beat(redeemed["machine_id"], redeemed["machine_secret"])
        self.assertEqual(status, 200)
        _, listed = self.call("GET", "/cp/machines", user="alice-token")
        self.assertEqual([m["name"] for m in listed["machines"]], ["Closet mini"])
        self.assertTrue(listed["machines"][0]["added_with_command"])
        self.assertEqual(self.redeem(created["token"])[0], 404)

    def test_expired_and_unknown_tokens_are_refused(self):
        _, created = self.create()
        self.clock.t += cp.ENROLLMENT_TTL
        self.assertEqual(self.redeem(created["token"])[0], 404)
        self.assertEqual(self.redeem("enroll_" + "x" * 32)[0], 404)
        self.assertEqual(self.redeem("")[0], 404)
        self.assertEqual(self.call("GET", "/cp/machines", user="alice-token")[1]["machines"], [])

    def test_account_limit_applies(self):
        for index in range(cp.MAX_MACHINES_PER_USER):
            self.pair(name=f"Mac {index}")
        _, created = self.create()
        self.assertEqual(self.redeem(created["token"])[0], 409)

    def test_owner_is_told_when_a_mac_is_added(self):
        self.call("POST", "/cp/push/register", {"token": "a" * 40, "label": "phone"}, user="alice-token")
        _, created = self.create()
        self.redeem(created["token"])
        self.assertEqual(len(self.pusher.sent), 1)
        self.assertIn("Closet mini", self.pusher.sent[0][1]["title"] + self.pusher.sent[0][1]["body"])

    def test_added_with_command_mark_fades_after_a_day(self):
        _, created = self.create()
        self.redeem(created["token"])
        self.clock.t += 86400
        self.assertFalse(self.call("GET", "/cp/machines", user="alice-token")[1]["machines"][0]["added_with_command"])
        self.assertFalse(self.pair(name="Desk Mac") is None)
        names = {m["name"]: m["added_with_command"] for m in self.call("GET", "/cp/machines", user="alice-token")[1]["machines"]}
        self.assertFalse(names["Desk Mac"])


class RemoteUpdateTests(PlaneCase):
    def test_owner_requests_an_update_and_the_next_heartbeat_carries_it_once(self):
        machine_id, secret = self.pair()
        self.call("POST", "/cp/machine/heartbeat", {"endpoint": "https://a.example.com", "packaged": True},
                  machine_auth=f"Machine {machine_id}:{secret}")
        self.assertEqual(self.call("POST", "/cp/machines/update", {"machine_id": machine_id}, user="bob-token")[0], 404)
        self.assertEqual(self.call("POST", "/cp/machines/update", {"machine_id": machine_id})[0], 401)
        status, _ = self.call("POST", "/cp/machines/update", {"machine_id": machine_id}, user="alice-token")
        self.assertEqual(status, 200)
        view = self.call("GET", "/cp/machines", user="alice-token")[1]["machines"][0]
        self.assertEqual(view["update_state"], "requested")
        _, first = self.beat(machine_id, secret)
        self.assertTrue(first["update_requested"])
        _, second = self.beat(machine_id, secret)
        self.assertFalse(second["update_requested"])

    def test_owner_requests_tunnel_reset_and_next_heartbeat_carries_it_once(self):
        machine_id, secret = self.pair()
        self.beat(machine_id, secret)
        self.assertEqual(self.call("POST", "/cp/machines/reset-tunnel", {"machine_id": machine_id}, user="bob-token")[0], 404)
        self.assertEqual(self.call("POST", "/cp/machines/reset-tunnel", {"machine_id": machine_id})[0], 401)
        status, _ = self.call("POST", "/cp/machines/reset-tunnel", {"machine_id": machine_id}, user="alice-token")
        self.assertEqual(status, 200)
        _, first = self.beat(machine_id, secret)
        self.assertTrue(first["reset_tunnel"])
        _, second = self.beat(machine_id, secret)
        self.assertFalse(second["reset_tunnel"])

    def test_only_app_installs_can_be_asked_to_update(self):
        machine_id, secret = self.pair()
        self.beat(machine_id, secret)
        view = self.call("GET", "/cp/machines", user="alice-token")[1]["machines"][0]
        self.assertFalse(view["updatable"])
        self.assertEqual(self.call("POST", "/cp/machines/update", {"machine_id": machine_id}, user="alice-token")[0], 409)

    def test_reported_update_progress_is_kept_to_known_states(self):
        machine_id, secret = self.pair()
        auth = f"Machine {machine_id}:{secret}"
        for state, shown in (("waiting_for_work", "waiting_for_work"), ("<script>", ""), ("current", "current")):
            self.call("POST", "/cp/machine/heartbeat", {"endpoint": "", "packaged": True, "update_state": state}, machine_auth=auth)
            view = self.call("GET", "/cp/machines", user="alice-token")[1]["machines"][0]
            self.assertEqual(view["update_state"], shown)


class ReadinessTests(PlaneCase):
    def test_reported_readiness_is_kept_to_known_checks_and_states(self):
        machine_id, secret = self.pair()
        self.call("POST", "/cp/machine/heartbeat", {"endpoint": "", "readiness": {
            "auto_login": "off", "sleep": "<script>", "secret_thing": "ok", "power_restart": "ok"}},
            machine_auth=f"Machine {machine_id}:{secret}")
        view = self.call("GET", "/cp/machines", user="alice-token")[1]["machines"][0]
        self.assertEqual(view["readiness"], {"auto_login": "off", "power_restart": "ok"})

    def test_control_plane_and_mac_agree_on_readiness_names(self):
        from orchestrator import mac_readiness
        self.assertEqual(cp.READINESS_CHECKS, mac_readiness.CHECKS)
        self.assertEqual(cp.READINESS_STATES, mac_readiness.STATES)


class FakePusher:
    def __init__(self):
        self.sent: list[tuple[list[str], dict]] = []
        self.gone: set[str] = set()

    def send(self, tokens, message):
        self.sent.append((list(tokens), dict(message)))
        return [t for t in tokens if t in self.gone]


class NotificationTests(PlaneCase):
    def setUp(self):
        super().setUp()
        self.pusher = FakePusher()
        self.plane = cp.ControlPlane(self.store, now=self.clock, pusher=self.pusher)

    def register(self, token, user="alice-token"):
        return self.call("POST", "/cp/push/register", {"token": token, "label": "iPhone · Safari"}, user=user)

    def events(self, machine_id, secret, events):
        return self.call("POST", "/cp/machine/events", {"events": events}, machine_auth=f"Machine {machine_id}:{secret}")

    def test_events_reach_the_owners_devices_only(self):
        machine_id, secret = self.pair()
        self.register("a" * 40)
        self.register("b" * 40)
        self.register("c" * 40, user="bob-token")
        status, data = self.events(machine_id, secret, [
            {"key": "inbox-1", "title": "Plan ready · Demo", "body": "Lobby: approve the plan", "path": "#/jobs/20260922-bug-1"}])
        self.assertEqual((status, data["sent"]), (200, 1))
        tokens, message = self.pusher.sent[0]
        self.assertEqual(sorted(tokens), ["a" * 40, "b" * 40])
        self.assertEqual(message["tag"], "inbox-1")
        self.assertEqual(message["link"], f"{cp.HOSTED_APP_URL}/?machine={machine_id}#/jobs/20260922-bug-1")

    def test_event_links_only_go_to_app_routes(self):
        machine_id, secret = self.pair()
        self.register("a" * 40)
        self.events(machine_id, secret, [{"key": "k", "title": "t", "body": "b", "path": "javascript:alert(1)"},
                                         {"key": "k2", "title": "t", "body": "b", "path": "https://evil.example.com"}])
        for _, message in self.pusher.sent:
            self.assertEqual(message["link"], f"{cp.HOSTED_APP_URL}/?machine={machine_id}#/")

    def test_events_need_the_machine_secret_and_are_capped(self):
        machine_id, secret = self.pair()
        self.register("a" * 40)
        self.assertEqual(self.events(machine_id, "wrong", [{"title": "x"}])[0], 401)
        _, data = self.events(machine_id, secret, [{"key": str(i), "title": "x"} for i in range(50)])
        self.assertEqual(data["sent"], cp.MAX_EVENTS_PER_CALL)

    def test_no_devices_means_nothing_is_sent(self):
        machine_id, secret = self.pair()
        _, data = self.events(machine_id, secret, [{"key": "k", "title": "x"}])
        self.assertEqual(data["sent"], 0)
        self.assertEqual(self.pusher.sent, [])
        self.assertEqual(self.call("POST", "/cp/push/test", user="alice-token")[0], 409)

    def test_dead_tokens_are_dropped(self):
        machine_id, secret = self.pair()
        self.register("a" * 40)
        self.register("b" * 40)
        self.pusher.gone = {"a" * 40}
        self.events(machine_id, secret, [{"key": "1", "title": "x"}])
        self.events(machine_id, secret, [{"key": "2", "title": "x"}])
        self.assertEqual(self.pusher.sent[-1][0], ["b" * 40])

    def test_register_unregister_and_test(self):
        self.assertEqual(self.register("short")[0], 400)
        self.assertEqual(self.register("a" * 40)[0], 200)
        self.assertEqual(self.register("a" * 40)[0], 200)  # the same browser again: still one device
        self.assertEqual(len(self.store.where("push_tokens", "uid", "u-alice")), 1)
        _, data = self.call("POST", "/cp/push/test", user="alice-token")
        self.assertEqual(data["sent"], 1)
        self.call("POST", "/cp/push/unregister", {"token": "a" * 40}, user="bob-token")  # not theirs: ignored
        self.assertEqual(len(self.store.where("push_tokens", "uid", "u-alice")), 1)
        self.call("POST", "/cp/push/unregister", {"token": "a" * 40}, user="alice-token")
        self.assertEqual(self.store.where("push_tokens", "uid", "u-alice"), [])
        self.assertEqual(self.call("POST", "/cp/push/register", {"token": "a" * 40})[0], 401)

    def test_device_cap(self):
        for i in range(cp.MAX_DEVICES_PER_USER):
            self.assertEqual(self.register(f"{i:040d}")[0], 200)
        self.assertEqual(self.register("z" * 40)[0], 409)

    def test_sweep_alerts_once_when_a_busy_computer_disappears(self):
        machine_id, secret = self.pair()
        self.register("a" * 40)
        self.call("POST", "/cp/machine/heartbeat", {"endpoint": "", "running": 2}, machine_auth=f"Machine {machine_id}:{secret}")
        self.assertEqual(self.plane.sweep(), 0)  # still reporting
        self.clock.t += cp.ONLINE_WINDOW + 1
        self.assertEqual(self.plane.sweep(), 1)
        _, message = self.pusher.sent[-1]
        self.assertEqual(message["title"], "Studio Mac went offline")
        self.assertIn("2 runs were in progress", message["body"])
        self.assertEqual(message["tag"], f"offline:{machine_id}")
        self.assertEqual(self.plane.sweep(), 0)  # once per disappearance
        self.call("POST", "/cp/machine/heartbeat", {"endpoint": "", "running": 1}, machine_auth=f"Machine {machine_id}:{secret}")
        self.clock.t += cp.ONLINE_WINDOW + 1
        self.assertEqual(self.plane.sweep(), 1)  # back, busy, gone again
        self.assertIn("1 run was in progress", self.pusher.sent[-1][1]["body"])

    def test_sweep_ignores_idle_computers(self):
        machine_id, secret = self.pair()
        self.register("a" * 40)
        self.call("POST", "/cp/machine/heartbeat", {"endpoint": "", "running": 0}, machine_auth=f"Machine {machine_id}:{secret}")
        self.clock.t += cp.ONLINE_WINDOW + 1
        self.assertEqual(self.plane.sweep(), 0)
        self.assertEqual(self.pusher.sent, [])


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

    def test_events_and_running_count_reach_the_control_plane(self):
        machine, _ = self.connect_as()
        pusher = FakePusher()
        self.plane.pusher = pusher
        self.plane.handle("POST", "/cp/push/register", {"authorization": "Bearer alice-token"}, {"token": "t" * 40}, verify_user)
        account.heartbeat(machine, "", running=3)
        self.assertEqual(self.plane.store.get("machines", machine["machine_id"])["running"], 3)
        account.send_events(machine, [{"key": "k1", "title": "Plan ready", "body": "Lobby", "path": "#/jobs/j1"}], "Demo")
        self.assertEqual(pusher.sent[0][1]["title"], "Plan ready · Demo")
        self.assertTrue(pusher.sent[0][1]["link"].endswith(f"?machine={machine['machine_id']}#/jobs/j1"))

    def test_the_ui_server_forwards_what_it_notices(self):
        machine, _ = self.connect_as()
        server = ui.UIServer(("127.0.0.1", 0), self.root, token="owner-token")
        self.addCleanup(server.server_close)
        tracker = ui.notifier.Tracker()
        with patch.object(ui.account, "send_events") as send:
            server.notify_once(tracker)  # seeds: nothing sent
            send.assert_not_called()
            event = {"kind": "needs-you", "key": "x", "title": "Needs you", "body": "b", "path": "#/"}
            with patch.object(tracker, "update", return_value=[event]):
                server.notify_once(tracker)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[1], [event])

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

    def test_snapshot_sync_and_retrieval(self):
        machine, _ = self.connect_as()
        jobs_dir = self.root / ".orchestrator" / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        job_data = {
            "job_id": "job-101",
            "title": "Build Offline View",
            "type": "feature-plan",
            "status": "completed",
            "plan": {
                "summary": "State sync architecture",
                "tasks": [{"title": "Task 1", "status": "done"}]
            }
        }
        (jobs_dir / "job-101.json").write_text(json.dumps(job_data))

        # Build snapshot locally
        snapshot = account.build_snapshot(root=self.root)
        self.assertEqual(len(snapshot["projects"]), 1)
        self.assertEqual(len(snapshot["recent_jobs"]), 1)
        self.assertEqual(snapshot["recent_jobs"][0]["job_id"], "job-101")

        # Sync snapshot to control plane
        res = account.sync_snapshot(machine, snapshot)
        self.assertTrue(res["ok"])

        # Check that machine view shows has_snapshot
        status, listed = self.plane.handle("GET", "/cp/machines", {"authorization": "Bearer alice-token"}, {}, verify_user)
        self.assertEqual(status, 200)
        target_machine = next(m for m in listed["machines"] if m["id"] == machine["machine_id"])
        self.assertTrue(target_machine["has_snapshot"])
        self.assertIsNotNone(target_machine["snapshot_synced_at"])

        # Fetch snapshot as owner
        status, got = self.plane.handle("POST", "/cp/machine/snapshot", {"authorization": "Bearer alice-token"},
                                        {"machine_id": machine["machine_id"]}, verify_user)
        self.assertEqual(status, 200)
        self.assertIsNotNone(got["snapshot"])
        self.assertEqual(got["snapshot"]["machine_id"], machine["machine_id"])
        self.assertEqual(got["snapshot"]["recent_jobs"][0]["title"], "Build Offline View")

        # Non-owner cannot fetch snapshot
        status, _ = self.plane.handle("POST", "/cp/machine/snapshot", {"authorization": "Bearer bob-token"},
                                      {"machine_id": machine["machine_id"]}, verify_user)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()



class PrimaryComputerTests(PlaneCase):
    """One primary computer per project: the first to open it, until another takes over while it isn't running anything."""

    def primary(self, machine, secret, claim=False, project="github.com/alice/shop"):
        return self.call("POST", "/cp/machine/primary/claim" if claim else "/cp/machine/primary", {"project": project, "label": "Shop"},
                         machine_auth=f"Machine {machine}:{secret}")

    def test_the_first_computer_to_open_a_project_becomes_its_primary(self):
        studio, s1 = self.pair(name="Studio")
        laptop, s2 = self.pair(name="Laptop")
        status, first = self.primary(studio, s1)
        self.assertEqual((status, first["you"], first["primary"]["name"]), (200, True, "Studio"))
        _, second = self.primary(laptop, s2)
        self.assertFalse(second["you"])
        self.assertEqual(second["primary"]["name"], "Studio")
        _, other = self.primary(laptop, s2, project="github.com/alice/blog")  # a different project has its own primary
        self.assertTrue(other["you"])

    def test_taking_over_waits_while_the_primary_is_running_jobs(self):
        studio, s1 = self.pair(name="Studio")
        laptop, s2 = self.pair(name="Laptop")
        self.primary(studio, s1)
        self.call("POST", "/cp/machine/heartbeat", {"endpoint": "https://studio.example.com", "running": 2}, machine_auth=f"Machine {studio}:{s1}")
        status, refused = self.primary(laptop, s2, claim=True)
        self.assertEqual(status, 409)
        self.assertIn("Studio is still running 2 jobs", refused["error"])
        self.call("POST", "/cp/machine/heartbeat", {"endpoint": "https://studio.example.com", "running": 0}, machine_auth=f"Machine {studio}:{s1}")
        status, taken = self.primary(laptop, s2, claim=True)
        self.assertEqual((status, taken["you"]), (200, True))
        self.assertFalse(self.primary(studio, s1)[1]["you"])

    def test_an_offline_or_removed_primary_can_be_replaced(self):
        studio, s1 = self.pair(name="Studio")
        laptop, s2 = self.pair(name="Laptop")
        self.primary(studio, s1)
        self.call("POST", "/cp/machine/heartbeat", {"endpoint": "https://studio.example.com", "running": 1}, machine_auth=f"Machine {studio}:{s1}")
        self.clock.t += cp.ONLINE_WINDOW + 1  # it went quiet with a job showing: it isn't working on anything now
        self.assertEqual(self.primary(laptop, s2, claim=True)[0], 200)
        self.call("POST", "/cp/machines/remove", {"machine_id": laptop}, user="alice-token")
        self.assertTrue(self.primary(studio, s1)[1]["you"])  # its primary was removed: the next to ask takes it

    def test_accounts_dont_share_primaries(self):
        alice, s1 = self.pair(user="alice-token", name="Alice Mac")
        bob, s2 = self.pair(user="bob-token", name="Bob Mac")
        self.assertTrue(self.primary(alice, s1)[1]["you"])
        self.assertTrue(self.primary(bob, s2)[1]["you"])

    def test_the_project_must_be_named(self):
        studio, s1 = self.pair()
        self.assertEqual(self.primary(studio, s1, project="")[0], 400)


class CentralWebTelemetryTests(PlaneCase):
    def setUp(self):
        super().setUp()
        self.events = []
        self.plane.telemetry = lambda event, distinct_id, props: self.events.append((event, distinct_id, props))

    def test_anonymous_and_signed_in_web_telemetry(self):
        status, data = self.call("POST", "/cp/telemetry", {
            "event": "page_viewed",
            "anon_id": "anon_12345678",
            "properties": {"page": "home", "token": "should-strip", "distinct_id": "spoof"},
        })
        self.assertEqual((status, data), (200, {"ok": True}))
        event, distinct_id, props = self.events[-1]
        self.assertEqual(event, "page_viewed")
        self.assertEqual(distinct_id, "anon:anon_12345678")
        self.assertEqual(props["page"], "home")
        self.assertFalse(props["signed_in"])
        self.assertNotIn("token", props)
        self.assertNotIn("distinct_id", props)

        status, data = self.call("POST", "/cp/telemetry", {
            "event": "web_action_triggered",
            "anon_id": "anon_12345678",
            "properties": {"action": "new_job", "platform": "macOS", "languages": "Swift, Python", "projects_count": 3, "jobs_count": 12},
        }, user="alice-token")
        self.assertEqual((status, data), (200, {"ok": True}))
        first_use = next(e for e in self.events if e[0] == "first_use")
        self.assertEqual(first_use[1], "u-alice")
        event, distinct_id, props = self.events[-1]
        self.assertEqual(event, "web_action_triggered")
        self.assertEqual(distinct_id, "u-alice")
        self.assertTrue(props["signed_in"])
        self.assertTrue(props["is_first_use"])
        self.assertEqual(props["anon_id"], "anon_12345678")
        self.assertEqual((props["platform"], props["languages"], props["projects_count"], props["jobs_count"]),
                         ("macOS", "Swift, Python", 3, 12))

        self.call("POST", "/cp/telemetry", {"event": "page_viewed", "properties": {"page": "tests"}}, user="alice-token")
        self.assertFalse(self.events[-1][2]["is_first_use"])

    def test_invalid_telemetry_is_rejected(self):
        self.assertEqual(self.call("POST", "/cp/telemetry", {"event": "Bad Event!", "anon_id": "anon_12345678"})[0], 400)
        self.assertEqual(self.call("POST", "/cp/telemetry", {"event": "page_viewed"})[0], 400)

    def test_pairing_emits_control_plane_event_and_web_ui_wires_telemetry(self):
        self.pair(user="alice-token", name="Alice Mac")
        self.assertTrue(any(e[0] == "cp_machine_paired" and e[1] == "u-alice" for e in self.events))

        app_js = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        for snippet in (
            "function telemetryContext(",
            "function trackWebEvent(",
            "function trackFirstUseAndSession(",
            'fetch("/cp/telemetry"',
            'trackWebEvent("web_session_started"',
            'trackWebEvent("first_use"',
            'trackWebEvent("page_viewed"',
            'trackWebEvent("feature_used"',
            'trackWebEvent("job_run_started"',
            'trackWebEvent("project_used"',
            'trackWebEvent("web_action_triggered"',
            'trackWebEvent("web_session_duration"',
            'trackWebEvent("job_run_error"',
            'function reportRunError(',
            'function trackKeyAction(',
            '"free_llm_mode"',
            '"parallel_jobs"',
            '"anti_cruft_guards"',
            '"prd_and_agile"',
            '"testing_and_delivery"',
            '"multi_machine"',
            '"vibe_depth"',
            '"local_machine"',
            'window.addEventListener("pagehide"',
            "ctx.primary_language",
            "ctx.projects_count",
            "ctx.jobs_count",
            "ctx.prd_status",
            "ctx.has_prd",
        ):
            self.assertIn(snippet, app_js)


class SetupCopyTests(PlaneCase):
    """A computer shares its setup for a project; the account's other computers list it and copy it, nobody else."""

    PROJECT = "github.com/alice/shop"

    def auth(self, machine, secret):
        return f"Machine {machine}:{secret}"

    def share(self, machine, secret, done=3, total=4, profile=None, project=PROJECT):
        progress = {"required_done": done, "required_total": total, "complete": done == total,
                    "items": [{"id": "project", "title": "Project configured", "required": True, "done": True}]}
        return self.call("POST", "/cp/machine/setup", {"project": project, "progress": progress,
                                                       "profile": {"project": {"build_command": "make"}} if profile is None else profile},
                         machine_auth=self.auth(machine, secret))

    def test_a_newer_computer_lists_and_copies_what_another_shared(self):
        studio, s1 = self.pair(name="Studio")
        laptop, s2 = self.pair(name="Laptop")
        self.assertEqual(self.share(studio, s1)[0], 200)
        status, listed = self.call("POST", "/cp/machine/setups", {"project": self.PROJECT}, machine_auth=self.auth(laptop, s2))
        self.assertEqual(status, 200)
        self.assertEqual([(s["name"], s["progress"]["required_done"], s["has_config"]) for s in listed["setups"]], [("Studio", 3, True)])
        self.assertNotIn("profile", listed["setups"][0])  # the list never carries the settings themselves
        status, got = self.call("POST", "/cp/machine/setup/get", {"project": self.PROJECT, "machine_id": studio}, machine_auth=self.auth(laptop, s2))
        self.assertEqual((status, got["profile"]["project"]["build_command"], got["name"]), (200, "make", "Studio"))

    def test_a_computer_is_not_offered_its_own_setup(self):
        studio, s1 = self.pair(name="Studio")
        self.share(studio, s1)
        _, listed = self.call("POST", "/cp/machine/setups", {"project": self.PROJECT}, machine_auth=self.auth(studio, s1))
        self.assertEqual(listed["setups"], [])
        self.assertEqual(self.call("POST", "/cp/machine/setup/get", {"project": self.PROJECT, "machine_id": studio},
                                   machine_auth=self.auth(studio, s1))[0], 404)

    def test_setups_are_per_project_and_per_account(self):
        alice, s1 = self.pair(user="alice-token", name="Alice Mac")
        alice2, s2 = self.pair(user="alice-token", name="Alice Laptop")
        bob, s3 = self.pair(user="bob-token", name="Bob Mac")
        self.share(alice, s1)
        _, other_project = self.call("POST", "/cp/machine/setups", {"project": "github.com/alice/blog"}, machine_auth=self.auth(alice2, s2))
        self.assertEqual(other_project["setups"], [])
        _, bobs = self.call("POST", "/cp/machine/setups", {"project": self.PROJECT}, machine_auth=self.auth(bob, s3))
        self.assertEqual(bobs["setups"], [])
        self.assertEqual(self.call("POST", "/cp/machine/setup/get", {"project": self.PROJECT, "machine_id": alice},
                                   machine_auth=self.auth(bob, s3))[0], 404)

    def test_progress_is_cleaned_and_oversize_profiles_refused(self):
        studio, s1 = self.pair()
        self.call("POST", "/cp/machine/setup", {"project": self.PROJECT, "progress": {"required_done": "x", "required_total": 500, "items": ["no", {"id": "a", "done": "yes"}]},
                                                "profile": {"project": {"build_command": "make"}}}, machine_auth=self.auth(studio, s1))
        record = next(iter(self.store.data["machine_setups"].values()))
        self.assertEqual((record["progress"]["required_done"], record["progress"]["required_total"]), (0, 99))
        self.assertEqual(record["progress"]["items"], [{"id": "a", "title": "", "required": False, "done": False}])
        big = {"prompts": {"reviewer.md": "x" * (cp.SETUP_PROFILE_LIMIT + 1)}}
        self.assertEqual(self.share(studio, s1, profile=big)[0], 413)

    def test_removing_a_computer_removes_what_it_shared(self):
        studio, s1 = self.pair(name="Studio")
        laptop, s2 = self.pair(name="Laptop")
        self.share(studio, s1)
        self.call("POST", "/cp/machines/remove", {"machine_id": studio}, user="alice-token")
        _, listed = self.call("POST", "/cp/machine/setups", {"project": self.PROJECT}, machine_auth=self.auth(laptop, s2))
        self.assertEqual(listed["setups"], [])

    def test_master_setup_sync_and_get(self):
        studio, s1 = self.pair(name="Studio")
        laptop, s2 = self.pair(name="Laptop")
        st, empty = self.call("POST", "/cp/machine/setup/master", {"project": self.PROJECT}, machine_auth=self.auth(laptop, s2))
        self.assertEqual(st, 200)
        self.assertIsNone(empty["master"])

        st, first = self.share(studio, s1, profile={"project": {"build_command": "make test"}})
        self.assertEqual(st, 200)
        self.assertEqual(first["master"]["machine_name"], "Studio")

        # Passive share from Laptop does not overwrite Studio's master
        st, passive = self.share(laptop, s2, profile={"project": {"build_command": "npm test"}})
        self.assertEqual(st, 200)
        self.assertEqual(passive["master"]["machine_name"], "Studio")

        # Explicit push_master from Laptop promotes Laptop to master
        st, pushed = self.call("POST", "/cp/machine/setup", {
            "project": self.PROJECT,
            "progress": {"required_done": 5, "required_total": 5, "complete": True, "items": []},
            "profile": {"project": {"build_command": "npm test"}},
            "push_master": True,
        }, machine_auth=self.auth(laptop, s2))
        self.assertEqual(st, 200)
        self.assertEqual(pushed["master"]["machine_name"], "Laptop")

        st, fetched = self.call("POST", "/cp/machine/setup/master", {"project": self.PROJECT}, machine_auth=self.auth(studio, s1))
        self.assertEqual(st, 200)
        self.assertEqual(fetched["master"]["machine_name"], "Laptop")
        self.assertEqual(fetched["master"]["profile"]["project"]["build_command"], "npm test")
