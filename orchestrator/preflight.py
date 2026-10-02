"""Will this machine be able to build and test the project? Found out before a job fails halfway.

Every check is a cheap probe (a `which`, a version command, `simctl list`) run through injected `run` and `which`
functions, so it is testable without Xcode or a phone. Each result says what it found, whether it blocks work
(`fail`), might (`warn`) or is fine (`ok`), and how to fix it. Stack-specific checks only run for projects that
need them: Xcode and simulator checks only for Apple app projects.
"""
from __future__ import annotations

import re
import shlex
from typing import Any, Callable

from orchestrator.env_issues import detect_simulator_environment_issue
from orchestrator.run_check import MIN_DISK_GB

Run = Callable[[list[str]], tuple[int, str]]   # argv -> (exit code, combined output); (127, "") when the tool is missing
Which = Callable[[str], bool]


def _item(id_: str, status: str, title: str, detail: str, fix: str = "", route: str = "") -> dict[str, str]:
    return {"id": id_, "status": status, "title": title, "detail": detail, "fix": fix, "route": route}


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


def checks(config: dict[str, Any], machines: list[dict[str, Any]], is_apple_app: bool, disk_free_gb: float | None,
           run: Run, which: Which, model_clis: Callable[[str], list[str]] = lambda m: []) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    # 1. The programs the build and test commands start.
    for label, command in (("build", config.get("build_command")), ("test", config.get("test_command"))):
        if not command:
            continue
        missing = [t for t in dict.fromkeys(command_tools(command)) if not t.startswith(("./", "/")) and not which(t)]
        if missing:
            out.append(_item(f"{label}-tool", "fail", f"The {label} command can't run", f"`{command}` needs {', '.join(missing)}, which isn't installed or isn't on the PATH.",
                             "Install it, or change the command", "#/config/project"))
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

    # 3. GitHub: needed to open pull requests and read CI.
    if which("gh"):
        code, text = run(["gh", "auth", "status"])
        out.append(_item("github", "ok" if code == 0 else "warn", "GitHub is signed in" if code == 0 else "GitHub isn't signed in",
                         "Pull requests and CI status will work." if code == 0 else "Jobs will build, but no pull request can be opened and CI status won't show.",
                         "" if code == 0 else "Run `gh auth login` in a terminal"))
    else:
        out.append(_item("github", "warn", "The GitHub CLI isn't installed", "Jobs will build, but no pull request can be opened.", "Install it with `brew install gh`"))

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

    # 5. Room to build.
    if disk_free_gb is not None and local and disk_free_gb < MIN_DISK_GB:
        out.append(_item("disk", "fail", "Not enough free disk space", f"{disk_free_gb:.0f} GB free; builds want {MIN_DISK_GB} GB.", "Free some space, or lower ORCHESTRATOR_MIN_DISK_GB for a small project"))
    return out


def failures(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return [i for i in items if i["status"] == "fail"]
