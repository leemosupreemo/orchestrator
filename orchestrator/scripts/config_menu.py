"""Runs one Configuration-menu screen of the console on its own.

The web UI's Configuration page starts this inside a terminal so every menu in
the console's "Configuration & Advanced Tools" section is reachable from the
browser, using the console's own handlers rather than a second implementation.

    python config_menu.py <menu>
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

import dev_console as dc  # noqa: E402
from common import BackException, StatusBar  # noqa: E402


def _machines() -> list[str]:
    try:
        return [m["name"] for m in dc.load_machines()]
    except Exception:
        return ["local"]


def _models() -> list[str]:
    return []


def _models_refresh() -> None:
    from model_registry import sync_models
    print("Scanning local CLIs (Ollama, OpenCode) and provider APIs for models...")
    ok, msg = sync_models(live_discovery=True)
    print(("OK: " if ok else "Failed: ") + msg)


def _project() -> None:
    with StatusBar(sub_menu=True) as bar:
        dc.handle_change_target_project(bar)


def _github() -> None:
    import subprocess
    print("Signing in to GitHub. The orchestrator opens an issue per job and a PR per finished job.\n")
    subprocess.run(["gh", "auth", "login"], check=False)
    subprocess.run(["gh", "auth", "status"], check=False)


MENUS = {
    "github": _github,
    "models": _models_refresh,
    "keys": lambda: dc.handle_api_keys(_machines(), _models()),
    "instructions": lambda: dc.handle_instruction_files(_machines(), _models()),
    "fleet": lambda: dc.handle_fleet_management(_machines()),
    "project": _project,
    "archived": lambda: dc.handle_archived_jobs_menu(_machines(), _models()),
    "firebase": lambda: dc.handle_firebase_distro(_machines(), _models()),
    "xcode": lambda: dc.handle_xcode_cloud_menu(_machines(), _models()),
    "email": lambda: dc.handle_email_settings(_machines(), _models()),
    "audit": lambda: dc.handle_system_health(_machines(), _models()),
    "selftests": lambda: dc.handle_tooling_tests(_machines(), _models()),
    "update": lambda: dc.handle_update_orchestrator(_machines()),
    "all": lambda: dc.handle_configuration_menu(_machines(), _models()),
}


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in MENUS:
        print(f"usage: config_menu.py <{'|'.join(MENUS)}>")
        return 2
    try:
        MENUS[argv[0]]()
    except (BackException, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
