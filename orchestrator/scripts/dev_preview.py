#!/usr/bin/env python3
"""Run a web project's dev server on this computer, so a branch can be previewed live while it changes.

It runs where the branch is checked out (the project folder, or another worktree of it); otherwise it checks the branch out
in a preview folder outside the project (so the project's tests and file watchers don't see it) and keeps it, so the next
start is quick. Installing packages never writes a lockfile into the project.

The job page reaches the dev server through a small proxy here, which adds preview-helper.js to each page so you can pick
elements on it to point out a problem. Everything else, live reloading's connections included, passes through unchanged.
The proxy's address is saved in .orchestrator/output/dev-previews/ for the job page to show; it's removed again when the
server stops.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

from orchestrator.scripts.web_preview import channel_name, git

ADDRESS = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):\d+[^\s\x1b'\"]*")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
HELPER = Path(__file__).resolve().parents[1] / "web" / "static" / "preview-helper.js"
HELPER_PATH = "/__orchestrator/preview-helper.js"
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-connection", "te", "trailer", "content-length"}
# Dropped from pages only: either would stop the job page showing the preview in its frame or adding the helper to it.
PAGE_ONLY = {"content-security-policy", "x-frame-options"}


def add_helper(html: bytes) -> bytes:
    """The page with Orchestrator's helper script added, as early as possible so it's there before the app runs."""
    tag = f'<script src="{HELPER_PATH}"></script>'.encode()
    found = re.search(rb"<head[^>]*>", html, re.I)
    if found:
        return html[:found.end()] + tag + html[found.end():]
    return tag + html


def proxy_handler(host: str, port: int) -> type[BaseHTTPRequestHandler]:
    class Proxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_: Any) -> None:
            pass

        def handle_one_request(self) -> None:  # every method goes to the same place
            try:
                self.raw_requestline = self.rfile.readline(65537)
                if not self.raw_requestline or not self.parse_request():
                    self.close_connection = True
                    return
                self.forward()
            except (OSError, http.client.HTTPException):
                self.close_connection = True

        def forward(self) -> None:
            if self.path == HELPER_PATH:
                body = HELPER.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.headers.get("Upgrade"):
                self.tunnel()
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP and k.lower() not in ("accept-encoding", "host")}
            headers["Host"] = f"{host}:{port}"  # as a reverse proxy does: apps that check who they're reached as see their own address
            upstream = http.client.HTTPConnection(host, port, timeout=300)
            upstream.request(self.command, self.path, body=body, headers=headers)
            reply = upstream.getresponse()
            html = "text/html" in (reply.getheader("Content-Type") or "") and not reply.getheader("Content-Encoding")
            self.send_response(reply.status, reply.reason)
            for name, value in reply.getheaders():
                if name.lower() not in HOP_BY_HOP and not (html and name.lower() in PAGE_ONLY):
                    self.send_header(name, value)
            if html:
                data = add_helper(reply.read())
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:  # streamed as it comes (live reload uses long-lived responses)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while chunk := reply.read1(65536) if self.command != "HEAD" else b"":
                    self.wfile.write(chunk)
                    self.wfile.flush()
            upstream.close()

        def tunnel(self) -> None:
            """A WebSocket (live reload): the request is passed on as it came, then bytes flow both ways untouched."""
            upstream = socket.create_connection((host, port), timeout=30)
            lines = [self.requestline] + [f"{k}: {v}" for k, v in self.headers.items()]
            upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            upstream.settimeout(None)
            client = self.connection
            client.settimeout(None)
            # Nothing is waiting unread: a WebSocket client sends nothing more until it has the server's answer.
            self.close_connection = True
            try:
                while True:
                    ready, _, _ = select.select([client, upstream], [], [], 600)
                    if not ready:
                        break
                    for side in ready:
                        data = side.recv(65536)
                        if not data:
                            return
                        (upstream if side is client else client).sendall(data)
            finally:
                upstream.close()

    return Proxy


def start_proxy(target: str) -> str:
    """Start the proxy in front of the dev server at `target`; returns the proxy's address."""
    parts = urlsplit(target)
    host = "127.0.0.1" if parts.hostname in ("localhost", "0.0.0.0", None) else parts.hostname.strip("[]")
    server = ThreadingHTTPServer(("127.0.0.1", 0), proxy_handler(host, parts.port or 80))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="preview-proxy").start()
    return f"http://localhost:{server.server_address[1]}{parts.path or '/'}{'?' + parts.query if parts.query else ''}"


def checked_out_in(root: Path, branch: str) -> Path | None:
    """The folder where the branch is checked out, if any (git keeps a branch in one place at a time)."""
    path = None
    for line in git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


# Folders a site's pages usually live in, when it has no build of its own: the first with an index.html is served.
SITE_FOLDERS = (".", "public", "dist", "build", "site", "www", "static", "docs")
NOT_FOUND = ("Couldn't tell how to run this web app: it has no package.json, Django manage.py or index.html. "
             "Enter the address of the app if it's already running (Preview an app at…), or set preview_dev_command "
             "in .orchestrator/project.json.")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def is_orchestrator(folder: Path) -> bool:
    """Orchestrator's own code (a branch of it being worked on): its web app is `orchestrator ui`."""
    return (folder / "orchestrator" / "web" / "server.py").is_file() and (folder / "orchestrator" / "__main__.py").is_file()


def preview_project(folder: Path) -> Path:
    """A small project of its own for a previewed copy of Orchestrator to show, so it never runs anything in a real one."""
    sample = folder.parent / f"{folder.name}-preview-project"
    if not (sample / ".orchestrator" / "project.json").is_file():
        (sample / ".orchestrator").mkdir(parents=True, exist_ok=True)
        (sample / ".orchestrator" / "project.json").write_text(json.dumps(
            {"name": "Preview", "project_kind": "web", "build_command": "true", "test_command": "true"}))
        subprocess.run(["git", "init", "-q"], cwd=sample, capture_output=True)
    return sample


def dev_env(folder: Path) -> dict[str, str]:
    """Extra environment for the dev server. A previewed Orchestrator runs this folder's code, with state of its own (one
    Orchestrator per user otherwise, and it must not touch the real one's settings) and the same access token, so the
    browser's sign-in cookie for this computer works for both."""
    if not is_orchestrator(folder):
        return {}
    token = ""
    try:
        token = (Path(os.environ.get("ORCHESTRATOR_USER_STATE_DIR") or Path.home() / ".orchestrator") / "ui_token").read_text().strip()
    except OSError:
        pass
    return {"PYTHONPATH": str(folder), "ORCHESTRATOR_USER_STATE_DIR": str(folder.parent / f"{folder.name}-preview-state"),
            **({"ORCHESTRATOR_PREVIEW_TOKEN": token} if token else {})}


def dev_commands(folder: Path, config: dict[str, Any]) -> tuple[list[list[str]], list[str]]:
    """(setup, server): installing packages when they're missing, then `preview_dev_command` from project.json, else the
    package's dev or start script, else Orchestrator's own app, a Django app, or a plain file server for a site without a
    build. Nothing recognisable is an error that says what to do, never a listing of the project's files."""
    if config.get("preview_dev_command"):
        return [], ["/bin/sh", "-c", str(config["preview_dev_command"])]
    try:
        scripts = json.loads((folder / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        scripts = None
    if scripts is None:
        if is_orchestrator(folder):
            token = dev_env(folder).get("ORCHESTRATOR_PREVIEW_TOKEN")
            return [], [sys.executable, "-P", "-u", "-m", "orchestrator", "ui", "--no-open", "--port", str(free_port()),
                        "--project", str(preview_project(folder)), *(["--token", token] if token else [])]
        if (folder / "manage.py").is_file():
            return [], [sys.executable, "-u", "manage.py", "runserver", f"127.0.0.1:{free_port()}"]
        site = next((folder / d for d in SITE_FOLDERS if (folder / d / "index.html").is_file()), None)
        if site is None:
            raise ValueError(NOT_FOUND)
        return [], [sys.executable, "-u", "-m", "http.server", "--bind", "127.0.0.1", "--directory", str(site), "0"]
    # The project's own package manager, from its lockfile; without one, npm is told not to write one.
    if (folder / "pnpm-lock.yaml").is_file():
        install, run = ["pnpm", "install", "--frozen-lockfile"], ["pnpm", "run"]
    elif (folder / "yarn.lock").is_file():
        install, run = ["yarn", "install", "--frozen-lockfile"], ["yarn", "run"]
    elif (folder / "package-lock.json").is_file():
        install, run = ["npm", "ci"], ["npm", "run"]
    else:
        install, run = ["npm", "install", "--no-package-lock"], ["npm", "run"]
    setup = [] if (folder / "node_modules").is_dir() else [install]
    script = next((s for s in ("dev", "start", "serve") if s in scripts), None)
    if not script:
        raise ValueError("package.json has no dev, start or serve script. Set preview_dev_command in .orchestrator/project.json.")
    return setup, [*run, script]


def copy_folder(root: Path, slug: str) -> Path:
    """Where a branch that isn't checked out runs from: a folder outside the project, one per project and branch."""
    project = f"{root.name}-{hashlib.sha1(str(root).encode()).hexdigest()[:8]}"
    return Path(tempfile.gettempdir()) / "orchestrator-dev-preview" / project / slug


def address(line: str) -> str:
    """The local address a dev server prints ("Local: http://localhost:5173/"), as a link that opens in a browser."""
    found = ADDRESS.search(ANSI.sub("", line))
    return found.group(0).replace("0.0.0.0", "localhost").rstrip(".,)") if found else ""


LOCAL_APP = re.compile(r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?(?:/[^\s]*)?", re.I)


def show_running_app(root: Path, branch: str, slug: str, url: str) -> int:
    """Preview an app that's already running on this computer (preview_url in project.json) instead of starting one: the
    person runs it their own way, and the preview shows whatever it serves."""
    if not LOCAL_APP.fullmatch(url):
        print(f"❌ {url} isn't an address on this computer. Use one like http://localhost:3000.")
        return 1
    state = root / ".orchestrator" / "output" / "dev-previews" / f"{slug}.json"
    try:
        shown = start_proxy(url)
    except OSError as exc:
        print(f"(Couldn't start the preview helper: {exc}. Picking elements is off.)")
        shown = url
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"branch": branch, "url": shown, "direct": url, "pid": os.getpid(), "at": time.time()}))
    print(f"Showing the app already running at {url}, as set in .orchestrator/project.json (preview_url).")
    print(f"✅ Live preview: {shown} (the app itself: {url})", flush=True)
    try:
        while not stopped.wait(1):
            pass
        return 0
    finally:
        state.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--root", default=os.environ.get("ORCHESTRATOR_PROJECT_ROOT") or os.getcwd())
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        config = json.loads((root / ".orchestrator" / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    if args.branch not in git(root, "branch", "--format=%(refname:short)").splitlines():
        print(f"❌ There's no branch called {args.branch} in this repository.")
        return 1

    slug = channel_name(args.branch)
    if config.get("preview_url"):
        return show_running_app(root, args.branch, slug, str(config["preview_url"]))
    folder = checked_out_in(root, args.branch)
    if folder is None:
        folder = copy_folder(root, slug)
        if not (folder / ".git").exists():
            folder.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True)  # a copy the computer cleared away
            subprocess.run(["git", "worktree", "add", "--detach", str(folder), args.branch], cwd=root, check=True,
                           stdout=subprocess.DEVNULL)
        else:  # the preview folder from last time: bring it up to the branch's latest commit
            subprocess.run(["git", "checkout", "--detach", "--force", args.branch], cwd=folder, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"{args.branch} isn't checked out here, so it runs from a copy at its latest commit. Changes a job makes "
              "show up after you start the preview again.")
    else:
        print(f"Running {args.branch} from {folder}. Changes show up as they're saved.")

    try:
        setup, server = dev_commands(folder, config)
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1
    for command in setup:
        print(f"$ {' '.join(command)}", flush=True)
        if subprocess.run(command, cwd=folder).returncode != 0:
            print(f"❌ {' '.join(command)} failed. Fix the error above, or set preview_dev_command in .orchestrator/project.json.")
            return 1

    state = root / ".orchestrator" / "output" / "dev-previews" / f"{slug}.json"
    print(f"$ {' '.join(server)}", flush=True)
    child = subprocess.Popen(server, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                             start_new_session=True, env={**os.environ, "BROWSER": "none", "FORCE_COLOR": "0", **dev_env(folder)})

    def stop(*_: Any) -> None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except OSError:
            pass

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    try:
        found = ""
        for line in child.stdout or []:
            print(line, end="", flush=True)
            if not found:
                found = address(line)
                if not found and "Serving HTTP on" in line:  # python's file server: "Serving HTTP on 127.0.0.1 port 54321"
                    port = re.search(r"port (\d+)", line)
                    found = f"http://localhost:{port.group(1)}/" if port else ""
                if found:
                    try:
                        shown = start_proxy(found)
                    except OSError as exc:  # the preview still works, just without picking elements
                        print(f"(Couldn't start the preview helper: {exc}. Picking elements is off.)")
                        shown = found
                    state.parent.mkdir(parents=True, exist_ok=True)
                    state.write_text(json.dumps({"branch": args.branch, "url": shown, "direct": found, "pid": os.getpid(),
                                                 "at": time.time()}))
                    print(f"✅ Live preview: {shown} (the dev server itself: {found})", flush=True)
        return child.wait()
    except KeyboardInterrupt:
        stop()
        return 130
    finally:
        stop()
        state.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
