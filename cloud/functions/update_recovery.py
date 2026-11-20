"""Pure per-machine recovery operation transitions (persisted separately from heartbeat)."""
from __future__ import annotations

TTL = 1800
TERMINAL = frozenset({"current", "cancelled", "failed", "unavailable", "expired"})
STAGES = {"requested": 0, "checking": 1, "downloading": 2, "waiting_for_work": 3, "updating": 4}
STATES = frozenset(STAGES) | TERMINAL | {"cancelling"}


def expire(operation, now):
    if operation and operation["state"] not in TERMINAL | {"updating"} and operation["expires"] <= now:
        return {**operation, "state": "expired"}
    return operation


def public(operation):
    return {key: operation[key] for key in ("id", "action", "state", "created", "expires")} if operation else None


def start(operation, now, operation_id):
    operation = expire(operation, now)
    if operation and operation["state"] not in TERMINAL:
        return operation
    return {"id": operation_id, "action": "update", "state": "requested", "created": now, "expires": now + TTL}


def cancel(operation, operation_id, now):
    operation = expire(operation, now)
    if not operation or operation["id"] != operation_id or operation["state"] == "updating":
        return operation, False
    if operation["state"] in TERMINAL:
        return operation, operation["state"] == "cancelled"
    return {**operation, "action": "cancel", "state": "cancelling"}, True


def progress(operation, operation_id, state, now):
    operation = expire(operation, now)
    if not operation or operation["id"] != operation_id or state not in STATES:
        return operation, False
    old = operation["state"]
    if old in TERMINAL:
        return operation, old == state
    if old == "cancelling":
        return ({**operation, "state": state}, True) if state == "cancelled" else (operation, False)
    if state in {"requested", "cancelling", "cancelled", "expired"}:
        return operation, False
    if state in STAGES and STAGES[state] < STAGES[old]:
        return operation, False
    return {**operation, "state": state}, True
