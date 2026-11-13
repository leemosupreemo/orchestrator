"""A throwaway project to run the real orchestrator against, end to end.

`DummyProject` builds a small Python project in a temp folder (a git repo with a local bare
remote, a passing test suite, the product documents, and orchestrator config that allows only
the models you name). `gh` is replaced by `fake_gh.py` on PATH, so issues and pull requests are
recorded in a file and nothing reaches GitHub. Everything else is the real code: the planner,
worker, builder, test runner, reviewer and the product-document plumbing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PYTHON = sys.executable

SCORING = '''"""Scores for the word game."""

LETTER_POINTS = {c: 1 for c in "aeioulnrst"} | {c: 2 for c in "dg"} | {c: 3 for c in "bcmp"} | {c: 4 for c in "fhvwy"} | {"k": 5} | {c: 8 for c in "jx"} | {c: 10 for c in "qz"}


def score(word: str) -> int:
    """Points for a word: the sum of its letters' points."""
    return sum(LETTER_POINTS.get(c, 0) for c in word.lower())
'''
SCORING_TESTS = '''import unittest

from wordgame.scoring import score


class ScoreTests(unittest.TestCase):
    def test_simple_word(self):
        self.assertEqual(score("tea"), 3)

    def test_case_does_not_matter(self):
        self.assertEqual(score("TEA"), score("tea"))

    def test_unknown_characters_score_nothing(self):
        self.assertEqual(score("t-e!"), 2)


if __name__ == "__main__":
    unittest.main()
'''
PRD_PITCH = "A tiny word-scoring library for a two-player word game.\n\nBuilt for: Python library"
PRD_WHO = "Two friends playing casually, a minute at a time."
PRD_FEATURES = "- As a player, I can score a word so that I know how many points it is worth\n- As a player, I can compare two words so that I can see which is worth more"
PRD_NOT = "- No accounts or logins\n- No chat\n- No network play: this is a library only"


def prd_text() -> str:
    from orchestrator import prd
    text = prd.template("Word Duel")
    for section, body in (("pitch", PRD_PITCH), ("who", PRD_WHO), ("features", PRD_FEATURES), ("not", PRD_NOT)):
        text = prd.replace_section(text, section, body)
    return text


class DummyProject:
    def __init__(self, models: list[str], base: Path | None = None):
        self._tmp = None if base else tempfile.TemporaryDirectory()
        self.base = Path(base or self._tmp.name).resolve()
        self.root = self.base / "wordgame-project"
        self.remote = self.base / "remote.git"
        self.bin = self.base / "bin"
        self.state_file = self.base / "fake-gh-state.json"
        self.user_state = self.base / "user-state"
        self.models = models

    @classmethod
    def open(cls, base: Path, models: list[str]) -> "DummyProject":
        """Pick up a project that `create` already built (for stepping through a run in pieces)."""
        return cls(models, base=Path(base))

    # ------------------------------------------------------------------ setup

    def git(self, *args: str, check: bool = True) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "E2E", "GIT_AUTHOR_EMAIL": "e2e@example.com", "GIT_COMMITTER_NAME": "E2E", "GIT_COMMITTER_EMAIL": "e2e@example.com"}
        res = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True, env=env)
        if check and res.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
        return res.stdout.strip()

    def create(self, with_product_docs: bool = True, code_host: str = "github", configured: bool = True) -> "DummyProject":
        """`configured=False` leaves out .orchestrator/, as someone's existing code is before they set it up in the app."""
        (self.root / "wordgame").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "wordgame" / "__init__.py").write_text("")
        (self.root / "wordgame" / "scoring.py").write_text(SCORING)
        (self.root / "tests" / "test_scoring.py").write_text(SCORING_TESTS)
        (self.root / "README.md").write_text("# Word Duel\n\nA tiny word-scoring library.\n")
        (self.root / "AGENTS.md").write_text("# Instructions\n\n- Python 3, standard library only.\n- Tests: `python3 -m unittest discover -s tests`.\n- Keep changes small.\n")
        (self.root / ".gitignore").write_text("__pycache__/\n*.pyc\n.orchestrator/\n")
        if with_product_docs:
            sys.path.insert(0, str(REPO))
            (self.root / "docs" / "product").mkdir(parents=True)
            (self.root / "docs" / "product" / "prd.md").write_text(prd_text())
        if configured:
            self._write_config(code_host)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Initial project")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "-u", "origin", "main")
        self.bin.mkdir()
        gh = self.bin / "gh"
        gh.write_text(f'#!/bin/sh\nexec "{PYTHON}" "{HERE / "fake_gh.py"}" "$@"\n')
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        return self

    def _write_config(self, code_host: str) -> None:
        runtime = self.root / ".orchestrator"
        (runtime / "config").mkdir(parents=True)
        (runtime / "project.json").write_text(json.dumps({
            "project_name": "Word Duel", "base_branch": "main", "pr_base_branch": "main", "branch_prefix": "ai/issue", "git_remote": str(self.remote),
            # GitHub's pipeline by default (gh is faked; the local remote isn't on github.com), or "git" for plain git.
            "code_host": code_host,
            "build_command": "python3 -m compileall -q wordgame", "test_command": "python3 -m unittest discover -s tests", "firebase_distribution": False}, indent=2))
        (runtime / "config" / "machines.json").write_text(json.dumps({"version": 1, "machines": [{
            "name": "local", "enabled": True, "execution_mode": "local", "ssh_target": None, "repo_path": str(self.root), "roles": ["planner", "reviewer", "worker", "build", "test"],
            "models": self.models, "priority": 100, "max_concurrent_jobs": 1, "max_heavy_jobs": 1, "supports_xcode": False, "supports_simulator": False,
            "supports_backend_tests": True, "interactive_reserved": False, "tags": []}]}, indent=2))
        (runtime / "config" / "settings.json").write_text(json.dumps({"setup_seen": True, "notification_emails": []}))

    def fake_models(self, behaviour: str = "") -> "DummyProject":
        """Put a scripted `opencode` on PATH, so free-model ids work offline. Calls are recorded (see `llm_calls`)."""
        cli = self.bin / "opencode"
        cli.write_text(f'#!/bin/sh\nexec "{PYTHON}" "{HERE / "fake_opencode.py"}" "$@"\n')
        cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
        self.llm_state = self.base / "fake-llm-state.json"
        self.llm_behaviour = behaviour
        return self

    def llm_calls(self) -> list[dict[str, Any]]:
        return json.loads(self.llm_state.read_text())["calls"] if getattr(self, "llm_state", None) and self.llm_state.exists() else []

    def cleanup(self) -> None:
        if self._tmp:
            self._tmp.cleanup()

    # ------------------------------------------------------------------ running

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({"PATH": f"{self.bin}{os.pathsep}{env['PATH']}", "FAKE_GH_STATE": str(self.state_file), "ORCHESTRATOR_PROJECT_ROOT": str(self.root),
                    "ORCHESTRATOR_USER_STATE_DIR": str(self.user_state), "ORCHESTRATOR_DISABLE_NOTIFICATIONS": "1", "ORCHESTRATOR_MIN_DISK_GB": "1", "PYTHONPATH": str(REPO),
                    "GIT_AUTHOR_NAME": "E2E", "GIT_AUTHOR_EMAIL": "e2e@example.com", "GIT_COMMITTER_NAME": "E2E", "GIT_COMMITTER_EMAIL": "e2e@example.com"})
        if getattr(self, "llm_state", None):
            env.update({"FAKE_LLM_STATE": str(self.llm_state), "FAKE_LLM_BEHAVIOUR": self.llm_behaviour})
        return env

    def orchestrator(self, *args: str, input: str = "", timeout: int = 600) -> subprocess.CompletedProcess:
        """Run `python -m orchestrator ...` inside the project, as a person would."""
        return subprocess.run([PYTHON, "-P", "-m", "orchestrator", *args], cwd=self.root, env=self.env(), input=input, capture_output=True, text=True, timeout=timeout)

    # ------------------------------------------------------------------ looking

    def gh(self) -> dict[str, Any]:
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else {"issues": {}, "prs": {}, "calls": [], "unsupported": []}

    def jobs(self) -> list[Path]:
        return sorted((self.root / ".orchestrator" / "jobs").glob("*.json"))  # the worker's per-task scratch copies are deleted when it finishes

    def job(self) -> dict[str, Any]:
        files = self.jobs()
        return json.loads(files[-1].read_text()) if files else {}

    def run_tests(self) -> subprocess.CompletedProcess:
        # No bytecode cache: tests switch branches within one second, and a .pyc from the previous branch can look current.
        return subprocess.run([PYTHON, "-m", "unittest", "discover", "-s", "tests"], cwd=self.root, capture_output=True, text=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


# The AI tools a fresh Mac might have installed. Each is replaced by a stub that records how it was called and fails, so it
# looks installed but signed out, and nothing can reach a real model. (Job commands put /opt/homebrew/bin and friends on PATH;
# the stubs come first.) OpenCode is the one tool set up: the scripted fake.
REAL_AI_CLIS = ("claude", "codex", "gemini", "agy", "antigravity", "ollama", "pool", "hermes", "qwen", "cursor-agent", "copilot")
# What the app may run on its own to see whether a tool is signed in. Anything else reaching a stub is a model call.
SIGN_IN_CHECKS = {"claude auth status", "codex login status", "ollama list", "opencode models", "pool config", "hermes --version"}


class FreshInstall:
    """Orchestrator on a computer that has never run it, driven through the web app's own API.

    An empty home folder and a minimal environment (no API keys from the person running the tests), someone's existing code
    with no Orchestrator config, and the web server started the way the Mac app starts it: bootstrap mode, no project yet.
    Requests carry the page's headers; payloads are the ones its forms send."""

    TOKEN = "e2e-fresh-install"

    def __init__(self, behaviour: str = ""):
        self.project = DummyProject([]).create(configured=False).fake_models(behaviour)
        self.base = self.project.base
        self.home = self.base / "home"
        self.home.mkdir()
        self.forbidden = self.base / "real-cli-calls.log"
        for cli in REAL_AI_CLIS:
            stub = self.project.bin / cli
            stub.write_text(f'#!/bin/sh\necho "{cli} $*" >> "{self.forbidden}"\nexit 1\n')
            stub.chmod(0o755)
        # Nothing is connected yet: until `connect_opencode` (after the project is set up), OpenCode is a stub like the others.
        # (It can't simply be missing: a real one in /opt/homebrew/bin would be found instead.)
        self._opencode = self.project.bin / "opencode"
        self._opencode.rename(self.base / "opencode.fake")
        self._opencode.write_text(f'#!/bin/sh\necho "opencode $*" >> "{self.forbidden}"\nexit 1\n')
        self._opencode.chmod(0o755)
        self.server: subprocess.Popen | None = None

    def connect_opencode(self) -> None:
        """Set up OpenCode, the free tool: it needs no account, so working means ready."""
        (self.base / "opencode.fake").replace(self._opencode)

    def env(self) -> dict[str, str]:
        p = self.project
        path = os.pathsep.join([str(p.bin), "/opt/homebrew/bin", "/usr/local/bin", str(self.home / ".local" / "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        return {"HOME": str(self.home), "PATH": path, "PYTHONPATH": str(REPO), "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "LANG": "en_US.UTF-8",
                "FAKE_GH_STATE": str(p.state_file), "FAKE_LLM_STATE": str(p.llm_state), "FAKE_LLM_BEHAVIOUR": p.llm_behaviour,
                "ORCHESTRATOR_DISABLE_NOTIFICATIONS": "1", "ORCHESTRATOR_MIN_DISK_GB": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "GIT_AUTHOR_NAME": "E2E", "GIT_AUTHOR_EMAIL": "e2e@example.com", "GIT_COMMITTER_NAME": "E2E", "GIT_COMMITTER_EMAIL": "e2e@example.com"}

    def start(self) -> "FreshInstall":
        code = ("from orchestrator.web.server import UIServer\n"
                f"s = UIServer(('127.0.0.1', 0), None, token={self.TOKEN!r}, bootstrap_mode=True)\n"
                "print(s.server_address[1], flush=True)\ns.serve_forever()\n")
        self.server_log = self.base / "server.log"
        self._log = self.server_log.open("w")
        self.server = subprocess.Popen([PYTHON, "-P", "-c", code], cwd=self.home, env=self.env(), stdout=subprocess.PIPE, stderr=self._log, text=True)
        line = self.server.stdout.readline()
        if not line.strip().isdigit():
            raise RuntimeError(f"The web server didn't start:\n{self.server_log.read_text()[-2000:]}")
        self.url = f"http://127.0.0.1:{int(line)}/api/"
        return self

    def stop(self) -> None:
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(10)
            except subprocess.TimeoutExpired:
                self.server.kill()
        if self.server and self.server.stdout:
            self.server.stdout.close()
        if getattr(self, "_log", None):
            self._log.close()
        self.project.cleanup()

    def api(self, path: str, body: dict | None = None) -> tuple[int, dict[str, Any]]:
        import urllib.error
        import urllib.request
        request = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST",
                                         headers={"Authorization": f"Bearer {self.TOKEN}", "X-Orchestrator-UI": "1", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def run(self, action: str, params: dict[str, Any], timeout: float = 120) -> dict[str, Any]:
        """Start an action the way the page's buttons do (POST /api/runs), follow its output to the end, and return the run
        as the page sees it, with its full output under "output"."""
        import base64
        import time
        status, started = self.api("runs", {"action": action, "params": params})
        if status != 201:
            raise RuntimeError(f"{action} didn't start ({status}): {started}")
        run_id, offset, output, deadline = started["run"]["id"], 0, b"", time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, chunk = self.api(f"runs/{run_id}/output?offset={offset}&wait=10")
            output += base64.b64decode(chunk["data"])
            offset = chunk["offset"]
            if chunk["finished"]:
                break
        else:
            raise RuntimeError(f"{action} was still running after {timeout}s:\n{output.decode(errors='replace')[-2000:]}")
        view = next(r for r in self.api("runs")[1]["runs"] if r["id"] == run_id)
        return {**view, "output": re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[78]", "", output.decode(errors="replace"))}

    def jobs(self) -> list[dict[str, Any]]:
        return self.api("jobs")[1]["jobs"]

    def real_cli_calls(self) -> list[str]:
        """Every time a stubbed AI tool was run for anything other than a sign-in check."""
        lines = self.forbidden.read_text().splitlines() if self.forbidden.exists() else []
        return [line for line in lines if line.strip() not in SIGN_IN_CHECKS]
