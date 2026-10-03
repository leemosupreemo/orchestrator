#!/usr/bin/env python3
"""Pull app runtime logs from the project's central log store.

Device builds (e.g. Firebase App Distribution) ship their logs to Sentry Logs,
tagged with a per-launch `app_session` attribute. This module is the one
standard way to get them back into the orchestrator, from any machine —
including an SSH session on a phone, since nothing here touches the device:

    orchestrator logs setup                 # one-time: detect DSN, store read token, verify
    orchestrator logs sessions              # recent app launches
    orchestrator logs pull --latest         # newest launch -> .orchestrator/output/cloud_logs/...
    orchestrator logs pull --session 1a2b3c4d --level warning
    orchestrator logs tail                  # follow the newest launch live

Pulled logs are written as `cloud.log` in a directory, the same shape as pasted
logs, so they can be linked to a job and read by debug_job unchanged. Anywhere
a log path is accepted by debug_job, `cloud:latest` or `cloud:<session>` pulls
fresh logs instead.

Config lives in `.orchestrator/project.json` under `remote_logs`:

    "remote_logs": {
      "provider": "sentry",
      "api_base": "https://us.sentry.io",
      "org": "4510342215434240",
      "project": "4510342216941568",
      "token_env": "SENTRY_LOGS_TOKEN",
      "session_attribute": "app_session"
    }

The token (scopes: org:read, project:read, event:read) is read from the
`token_env` variable, which `common.load_secrets()` fills from
`.secrets/project-secrets.zsh`, `.orchestrator/.env` or `.env`.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from common import OUTPUT_DIR, ROOT, ORCHESTRATOR_RUNTIME_DIR, load_secrets, timestamp, write_json, write_text
from orchestrator.project_config import PROJECT_CONFIG

CLOUD_LOGS_DIR = OUTPUT_DIR / "cloud_logs"
SESSION_START_MARKER = "remote_log.session_start"
DEFAULT_TOKEN_ENV = "SENTRY_LOGS_TOKEN"
DEFAULT_SESSION_ATTRIBUTE = "app_session"
PAGE_SIZE = 100
DEFAULT_MAX_ROWS = 5000
LEVELS = ["trace", "debug", "info", "warn", "error", "fatal"]
LEVEL_ALIASES = {"warning": "warn", "critical": "fatal", "fault": "fatal", "notice": "info"}
REF_PREFIX = "cloud:"

DSN_RE = re.compile(r"https://[0-9a-f]{16,}@[A-Za-z0-9.\-]+(?::\d+)?/\d+")
DSN_SCAN_SUFFIXES = {".swift", ".m", ".plist", ".xcconfig", ".json", ".js", ".ts", ".kt", ".java", ".py", ".dart", ".env"}
# Hidden directories (.git, .swiftpm, .worktrees, ...) are skipped too.
DSN_SCAN_SKIP_DIRS = {"DerivedData", "node_modules", "Pods", "build", "SPM"}


class CloudLogsError(RuntimeError):
    pass


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class RemoteLogsConfig:
    api_base: str
    org: str
    project: str | None
    token_env: str = DEFAULT_TOKEN_ENV
    session_attribute: str = DEFAULT_SESSION_ATTRIBUTE
    provider: str = "sentry"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RemoteLogsConfig":
        if not data:
            raise CloudLogsError("remote_logs is not configured. Run: orchestrator logs setup")
        provider = data.get("provider", "sentry")
        if provider != "sentry":
            raise CloudLogsError(f"remote_logs.provider '{provider}' is not supported (only 'sentry').")
        missing = [k for k in ("api_base", "org") if not data.get(k)]
        if missing:
            raise CloudLogsError(f"remote_logs is missing {', '.join(missing)}. Run: orchestrator logs setup")
        return cls(
            api_base=str(data["api_base"]).rstrip("/"),
            org=str(data["org"]),
            project=str(data["project"]) if data.get("project") else None,
            token_env=data.get("token_env") or DEFAULT_TOKEN_ENV,
            session_attribute=data.get("session_attribute") or DEFAULT_SESSION_ATTRIBUTE,
            provider=provider,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"provider": self.provider, "api_base": self.api_base, "org": self.org}
        if self.project:
            data["project"] = self.project
        data["token_env"] = self.token_env
        data["session_attribute"] = self.session_attribute
        return data

    def token(self) -> str:
        load_secrets()
        # SENTRY_AUTH_TOKEN is often an upload-only token, so it is only a fallback.
        token = os.environ.get(self.token_env) or os.environ.get("SENTRY_AUTH_TOKEN")
        if not token:
            raise CloudLogsError(
                f"No Sentry read token found in ${self.token_env}. Run: orchestrator logs setup"
            )
        return token.strip()

    def token_settings_url(self) -> str:
        return f"{self.api_base}/settings/account/api/auth-tokens/"


def load_config() -> RemoteLogsConfig:
    return RemoteLogsConfig.from_dict(PROJECT_CONFIG.remote_logs)


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


# --------------------------------------------------------------------------- API client


def parse_next_cursor(link_header: str | None) -> str | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part and 'results="true"' in part:
            match = re.search(r'cursor="([^"]+)"', part)
            if match:
                return match.group(1)
    return None


def normalize_level(level: str | None) -> str | None:
    if not level:
        return None
    level = LEVEL_ALIASES.get(level.lower(), level.lower())
    if level not in LEVELS:
        raise CloudLogsError(f"Unknown level '{level}'. Use one of: {', '.join(LEVELS)}")
    return level


def level_query(min_level: str | None) -> str | None:
    level = normalize_level(min_level)
    if not level or level == LEVELS[0]:
        return None
    return "severity:[" + ",".join(LEVELS[LEVELS.index(level):]) + "]"


def since_to_stats_period(since: str) -> str:
    since = since.strip().lower()
    if not re.fullmatch(r"\d+[mhdw]", since):
        raise CloudLogsError(f"Invalid --since '{since}'. Use e.g. 30m, 6h, 2d, 1w.")
    return since


def verified_ssl_context() -> ssl.SSLContext:
    """python.org framework builds ship without CA certs until 'Install
    Certificates.command' is run, so prefer certifi, then macOS's bundle.
    Never falls back to an unverified context: requests carry a bearer token."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    if Path("/etc/ssl/cert.pem").exists():
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ssl.create_default_context()


def _default_urlopen(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout, context=verified_ssl_context())


class SentryLogsClient:
    """Minimal client for Sentry's Explore events API with dataset=logs."""

    def __init__(self, config: RemoteLogsConfig, token: str,
                 urlopen: Callable[..., Any] = _default_urlopen):
        self.config = config
        self.token = token
        self._urlopen = urlopen

    def _get(self, params: list[tuple[str, str]]) -> tuple[Any, str | None]:
        url = (f"{self.config.api_base}/api/0/organizations/{urllib.parse.quote(self.config.org)}/events/?"
               + urllib.parse.urlencode(params))
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "swift-orchestrator-cloud-logs",
        })
        try:
            with self._urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8")), response.headers.get("Link")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
            except Exception:
                pass
            if exc.code in (401, 403):
                raise CloudLogsError(
                    f"Sentry refused the token ({exc.code}{': ' + detail if detail else ''}). It needs the "
                    f"org:read, project:read and event:read scopes — create one at "
                    f"{self.config.token_settings_url()} and run: orchestrator logs setup"
                ) from exc
            raise CloudLogsError(f"Sentry API error {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise CloudLogsError(f"Could not reach {self.config.api_base}: {exc.reason}") from exc

    def query(self, fields: list[str], query: str = "", *, since: str = "24h", sort: str = "-timestamp",
              max_rows: int = DEFAULT_MAX_ROWS, fallback_fields: list[str] | None = None) -> list[dict[str, Any]]:
        """Returns up to `max_rows` log rows, following pagination.

        If Sentry rejects a field (400), retries once with `fallback_fields`, so an
        attribute that was never sent can't break a pull.
        """
        try:
            return self._query(fields, query, since, sort, max_rows)
        except CloudLogsError as exc:
            if fallback_fields and "error 400" in str(exc):
                return self._query(fallback_fields, query, since, sort, max_rows)
            raise

    def _query(self, fields: list[str], query: str, since: str, sort: str, max_rows: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(rows) < max_rows:
            params = [("dataset", "logs"), *[("field", f) for f in fields],
                      ("statsPeriod", since_to_stats_period(since)), ("sort", sort),
                      ("per_page", str(min(PAGE_SIZE, max_rows - len(rows))))]
            if self.config.project:
                params.append(("project", self.config.project))
            if query:
                params.append(("query", query))
            if cursor:
                params.append(("cursor", cursor))
            payload, link = self._get(params)
            page = payload.get("data", []) if isinstance(payload, dict) else []
            rows.extend(page)
            cursor = parse_next_cursor(link)
            if not cursor or not page:
                break
        return rows[:max_rows]


# --------------------------------------------------------------------------- sessions & formatting


@dataclass
class SessionInfo:
    session: str
    started: str
    channel: str = "?"
    version: str = "?"
    build: str = "?"
    user: str = ""
    device: str = ""

    def describe(self) -> str:
        who = " ".join(p for p in (self.device, f"user={self.user}" if self.user else "") if p)
        return (f"{self.session}  {format_timestamp(self.started)}  {self.channel:<10} "
                f"v{self.version} ({self.build})  {who}").rstrip()


def parse_session_start(row: dict[str, Any], session_attribute: str) -> SessionInfo | None:
    message = str(row.get("message") or "")
    values = dict(re.findall(r"(\w+)=(\S+)", message))
    session = values.get("session") or row.get(session_attribute)
    if not session:
        return None
    return SessionInfo(
        session=str(session),
        started=str(row.get("timestamp") or ""),
        channel=values.get("channel", "?"),
        version=values.get("version", "?"),
        build=values.get("build", "?"),
        user=str(row.get("user.id") or ""),
        device=str(row.get("device.model") or ""),
    )


def format_timestamp(value: Any) -> str:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.") + f"{parsed.microsecond // 1000:03d}Z"


def format_log_line(row: dict[str, Any]) -> str:
    level = str(row.get("severity") or "?").upper()
    category = row.get("category")
    prefix = f"[{category}] " if category else ""
    return f"{format_timestamp(row.get('timestamp'))}  {level:<5}  {prefix}{row.get('message', '')}"


def log_fields(session_attribute: str) -> list[str]:
    return ["timestamp", "severity", "message", "category", session_attribute]


BASE_FIELDS = ["timestamp", "severity", "message"]


def list_sessions(client: SentryLogsClient, *, since: str = "24h", limit: int = 20) -> list[SessionInfo]:
    attr = client.config.session_attribute
    rows = client.query(
        ["timestamp", "message", attr, "user.id", "device.model"],
        f"message:*{SESSION_START_MARKER}*",
        since=since, max_rows=limit,
        fallback_fields=["timestamp", "message"],
    )
    sessions = [s for s in (parse_session_start(r, attr) for r in rows) if s]
    if sessions:
        return sessions

    # No start markers (e.g. an App Store build, which only sends errors):
    # derive sessions from whatever logs carry the session attribute.
    rows = client.query(["timestamp", attr], f"has:{attr}", since=since, max_rows=1000)
    seen: dict[str, SessionInfo] = {}
    for row in rows:
        session = row.get(attr)
        if session and session not in seen:
            seen[session] = SessionInfo(session=str(session), started=str(row.get("timestamp") or ""))
    return list(seen.values())[:limit]


def resolve_session(client: SentryLogsClient, session: str | None, since: str) -> str:
    if session and session != "latest":
        return session
    sessions = list_sessions(client, since=since, limit=1)
    if not sessions:
        raise CloudLogsError(f"No app sessions found in the last {since}. Launch the app, wait ~5s, and retry.")
    return sessions[0].session


def fetch_session_logs(client: SentryLogsClient, session: str, *, since: str = "24h",
                       min_level: str | None = None, extra_query: str = "",
                       max_rows: int = DEFAULT_MAX_ROWS) -> list[dict[str, Any]]:
    attr = client.config.session_attribute
    query = " ".join(q for q in (f"{attr}:{session}", level_query(min_level), extra_query) if q)
    # Newest first so a capped pull keeps the end of the session, then flip to reading order.
    rows = client.query(log_fields(attr), query, since=since, max_rows=max_rows, fallback_fields=BASE_FIELDS)
    rows.reverse()
    return rows


def write_pull(session: str, rows: list[dict[str, Any]], meta: dict[str, Any],
               base_dir: Path | None = None) -> Path:
    base_dir = base_dir or CLOUD_LOGS_DIR
    out_dir = base_dir / f"{timestamp()}-{session}"
    header = [f"# cloud logs  session={session}  rows={len(rows)}  pulled={datetime.now(timezone.utc).isoformat(timespec='seconds')}"]
    if meta.get("query"):
        header.append(f"# query: {meta['query']}")
    if meta.get("truncated"):
        header.append(f"# NOTE: capped at {len(rows)} rows; earlier entries omitted (use --max-rows)")
    write_text(out_dir / "cloud.log", "\n".join(header + [format_log_line(r) for r in rows]) + "\n")
    write_json(out_dir / "meta.json", {**meta, "session": session, "rows": len(rows)})

    latest = base_dir / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(out_dir.name, target_is_directory=True)
    except OSError:
        pass
    return out_dir


def pull(session: str | None = "latest", *, since: str = "24h", min_level: str | None = None,
         extra_query: str = "", max_rows: int = DEFAULT_MAX_ROWS,
         client: SentryLogsClient | None = None) -> Path:
    """Pulls one app session's logs to disk and returns the output directory."""
    if client is None:
        config = load_config()
        client = SentryLogsClient(config, config.token())
    resolved = resolve_session(client, session, since)
    rows = fetch_session_logs(client, resolved, since=since, min_level=min_level,
                              extra_query=extra_query, max_rows=max_rows)
    if not rows:
        raise CloudLogsError(f"Session {resolved} has no logs in the last {since}.")
    meta = {"provider": "sentry", "since": since, "min_level": min_level, "query": extra_query,
            "truncated": len(rows) >= max_rows}
    return write_pull(resolved, rows, meta)


def resolve_log_ref(ref: str) -> str:
    """Turns `cloud:latest` / `cloud:<session>` into a freshly pulled log directory."""
    session = ref[len(REF_PREFIX):].strip() or "latest"
    out_dir = pull(session)
    try:
        return str(out_dir.relative_to(ROOT))
    except ValueError:
        return str(out_dir)


def is_log_ref(value: str) -> bool:
    return value.strip().startswith(REF_PREFIX)


def tail(client: SentryLogsClient, session: str | None, *, interval: float = 5.0,
         min_level: str | None = None, out: Any = sys.stdout, iterations: int | None = None,
         sleep: Callable[[float], None] = time.sleep) -> None:
    resolved = resolve_session(client, session, "24h")
    print(f"Following session {resolved} (Ctrl-C to stop)...", file=out, flush=True)
    seen: set[tuple[str, str]] = set()
    count = 0
    while iterations is None or count < iterations:
        rows = fetch_session_logs(client, resolved, since="1h", min_level=min_level, max_rows=500)
        for row in rows:
            key = (str(row.get("timestamp")), str(row.get("message")))
            if key not in seen:
                seen.add(key)
                print(format_log_line(row), file=out, flush=True)
        count += 1
        if iterations is None or count < iterations:
            sleep(interval)


# --------------------------------------------------------------------------- setup


def project_json_path() -> Path:
    override = os.environ.get("ORCHESTRATOR_CONFIG")
    return Path(override).expanduser() if override else ORCHESTRATOR_RUNTIME_DIR / "project.json"


def save_config(config: RemoteLogsConfig, path: Path | None = None) -> Path:
    path = path or project_json_path()
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data["remote_logs"] = config.to_dict()
    write_json(path, data)
    return path


def save_token(env_name: str, token: str, env_file: Path | None = None) -> Path:
    env_file = env_file or ORCHESTRATOR_RUNTIME_DIR / ".env"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    lines = [l for l in lines if not re.match(rf"^\s*(export\s+)?{re.escape(env_name)}=", l)]
    lines.append(f"{env_name}={token}")
    write_text(env_file, "\n".join(lines) + "\n")
    os.chmod(env_file, 0o600)
    os.environ[env_name] = token
    return env_file


def is_git_ignored(path: Path) -> bool | None:
    try:
        result = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=ROOT,
                                capture_output=True, timeout=5)
    except Exception:
        return None
    return result.returncode == 0


def verify(client: SentryLogsClient) -> str:
    rows = client.query(BASE_FIELDS, "", since="7d", max_rows=1)
    if not rows:
        return "token works, but no logs in the last 7 days yet (launch a tester build to send some)"
    return f"token works; newest log {format_timestamp(rows[0].get('timestamp'))}"


def run_setup(args: argparse.Namespace) -> int:
    existing = PROJECT_CONFIG.remote_logs or {}
    dsn = args.dsn
    if not dsn and not (args.org and args.api_base):
        found = detect_dsn(ROOT)
        if found:
            dsn, where = found
            try:
                where = where.relative_to(ROOT)
            except ValueError:
                pass
            print(f"Found Sentry DSN in {where}")
    coords: dict[str, Any] = {**existing}
    if dsn:
        coords.update({k: v for k, v in parse_dsn(dsn).items() if v})
    for key in ("api_base", "org", "project"):
        if getattr(args, key, None):
            coords[key] = getattr(args, key)
    if args.token_env:
        coords["token_env"] = args.token_env
    if not coords.get("api_base") or not coords.get("org"):
        print("Could not determine the Sentry org. Pass --org <slug-or-id> and --api-base <url> (or --dsn).")
        return 1
    config = RemoteLogsConfig.from_dict({"provider": "sentry", **coords})
    print(f"  api_base: {config.api_base}\n  org:      {config.org}\n  project:  {config.project or '(all)'}")

    load_secrets()
    token = os.environ.get(config.token_env)
    if not token and not args.no_prompt and sys.stdin.isatty():
        print(f"\nCreate a Sentry token with scopes org:read, project:read, event:read:\n  {config.token_settings_url()}")
        token = getpass.getpass(f"Paste token for ${config.token_env} (input hidden, Enter to skip): ").strip()
        if token:
            env_file = save_token(config.token_env, token)
            ignored = is_git_ignored(env_file)
            print(f"Saved to {env_file} (chmod 600)")
            if ignored is False:
                print(f"\033[93mWARNING: {env_file} is not git-ignored. Add it to .gitignore before committing.\033[0m")

    path = save_config(config)
    print(f"Wrote remote_logs to {path}")

    if args.no_verify:
        return 0
    try:
        print("Verifying: " + verify(SentryLogsClient(config, config.token())))
    except CloudLogsError as exc:
        print(f"\033[91mVerification failed: {exc}\033[0m")
        return 1
    return 0


# --------------------------------------------------------------------------- CLI


def default_client() -> SentryLogsClient:
    config = load_config()
    return SentryLogsClient(config, config.token())


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator logs", description="App runtime logs from the central log store.")
    sub = parser.add_subparsers(dest="action", required=True)

    setup = sub.add_parser("setup", help="Configure and verify access to the log store")
    setup.add_argument("--dsn", help="Sentry DSN (default: detected from the repo)")
    setup.add_argument("--org", help="Sentry org slug or id")
    setup.add_argument("--project", help="Sentry project id")
    setup.add_argument("--api-base", dest="api_base", help="e.g. https://us.sentry.io")
    setup.add_argument("--token-env", dest="token_env", help=f"Env var holding the read token (default {DEFAULT_TOKEN_ENV})")
    setup.add_argument("--no-prompt", action="store_true", help="Never prompt for a token")
    setup.add_argument("--no-verify", action="store_true", help="Skip the live API check")

    sessions = sub.add_parser("sessions", help="List recent app launches")
    sessions.add_argument("--since", default="24h")
    sessions.add_argument("--limit", type=int, default=20)
    sessions.add_argument("--json", action="store_true")

    pull_p = sub.add_parser("pull", help="Download one launch's logs")
    target = pull_p.add_mutually_exclusive_group()
    target.add_argument("--session", help="app_session id (default: latest)")
    target.add_argument("--latest", action="store_true", help="Newest launch (default)")
    pull_p.add_argument("--since", default="24h")
    pull_p.add_argument("--level", help="Minimum level: trace, debug, info, warn, error, fatal")
    pull_p.add_argument("--query", default="", help="Extra Sentry search, e.g. 'category:LobbyViewModel'")
    pull_p.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    pull_p.add_argument("--print", dest="print_logs", action="store_true", help="Also print the logs")

    tail_p = sub.add_parser("tail", help="Follow a launch's logs live")
    tail_p.add_argument("--session", help="app_session id (default: latest)")
    tail_p.add_argument("--level")
    tail_p.add_argument("--interval", type=float, default=5.0)

    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.action == "setup":
            return run_setup(args)
        if args.action == "sessions":
            found = list_sessions(default_client(), since=args.since, limit=args.limit)
            if args.json:
                print(json.dumps([s.__dict__ for s in found], indent=2))
            elif not found:
                print(f"No app sessions in the last {args.since}.")
            for s in ([] if args.json else found):
                print(s.describe())
            return 0
        if args.action == "pull":
            out_dir = pull(args.session or "latest", since=args.since, min_level=args.level,
                           extra_query=args.query, max_rows=args.max_rows)
            log_file = out_dir / "cloud.log"
            if args.print_logs:
                print(log_file.read_text(encoding="utf-8"), end="")
            try:
                shown = out_dir.relative_to(ROOT)
            except ValueError:
                shown = out_dir
            meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
            print(f"Saved {meta['rows']} log lines from session {meta['session']} to {shown}/cloud.log")
            return 0
        if args.action == "tail":
            tail(default_client(), args.session, interval=args.interval, min_level=args.level)
            return 0
    except CloudLogsError as exc:
        print(f"\033[91m{exc}\033[0m", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
