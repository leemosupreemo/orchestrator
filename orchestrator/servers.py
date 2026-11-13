"""The project's Docker servers: find them, say whether they are up, and start, stop or restart them.

A project that needs a server for its app or its tests usually has a compose file for it (`docker-compose.yml`, or
`compose.profile.yml` for a test copy). `find` reads those files without running anything. `status` asks Docker what is
actually running (`docker compose ps`, read-only). `start`, `stop` and `restart` run the matching compose command, after
making sure Docker itself is running, and `start` waits until every service is up and healthy.

It runs on this computer or on another one over SSH (a machine from machines.json: `ssh_target` and `repo_path`).

What it will not do: remove containers, networks or volumes (no `down`, no `-v`, no `rm`, no `prune`). `stop` stops,
`start` starts; the data stays. Only compose files found in the project can be named, so nothing typed in can become a
command.

The shell is injected, so everything here can be tested without Docker.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from orchestrator import test_commands

SKIP_DIRS = {".git", "node_modules", "bin", "obj", "Library", "Temp", "Logs", "PackageCache", ".venv", "venv", "build", "dist", ".orchestrator",
             "DerivedData", "Pods", "__pycache__", "TestResults", ".build", ".gradle", "target", "Carthage", ".worktrees", ".claude", ".local"}
COMPOSE_FILE = re.compile(r"^(?:docker-)?compose(?:[.-][\w.-]+)?\.ya?ml$", re.I)
DEFAULT_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
OVERRIDE_FILES = ("docker-compose.override.yml", "docker-compose.override.yaml", "compose.override.yml", "compose.override.yaml")
HOST_PORT = re.compile(r"""^\s*-\s*["']?(?:\d{1,3}(?:\.\d{1,3}){3}:)?(\d+)(?:-\d+)?:\d+""", re.M)
ACTIONS = ("start", "stop", "restart")
MAX_DEPTH = 4
LOG_LINES = 80
START_TIMEOUT = 900
HEALTHY_WAIT = 180
DOCKER_WAIT = 120
PROTECTED_WORDS = re.compile(r"(?is)(live|baseline|production|prod)\b.{0,200}\bserver|\bserver\b.{0,200}\b(live|baseline|production|prod)\b")


class Shell(Protocol):
    def run(self, command: str, cwd: str, timeout: int) -> tuple[int, str, bool]:
        """Run a shell command in a project-relative folder: (exit code, output, timed out)."""


@dataclass
class LocalShell:
    root: Path

    def run(self, command: str, cwd: str, timeout: int) -> tuple[int, str, bool]:
        return test_commands.execute(command, self.root / cwd, timeout)


@dataclass
class SshShell:
    """Another computer, over SSH, in that machine's copy of the project."""
    target: str
    repo_path: str

    def run(self, command: str, cwd: str, timeout: int) -> tuple[int, str, bool]:
        folder = f"{self.repo_path.rstrip('/')}/{cwd}" if cwd not in ("", ".") else self.repo_path
        remote = f"cd {shlex.quote(folder)} && {command}"
        try:
            done = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", self.target, remote], capture_output=True,
                                  text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            return 124, (exc.stdout or "") if isinstance(exc.stdout, str) else "", True
        except OSError as exc:
            return 127, str(exc), False
        return done.returncode, (done.stdout or "") + (done.stderr or ""), False


def shell_for(root: Path, machine: dict[str, Any] | None) -> Shell:
    """This computer, or the machine's SSH shell when it is not local and has an address and a project folder."""
    if machine and machine.get("execution_mode") == "ssh" and machine.get("ssh_target") and machine.get("repo_path"):
        target = machine["ssh_target"][0] if isinstance(machine["ssh_target"], list) else machine["ssh_target"]
        return SshShell(str(target), str(machine["repo_path"]))
    return LocalShell(root)


# ---------------------------------------------------------------------------------------------------- finding them

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "server"


def parse_compose(text: str) -> tuple[list[str], list[int]]:
    """Service names and the host ports they publish, from a compose file's text. Nothing else is read: environment
    values (passwords) are never looked at."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if re.match(r"^services:\s*(#.*)?$", line))
    except StopIteration:
        return [], []
    services: list[str] = []
    ports: list[int] = []
    for line in lines[start + 1:]:
        if line and not line.startswith((" ", "#", "\t")):
            break  # the next top-level key
        match = re.match(r"^ {2}([A-Za-z0-9_.-]+):\s*(#.*)?$", line)
        if match:
            services.append(match.group(1))
        port = HOST_PORT.match(line)
        if port and int(port.group(1)) not in ports:
            ports.append(int(port.group(1)))
    return services, ports


def find(root: Path) -> list[dict[str, Any]]:
    """The compose setups in the project, nested repositories included (a server is often a repository cloned inside).
    Each is {id, name, dir, file, files, services, ports}: `file` is None for a default-named file, which docker compose
    finds (and merges with its override) by itself."""
    found: list[dict[str, Any]] = []
    base = len(root.parts)
    for folder, dirs, files in os.walk(root):
        depth = len(Path(folder).parts) - base
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".") and depth < MAX_DEPTH)  # hidden folders are build copies and dependencies
        names = sorted(f for f in files if COMPOSE_FILE.match(f))
        if not names:
            continue
        rel = Path(folder).relative_to(root).as_posix()
        default = next((n for n in DEFAULT_FILES if n in names), None)
        entries = [(default, None)] if default else []
        entries += [(n, n) for n in names if n not in DEFAULT_FILES and n not in OVERRIDE_FILES]
        for primary, flag in entries:
            services, ports = parse_compose((Path(folder) / primary).read_text(encoding="utf-8", errors="ignore")[:200_000])
            if default and flag is None:
                for override in (n for n in OVERRIDE_FILES if n in names):
                    more_services, more_ports = parse_compose((Path(folder) / override).read_text(encoding="utf-8", errors="ignore")[:200_000])
                    services += [s for s in more_services if s not in services]
                    ports += [p for p in more_ports if p not in ports]
            label = "root" if rel == "." else rel
            name = label if flag is None else f"{label} ({flag})"
            found.append({"id": _slug(f"{label}-{flag or 'default'}"), "name": name, "dir": rel, "file": flag, "files": names,
                          "services": services, "ports": ports})
    return found


def parse_selection(raw: Any, found: list[dict[str, Any]], machines: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    """The servers to start before a build is sent (`servers_for_app` in project.json): [{id, machine}], each naming a server
    the project has and a computer that is local or in machines.json. Anything else is reported and left out."""
    if raw in (None, []):
        return [], []
    if not isinstance(raw, list):
        return [], ["servers_for_app must be a list."]
    ids = {s["id"] for s in found}
    clean: list[dict[str, str]] = []
    errors: list[str] = []
    for item in raw:
        server = str(item.get("id") or "") if isinstance(item, dict) else ""
        machine = str(item.get("machine") or "local") if isinstance(item, dict) else "local"
        if server not in ids:
            errors.append(f'There is no server "{server}" in this project.')
        elif machine != "local" and machine not in machines:
            errors.append(f'"{machine}" isn\'t a computer that servers can be managed on.')
        elif not any(c["id"] == server and c["machine"] == machine for c in clean):
            clean.append({"id": server, "machine": machine})
    return clean, errors


def ensure_running(selection: list[dict[str, str]], found: list[dict[str, Any]], shell_for_machine: Callable[[str], Shell],
                   say: Callable[[str], None] = lambda s: None) -> bool:
    """Make sure each selected server is up, starting the ones that aren't and waiting until they are healthy. False (after
    saying why) as soon as one can't be brought up: whatever needed the server should not go ahead."""
    by_id = {s["id"]: s for s in found}
    for item in selection:
        server = by_id.get(item["id"])
        if server is None:
            say(f"The server {item['id']} is no longer in this project.")
            return False
        where = "this computer" if item["machine"] == "local" else item["machine"]
        shell = shell_for_machine(item["machine"])
        if status(server, shell)["state"] == "running":
            say(f"{server['name']} is already running on {where}.")
            continue
        say(f"Starting {server['name']} on {where} …")
        result = change(server, "start", shell, say)
        if not result["ok"]:
            say(f"{server['name']} didn't come up: {result['message'] or result['state']}")
            return False
        say(f"{server['name']} is running on {where}.")
    return True


def protected_note(root: Path) -> str | None:
    """A warning when the project's own rules talk about a live, baseline or production server."""
    for name in ("AGENTS.md", "CLAUDE.md"):
        try:
            text = (root / name).read_text(encoding="utf-8", errors="ignore")[:150_000]
        except OSError:
            continue
        if PROTECTED_WORDS.search(text):
            return f"{name} mentions a live, baseline or production server. Check which one this is before you start, stop or restart it."
    return None


# ---------------------------------------------------------------------------------------------------- asking Docker

def _compose(server: dict[str, Any], *args: str) -> str:
    flag = f"-f {shlex.quote(server['file'])} " if server.get("file") else ""
    return f"docker compose {flag}{' '.join(args)}"


def docker_ready(shell: Shell) -> bool:
    code, _, _ = shell.run("docker info", ".", 20)
    return code == 0


def parse_ps(output: str) -> list[dict[str, Any]]:
    """Containers from `docker compose ps --format json`, which is one JSON array or one object per line."""
    text = output.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        rows = data if isinstance(data, list) else [data]
    except ValueError:
        rows = []
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ports = sorted({int(p["PublishedPort"]) for p in row.get("Publishers") or [] if p.get("PublishedPort")})
        out.append({"service": row.get("Service") or row.get("Name") or "?", "state": str(row.get("State") or "").lower(),
                    "health": str(row.get("Health") or "").lower(), "status": str(row.get("Status") or ""), "ports": ports})
    return sorted(out, key=lambda r: r["service"])


def summarize(expected: list[str], containers: list[dict[str, Any]]) -> str:
    """running | starting | unhealthy | partial | stopped, from what Docker reports against the services the file defines."""
    if not containers:
        return "stopped"
    names = set(expected) | {c["service"] for c in containers}
    by = {c["service"]: c for c in containers}
    running = [n for n in names if by.get(n, {}).get("state") == "running"]
    if not running:
        return "stopped"
    if any(by[n]["health"] == "unhealthy" for n in running):
        return "unhealthy"
    if any(by.get(n, {}).get("state") == "restarting" for n in names):
        return "unhealthy"
    if len(running) < len(names):
        return "partial"
    if any(by[n]["health"] == "starting" for n in running):
        return "starting"
    return "running"


def status(server: dict[str, Any], shell: Shell, *, docker: bool | None = None) -> dict[str, Any]:
    """{state, containers, message}. `state` is docker-off, unreachable or one of `summarize`'s values. Read-only."""
    if docker is None:
        docker = docker_ready(shell)
    if not docker:
        return {"state": "docker-off", "containers": [], "message": "Docker isn't running (start Docker Desktop, or use Start)."}
    code, output, timed_out = shell.run(_compose(server, "ps", "-a", "--format", "json"), server["dir"], 30)
    if timed_out or code != 0:
        return {"state": "unreachable", "containers": [], "message": (output.strip().splitlines() or ["docker compose didn't answer"])[-1][:200]}
    containers = parse_ps(output)
    return {"state": summarize(server["services"], containers), "containers": containers, "message": ""}


def logs(server: dict[str, Any], shell: Shell, lines: int = LOG_LINES) -> str:
    code, output, _ = shell.run(_compose(server, "logs", "--tail", str(int(lines)), "--no-color"), server["dir"], 60)
    return output[-60_000:] if output else ("(no log output)" if code == 0 else "Couldn't read the logs.")


# ---------------------------------------------------------------------------------------------------- changing things

def ensure_docker(shell: Shell, say: Callable[[str], None], *, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic, can_launch: bool | None = None) -> bool:
    """Docker running, starting Docker Desktop on this Mac if it isn't. False (with a reason said) when it can't be."""
    if docker_ready(shell):
        return True
    launch = isinstance(shell, LocalShell) and sys.platform == "darwin" and Path("/Applications/Docker.app").exists() if can_launch is None else can_launch
    if not launch:
        say("Docker isn't running, and it can't be started from here. Start Docker on that computer and try again.")
        return False
    say("Docker isn't running: starting Docker Desktop …")
    shell.run("open -a Docker", ".", 30)
    deadline = clock() + DOCKER_WAIT
    while clock() < deadline:
        sleep(3)
        if docker_ready(shell):
            say("Docker is running.")
            return True
    say(f"Docker didn't start within {DOCKER_WAIT} seconds.")
    return False


def wait_until_up(server: dict[str, Any], shell: Shell, say: Callable[[str], None], *, wait: int = HEALTHY_WAIT,
                  sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Poll until every service is running and healthy, something is unhealthy, or the wait runs out."""
    deadline = clock() + wait
    last = status(server, shell, docker=True)
    while clock() < deadline:
        if last["state"] in ("running", "unhealthy"):
            break
        say(f"  {last['state']} …")
        sleep(3)
        last = status(server, shell, docker=True)
    return last


def change(server: dict[str, Any], action: str, shell: Shell, say: Callable[[str], None] = lambda s: None, *,
           sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Start, stop or restart one server. {ok, state, containers, message}. Never removes anything."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    if action in ("start", "restart") and not ensure_docker(shell, say, sleep=sleep, clock=clock):
        return {"ok": False, "state": "docker-off", "containers": [], "message": "Docker isn't running."}
    if action == "stop" and not docker_ready(shell):
        return {"ok": True, "state": "docker-off", "containers": [], "message": "Docker isn't running, so there is nothing to stop."}
    command = {"start": _compose(server, "up", "-d"), "stop": _compose(server, "stop"), "restart": _compose(server, "restart")}[action]
    say(f"Running: {command}  (in {server['dir']})")
    code, output, timed_out = shell.run(command, server["dir"], START_TIMEOUT)
    tail = "\n".join(output.strip().splitlines()[-8:])
    if tail:
        say(tail)
    if timed_out or code != 0:
        reason = f"stopped after {START_TIMEOUT // 60} minutes" if timed_out else f"docker compose exited with {code}"
        return {"ok": False, "state": "error", "containers": [], "message": reason + (f": {tail.splitlines()[-1]}" if tail else "")}
    if action == "stop":
        final = status(server, shell, docker=True)
        return {"ok": final["state"] == "stopped", **final}
    say("Waiting for every service to be up and healthy …")
    final = wait_until_up(server, shell, say, sleep=sleep, clock=clock)
    ok = final["state"] == "running"
    message = "" if ok else {"unhealthy": "A service is running but reports unhealthy. Check the logs.", "partial": "Some services aren't running yet.",
                             "starting": "Services are still starting.", "stopped": "Nothing is running."}.get(final["state"], final.get("message", ""))
    return {"ok": ok, **final, "message": message or final.get("message", "")}
