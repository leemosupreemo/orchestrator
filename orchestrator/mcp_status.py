"""Which AI tools on this computer have each recommended MCP server, and adding one to a tool that lacks it.

Each tool keeps its MCP servers in its own config: Claude Code in ~/.claude.json, Codex in ~/.codex/config.toml, Gemini in
~/.gemini/settings.json, Antigravity under ~/.gemini/antigravity-cli/mcp/, OpenCode in ~/.config/opencode/opencode.json.
People name servers differently (context7-mcp, XcodeBuildMCP), so a server counts as present when its name matches one
of the recommended server's names, or its launch command mentions the package.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

# id: the name it's added under. names: what it's often called. launch: how it runs (None: it has a setup step instead).
SERVERS: dict[str, dict[str, Any]] = {
    "superpowers": {"names": ["superpowers"], "launch": ["npx", "-y", "superpowers-mcp"]},
    "context7": {"names": ["context7"], "launch": ["npx", "-y", "@upstash/context7-mcp"]},
    "openspec": {"names": ["openspec"], "launch": ["npx", "-y", "@openspec/mcp"]},
    "github": {"names": ["github"], "launch": ["npx", "-y", "@modelcontextprotocol/server-github"]},
    "xcodebuild": {"names": ["xcodebuild"], "launch": ["npx", "-y", "xcodebuildmcp"]},
    "xcodediagnostics": {"names": ["xcodediagnostics"], "launch": ["npx", "-y", "xcodebuildmcp", "--diagnostics"]},
    "swiftlens": {"names": ["swiftlens"], "launch": ["npx", "-y", "swiftlens-mcp"]},
    "mcp-ssh-manager": {"names": ["ssh-manager"], "launch": ["npx", "-y", "mcp-ssh-manager"]},
    "rea": {"names": ["rea-agents", "rea"], "launch": ["npx", "-y", "rea-agents", "--mcp"]},  # `rea --mcp` serves REA over MCP
}

TOOLS = [
    {"id": "claude", "name": "Claude Code", "bin": "claude"},
    {"id": "codex", "name": "Codex", "bin": "codex"},
    {"id": "gemini", "name": "Gemini", "bin": "gemini"},
    {"id": "agy", "name": "Antigravity", "bin": "agy"},
    {"id": "opencode", "name": "OpenCode", "bin": "opencode"},
]


def _json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _opencode_config(home: Path) -> Path:
    folder = home / ".config" / "opencode"
    return folder / "opencode.json" if (folder / "opencode.json").is_file() or not (folder / "opencode.jsonc").is_file() else folder / "opencode.jsonc"


def configured(tool: str, home: Path, project: Path | None = None) -> dict[str, str]:
    """The MCP servers a tool has: name -> its settings as text (to look for the package in)."""
    found: dict[str, Any] = {}
    if tool == "claude":
        data = _json(home / ".claude.json")
        found.update(data.get("mcpServers") or {})
        if project is not None:
            found.update(((data.get("projects") or {}).get(str(project)) or {}).get("mcpServers") or {})
            found.update(_json(project / ".mcp.json").get("mcpServers") or {})
    elif tool == "codex":
        try:
            found.update(tomllib.loads((home / ".codex" / "config.toml").read_text(encoding="utf-8")).get("mcp_servers") or {})
        except (OSError, tomllib.TOMLDecodeError):
            pass
    elif tool == "gemini":
        found.update(_json(home / ".gemini" / "settings.json").get("mcpServers") or {})
        if project is not None:
            found.update(_json(project / ".gemini" / "settings.json").get("mcpServers") or {})
    elif tool == "agy":
        folder = home / ".gemini" / "antigravity-cli" / "mcp"
        for entry in folder.iterdir() if folder.is_dir() else []:
            text = ""
            for f in entry.glob("*.json") if entry.is_dir() else []:
                try:
                    text += f.read_text(encoding="utf-8")
                except OSError:
                    pass
            found[entry.name] = text
    elif tool == "opencode":
        found.update(_json(_opencode_config(home)).get("mcp") or {})
    return {str(name): value if isinstance(value, str) else json.dumps(value) for name, value in found.items()}


def has_server(server_id: str, servers: dict[str, str]) -> bool:
    spec = SERVERS[server_id]
    names = [server_id.lower(), *(n.lower() for n in spec["names"])]
    # The parts of its launch command that identify it: the package, plus any flag (xcodebuildmcp --diagnostics is a different server).
    marks = [a.lower() for a in (spec["launch"] or []) if a not in ("npx", "-y")]
    for name, settings in servers.items():
        lowered = name.lower()
        text = settings.lower()
        flagged = any(m.startswith("--") for m in marks)
        if any(lowered == n or (len(n) >= 6 and n in lowered) for n in names):
            if not flagged and any(o in lowered for o in ("diagnostics",)) and server_id != "xcodediagnostics":
                continue
            return True
        if marks and all(m in text for m in marks) and (flagged or "--diagnostics" not in text):
            return True
    return False


def status(home: Path | None = None, project: Path | None = None) -> dict[str, Any]:
    """The tools on this computer, and for each recommended server which of them have it."""
    home = home or Path.home()
    tools = [t for t in TOOLS if shutil.which(t["bin"])]
    have = {t["id"]: configured(t["id"], home, project) for t in tools}
    return {"tools": [{"id": t["id"], "name": t["name"]} for t in tools],
            "servers": [{"id": sid, "installable": bool(spec["launch"]),
                         "installed": {t["id"]: has_server(sid, have[t["id"]]) for t in tools}} for sid, spec in SERVERS.items()]}


def install(server_id: str, tool: str, home: Path | None = None) -> None:
    """Add a recommended server to one tool, for this user (all projects). Raises ValueError with a plain reason."""
    home = home or Path.home()
    spec = SERVERS.get(server_id)
    entry = next((t for t in TOOLS if t["id"] == tool), None)
    if not spec or not entry:
        raise ValueError("Unknown MCP server or AI tool.")
    launch = spec["launch"]
    if not launch:
        raise ValueError(f"{server_id} has its own setup step: run it from the card's command.")
    if tool in ("gemini", "opencode"):  # their add commands are interactive or misread "-y": write the setting directly
        path = home / ".gemini" / "settings.json" if tool == "gemini" else _opencode_config(home)
        data = _json(path)
        if tool == "gemini":
            data.setdefault("mcpServers", {})[server_id] = {"command": launch[0], "args": launch[1:]}
        else:
            data.setdefault("mcp", {})[server_id] = {"type": "local", "command": launch, "enabled": True}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return
    if not shutil.which(entry["bin"]):
        raise ValueError(f"{entry['name']} isn't installed on this computer.")
    argv = {"claude": ["claude", "mcp", "add", "--scope", "user", server_id, "--", *launch],
            "codex": ["codex", "mcp", "add", server_id, "--", *launch],
            "agy": ["agy", "mcp", "add", server_id, *launch]}[tool]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"{entry['name']} didn't add it: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise ValueError(f"{entry['name']} didn't add it: {detail[-1] if detail else f'exit {done.returncode}'}")
