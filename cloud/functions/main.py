"""Cloud Functions entry point: serves the control plane at /cp/** on the hosted app (see firebase.json)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from firebase_admin import auth, exceptions, firestore, initialize_app, messaging
from firebase_functions import https_fn, options, scheduler_fn

from control_plane import ControlError, ControlPlane

initialize_app()


class FirestoreStore:
    def __init__(self) -> None:
        self.db = firestore.client()

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        snap = self.db.collection(collection).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self.db.collection(collection).document(doc_id).set(_with_expiry(data))

    def update(self, collection: str, doc_id: str, fields: dict[str, Any]) -> None:
        self.db.collection(collection).document(doc_id).update(_with_expiry(fields))

    def delete(self, collection: str, doc_id: str) -> None:
        self.db.collection(collection).document(doc_id).delete()

    def where(self, collection: str, field: str, value: Any) -> list[tuple[str, dict[str, Any]]]:
        query = self.db.collection(collection).where(filter=firestore.FieldFilter(field, "==", value))
        return [(snap.id, snap.to_dict()) for snap in query.stream()]


def _with_expiry(data: dict[str, Any]) -> dict[str, Any]:
    """Firestore's TTL policy needs a timestamp field; `expire_at` mirrors the numeric `expires` so stale pairings are swept."""
    if "expires" in data:
        return {**data, "expire_at": datetime.fromtimestamp(data["expires"], tz=timezone.utc)}
    return data


class FcmPusher:
    """Web push through Firebase Cloud Messaging. Data-only, so the app's service worker decides how it looks."""

    GONE = (messaging.UnregisteredError, messaging.SenderIdMismatchError, exceptions.InvalidArgumentError)

    def send(self, tokens: list[str], message: dict[str, str]) -> list[str]:
        gone: list[str] = []
        for start in range(0, len(tokens), 500):
            batch = tokens[start:start + 500]
            result = messaging.send_each_for_multicast(messaging.MulticastMessage(
                tokens=batch, data=message,
                webpush=messaging.WebpushConfig(headers={"Urgency": "high", "TTL": "86400"})))
            gone += [t for t, r in zip(batch, result.responses) if not r.success and isinstance(r.exception, self.GONE)]
        return gone


def _verify_user(id_token: str) -> dict[str, Any]:
    claims = auth.verify_id_token(id_token)
    return {"uid": claims["uid"], "email": claims.get("email", ""), "email_verified": bool(claims.get("email_verified"))}


_plane: ControlPlane | None = None


def _get_plane() -> ControlPlane:
    global _plane
    if _plane is None:
        _plane = ControlPlane(FirestoreStore(), pusher=FcmPusher())
    return _plane


@scheduler_fn.on_schedule(schedule="every 5 minutes", region="us-central1", max_instances=1)
def sweep(event: scheduler_fn.ScheduledEvent) -> None:
    """Tell people when a computer stops reporting while work is running on it."""
    _get_plane().sweep()


@https_fn.on_request(region="us-central1", max_instances=10, memory=options.MemoryOption.MB_256)
def cp(req: https_fn.Request) -> https_fn.Response:
    plane = _get_plane()
    if req.method == "POST" and req.content_length and req.content_length > 16_384:
        return _json(413, {"error": "Request too large"})
    try:
        body = (req.get_json(silent=True) or {}) if req.method == "POST" else {}
        if not isinstance(body, dict):
            raise ControlError("Invalid JSON")
    except ControlError as exc:
        return _json(exc.status, {"error": str(exc)})
    headers = {k.lower(): v for k, v in req.headers.items()}
    status, payload = plane.handle(req.method, req.path, headers, body, _verify_user)
    return _json(status, payload)


def _json(status: int, payload: dict[str, Any]) -> https_fn.Response:
    return https_fn.Response(json.dumps(payload), status=status, content_type="application/json",
                             headers={"Cache-Control": "no-store"})
