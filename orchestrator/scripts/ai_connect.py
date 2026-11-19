#!/usr/bin/env python3
"""Connect an AI tool for someone who doesn't know what a terminal is. Run by the AI tools page's Connect button, in
Orchestrator's own terminal: install the tool if it's missing (with whichever of its installers can run here), run its
sign-in (which opens the browser to approve), then check it really is signed in and say so plainly. When it worked, this
computer's models follow at once, so jobs can use it straight away.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from orchestrator import ai_providers


def _installer(provider: dict, which: Callable[[str], str | None]) -> tuple[str | None, list[str]]:
    """The first install command that can run here, or (None, what's missing for each)."""
    missing: list[str] = []
    for command in (provider.get("install"), provider.get("install_alt")):
        if not command:
            continue
        first = command.split()[0]
        needs = ai_providers.INSTALLER_NEEDS.get(first, [first])
        if all(which(tool) for tool in needs):
            return command, []
        missing.append(first)
    return None, missing


KEYCHAIN_ITEM = "Claude Code-credentials"
KEYCHAIN_HINT = ("If your Mac's keychain is locked or waiting for permission, unlock it on the Mac itself (a prompt may be on its screen), "
                 "then press Connect again.")


def claude_credentials_state(*, home: Path | None = None, security: Callable[[], str] | None = None) -> tuple:
    """Where Claude Code keeps its sign-in, as something that changes whenever a sign-in is saved: the keychain item's
    modification date (macOS) and the credentials file's. It never reads the secret. `claude auth login` can finish in the
    browser and then fail to save ("Couldn't save your login"), leaving the old, possibly dead, sign-in in place, which
    `claude auth status` still calls signed in."""
    def keychain() -> str:
        try:
            out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_ITEM], capture_output=True, text=True,
                                 timeout=5, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
        return next((line.strip() for line in out.splitlines() if '"mdat"' in line), "")
    folder = Path(os.environ.get("CLAUDE_CONFIG_DIR") or ((home or Path.home()) / ".claude"))
    try:
        stamp = (folder / ".credentials.json").stat().st_mtime_ns
    except OSError:
        stamp = None
    return ((security or keychain)(), stamp)


def connect(provider_id: str, *, which: Callable[[str], str | None], run: Callable[[list[str]], int],
            ready: Callable[[str], bool], say: Callable[[str], None], after: Callable[[], None],
            ensure_prerequisites: Callable[[str], None] | None = None,
            progress: Callable[[str, str], None] | None = None,
            credentials: Callable[[], object] | None = None) -> int:
    if provider_id not in ai_providers.SIGN_IN_COMMANDS:
        raise ValueError("That AI tool has no sign-in to connect.")
    provider = next(p for p in ai_providers.PROVIDERS if p["id"] == provider_id)
    name, cli = provider["name"], provider["cli"]
    def report(step: str, detail: str) -> None:
        if progress:
            progress(step, detail)
        say(detail)

    report("check", f"Connecting {name}.")
    path = which(cli)
    if not path:
        if provider_id == "codex" and ensure_prerequisites and (not which("npm") or not which("node")):
            report("prerequisites", "Installing Node.js and npm for this user. This can take a minute or two.")
            try:
                ensure_prerequisites(provider_id)
            except (RuntimeError, OSError) as exc:
                report("error", f"Couldn't prepare {name}: {exc}. Press Connect to try again.")
                return 1
        command, missing = _installer(provider, which)
        if not command:
            needed = " or ".join(ai_providers.MISSING_NEEDS.get(m, m) for m in missing)
            report("error", f"{name} installs with {needed}, which isn't on this computer yet. Install it, then press Connect again.")
            return 1
        report("install", f"Installing {name}. This can take a minute or two. If it asks you to agree to its terms, type y and press Enter.")
        try:
            result = run(["bash", "-c", command])
        except OSError as exc:
            report("error", f"Couldn't start the installer for {name}: {exc}. Press Connect to try again.")
            return 1
        if result != 0:
            report("error", f"{name} didn't install (see above). Press Connect to try again, or see {provider.get('link', 'its website')}.")
            return 1
        path = which(cli)
        if not path:
            report("error", f"{name} installed, but it can't be found on this computer. Restart Orchestrator, then press Connect again.")
            return 1
    report("sign_in", f"Signing in to {name}. "
        + (ai_providers.SIGN_IN_HINTS.get(provider_id) or "Your browser opens to approve: sign in there, then come back here.")
        + (" Orchestrator finishes when sign-in is detected." if not ai_providers.SIGN_IN_COMMANDS[provider_id] else ""))
    saved_before = credentials() if credentials else None
    try:
        result = run([path, *ai_providers.SIGN_IN_COMMANDS[provider_id]])
    except OSError as exc:
        report("error", f"Couldn't start sign-in for {name}: {exc}. Press Connect to try again.")
        return 1
    if isinstance(result, int) and result != 0:
        report("error", f"{name}'s sign-in didn't finish (it stopped with code {result}). "
               + (KEYCHAIN_HINT if provider_id == "claude" else "Press Connect to try again."))
        return 1
    if credentials and credentials() == saved_before:
        # Signed in in the browser, but nothing was written: the old sign-in is still what jobs would use.
        report("error", f"{name} didn't save the new sign-in, so this computer is still on the old one. "
               + (KEYCHAIN_HINT if provider_id == "claude" else "Press Connect to try again."))
        return 1
    if ready(cli):
        report("ready", f"✅ {name} is connected. Its models are on for this computer's jobs. You can close this page.")
        after()
        return 0
    report("error", f"{name} isn't signed in yet. Press Connect to try again.")
    return 1


def run_interactive_sign_in(argv: list[str], *, env: dict[str, str], ready: Callable[[], bool],
                            poll_interval: float = 0.5) -> int:
    """Keep terminal input/output live; stop only our child when its credentials appear."""
    child = subprocess.Popen(argv, env=env)
    try:
        while child.poll() is None:
            if ready():
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                return 0
            time.sleep(poll_interval)
        return child.returncode
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=sorted(ai_providers.SIGN_IN_COMMANDS))
    args = parser.parse_args(argv)
    path = ai_providers.tool_path()
    env = {**os.environ, "PATH": path, "npm_config_prefix": str(Path.home() / ".local")}

    def ready(cli: str) -> bool:
        from orchestrator import setup_checklist
        setup_checklist._cache.pop("llm", None)
        return setup_checklist._cli_ready(cli)

    def after() -> None:
        from orchestrator import job_problem
        try:
            job_problem.record_sign_in(args.provider)  # jobs that stopped on an expired sign-in stop asking for one
        except OSError:
            pass
        from orchestrator.project_setup import refresh_ready_models
        refresh_ready_models(Path(os.environ.get("ORCHESTRATOR_PROJECT_ROOT") or os.getcwd()))

    def ensure_prerequisites(provider_id: str) -> None:
        from orchestrator.tool_prerequisites import install_node
        install_node()

    def run(argv: list[str]) -> int:
        if args.provider == "agy" and Path(argv[0]).name == "agy":
            return run_interactive_sign_in(argv, env=env, ready=lambda: ready("agy"))
        return subprocess.call(argv, env=env)

    return connect(args.provider, which=lambda cli: shutil.which(cli, path=path),
                   run=run, ready=ready,
                   say=lambda line: print(f"\n{line}", flush=True), after=after,
                   ensure_prerequisites=ensure_prerequisites,
                   progress=lambda step, detail: print("@@ORCHESTRATOR_CONNECT@@" + json.dumps(
                       {"step": step, "detail": detail}), flush=True),
                   credentials=claude_credentials_state if args.provider == "claude" else None)


if __name__ == "__main__":
    sys.exit(main())
