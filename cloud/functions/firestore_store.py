"""Firestore persistence adapter, independent of Cloud Functions entry points."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore


class FirestoreStore:
    def __init__(self, client) -> None:
        self.db = client

    def mutate(self, collection: str, doc_id: str, transform):
        reference = self.db.collection(collection).document(doc_id)

        @firestore.transactional
        def apply(transaction):
            snapshot = reference.get(transaction=transaction)
            document, result = transform(snapshot.to_dict() if snapshot.exists else None)
            if document is not None:
                # Operation expiry is logical, not a Firestore TTL: installing operations
                # must survive their deadline until the runner reconciles after relaunch.
                transaction.set(reference, document)
            return result
        return apply(self.db.transaction())

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

