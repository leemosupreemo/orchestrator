#!/usr/bin/env python3
"""A snapshot of which models this account can really use, from what each CLI reports."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from model_registry import ModelMetadata, parse_agy_models_output, parse_opencode_models_output

MAX_AGE_SECONDS = 3600
PROBE_TIMEOUT_SECONDS = 8
OLLAMA_MODEL_NAMES = {"deepseek": "deepseek-coder"}
AGY_BINARIES = ("agy", "antigravity")
# Catalog ids that run the CLI's own default model, so they work if the CLI lists anything.
ACCOUNT_DEFAULT_IDS = {"codex", "gemini"}


def _snapshot_path() -> Path:
    return Path.home() / ".orchestrator" / "model_availability.json"


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False)


def _probe_agy() -> list[str]:
    binary = next((name for name in AGY_BINARIES if shutil.which(name)), None)
    if binary is None:
        return []
    result = _run([binary, "models"])
    return parse_agy_models_output(result.stdout) if result.returncode == 0 else []


def _probe_codex() -> list[str]:
    if not shutil.which("codex") or _run(["codex", "login", "status"]).returncode != 0:
        return []
    cache = json.loads((Path.home() / ".codex" / "models_cache.json").read_text(encoding="utf-8"))
    return sorted(m["slug"] for m in cache.get("models", []) if m.get("visibility") == "list")


def _probe_opencode() -> list[str]:
    if not shutil.which("opencode"):
        return []
    result = _run(["opencode", "models"])
    return parse_opencode_models_output(result.stdout) if result.returncode == 0 else []


def _probe_ollama() -> list[str]:
    if not shutil.which("ollama"):
        return []
    result = _run(["ollama", "list"])
    if result.returncode != 0:
        return []
    return sorted(line.split()[0] for line in result.stdout.splitlines()[1:] if line.strip())


def _probe_claude() -> list[str]:
    # The Claude CLI has no model list, so a signed-in CLI is the best signal available.
    if not shutil.which("claude") or _run(["claude", "auth", "status"]).returncode != 0:
        return []
    return ["*"]


def _probe_copilot() -> list[str]:
    if not shutil.which("gh") or _run(["gh", "copilot", "--version"]).returncode != 0:
        return []
    return ["*"]


PROBES: dict[str, Callable[[], list[str]]] = {
    "agy": _probe_agy, "codex": _probe_codex, "opencode": _probe_opencode,
    "ollama": _probe_ollama, "claude": _probe_claude, "copilot": _probe_copilot,
}


def _probe_safely(probe: Callable[[], list[str]]) -> list[str] | None:
    """Returns None when the provider couldn't be checked, as opposed to [] for nothing usable."""
    try:
        return probe()
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return None


def refresh_snapshot() -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=len(PROBES)) as pool:
        results = dict(zip(PROBES, pool.map(_probe_safely, PROBES.values())))
    snapshot = {"fetched_at": time.time(), "providers": results}
    path = _snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    return snapshot


def read_snapshot() -> dict[str, Any] | None:
    """Returns the saved snapshot without running any CLI."""
    try:
        snapshot = json.loads(_snapshot_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return snapshot if isinstance(snapshot.get("providers"), dict) else None


def get_snapshot(max_age: float = MAX_AGE_SECONDS) -> dict[str, Any]:
    snapshot = read_snapshot()
    if snapshot and time.time() - snapshot.get("fetched_at", 0) < max_age:
        return snapshot
    return refresh_snapshot()


def is_available(model: ModelMetadata, snapshot: dict[str, Any]) -> bool | None:
    """True or False from the provider's report; None when the provider couldn't be checked."""
    providers = snapshot.get("providers", {})
    cli = model.required_clis[0] if model.required_clis else None

    def listed(provider: str) -> list[str] | None:
        return providers.get(provider)

    if cli in {"gemini", "agy", "antigravity"}:
        names = listed("agy")
        if names is None:
            return None
        return bool(names) if model.id in ACCOUNT_DEFAULT_IDS else (model.api_model_id or model.id).lower() in names
    if cli == "codex":
        names = listed("codex")
        if names is None:
            return None
        return bool(names) if model.id in ACCOUNT_DEFAULT_IDS else (model.api_model_id or model.id) in names
    if cli == "opencode":
        names = listed("opencode")
        return None if names is None else model.id in names
    if cli == "ollama":
        names = listed("ollama")
        if names is None:
            return None
        wanted = OLLAMA_MODEL_NAMES.get(model.id, model.id)
        return wanted in names or f"{wanted}:latest" in names
    if cli in {"claude", "gh"}:
        names = listed("claude" if cli == "claude" else "copilot")
        return None if names is None else bool(names)
    return None
