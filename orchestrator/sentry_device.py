"""Sign in to Sentry with its device flow: Orchestrator shows a code, the person approves it in the browser, and the token
comes back here. Nothing to create or paste, and the organization comes from the token, so there's no slug to type.

From Sentry's OAuth guide (docs.sentry.io/api/guides/oauth/, Device Authorization Flow):
  POST https://sentry.io/oauth/device/code/   client_id, scope (space separated)
       -> device_code, user_code, verification_uri(_complete), expires_in (600), interval (5)
  POST https://sentry.io/oauth/token/         client_id, device_code, grant_type=urn:ietf:params:oauth:grant-type:device_code
       -> 400 {"error": "authorization_pending" | "slow_down" | "access_denied" | "expired_token"} until approved,
          then access_token, refresh_token, expires_at (tokens last 30 days)
A token covers the one organization picked while approving, so GET /api/0/organizations/ returns just that one.

Orchestrator is a public client (no secret), registered once in Sentry under Developer Settings. Its client id comes from
ORCHESTRATOR_SENTRY_CLIENT_ID, the `sentry_client_id` setting, or CLIENT_ID below. Refreshing needs a client secret per the
guide, so when a token runs out the person signs in again. Self-hosted Sentry keeps the token form.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

HOST = "https://sentry.io"
CLIENT_ID = ""  # Orchestrator's public OAuth app on sentry.io, once registered
# Reading issues and events (Device logs, jobs' Sentry links), plus event:write so jobs can comment on and resolve issues.
SCOPES = "org:read project:read event:read event:write"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"
TIMEOUT = 20

Post = Callable[[str, dict], tuple[int, dict]]
Get = Callable[[str, str], Any]


class DeviceError(RuntimeError):
    pass


def client_id(settings: Mapping[str, Any] | None = None, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return (env.get("ORCHESTRATOR_SENTRY_CLIENT_ID") or str((settings or {}).get("sentry_client_id") or "") or CLIENT_ID).strip()


def _post(url: str, form: dict) -> tuple[int, dict]:
    """A form POST; Sentry answers errors (including "still waiting") as JSON with a 4xx status, so both come back."""
    request = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            return reply.status, json.loads(reply.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DeviceError("Couldn't reach Sentry. Check the connection and try again.") from exc


def _get(url: str, token: str) -> Any:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            return json.loads(reply.read() or b"[]")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DeviceError("Signed in, but Sentry wouldn't say which organization. Try again.") from exc


def start(cid: str, *, post: Post = _post) -> dict[str, Any]:
    """Ask Sentry for a code. Returns the code to show, the page to approve it on, how long it lasts and how often to check.
    `device_code` is the secret half: keep it on the server."""
    if not cid:
        raise DeviceError("Sign in with Sentry isn't set up here yet. Use a token instead.")
    status, data = post(f"{HOST}/oauth/device/code/", {"client_id": cid, "scope": SCOPES})
    if status != 200 or not data.get("device_code") or not data.get("user_code"):
        raise DeviceError(f"Sentry didn't start the sign-in{': ' + str(data['error']) if data.get('error') else ''}. Try again.")
    return {"device_code": data["device_code"], "user_code": data["user_code"],
            "url": data.get("verification_uri_complete") or data.get("verification_uri") or f"{HOST}/oauth/device/",
            "expires_in": int(data.get("expires_in") or 600), "interval": int(data.get("interval") or 5)}


def poll(cid: str, device_code: str, *, post: Post = _post) -> dict[str, Any]:
    """Check once whether the code has been approved: pending (maybe with slow_down), done with the token, or why it ended."""
    status, data = post(f"{HOST}/oauth/token/", {"client_id": cid, "device_code": device_code, "grant_type": GRANT})
    if status == 200 and data.get("access_token"):
        expires = str(data.get("expires_at") or "")[:10]
        return {"status": "done", "token": data["access_token"], "refresh_token": data.get("refresh_token") or "", "expires": expires}
    error = data.get("error")
    if error == "authorization_pending":
        return {"status": "pending"}
    if error == "slow_down":
        return {"status": "pending", "slow_down": True}
    if error == "access_denied":
        return {"status": "denied", "message": "The sign-in wasn't approved in Sentry. Start again if that was a mistake."}
    if error == "expired_token":
        return {"status": "expired", "message": "The code ran out before it was approved. Start again for a new one."}
    return {"status": "error", "message": f"Sentry stopped the sign-in ({error or status}). Start again, or use a token instead."}


def organization(token: str, *, get: Get = _get) -> dict[str, str]:
    """The organization the token was approved for (a device-flow token covers just the one picked while approving)."""
    orgs = get(f"{HOST}/api/0/organizations/", token)
    if not isinstance(orgs, list) or not orgs or not isinstance(orgs[0], dict) or not orgs[0].get("slug"):
        raise DeviceError("Signed in, but no Sentry organization came with it. Start again and pick one when approving.")
    return {"slug": str(orgs[0]["slug"]), "name": str(orgs[0].get("name") or orgs[0]["slug"])}
