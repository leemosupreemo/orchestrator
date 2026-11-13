"""`orchestrator sandbox`: try Orchestrator as on a brand-new Mac, again and again, without touching your real setup.

It runs the web app the way the Mac app does on a first launch (no project yet, so setup asks for one), with a throwaway
home folder at ~/.orchestrator-sandbox/<name>/home. Everything Orchestrator and the AI tools keep in a home folder lands
there instead of in yours: projects, settings, the instance lock, OpenCode and Codex sign-ins, tools installed by setup.
By default it can't see the tools you've installed either (Homebrew, ~/.local/bin, ~/.opencode/bin), so setup installs them
from scratch into the sandbox, just like on a new Mac. --reset starts over; --wipe deletes it.

What a home folder can't isolate: the macOS Keychain, where Claude Code and GitHub's gh keep their sign-ins, and things
outside your home (system-wide apps). The browser keeps its sign-in per address, so the sandbox uses its own port.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8799
BARE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"  # what a new Mac has: no Homebrew, nothing you've installed
# From your environment, only what a program needs to run normally. Nothing else carries over: no AI or GitHub keys,
# no Orchestrator settings, no PATH additions.
KEEP_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME", "SHELL", "TERM")
PACKAGE_PARENT = Path(__file__).resolve().parents[1]


def base_dir() -> Path:
    return Path(os.environ.get("ORCHESTRATOR_SANDBOX_DIR") or Path.home() / ".orchestrator-sandbox")


def _folder(name: str, base: Path | None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name or ""):
        raise ValueError("A sandbox name is letters, numbers, dots, dashes and underscores.")
    return (base or base_dir()) / name


def prepare(name: str = "default", *, base: Path | None = None, reset: bool = False) -> Path:
    """The sandbox's home folder, made if it's new; with `reset`, emptied first."""
    folder = _folder(name, base)
    home = folder / "home"
    if reset and folder.exists():
        shutil.rmtree(folder)
    home.mkdir(parents=True, exist_ok=True)
    return home


def wipe(name: str = "default", *, base: Path | None = None) -> bool:
    folder = _folder(name, base)
    if not folder.exists():
        return False
    shutil.rmtree(folder)
    return True


def environment(home: Path, *, keep_tools: bool = False, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment the sandboxed app runs in: its own HOME, and (unless `keep_tools`) none of this computer's tools."""
    source = dict(os.environ if base_env is None else base_env)
    env = {key: source[key] for key in KEEP_ENV if source.get(key)}
    env["HOME"] = str(home)
    env["ORCHESTRATOR_SANDBOX"] = str(home)
    env["PYTHONPATH"] = str(PACKAGE_PARENT)  # this copy of Orchestrator, whatever is installed on the computer
    if keep_tools:
        env["PATH"] = source.get("PATH", BARE_PATH)
    else:
        env["PATH"] = BARE_PATH
        # Tools count only once setup has installed them into the sandbox (see ai_providers.tool_path)
        env["ORCHESTRATOR_TOOL_DIRS"] = os.pathsep.join([str(home / ".opencode" / "bin"), str(home / ".local" / "bin")])
    return env


def sample_project(home: Path) -> Path:
    """A small git project inside the sandbox, for trying "Add existing project" without your real code."""
    project = home / "Projects" / "sample-app"
    if (project / ".git").exists():
        return project
    project.mkdir(parents=True, exist_ok=True)
    (project / "README.md").write_text("# Sample app\n\nA word game: players score points for the words they make.\n", encoding="utf-8")
    (project / "scoring.py").write_text('def score(word: str) -> int:\n    """One point per letter."""\n    return len(word)\n', encoding="utf-8")
    (project / "test_scoring.py").write_text("import unittest\n\nfrom scoring import score\n\n\nclass ScoreTests(unittest.TestCase):\n"
                                             "    def test_one_point_per_letter(self):\n        self.assertEqual(score(\"cat\"), 3)\n",
                                             encoding="utf-8")
    git = ["git", "-c", "user.name=Sandbox", "-c", "user.email=sandbox@example.com"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "Sample app"], cwd=project, check=True)
    return project


def start_server(home: Path, *, port: int, token: str, env: dict[str, str]) -> subprocess.Popen:
    """The web app as on a first launch of the Mac app: no project yet. Prints its port on the first line."""
    code = ("from orchestrator.web.server import UIServer\n"
            f"s = UIServer(('127.0.0.1', {int(port)}), None, token={token!r}, bootstrap_mode=True)\n"
            "print(s.server_address[1], flush=True)\ns.serve_forever()\n")
    return subprocess.Popen([sys.executable, "-P", "-c", code], cwd=home, env=env, stdout=subprocess.PIPE, text=True)


def wait_for_port(server: subprocess.Popen, timeout: float = 30) -> int:
    started = time.time()
    line = server.stdout.readline() if server.stdout else ""
    if not line.strip().isdigit():
        server.terminate()
        raise RuntimeError("The sandboxed web app didn't start." + (" It took too long." if time.time() - started > timeout else ""))
    return int(line)


def command(name: str = "default", *, reset: bool = False, wipe_only: bool = False, keep_tools: bool = False,
            sample: bool = False, port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)  # each line as it's printed, even into a file or a pipe
    except (AttributeError, ValueError):
        pass
    if wipe_only:
        print(f"Deleted the sandbox '{name}'." if wipe(name) else f"There's no sandbox called '{name}'.")
        return 0
    home = prepare(name, reset=reset)
    fresh = not any(home.iterdir())
    project = sample_project(home) if sample else None
    token = secrets.token_urlsafe(18)
    server = start_server(home, port=port, token=token, env=environment(home, keep_tools=keep_tools))
    try:
        actual = wait_for_port(server)
    except RuntimeError as exc:
        print(exc)
        return 1
    url = f"http://localhost:{actual}/?token={token}"
    print(f"Orchestrator sandbox '{name}'{' (fresh)' if fresh else ''}: {url}")
    print(f"  Home folder: {home}")
    print("  Your projects, settings, sign-ins and accounts aren't visible here, and nothing here changes them.")
    print("  " + ("Your installed tools are visible (--keep-tools)." if keep_tools
                  else "Your installed tools are hidden: setup installs what it needs into the sandbox."))
    if project:
        print(f"  Sample project to add: {project}")
    print("  Not isolated: the macOS Keychain (Claude Code and GitHub keep sign-ins there).")
    print(f"  Start over: orchestrator sandbox --reset{'' if name == 'default' else f' --name {name}'}   Delete: --wipe   Stop: Ctrl+C")
    if open_browser:
        webbrowser.open(url)
    try:
        return server.wait()
    except KeyboardInterrupt:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        print("\nSandbox stopped. It's kept as it was: run the same command to carry on, or add --reset to start over.")
        return 0
