"""What has to be set up before the orchestrator can run a job, as data.

One list feeds the web UI's setup checklist. Each item is `required` or
optional, says whether it's done, and says how to fix it. "Required" matches
what a job actually needs: `new job` opens a GitHub issue (its number becomes the
job id) and finishing opens a PR, every planning/build step calls an LLM, and
work runs on a machine listed in machines.json.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

API_KEY_ENV = {"gemini_api_key": "GEMINI_API_KEY", "anthropic_api_key": "ANTHROPIC_API_KEY",
               "openai_api_key": "OPENAI_API_KEY", "ollama_api_key": "OLLAMA_API_KEY"}
LLM_CLIS = ["claude", "codex", "agy", "gemini", "opencode", "ollama"]
GITHUB_REMOTE = re.compile(r"github\.com[:/]")

_cache: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def _run(argv: list[str], cwd: Path | None = None, timeout: int = 6) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None


def github_cli_state(fresh: bool = False) -> dict[str, Any]:
    """{'installed': bool, 'user': str | None}. `fresh` skips the short-lived cache."""
    if fresh:
        _cache.pop("gh", None)

    def probe() -> dict[str, Any]:
        if not shutil.which("gh"):
            return {"installed": False, "user": None}
        res = _run(["gh", "auth", "status", "--hostname", "github.com"])
        text = (res.stdout + res.stderr) if res else ""
        match = re.search(r"Logged in to github\.com (?:as|account) (\S+)", text)
        return {"installed": True, "user": match.group(1) if match and res.returncode == 0 else None}
    return _cached("gh", 20, probe)


def _cli_ready(cli: str) -> bool:
    if not shutil.which(cli):
        return False
    commands = {"claude": ["claude", "auth", "status"], "codex": ["codex", "login", "status"],
                "opencode": ["opencode", "models"]}
    if cli in commands:
        res = _run(commands[cli], timeout=5)
        return bool(res and res.returncode == 0 and (cli != "opencode" or res.stdout.strip()))
    if cli in ("agy", "gemini"):
        home = Path.home() / ".gemini"
        return (home / "oauth_creds.json").exists() or (home / "google_accounts.json").exists()
    if cli == "ollama":
        res = _run(["ollama", "list"], timeout=3)
        return bool(res and res.returncode == 0)
    return True


def ready_llm_providers(settings: dict[str, Any]) -> list[str]:
    def probe() -> list[str]:
        with ThreadPoolExecutor(max_workers=len(LLM_CLIS)) as pool:
            results = list(pool.map(_cli_ready, LLM_CLIS))
        return [cli for cli, ok in zip(LLM_CLIS, results) if ok]
    found = list(_cached("llm", 60, probe))
    found += [f"{key.split('_')[0]} API key" for key, env in API_KEY_ENV.items() if settings.get(key) or os.environ.get(env)]
    return found


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _item(id: str, title: str, required: bool, done: bool, detail: str, *, action: dict | None = None,
          hint: str | None = None, group: str = "") -> dict[str, Any]:
    return {"id": id, "title": title, "required": required, "done": done, "detail": detail,
            "action": action, "hint": hint, "group": group}


def setup_checklist(root: Path, runtime: Path) -> dict[str, Any]:
    project = _read_json(runtime / "project.json")
    settings = _read_json(runtime / "config" / "settings.json")
    machines = [m for m in _read_json(runtime / "config" / "machines.json").get("machines", []) if isinstance(m, dict)]
    wizard = {"type": "run", "action": "wizard"}
    menu = lambda name: {"type": "run", "action": "config_menu", "params": {"menu": name}}  # noqa: E731

    is_git = (root / ".git").exists()
    origin = (_run(["git", "remote", "get-url", "origin"], cwd=root) or subprocess.CompletedProcess([], 1, "", "")).stdout.strip() if is_git else ""
    gh = github_cli_state()
    providers = ready_llm_providers(settings)
    models = list(dict.fromkeys(m for mach in machines if mach.get("enabled", True) for m in mach.get("models", [])))

    items = [
        _item("project", "Project configured", True, bool(project), "Name, build and test settings" if project else "No .orchestrator/project.json yet",
              action=wizard, group="Project"),
        _item("git", "Git repository", True, is_git, "Initialized" if is_git else "This folder isn't a git repository",
              hint=None if is_git else "git init", group="GitHub"),
        _item("github_remote", "GitHub remote (origin)", True, bool(GITHUB_REMOTE.search(origin)),
              origin if GITHUB_REMOTE.search(origin) else ("origin isn't a GitHub URL" if origin else "No origin remote"),
              hint=None if GITHUB_REMOTE.search(origin) else "gh repo create --source . --push", group="GitHub"),
        _item("github_cli", "GitHub CLI signed in", True, bool(gh["user"]),
              f"Signed in as {gh['user']}" if gh["user"] else ("Installed, not signed in" if gh["installed"] else "GitHub CLI (gh) isn't installed"),
              action=menu("github") if gh["installed"] and not gh["user"] else None,
              hint=None if gh["installed"] else "brew install gh", group="GitHub"),
        _item("llm", "An AI provider is ready", True, bool(providers),
              ", ".join(providers[:4]) if providers else "No logged-in AI CLI or API key found",
              action={"type": "route", "to": "#/config"}, group="AI"),
        _item("machines", "A machine to run jobs on", True, bool(machines),
              f"{len(machines)} configured" if machines else "machines.json is missing or empty",
              action=wizard, group="AI"),
        _item("models", "At least one model selected", True, bool(models),
              f"{len(models)} selected" if models else "No models are assigned to a machine",
              action=wizard, group="AI"),
        _item("docs", "Grounding docs (AGENTS.md, docs/)", False,
              (root / "AGENTS.md").exists() and (root / "docs" / "architecture.md").exists(),
              "Give the AI the project's rules and architecture", action={"type": "route", "to": "#/config"}, group="Recommended"),
        _item("connections", "Connect Jira, Trello, Sentry or Figma", False, bool(settings.get("integrations")),
              "Tie jobs to tickets, pull in error logs and designs", action={"type": "route", "to": "#/connections"}, group="Optional"),
        _item("firebase", "Firebase delivery to testers", False, bool(project.get("firebase_distribution")),
              "Send builds to testers after a job", action=menu("firebase"), group="Optional"),
        _item("email", "Email notifications", False, bool(settings.get("notification_emails")),
              "Get told when a job finishes or needs you", action={"type": "route", "to": "#/config"}, group="Optional"),
        _item("workers", "Remote SSH workers", False, any(m.get("execution_mode") == "ssh" for m in machines),
              "Run jobs on other Macs", action=menu("fleet"), group="Optional"),
        _item("prompts", "Custom role prompts", False, (runtime / "prompts").is_dir() and any((runtime / "prompts").iterdir()),
              "Tune how the planner, builder and reviewer behave", action=wizard, group="Optional"),
    ]
    required = [i for i in items if i["required"]]
    return {
        "items": items,
        "required_total": len(required),
        "required_done": sum(1 for i in required if i["done"]),
        "complete": all(i["done"] for i in required),
        "seen": bool(settings.get("setup_seen")),
    }
