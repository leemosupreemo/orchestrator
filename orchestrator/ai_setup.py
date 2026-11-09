"""Get a free AI working in one step, for someone who has never used a command line.

OpenCode's free models need no account, so it's the one Orchestrator can set up on its own:
  check    is OpenCode already on this computer?
  install  if not, run its official installer (one line, no Node or Homebrew needed)
  verify   ask OpenCode which models it offers, and make sure at least one is a free model Orchestrator knows
  ready    turn those models on for this computer's jobs, so setup ticks off straight away
Each step is reported as it starts (a TaskProgress from the web server: stage = the step's id, detail = what's happening),
so the page can show a live checklist. When it can't finish, it says which step stopped it and why, and offers Poolside, the
other free option (which needs a sign-in, so it can't be set up silently).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from orchestrator import ai_providers

STEPS = [("check", "Checking this computer"), ("install", "Installing OpenCode"),
         ("verify", "Testing the free models"), ("ready", "Turning it on for jobs")]
FALLBACK = {"id": "pool", "name": "Poolside",
            "why": "Poolside is free too. It needs a quick sign-in in your browser, which Connect walks you through."}


def _which(cli: str) -> str | None:
    return shutil.which(cli, path=ai_providers.tool_path())


def _list_models(path: str) -> list[str]:
    result = subprocess.run([path, "models"], capture_output=True, text=True, timeout=90, check=False,
                            env={**os.environ, "PATH": ai_providers.tool_path()})
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "OpenCode didn't list its models.").strip()[-600:])
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _known_free() -> list[str]:
    from orchestrator.scripts.model_registry import get_all_models
    return [m.id for m in get_all_models() if m.id.startswith("opencode/")]


def _refresh_known() -> None:
    """Learn the models OpenCode offers today (its free line-up changes often), so they count as known."""
    from orchestrator.scripts import model_registry
    model_registry.refresh_provider_models()
    model_registry._ALL_MODELS_CACHE = None


def _refresh(root: Path) -> None:
    """Let this computer's model list follow what's now ready (the readiness check is cached briefly, so start afresh)."""
    from orchestrator import setup_checklist
    from orchestrator.project_setup import refresh_ready_models
    setup_checklist._cache.pop("llm", None)
    refresh_ready_models(root)


def is_free(model: str) -> bool:
    return "-free" in model or model.endswith("free")


def ensure_free_ai(root: Path, progress: Any = None, *, which: Callable[[str], str | None] = _which,
                   install: Callable[[str], dict] = ai_providers.install_provider,
                   list_models: Callable[[str], list[str]] = _list_models,
                   known: list[str] | Callable[[], list[str]] | None = None,
                   refresh_known: Callable[[], None] = _refresh_known,
                   refresh: Callable[[Path], None] = _refresh) -> dict[str, Any]:
    """Run the steps. Returns {"ok": True, "model", "models", "installed_now"} or {"ok": False, "step", "message", "fallback"}."""
    def say(step: str, detail: str) -> None:
        if progress is not None:
            progress.update(step, detail)

    installed_now = False

    def stopped(step: str, message: str) -> dict[str, Any]:
        return {"ok": False, "step": step, "message": message, "fallback": FALLBACK, "installed_now": installed_now}

    say("check", "Looking for OpenCode on this computer")
    path = which("opencode")
    if not path:
        say("install", "Downloading and installing OpenCode. This takes a minute or two.")
        try:
            install("opencode")
        except (RuntimeError, ValueError, OSError) as exc:
            return stopped("install", str(exc))
        path = which("opencode")
        if not path:
            return stopped("install", "OpenCode's installer finished, but OpenCode can't be found on this computer.")
        installed_now = True

    say("verify", "Asking OpenCode which free models it offers")
    try:
        offered = set(list_models(path))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return stopped("verify", f"OpenCode is installed but didn't answer: {exc}")
    def known_free() -> list[str]:
        names = known() if callable(known) else known if known is not None else _known_free()
        return [m for m in names if is_free(m) and m in offered]

    # Record what OpenCode offers today. Orchestrator counts only those of its OpenCode models (so paid ones it doesn't offer
    # without an account aren't picked), and it learns free models that are new since its list was made.
    say("verify", "Updating Orchestrator's list of OpenCode's models")
    try:
        refresh_known()
    except Exception:  # noqa: BLE001 - a failed refresh just means nothing new was learned
        pass
    free = known_free()
    if not free:
        return stopped("verify", "OpenCode is installed, but it isn't offering any free models right now.")

    say("ready", f"Turning on {len(free)} free model{'s' if len(free) != 1 else ''} for this computer's jobs")
    refresh(root)
    return {"ok": True, "tool": "OpenCode", "model": free[0], "models": free, "installed_now": installed_now}
