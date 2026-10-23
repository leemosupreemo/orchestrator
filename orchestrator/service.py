"""Run `orchestrator ui --tunnel` in the background, starting at login and restarting if it stops, so this computer
stays reachable from the hosted app without a terminal left open.

macOS uses a launchd agent (~/Library/LaunchAgents), Linux a systemd user unit. Both start the same command in a
neutral working directory, so the server opens the project you last used (the active project) rather than whatever
folder `install` was run from; switching projects in the web app carries over to the next start.
"""
from __future__ import annotations

import os
import plistlib
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from orchestrator.project_config import DEFAULT_RUNTIME_DIRNAME, active_project_root, find_project_root, safe_resolve, user_state_dir

LABEL = "com.orchestrator.ui"
UNIT = "orchestrator-ui.service"
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
        return [self.python, "-m", "orchestrator", "ui", "--no-open", "--tunnel"]

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
    return root if root and (root / DEFAULT_RUNTIME_DIRNAME / "project.json").is_file() else None


# -- the files

def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def systemd_user_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


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


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_systemd(spec: Spec) -> str:
    env = "\n".join(f"Environment={_systemd_quote(f'{k}={v}')}" for k, v in spec.environment.items())
    return f"""[Unit]
Description=Orchestrator web server and tunnel
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={" ".join(_systemd_quote(part) for part in spec.argv)}
WorkingDirectory={spec.workdir}
{env}
Restart=always
RestartSec={RESTART_DELAY_SECONDS}
StandardOutput=append:{spec.log}
StandardError=append:{spec.log}

[Install]
WantedBy=default.target
"""


Runner = Callable[..., subprocess.CompletedProcess]


def _run(run: Runner, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
    result = run(list(argv), capture_output=True, text=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise ServiceError(f"`{' '.join(argv)}` failed: {detail[-1] if detail else f'exit {result.returncode}'}")
    return result


def _domain() -> str:
    return f"gui/{os.getuid()}"


def definition_path(system: str) -> Path:
    return launch_agents_dir() / f"{LABEL}.plist" if system == "Darwin" else systemd_user_dir() / UNIT


def _check_system(system: str) -> None:
    if system not in ("Darwin", "Linux"):
        raise ServiceError("Running in the background is supported on macOS and Linux. On Windows, keep `orchestrator ui --tunnel` open.")


# -- install, uninstall, status

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
    path.parent.mkdir(parents=True, exist_ok=True)
    notes = [f"Opens {project.name} ({project}) at login; the last project you used from the web app carries over."]
    if system == "Darwin":
        path.write_bytes(render_launchd(spec))
        _run(run, "launchctl", "bootout", f"{_domain()}/{LABEL}", check=False)  # replace one that's already loaded
        _run(run, "launchctl", "bootstrap", _domain(), str(path))
    else:
        path.write_text(render_systemd(spec))
        _run(run, "systemctl", "--user", "daemon-reload")
        _run(run, "systemctl", "--user", "enable", "--now", UNIT)
        notes.append("To keep it running when you're logged out: loginctl enable-linger $USER")
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
    if system == "Darwin":
        _run(run, "launchctl", "bootout", f"{_domain()}/{LABEL}", check=False)
    else:
        _run(run, "systemctl", "--user", "disable", "--now", UNIT, check=False)
    path.unlink()
    if system == "Linux":
        _run(run, "systemctl", "--user", "daemon-reload", check=False)
    return True


def status(system: str | None = None, run: Runner = subprocess.run) -> dict[str, object]:
    """{installed, running, detail}"""
    system = system or platform.system()
    _check_system(system)
    installed = definition_path(system).exists()
    if not installed:
        return {"installed": False, "running": False, "detail": "Not installed."}
    if system == "Darwin":
        result = _run(run, "launchctl", "print", f"{_domain()}/{LABEL}", check=False)
        text = result.stdout or ""
        running = result.returncode == 0 and "state = running" in text
        pid = next((line.split("=")[1].strip() for line in text.splitlines() if line.strip().startswith("pid =")), "")
        detail = f"Running (process {pid})." if running and pid else "Running." if running else (
            "Installed but not running. It retries every 30 seconds; the log says why." if result.returncode == 0 else "Installed but not loaded.")
    else:
        result = _run(run, "systemctl", "--user", "is-active", UNIT, check=False)
        running = (result.stdout or "").strip() == "active"
        detail = "Running." if running else f"Installed but {(result.stdout or 'not running').strip()}."
    return {"installed": True, "running": running, "detail": detail}
