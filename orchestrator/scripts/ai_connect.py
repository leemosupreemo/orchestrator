#!/usr/bin/env python3
"""Connect an AI tool for someone who doesn't know what a terminal is. Run by the AI tools page's Connect button, in
Orchestrator's own terminal: install the tool if it's missing (with whichever of its installers can run here), run its
sign-in (which opens the browser to approve), then check it really is signed in and say so plainly. When it worked, this
computer's models follow at once, so jobs can use it straight away.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
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


def connect(provider_id: str, *, which: Callable[[str], str | None], run: Callable[[list[str]], int],
            ready: Callable[[str], bool], say: Callable[[str], None], after: Callable[[], None]) -> int:
    if provider_id not in ai_providers.SIGN_IN_COMMANDS:
        raise ValueError("That AI tool has no sign-in to connect.")
    provider = next(p for p in ai_providers.PROVIDERS if p["id"] == provider_id)
    name, cli = provider["name"], provider["cli"]
    say(f"Connecting {name}.")
    path = which(cli)
    if not path:
        command, missing = _installer(provider, which)
        if not command:
            needed = " or ".join(ai_providers.MISSING_NEEDS.get(m, m) for m in missing)
            say(f"{name} installs with {needed}, which isn't on this computer yet. Install it, then press Connect again.")
            return 1
        say(f"Installing {name}. This can take a minute or two. If it asks you to agree to its terms, type y and press Enter.")
        if run(["bash", "-lc", command]) != 0:
            say(f"{name} didn't install (see above). Press Connect to try again, or see {provider.get('link', 'its website')}.")
            return 1
        path = which(cli)
        if not path:
            say(f"{name} installed, but it can't be found on this computer. Restart Orchestrator, then press Connect again.")
            return 1
    say(f"Signing in to {name}. "
        + (ai_providers.SIGN_IN_HINTS.get(provider_id) or "Your browser opens to approve: sign in there, then come back here.")
        + (" When it says you're signed in, press Ctrl+C here." if not ai_providers.SIGN_IN_COMMANDS[provider_id] else ""))
    run([path, *ai_providers.SIGN_IN_COMMANDS[provider_id]])
    if ready(cli):
        say(f"✅ {name} is connected. Its models are on for this computer's jobs. You can close this page.")
        after()
        return 0
    say(f"{name} isn't signed in yet. Press Connect to try again.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=sorted(ai_providers.SIGN_IN_COMMANDS))
    args = parser.parse_args(argv)
    path = ai_providers.tool_path()
    env = {**os.environ, "PATH": path}

    def ready(cli: str) -> bool:
        from orchestrator import setup_checklist
        setup_checklist._cache.pop("llm", None)
        return setup_checklist._cli_ready(cli)

    def after() -> None:
        from orchestrator.project_setup import refresh_ready_models
        refresh_ready_models(Path(os.environ.get("ORCHESTRATOR_PROJECT_ROOT") or os.getcwd()))

    return connect(args.provider, which=lambda cli: shutil.which(cli, path=path),
                   run=lambda argv: subprocess.call(argv, env=env), ready=ready,
                   say=lambda line: print(f"\n{line}", flush=True), after=after)


if __name__ == "__main__":
    sys.exit(main())
