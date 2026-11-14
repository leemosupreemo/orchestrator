"""Run `orchestrator ui --tunnel` in the background, starting at login and restarting if it stops, so this computer
stays reachable from the hosted app without a terminal left open.

Orchestrator runs on a Mac, so this is a launchd agent (~/Library/LaunchAgents). It starts the command in a neutral
working directory, so the server opens the project you last used (the active project) rather than whatever folder
`install` was run from; switching projects in the web app carries over to the next start.
"""
from __future__ import annotations

import os
import plistlib
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from orchestrator.project_config import DEFAULT_RUNTIME_DIRNAME, active_project_root, find_project_root, load_recent_projects, safe_resolve, user_state_dir

LABEL = "com.orchestrator.ui"
RESTART_DELAY_SECONDS = 30  # between restarts, so a server that can't start doesn't spin


class ServiceError(Exception):
    pass


@dataclass(frozen=True)
class Spec:
    python: str
    workdir: Path
    log: Path
    path: str
    keep_awake: bool = False
    state_dir: str = ""  # only when ORCHESTRATOR_USER_STATE_DIR is set (tests, unusual setups)

    @property
    def command(self) -> list[str]:
        return [self.python, "-m", "orchestrator", "ui", "--no-open", "--managed-connection"]

    @property
    def argv(self) -> list[str]:
        # caffeinate -i keeps an idle Mac from sleeping while the server runs (the screen can still turn off).
        return (["/usr/bin/caffeinate", "-i"] if self.keep_awake else []) + self.command

    @property
    def environment(self) -> dict[str, str]:
        # A service starts with a nearly empty PATH, which would hide cloudflared, gh, xcodebuild and the AI tools' CLIs.
        env = {"PATH": self.path}
        if self.state_dir:
            env["ORCHESTRATOR_USER_STATE_DIR"] = self.state_dir
        return env


def make_spec(keep_awake: bool = False) -> Spec:
    state = user_state_dir()
    return Spec(python=sys.executable, workdir=state / "service", log=state / "logs" / "service.log",
                path=os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"), keep_awake=keep_awake,
                state_dir=os.environ.get("ORCHESTRATOR_USER_STATE_DIR", ""))


def service_project(workdir: Path) -> Path | None:
    """The project the service will open: the first one found above its working directory, else the active project."""
    root = find_project_root(workdir)
    if root == safe_resolve(workdir):
        root = active_project_root()
    if root and (root / DEFAULT_RUNTIME_DIRNAME / "project.json").is_file():
        return root
    # Discovery can remember a folder before it has been configured. That must not leave
    # the machine's background server in a restart loop when another configured project exists.
    for project in load_recent_projects().get("projects", []):
        candidate = safe_resolve(Path(project["root"]).expanduser())
        if (candidate / DEFAULT_RUNTIME_DIRNAME / "project.json").is_file():
            return candidate
    return None


# -- the files

def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def render_launchd(spec: Spec) -> bytes:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": spec.argv,
        "WorkingDirectory": str(spec.workdir),
        "EnvironmentVariables": spec.environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": RESTART_DELAY_SECONDS,
        "StandardOutPath": str(spec.log),
        "StandardErrorPath": str(spec.log),
    })


Runner = Callable[..., subprocess.CompletedProcess]


def _run(run: Runner, *argv: str, check: bool = True, bootstrap_retries: int = 0) -> subprocess.CompletedProcess:
    for attempt in range(bootstrap_retries + 1):
        result = run(list(argv), capture_output=True, text=True)
        if result.returncode != 5 or attempt == bootstrap_retries:
            break
        time.sleep(1)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise ServiceError(f"`{' '.join(argv)}` failed: {detail[-1] if detail else f'exit {result.returncode}'}")
    return result


def _domain() -> str:
    return f"gui/{os.getuid()}"


def definition_path(system: str = "Darwin") -> Path:
    return launch_agents_dir() / f"{LABEL}.plist"


def _check_system(system: str) -> None:
    if system != "Darwin":
        raise ServiceError("Orchestrator runs on a Mac, so running it in the background is only supported on macOS. "
                           "Use it from this computer's browser, phone or tablet once it is running on your Mac.")


# -- install, uninstall, status

def adopt_service_url(path: Path, system: str) -> str:
    """Migrate an installed explicit public origin before replacing its definition."""
    from orchestrator.connection_manager import ConnectionStore, endpoint_url
    adopted = ""
    token = None
    try:
        argv = plistlib.loads(path.read_bytes()).get("ProgramArguments", [])
        for index, arg in enumerate(argv):
            value = arg.split("=", 1)[1] if arg.startswith("--public-url=") else (argv[index + 1] if arg == "--public-url" and index + 1 < len(argv) else "")
            if endpoint_url(value):
                adopted = value
            credential = arg.split("=", 1)[1] if arg.startswith("--tunnel-token=") else (argv[index + 1] if arg == "--tunnel-token" and index + 1 < len(argv) else None)
            if credential:
                token = credential
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
        pass
    if token is not None:
        ConnectionStore().configure("cloudflare", adopted, token=token)
    elif adopted:
        ConnectionStore().adopt(adopted)
    return adopted


def install(spec: Spec, system: str | None = None, run: Runner = subprocess.run) -> list[str]:
    """Write the service definition and start it. Returns lines to show the person."""
    system = system or platform.system()
    _check_system(system)
    spec.workdir.mkdir(parents=True, exist_ok=True)
    spec.log.parent.mkdir(parents=True, exist_ok=True)
    if not spec.log.exists():
        spec.log.touch(mode=0o600)  # the log shows this computer's access address, so only this person may read it
    project = service_project(spec.workdir)
    if project is None:
        raise ServiceError("There's no project for it to open. Run `orchestrator wizard` (or `orchestrator use <project>`) first, "
                           "then install the service.")
    path = definition_path(system)
    adopt_service_url(path, system)
    path.parent.mkdir(parents=True, exist_ok=True)
    notes = [f"Opens {project.name} ({project}) at login; the last project you used from the web app carries over."]
    path.write_bytes(render_launchd(spec))
    _run(run, "launchctl", "bootout", f"{_domain()}/{LABEL}", check=False)  # replace one that's already loaded
    _run(run, "launchctl", "bootstrap", _domain(), str(path), bootstrap_retries=2)
    if spec.keep_awake:
        notes.append("This computer won't go to sleep from idleness while it runs (the screen still turns off).")
    notes.append(f"Log: {spec.log}")
    return notes


def uninstall(system: str | None = None, run: Runner = subprocess.run) -> bool:
    """Stop the service and remove its definition. False if it wasn't installed."""
    system = system or platform.system()
    _check_system(system)
    path = definition_path(system)
    if not path.exists():
        return False
    _run(run, "launchctl", "bootout", f"{_domain()}/{LABEL}", check=False)
    path.unlink()
    return True


def status(system: str | None = None, run: Runner = subprocess.run) -> dict[str, object]:
    """{installed, running, detail}"""
    system = system or platform.system()
    _check_system(system)
    installed = definition_path(system).exists()
    if not installed:
        return {"installed": False, "running": False, "detail": "Not installed."}
    result = _run(run, "launchctl", "print", f"{_domain()}/{LABEL}", check=False)
    text = result.stdout or ""
    running = result.returncode == 0 and "state = running" in text
    pid = next((line.split("=")[1].strip() for line in text.splitlines() if line.strip().startswith("pid =")), "")
    detail = f"Running (process {pid})." if running and pid else "Running." if running else (
        "Installed but not running. It retries every 30 seconds; the log says why." if result.returncode == 0 else "Installed but not loaded.")
    return {"installed": True, "running": running, "detail": detail}
