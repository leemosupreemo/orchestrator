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


_gh_git_wired = False


def ensure_gh_git_credentials() -> bool:
    """Wire `gh` as git's HTTPS credential helper when `gh` is signed in."""
    global _gh_git_wired
    if _gh_git_wired or not shutil.which("gh"):
        return _gh_git_wired
    res = _run(["gh", "auth", "setup-git", "--hostname", "github.com"], timeout=5)
    if res and res.returncode == 0:
        _gh_git_wired = True
    return _gh_git_wired


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
        user = match.group(1) if match and res.returncode == 0 else None
        if user:
            ensure_gh_git_credentials()
        return {"installed": True, "user": user}
    return _cached("gh", 20, probe)


def _cli_ready(cli: str) -> bool:
    found = shutil.which(cli, path=ai_providers.tool_path())  # also where installers put tools, e.g. OpenCode's ~/.opencode/bin
    if not found:
        return False
    commands = {"claude": [found, "auth", "status"], "codex": [found, "login", "status"],
                "opencode": [found, "models"]}
    if cli in commands:
        res = _run(commands[cli], timeout=5)
        if not res or res.returncode != 0:
            return False
        text = ((res.stdout or "") + "\n" + (res.stderr or "")).lower()
        if cli == "opencode" and not (res.stdout or "").strip():
            return False
        if any(marker in text for marker in ("expired", "re-authenticate", "not logged in", "logged out")):
            return False
        return True
    if cli == "gemini":
        return False  # Google-account access to Gemini CLI has ended; only a Gemini API key (counted separately) works
    if cli == "agy":
        home = Path.home() / ".gemini"
        return (home / "oauth_creds.json").exists() or (home / "google_accounts.json").exists()
    if cli == "ollama":
        res = _run([found, "list"], timeout=3)
        return bool(res and res.returncode == 0)
    if cli == "pool":
        # `pool login` has no status subcommand; readiness is signaled by the credentials file
        # that `pool login` writes (the path `pool config` reports, honouring XDG_CONFIG_HOME).
        config_dir = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "poolside"
        return (config_dir / "credentials.json").exists()
    return True


def saved_api_keys(settings: dict[str, Any]) -> set[str]:
    """The AI provider keys set in settings or the environment (by setting name, e.g. "openai_api_key")."""
    return {key for key, env in API_KEY_ENV.items() if settings.get(key) or os.environ.get(env)}


def _cli_auth_rejected(cli: str) -> bool:
    """True when `cli` recently returned an auth/token-expired rejection and hasn't been signed in again since."""
    try:
        from orchestrator.project_chat import rejected_clis
        target = "gemini" if cli == "agy" else cli
        return target in rejected_clis()
    except Exception:
        return False


def ready_llm_providers(settings: dict[str, Any]) -> list[str]:
    def probe() -> list[str]:
        with ThreadPoolExecutor(max_workers=len(LLM_CLIS)) as pool:
            results = list(pool.map(_cli_ready, LLM_CLIS))
        return [cli for cli, ok in zip(LLM_CLIS, results) if ok]
    found = [cli for cli in _cached("llm", 60, probe) if not _cli_auth_rejected(cli)]
    found += [f"{key.split('_')[0]} API key" for key in API_KEY_ENV if key in saved_api_keys(settings)]
    return found


def has_runnable_machine_model(providers: list[str], models: list[str]) -> bool:
    """True when at least one model switched on in `machines.json` can run with `providers`."""
    if not providers or not models:
        return False
    tags: set[str] = set()
    for p in providers:
        low = str(p).lower()
        if "claude" in low or "anthropic" in low:
            tags.update({"claude", "anthropic"})
        elif "codex" in low or "openai" in low:
            tags.update({"codex", "openai"})
        elif "gemini" in low or "agy" in low:
            tags.update({"gemini", "agy"})
        elif "opencode" in low:
            tags.add("opencode")
        elif "ollama" in low:
            tags.update({"ollama", "deepseek", "qwen"})
        elif "pool" in low:
            tags.add("pool")
        else:
            tags.add(low.split()[0])
    try:
        from orchestrator.scripts.model_registry import get_model
    except Exception:
        get_model = None
    for have in models:
        h_low = str(have).lower()
        if any(h_low == t or h_low.startswith(t) for t in tags):
            return True
        if get_model:
            m = get_model(have)
            if m and (
                m.family.lower() in tags
                or any(c.lower() in tags for c in (m.required_clis or ()))
            ):
                return True
    return False


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


def _documented_command(content: str, heading: str) -> str | None:
    pattern = rf"^## (?:iOS app )?{heading}\s*$((?:(?!^## ).)*?)^```bash\s*\n(.*?)^```"
    match = re.search(pattern, content, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return match.group(2).strip() if match else None


def has_build_and_test_commands(root: Path, project: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Return (ok, detail) matching common.extract_commands requirements."""
    proj = project if project is not None else _read_json(root / ".orchestrator" / "project.json")
    has_xcode = (
        bool(proj.get("xcode_project") or proj.get("xcode_workspace") or proj.get("scheme"))
        or any(root.glob("*.xcodeproj"))
        or any(root.glob("*.xcworkspace"))
        or (root / "Package.swift").is_file()
    )
    doc_build = None
    doc_test = None
    doc_path = root / "docs" / "build-test-commands.md"
    if doc_path.is_file():
        try:
            content = doc_path.read_text(encoding="utf-8", errors="replace")
            doc_build = _documented_command(content, "build")
            doc_test = _documented_command(content, "tests?")
        except OSError:
            pass
    build = str(proj.get("build_command") or "").strip() or doc_build or ("xcode" if has_xcode else "")
    test = str(proj.get("test_command") or "").strip() or doc_test or ("xcode" if has_xcode else "")
    stack = None
    if not build or not test:
        from orchestrator.stack_detection import detect_project_stack

        stack = detect_project_stack(root)
        build = build or (stack.build_command or "")
        test = test or (stack.test_command or "")
    if build and test:
        if str(proj.get("build_command") or "").strip() and str(proj.get("test_command") or "").strip():
            return True, "Configured in .orchestrator/project.json"
        if doc_build and doc_test:
            return True, "Documented in docs/build-test-commands.md"
        if has_xcode:
            return True, "Auto-detected Xcode / Swift project"
        return True, f"Auto-detected {stack.display_name if stack else 'project'} commands"
    if not build and not test:
        return False, "Set build_command and test_command in .orchestrator/project.json or docs/build-test-commands.md"
    if not build:
        return False, "Set build_command in .orchestrator/project.json or ## Build in docs/build-test-commands.md"
    return False, "Set test_command in .orchestrator/project.json or ## Tests in docs/build-test-commands.md"


def git_remote_auth_state(root: Path, origin: str, gh_user: str | None = None, fresh: bool = False) -> tuple[bool, str]:
    """Verify git can reach `origin` non-interactively without prompting for a username or password."""
    if not origin or not (root / ".git").exists():
        return True, "No remote configured"
    if gh_user and GITHUB_REMOTE.search(origin) and ensure_gh_git_credentials():
        return True, "Authenticated via GitHub CLI"
    cache_key = f"git_auth:{root}:{origin}"
    if fresh:
        _cache.pop(cache_key, None)

    def probe() -> tuple[bool, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        try:
            res = subprocess.run(
                ["git", "ls-remote", "--heads", "origin"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=6,
            )
        except Exception:
            return True, "Could not reach remote"
        if res.returncode == 0:
            return True, "Non-interactive git access verified"
        err = ((res.stderr or "") + "\n" + (res.stdout or "")).lower()
        auth_markers = (
            "could not read username",
            "could not read password",
            "terminal prompts disabled",
            "authentication failed",
            "permission denied",
            "invalid username or token",
            "access denied",
        )
        if any(marker in err for marker in auth_markers):
            return False, "Git cannot authenticate with origin non-interactively. Sign in with GitHub CLI or save a GitHub token."
        return True, "Could not reach remote"

    return _cached(cache_key, 60, probe)


def _architecture_item(root: Path, page: Any) -> dict[str, Any]:
    """Recommended, never required: done once there is an approved architecture or a working assumption (Skip records one)."""
    from orchestrator import architecture
    try:
        record = architecture.load_decision(root)
        codeless = record is None and architecture.is_codeless(root)
    except OSError:
        record, codeless = None, False
    if record and architecture.is_approved(record):
        detail = f"Approved: {record.get('chosen') or 'recorded'} (v{record.get('version', '1.0')}). Every job follows it."
    elif record:
        detail = f"Working assumption: {record.get('chosen') or 'from the code'}. Approve it so jobs treat it as a rule."
    elif codeless:
        detail = "Not chosen yet. Choose how this project is built before planning the first feature."
    else:
        detail = "Not described yet. Describe how the code is built, so every job follows it."
    return _item("architecture", "Choose or describe the architecture", False, record is not None, detail,
                 action=page("#/product?section=architecture"), group="Recommended")


def setup_checklist(root: Path, runtime: Path, fresh: bool = False) -> dict[str, Any]:
    if fresh:
        _cache.pop("gh", None)
        _cache.pop("llm", None)
        for k in [k for k in _cache if k.startswith("git_auth:")]:
            _cache.pop(k, None)
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
    enabled_machines = [m for m in machines if m.get("enabled", True)]
    ssh_machines = sum(1 for m in enabled_machines if m.get("execution_mode") == "ssh")
    models = list(dict.fromkeys(m for mach in enabled_machines for m in mach.get("models", [])))

    cmds_ok, cmds_detail = has_build_and_test_commands(root, project)
    items = [
        _item("project", "Project configured", True, bool(project), "Name, build and test settings" if project else "No .orchestrator/project.json yet",
              action=None if project else {"type": "configure_project", "label": "Configure"}, group="Project"),
        _item("build_test_commands", "Build & test commands", True, cmds_ok, cmds_detail,
              action=None if cmds_ok else ({"type": "configure_project", "label": "Configure"} if not project else {"type": "route", "to": "#/checkup", "label": "Configure"}),
              group="Project"),
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
    if is_git and origin:
        git_auth_ok, git_auth_detail = git_remote_auth_state(root, origin, gh_user=gh.get("user"), fresh=fresh)
        items.append(_item(
            "git_auth",
            "Git remote authentication",
            True,
            git_auth_ok,
            git_auth_detail,
            action=({"type": "github"} if gh["installed"] and not gh["user"] else page("#/settings")) if not git_auth_ok else None,
            hint=None if git_auth_ok else "gh auth setup-git",
            group="GitHub" if github else "Git",
        ))
    local_models = [m for mach in enabled_machines if mach.get("execution_mode") != "ssh" for m in mach.get("models", [])]
    llm_overlap = has_runnable_machine_model(providers, local_models or models)
    llm_ok = bool(providers) and bool(models) and llm_overlap
    items += [
        _item("llm", "An AI model is ready", True, llm_ok,
              "No AI is set up yet. Set up a free one in one click, or connect your own." if not providers
              else "No model is assigned to a machine" if not models
              else "Ready AI tools aren't switched on for your enabled computers. Enable matching models under Computers." if not llm_overlap
              else "Ready to run jobs",
              # No AI at all: set up the free one right here (ai_setup.py), no terminal and no account
              action={"type": "free_ai", "label": "Set up free AI"} if not providers
              else page("#/config/fleet") if not llm_overlap
              else {"type": "route", "to": "#/config/models"},
              group="AI"),
        _item("machines", "A machine to run jobs on", True, bool(enabled_machines),
              (f"{len(enabled_machines)} enabled, {ssh_machines} remote over SSH" if ssh_machines else f"{len(enabled_machines)} enabled. Add other Macs to run jobs side by side.")
              if enabled_machines else "No enabled machines. Add or enable a machine.",
              action=page("#/config/fleet"), group="AI"),
        _architecture_item(root, page),
        _item("docs", "Project rules for the AI (AGENTS.md)", False, (root / "AGENTS.md").exists(),
              "Give the AI the project's rules and conventions", action=page("#/config/ai-instructions"), group="Recommended"),
        _item("connections", "Connect Jira, Trello, Sentry or Figma", False, bool(settings.get("integrations")),
              "Tie jobs to tickets, pull in error logs and designs", action={"type": "route", "to": "#/connections"}, group="Optional"),
        _item("firebase", "Firebase delivery to testers", False, bool(project.get("firebase_distribution")),
              "Send builds to testers after a job", action=page("#/config/firebase"), group="Optional"),
        _item("email", "Email notifications", False, bool(settings.get("notification_emails")),
              "Get told when a job finishes or needs you", action=page("#/config/email"), group="Optional"),
        _item("prompts", "Custom role prompts", False, (runtime / "prompts").is_dir() and any((runtime / "prompts").iterdir()),
              "Tune how the planner, builder and reviewer behave", action=page("#/config/ai-instructions"), group="Optional"),
    ]
    is_python = any((root / f).exists() for f in ("pyproject.toml", "setup.py", "requirements.txt")) or bool(project.get("test_command") and ("python" in project.get("test_command") or "pytest" in project.get("test_command") or "unittest" in project.get("test_command")))
    if is_python:
        import sys
        cov_check = _run([sys.executable, "-m", "coverage", "--version"], timeout=2)
        cov_ok = bool(cov_check and cov_check.returncode == 0)
        items.append(_item("coverage_py", "Test coverage (coverage.py)", False, cov_ok,
                           "Installed and ready" if cov_ok else "Will be auto-installed on first test measurement",
                           action={"type": "run", "action": "coverage", "label": "Measure"} if cov_ok else {"type": "run", "action": "coverage", "label": "Auto-add"},
                           hint=None if cov_ok else "pip install coverage", group="Testing"))
    required = [i for i in items if i["required"]]

    return {
        "items": items,
        "required_total": len(required),
        "required_done": sum(1 for i in required if i["done"]),
        "complete": all(i["done"] for i in required),
        "seen": bool(settings.get("setup_seen")),
    }
