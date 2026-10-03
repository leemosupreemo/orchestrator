"""The control plane: which computers belong to which account, and the one-time tickets that let a signed-in
person into one of them. It never sees code, model subscriptions or integration tokens: those stay on the computer.

Pure logic over a small Store interface so it can be tested without Firebase; main.py wires it to Firestore and
Firebase Auth. Routes (all JSON, under /cp on the hosted app):

  POST /cp/pair/start      computer       {name, os, version}        -> {code, machine_id, poll_secret, expires_in}
  POST /cp/pair/poll       computer       {code, poll_secret}        -> {status: waiting} | {status: claimed, machine_secret, owner_email}
  POST /cp/pair/preview    signed-in user {code}                     -> {name, os, created}
  POST /cp/pair/claim      signed-in user {code}                     -> {machine}
  POST /cp/machine/heartbeat  computer (Authorization: Machine <id>:<secret>) {endpoint, version, name} -> {ok, owner_email}
  POST /cp/machine/leave      computer (same)                        -> {ok}
  GET  /cp/machines        signed-in user                            -> {machines: [...]}
  POST /cp/machines/remove signed-in user {machine_id}               -> {ok}
  POST /cp/machine/ticket  signed-in user {machine_id}               -> {ticket, endpoint}
  POST /cp/machine/events  computer (same) {events: [{key, title, body, path}]} -> {sent}
  POST /cp/push/register   signed-in user {token, label}             -> {ok}
  POST /cp/push/unregister signed-in user {token}                    -> {ok}
  POST /cp/push/test       signed-in user                            -> {sent}
  POST /cp/enroll/create   signed-in user                            -> {token, expires_in}
  POST /cp/enroll/redeem   computer       {token, name, os, version} -> {machine_id, machine_secret, owner_email}

Enrollment is pairing for a computer nobody is sitting at: the owner makes a one-time token in the hosted app and runs
`orchestrator enroll` with it over SSH. Only the token's hash is stored, it works once, and the owner is told.

Notifications: a computer forwards what its UI server noticed (something needs you, a run ended) and the control plane
pushes it to every browser and phone the owner turned alerts on in. The scheduled sweep() adds what only the control
plane can see: a computer that stopped reporting while work was running on it. Nothing is stored but the push tokens.

A ticket is `<payload>.<signature>`: payload is base64url JSON {v, mid, uid, email, exp, nonce}, signature is
base64url HMAC-SHA256 of the payload text keyed with the machine's secret. The computer checks it (orchestrator/account.py),
so a ticket for one computer is useless on another, and it expires in two minutes.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Callable, Protocol

PAIRING_TTL = 600         # seconds a pairing code stays valid
ENROLLMENT_TTL = 900      # seconds an enrollment token stays valid
ADDED_MARK_SECONDS = 86400  # how long a computer added with a token is marked so in the list
TICKET_TTL = 120          # seconds a sign-in ticket stays valid
ONLINE_WINDOW = 180       # a computer is online if it reported within this many seconds
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
CODE_LENGTH = 8
MAX_MACHINES_PER_USER = 20
MAX_DEVICES_PER_USER = 20
MAX_EVENTS_PER_CALL = 10
HOSTED_APP_URL = "https://swift-orch-web-20260923.web.app"


class ControlError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Store(Protocol):
    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...
    def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None: ...
    def update(self, collection: str, doc_id: str, fields: dict[str, Any]) -> None: ...
    def delete(self, collection: str, doc_id: str) -> None: ...
    def where(self, collection: str, field: str, value: Any) -> list[tuple[str, dict[str, Any]]]: ...


class Pusher(Protocol):
    def send(self, tokens: list[str], message: dict[str, str]) -> list[str]:
        """Deliver to each token; return the tokens that are gone for good (uninstalled, revoked) so they can be dropped."""
        ...


class MemoryStore:
    """A Store in a dict, for tests and the local emulator-free dev server."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict[str, Any]]] = {}

    def get(self, collection, doc_id):
        doc = self.data.get(collection, {}).get(doc_id)
        return dict(doc) if doc is not None else None

    def set(self, collection, doc_id, data):
        self.data.setdefault(collection, {})[doc_id] = dict(data)

    def update(self, collection, doc_id, fields):
        self.data.setdefault(collection, {}).setdefault(doc_id, {}).update(fields)

    def delete(self, collection, doc_id):
        self.data.get(collection, {}).pop(doc_id, None)

    def where(self, collection, field, value):
        return [(k, dict(v)) for k, v in self.data.get(collection, {}).items() if v.get(field) == value]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def sign_ticket(secret: str, claims: dict[str, Any]) -> str:
    payload = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = _b64(hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def normalize_code(code: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


def _clean(value: Any, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()[:limit]


def _endpoint(value: Any) -> str:
    """Where browsers reach the computer: an https:// origin (the hosted app is https, so http would be blocked), or nothing yet."""
    url = _clean(value, 300).rstrip("/")
    if not url:
        return ""
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d{1,5})?", url):
        raise ControlError("The address must be an https:// origin with no path.")
    return url


class ControlPlane:
    def __init__(self, store: Store, now: Callable[[], float] = time.time, pusher: Pusher | None = None):
        self.store = store
        self.now = now
        self.pusher = pusher

    # -- pairing, from the computer

    def start_pairing(self, body: dict[str, Any]) -> dict[str, Any]:
        poll_secret = secrets.token_urlsafe(24)
        for _ in range(10):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if self.store.get("pairings", code) is None:
                break
        else:  # pragma: no cover - 31^8 codes
            raise ControlError("Couldn't make a pairing code. Try again.", 503)
        now = self.now()
        machine_id = "m_" + secrets.token_hex(8)
        self.store.set("pairings", code, {
            "machine_id": machine_id, "name": _clean(body.get("name"), 80) or "My computer",
            "os": _clean(body.get("os"), 40), "version": _clean(body.get("version"), 40),
            "created": now, "expires": now + PAIRING_TTL, "poll_hash": _hash(poll_secret), "status": "waiting",
        })
        return {"code": code, "machine_id": machine_id, "poll_secret": poll_secret, "expires_in": PAIRING_TTL}

    def poll_pairing(self, body: dict[str, Any]) -> dict[str, Any]:
        code = normalize_code(body.get("code"))
        pairing = self.store.get("pairings", code) if code else None
        if pairing is None or not hmac.compare_digest(_hash(str(body.get("poll_secret") or "")), pairing["poll_hash"]):
            raise ControlError("That pairing isn't known.", 404)
        if pairing["status"] == "claimed":
            self.store.delete("pairings", code)  # the secret is handed over exactly once
            return {"status": "claimed", "machine_id": pairing["machine_id"], "machine_secret": pairing["machine_secret"],
                    "owner_email": pairing["owner_email"]}
        if pairing["expires"] <= self.now():
            self.store.delete("pairings", code)
            raise ControlError("The pairing code expired. Run `orchestrator connect` again.", 410)
        return {"status": "waiting"}

    # -- pairing, from the person

    def _waiting_pairing(self, code: Any) -> tuple[str, dict[str, Any]]:
        code = normalize_code(code)
        pairing = self.store.get("pairings", code) if len(code) == CODE_LENGTH else None
        if pairing is None or pairing["status"] != "waiting" or pairing["expires"] <= self.now():
            raise ControlError("That code isn't valid or has expired. Run `orchestrator connect` again for a new one.", 404)
        return code, pairing

    def preview_pairing(self, user: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        _require_email(user)
        _, pairing = self._waiting_pairing(body.get("code"))
        return {"name": pairing["name"], "os": pairing["os"], "version": pairing["version"], "created": pairing["created"]}

    def claim_pairing(self, user: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        email = _require_email(user)
        code, pairing = self._waiting_pairing(body.get("code"))
        machine_id, machine, secret = self._create_machine(user["uid"], email, pairing, pairing["machine_id"])
        self.store.update("pairings", code, {"status": "claimed", "owner_email": email, "machine_secret": secret})
        return {"machine": self._view(machine_id, machine)}

    def _create_machine(self, uid: str, email: str, details: dict[str, Any], machine_id: str,
                        enrolled: bool = False) -> tuple[str, dict[str, Any], str]:
        if len(self.store.where("machines", "owner_uid", uid)) >= MAX_MACHINES_PER_USER:
            raise ControlError(f"An account can have up to {MAX_MACHINES_PER_USER} computers. Remove one first.", 409)
        secret, now = secrets.token_urlsafe(32), self.now()
        machine = {"owner_uid": uid, "owner_email": email, "name": details["name"], "os": details["os"],
                   "version": details["version"], "endpoint": "", "created": now, "last_seen": 0}
        if enrolled:
            machine["enrolled"] = now
        self.store.set("machines", machine_id, machine)
        self.store.set("machine_secrets", machine_id, {"secret": secret, "created": now})
        return machine_id, machine, secret

    # -- enrollment: pairing for a computer nobody is sitting at

    def create_enrollment(self, user: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        email = _require_email(user)
        token = "enroll_" + secrets.token_urlsafe(24)
        now = self.now()
        self.store.set("enrollments", _hash(token), {"owner_uid": user["uid"], "owner_email": email,
                                                     "created": now, "expires": now + ENROLLMENT_TTL})
        return {"token": token, "expires_in": ENROLLMENT_TTL}

    def redeem_enrollment(self, body: dict[str, Any]) -> dict[str, Any]:
        token = str(body.get("token") or "")
        doc_id = _hash(token)
        enrollment = self.store.get("enrollments", doc_id) if token.startswith("enroll_") else None
        if enrollment is None or enrollment["expires"] <= self.now():
            raise ControlError("That enrollment command isn't valid or has expired. Make a new one with Add a Mac.", 404)
        details = {"name": _clean(body.get("name"), 80) or "My Mac", "os": _clean(body.get("os"), 40),
                   "version": _clean(body.get("version"), 40)}
        machine_id, machine, secret = self._create_machine(enrollment["owner_uid"], enrollment["owner_email"], details,
                                                           "m_" + secrets.token_hex(8), enrolled=True)
        self.store.delete("enrollments", doc_id)  # works once
        self._push(enrollment["owner_uid"], [_message(
            f"enrolled:{machine_id}", f"{machine['name']} was added to your account",
            "It was added with an Add a Mac command. If that wasn't you, remove it from your computers.",
            _link(machine_id, "#/"))])
        return {"machine_id": machine_id, "machine_secret": secret, "owner_email": enrollment["owner_email"]}

    # -- the computer, once paired

    def _machine_from_header(self, header: str) -> tuple[str, dict[str, Any]]:
        match = re.fullmatch(r"Machine (m_[0-9a-f]{16}):(\S+)", header or "")
        if not match:
            raise ControlError("This computer isn't signed in to the control plane.", 401)
        machine_id, secret = match.groups()
        stored = self.store.get("machine_secrets", machine_id)
        machine = self.store.get("machines", machine_id)
        if stored is None or machine is None:
            raise ControlError("This computer was removed from its account.", 410)
        if not hmac.compare_digest(secret, stored["secret"]):
            raise ControlError("This computer isn't signed in to the control plane.", 401)
        return machine_id, machine

    def heartbeat(self, auth_header: str, body: dict[str, Any]) -> dict[str, Any]:
        machine_id, machine = self._machine_from_header(auth_header)
        try:
            running = max(0, min(int(body.get("running") or 0), 999))
        except (TypeError, ValueError):
            running = 0
        try:
            api_version = max(0, min(int(body.get("api_version") or 0), 9999))
        except (TypeError, ValueError):
            api_version = 0
        fields: dict[str, Any] = {"last_seen": self.now(), "endpoint": _endpoint(body.get("endpoint")),
                                  "running": running, "busy": running > 0, "offline_notified": False,
                                  "api_version": api_version}
        for key, limit in (("version", 40), ("name", 80), ("os", 40)):
            if body.get(key):
                fields[key] = _clean(body[key], limit)
        self.store.update("machines", machine_id, fields)
        return {"ok": True, "owner_email": machine["owner_email"]}

    def leave(self, auth_header: str) -> dict[str, Any]:
        machine_id, _ = self._machine_from_header(auth_header)
        self._remove(machine_id)
        return {"ok": True}

    # -- the person, once paired

    def _view(self, machine_id: str, machine: dict[str, Any]) -> dict[str, Any]:
        online = self.now() - (machine.get("last_seen") or 0) < ONLINE_WINDOW
        return {"id": machine_id, "name": machine["name"], "os": machine.get("os", ""), "version": machine.get("version", ""),
                "api_version": machine.get("api_version", 0),
                "endpoint": machine.get("endpoint", ""), "last_seen": machine.get("last_seen") or 0, "online": online,
                "reachable": online and bool(machine.get("endpoint")),
                "added_with_command": self.now() - (machine.get("enrolled") or -ADDED_MARK_SECONDS) < ADDED_MARK_SECONDS}

    def _owned(self, user: dict[str, Any], machine_id: Any) -> tuple[str, dict[str, Any]]:
        machine_id = str(machine_id or "")
        machine = self.store.get("machines", machine_id) if machine_id else None
        if machine is None or machine["owner_uid"] != user["uid"]:
            raise ControlError("That computer isn't on your account.", 404)
        return machine_id, machine

    def list_machines(self, user: dict[str, Any]) -> dict[str, Any]:
        machines = [self._view(mid, m) for mid, m in self.store.where("machines", "owner_uid", user["uid"])]
        machines.sort(key=lambda m: (not m["reachable"], -m["last_seen"], m["name"]))
        return {"machines": machines}

    def remove_machine(self, user: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        machine_id, _ = self._owned(user, body.get("machine_id"))
        self._remove(machine_id)
        return {"ok": True}

    def _remove(self, machine_id: str) -> None:
        self.store.delete("machine_secrets", machine_id)
        self.store.delete("machines", machine_id)

    def issue_ticket(self, user: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        email = _require_email(user)
        machine_id, machine = self._owned(user, body.get("machine_id"))
        view = self._view(machine_id, machine)
        if not view["online"]:
            raise ControlError(f"{machine['name']} is offline. Start Orchestrator on it (`orchestrator ui`).", 409)
        if not view["endpoint"]:
            raise ControlError(f"{machine['name']} is online but can't be reached from the web yet.", 409)
        stored = self.store.get("machine_secrets", machine_id)
        if stored is None:
            raise ControlError("That computer isn't on your account.", 404)
        claims = {"v": 1, "mid": machine_id, "uid": user["uid"], "email": email,
                  "exp": int(self.now()) + TICKET_TTL, "nonce": secrets.token_urlsafe(12)}
        return {"ticket": sign_ticket(stored["secret"], claims), "endpoint": view["endpoint"]}

    # -- notifications

    def _devices(self, uid: str) -> list[tuple[str, dict[str, Any]]]:
        return self.store.where("push_tokens", "uid", uid)

    def _push(self, uid: str, messages: list[dict[str, str]]) -> int:
        devices = self._devices(uid)
        if not devices or not messages or self.pusher is None:
            return 0
        tokens = [d["token"] for _, d in devices]
        gone: set[str] = set()
        for message in messages:
            gone.update(self.pusher.send([t for t in tokens if t not in gone], message))
        for doc_id, device in devices:
            if device["token"] in gone:
                self.store.delete("push_tokens", doc_id)
        return len(messages)

    def register_device(self, user: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        token = _clean(body.get("token"), 4096)
        if len(token) < 20:
            raise ControlError("That browser didn't give a notification token.")
        doc_id = _hash(token)
        if self.store.get("push_tokens", doc_id) is None and len(self._devices(user["uid"])) >= MAX_DEVICES_PER_USER:
            raise ControlError(f"Alerts are on in {MAX_DEVICES_PER_USER} browsers already. Turn them off in one first.", 409)
        self.store.set("push_tokens", doc_id, {"uid": user["uid"], "token": token, "label": _clean(body.get("label"), 80),
                                               "created": self.now()})
        return {"ok": True}

    def unregister_device(self, user: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        doc_id = _hash(_clean(body.get("token"), 4096))
        device = self.store.get("push_tokens", doc_id)
        if device is not None and device["uid"] == user["uid"]:
            self.store.delete("push_tokens", doc_id)
        return {"ok": True}

    def test_push(self, user: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        if not self._devices(user["uid"]):
            raise ControlError("Alerts aren't on in any browser yet.", 409)
        return {"sent": self._push(user["uid"], [_message("test", "Alerts are on", "You'll hear from Orchestrator here.", "")])}

    def machine_events(self, auth_header: str, body: dict[str, Any]) -> dict[str, Any]:
        machine_id, machine = self._machine_from_header(auth_header)
        raw = body.get("events")
        if not isinstance(raw, list):
            raise ControlError("events must be a list")
        messages = [_message(_clean(e.get("key"), 120), _clean(e.get("title"), 120) or "Orchestrator", _clean(e.get("body"), 400),
                             _link(machine_id, e.get("path")))
                    for e in raw[:MAX_EVENTS_PER_CALL] if isinstance(e, dict)]
        return {"sent": self._push(machine["owner_uid"], messages)}

    def sweep(self) -> int:
        """Alert owners whose computer stopped reporting while work was running on it. Once per disappearance."""
        cutoff = self.now() - ONLINE_WINDOW
        alerted = 0
        for machine_id, machine in self.store.where("machines", "busy", True):
            if machine.get("offline_notified") or (machine.get("last_seen") or 0) >= cutoff:
                continue
            running = int(machine.get("running") or 0)
            work = f"{running} run{'s' if running != 1 else ''}" if running else "work"
            self._push(machine["owner_uid"], [_message(
                f"offline:{machine_id}", f"{machine['name']} went offline",
                f"It stopped responding while {work} {'were' if running > 1 else 'was'} in progress. "
                "It may be asleep or have lost its connection; work continues when it's back.",
                _link(machine_id, "#/"))])
            self.store.update("machines", machine_id, {"offline_notified": True})
            alerted += 1
        return alerted

    # -- routing

    def handle(self, method: str, path: str, headers: dict[str, str], body: dict[str, Any],
               verify_user: Callable[[str], dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        """Route one request. `verify_user` turns a Firebase ID token into {uid, email, email_verified} or raises."""
        route = "/" + "/".join(p for p in path.split("/") if p and p != "cp")
        try:
            computer = {
                ("POST", "/pair/start"): lambda: self.start_pairing(body),
                ("POST", "/pair/poll"): lambda: self.poll_pairing(body),
                ("POST", "/machine/heartbeat"): lambda: self.heartbeat(headers.get("authorization", ""), body),
                ("POST", "/machine/leave"): lambda: self.leave(headers.get("authorization", "")),
                ("POST", "/machine/events"): lambda: self.machine_events(headers.get("authorization", ""), body),
                ("POST", "/enroll/redeem"): lambda: self.redeem_enrollment(body),
            }.get((method, route))
            if computer:
                return 200, computer()
            person = {
                ("POST", "/pair/preview"): self.preview_pairing,
                ("POST", "/pair/claim"): self.claim_pairing,
                ("GET", "/machines"): lambda user, _body: self.list_machines(user),
                ("POST", "/machines/remove"): self.remove_machine,
                ("POST", "/machine/ticket"): self.issue_ticket,
                ("POST", "/push/register"): self.register_device,
                ("POST", "/push/unregister"): self.unregister_device,
                ("POST", "/push/test"): self.test_push,
                ("POST", "/enroll/create"): self.create_enrollment,
            }.get((method, route))
            if person is None:
                raise ControlError("Not found", 404)
            auth = headers.get("authorization", "")
            if not auth.startswith("Bearer "):
                raise ControlError("Sign in first.", 401)
            try:
                user = verify_user(auth[7:])
            except ControlError:
                raise
            except Exception:
                raise ControlError("Your sign-in has expired. Sign in again.", 401)
            return 200, person(user, body)
        except ControlError as exc:
            return exc.status, {"error": str(exc)}


def _message(key: str, title: str, body: str, link: str) -> dict[str, str]:
    """A push, as data only: the app's service worker shows it, so it looks the same in every browser. The tag matches
    the alert an open tab shows for the same event, so the two replace each other instead of doubling up."""
    return {"tag": key or "orchestrator", "title": title, "body": body, "link": link or HOSTED_APP_URL + "/"}


def _link(machine_id: str, path: Any) -> str:
    """Opens the hosted app on that computer, at the page the event is about (a hash route like #/jobs/<id>)."""
    route = str(path or "")
    if not re.fullmatch(r"#/[A-Za-z0-9_./?=&%-]*", route):
        route = "#/"
    return f"{HOSTED_APP_URL}/?machine={machine_id}{route}"


def _require_email(user: dict[str, Any]) -> str:
    email = str(user.get("email") or "").strip().lower()
    if not email:
        raise ControlError("This account didn't share an email address.", 403)
    if not user.get("email_verified"):
        raise ControlError(f"The email {email} isn't verified with that provider.", 403)
    return email
