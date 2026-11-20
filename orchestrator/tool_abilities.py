"""What each AI tool can do beyond writing code: its slash commands, skills, plugins and subcommands, scanned on this computer,
and the handful worth a button in Orchestrator (a review of a job's changes, by a second tool).

The scan reads what each tool reports or keeps, never a list from memory:
- Claude Code reports everything it has loaded (built-in commands, skills, plugin commands) in the first message of a
  stream-JSON session, run in the project folder so its own commands count. Nothing is asked of the model: the session is
  ended as soon as that message arrives.
- The other tools: their subcommands (from `--help`) and the folders they keep custom commands and skills in.

Results are cached in `<runtime>/state/tool-abilities.json`; the AI tools page rescans when it's older than RESCAN_AFTER.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

RESCAN_AFTER = 15 * 60
CHECKED = "2026-11-16"  # when the highlights below were last checked against the tools

# The few abilities worth a button: each reviews a job's changes, by a second tool, and needs no typing (except `terminal`,
# which opens in the run's terminal because the tool asks before it bills). `needs` is what the scan must have found.
HIGHLIGHTS: list[dict[str, Any]] = [
    {"id": "claude-code-review", "tool": "claude", "needs": "code-review", "label": "Code review by Claude",
     "what": "Claude Code's /code-review of the job's branch: bugs, edge cases and simplifications, ranked.", "mode": "headless"},
    {"id": "claude-security-review", "tool": "claude", "needs": "security-review", "label": "Security review by Claude",
     "what": "Claude Code's /security-review of the job's changes: injection, secrets, unsafe input, auth mistakes.", "mode": "headless"},
    {"id": "claude-ultrareview", "tool": "claude", "needs": "ultrareview", "label": "Deep cloud review (Claude ultra)",
     "what": "Claude Code's /code-review ultra: a multi-agent review in Anthropic's cloud. Billed to your Claude account, and "
             "Claude asks before it starts, so it opens in the run's terminal.", "mode": "terminal", "billed": True},
    {"id": "codex-review", "tool": "codex", "needs": "review", "label": "Code review by Codex",
     "what": "OpenAI Codex's own reviewer (codex review) on the job's branch against its base: a second opinion from another model.",
     "mode": "headless"},
]

# Where each tool keeps custom commands and skills (user-wide, then the project's own).
FOLDERS: dict[str, list[tuple[str, str, str]]] = {  # tool -> [(path, kind, glob)]
    "codex": [("~/.codex/prompts", "custom", "*.md"), ("~/.codex/skills", "skill", "*/")],
    "gemini": [("~/.gemini/commands", "custom", "**/*.toml"), ("{root}/.gemini/commands", "custom", "**/*.toml"),
               ("~/.gemini/skills", "skill", "*/")],
    "opencode": [("~/.config/opencode/command", "custom", "*.md"), ("~/.config/opencode/commands", "custom", "*.md"),
                 ("{root}/.opencode/command", "custom", "*.md"), ("{root}/.opencode/commands", "custom", "*.md")],
}
HELP_LINE = re.compile(r"^\s{2,}([a-z][a-z0-9-]{1,30})(?:\s{2,}|\t)(\S.*)$")


def _run(argv: list[str], cwd: Path, timeout: int = 15) -> str:
    try:
        return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "NO_COLOR": "1"}).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def subcommands(cli: str, root: Path) -> list[dict[str, str]]:
    """A tool's subcommands, from its own --help."""
    seen: dict[str, str] = {}
    for line in _run([cli, "--help"], root).splitlines():
        if match := HELP_LINE.match(line):
            name, what = match.groups()
            if name not in seen and name not in ("help", "completion"):
                seen[name] = what.strip()[:160]
    return [{"name": n, "kind": "subcommand", "what": w} for n, w in seen.items()]


def claude_loaded(root: Path, timeout: int = 40) -> list[dict[str, str]]:
    """Everything Claude Code has loaded here, from its session's first (init) message. The session ends right after it."""
    try:
        proc = subprocess.Popen(["claude", "-p", "Reply with the word ok.", "--output-format", "stream-json", "--verbose", "--max-turns", "1"],
                                cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env={**os.environ, "NO_COLOR": "1"})
    except OSError:
        return []
    init: dict[str, Any] = {}
    deadline = time.monotonic() + timeout
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("type") == "system" and message.get("subtype") == "init":
                init = message
                break
            if time.monotonic() > deadline:
                break
    finally:
        proc.kill()
        proc.wait(timeout=5)
    skills = set(init.get("skills") or [])
    out = []
    for name in init.get("slash_commands") or []:
        if not isinstance(name, str) or name.startswith("_"):
            continue
        kind = "skill" if name in skills else "plugin" if ":" in name else "built-in"
        out.append({"name": name, "kind": kind, "what": ""})
    return out


def _folder_items(spec: list[tuple[str, str, str]], root: Path) -> list[dict[str, str]]:
    out = []
    for raw, kind, pattern in spec:
        folder = Path(raw.replace("{root}", str(root))).expanduser()
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob(pattern))[:200]:
            name = path.name.rstrip("/") if pattern.endswith("/") else path.stem
            if path.is_dir() == pattern.endswith("/"):
                out.append({"name": name, "kind": kind, "what": ""})
    return out


def scan_tool(cli: str, root: Path) -> list[dict[str, str]]:
    if not shutil.which(cli):
        return []
    found = claude_loaded(root) if cli == "claude" else subcommands(cli, root)
    found += _folder_items(FOLDERS.get(cli, []), root)
    unique: dict[str, dict[str, str]] = {}
    for item in found:
        unique.setdefault(item["name"], item)
    return sorted(unique.values(), key=lambda i: ("built-in subcommand skill plugin custom".split().index(i["kind"])
                                                    if i["kind"] in "built-in subcommand skill plugin custom".split() else 9, i["name"]))


def cache_path(runtime: Path) -> Path:
    return Path(runtime) / "state" / "tool-abilities.json"


def load(runtime: Path) -> dict[str, Any]:
    try:
        data = json.loads(cache_path(runtime).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def scan(root: Path, runtime: Path, clis: list[str]) -> dict[str, Any]:
    """Scans each installed tool and saves the result: {at, tools: {cli: [{name, kind, what}]}}."""
    data = {"at": time.time(), "tools": {cli: scan_tool(cli, root) for cli in clis}}
    path = cache_path(runtime)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def is_stale(data: dict[str, Any], now: float | None = None) -> bool:
    return (time.time() if now is None else now) - float(data.get("at") or 0) > RESCAN_AFTER


def highlights(data: dict[str, Any], ready: set[str]) -> list[dict[str, Any]]:
    """The highlighted abilities this computer can use now: the tool is signed in and the scan found the ability."""
    tools = data.get("tools") or {}
    out = []
    for h in HIGHLIGHTS:
        names = {i.get("name") for i in tools.get(h["tool"]) or []}
        if h["tool"] in ready and h["needs"] in names:
            out.append({k: h[k] for k in ("id", "tool", "label", "what", "mode")} | {"billed": bool(h.get("billed"))})
    return out


def find(ability_id: str) -> dict[str, Any]:
    for h in HIGHLIGHTS:
        if h["id"] == ability_id:
            return h
    raise KeyError(ability_id)


READ_ONLY_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git status:*),Bash(git show:*)"  # a review reads, never edits


def command(ability: dict[str, Any], branch: str, base: str, pr_number: int | None = None) -> list[str]:
    """The command that runs `ability` in a checkout of the job's branch (`branch`), against `base`."""
    if ability["id"] == "claude-code-review":
        return ["claude", "-p", f"/code-review {branch}", "--allowedTools", READ_ONLY_TOOLS]
    if ability["id"] == "claude-security-review":
        return ["claude", "-p", "/security-review", "--allowedTools", READ_ONLY_TOOLS]
    if ability["id"] == "claude-ultrareview":
        return ["claude", f"/code-review ultra{f' {pr_number}' if pr_number else ''}"]
    if ability["id"] == "codex-review":
        return ["codex", "review", "--base", base]
    raise KeyError(ability["id"])
