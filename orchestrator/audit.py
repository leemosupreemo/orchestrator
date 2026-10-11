"""A record, kept on this computer, of who did what through the web app: sign-ins (and refusals), runs they started,
settings they changed, and anything a member was stopped from doing. It holds names and actions, never values: no
tokens, keys or settings contents.

One JSON object per line in ~/.orchestrator/audit.jsonl. At about 5 MB the file moves to audit.jsonl.1 (replacing the
older one), so it can't grow without bound.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

MAX_BYTES = 5_000_000
TAIL_BYTES = 256_000
MAX_VALUE_CHARS = 200


class AuditLog:
    def __init__(self, path: Path, max_bytes: int = MAX_BYTES):
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def record(self, event: str, who: str, **detail: Any) -> None:
        entry: dict[str, Any] = {"at": round(time.time(), 3), "event": event, "who": _short(who)}
        entry.update({k: _short(v) for k, v in detail.items() if v not in (None, "")})
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate()
                new = not self.path.exists()
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
                if new:
                    self.path.chmod(0o600)  # it names people and where they connected from
        except OSError as exc:  # keeping a record must never break what is being recorded
            print(f"  Couldn't write the audit log: {exc}", flush=True)

    def _rotate(self) -> None:  # caller holds the lock
        try:
            if self.path.stat().st_size >= self.max_bytes:
                self.path.replace(self.path.with_name(self.path.name + ".1"))
        except FileNotFoundError:
            pass

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """The newest entries, newest first."""
        try:
            with self._lock, self.path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - TAIL_BYTES))
                raw = fh.read().decode("utf-8", "replace")
        except FileNotFoundError:
            return []
        lines = raw.splitlines()
        if size > TAIL_BYTES:
            lines = lines[1:]  # the first line of a tail is usually cut in half
        entries: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
            if len(entries) >= limit:
                break
        return entries


def _short(value: Any) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS - 1] + "…"
