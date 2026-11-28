"""Durable delivery of a machine's account-owned, cancellable update operation."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path

from orchestrator.project_setup import _atomic_json

TERMINAL = {"current", "cancelled", "failed", "unavailable", "expired"}
STATES = TERMINAL | {"requested", "checking", "downloading", "waiting_for_work", "updating", "cancelling"}
ORDER = {name: i for i, name in enumerate(("requested", "checking", "downloading", "waiting_for_work", "updating"))}


def clean_operation(value):
    if not isinstance(value, dict):
        return None
    if not isinstance(value.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", value["id"]):
        return None
    if not isinstance(value.get("action"), str) or not isinstance(value.get("state"), str) or value["action"] not in {"update", "cancel"} or value["state"] not in STATES:
        return None
    created, expires = value.get("created"), value.get("expires")
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (created, expires)):
        return None
    if not 0 <= created <= time.time() + 30 or not 0 < expires - created <= 1800:
        return None
    return {key: value[key] for key in ("id", "action", "state", "created", "expires")}


class UpdateRecovery:
    def __init__(self, path: Path, report):
        self.path, self._report = path, report
        self._lock = threading.RLock()
        self._pending = None
        try:
            saved = json.loads(path.read_text())
            self._operation = clean_operation(saved)
            if self._operation and isinstance(saved.get("pending_progress"), str) and saved["pending_progress"] in TERMINAL:
                self._pending = saved["pending_progress"]
        except (OSError, ValueError):
            self._operation = None

    def _save(self):
        _atomic_json(self.path, {**self._operation, **({"pending_progress": self._pending} if self._pending else {})})

    def snapshot(self):
        with self._lock:
            return dict(self._operation) if self._operation else None

    def ack(self):
        op = self.snapshot()
        return op["id"] if op and op["state"] in TERMINAL else None

    def accept(self, value):
        op = clean_operation(value)
        if op is None or (op["expires"] <= time.time() and op["state"] not in TERMINAL | {"updating", "cancelling"}):
            return False
        with self._lock:
            old = self._operation
            if old and old["id"] == op["id"]:
                if old["state"] in TERMINAL and op["state"] != old["state"]:
                    return False
                if old["action"] == "cancel" and op["action"] != "cancel":
                    return False
                if old["state"] == "updating" and op["state"] not in TERMINAL | {"updating"}:
                    return False
                if old["state"] in ORDER and op["state"] in ORDER and ORDER[op["state"]] < ORDER[old["state"]]:
                    return False
            elif old and old["state"] not in TERMINAL and (old["expires"] > time.time() or old["state"] == "updating"):
                return False
            if not old or old["id"] != op["id"] or op["state"] in TERMINAL:
                self._pending = None
            self._operation = op
            self._save()
            return True

    def retry_pending(self):
        with self._lock:
            state = self._pending
            op = self.snapshot()
        return self.report(op["id"], state) if state and op else False

    def report(self, operation_id, state):
        op = self.snapshot()
        if not op or op["id"] != operation_id or state not in STATES:
            return False
        if op["state"] in TERMINAL:
            return state == op["state"]
        if op["action"] == "cancel" and state != "cancelled":
            return False
        if op["expires"] <= time.time() and state == "updating":
            return False
        if state in TERMINAL:
            with self._lock:
                self._pending = state
                self._save()
        try:
            reply = self._report(operation_id, state)
        except Exception:
            return False  # A failed account connection never grants permission to install.
        if not isinstance(reply, dict):
            return False
        current = reply.get("update_operation")
        if isinstance(current, dict):
            self.accept(current)
        with self._lock:
            if self._operation["id"] != operation_id:
                return False
            self._pending = None
            self._save()
        return reply.get("accepted") is True and bool(current) and current.get("id") == operation_id and current.get("state") == state
