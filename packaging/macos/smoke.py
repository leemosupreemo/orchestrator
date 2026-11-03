"""Start an assembled app's bundled agent with isolated state, and check it answers like a fresh install.

It never registers a login item, reads the person's ~/.orchestrator, or touches a server already running: the agent
uses the given state and control folders and picks another port if its usual one is taken.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

MACHO = (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")


def check_executable(bundle: Path) -> None:
    executable = bundle / "Contents/MacOS/Orchestrator"
    with executable.open("rb") as stream:
        if stream.read(4) not in MACHO:
            raise ValueError(f"{executable} is not a macOS executable.")
    archs = subprocess.check_output(["/usr/bin/lipo", "-archs", str(executable)], text=True).split()
    if platform.machine() not in archs:
        raise ValueError(f"The app executable is built for {' '.join(archs)}; this Mac is {platform.machine()}.")


def request(control_dir: Path, command: str, params: dict | None = None) -> dict:
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5)
        client.connect(str(control_dir / "agent.sock"))
        client.sendall(json.dumps({"version": 1, "request_id": "smoke", "command": command, "params": params or {}}).encode() + b"\n")
        with client.makefile("rb") as stream:
            reply = json.loads(stream.readline())
    if not reply.get("ok"):
        raise RuntimeError(f"{command} failed: {reply.get('error')}")
    return reply["result"]


def smoke(bundle: Path, state_dir: Path, control_dir: Path) -> dict:
    bundle = bundle.resolve()
    check_executable(bundle)
    version = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())["CFBundleShortVersionString"]
    env = {"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin", "ORCHESTRATOR_USER_STATE_DIR": str(state_dir)}
    agent = subprocess.Popen([bundle / "Contents/MacOS/OrchestratorAgentLauncher", "--control-dir", control_dir], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline and agent.poll() is None:
            try:
                status = request(control_dir, "status")
                if status["local_interface"] == "ready":
                    break
            except (OSError, ValueError):
                pass
            time.sleep(.2)
        if not status or status["local_interface"] != "ready":
            raise RuntimeError("The bundled agent did not become ready:\n" + (agent.stdout.read().decode(errors="replace") if agent.poll() is not None else ""))
        if status["setup"] != "needs_project" or status["runner"]["version"] != version:
            raise RuntimeError(f"Unexpected fresh-install status: {status}")
        grant = request(control_dir, "browser_grant", {"route": "#/"})["url"]
        with urllib.request.urlopen(grant, timeout=10) as page:
            if page.status != 200 or b"<html" not in page.read(4096).lower():
                raise RuntimeError("The browser workspace did not open.")
        diagnostics = request(control_dir, "diagnostics")
        if not request(control_dir, "stop_if_idle")["accepted"]:
            raise RuntimeError("A fresh agent refused to stop while idle.")
        agent.wait(20)
        return {"version": version, "status": status["setup"], "checks": diagnostics["checks"], "exit": agent.returncode}
    finally:
        if agent.poll() is None:
            agent.terminate()
            agent.wait(10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--control-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(smoke(args.bundle, args.state_dir, args.control_dir), indent=2))


if __name__ == "__main__":
    main()
