#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from common import CONFIG_DIR, ROOT, read_json
from orchestrator.project_config import PACKAGE_ROOT


DEFAULT_REMOTE_PACKAGE_PATH = "~/.orchestrator/package"


def load_machines() -> list[dict[str, Any]]:
    config_path = CONFIG_DIR / "machines.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Missing machines config: {config_path}")
    return [machine for machine in read_json(config_path)["machines"] if machine.get("enabled", True)]


def selected_machines(name: str | None) -> list[dict[str, Any]]:
    machines = load_machines()
    if name:
        machines = [machine for machine in machines if machine["name"] == name]
        if not machines:
            raise ValueError(f"No enabled machine named {name!r}")
    return machines


def first_ssh_target(machine: dict[str, Any]) -> str:
    target = machine.get("ssh_target")
    if isinstance(target, list):
        target = target[0] if target else None
    if not target:
        raise ValueError(f"Machine {machine['name']} does not have ssh_target configured")
    return target


def remote_package_path(machine: dict[str, Any]) -> str:
    return machine.get("orchestrator_package_path") or DEFAULT_REMOTE_PACKAGE_PATH


def remote_env_prefix(machine: dict[str, Any]) -> str:
    package_path = remote_package_path(machine)
    return f"PYTHONPATH={shlex.quote(package_path)}:$PYTHONPATH"


def remote_worker_command(machine: dict[str, Any], command: str) -> str:
    repo_path = shlex.quote(machine["repo_path"])
    return f"cd {repo_path} && {remote_env_prefix(machine)} {command}"


def remote_import_check_command(machine: dict[str, Any]) -> str:
    return remote_worker_command(
        machine,
        "python3 -c 'import orchestrator.scripts.worker_run; print(\"PACKAGE_OK\")'",
    )


def check_local(machine: dict[str, Any]) -> int:
    print(f"\n[{machine['name']}] local")
    print(f"  repo: {machine['repo_path']}")
    try:
        import orchestrator.scripts.worker_run  # noqa: F401
        print("  package: OK")
    except Exception as exc:
        print(f"  package: MISSING ({exc})")
        return 1

    xcode = subprocess.run(["xcodebuild", "-version"], capture_output=True, text=True, check=False)
    print(f"  xcodebuild: {'OK' if xcode.returncode == 0 else 'MISSING'}")
    return 0 if xcode.returncode == 0 else 1


def check_remote(machine: dict[str, Any]) -> int:
    target = first_ssh_target(machine)
    print(f"\n[{machine['name']}] ssh {target}")
    print(f"  repo: {machine['repo_path']}")
    print(f"  package path: {remote_package_path(machine)}")

    command = " && ".join([
        f"test -d {shlex.quote(machine['repo_path'])}",
        remote_import_check_command(machine).split(" && ", 1)[1],
        "command -v xcodebuild >/dev/null",
    ])
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, command],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        print("  worker: OK")
        return 0

    details = (result.stderr or result.stdout or "").strip()
    print("  worker: NOT READY")
    if details:
        print(f"  details: {details}")
    print("  hint: run orchestrator worker-install --machine " + machine["name"])
    return 1


def install_remote(machine: dict[str, Any]) -> int:
    target = first_ssh_target(machine)
    package_parent = remote_package_path(machine)
    source_package = PACKAGE_ROOT

    print(f"\n[{machine['name']}] installing package to {target}:{package_parent}")
    mkdir = subprocess.run(
        ["ssh", target, f"mkdir -p {shlex.quote(package_parent)}"],
        check=False,
    )
    if mkdir.returncode != 0:
        return mkdir.returncode

    rsync = subprocess.run(
        [
            "rsync",
            "-az",
            "--delete",
            "--exclude",
            "__pycache__/",
            "--exclude",
            "*.pyc",
            f"{source_package}/",
            f"{target}:{package_parent}/orchestrator/",
        ],
        check=False,
    )
    if rsync.returncode != 0:
        return rsync.returncode

    return check_remote(machine)


def paint(text: str, code: str) -> str:
    """Colour text for a terminal (green 92, yellow 93, red 91); plain when piped or NO_COLOR is set."""
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() and not os.environ.get("NO_COLOR") else text


def summary_line(ready: int, total: int, not_ready: list[str]) -> str:
    """One line for the whole check: how many machines can take work, and which can't."""
    if not total:
        return paint("No machines are enabled. Add one under Configuration → Machines.", "93")
    count = f"{ready} of {total} {'machine' if total == 1 else 'machines'} ready"
    if not not_ready:
        return paint(f"✅ {count}", "92")
    return paint(f"{'❌' if not ready else '⚠️'} {count}. Not ready: {', '.join(not_ready)}", "91" if not ready else "93")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "install"])
    parser.add_argument("--machine")
    args = parser.parse_args()

    failures = 0
    machines = selected_machines(args.machine)
    not_ready: list[str] = []
    for machine in machines:
        if machine["execution_mode"] == "local":
            if args.action == "install":
                print(f"\n[{machine['name']}] local machine does not need remote install")
                continue
            failed = check_local(machine)
        elif machine["execution_mode"] == "ssh":
            failed = check_remote(machine) if args.action == "check" else install_remote(machine)
        else:
            print(f"\n[{machine['name']}] unsupported execution mode: {machine['execution_mode']}")
            failed = 1
        failures += failed
        if failed:
            not_ready.append(machine["name"])

    if args.action == "check":
        print(f"\n{summary_line(len(machines) - len(not_ready), len(machines), not_ready)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
