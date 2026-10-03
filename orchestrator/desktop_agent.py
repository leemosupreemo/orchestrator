"""Desktop-owned runner. Native administration never travels over the tunnel."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from orchestrator import account, mac_readiness
from orchestrator.desktop_diagnostics import CHECK_NAMES, diagnostics_snapshot
from orchestrator.desktop_migration import inspect_legacy, migrate_legacy
from orchestrator.desktop_protocol import MAX_MESSAGE, ProtocolError, peer_uid, validate_request
from orchestrator.project_config import active_project_root, user_state_dir
from orchestrator.project_setup import _atomic_json
from orchestrator.runtime_control import BusyError, InstanceLease
from orchestrator.web.bootstrap import project_ready
from orchestrator.web.server import UIServer, start_tunnel


def _stop_tunnel(proc):
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
            proc.wait(timeout=3)


def _open_app():
    """Starts the menu app hidden, so it can install an update the hosted app asked for."""
    bundle = os.environ.get("ORCHESTRATOR_PACKAGED_APP")
    if bundle:
        subprocess.run(["/usr/bin/open", "-g", "-j", "-a", bundle], capture_output=True, timeout=30)


@dataclass
class AgentDependencies:
    pair_start: Callable = account.start_pairing
    pair_poll: Callable = account.poll_pairing
    heartbeat: Callable = account.heartbeat
    tunnel_start: Callable = start_tunnel
    tunnel_stop: Callable = _stop_tunnel
    legacy_inspect: Callable = inspect_legacy
    legacy_migrate: Callable = migrate_legacy
    open_app: Callable = _open_app
    readiness: Callable = lambda control_dir: mac_readiness.check(control_dir=control_dir)


def control_socket_path(control_dir: Path) -> Path:
    path = control_dir / "agent.sock"
    if len(os.fsencode(path)) >= 104:
        raise ValueError("The desktop control path is too long. Choose a shorter application-support path.")
    return path


class ControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        request_id = ""
        try:
            raw = self.rfile.readline(MAX_MESSAGE + 1)
            if peer_uid(self.request) != os.getuid():
                raise ProtocolError("Only this Mac user can control the agent.", "unauthorized")
            request = validate_request(raw)
            request_id = request["request_id"]
            result = self.server.agent.handle(request["command"], request["params"])
            reply = {"version": 1, "request_id": request_id, "ok": True, "result": result}
        except (ProtocolError, BusyError, ValueError) as exc:
            reply = {"version": 1, "request_id": request_id, "ok": False,
                     "error": {"code": getattr(exc, "code", "busy" if isinstance(exc, BusyError) else "invalid_request"),
                               "message": str(exc)}}
        except (OSError, TimeoutError):
            return
        except Exception:
            reply = {"version": 1, "request_id": request_id, "ok": False,
                     "error": {"code": "operation_failed", "message": "The operation failed. Try again or open Diagnostics."}}
        try:
            self.wfile.write(json.dumps(reply).encode() + b"\n")
        except OSError:
            pass


class DesktopAgent:
    def __init__(self, state_dir: Path, control_dir: Path, runtime: dict[str, str], dependencies: AgentDependencies | None = None):
        self.state_dir, self.control_dir, self.runtime = state_dir, control_dir, runtime
        self.dependencies = dependencies or AgentDependencies()
        self.stop_event = threading.Event()
        self.local = self.remote = None
        self.preferred_port = 8765
        self._lock = threading.RLock()
        self._pair_cancel = threading.Event()
        self._pairing = {"state": "not_connected"}
        self._pair_thread = None
        self._remote_thread = None
        self._tunnel_proc = None
        self._endpoint = ""
        self._remote_state = "connecting"
        self._remote_generation = 0
        self._retry_at = self._beat_at = 0.0
        self._beat_thread = None
        self._last_error = ""
        self._update = "current"
        self._legacy = {"state": "none", "manual_server": False}
        self._legacy_at = 0.0
        self._update_request = False   # the hosted app asked this Mac to update; the menu app carries it out
        self._update_state = ""         # what the menu app last reported, sent with the heartbeat
        self._status_at = 0.0           # when a client (normally the menu app) last asked for status
        self._open_app_at = 0.0
        self._readiness = {}
        self._readiness_at = 0.0
        self._readiness_thread = None
        try:
            preferences = json.loads((state_dir / "desktop.json").read_text())
            self.remote_enabled = preferences.get("remote_enabled") is not False
        except (OSError, ValueError, AttributeError):
            self.remote_enabled = True

    def _socket(self):
        path = control_socket_path(self.control_dir)
        if self.control_dir.is_symlink():
            raise ValueError("Unsafe control directory.")
        self.control_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.control_dir.stat()
        if info.st_uid != os.getuid():
            raise ValueError("The control directory belongs to another user.")
        self.control_dir.chmod(0o700)
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("Unsafe existing control socket. No files were removed.")
            with socket.socket(socket.AF_UNIX) as check:
                check.settimeout(.2)
                try:
                    check.connect(str(path))
                except ConnectionRefusedError:
                    path.unlink()
                else:
                    raise BusyError("The desktop control socket is already in use.")
        control = ControlServer(str(path), ControlHandler)
        path.chmod(0o600)
        control.agent = self
        return control

    def run(self) -> int:
        os.environ.update(self.runtime)
        os.environ["ORCHESTRATOR_USER_STATE_DIR"] = str(self.state_dir)
        lease = InstanceLease.acquire(self.state_dir / "ui-instance.lock")
        control = None
        servers, threads = [], []
        try:
            control = self._socket()
            root = active_project_root()
            root = root if project_ready(root) else None
            try:
                self.local = UIServer(("127.0.0.1", self.preferred_port), root, bootstrap_mode=True, desktop_listener=True)
            except OSError:
                self.local = UIServer(("127.0.0.1", 0), root, bootstrap_mode=True, desktop_listener=True)
            self.remote = UIServer(("127.0.0.1", 0), root, shared=self.local)
            self.remote.accepting_requests = False
            servers = [self.local, self.remote, control]
            for server in servers:
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
                thread.start()
                threads.append(thread)
            self.local.start_notifier()
            while not self.stop_event.wait(.1):
                self._reconcile()
            return 0
        finally:
            self.stop_event.set()
            self._pair_cancel.set()
            if self.local:
                self.local._stopping.set()
            with self._lock:
                self._remote_generation += 1
                self._stop_remote()
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(2)
            if control:
                path = control_socket_path(self.control_dir)
                if path.exists() and stat.S_ISSOCK(path.lstat().st_mode):
                    path.unlink()
                if not servers:
                    control.server_close()
            for worker in (self._pair_thread, self._remote_thread, self._beat_thread, self._readiness_thread):
                if worker:
                    worker.join(20)
            lease.close()

    def status(self) -> dict:
        with self._lock:
            machine = account.load_machine()
            root = self.local.root if self.local else None
            pairing = dict(self._pairing)
            if machine:
                pairing = {"state": "connected", "owner_email": machine.get("owner_email", "")}
            runs = sum(bool(run["running"]) for run in self.local.sessions.list()) if self.local else 0
            active = self.local.gate.snapshot()["active"] if self.local else 0
            legacy_running = self._legacy["state"] == "running"
            return {"agent": "running" if self.local else "starting", "setup": "needs_project" if not root else "ready" if machine else "needs_pairing",
                    "local_interface": "ready" if self.local else "starting", "local_origin": self.local.base_url.rstrip("/") if self.local else "",
                    "remote_access": "off" if not self.remote_enabled else "waiting_for_legacy" if legacy_running else self._remote_state,
                    "remote_enabled": self.remote_enabled, "legacy": self._legacy["state"], "manual_server": self._legacy["manual_server"],
                    "activity": {"runs": runs, "tasks": max(0, active - runs)},
                    "project": {"name": root.name, "root": str(root)} if root else None, "pairing": pairing,
                    "runner": {"version": account.package_version(), "api_version": account.API_VERSION}, "update": self._update,
                    "update_request": self._update_request, "readiness": dict(self._readiness)}

    def handle(self, command: str, params: dict) -> dict:
        validate_request(json.dumps({"version": 1, "request_id": "internal", "command": command, "params": params}).encode())
        if command == "status":
            self._status_at = time.monotonic()
            return self.status()
        if command == "update_progress":
            with self._lock:
                self._update_state = params["state"]
                self._update_request = False
                self._beat_at = 0  # tell the hosted app soon
            return {"accepted": True}
        if command == "diagnostics":
            path = os.environ.get("PATH", "")
            checks = [{"name": name, "state": "found" if shutil.which(name, path=path) else "missing"} for name in sorted(CHECK_NAMES)]
            return {**diagnostics_snapshot({**self.status(), "last_error": self._last_error}, checks),
                    "readiness": mac_readiness.clean(self._readiness)}
        if command == "legacy_status":
            self._check_legacy()
            return dict(self._legacy)
        if command == "legacy_migrate":
            result = self.dependencies.legacy_migrate(self.state_dir, params["consent"])
            self._check_legacy()
            return {**result, "manual_server": self._legacy["manual_server"]}
        if command in ("prepare_update", "stop_if_idle"):
            accepted = self.local.gate.prepare("update" if command == "prepare_update" else "stop")
            if accepted:
                if command == "stop_if_idle":
                    self.stop_event.set()
                else:
                    self._update = "installing"
            return {"accepted": accepted, "reason": "" if accepted else "Work is still running."}
        if command == "cancel_update":
            self.local.gate.cancel()
            self._update = "current"
            return {"accepted": True, "reason": ""}
        if command == "browser_grant":
            route = params.get("route", "#/")
            if not self.local.root and route in ("#/", "#/projects"):
                route = "#/setup"
            grant = self.local.browser_grants.issue(route)
            return {"url": f"{self.local.base_url}desktop/open?grant={grant}"}
        if command == "pair_cancel":
            with self._lock:
                self._pair_cancel.set()
                self._pairing = {"state": "cancelled"}
            return {"accepted": True}
        if command == "pair_start":
            with self._lock:
                if self._pair_thread and self._pair_thread.is_alive():
                    raise BusyError("A pairing attempt is already running. Cancel it or wait for it to finish.")
                release = self.local.gate.hold()
                self._pair_cancel = threading.Event()
                self._pairing = {"state": "starting"}
                self._pair_thread = threading.Thread(target=self._pair, args=(params.get("name"), release), daemon=True)
                self._pair_thread.start()
            return {"accepted": True}
        if command == "remote_access":
            with self.local.gate.admit(), self._lock:
                self.remote_enabled = params["enabled"]
                _atomic_json(self.state_dir / "desktop.json", {"remote_enabled": self.remote_enabled})
                self._remote_generation += 1
                if not self.remote_enabled:
                    self._stop_remote()
                else:
                    self._retry_at = 0
                self._beat_at = 0
            return {"accepted": True}
        raise ProtocolError("Unknown command.")

    def _pair(self, name, release):
        try:
            pairing = self.dependencies.pair_start(name)
            deadline = time.monotonic() + min(int(pairing["expires_in"]), 900)
            with self._lock:
                if self._pair_cancel.is_set():
                    return
                self._pairing = {"state": "waiting", "code": account.format_code(pairing["code"]),
                                 "url": f"{account.HOSTED_APP_URL}/#/connect?code={pairing['code']}"}
            while not self._pair_cancel.wait(.2) and not self.stop_event.is_set():
                if time.monotonic() >= deadline:
                    self._pairing = {"state": "expired"}
                    return
                try:
                    claimed = self.dependencies.pair_poll(pairing)
                except account.AccountError as exc:
                    if exc.status in (404, 410):
                        self._pairing = {"state": "expired"}
                        return
                    self._pair_cancel.wait(3)
                    continue
                with self._lock:
                    if self._pair_cancel.is_set():
                        return
                    if claimed.get("status") == "claimed":
                        account.finish_pairing(pairing, claimed)
                        self.local.forget_allowed_emails()
                        self.remote.forget_allowed_emails()
                        self._pairing = {"state": "connected"}
                        self._beat_at = 0
                        return
                self._pair_cancel.wait(2.8)
        except Exception:
            self._pairing = {"state": "failed"}
            self._last_error = "pairing_failed"
        finally:
            release()

    def _stop_remote(self):
        if self.remote:
            self.remote.accepting_requests = False
        proc, self._tunnel_proc = self._tunnel_proc, None
        self._endpoint = ""
        self._remote_state = "off" if not self.remote_enabled else "connecting"
        self.dependencies.tunnel_stop(proc)

    def _start_remote(self, generation):
        proc = None
        try:
            proc, endpoint = self.dependencies.tunnel_start(self.remote.server_address[1])
            parsed = urlsplit(endpoint or "")
            with self._lock:
                if generation != self._remote_generation or not self.remote_enabled or not project_ready(self.local.root) or not account.load_machine() or self.stop_event.is_set():
                    self.dependencies.tunnel_stop(proc)
                    return
                self._endpoint = endpoint if parsed.scheme == "https" and parsed.netloc and not parsed.username else ""
                self._remote_state = "connected" if self._endpoint and proc and proc.poll() is None else "unavailable"
                if self._remote_state == "connected":
                    self._tunnel_proc = proc
                else:
                    self.dependencies.tunnel_stop(proc)
                    self._tunnel_proc = None
                    self._endpoint = ""
                self.remote.accepting_requests = self._remote_state == "connected"
                self._retry_at = time.monotonic() + 10
                self._beat_at = 0
        except Exception:
            self.dependencies.tunnel_stop(proc)
            self._retry_at = time.monotonic() + 30
            self._last_error = "tunnel_failed"

    def _check_legacy(self):
        try:
            found = self.dependencies.legacy_inspect(self.state_dir)
            found = {"state": found["state"], "manual_server": bool(found["manual_server"])}
        except Exception:
            found = {"state": "none", "manual_server": False}
        with self._lock:
            self._legacy = found
            self._legacy_at = time.monotonic() + 30

    def _check_readiness(self):
        try:
            found = mac_readiness.clean(self.dependencies.readiness(self.control_dir))
        except Exception:
            found = {}
        with self._lock:
            self._readiness = found
            self._beat_at = 0

    def _reconcile(self):
        if time.monotonic() >= self._legacy_at:
            self._check_legacy()
        if time.monotonic() >= self._readiness_at and not (self._readiness_thread and self._readiness_thread.is_alive()):
            self._readiness_at = time.monotonic() + 3600  # settings change rarely; xcodebuild is slow to ask
            self._readiness_thread = threading.Thread(target=self._check_readiness, daemon=True)
            self._readiness_thread.start()
        with self._lock:
            machine = account.load_machine()
            # An older `orchestrator service` still running owns this computer's address; two would overwrite each other.
            legacy_running = self._legacy["state"] == "running"
            if self.local.root and not project_ready(self.local.root):
                self.local.selected_root = self.local.root
                self.local.root = None
                self._remote_generation += 1
            if not self.remote_enabled or not self.local.root or not machine or legacy_running:
                if self._tunnel_proc or self._endpoint:
                    self._remote_generation += 1
                    self._stop_remote()
            elif (not self._remote_thread or not self._remote_thread.is_alive()) and time.monotonic() >= self._retry_at:
                if self._tunnel_proc and self._tunnel_proc.poll() is None:
                    self._retry_at = time.monotonic() + 10
                else:
                    self._stop_remote()
                    self._remote_state = "connecting"
                    self._remote_thread = threading.Thread(target=self._start_remote, args=(self._remote_generation,), daemon=True)
                    self._remote_thread.start()
            if machine and not legacy_running and time.monotonic() >= self._beat_at and (not self._beat_thread or not self._beat_thread.is_alive()):
                self._beat_at = time.monotonic() + account.HEARTBEAT_SECONDS
                endpoint = self._endpoint if self.remote_enabled else ""
                runs = sum(bool(run["running"]) for run in self.local.sessions.list())
                self._beat_thread = threading.Thread(target=self._heartbeat, args=(machine, endpoint, runs, self._update_state, dict(self._readiness)), daemon=True)
                self._beat_thread.start()
            # An update was asked for but no menu app has checked in for a minute (someone quit it): start it hidden.
            now = time.monotonic()
            if self._update_request and now - self._status_at > 60 and now >= self._open_app_at:
                self._open_app_at = now + 300
                threading.Thread(target=self._start_menu, daemon=True).start()

    def _heartbeat(self, machine, endpoint, runs, update_state, readiness):
        try:
            reply = self.dependencies.heartbeat(machine, endpoint, running=runs, update_state=update_state or None,
                                                readiness=readiness or None)
        except Exception:
            self._last_error = "heartbeat_failed"
            return
        if isinstance(reply, dict) and reply.get("update_requested") is True:
            with self._lock:
                self._update_request = True

    def _start_menu(self):
        try:
            self.dependencies.open_app()
        except Exception:
            self._last_error = "menu_start_failed"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-dir", type=Path, default=Path.home() / "Library/Application Support/Orchestrator/control")
    args = parser.parse_args(argv)
    runtime = {}
    if os.environ.get("ORCHESTRATOR_PACKAGED_APP"):
        from orchestrator.desktop_runtime import bundle_environment
        runtime = bundle_environment(Path(os.environ["ORCHESTRATOR_PACKAGED_APP"]), [])
    agent = DesktopAgent(user_state_dir(), args.control_dir, runtime)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: agent.stop_event.set())
    return agent.run()


if __name__ == "__main__":
    raise SystemExit(main())
