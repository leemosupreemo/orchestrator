"""Logs for working out why the web app lost its connection, kept on this computer in ~/.orchestrator/logs/:

- requests.jsonl   every API request this server answered: the path (never the query, which can carry a token), status, time taken
- client.jsonl     what the page itself saw go wrong (failed fetches, going offline, the tab hidden), sent once it reconnects
- cloudflared.log  the tunnel's own output, which used to be read and thrown away

`orchestrator connection-log -f` follows all three as one stream.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from orchestrator.audit import AuditLog
from orchestrator.project_config import user_state_dir

MAX_TUNNEL_BYTES = 5_000_000
EVENTS_PER_POST = 100
# What the page may report about a failure. Anything else it sends is dropped.
CLIENT_FIELDS = ("client_at", "method", "path", "status", "ms", "error", "online", "visible", "backend",
                 "down_ms", "failures", "connection", "ua")


def logs_dir() -> Path:
    return user_state_dir() / "logs"


def requests_log() -> AuditLog:
    return AuditLog(logs_dir() / "requests.jsonl")


def client_log() -> AuditLog:
    return AuditLog(logs_dir() / "client.jsonl")


def tunnel_log_path() -> Path:
    return logs_dir() / "cloudflared.log"


def open_tunnel_log() -> IO[str] | None:
    """Somewhere to copy cloudflared's output, or None if it can't be written (the tunnel runs either way)."""
    path = tunnel_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= MAX_TUNNEL_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        handle = path.open("a", encoding="utf-8", buffering=1)
        path.chmod(0o600)  # it shows this computer's tunnel address
        return handle
    except OSError:
        return None


def record_client_events(log: AuditLog, events: Any, who: str, ip: str) -> int:
    """Keep what the page reported, limited to known fields. Returns how many events were kept."""
    if not isinstance(events, list):
        return 0
    kept = 0
    for event in events[:EVENTS_PER_POST]:
        if not isinstance(event, dict):
            continue
        detail = {key: event[key] for key in CLIENT_FIELDS if key in event}
        log.record(str(event.get("kind") or "event"), who, ip=ip, **detail)
        kept += 1
    return kept


# -- reading them back

SOURCES = (("server", "requests.jsonl"), ("browser", "client.jsonl"), ("tunnel", "cloudflared.log"))


def format_line(source: str, line: str) -> str:
    line = line.rstrip("\n")
    if source == "tunnel":
        return f"tunnel   {line}"
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return f"{source:<8} {line}"
    if not isinstance(entry, dict):
        return f"{source:<8} {line}"
    at = datetime.fromtimestamp(float(entry.pop("at", 0) or 0)).strftime("%H:%M:%S")
    event = entry.pop("event", "")
    entry.pop("who", None)
    if source == "server":
        head = f"{entry.pop('method', '')} {entry.pop('path', '')} {entry.pop('status', '')} {entry.pop('ms', '')}ms"
    else:
        head = event
    rest = " ".join(f"{key}={value}" for key, value in entry.items())
    return f"{at} {source:<8} {head} {rest}".rstrip()


def _tail(path: Path, lines: int) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 200_000))
            return fh.read().decode("utf-8", "replace").splitlines()[-lines:]
    except FileNotFoundError:
        return []


def show(lines: int = 40, follow: bool = False, out: Any = None) -> int:
    import sys
    out = out or sys.stdout
    folder = logs_dir()
    print(f"Connection logs in {folder}", file=out)
    for source, name in SOURCES:
        recent = _tail(folder / name, lines)
        if recent:
            print(f"-- {name} (last {len(recent)})", file=out)
            for line in recent:
                print(format_line(source, line), file=out)
    if not follow:
        return 0
    print("-- following (Ctrl-C to stop)", file=out, flush=True)
    offsets = {name: _size(folder / name) for _, name in SOURCES}
    try:
        while True:
            for source, name in SOURCES:
                path = folder / name
                size = _size(path)
                if size < offsets[name]:  # rotated: start the new file from the top
                    offsets[name] = 0
                if size > offsets[name]:
                    with path.open("rb") as fh:
                        fh.seek(offsets[name])
                        chunk = fh.read()
                    complete = chunk.rfind(b"\n") + 1
                    offsets[name] += complete
                    for line in chunk[:complete].decode("utf-8", "replace").splitlines():
                        print(format_line(source, line), file=out, flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0
