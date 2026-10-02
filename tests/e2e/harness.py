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
USE_CASES = """# Users, use cases and non-goals

## Primary user

Two friends who want a quick word game they can play when they each have a minute.

## Core use cases

- As a player, I can score a word so that I know how many points it is worth
- As a player, I can compare two words so that I can see which is worth more

## Edge cases that matter

- Empty words and words with punctuation must not crash

## Non-goals for version 1

- No accounts or logins
- No chat
- No network play: this is a library only
"""
BRIEF = """# Word Duel: product brief

## What it is

A tiny word-scoring library for a two-player word game.

## Who it's for

Two friends playing casually.

## The problem

Scoring words by hand is slow and argued over.

## Version 1 must do

1. Score a word
2. Compare two words
"""


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

    def create(self, with_product_docs: bool = True) -> "DummyProject":
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
            from orchestrator import product_docs
            product_docs.write(self.root, "brief", BRIEF)
            product_docs.scaffold(self.root)
            product_docs.write(self.root, "use-cases", USE_CASES)
        runtime = self.root / ".orchestrator"
        (runtime / "config").mkdir(parents=True)
        (runtime / "project.json").write_text(json.dumps({
            "project_name": "Word Duel", "base_branch": "main", "pr_base_branch": "main", "branch_prefix": "ai/issue", "git_remote": str(self.remote),
            "build_command": "python3 -m compileall -q wordgame", "test_command": "python3 -m unittest discover -s tests", "firebase_distribution": False}, indent=2))
        (runtime / "config" / "machines.json").write_text(json.dumps({"version": 1, "machines": [{
            "name": "local", "enabled": True, "execution_mode": "local", "ssh_target": None, "repo_path": str(self.root), "roles": ["planner", "reviewer", "worker", "build", "test"],
            "models": self.models, "priority": 100, "max_concurrent_jobs": 1, "max_heavy_jobs": 1, "supports_xcode": False, "supports_simulator": False,
            "supports_backend_tests": True, "interactive_reserved": False, "tags": []}]}, indent=2))
        (runtime / "config" / "settings.json").write_text(json.dumps({"setup_seen": True, "notification_emails": []}))
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
        return subprocess.run([PYTHON, "-m", "unittest", "discover", "-s", "tests"], cwd=self.root, capture_output=True, text=True)
