"""This computer's link to an Orchestrator account: pairing (`orchestrator connect`), the heartbeat that tells the
hosted app where to reach it, and checking the one-time tickets the hosted app signs people in with.

The control plane (cloud/functions/control_plane.py) only learns the computer's name, OS, version and web address.
Code, model subscriptions and integration tokens never leave the computer.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import socket
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from orchestrator.project_config import user_state_dir

FIREBASE_PROJECT_ID = "swift-orch-web-20260923"
HOSTED_APP_URL = f"https://{FIREBASE_PROJECT_ID}.web.app"
HOSTED_ORIGINS = (HOSTED_APP_URL, f"https://{FIREBASE_PROJECT_ID}.firebaseapp.com")
CONTROL_URL_ENV = "ORCHESTRATOR_CONTROL_URL"
HEARTBEAT_SECONDS = 60
# What this computer's server can do for the hosted app. Bumped when the hosted page starts relying on something older
# computers lack; the page compares it with the one it needs (REQUIRED_RUNNER_API in account.js) and says to update.
API_VERSION = 2
INSTALL_SPEC = "git+https://github.com/leemosupreemo/orchestrator.git"  # what the install command on the hosted app installs


class AccountError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def control_url() -> str:
    return (os.environ.get(CONTROL_URL_ENV) or f"{HOSTED_APP_URL}/cp").rstrip("/")


def machine_path() -> Path:
    return user_state_dir() / "machine.json"


def load_machine() -> dict[str, Any] | None:
    try:
        data = json.loads(machine_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("machine_id") and data.get("machine_secret"):
        return data
    return None


def save_machine(data: dict[str, Any]) -> None:
    path = machine_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def forget_machine() -> None:
    try:
        machine_path().unlink()
    except FileNotFoundError:
        pass


def default_name() -> str:
    name = socket.gethostname().split(".")[0]
    return name.replace("-", " ") if name else "My computer"


def os_label() -> str:
    system = platform.system()
    return {"Darwin": "macOS"}.get(system, system) + (f" {platform.mac_ver()[0]}" if system == "Darwin" and platform.mac_ver()[0] else "")


def package_version() -> str:
    try:
        from importlib.metadata import version
        return version("orchestrator")
    except Exception:
        return "dev"


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def call(path: str, body: dict[str, Any] | None = None, machine: dict[str, Any] | None = None, timeout: float = 15) -> dict[str, Any]:
    """POST JSON to the control plane. Raises AccountError with the control plane's message and status."""
    headers = {"Content-Type": "application/json"}
    if machine:
        headers["Authorization"] = f"Machine {machine['machine_id']}:{machine['machine_secret']}"
    req = urllib.request.Request(f"{control_url()}/{path.lstrip('/')}", data=json.dumps(body or {}).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error") or exc.reason
        except Exception:
            message = str(exc.reason)
        raise AccountError(str(message), exc.code)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise AccountError(f"Couldn't reach Orchestrator's servers ({getattr(exc, 'reason', exc)}).")


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def start_pairing(name: str | None = None) -> dict:
    name = name or default_name()
    return {**call("pair/start", {"name": name, "os": os_label(), "version": package_version()}), "name": name}


def poll_pairing(pairing: dict) -> dict:
    return call("pair/poll", {"code": pairing["code"], "poll_secret": pairing["poll_secret"]})


def finish_pairing(pairing: dict, claimed: dict) -> dict:
    if claimed.get("status") != "claimed":
        raise AccountError("Pairing hasn't completed.")
    machine = {"machine_id": claimed["machine_id"], "machine_secret": claimed["machine_secret"],
               "owner_email": str(claimed.get("owner_email") or "").lower(), "name": pairing["name"],
               "control_url": control_url(), "paired_at": time.time()}
    save_machine(machine)
    return machine


def connect(name: str | None = None, out: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
            poll_every: float = 3.0) -> dict[str, Any]:
    """Pair this computer with an account: show a code, wait for someone signed in to confirm it, keep the secret."""
    started = start_pairing(name)
    code = started["code"]
    out("")
    out(f"  Your code: {format_code(code)}")
    out(f"  Open {HOSTED_APP_URL}/#/connect?code={code} and sign in to add this computer to your account.")
    out(f"  (Or sign in at {HOSTED_APP_URL} and enter the code.) It expires in {started['expires_in'] // 60} minutes.")
    out("")
    deadline = time.time() + started["expires_in"] + 30
    while time.time() < deadline:
        sleep(poll_every)
        try:
            result = poll_pairing(started)
        except AccountError as exc:
            if exc.status in (404, 410):
                raise
            continue  # a dropped connection: keep waiting
        if result.get("status") == "claimed":
            return finish_pairing(started, result)
    raise AccountError("The pairing code expired. Run `orchestrator connect` again.", 410)


def heartbeat(machine: dict[str, Any], endpoint: str, running: int = 0, update_state: str | None = None,
              readiness: dict[str, str] | None = None) -> dict[str, Any]:
    """Tell the control plane this computer is up, where browsers reach it, and how many runs are going (so it can say
    so if the computer disappears mid-run). The app also reports its update progress, and the reply may ask it to update.
    Forgets the pairing if the account removed it."""
    body = {"endpoint": endpoint, "version": package_version(), "os": os_label(), "api_version": API_VERSION,
            "running": running, "packaged": bool(os.environ.get("ORCHESTRATOR_PACKAGED_APP"))}
    if update_state:
        body["update_state"] = update_state
    if readiness:
        body["readiness"] = readiness
    try:
        return call("machine/heartbeat", body, machine=machine)
    except AccountError as exc:
        if exc.status == 410:
            forget_machine()
        raise


def send_events(machine: dict[str, Any], events: list[dict[str, Any]], project: str) -> None:
    """Push what the UI server noticed to the owner's phones and browsers: [{key, title, body, path}] (notifier.Tracker)."""
    if events:
        call("machine/events", {"events": [{"key": e["key"], "title": f"{e['title']} · {project}", "body": e["body"],
                                            "path": e["path"]} for e in events]}, machine=machine)


def disconnect() -> bool:
    """Remove this computer from its account (best effort online) and forget the pairing here."""
    machine = load_machine()
    if machine is None:
        return False
    try:
        call("machine/leave", machine=machine)
    except AccountError as exc:
        if exc.status not in (401, 410):
            forget_machine()
            raise AccountError(f"Forgot the pairing here, but couldn't tell the account ({exc}). Remove it in the web app too.")
    forget_machine()
    return True


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_ticket(ticket: str, machine: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """The claims of a ticket the control plane signed for this computer, or AccountError. Replay is the caller's job (nonce)."""
    try:
        payload, signature = ticket.split(".", 1)
        expected = hmac.new(machine["machine_secret"].encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(signature), expected):
            raise ValueError("signature")
        claims = json.loads(_unb64(payload))
    except (ValueError, UnicodeError, json.JSONDecodeError, KeyError):
        raise AccountError("That sign-in link isn't valid for this computer.", 401)
    if claims.get("v") != 1 or claims.get("mid") != machine["machine_id"]:
        raise AccountError("That sign-in link is for a different computer.", 401)
    if float(claims.get("exp", 0)) < (time.time() if now is None else now):
        raise AccountError("That sign-in link expired. Sign in again.", 401)
    if not claims.get("email") or not claims.get("nonce"):
        raise AccountError("That sign-in link isn't valid for this computer.", 401)
    return claims
