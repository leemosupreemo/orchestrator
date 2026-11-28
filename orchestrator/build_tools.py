"""Curated build/test tool detection, version/SDK checks, and non-interactive installation.

Keeps tool discovery, pinned toolchain version checks, platform SDK probes,
user-facing install descriptions, and safe allowlisted install recipes in one
place so preflight checks, worker startup gates, the CLI, and the Web UI stay
in lockstep.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from orchestrator import ai_providers

UNITY_HUB_BIN = Path("/Applications/Unity Hub.app/Contents/MacOS/Unity Hub")
UNITY_EDITOR_RE = re.compile(r"/Unity/Hub/Editor/([0-9][0-9A-Za-z._-]{1,30})/Unity\.app(?:/Contents/MacOS/Unity)?$")
UNITY_VERSION_ID_RE = re.compile(r"^unity:([0-9][0-9A-Za-z._-]{1,30})$")
UNITY_MODULE_ID_RE = re.compile(r"^unity-module:(ios|android|webgl):([0-9][0-9A-Za-z._-]{1,30})$")

_UNITY_MODULE_DIRS = {
    "ios": "iOSSupport",
    "android": "AndroidPlayer",
    "webgl": "WebGLSupport",
}
_UNITY_MODULE_LABELS = {
    "ios": "Unity iOS Build Support",
    "android": "Unity Android Build Support",
    "webgl": "Unity WebGL Build Support",
}

# Curated non-interactive Homebrew formulae (no sudo password prompt, safe from launchd/UI).
_BREW_RECIPES: dict[str, dict[str, Any]] = {
    "dotnet": {
        "name": ".NET SDK",
        "formula": "dotnet",
        "binary": "dotnet",
        "size_hint": "~200 MB download",
        "summary": "Installs the .NET SDK via Homebrew (`brew install dotnet`), with no password prompt.",
        "manual_hint": "Install the .NET SDK from https://dotnet.microsoft.com/download or run `brew install dotnet`.",
    },
    "gh": {
        "name": "GitHub CLI",
        "formula": "gh",
        "binary": "gh",
        "size_hint": "~15 MB download",
        "summary": "Installs the GitHub CLI via Homebrew (`brew install gh`).",
        "manual_hint": "Run `brew install gh`, then `gh auth login`.",
    },
    "node": {
        "name": "Node.js",
        "formula": "node",
        "binary": "node",
        "size_hint": "~50 MB download",
        "summary": "Installs Node.js and npm via Homebrew (`brew install node`).",
        "manual_hint": "Install Node.js from https://nodejs.org or run `brew install node`.",
    },
    "pnpm": {
        "name": "pnpm",
        "formula": "pnpm",
        "binary": "pnpm",
        "size_hint": "~15 MB download",
        "summary": "Installs pnpm via Homebrew (`brew install pnpm`).",
        "manual_hint": "Run `brew install pnpm`.",
    },
    "yarn": {
        "name": "Yarn",
        "formula": "yarn",
        "binary": "yarn",
        "size_hint": "~10 MB download",
        "summary": "Installs Yarn via Homebrew (`brew install yarn`).",
        "manual_hint": "Run `brew install yarn`.",
    },
    "python3": {
        "name": "Python 3",
        "formula": "python",
        "binary": "python3",
        "size_hint": "~45 MB download",
        "summary": "Installs Python 3 via Homebrew (`brew install python`).",
        "manual_hint": "Run `brew install python`.",
    },
    "go": {
        "name": "Go",
        "formula": "go",
        "binary": "go",
        "size_hint": "~100 MB download",
        "summary": "Installs Go via Homebrew (`brew install go`).",
        "manual_hint": "Run `brew install go`.",
    },
    "rust": {
        "name": "Rust",
        "formula": "rust",
        "binary": "cargo",
        "size_hint": "~250 MB download",
        "summary": "Installs Rust and Cargo via Homebrew (`brew install rust`).",
        "manual_hint": "Run `brew install rust` or install via https://rustup.rs.",
    },
    "ruby": {
        "name": "Ruby",
        "formula": "ruby",
        "binary": "ruby",
        "size_hint": "~40 MB download",
        "summary": "Installs Ruby via Homebrew (`brew install ruby`).",
        "manual_hint": "Run `brew install ruby`.",
    },
    "jq": {
        "name": "jq",
        "formula": "jq",
        "binary": "jq",
        "size_hint": "~3 MB download",
        "summary": "Installs jq via Homebrew (`brew install jq`).",
        "manual_hint": "Run `brew install jq`.",
    },
    "bun": {
        "name": "Bun",
        "formula": "oven-sh/tap/bun",
        "binary": "bun",
        "size_hint": "~40 MB download",
        "summary": "Installs Bun via Homebrew (`brew install oven-sh/tap/bun`).",
        "manual_hint": "Run `brew install oven-sh/tap/bun`.",
    },
    "uv": {
        "name": "uv",
        "formula": "uv",
        "binary": "uv",
        "size_hint": "~20 MB download",
        "summary": "Installs uv via Homebrew (`brew install uv`).",
        "manual_hint": "Run `brew install uv`.",
    },
    "poetry": {
        "name": "Poetry",
        "formula": "poetry",
        "binary": "poetry",
        "size_hint": "~30 MB download",
        "summary": "Installs Poetry via Homebrew (`brew install poetry`).",
        "manual_hint": "Run `brew install poetry`.",
    },
    "cmake": {
        "name": "CMake",
        "formula": "cmake",
        "binary": "cmake",
        "size_hint": "~40 MB download",
        "summary": "Installs CMake via Homebrew (`brew install cmake`).",
        "manual_hint": "Run `brew install cmake`.",
    },
    "gradle": {
        "name": "Gradle",
        "formula": "gradle",
        "binary": "gradle",
        "size_hint": "~130 MB download",
        "summary": "Installs Gradle via Homebrew (`brew install gradle`).",
        "manual_hint": "Run `brew install gradle`.",
    },
    "java": {
        "name": "OpenJDK",
        "formula": "openjdk",
        "binary": "java",
        "size_hint": "~200 MB download",
        "summary": "Installs OpenJDK via Homebrew (`brew install openjdk`).",
        "manual_hint": "Run `brew install openjdk`.",
    },
    "swiftlint": {
        "name": "SwiftLint",
        "formula": "swiftlint",
        "binary": "swiftlint",
        "size_hint": "~25 MB download",
        "summary": "Installs SwiftLint via Homebrew (`brew install swiftlint`).",
        "manual_hint": "Run `brew install swiftlint`.",
    },
    "xcodegen": {
        "name": "XcodeGen",
        "formula": "xcodegen",
        "binary": "xcodegen",
        "size_hint": "~15 MB download",
        "summary": "Installs XcodeGen via Homebrew (`brew install xcodegen`).",
        "manual_hint": "Run `brew install xcodegen`.",
    },
    "cocoapods": {
        "name": "CocoaPods",
        "formula": "cocoapods",
        "binary": "pod",
        "size_hint": "~30 MB download",
        "summary": "Installs CocoaPods via Homebrew (`brew install cocoapods`).",
        "manual_hint": "Run `brew install cocoapods`.",
    },
    "fastlane": {
        "name": "Fastlane",
        "formula": "fastlane",
        "binary": "fastlane",
        "size_hint": "~60 MB download",
        "summary": "Installs Fastlane via Homebrew (`brew install fastlane`).",
        "manual_hint": "Run `brew install fastlane`.",
    },
    "firebase": {
        "name": "Firebase CLI",
        "formula": "firebase-cli",
        "binary": "firebase",
        "size_hint": "~40 MB download",
        "summary": "Installs Firebase CLI via Homebrew (`brew install firebase-cli`).",
        "manual_hint": "Run `brew install firebase-cli`.",
    },
}

_ALIASES: dict[str, str] = {
    "npm": "node",
    "npx": "node",
    "python": "python3",
    "pytest": "python3",
    "cargo": "rust",
    "rustc": "rust",
    "bundle": "ruby",
    "bundler": "ruby",
    "pod": "cocoapods",
    "javac": "java",
    "docker-compose": "docker",
}

_MANUAL_TOOLS: dict[str, dict[str, str]] = {
    "xcodebuild": {
        "name": "Xcode",
        "manual_hint": "Install Xcode from the Mac App Store, open it once, and run `xcode-select --install`.",
    },
    "xcrun": {
        "name": "Xcode Command Line Tools",
        "manual_hint": "Install Xcode or run `xcode-select --install`.",
    },
    "flutter": {
        "name": "Flutter SDK",
        "manual_hint": "Install Flutter from https://docs.flutter.dev/get-started/install or `brew install --cask flutter`.",
    },
    "dart": {
        "name": "Dart SDK",
        "manual_hint": "Install Dart (`brew tap dart-lang/dart && brew install dart`) or Flutter.",
    },
}

Runner = Callable[[list[str], int], tuple[int, str, bool]]  # (argv, timeout_s) -> (code, output, timed_out)
ProbeRun = Callable[[list[str]], tuple[int, str]]
WhichFn = Callable[[str], bool]


def default_which(tool: str) -> bool:
    """Check whether `tool` (name or absolute path) is executable on Orchestrator's search PATH."""
    if not tool:
        return False
    if "/" in tool:
        p = Path(tool)
        return p.is_file() and os.access(p, os.X_OK)
    return shutil.which(tool, path=ai_providers.tool_path()) is not None


def _default_run(argv: list[str], timeout: int = 600) -> tuple[int, str, bool]:
    env = {**os.environ, "PATH": ai_providers.tool_path(), "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ENV_HINTS": "1"}
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return res.returncode, (res.stdout or "") + (res.stderr or ""), False
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        return 124, out, True
    except OSError as exc:
        return 127, str(exc), False


def canonical_tool_id(raw_tool: str) -> str:
    """Normalize a binary name, absolute path, or tool ID to a safe catalog key."""
    raw = str(raw_tool or "").strip()
    if not raw:
        return ""
    if m := UNITY_MODULE_ID_RE.match(raw):
        return f"unity-module:{m.group(1)}:{m.group(2)}"
    if m := UNITY_VERSION_ID_RE.match(raw):
        return f"unity:{m.group(1)}"
    if m := UNITY_EDITOR_RE.search(raw):
        return f"unity:{m.group(1)}"
    if raw.lower() == "unity" or raw.endswith("/Unity"):
        return "unity"
    base = Path(raw).name.lower() if "/" in raw else raw.lower()
    return _ALIASES.get(base, base)


def describe_tool(raw_tool: str, *, unity_hub_installed: bool | None = None) -> dict[str, Any]:
    """Return user-facing metadata and install capability for a missing build/test tool."""
    raw = str(raw_tool or "").strip()
    tool_id = canonical_tool_id(raw)
    if tool_id == "docker":
        return {
            "id": "docker",
            "binary": raw or "docker",
            "name": "Docker",
            "installable": True,
            "method": "Homebrew",
            "size_hint": "~700 MB download",
            "summary": "Installs Colima, Docker CLI, and Docker Compose via Homebrew and starts Docker automatically — no Docker Desktop or password needed.",
            "manual_hint": "Install Docker Desktop or run `orchestrator server install-docker`.",
        }
    if m := UNITY_MODULE_ID_RE.match(tool_id):
        module, version = m.group(1), m.group(2)
        has_hub = UNITY_HUB_BIN.is_file() if unity_hub_installed is None else unity_hub_installed
        label = f"{_UNITY_MODULE_LABELS[module]} ({version})"
        return {
            "id": tool_id,
            "binary": f"/Applications/Unity/Hub/Editor/{version}/PlaybackEngines/{_UNITY_MODULE_DIRS[module]}",
            "name": label,
            "installable": has_hub,
            "method": "Unity Hub" if has_hub else "Manual",
            "size_hint": "~1.5–3 GB download",
            "summary": (
                f"Installs {_UNITY_MODULE_LABELS[module]} for Unity {version} via Unity Hub."
                if has_hub
                else f"{label} isn't installed and Unity Hub wasn't found in /Applications."
            ),
            "manual_hint": f"Open Unity Hub > Installs > Unity {version} > Add modules > {_UNITY_MODULE_LABELS[module]}.",
        }
    if tool_id.startswith("unity:"):
        version = tool_id.split(":", 1)[1]
        has_hub = UNITY_HUB_BIN.is_file() if unity_hub_installed is None else unity_hub_installed
        return {
            "id": tool_id,
            "binary": raw or f"/Applications/Unity/Hub/Editor/{version}/Unity.app/Contents/MacOS/Unity",
            "name": f"Unity {version}",
            "installable": has_hub,
            "method": "Unity Hub" if has_hub else "Manual",
            "size_hint": "~3–5 GB download",
            "summary": (
                f"Installs Unity {version} headlessly through Unity Hub on this computer."
                if has_hub
                else f"Unity {version} isn't installed and Unity Hub wasn't found in /Applications."
            ),
            "manual_hint": f"Install Unity Hub (https://unity.com/download), sign in, and install Unity {version}.",
        }
    if tool_id == "unity":
        return {
            "id": "unity",
            "binary": raw or "Unity",
            "name": "Unity",
            "installable": False,
            "method": "Manual",
            "size_hint": "",
            "summary": "Unity isn't installed at the configured path on this computer.",
            "manual_hint": "Install Unity via Unity Hub and set `UNITY_PATH` or update `build_command` in `.orchestrator/project.json`.",
        }
    if recipe := _BREW_RECIPES.get(tool_id):
        return {
            "id": tool_id,
            "binary": raw or recipe["binary"],
            "name": recipe["name"],
            "installable": True,
            "method": "Homebrew",
            "size_hint": recipe["size_hint"],
            "summary": recipe["summary"],
            "manual_hint": recipe["manual_hint"],
        }
    if manual := _MANUAL_TOOLS.get(tool_id):
        return {
            "id": tool_id,
            "binary": raw or tool_id,
            "name": manual["name"],
            "installable": False,
            "method": "Manual",
            "size_hint": "",
            "summary": f"{manual['name']} isn't installed on this computer.",
            "manual_hint": manual["manual_hint"],
        }
    label = Path(raw).name if "/" in raw else (raw or "tool")
    return {
        "id": tool_id or label,
        "binary": raw or label,
        "name": label,
        "installable": False,
        "method": "Manual",
        "size_hint": "",
        "summary": f"{label} isn't installed on this computer.",
        "manual_hint": f"Install {label} on this computer or update `.orchestrator/project.json`.",
    }


def is_valid_install_id(tool_id: str) -> bool:
    """Strict allowlist check before invoking `install_tool` from the web API or CLI."""
    tid = str(tool_id or "").strip()
    return (
        tid == "docker"
        or tid in _BREW_RECIPES
        or bool(UNITY_VERSION_ID_RE.match(tid))
        or bool(UNITY_MODULE_ID_RE.match(tid))
    )


def missing_project_tools(
    config: Mapping[str, Any],
    *,
    which: WhichFn | None = None,
    env: Mapping[str, str] | None = None,
    include_delivery: bool = False,
) -> list[dict[str, Any]]:
    """Return deduplicated missing build/test tools referenced by `project.json` commands."""
    from orchestrator import preflight

    probe = which or default_which
    env_map = os.environ if env is None else env
    fields = [("build", config.get("build_command")), ("test", config.get("test_command"))]
    if include_delivery:
        fields.append(("delivery", config.get("delivery_build_command")))

    by_id: dict[str, dict[str, Any]] = {}
    for role, command in fields:
        if not command or not isinstance(command, str):
            continue
        for raw in preflight.command_tools(command):
            resolved = preflight._resolve_tool(raw, env_map)
            if not resolved or resolved.startswith(("./", "../")):
                continue
            if probe(resolved):
                continue
            info = describe_tool(resolved)
            entry = by_id.setdefault(info["id"], {**info, "needed_by": []})
            if role not in entry["needed_by"]:
                entry["needed_by"].append(role)
    return list(by_id.values())


# ---- Layer 1b: Pinned toolchain version checks ----

def _parse_ver_tuple(text: str) -> tuple[int, ...]:
    nums = [int(part) for part in re.findall(r"\d+", text or "")[:3]]
    return tuple(nums)


def _semver_satisfies(actual: str, spec: str) -> bool:
    """Lightweight version constraint check (`>=20`, `^20.10`, `20.x`, `3.11`, `>=3.11,<4`)."""
    act = _parse_ver_tuple(actual)
    if not act:
        return True
    clean_spec = (spec or "").strip()
    if not clean_spec or clean_spec == "*":
        return True
    # Comma-separated clauses (e.g. Python `>=3.10,<4`) or OR clauses (`18 || 20 || >=22`)
    if "||" in clean_spec:
        return any(_semver_satisfies(actual, alt) for alt in clean_spec.split("||"))
    for part in re.split(r"[,\s]+", clean_spec):
        part = part.strip()
        if not part:
            continue
        if part.startswith(">="):
            req = _parse_ver_tuple(part[2:])
            if req and act[: len(req)] < req and act < req:
                return False
        elif part.startswith(">"):
            req = _parse_ver_tuple(part[1:])
            if req and act <= req:
                return False
        elif part.startswith("<="):
            req = _parse_ver_tuple(part[2:])
            if req and act[: len(req)] > req:
                return False
        elif part.startswith("<"):
            req = _parse_ver_tuple(part[1:])
            if req and act[: len(req)] >= req:
                return False
        elif part.startswith("^"):
            req = _parse_ver_tuple(part[1:])
            if req and (act[0] != req[0] or act < req):
                return False
        elif part.startswith("~"):
            req = _parse_ver_tuple(part[1:])
            if len(req) >= 2 and (act[:2] != req[:2] or act < req):
                return False
        else:
            cleaned = re.sub(r"\.[xX*]+$", "", part.lstrip("vV="))
            req = _parse_ver_tuple(cleaned)
            if req and act[: len(req)] != req:
                return False
    return True


def _dotnet_sdk_compatible(required: str, roll_forward: str, installed: list[str]) -> bool:
    req = _parse_ver_tuple(required)
    if not req or not installed:
        return True
    policy = (roll_forward or "latestPatch").strip().lower()
    for sdk in installed:
        cur = _parse_ver_tuple(sdk)
        if not cur:
            continue
        if policy in ("latestmajor", "major") and cur >= req:
            return True
        if policy in ("latestminor", "minor", "latestfeature", "feature") and cur[0] == req[0] and cur >= req:
            return True
        if policy == "disable" and cur == req:
            return True
        # Default (`latestPatch` / `patch`): same major.minor and patch >= req (or feature band match)
        if len(req) >= 2 and len(cur) >= 2 and cur[0] == req[0] and cur[1] == req[1] and cur >= req:
            return True
    return False


def check_toolchain_versions(root: Path, run: ProbeRun, which: WhichFn) -> list[dict[str, Any]]:
    """Check repository-pinned toolchain version files against installed CLI versions."""
    findings: list[dict[str, Any]] = []

    # 1. .NET global.json
    global_json = root / "global.json"
    if global_json.is_file() and which("dotnet"):
        try:
            sdk_cfg = (json.loads(global_json.read_text(encoding="utf-8")).get("sdk") or {})
            req_ver = str(sdk_cfg.get("version") or "").strip()
            roll = str(sdk_cfg.get("rollForward") or "latestPatch")
        except (OSError, ValueError, AttributeError):
            req_ver, roll = "", "latestPatch"
        if req_ver:
            code, out = run(["dotnet", "--list-sdks"])
            sdks = [line.split()[0] for line in out.splitlines() if line.strip() and re.match(r"^\d+\.", line.strip())]
            if code == 0 and sdks and not _dotnet_sdk_compatible(req_ver, roll, sdks):
                findings.append({
                    "id": "version-dotnet",
                    "status": "fail",
                    "title": f".NET {req_ver} is required",
                    "detail": f"`global.json` pins .NET SDK {req_ver} (rollForward: {roll}), and this computer has {', '.join(sdks[:3])}.",
                    "fix": f"Install .NET SDK {req_ver} from https://dotnet.microsoft.com/download or adjust `rollForward` in `global.json`",
                    "route": "",
                })

    # 2. Node (.nvmrc, .node-version, or package.json engines.node)
    node_pin, node_source = "", ""
    for fname in (".nvmrc", ".node-version"):
        p = root / fname
        if p.is_file():
            val = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            if val and re.search(r"\d", val[0]):
                node_pin, node_source = val[0].strip(), fname
                break
    if not node_pin and (root / "package.json").is_file():
        try:
            eng = (json.loads((root / "package.json").read_text(encoding="utf-8")).get("engines") or {}).get("node")
            if isinstance(eng, str) and re.search(r"\d", eng):
                node_pin, node_source = eng.strip(), "package.json"
        except (OSError, ValueError, AttributeError):
            pass
    if node_pin and which("node"):
        code, out = run(["node", "--version"])
        actual = out.strip().lstrip("v")
        if code == 0 and actual and not _semver_satisfies(actual, node_pin):
            findings.append({
                "id": "version-node",
                "status": "fail",
                "title": f"Node.js {node_pin} is required",
                "detail": f"`{node_source}` requires Node {node_pin}, and this computer has {actual}.",
                "fix": f"Switch or upgrade Node.js to match `{node_source}`",
                "route": "",
            })

    # 3. Python (.python-version or pyproject.toml requires-python)
    py_pin, py_source = "", ""
    py_ver_file = root / ".python-version"
    if py_ver_file.is_file():
        lines = [l.strip() for l in py_ver_file.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip() and not l.startswith("#")]
        if lines and re.search(r"\d", lines[0]):
            py_pin, py_source = lines[0], ".python-version"
    if not py_pin and (root / "pyproject.toml").is_file():
        text = (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
        m = re.search(r'^\s*requires-python\s*=\s*["\']([^"\']+)["\']', text, re.M)
        if m:
            py_pin, py_source = m.group(1).strip(), "pyproject.toml"
    if py_pin and which("python3"):
        code, out = run(["python3", "--version"])
        actual = re.sub(r"^Python\s+", "", out.strip(), flags=re.I)
        if code == 0 and actual and not _semver_satisfies(actual, py_pin):
            findings.append({
                "id": "version-python",
                "status": "fail",
                "title": f"Python {py_pin} is required",
                "detail": f"`{py_source}` requires Python {py_pin}, and this computer has {actual}.",
                "fix": f"Install a matching Python version (`brew install python` or `uv python install`)",
                "route": "",
            })

    # 4. Go (go.mod `go 1.22`)
    go_mod = root / "go.mod"
    if go_mod.is_file() and which("go"):
        m = re.search(r"^\s*go\s+(\d+\.\d+(?:\.\d+)?)", go_mod.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            req_go = m.group(1)
            code, out = run(["go", "version"])
            ver_m = re.search(r"go(\d+\.\d+(?:\.\d+)?)", out)
            if code == 0 and ver_m and not _semver_satisfies(ver_m.group(1), f">={req_go}"):
                findings.append({
                    "id": "version-go",
                    "status": "fail",
                    "title": f"Go {req_go}+ is required",
                    "detail": f"`go.mod` requires Go {req_go}+, and this computer has {ver_m.group(1)}.",
                    "fix": "Upgrade Go (`brew upgrade go`)",
                    "route": "",
                })

    # 5. Rust (rust-toolchain.toml / rust-toolchain)
    rust_pin, rust_source = "", ""
    if (root / "rust-toolchain.toml").is_file():
        m = re.search(r'^\s*channel\s*=\s*["\']([^"\']+)["\']', (root / "rust-toolchain.toml").read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            rust_pin, rust_source = m.group(1).strip(), "rust-toolchain.toml"
    elif (root / "rust-toolchain").is_file():
        rust_pin = (root / "rust-toolchain").read_text(encoding="utf-8", errors="replace").strip()
        rust_source = "rust-toolchain"
    if rust_pin and re.match(r"^\d+\.\d+", rust_pin) and which("rustc"):
        code, out = run(["rustc", "--version"])
        ver_m = re.search(r"rustc\s+(\d+\.\d+(?:\.\d+)?)", out)
        if code == 0 and ver_m and not _semver_satisfies(ver_m.group(1), f">={rust_pin}"):
            findings.append({
                "id": "version-rust",
                "status": "fail",
                "title": f"Rust {rust_pin}+ is required",
                "detail": f"`{rust_source}` pins Rust {rust_pin}, and this computer has {ver_m.group(1)}.",
                "fix": f"Run `rustup toolchain install {rust_pin}` or `brew upgrade rust`",
                "route": "",
            })

    # 6. Ruby (.ruby-version)
    ruby_ver = root / ".ruby-version"
    if ruby_ver.is_file() and which("ruby"):
        req_rb = re.sub(r"^ruby-", "", ruby_ver.read_text(encoding="utf-8", errors="replace").strip())
        if re.match(r"^\d+\.\d+", req_rb):
            code, out = run(["ruby", "--version"])
            ver_m = re.search(r"ruby\s+(\d+\.\d+(?:\.\d+)?)", out)
            if code == 0 and ver_m and not _semver_satisfies(ver_m.group(1), req_rb):
                findings.append({
                    "id": "version-ruby",
                    "status": "fail",
                    "title": f"Ruby {req_rb} is required",
                    "detail": f"`.ruby-version` pins Ruby {req_rb}, and this computer has {ver_m.group(1)}.",
                    "fix": f"Switch or install Ruby {req_rb}",
                    "route": "",
                })

    return findings


# ---- Layer 3: Platform SDK, license, and build-module probes (Unity & Android) ----

def _find_unity_project_dir(root: Path) -> Path | None:
    for candidate in (root, *[p for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")][:15]) if root.is_dir() else ():
        if (candidate / "ProjectSettings" / "ProjectVersion.txt").is_file() and (candidate / "Assets").is_dir():
            return candidate
    return None


def _unity_project_version(unity_dir: Path) -> str:
    try:
        text = (unity_dir / "ProjectSettings" / "ProjectVersion.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r"m_EditorVersion:\s*([0-9][0-9A-Za-z._-]{1,30})", text)
    return m.group(1) if m else ""


def _has_unity_license(home: Path | None = None) -> bool:
    if os.environ.get("UNITY_LICENSE") or os.environ.get("UNITY_SERIAL"):
        return True
    base = (home or Path.home()) / "Library" / "Application Support"
    unity_dir = base / "Unity"
    if unity_dir.is_dir() and any(unity_dir.glob("*.ulf")):
        return True
    hub_dir = base / "UnityHub"
    if hub_dir.is_dir() and any((hub_dir / name).exists() for name in ("userInfo.json", "prefs.json")):
        return True
    return False


def check_platform_sdks(
    root: Path,
    config: Mapping[str, Any],
    which: WhichFn,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    unity_editors_root: Path = Path("/Applications/Unity/Hub/Editor"),
) -> list[dict[str, Any]]:
    """Probe Unity licenses/playback modules and Android SDK/JDK readiness when applicable."""
    env_map = os.environ if env is None else env
    home_dir = home or Path.home()
    out: list[dict[str, Any]] = []

    # 1. Unity license + target playback module
    unity_dir = _find_unity_project_dir(root)
    if unity_dir is not None:
        version = _unity_project_version(unity_dir)
        editor_bin = env_map.get("UNITY_PATH") or (str(unity_editors_root / version / "Unity.app/Contents/MacOS/Unity") if version else "")
        if editor_bin and which(editor_bin):
            if not _has_unity_license(home_dir):
                out.append({
                    "id": "unity-license",
                    "status": "warn",
                    "title": "Unity license couldn't be confirmed",
                    "detail": "No local Unity `.ulf` license or signed-in Unity Hub profile was found; batchmode builds need an active Unity license.",
                    "fix": "Open Unity Hub and sign in to activate your Personal or Pro license",
                    "route": "",
                })
            # Check target playback support module when delivery/build targets iOS or Android
            combined_cmds = " ".join(str(config.get(k) or "") for k in ("build_command", "test_command", "delivery_build_command"))
            needed_modules: list[str] = []
            if version and (re.search(r"-buildTarget\s+iOS\b|package_ios|\.ipa\b", combined_cmds, re.I) or str(config.get("delivery_artifact") or "").endswith(".ipa")):
                needed_modules.append("ios")
            if version and (re.search(r"-buildTarget\s+Android\b|\.apk\b|\.aab\b", combined_cmds, re.I) or str(config.get("delivery_artifact") or "").endswith((".apk", ".aab"))):
                needed_modules.append("android")
            editor_root = Path(editor_bin).parents[3] if "/Unity.app/" in editor_bin else (unity_editors_root / version)
            for mod in needed_modules:
                mod_path = editor_root / "PlaybackEngines" / _UNITY_MODULE_DIRS[mod]
                if not mod_path.is_dir():
                    mod_info = describe_tool(f"unity-module:{mod}:{version}")
                    out.append({
                        "id": f"unity-module-{mod}",
                        "status": "fail",
                        "title": f"{mod_info['name']} isn't installed",
                        "detail": f"Building this Unity project for {mod.upper()} requires `{_UNITY_MODULE_DIRS[mod]}` under Unity {version}.",
                        "fix": mod_info["manual_hint"],
                        "route": "",
                        "missing_tools": [mod_info],
                    })

    # 2. Android / Gradle projects
    is_android = any((root / p).is_file() for p in ("app/src/main/AndroidManifest.xml", "AndroidManifest.xml")) or (
        any((root / g).is_file() for g in ("build.gradle", "build.gradle.kts"))
        and any("com.android" in (root / g).read_text(encoding="utf-8", errors="replace") for g in ("build.gradle", "build.gradle.kts") if (root / g).is_file())
    )
    if is_android:
        if not which("java"):
            java_info = describe_tool("java")
            out.append({
                "id": "android-jdk",
                "status": "fail",
                "title": "Java (JDK) isn't installed",
                "detail": "Android and Gradle builds require a JDK (`java`), which isn't installed on this computer.",
                "fix": java_info["manual_hint"],
                "route": "",
                "missing_tools": [java_info],
            })
        sdk_candidates = [
            env_map.get("ANDROID_HOME") or "",
            env_map.get("ANDROID_SDK_ROOT") or "",
            str(home_dir / "Library" / "Android" / "sdk"),
            *(["/opt/homebrew/share/android-commandlinetools"] if home is None else []),
        ]
        sdk_dir = next((Path(p) for p in sdk_candidates if p and Path(p).is_dir()), None)
        if sdk_dir is None:
            out.append({
                "id": "android-sdk",
                "status": "fail",
                "title": "Android SDK isn't installed",
                "detail": "No Android SDK was found in `ANDROID_HOME` or `~/Library/Android/sdk`.",
                "fix": "Install Android Studio (or `brew install --cask android-commandlinetools`) and set `ANDROID_HOME`",
                "route": "",
            })
        elif not (sdk_dir / "licenses" / "android-sdk-license").is_file():
            out.append({
                "id": "android-licenses",
                "status": "warn",
                "title": "Android SDK licenses haven't been accepted",
                "detail": f"Found Android SDK at `{sdk_dir}`, without `licenses/android-sdk-license`.",
                "fix": "Run `sdkmanager --licenses` or accept licenses in Android Studio > SDK Manager",
                "route": "",
            })

    return out


# ---- Non-interactive installer ----

def install_tool(
    tool_id: str,
    say: Callable[[str], None] = lambda s: None,
    *,
    run: Runner | None = None,
    which: WhichFn | None = None,
    unity_hub_bin: Path = UNITY_HUB_BIN,
    unity_editors_root: Path = Path("/Applications/Unity/Hub/Editor"),
) -> dict[str, Any]:
    """Install a curated missing build/test tool or Unity module non-interactively on this computer."""
    tid = canonical_tool_id(tool_id)
    if not is_valid_install_id(tid):
        msg = f"Automatic installation isn't supported for '{tool_id}'."
        say(f"Not done: {msg}")
        return {"ok": False, "tool": tid, "message": msg}

    probe = which or default_which
    exec_run = run or _default_run

    if tid == "docker":
        from orchestrator import servers
        res = servers.install_docker(servers.LocalShell(Path.cwd()), say)
        ok = bool(res.get("ok"))
        msg = str(res.get("message") or ("Docker is installed and running." if ok else "Docker install failed."))
        say(("Done: " if ok else "Not done: ") + msg)
        return {"ok": ok, "tool": "docker", "message": msg}

    if m := UNITY_MODULE_ID_RE.match(tid):
        module, version = m.group(1), m.group(2)
        mod_dir = unity_editors_root / version / "PlaybackEngines" / _UNITY_MODULE_DIRS[module]
        if mod_dir.is_dir():
            msg = f"{_UNITY_MODULE_LABELS[module]} ({version}) is already installed."
            say(f"Done: {msg}")
            return {"ok": True, "tool": tid, "message": msg}
        if not unity_hub_bin.is_file():
            msg = f"Unity Hub isn't installed at {unity_hub_bin}. Install Unity Hub from https://unity.com/download first."
            say(f"Not done: {msg}")
            return {"ok": False, "tool": tid, "message": msg}
        say(f"Installing {_UNITY_MODULE_LABELS[module]} for Unity {version} via Unity Hub …")
        code, output, timed_out = exec_run(
            [str(unity_hub_bin), "--", "--headless", "install-modules", "--version", version, "-m", module, "--childModules"],
            1800,
        )
        tail = "\n".join(output.strip().splitlines()[-8:])
        if tail:
            say(tail)
        if timed_out or code != 0:
            reason = "Unity Hub module install timed out" if timed_out else f"Unity Hub exited with {code}"
            detail = (output.strip().splitlines() or [""])[-1][:200]
            msg = reason + (f": {detail}" if detail else "")
            say(f"Not done: {msg}")
            return {"ok": False, "tool": tid, "message": msg}
        msg = f"{_UNITY_MODULE_LABELS[module]} ({version}) is installed."
        say(f"Done: {msg}")
        return {"ok": True, "tool": tid, "message": msg}

    if m := UNITY_VERSION_ID_RE.match(tid):
        version = m.group(1)
        unity_bin = f"/Applications/Unity/Hub/Editor/{version}/Unity.app/Contents/MacOS/Unity"
        if probe(unity_bin):
            msg = f"Unity {version} is already installed."
            say(f"Done: {msg}")
            return {"ok": True, "tool": tid, "message": msg}
        if not unity_hub_bin.is_file():
            msg = f"Unity Hub isn't installed at {unity_hub_bin}. Install Unity Hub from https://unity.com/download first."
            say(f"Not done: {msg}")
            return {"ok": False, "tool": tid, "message": msg}
        say(f"Installing Unity {version} via Unity Hub (this downloads ~3–5 GB and may take several minutes) …")
        code, output, timed_out = exec_run([str(unity_hub_bin), "--", "--headless", "install", "--version", version], 1800)
        tail = "\n".join(output.strip().splitlines()[-8:])
        if tail:
            say(tail)
        if timed_out or code != 0:
            reason = "Unity Hub install timed out" if timed_out else f"Unity Hub exited with {code}"
            detail = (output.strip().splitlines() or [""])[-1][:200]
            msg = reason + (f": {detail}" if detail else "")
            say(f"Not done: {msg}")
            return {"ok": False, "tool": tid, "message": msg}
        if not probe(unity_bin):
            msg = f"Unity Hub finished, but {unity_bin} was not found. Open Unity Hub to confirm the install and license."
            say(f"Not done: {msg}")
            return {"ok": False, "tool": tid, "message": msg}
        msg = f"Unity {version} is installed."
        say(f"Done: {msg}")
        return {"ok": True, "tool": tid, "message": msg}

    recipe = _BREW_RECIPES[tid]
    if probe(recipe["binary"]):
        msg = f"{recipe['name']} is already installed."
        say(f"Done: {msg}")
        return {"ok": True, "tool": tid, "message": msg}
    if not probe("brew"):
        msg = f"Homebrew isn't installed, so {recipe['name']} can't be installed automatically. Install Homebrew (https://brew.sh) or follow: {recipe['manual_hint']}"
        say(f"Not done: {msg}")
        return {"ok": False, "tool": tid, "message": msg}

    say(f"Installing {recipe['name']} via Homebrew (`brew install {recipe['formula']}`) …")
    code, output, timed_out = exec_run(["brew", "install", recipe["formula"]], 900)
    tail = "\n".join(output.strip().splitlines()[-10:])
    if tail:
        say(tail)
    if timed_out or code != 0:
        reason = "brew install timed out" if timed_out else f"brew install exited with {code}"
        detail = (output.strip().splitlines() or [""])[-1][:200]
        msg = reason + (f": {detail}" if detail else "")
        say(f"Not done: {msg}")
        return {"ok": False, "tool": tid, "message": msg}

    if not probe(recipe["binary"]):
        msg = f"Homebrew installed {recipe['formula']}, but `{recipe['binary']}` still isn't on PATH."
        say(f"Not done: {msg}")
        return {"ok": False, "tool": tid, "message": msg}
    msg = f"{recipe['name']} is installed and ready."
    say(f"Done: {msg}")
    return {"ok": True, "tool": tid, "message": msg}
