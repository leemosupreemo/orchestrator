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
import shutil
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
HOST_PORT = re.compile(
    r"""^\s*-\s*["']?(?:(?:\d{1,3}(?:\.\d{1,3}){3}|\$\{[A-Za-z0-9_]+(?::-?[^}]*)?\}):)?"""
    r"""(?:\$\{[A-Za-z0-9_]+:-(\d+)\}|(\d+))(?:-\d+)?"""
    r""":(?:\d+|\$\{[A-Za-z0-9_]+:-\d+\})""",
    re.M,
)
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


_EXTRA_BIN_DIRS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/Applications/Docker.app/Contents/Resources/bin",
)


def _extra_bin_dirs() -> list[str]:
    home = Path.home()
    return [*_EXTRA_BIN_DIRS, str(home / ".orbstack" / "bin"), str(home / ".rd" / "bin"), str(home / ".docker" / "bin")]


def _local_env() -> dict[str, str]:
    """Ensure local commands see standard Docker/Homebrew paths even when launched from launchd or a GUI app."""
    env = os.environ.copy()
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for d in _extra_bin_dirs():
        if d not in parts:
            parts.append(d)
    env["PATH"] = os.pathsep.join(parts)
    return env


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in _extra_bin_dirs():
        candidate = Path(d) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


@dataclass
class LocalShell:
    root: Path

    def run(self, command: str, cwd: str, timeout: int) -> tuple[int, str, bool]:
        return test_commands.execute(command, self.root / cwd, timeout, env=_local_env())


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
        if port:
            port_num = int(port.group(1) or port.group(2))
            if port_num not in ports:
                ports.append(port_num)
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


def protected_rule(root: Path) -> dict[str, str] | None:
    """The project's own rule about a live, baseline or production server, quoted: {file, sentence}. The card shows the
    sentence itself, so the person reads what the project says instead of a summary of it."""
    for name in ("AGENTS.md", "CLAUDE.md"):
        try:
            text = (root / name).read_text(encoding="utf-8", errors="ignore")[:150_000]
        except OSError:
            continue
        best: tuple[int, str] | None = None
        for sentence in re.split(r"(?<=[.;])\s+|\n+", text):
            if len(sentence) > 25 and PROTECTED_WORDS.search(sentence):
                # the sentences that say what may be done to the server (its environment, starting, updating) beat ones about publishing
                score = 2 * bool(re.search(r"(?i)environment|port \d+", sentence)) + bool(re.search(r"(?i)\b(start|stop|restart|run|test|update|deploy)", sentence))
                if best is None or score > best[0]:
                    best = (score, sentence)
        if best:
            cleaned = re.sub(r"[*`_>#]+", "", best[1]).strip(" -\t").rstrip(";:,")
            cleaned = cleaned if len(cleaned) <= 240 else cleaned[:237].rstrip() + "..."
            return {"file": name, "sentence": cleaned if cleaned.endswith((".", "!", "?")) else cleaned + "."}
    return None


# ---------------------------------------------------------------------------------------------------- asking Docker

def _compose(server: dict[str, Any], *args: str) -> str:
    flag = f"-f {shlex.quote(server['file'])} " if server.get("file") else ""
    return f"docker compose {flag}{' '.join(args)}"


def docker_ready(shell: Shell) -> bool:
    code, _, _ = shell.run("docker info", ".", 20)
    return code == 0


def docker_installed(shell: Shell, *, docker: bool | None = None) -> bool:
    """Whether any Docker runtime (or CLI) is installed on `shell`."""
    if docker is True:
        return True
    if docker is None and docker_ready(shell):
        return True
    return _docker_launch_command(shell) is not None


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
                    "health": str(row.get("Health") or "").lower(), "status": str(row.get("Status") or ""),
                    "image": str(row.get("Image") or ""), "ports": ports})
    return sorted(out, key=lambda r: r["service"])


def version_info(root: Path, server: dict[str, Any]) -> dict[str, str]:
    """Read-only git version (tag or short commit, plus branch) for the server's directory, if it is inside a git repo."""
    target = root / str(server.get("dir") or ".")
    if not target.exists():
        return {}
    out: dict[str, str] = {}
    try:
        ref = subprocess.run(["git", "-C", str(target), "describe", "--tags", "--always"], capture_output=True, text=True, timeout=3, check=False)
        if ref.returncode == 0 and ref.stdout.strip():
            out["ref"] = ref.stdout.strip()[:40]
        branch = subprocess.run(["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=3, check=False)
        if branch.returncode == 0 and branch.stdout.strip() and branch.stdout.strip() != "HEAD":
            out["branch"] = branch.stdout.strip()[:60]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return out


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


_LABEL_PAIR = re.compile(r"([A-Za-z0-9_./-]+)=(.*?)(?=,[A-Za-z0-9_./-]+=|$)")
_PUBLISHED_PORT = re.compile(r":(\d+)->")
_PORT_CONFLICT = re.compile(
    r"(?:exposing port\s+(tcp|udp)\s+[\d.*:]+:(\d+)\b|"
    r"listen\s+(tcp\d*|udp\d*)\s+[\d.*:]+:(\d+):\s*bind:\s*address already in use|"
    r"Bind for [\d.*:]+:(\d+)\s+failed:\s*port is already allocated)",
    re.I,
)


def _parse_labels(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if not isinstance(raw, str) or not raw:
        return {}
    return {m.group(1): m.group(2) for m in _LABEL_PAIR.finditer(raw)}


def _shell_root(shell: Shell) -> str:
    if isinstance(shell, LocalShell):
        return shell.root.as_posix().rstrip("/")
    if isinstance(shell, SshShell):
        return shell.repo_path.rstrip("/")
    return ""


def _labeled_containers(server: dict[str, Any], shell: Shell) -> list[dict[str, Any]]:
    """Containers Docker already stamped with compose labels for `server`, even when started under `-p <name>` or with
    deploy-time env vars not present in the current shell."""
    if getattr(shell, "ps_sequence", None) is not None:
        return []
    code, output, timed_out = shell.run("docker container ls -a --filter label=com.docker.compose.project --format json", ".", 20)
    if timed_out or code != 0 or not output.strip():
        return []
    base = _shell_root(shell)
    rel_dir = str(server.get("dir") or ".")
    expected_dir = (base if rel_dir in ("", ".") else f"{base}/{rel_dir}").rstrip("/").lower() if base else ""
    primary_file = (server.get("file") or next(iter(server.get("files") or []), "") or "").lower()
    matched: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        labels = _parse_labels(row.get("Labels"))
        if labels.get("com.docker.compose.oneoff", "").lower() == "true":
            continue
        work_dir = labels.get("com.docker.compose.project.working_dir", "").rstrip("/").lower()
        cfg_files = [p.strip().lower() for p in labels.get("com.docker.compose.project.config_files", "").split(",") if p.strip()]
        cfg_basenames = {Path(p).name for p in cfg_files}
        if expected_dir:
            if work_dir != expected_dir:
                continue
        elif rel_dir not in ("", ".") and not work_dir.endswith(f"/{rel_dir.lower()}"):
            continue
        if server.get("file"):
            if server["file"].lower() not in cfg_basenames:
                continue
        elif cfg_basenames and primary_file and primary_file not in cfg_basenames:
            if not (cfg_basenames & {f.lower() for f in DEFAULT_FILES}):
                continue
        raw_health = str(row.get("HealthStatus") or row.get("Health") or "").lower()
        status_str = str(row.get("Status") or "")
        if raw_health in ("", "none"):
            if "(healthy)" in status_str.lower():
                raw_health = "healthy"
            elif "(unhealthy)" in status_str.lower():
                raw_health = "unhealthy"
            elif "health: starting" in status_str.lower():
                raw_health = "starting"
            else:
                raw_health = ""
        ports_str = str(row.get("Ports") or "")
        ports = sorted({int(m.group(1)) for m in _PUBLISHED_PORT.finditer(ports_str)})
        name = str(row.get("Names") or "").lstrip("/")
        matched.append({
            "service": labels.get("com.docker.compose.service") or name or "?",
            "container_name": name,
            "compose_project": labels.get("com.docker.compose.project") or "",
            "state": str(row.get("State") or "").lower(),
            "health": raw_health,
            "status": status_str,
            "image": str(row.get("Image") or ""),
            "ports": ports,
        })
    return sorted(matched, key=lambda r: r["service"])


def _port_owner(port: int, proto: str, shell: Shell) -> str:
    """Best-effort read-only lookup of what local process holds `port` so port-conflict messages name the culprit."""
    if not isinstance(shell, LocalShell):
        return ""
    flag = f"-iUDP:{port}" if proto.upper().startswith("UDP") else f"-iTCP:{port} -sTCP:LISTEN"
    code, out, timed_out = shell.run(f"lsof -nP {flag}", ".", 3)
    if timed_out or code != 0 or not out.strip():
        return ""
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            pid = int(parts[1])
            _, ps_out, _ = shell.run(f"ps -p {pid} -o args=", ".", 3)
            cmdline = ps_out.strip()
            if cmdline:
                tokens = cmdline.split()
                script = next((Path(t).name for t in tokens[1:] if not t.startswith("-") and ("/" in t or t.endswith((".py", ".js", ".sh", ".dll")))), "")
                if script:
                    return script
            return parts[0]
    return ""


def _explain_compose_failure(output: str, code: int, timed_out: bool, shell: Shell | None = None) -> str:
    if timed_out:
        return f"stopped after {START_TIMEOUT // 60} minutes"
    match = _PORT_CONFLICT.search(output or "")
    if match:
        proto = (match.group(1) or match.group(3) or "TCP").upper()
        if proto.startswith("UDP"):
            proto = "UDP"
        elif proto.startswith("TCP"):
            proto = "TCP"
        port = int(match.group(2) or match.group(4) or match.group(5))
        holder = _port_owner(port, proto, shell) if shell is not None else ""
        by = f" by {holder}" if holder else ""
        return f"{proto} port {port} is already in use on this computer{by} (another server or relay is already using that port)."
    tail_line = ((output or "").strip().splitlines() or [""])[-1]
    return f"docker compose exited with {code}" + (f": {tail_line}" if tail_line else "")


def status(server: dict[str, Any], shell: Shell, *, docker: bool | None = None) -> dict[str, Any]:
    """{state, containers, message}. `state` is docker-off, unreachable or one of `summarize`'s values. Read-only."""
    if docker is None:
        docker = docker_ready(shell)
    if not docker:
        return {"state": "docker-off", "containers": [], "message": "Docker isn't running (start Docker Desktop, or use Start)."}
    code, output, timed_out = shell.run(_compose(server, "ps", "-a", "--format", "json"), server["dir"], 30)
    if not timed_out and code == 0:
        containers = parse_ps(output)
        if not containers:
            labeled = _labeled_containers(server, shell)
            if labeled:
                containers = [{k: v for k, v in c.items() if k not in ("container_name", "compose_project")} for c in labeled]
        return {"state": summarize(server["services"], containers), "containers": containers, "message": ""}
    if not timed_out and "no configuration file" not in output.lower():
        labeled = _labeled_containers(server, shell)
        if labeled:
            containers = [{k: v for k, v in c.items() if k not in ("container_name", "compose_project")} for c in labeled]
            return {"state": summarize(server["services"], containers), "containers": containers, "message": ""}
    return {"state": "unreachable", "containers": [], "message": (output.strip().splitlines() or ["docker compose didn't answer"])[-1][:200]}


def logs(server: dict[str, Any], shell: Shell, lines: int = LOG_LINES) -> str:
    code, output, _ = shell.run(_compose(server, "logs", "--tail", str(int(lines)), "--no-color"), server["dir"], 60)
    if (code != 0 or not output.strip()):
        labeled = _labeled_containers(server, shell)
        names = [c["container_name"] for c in labeled if c.get("container_name")]
        if names:
            chunks: list[str] = []
            per_container = max(20, int(lines) // max(1, len(names)))
            for name in names:
                c_code, c_out, _ = shell.run(f"docker logs --tail {per_container} {shlex.quote(name)}", ".", 30)
                if c_out.strip() or c_code == 0:
                    chunks.append(f"=== {name} ===\n{c_out.rstrip()}")
            if chunks:
                return "\n\n".join(chunks)[-60_000:]
    return output[-60_000:] if output else ("(no log output)" if code == 0 else "Couldn't read the logs.")


# ---------------------------------------------------------------------------------------------------- changing things

_DOCKER_LAUNCHERS: dict[str, tuple[str, str]] = {
    "docker-desktop": ("Docker Desktop", "open -a Docker"),
    "docker-desktop-bg": ("Docker Desktop", "open -g -a Docker"),
    "docker-desktop-cli": ("Docker Desktop", "docker desktop start"),
    "orbstack": ("OrbStack", "open -g -a OrbStack"),
    "orbctl": ("OrbStack", "orbctl start"),
    "colima": ("Colima", "colima start"),
    "rancher": ("Rancher Desktop", 'open -g -a "Rancher Desktop"'),
    "podman": ("Podman", "podman machine start"),
    "systemd": ("Docker", "systemctl --user start docker-desktop 2>/dev/null || systemctl start docker"),
}

_REMOTE_DOCKER_PROBE = (
    'PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.orbstack/bin:$HOME/.rd/bin:/Applications/Docker.app/Contents/Resources/bin"; '
    'if [ -d /Applications/Docker.app ] || [ -d "$HOME/Applications/Docker.app" ]; then echo docker-desktop-bg; '
    'elif [ -d /Applications/OrbStack.app ] || [ -d "$HOME/Applications/OrbStack.app" ]; then echo orbstack; '
    'elif command -v orbctl >/dev/null 2>&1; then echo orbctl; '
    'elif command -v colima >/dev/null 2>&1; then echo colima; '
    'elif [ -d "/Applications/Rancher Desktop.app" ] || [ -d "$HOME/Applications/Rancher Desktop.app" ]; then echo rancher; '
    'elif command -v podman >/dev/null 2>&1; then echo podman; '
    'elif command -v systemctl >/dev/null 2>&1; then echo systemd; '
    'fi'
)


def _active_docker_context() -> str:
    """Return the active Docker CLI context name from ~/.docker/config.json, or '' when unset/unreadable."""
    try:
        cfg = Path.home() / ".docker" / "config.json"
        if not cfg.exists():
            return ""
        data = json.loads(cfg.read_text(encoding="utf-8", errors="ignore"))
        return str(data.get("currentContext") or "").strip().lower() if isinstance(data, dict) else ""
    except (OSError, ValueError):
        return ""


def _docker_launch_command(shell: Shell) -> tuple[str, str] | None:
    """Detect how to start Docker on `shell` (local or SSH macOS/Linux): (display label, shell command), or None."""
    if isinstance(shell, LocalShell):
        if sys.platform == "darwin":
            home_apps = Path.home() / "Applications"
            has_docker_app = Path("/Applications/Docker.app").exists() or (home_apps / "Docker.app").exists()
            has_orb_app = Path("/Applications/OrbStack.app").exists() or (home_apps / "OrbStack.app").exists()
            has_rancher_app = Path("/Applications/Rancher Desktop.app").exists() or (home_apps / "Rancher Desktop.app").exists()
            has_orbctl = bool(_which("orbctl"))
            has_colima = bool(_which("colima"))
            has_podman = bool(_which("podman"))

            ctx = _active_docker_context()
            if ctx.startswith("colima") and has_colima:
                return _DOCKER_LAUNCHERS["colima"]
            if ctx.startswith("orbstack"):
                if has_orb_app:
                    return _DOCKER_LAUNCHERS["orbstack"]
                if has_orbctl:
                    return _DOCKER_LAUNCHERS["orbctl"]
            if ctx.startswith("rancher") and has_rancher_app:
                return _DOCKER_LAUNCHERS["rancher"]

            if has_docker_app:
                return _DOCKER_LAUNCHERS["docker-desktop"]
            if has_orb_app:
                return _DOCKER_LAUNCHERS["orbstack"]
            if has_orbctl:
                return _DOCKER_LAUNCHERS["orbctl"]
            if has_colima:
                return _DOCKER_LAUNCHERS["colima"]
            if has_rancher_app:
                return _DOCKER_LAUNCHERS["rancher"]
            if has_podman:
                return _DOCKER_LAUNCHERS["podman"]
            if _which("docker"):
                return _DOCKER_LAUNCHERS["docker-desktop"]
        elif sys.platform.startswith("linux"):
            if _which("systemctl"):
                return _DOCKER_LAUNCHERS["systemd"]
            if _which("colima"):
                return _DOCKER_LAUNCHERS["colima"]
        return None
    if isinstance(shell, SshShell):
        code, output, timed_out = shell.run(_REMOTE_DOCKER_PROBE, ".", 15)
        if not timed_out and code == 0:
            found = (output.strip().splitlines() or [""])[-1].strip()
            return _DOCKER_LAUNCHERS.get(found)
    return None


def ensure_docker(shell: Shell, say: Callable[[str], None], *, sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic, can_launch: bool | None = None) -> bool:
    """Docker running, starting Docker Desktop / OrbStack / Colima / Rancher / Podman / systemd locally or over SSH
    if it isn't. False (with a reason said) when it can't be started."""
    if docker_ready(shell):
        return True
    if can_launch is False:
        launcher = None
    elif can_launch is True:
        launcher = _docker_launch_command(shell) or _DOCKER_LAUNCHERS["docker-desktop"]
    else:
        launcher = _docker_launch_command(shell)
    if not launcher:
        if can_launch is False:
            say("Docker isn't running, and it can't be started from here. Start Docker on that computer and try again.")
        else:
            say("Docker isn't installed on this computer, so it can't be started from here. Click Install Docker (or run `orchestrator server install-docker`) to set it up automatically.")
        return False
    label, command = launcher
    say(f"Docker isn't running: starting {label} …")
    run_cmd = command
    if isinstance(shell, SshShell) and not command.startswith("open "):
        run_cmd = 'PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.orbstack/bin:$HOME/.rd/bin:/Applications/Docker.app/Contents/Resources/bin" ' + command
    code, output, timed_out = shell.run(run_cmd, ".", 60)
    if timed_out or code != 0:
        detail = (output.strip().splitlines() or [""])[-1][:200]
        if detail:
            say(f"  Couldn't start {label}: {detail}")
        if label == "Docker Desktop" and command != _DOCKER_LAUNCHERS["docker-desktop-cli"][1]:
            say("  Trying `docker desktop start` …")
            fb_cmd = _DOCKER_LAUNCHERS["docker-desktop-cli"][1]
            if isinstance(shell, SshShell):
                fb_cmd = 'PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin" ' + fb_cmd
            fb_code, fb_out, fb_to = shell.run(fb_cmd, ".", 60)
            if fb_to or fb_code != 0:
                fb_detail = (fb_out.strip().splitlines() or [""])[-1][:200]
                if fb_detail:
                    say(f"  Couldn't start Docker Desktop via CLI: {fb_detail}")
    deadline = clock() + DOCKER_WAIT
    while clock() < deadline:
        if docker_ready(shell):
            say("Docker is running.")
            return True
        sleep(3)
    if docker_ready(shell):
        say("Docker is running.")
        return True
    say(f"Docker didn't start within {DOCKER_WAIT} seconds.")
    return False


_SSH_BREW_PATH = 'PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.orbstack/bin:$HOME/.rd/bin:/Applications/Docker.app/Contents/Resources/bin"'


def _has_brew(shell: Shell) -> bool:
    if isinstance(shell, LocalShell):
        return bool(_which("brew"))
    if isinstance(shell, SshShell):
        code, _, timed_out = shell.run(f"{_SSH_BREW_PATH} command -v brew >/dev/null 2>&1", ".", 15)
        return not timed_out and code == 0
    code, _, timed_out = shell.run("command -v brew >/dev/null 2>&1", ".", 15)
    return not timed_out and code == 0


def install_docker(shell: Shell, say: Callable[[str], None] = lambda s: None, *,
                   sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Install a headless Docker runtime (Colima + Docker CLI + Docker Compose via Homebrew) and start it.
    Needs no sudo password or Docker Desktop GUI prompt, so it works from the web app and over SSH."""
    if docker_ready(shell):
        say("Docker is already installed and running.")
        return {"ok": True, "state": "running", "message": "Docker is already running."}
    if _docker_launch_command(shell) is not None:
        notes: list[str] = []

        def _capture(line: str) -> None:
            notes.append(line.strip())
            say(line)

        if ensure_docker(shell, _capture, sleep=sleep, clock=clock):
            return {"ok": True, "state": "running", "message": "Docker is running."}
        return {"ok": False, "state": "docker-off", "message": notes[-1] if notes else "Docker is installed but didn't start."}
    if not _has_brew(shell):
        msg = "Homebrew isn't installed, so Docker can't be installed automatically. Install Homebrew (https://brew.sh) or Docker Desktop (https://www.docker.com/products/docker-desktop/) and try again."
        say(msg)
        return {"ok": False, "state": "docker-missing", "message": msg}

    prefix = f"{_SSH_BREW_PATH} " if isinstance(shell, SshShell) else ""
    say("Installing Docker CLI, Docker Compose, and Colima via Homebrew …")
    code, output, timed_out = shell.run(f"{prefix}HOMEBREW_NO_AUTO_UPDATE=1 brew install docker docker-compose colima", ".", 600)
    tail = "\n".join(output.strip().splitlines()[-8:])
    if tail:
        say(tail)
    if timed_out or code != 0:
        reason = "brew install timed out" if timed_out else f"brew install exited with {code}"
        detail = (output.strip().splitlines() or [""])[-1][:200]
        return {"ok": False, "state": "error", "message": reason + (f": {detail}" if detail else "")}

    say("Linking Docker Compose plugin …")
    link_cmd = (
        f'{prefix}sh -c \'PREFIX="$(brew --prefix)"; mkdir -p "$HOME/.docker/cli-plugins"; '
        'if [ -x "$PREFIX/opt/docker-compose/bin/docker-compose" ]; then '
        'ln -sfn "$PREFIX/opt/docker-compose/bin/docker-compose" "$HOME/.docker/cli-plugins/docker-compose"; '
        'elif command -v docker-compose >/dev/null 2>&1; then '
        'ln -sfn "$(command -v docker-compose)" "$HOME/.docker/cli-plugins/docker-compose"; fi\''
    )
    shell.run(link_cmd, ".", 30)

    say("Starting Colima (first start downloads the Linux VM image and can take a minute) …")
    code, output, timed_out = shell.run(f"{prefix}colima start", ".", 300)
    tail = "\n".join(output.strip().splitlines()[-8:])
    if tail:
        say(tail)
    if timed_out or code != 0:
        reason = "colima start timed out" if timed_out else f"colima start exited with {code}"
        detail = (output.strip().splitlines() or [""])[-1][:200]
        return {"ok": False, "state": "docker-off", "message": reason + (f": {detail}" if detail else "")}

    deadline = clock() + DOCKER_WAIT
    while clock() < deadline:
        if docker_ready(shell):
            say("Docker is installed and running.")
            return {"ok": True, "state": "running", "message": "Docker is installed and running."}
        sleep(3)
    if docker_ready(shell):
        say("Docker is installed and running.")
        return {"ok": True, "state": "running", "message": "Docker is installed and running."}
    msg = f"Colima finished starting, but Docker didn't respond within {DOCKER_WAIT} seconds."
    say(msg)
    return {"ok": False, "state": "docker-off", "message": msg}


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
    if action in ("start", "restart"):
        docker_notes: list[str] = []

        def _say_docker(line: str) -> None:
            docker_notes.append(line.strip())
            say(line)

        if not ensure_docker(shell, _say_docker, sleep=sleep, clock=clock):
            reason = docker_notes[-1] if docker_notes else "Docker isn't running."
            return {"ok": False, "state": "docker-off", "containers": [], "message": reason}
    if action == "stop" and not docker_ready(shell):
        return {"ok": True, "state": "docker-off", "containers": [], "message": "Docker isn't running, so there is nothing to stop."}
    command = {"start": _compose(server, "up", "-d"), "stop": _compose(server, "stop"), "restart": _compose(server, "restart")}[action]
    say(f"Running: {command}  (in {server['dir']})")
    code, output, timed_out = shell.run(command, server["dir"], START_TIMEOUT)
    # Parameterized compose stacks started under `-p <project>` with deploy-time env vars can still be started/stopped/restarted
    # via their existing labeled containers when `docker compose` fails interpolation or misses the `-p` project name.
    if not timed_out and (code != 0 or action in ("stop", "restart")):
        needs_fallback = (
            (code != 0 and _PORT_CONFLICT.search(output or "") is None)
            or (code == 0 and action in ("stop", "restart") and not output.strip())
        )
        if needs_fallback:
            labeled = _labeled_containers(server, shell)
            names = [c["container_name"] for c in labeled if c.get("container_name")]
            services_found = {c["service"] for c in labeled}
            if names and (action in ("stop", "restart") or set(server.get("services") or ()) <= services_found):
                docker_verb = {"start": "start", "stop": "stop", "restart": "restart"}[action]
                fb_cmd = f"docker {docker_verb} " + " ".join(shlex.quote(n) for n in names)
                say(f"Running: {fb_cmd}")
                code, output, timed_out = shell.run(fb_cmd, ".", START_TIMEOUT)
    tail = "\n".join(output.strip().splitlines()[-8:])
    if tail:
        say(tail)
    if timed_out or code != 0:
        return {"ok": False, "state": "error", "containers": [], "message": _explain_compose_failure(output, code, timed_out, shell)}
    if action == "stop":
        final = status(server, shell, docker=True)
        return {"ok": final["state"] == "stopped", **final}
    say("Waiting for every service to be up and healthy …")
    final = wait_until_up(server, shell, say, sleep=sleep, clock=clock)
    ok = final["state"] == "running"
    message = "" if ok else {"unhealthy": "A service is running but reports unhealthy. Check the logs.", "partial": "Some services aren't running yet.",
                             "starting": "Services are still starting.", "stopped": "Nothing is running."}.get(final["state"], final.get("message", ""))
    return {"ok": ok, **final, "message": message or final.get("message", "")}
