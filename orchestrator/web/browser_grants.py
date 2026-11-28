"""Ephemeral, single-use grants minted only through the private desktop socket."""
import secrets
import threading
import time
from urllib.parse import urlsplit


class BrowserGrantStore:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._items = {}

    def issue(self, route: str) -> str:
        if not isinstance(route, str) or len(route) > 8192 or any(ord(c) < 32 for c in route):
            raise ValueError("Invalid browser route")
        parsed = urlsplit(route)
        fragment_path = parsed.fragment.split("?", 1)[0]
        if parsed.scheme or parsed.netloc or parsed.path or parsed.query or fragment_path not in (
                "/", "/setup", "/projects", "/new-project"):
            raise ValueError("Invalid browser route")
        with self._lock:
            now = self.clock()
            self._items = {key: value for key, value in self._items.items() if value[0] > now}
            if len(self._items) >= 128:
                raise ValueError("Too many pending browser openings")
            grant = secrets.token_urlsafe(32)
            self._items[grant] = (now + 60, route)
            return grant

    def consume(self, grant: str) -> str | None:
        with self._lock:
            item = self._items.pop(grant, None)
            return item[1] if item and item[0] > self.clock() else None
