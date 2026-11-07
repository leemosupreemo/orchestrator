from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from orchestrator.project_config import (
    DEFAULT_RUNTIME_DIRNAME,
    find_project_root,
    load_recent_projects,
    safe_cwd,
    safe_resolve,
)
from orchestrator.web.contract import Operation, validate_command
from orchestrator.web.jobs import create_job, list_jobs, list_archived_jobs, run_job_action, _job_path
from orchestrator.web.projection import project_job, redact_data, redact_text
from orchestrator.web.tools import run_tool, run_app_tests, run_refresh, _find_test_plans, _read, _NAME


_COMMAND_FIELDS = ("project_id", "operation", "job_id", "inputs", "expected_revision")
_NEW_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
_MAX_BODY_BYTES = 16 * 1024 * 1024  # Room for a maximum artifact after base64 encoding.


def _project_id(name: str) -> str:
    """Turn a folder or project name into an ID the command contract accepts."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).lstrip("-_.")[:128] or "project"


def get_static_dir() -> Path:
    pkg_dist = Path(__file__).resolve().parent / "dist"
    if pkg_dist.is_dir() and (pkg_dist / "index.html").is_file():
        return pkg_dist
    repo_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if repo_dist.is_dir() and (repo_dist / "index.html").is_file():
        return repo_dist
    return pkg_dist


def discover_local_projects(explicit_root: Path | None = None) -> dict[str, Path]:
    projects: dict[str, Path] = {}
    
    # 1. Explicit root or environment variable
    if explicit_root and explicit_root.is_dir():
        name = explicit_root.name
        projects[name] = explicit_root.resolve()
    elif os.environ.get("ORCHESTRATOR_PROJECT_ROOT"):
        p = Path(os.environ["ORCHESTRATOR_PROJECT_ROOT"]).resolve()
        if p.is_dir():
            projects[p.name] = p

    # 2. Configured web projects JSON env var
    if os.environ.get("ORCHESTRATOR_WEB_PROJECTS"):
        try:
            extra = json.loads(os.environ["ORCHESTRATOR_WEB_PROJECTS"])
            for k, v in extra.items():
                p = Path(v).expanduser().resolve()
                if p.is_dir():
                    projects[k] = p
        except Exception:
            pass

    # 3. Current working directory
    current = safe_resolve(safe_cwd())
    for cand in [current, *current.parents]:
        if (cand / DEFAULT_RUNTIME_DIRNAME / "project.json").exists():
            projects[cand.name] = cand
            break

    # 4. Recent projects from user config
    try:
        recent = load_recent_projects()
        for item in recent.get("projects", []):
            name = item.get("name")
            root_str = item.get("root")
            if name and root_str:
                p = Path(root_str).resolve()
                if p.is_dir() and name not in projects:
                    projects[name] = p
    except Exception:
        pass

    if not projects:
        projects["default"] = current

    return {_project_id(name): root for name, root in projects.items()}


class LocalCommandRunner:
    def __init__(self, projects: dict[str, Path]):
        self.projects = projects
        self.commands: list[dict[str, Any]] = []
        self.artifacts: dict[str, tuple[str, bytes]] = {}
        self._lock = threading.Lock()
        self._project_locks: dict[str, threading.Lock] = {}

    def add_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Command must be an object")
        command = validate_command({key: payload[key] for key in _COMMAND_FIELDS if key in payload})
        record = {
            "id": str(uuid.uuid4()),
            "project_id": command.project_id,
            "operation": command.operation.value,
            "inputs": command.inputs,
            "job_id": command.job_id,
            "expected_revision": command.expected_revision,
            "status": "queued",
            "requested_by": "local@localhost",
            "created_at": {"seconds": int(time.time()), "nanoseconds": 0},
            "output": "",
        }
        with self._lock:
            self.commands.insert(0, record)
            # Keep last 50 commands
            if len(self.commands) > 50:
                self.commands = self.commands[:50]

        threading.Thread(target=self._execute, args=(record,), daemon=True).start()
        return record

    def cancel_command(self, command_id: str) -> dict[str, Any]:
        with self._lock:
            record = next((item for item in self.commands if item["id"] == command_id), None)
            if record is None:
                raise LookupError("Command not found")
            if record["status"] != "queued":
                raise ValueError(f"Only queued commands can be cancelled (this one is {record['status']})")
            record["status"] = "cancelled"
            return record

    def store_artifact(self, artifact_id: str, filename: str, data: bytes) -> None:
        with self._lock:
            self.artifacts[artifact_id] = (filename, data)

    def _execute(self, record: dict[str, Any]) -> None:
        # Run one command per project at a time; waiting commands stay cancellable.
        with self._lock:
            project_lock = self._project_locks.setdefault(record["project_id"], threading.Lock())
        with project_lock:
            with self._lock:
                if record["status"] != "queued":
                    return
                record["status"] = "running"
            self._run(record)

    def _with_artifact(self, inputs: dict[str, Any], action: Any) -> Any:
        art_id = inputs.get("artifact_id")
        if not art_id:
            return action(inputs)
        with self._lock:
            artifact = self.artifacts.get(art_id)
        if artifact is None:
            raise ValueError("Uploaded file not found. Upload it again.")
        name, data = artifact
        with tempfile.TemporaryDirectory(prefix="orchestrator-spec-") as tmp:
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", name or "spec.md")[:128]
            src = Path(tmp) / safe_name
            src.write_bytes(data)
            return action({**inputs, "_trusted_source_file": str(src), "_trusted_file_name": name})

    def _run(self, record: dict[str, Any]) -> None:
        project_id = record["project_id"]
        op = record["operation"]
        inputs = record["inputs"]
        job_id = record.get("job_id")
        expected_rev = record.get("expected_revision")

        root = self.projects.get(project_id)
        if not root or not root.is_dir():
            record["status"] = "failed"
            record["error"] = f"Project '{project_id}' not found on local machine."
            return

        def progress_cb(text: str) -> None:
            record["output"] = (record.get("output", "") + redact_text(text))[-20000:]

        try:
            if op == Operation.JOB_CREATE.value:
                job = self._with_artifact(inputs, lambda opts: create_job(root, opts))
                record["result"] = {"job_id": job["job_id"]}
                record["status"] = "succeeded"
            elif op.startswith("job."):
                def action(opts: dict[str, Any]) -> dict[str, Any]:
                    return run_job_action(root, job_id, Operation(op), opts, expected_rev)
                job = self._with_artifact(inputs, action) if op == Operation.JOB_ATTACH_REFERENCE.value else action(inputs)
                res = {"job_id": job["job_id"], "revision": job["revision"]}
                if "_web_result" in job:
                    res.update(job["_web_result"])
                record["result"] = res
                record["status"] = "succeeded"
            elif op == Operation.TESTS_RUN.value:
                res = run_app_tests(root, inputs, progress=progress_cb)
                record["result"] = redact_data(res)
                record["status"] = "failed" if res.get("exit_code", 0) != 0 else "succeeded"
            elif op == Operation.PROJECT_REFRESH.value:
                res = run_refresh(root)
                record["result"] = redact_data(res)
                record["status"] = "succeeded"
            elif op == Operation.TOOL_PROJECT.value and inputs.get("action") == "add":
                new_id = str(inputs.get("project_id", "")).strip()
                raw_path = str(inputs.get("path", "")).strip()
                if not _NEW_PROJECT_ID.fullmatch(new_id):
                    raise ValueError("Project ID must contain only letters, numbers, dashes, and underscores (1-64 chars)")
                new_path = Path(raw_path).expanduser().resolve()
                if not new_path.is_dir():
                    raise ValueError(f"Directory not found: {raw_path}")
                self.projects[new_id] = new_path
                record["result"] = {"registered": new_id, "path": str(new_path), "projects": sorted(self.projects.keys())}
                record["status"] = "succeeded"
            else:
                res = run_tool(root, Operation(op), inputs, progress=progress_cb)
                record["result"] = redact_data(res)
                record["status"] = "failed" if isinstance(res, dict) and res.get("exit_code", 0) != 0 else "succeeded"
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = redact_text(str(exc))


_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class LocalUIHandler(BaseHTTPRequestHandler):
    runner: LocalCommandRunner
    static_dir: Path
    api_token: str

    def log_message(self, format: str, *args: Any) -> None:
        # Silent logging for clean terminal
        pass

    def _client_is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def _has_valid_token(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.api_token}"
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _api_allowed(self) -> bool:
        """Reject other sites, and require the token outside direct loopback use.

        The Host check stops DNS rebinding: a hostile domain resolving to
        127.0.0.1 connects from loopback but cannot present a loopback Host.
        """
        host = self.headers.get("Host", "").lower()
        origin = self.headers.get("Origin")
        try:
            if origin is not None and urllib.parse.urlsplit(origin).netloc.lower() != host:
                self._send_json({"error": "Cross-origin request refused"}, status=HTTPStatus.FORBIDDEN)
                return False
            hostname = urllib.parse.urlsplit(f"//{host}").hostname
        except ValueError:
            hostname = None
        if (self._client_is_loopback() and hostname in _LOOPBACK_HOSTS) or self._has_valid_token():
            return True
        self._send_json({"error": "Access token required"}, status=HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/") and not self._api_allowed():
            return

        # API Endpoints
        if path == "/api/worker":
            test_plans: dict[str, list[str]] = {}
            for pid, root in self.runner.projects.items():
                try:
                    cfg = _read(root / ".orchestrator" / "project.json", {})
                    target = cfg.get("test_target")
                    safe_target = target if isinstance(target, str) and _NAME.fullmatch(target) else None
                    plans = _find_test_plans(root, safe_target)
                    test_plans[pid] = [p.name for p in plans]
                except Exception:
                    test_plans[pid] = []

            data = {
                "project_ids": sorted(self.runner.projects.keys()),
                "last_seen": {"seconds": int(time.time()), "nanoseconds": 0},
                "test_plans": test_plans,
                "local_mode": True,
            }
            self._send_json(data)
            return

        if path == "/api/commands":
            self._send_json(self.runner.commands)
            return

        # /api/projects/<pid>/jobs
        m_jobs = re.match(r"^/api/projects/([^/]+)/jobs$", path)
        if m_jobs:
            pid = urllib.parse.unquote(m_jobs.group(1))
            root = self.runner.projects.get(pid)
            if root and root.is_dir():
                jobs = [project_job(j) for j in list_jobs(root)]
                self._send_json(jobs)
            else:
                self._send_json([])
            return

        # /api/projects/<pid>/archives
        m_arch = re.match(r"^/api/projects/([^/]+)/archives$", path)
        if m_arch:
            pid = urllib.parse.unquote(m_arch.group(1))
            root = self.runner.projects.get(pid)
            if root and root.is_dir():
                archs = [project_job(j) for j in list_archived_jobs(root)]
                self._send_json(archs)
            else:
                self._send_json([])
            return

        # Static assets
        self._serve_static(path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/") and not self._api_allowed():
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > _MAX_BODY_BYTES:
            self._send_json({"error": "Request is too large"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        body = self.rfile.read(length) if length > 0 else b""

        if path == "/api/commands":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
                record = self.runner.add_command(payload)
                self._send_json(record, status=HTTPStatus.CREATED)
            except Exception as e:
                self._send_json({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
            return

        m_cancel = re.match(r"^/api/commands/([^/]+)/cancel$", path)
        if m_cancel:
            try:
                self._send_json(self.runner.cancel_command(urllib.parse.unquote(m_cancel.group(1))))
            except LookupError as e:
                self._send_json({"error": str(e)}, status=HTTPStatus.NOT_FOUND)
            except ValueError as e:
                self._send_json({"error": str(e)}, status=HTTPStatus.CONFLICT)
            return

        if path.startswith("/api/projects/") and "/artifacts" in path:
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
                art_id = payload.get("artifact_id") or payload.get("id") or str(uuid.uuid4())
                if not isinstance(art_id, str) or not _ARTIFACT_ID.fullmatch(art_id):
                    raise ValueError("Invalid artifact ID")
                filename = str(payload.get("name", "spec.md"))
                raw_data = payload.get("data", "")
                data_bytes = base64.b64decode(raw_data, validate=True) if raw_data else b""
                if len(data_bytes) > _MAX_ARTIFACT_BYTES:
                    raise ValueError("Uploaded file is larger than 10 MB")
                self.runner.store_artifact(art_id, filename, data_bytes)
                self._send_json({"id": art_id}, status=HTTPStatus.CREATED)
            except Exception as e:
                self._send_json({"error": str(e)}, status=HTTPStatus.BAD_REQUEST)
            return

        self._send_json({"error": "Endpoint not found"}, status=HTTPStatus.NOT_FOUND)

    def _serve_static(self, path: str) -> None:
        if path == "/" or not path:
            target = self.static_dir / "index.html"
        else:
            rel = path.lstrip("/")
            target = (self.static_dir / rel).resolve()
            if not target.is_relative_to(self.static_dir.resolve()):
                self.send_response(HTTPStatus.NOT_FOUND)
                self.end_headers()
                self.wfile.write(b"404 Not Found")
                return
            if not target.is_file():
                # SPA Fallback
                target = self.static_dir / "index.html"

        if not target.is_file():
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            self.wfile.write(b"404 Not Found")
            return

        mime, _ = mimetypes.guess_type(str(target))
        if target.name.endswith(".js") or target.name.endswith(".mjs"):
            mime = "text/javascript"
        elif target.name.endswith(".css"):
            mime = "text/css"
        elif target.name.endswith(".html"):
            mime = "text/html"

        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache" if target.name.endswith(".html") else "max-age=31536000")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


import socket


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_tailscale_ip() -> str | None:
    try:
        ts = shutil.which("tailscale")
        if ts:
            res = subprocess.run([ts, "ip", "-4"], capture_output=True, text=True, timeout=2, check=False)
            if res.returncode == 0:
                ip = res.stdout.strip().split("\n")[0].strip()
                if ip:
                    return ip
    except Exception:
        pass
    return None


_BACKGROUND_SERVER: tuple[HTTPServer, str, str | None, str | None] | None = None


class LocalHTTPServer(HTTPServer):
    def server_bind(self) -> None:
        # HTTPServer's reverse DNS lookup of the host can stall startup for over 30 seconds.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_handler(projects: dict[str, Path], static_dir: Path, api_token: str) -> type[LocalUIHandler]:
    class CustomHandler(LocalUIHandler):
        pass

    CustomHandler.runner = LocalCommandRunner(projects)
    CustomHandler.static_dir = static_dir
    CustomHandler.api_token = api_token
    return CustomHandler


def server_urls(host: str, port: int, api_token: str) -> tuple[str, str | None, str | None]:
    """Return local, LAN and Tailscale URLs; only network URLs carry the token."""
    local_url = f"http://127.0.0.1:{port}"
    if host in _LOOPBACK_HOSTS:
        return local_url, None, None
    query = urllib.parse.urlencode({"token": api_token})
    if host != "0.0.0.0":
        return local_url, f"http://{host}:{port}/?{query}", None
    ts_ip = get_tailscale_ip()
    ts_url = f"http://{ts_ip}:{port}/?{query}" if ts_ip else None
    return local_url, f"http://{get_lan_ip()}:{port}/?{query}", ts_url


def get_or_start_background_server(host: str = "127.0.0.1", port: int = 4040, project_root: Path | None = None) -> tuple[str, str | None, str | None]:
    global _BACKGROUND_SERVER
    if _BACKGROUND_SERVER is not None:
        return _BACKGROUND_SERVER[1], _BACKGROUND_SERVER[2], _BACKGROUND_SERVER[3]

    api_token = secrets.token_urlsafe(32)
    handler = make_handler(discover_local_projects(project_root), get_static_dir(), api_token)

    for test_port in range(port, port + 20):
        try:
            httpd = LocalHTTPServer((host, test_port), handler)
            local_url, network_url, ts_url = server_urls(host, test_port, api_token)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            _BACKGROUND_SERVER = (httpd, local_url, network_url, ts_url)
            return local_url, network_url, ts_url
        except OSError as e:
            if e.errno == 48:
                continue
            raise
    raise RuntimeError(f"Could not bind local UI server on ports {port}-{port+20}")


def start_local_ui_server(
    host: str = "127.0.0.1",
    port: int = 4040,
    open_browser: bool = True,
    project_root: Path | None = None
) -> None:
    projects = discover_local_projects(project_root)
    api_token = secrets.token_urlsafe(32)
    handler = make_handler(projects, get_static_dir(), api_token)

    server_address = (host, port)
    try:
        httpd = LocalHTTPServer(server_address, handler)
    except OSError as e:
        if e.errno == 48:  # Address already in use
            alt_port = port + 1
            print(f"\033[93mPort {port} in use, trying port {alt_port}...\033[0m")
            server_address = (host, alt_port)
            httpd = LocalHTTPServer(server_address, handler)
            port = alt_port
        else:
            raise

    local_url, network_url, ts_url = server_urls(host, port, api_token)

    print("\n" + "=" * 64)
    print(f"\033[1;96m  ✨ Orchestrator Web Control Room\033[0m")
    print("=" * 64)
    print(f"  \033[1;92m● Local:    \033[0m  \033[4;94m{local_url}\033[0m")
    if network_url:
        print(f"  \033[1;92m● Network:  \033[0m  \033[4;94m{network_url}\033[0m  \033[90m(LAN)\033[0m")
    if ts_url:
        print(f"  \033[1;92m● Tailscale:\033[0m  \033[4;94m{ts_url}\033[0m  \033[90m(Accessible from anywhere on your Tailscale network)\033[0m")
    if network_url:
        print("  \033[93m● Network links contain an access token that gives full control. Don't share them.\033[0m")
    else:
        print("  \033[90m● Other devices: \033[0m  run with --host 0.0.0.0 to get a secure link")
    print(f"  \033[90m● Projects: \033[0m  {', '.join(projects.keys())}")
    print(f"  \033[90m● Mode:     \033[0m  Zero-Setup Local Engine (No Firebase required)")
    print("-" * 64)
    print(f"  \033[93mPress Ctrl+C to stop the local UI server.\033[0m\n")

    if open_browser and os.environ.get("SSH_CLIENT") is None and os.environ.get("SSH_TTY") is None:
        threading.Thread(target=lambda: (time.sleep(0.3), webbrowser.open(local_url)), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n\033[93mStopping local UI server...\033[0m")
        httpd.server_close()
