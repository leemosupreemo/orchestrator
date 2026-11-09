#!/usr/bin/env python3
"""Run a web project's dev server on this computer, so a branch can be previewed live while it changes.

It runs where the branch is checked out (the project folder, or another worktree of it); otherwise it checks the branch out
in a preview folder outside the project (so the project's tests and file watchers don't see it) and keeps it, so the next
start is quick. Installing packages never writes a lockfile into the project. When the dev server
prints its address, the address is saved in .orchestrator/output/dev-previews/ for the job page to show; it's removed again
when the server stops.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from orchestrator.scripts.web_preview import channel_name, git

ADDRESS = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):\d+[^\s\x1b'\"]*")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def checked_out_in(root: Path, branch: str) -> Path | None:
    """The folder where the branch is checked out, if any (git keeps a branch in one place at a time)."""
    path = None
    for line in git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


def dev_commands(folder: Path, config: dict[str, Any]) -> tuple[list[list[str]], list[str]]:
    """(setup, server): installing packages when they're missing, then `preview_dev_command` from project.json, else the
    package's dev or start script, else a plain file server for a site without a build."""
    if config.get("preview_dev_command"):
        return [], ["/bin/sh", "-c", str(config["preview_dev_command"])]
    try:
        scripts = json.loads((folder / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        scripts = None
    if scripts is None:
        return [], [sys.executable, "-u", "-m", "http.server", "--bind", "127.0.0.1", "0"]
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
                             start_new_session=True, env={**os.environ, "BROWSER": "none", "FORCE_COLOR": "0"})

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
                    state.parent.mkdir(parents=True, exist_ok=True)
                    state.write_text(json.dumps({"branch": args.branch, "url": found, "pid": os.getpid(), "at": time.time()}))
                    print(f"✅ Live preview: {found}", flush=True)
        return child.wait()
    except KeyboardInterrupt:
        stop()
        return 130
    finally:
        stop()
        state.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
