"""Will this machine be able to build and test the project? Found out before a job fails halfway.

Every check is a cheap probe (a `which`, a version command, `simctl list`) run through injected `run` and `which`
functions, so it is testable without Xcode or a phone. Each result says what it found, whether it blocks work
(`fail`), might (`warn`) or is fine (`ok`), and how to fix it. Stack-specific checks only run for projects that
need them: Xcode and simulator checks only for Apple app projects.
"""
from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from typing import Any, Callable

from orchestrator.env_issues import detect_simulator_environment_issue
from orchestrator.run_check import MIN_DISK_GB

Run = Callable[[list[str]], tuple[int, str]]   # argv -> (exit code, combined output); (127, "") when the tool is missing
Which = Callable[[str], bool]


def _item(id_: str, status: str, title: str, detail: str, fix: str = "", route: str = "", **extra: Any) -> dict[str, Any]:
    return {"id": id_, "status": status, "title": title, "detail": detail, "fix": fix, "route": route, **extra}


def command_tools(command: str | None) -> list[str]:
    """The programs a shell command line starts: `cd x && FOO=1 npm test | tee y` -> ['npm', 'tee']."""
    tools: list[str] = []
    for segment in re.split(r"&&|\|\||;|\|", command or ""):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]
        if words and words[0] not in {"cd", "export", "set", "true", "false", "test", "echo", "exit", "[", "source", "."}:
            tools.append(words[0])
    return tools


def _resolve_tool(tool: str, env: Mapping[str, str]) -> str | None:
    """Resolve simple variables and literal defaults without evaluating shell code."""
    default = re.fullmatch(r"\$\{([A-Za-z_]\w*):-([^$`{}\\]+)\}", tool)
    if default:
        return env.get(default[1]) or default[2]
    name = re.fullmatch(r"\$([A-Za-z_]\w*)|\$\{([A-Za-z_]\w*)\}", tool)
    if name:
        return env.get(name[1] or name[2]) or None
    return None if "$" in tool or "`" in tool else tool


def _project_requires_checks(
    root: Path,
    config: Mapping[str, Any],
    run: Run,
    which: Which,
    env: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Evaluate top-level `requires` and `per-job` `test_commands` `requires` in preflight."""
    from orchestrator import build_tools, servers

    reqs: list[str] = []
    top = config.get("requires")
    if isinstance(top, list):
        reqs.extend(r for r in top if isinstance(r, str))
    for entry in config.get("test_commands") or []:
        if isinstance(entry, dict) and str(entry.get("tier") or "per-job") == "per-job":
            for r in entry.get("requires") or []:
                if isinstance(r, str) and r not in reqs:
                    reqs.append(r)
    if not reqs:
        return []

    out: list[dict[str, Any]] = []
    docker_checked = False
    docker_up = False
    compose_servers: list[dict[str, Any]] | None = None

    def is_docker_up() -> bool:
        nonlocal docker_checked, docker_up
        if not docker_checked:
            docker_checked = True
            docker_up = which("docker") and run(["docker", "info"])[0] == 0
        return docker_up

    def get_servers() -> list[dict[str, Any]]:
        nonlocal compose_servers
        if compose_servers is None:
            try:
                compose_servers = servers.find(root)
            except Exception:
                compose_servers = []
        return compose_servers

    seen_ids: set[str] = set()
    for req in reqs:
        kind, _, val = req.partition(":")
        if kind == "command" and val:
            if not which(val):
                t_info = build_tools.describe_tool(val)
                iid = f"req-cmd-{t_info['id']}"
                if iid not in seen_ids:
                    seen_ids.add(iid)
                    out.append(_item(iid, "fail", f"{t_info['name']} isn't installed",
                                     f"Project requirement `command:{val}` isn't available on this computer.",
                                     t_info["manual_hint"], missing_tools=[t_info]))
        elif kind == "docker":
            if not which("docker"):
                iid = "req-docker"
                if iid not in seen_ids:
                    seen_ids.add(iid)
                    d_info = build_tools.describe_tool("docker")
                    out.append(_item(iid, "fail", "Docker isn't installed",
                                     "This project requires Docker, which isn't installed on this computer.",
                                     d_info["manual_hint"], missing_tools=[d_info]))
            elif not is_docker_up():
                iid = "req-docker"
                if iid not in seen_ids:
                    seen_ids.add(iid)
                    srvs = get_servers()
                    out.append(_item(iid, "fail", "Docker isn't running",
                                     "This project requires Docker, and the Docker daemon isn't running.",
                                     "Start Docker or click Set up environment",
                                     **({"start_servers": srvs[:1]} if srvs else {"missing_tools": [build_tools.describe_tool("docker")]})))
        elif kind == "container" and val:
            if not which("docker"):
                if "req-docker" not in seen_ids:
                    seen_ids.add("req-docker")
                    d_info = build_tools.describe_tool("docker")
                    out.append(_item("req-docker", "fail", "Docker isn't installed",
                                     f"Container `{val}` requires Docker, which isn't installed on this computer.",
                                     d_info["manual_hint"], missing_tools=[d_info]))
            else:
                code, c_out = run(["docker", "inspect", "--format", "{{.State.Running}}", val])
                if code != 0 or c_out.strip() != "true":
                    iid = f"req-container-{val}"
                    if iid not in seen_ids:
                        seen_ids.add(iid)
                        srvs = get_servers()
                        out.append(_item(iid, "fail", f"Container `{val}` isn't running",
                                         f"Tests require the `{val}` Docker container to be up.",
                                         "Start the project server before running tests",
                                         **({"start_servers": srvs} if srvs else {})))
        elif kind == "env" and val:
            if not env.get(val):
                iid = f"req-env-{val}"
                if iid not in seen_ids:
                    seen_ids.add(iid)
                    out.append(_item(iid, "fail", f"Environment variable `{val}` isn't set",
                                     f"This project requires `{val}` in the environment.",
                                     f"Export `{val}` or configure it in `.env` before running jobs"))
        elif kind == "path" and val:
            if not (root / val).exists():
                iid = f"req-path-{val}"
                if iid not in seen_ids:
                    seen_ids.add(iid)
                    out.append(_item(iid, "fail", f"Required path `{val}` is missing",
                                     f"`{val}` doesn't exist in this checkout yet.",
                                     f"Prepare `{val}` before running builds or tests"))
        elif kind == "device" and val == "ios":
            code, d_out = run(["xcrun", "xctrace", "list", "devices"]) if which("xcrun") else (127, "")
            has_ios = False
            if code == 0:
                in_devs = False
                for line in d_out.splitlines():
                    s = line.strip()
                    if s.startswith("== Devices =="):
                        in_devs = True
                    elif s.startswith("=="):
                        in_devs = False
                    elif in_devs and s and "Mac" not in s and "Simulator" not in s:
                        has_ios = True
                        break
            if not has_ios and "req-device-ios" not in seen_ids:
                seen_ids.add("req-device-ios")
                out.append(_item("req-device-ios", "fail", "No physical iOS device is connected",
                                 "This project requires a connected, unlocked iPhone or iPad.",
                                 "Connect and unlock an iOS device over USB, or run on a Mac with one paired"))
    return out


def checks(config: dict[str, Any], machines: list[dict[str, Any]], is_apple_app: bool, disk_free_gb: float | None,
           run: Run, which: Which, model_clis: Callable[[str], list[str]] = lambda m: [],
           github: bool = True, *, env: Mapping[str, str] | None = None,
           claude_plugin_findings: list[dict[str, str]] | None = None,
           root: Any = None) -> list[dict[str, Any]]:
    """`github`: the project uses GitHub issues and pull requests (code_host); plain git needs no GitHub check.
    `claude_plugin_findings`: project-scoped Claude Code plugins in a macOS-protected folder (`claude_plugins.findings`)."""
    out: list[dict[str, str]] = []

    # 1. The programs the build and test commands start.
    from orchestrator import build_tools
    for label, command in (("build", config.get("build_command")), ("test", config.get("test_command"))):
        if not command:
            continue
        tools = [_resolve_tool(tool, env or {}) for tool in command_tools(command)]
        missing = [t for t in dict.fromkeys(tools) if t and not t.startswith(("./", "../")) and not which(t)]
        if missing:
            tool_infos = [build_tools.describe_tool(t) for t in missing]
            out.append(_item(f"{label}-tool", "fail", f"The {label} command can't run",
                             f"This computer couldn't find an executable for {', '.join(missing)}.",
                             f"Install the tool or update `{label}_command` in `.orchestrator/project.json`",
                             missing_tools=tool_infos))
        elif None in tools:
            out.append(_item(f"{label}-tool", "warn", f"The {label} command uses a dynamic executable",
                             "The shell chooses the executable when the command runs."))
        else:
            out.append(_item(f"{label}-tool", "ok", f"The {label} command's tools are installed", f"`{command}`"))

    # 2. The AI command-line tools the models on this computer need.
    local = [m for m in machines if m.get("enabled", True) and m.get("execution_mode", "local") == "local"]
    needed: dict[str, list[str]] = {}
    for m in local:
        for model in m.get("models", []):
            for cli in model_clis(model):
                needed.setdefault(cli, []).append(model)
    missing_clis = {cli: models for cli, models in needed.items() if not which(cli)}
    if missing_clis:
        detail = "; ".join(f"{cli} (for {', '.join(sorted(set(models))[:3])})" for cli, models in missing_clis.items())
        out.append(_item("model-clis", "fail", "A model's command-line tool is missing", f"Not installed: {detail}.", "Choose models", "#/config/models"))
    elif needed:
        out.append(_item("model-clis", "ok", "The models' command-line tools are installed", ", ".join(sorted(needed))))

    # 3. GitHub, for a project that uses it: each job is a GitHub issue, so without it no job can start.
    if github and which("gh"):
        code, text = run(["gh", "auth", "status"])
        out.append(_item("github", "ok" if code == 0 else "fail", "GitHub is signed in" if code == 0 else "GitHub isn't signed in",
                         "Issues, pull requests and CI status will work." if code == 0
                         else "Jobs can't start: each one is a GitHub issue. Or set code_host to \"git\" to work without GitHub.",
                         "" if code == 0 else "Run `gh auth login` in a terminal"))
    elif github:
        out.append(_item("github", "fail", "The GitHub CLI isn't installed",
                         "Jobs can't start: each one is a GitHub issue. Or set code_host to \"git\" to work without GitHub.",
                         "Install it with `brew install gh`"))

    # 4. Apple apps: Xcode, its first-launch setup, and a simulator that works.
    if is_apple_app:
        if not which("xcodebuild"):
            out.append(_item("xcode", "fail", "Xcode isn't installed", "This project builds with Xcode, and `xcodebuild` isn't available.", "Install Xcode, then run `xcode-select --install`"))
        else:
            code, text = run(["xcode-select", "-p"])
            if code != 0:
                out.append(_item("xcode", "fail", "No Xcode is selected", text.strip()[:160] or "xcode-select has no developer directory.", "Run `sudo xcode-select -s /Applications/Xcode.app`"))
            else:
                code, text = run(["xcodebuild", "-checkFirstLaunchStatus"])
                out.append(_item("xcode", "ok" if code == 0 else "fail", "Xcode is ready" if code == 0 else "Xcode needs its first-launch setup",
                                 text.strip()[:160] if code != 0 else "Selected and set up.", "" if code == 0 else "Open Xcode once, or run `sudo xcodebuild -runFirstLaunch`"))
            code, text = run(["xcrun", "simctl", "list", "devices", "available"])
            problem = detect_simulator_environment_issue(text)
            if problem or code != 0:
                out.append(_item("simulator", "fail", "The simulator can't be used", problem or (text.strip()[:200] or "simctl failed."), "Restart CoreSimulator or the Mac, then reopen Xcode"))
            elif not re.search(r"\((?:Booted|Shutdown)\)", text):
                out.append(_item("simulator", "warn", "No simulator is installed", "UI tests and visual checks need one.", "Xcode > Settings > Components"))
            else:
                out.append(_item("simulator", "ok", "A simulator is available", "UI tests and visual checks can run."))
        if local and not any(m.get("supports_xcode") for m in local) and not any(m.get("execution_mode") == "remote" and m.get("supports_xcode") for m in machines):
            out.append(_item("xcode-machine", "fail", "No machine is marked as able to build with Xcode", "Jobs for this project will have nowhere to run.", "Open Machines", "#/config/fleet"))

    # 5. Repository-level version pins, manifest lockfiles, platform SDKs, and declarative `requires` (when root is supplied).
    if root is not None:
        from pathlib import Path as _Path
        from orchestrator import manifest_deps
        root_path = _Path(root)
        out.extend(build_tools.check_toolchain_versions(root_path, run, which))
        out.extend(manifest_deps.preflight_items_for_manifests(root_path, config, which=which))
        out.extend(build_tools.check_platform_sdks(root_path, config, which, env=env))
        out.extend(_project_requires_checks(root_path, config, run, which, env or {}))

    # 6. Claude Code plugins that can make `claude -p` wait for a macOS permission question nobody can answer.
    if claude_plugin_findings:
        folders = sorted({f["folder"] for f in claude_plugin_findings})
        out.append(_item("claude-plugins", "warn", "A Claude Code plugin may make Claude calls hang",
                         "; ".join(f"{f['plugin']} is installed for {f['path']}" for f in claude_plugin_findings)
                         + ". In a git repository Claude opens that folder at startup, and macOS can hold it for a permission question.",
                         f"Allow {' and '.join(folders)} for the app that runs Orchestrator (System Settings > Privacy & Security > "
                         "Files and Folders), or remove the entry from ~/.claude/plugins/installed_plugins.json"))

    # 6. Room to build.
    if disk_free_gb is not None and local and disk_free_gb < MIN_DISK_GB:
        out.append(_item("disk", "fail", "Not enough free disk space", f"{disk_free_gb:.0f} GB free; builds want {MIN_DISK_GB} GB.", "Free some space, or lower ORCHESTRATOR_MIN_DISK_GB for a small project"))
    return out


def failures(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return [i for i in items if i["status"] == "fail"]
