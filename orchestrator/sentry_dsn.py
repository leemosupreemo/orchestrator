"""Finding a project's Sentry DSN and turning it into API coordinates.

Shared by the device logs (scripts/cloud_logs.py) and the Sentry connection, so both find the same org and
project. Standard library only.
"""
from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path

DSN_RE = re.compile(r"https://[0-9a-f]{16,}@[A-Za-z0-9.\-]+(?::\d+)?/\d+")
DSN_SCAN_SUFFIXES = {".swift", ".m", ".plist", ".xcconfig", ".json", ".js", ".ts", ".kt", ".java", ".py", ".dart", ".env"}
# Hidden directories (.git, .swiftpm, .worktrees, ...) are skipped too.
DSN_SCAN_SKIP_DIRS = {"DerivedData", "node_modules", "Pods", "build", "SPM", "venv", "site-packages"}


def parse_dsn(dsn: str) -> dict[str, str | None]:
    """Maps a Sentry DSN to API coordinates.

    SaaS DSNs look like https://<key>@o<org_id>.ingest[.<region>].sentry.io/<project_id>;
    the API for that org lives at https://[<region>.]sentry.io. Self-hosted DSNs
    carry no org, so `org` comes back None and must be supplied.
    """
    parsed = urllib.parse.urlparse(dsn.strip())
    host = parsed.hostname or ""
    project = parsed.path.strip("/").split("/")[-1] or None
    match = re.match(r"^o(\d+)\.ingest\.(?:([a-z0-9-]+)\.)?sentry\.io$", host)
    if match:
        org_id, region = match.groups()
        api_host = f"{region}.sentry.io" if region else "sentry.io"
        return {"api_base": f"https://{api_host}", "org": org_id, "project": project}
    port = f":{parsed.port}" if parsed.port else ""
    return {"api_base": f"{parsed.scheme}://{host}{port}", "org": None, "project": project}


TEST_PATH_RE = re.compile(r"test|spec|fixture|mock|sample|example", re.IGNORECASE)


def detect_dsn(root: Path) -> tuple[str, Path] | None:
    """Finds the app's Sentry DSN. Test code often carries fake DSNs, so a hit
    under a test/fixture/mock path is only used when nothing else matches."""
    fallback: tuple[str, Path] | None = None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in DSN_SCAN_SKIP_DIRS and not d.startswith(".") and not d.endswith(".xcassets"))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.suffix not in DSN_SCAN_SUFFIXES:
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                match = DSN_RE.search(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            if not match:
                continue
            if not TEST_PATH_RE.search(str(path.relative_to(root))):
                return match.group(0), path
            fallback = fallback or (match.group(0), path)
    return fallback
