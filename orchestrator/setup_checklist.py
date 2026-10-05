"""What has to be set up before the orchestrator can run a job, as data.

One list feeds the web UI's setup checklist. Each item is `required` or
optional, says whether it's done, and says how to fix it. "Required" matches
what a job actually needs: a git repository; with GitHub (`code_host`) a GitHub
remote and a signed-in `gh`, because each job is an issue and finishing opens a
PR; every planning/build step calls an LLM; and work runs on a machine listed
in machines.json. With plain git (GitLab, Bitbucket, no remote) no GitHub is needed.
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

from orchestrator import ai_providers

API_KEY_ENV = {key: env for key, _label, env in ai_providers.API_KEYS}  # one list of providers: orchestrator/ai_providers.py
LLM_CLIS = ai_providers.CLIS
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


def saved_api_keys(settings: dict[str, Any]) -> set[str]:
    """The AI provider keys set in settings or the environment (by setting name, e.g. "openai_api_key")."""
    return {key for key, env in API_KEY_ENV.items() if settings.get(key) or os.environ.get(env)}


def ready_llm_providers(settings: dict[str, Any]) -> list[str]:
    def probe() -> list[str]:
        with ThreadPoolExecutor(max_workers=len(LLM_CLIS)) as pool:
            results = list(pool.map(_cli_ready, LLM_CLIS))
        return [cli for cli, ok in zip(LLM_CLIS, results) if ok]
    found = list(_cached("llm", 60, probe))
    found += [f"{key.split('_')[0]} API key" for key in API_KEY_ENV if key in saved_api_keys(settings)]
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
    # Every fix is a page in the app (or the in-app GitHub sign-in), never a terminal menu or the terminal wizard.
    page = lambda to: {"type": "route", "to": to}  # noqa: E731

    is_git = (root / ".git").exists()
    origin = (_run(["git", "remote", "get-url", "origin"], cwd=root) or subprocess.CompletedProcess([], 1, "", "")).stdout.strip() if is_git else ""
    from orchestrator.code_host import host_kind, resolve_mode
    github = resolve_mode(project.get("code_host"), origin) == "github"
    gh = github_cli_state()
    providers = ready_llm_providers(settings)
    ssh_machines = sum(1 for m in machines if m.get("execution_mode") == "ssh")
    models = list(dict.fromkeys(m for mach in machines if mach.get("enabled", True) for m in mach.get("models", [])))

    items = [
        _item("project", "Project configured", True, bool(project), "Name, build and test settings" if project else "No .orchestrator/project.json yet",
              action=page("#/setup"), group="Project"),
        _item("git", "Git repository", True, is_git, "Initialized" if is_git else "This folder isn't a git repository",
              action=None if is_git else {"type": "git_init", "label": "Initialize"},
              hint=None if is_git else "git init", group="GitHub" if github else "Git"),
    ]
    if github:
        items += [
            _item("github_remote", "GitHub remote (origin)", True, bool(GITHUB_REMOTE.search(origin)),
                  origin if GITHUB_REMOTE.search(origin) else ("origin isn't a GitHub URL" if origin else "No origin remote"),
                  action={"type": "github_create", "label": "Create repo"} if (not GITHUB_REMOTE.search(origin) and gh.get("user")) else None,
                  hint=None if GITHUB_REMOTE.search(origin) else "gh repo create --source . --push", group="GitHub"),
            _item("github_cli", "GitHub CLI signed in", True, bool(gh["user"]),
                  f"Signed in as {gh['user']}" if gh["user"] else ("Installed, not signed in" if gh["installed"] else "GitHub CLI (gh) isn't installed"),
                  action={"type": "github"} if gh["installed"] and not gh["user"] else None,
                  hint=None if gh["installed"] else "brew install gh", group="GitHub"),
        ]
    else:
        host = {"gitlab": "GitLab", "bitbucket": "Bitbucket", "azure": "Azure DevOps", "github": "GitHub"}.get(host_kind(origin), "")
        remote_action = {"type": "github_create", "label": "Create repo"} if (is_git and not origin and gh.get("user")) else None
        items.append(_item("remote", "Git remote (origin)", False, bool(origin),
                           f"{host + ': ' if host else ''}{origin}. Finished jobs are pushed here as branches." if origin
                           else "None: finished work stays on this computer as branches.",
                           action=remote_action,
                           hint=None if origin else "git remote add origin <url>", group="Git"))
    items += [
        _item("llm", "An AI provider is ready", True, bool(providers),
              ", ".join(providers[:4]) if providers else "No AI is set up yet. Free options are available.",
              action={"type": "route", "to": "#/config/ai"}, group="AI"),
        _item("machines", "A machine to run jobs on", True, bool(machines),
              (f"{len(machines)} configured, {ssh_machines} remote over SSH" if ssh_machines else f"{len(machines)} configured. Add other Macs to run jobs side by side.")
              if machines else "machines.json is missing or empty",
              action=page("#/config/fleet"), group="AI"),
        _item("models", "At least one model selected", True, bool(models),
              f"{len(models)} selected" if models else "No models are assigned to a machine",
              action=page("#/config/models"), group="AI"),
        _item("docs", "Grounding docs (AGENTS.md, docs/)", False,
              (root / "AGENTS.md").exists() and (root / "docs" / "architecture.md").exists(),
              "Give the AI the project's rules and architecture", action=page("#/config/ai-instructions"), group="Recommended"),
        _item("connections", "Connect Jira, Trello, Sentry or Figma", False, bool(settings.get("integrations")),
              "Tie jobs to tickets, pull in error logs and designs", action={"type": "route", "to": "#/connections"}, group="Optional"),
        _item("firebase", "Firebase delivery to testers", False, bool(project.get("firebase_distribution")),
              "Send builds to testers after a job", action=page("#/config/firebase"), group="Optional"),
        _item("email", "Email notifications", False, bool(settings.get("notification_emails")),
              "Get told when a job finishes or needs you", action=page("#/config/email"), group="Optional"),
        _item("prompts", "Custom role prompts", False, (runtime / "prompts").is_dir() and any((runtime / "prompts").iterdir()),
              "Tune how the planner, builder and reviewer behave", action=page("#/config/ai-instructions"), group="Optional"),
    ]
    required = [i for i in items if i["required"]]
    return {
        "items": items,
        "required_total": len(required),
        "required_done": sum(1 for i in required if i["done"]),
        "complete": all(i["done"] for i in required),
        "seen": bool(settings.get("setup_seen")),
    }
