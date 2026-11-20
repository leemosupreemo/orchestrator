"""Remove a recognized AI CLI installation, preserving user settings and data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

from orchestrator import ai_providers

NPM_PACKAGES = {"codex": "@openai/codex", "gemini": "@google/gemini-cli",
                "claude": "@anthropic-ai/claude-code", "opencode": "opencode-ai"}
BREW_PACKAGES = {"opencode": {"opencode"}, "ollama": {"ollama"}, "codex": {"codex"},
                 "gemini": {"gemini-cli"}, "claude": {"claude-code"}}


def _run(command: list[str]) -> None:
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=360, env={**os.environ, "PATH": ai_providers.tool_path()})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("The uninstaller could not finish. Check this computer and try again.") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "Uninstaller exited without details.").strip()[-1000:]
        raise RuntimeError(f"Could not uninstall the AI tool. {detail}")


def _remove(provider_id: str, binary: Path) -> None:
    target = binary.resolve()
    # Package-manager ownership is established by the resolved executable, not by
    # whichever package manager happens to appear first on PATH.
    for directory in target.parents:
        if directory.name in ("Cellar", "Caskroom"):
            package = target.relative_to(directory).parts[0]
            if package not in BREW_PACKAGES.get(provider_id, set()):
                break
            brew = directory.parent / "bin/brew"
            if not brew.is_file():
                break
            if provider_id == "ollama":
                _run([str(brew), "services", "stop", "ollama"])
            _run([str(brew), "uninstall", "--formula" if directory.name == "Cellar" else "--cask", package])
            return
        if directory.name == "node_modules" and directory.parent.name == "lib":
            package = NPM_PACKAGES.get(provider_id)
            package_root = directory / package if package else None
            prefix = directory.parent.parent
            if not package_root or not target.is_relative_to(package_root) or binary.parent != prefix / "bin":
                break
            try:
                metadata = json.loads((package_root / "package.json").read_text())
            except (OSError, ValueError):
                break
            npm = shutil.which("npm", path=ai_providers.tool_path())
            if metadata.get("name") != package or not npm:
                break
            _run([npm, "uninstall", "--global", "--ignore-scripts", "--prefix", str(prefix), package])
            return
    home = Path.home().resolve()
    native = home / ".local/bin" / provider_id
    if provider_id == "opencode" and binary == home / ".opencode/bin/opencode":
        native = binary
    claude_version = home / ".local/share/claude/versions"
    if binary == native and (target == binary or provider_id == "claude" and target.is_relative_to(claude_version)):
        binary.unlink()
        if target != binary:
            target.unlink()
        return
    if provider_id == "ollama":
        for app in (Path("/Applications/Ollama.app"), home / "Applications/Ollama.app"):
            if target.is_relative_to(app) and app.is_dir():
                # Put the app in Trash; ~/.ollama contains the user's models and is never touched.
                _run(["osascript", "-e", 'tell application "Ollama" to quit'])
                trash = home / ".Trash"
                trash.mkdir(exist_ok=True)
                shutil.move(str(app), str(trash / f"Ollama-{secrets.token_hex(4)}.app"))
                if binary.is_symlink() and os.access(binary.parent, os.W_OK):
                    binary.unlink()
                return
    raise ValueError("This installation is not recognized. Use the tool's official uninstall instructions; no files were removed.")


def uninstall_provider(provider_id: str) -> dict:
    provider = next((p for p in ai_providers.PROVIDERS if p["id"] == provider_id), None)
    if provider is None:
        raise ValueError("Unknown AI provider")
    with ai_providers._INSTALL_LOCK:
        if provider_id in ai_providers._INSTALLING:
            raise RuntimeError("This AI tool is already being installed or uninstalled. Wait for it to finish.")
        ai_providers._INSTALLING.add(provider_id)
    try:
        executable = shutil.which(provider["cli"], path=ai_providers.tool_path())
        if not executable:
            raise ValueError(f"{provider['name']} is not installed.")
        binary = Path(executable)
        _remove(provider_id, binary.parent.resolve() / binary.name)
        remaining = bool(shutil.which(provider["cli"], path=ai_providers.tool_path()))
        detail = ("Removed this installation. Another installation of the tool is still available."
                  if remaining else "Uninstalled. Saved settings, API keys, histories and downloaded models were kept.")
        return {"provider": provider["name"], "detail": detail, "remaining": remaining}
    finally:
        with ai_providers._INSTALL_LOCK:
            ai_providers._INSTALLING.discard(provider_id)
