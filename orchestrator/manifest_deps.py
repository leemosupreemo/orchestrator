"""Manifest/lockfile dependency hydration and unified 1-click environment setup.

Consolidates missing host toolchains (`build_tools`), unhydrated project
lockfiles (`npm ci`, `uv sync`, `pod install`, `dotnet restore`, `flutter pub get`),
and stopped project Docker servers into a single catch-all setup plan and
runner so the UI never spams separate buttons per dependency.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from orchestrator import ai_providers, build_tools

ExecInDir = Callable[[list[str], Path, int], tuple[int, str, bool]]


def _default_exec(argv: list[str], cwd: Path, timeout: int = 900) -> tuple[int, str, bool]:
    env = {
        **os.environ,
        "PATH": ai_providers.tool_path(),
        "CI": "1",
        "HOMEBREW_NO_AUTO_UPDATE": "1",
    }
    try:
        res = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)
        return res.returncode, (res.stdout or "") + (res.stderr or ""), False
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        return 124, out, True
    except OSError as exc:
        return 127, str(exc), False


def _node_has_deps(pkg_json: Path) -> bool:
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return bool(data.get("dependencies") or data.get("devDependencies"))


def unhydrated_manifests(
    root: Path,
    config: Mapping[str, Any] | None = None,
    *,
    which: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Detect project lockfiles/manifests whose local packages haven't been installed yet."""
    if not root.is_dir():
        return []
    cfg = config or {}
    probe = which or build_tools.default_which
    found: list[dict[str, Any]] = []

    # 1. Node / JS / TS lockfiles in root
    pkg_json = root / "package.json"
    if pkg_json.is_file() and _node_has_deps(pkg_json) and not (root / "node_modules").is_dir():
        if (root / "pnpm-lock.yaml").is_file():
            found.append({
                "id": "manifest:pnpm",
                "ecosystem": "pnpm",
                "name": "pnpm packages",
                "tool": "pnpm",
                "argv": ["pnpm", "install", "--frozen-lockfile"],
                "cwd": ".",
                "summary": "Installs locked Node dependencies (`pnpm install --frozen-lockfile`).",
            })
        elif (root / "yarn.lock").is_file():
            found.append({
                "id": "manifest:yarn",
                "ecosystem": "yarn",
                "name": "Yarn packages",
                "tool": "yarn",
                "argv": ["yarn", "install", "--immutable"],
                "cwd": ".",
                "summary": "Installs locked Node dependencies (`yarn install --immutable`).",
            })
        elif (root / "bun.lock").is_file() or (root / "bun.lockb").is_file():
            found.append({
                "id": "manifest:bun",
                "ecosystem": "bun",
                "name": "Bun packages",
                "tool": "bun",
                "argv": ["bun", "install", "--frozen-lockfile"],
                "cwd": ".",
                "summary": "Installs locked Bun dependencies (`bun install --frozen-lockfile`).",
            })
        elif (root / "package-lock.json").is_file():
            found.append({
                "id": "manifest:npm",
                "ecosystem": "npm",
                "name": "npm packages",
                "tool": "npm",
                "argv": ["npm", "ci"],
                "cwd": ".",
                "summary": "Installs locked Node dependencies (`npm ci`).",
            })

    # 2. Python lockfiles (only when a lockfile or explicit .venv command is used — never on plain stdlib Python repos)
    cmds_text = f"{cfg.get('build_command') or ''} {cfg.get('test_command') or ''}"
    has_venv = (root / ".venv").is_dir() or (root / "venv").is_dir()
    if (root / "uv.lock").is_file() and not has_venv:
        found.append({
            "id": "manifest:uv",
            "ecosystem": "uv",
            "name": "Python environment (uv)",
            "tool": "uv",
            "argv": ["uv", "sync", "--frozen"],
            "cwd": ".",
            "summary": "Creates `.venv` and syncs locked packages (`uv sync --frozen`).",
        })
    elif (root / "poetry.lock").is_file() and not has_venv and ".venv" in cmds_text:
        found.append({
            "id": "manifest:poetry",
            "ecosystem": "poetry",
            "name": "Python environment (Poetry)",
            "tool": "poetry",
            "argv": ["poetry", "install", "--no-interaction"],
            "cwd": ".",
            "summary": "Installs locked Python packages (`poetry install`).",
        })
    elif (root / "requirements.txt").is_file() and not has_venv and re.search(r"(?:^|\s|['\"])\.?venv/bin/", cmds_text):
        found.append({
            "id": "manifest:pip",
            "ecosystem": "pip",
            "name": "Python virtualenv packages",
            "tool": "python3",
            "argv": ["sh", "-c", "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"],
            "cwd": ".",
            "summary": "Creates `.venv` and installs `requirements.txt`.",
        })

    # 3. CocoaPods (`Podfile.lock` missing or out of sync with `Pods/Manifest.lock`)
    podfile_lock = root / "Podfile.lock"
    manifest_lock = root / "Pods" / "Manifest.lock"
    if podfile_lock.is_file():
        needs_pods = not manifest_lock.is_file()
        if not needs_pods:
            try:
                needs_pods = podfile_lock.read_text(encoding="utf-8") != manifest_lock.read_text(encoding="utf-8")
            except OSError:
                needs_pods = True
        if needs_pods:
            found.append({
                "id": "manifest:cocoapods",
                "ecosystem": "cocoapods",
                "name": "CocoaPods dependencies",
                "tool": "pod",
                "argv": ["pod", "install"],
                "cwd": ".",
                "summary": "Installs iOS/macOS pods from `Podfile.lock` (`pod install`).",
            })

    # 4. Flutter / Dart (`pubspec.lock` without `.dart_tool/package_config.json`)
    pubspec = root / "pubspec.yaml"
    if pubspec.is_file() and (root / "pubspec.lock").is_file() and not (root / ".dart_tool" / "package_config.json").is_file():
        uses_flutter = "flutter:" in pubspec.read_text(encoding="utf-8", errors="replace")
        cli = "flutter" if (uses_flutter and probe("flutter")) else ("dart" if not uses_flutter else "flutter")
        found.append({
            "id": "manifest:pub",
            "ecosystem": "pub",
            "name": "Flutter / Dart packages" if uses_flutter else "Dart packages",
            "tool": cli,
            "argv": [cli, "pub", "get"],
            "cwd": ".",
            "summary": f"Fetches Dart/Flutter packages (`{cli} pub get`).",
        })

    return found


def preflight_items_for_manifests(root: Path, config: Mapping[str, Any] | None = None, *, which: Callable[[str], bool] | None = None) -> list[dict[str, Any]]:
    """Convert unhydrated manifests into preflight check items."""
    unhydrated = unhydrated_manifests(root, config, which=which)
    if not unhydrated:
        return []
    names = ", ".join(m["name"] for m in unhydrated)
    cmds = ", ".join(f"`{' '.join(m['argv'][:3])}`" for m in unhydrated)
    return [{
        "id": "manifest-deps",
        "status": "fail",
        "title": "Project packages aren't installed yet",
        "detail": f"Found lockfiles without local packages installed ({names}).",
        "fix": f"Set up environment or run {cmds}",
        "route": "",
        "manifest_deps": unhydrated,
    }]


def summarize_environment_setup(preflight_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Consolidate all failing preflight items into ONE non-spammy catch-all setup summary for the UI."""
    auto_tools: dict[str, dict[str, Any]] = {}
    manifests: dict[str, dict[str, Any]] = {}
    servers_to_start: dict[str, dict[str, Any]] = {}
    manual_items: list[dict[str, str]] = []

    for item in preflight_items:
        if item.get("status") != "fail":
            continue
        iid = str(item.get("id") or "")
        if iid in {"disk", "github", "model-clis", "xcode-machine"}:
            continue  # handled by their dedicated cards (disk cleanup, GitHub login, model picker, machines)
        handled = False
        for t in item.get("missing_tools") or []:
            if not isinstance(t, dict):
                continue
            handled = True
            tid = str(t.get("id") or t.get("binary") or "")
            if t.get("installable"):
                auto_tools.setdefault(tid, t)
            else:
                manual_items.append({
                    "id": tid,
                    "name": str(t.get("name") or tid),
                    "detail": str(t.get("summary") or item.get("detail") or ""),
                    "hint": str(t.get("manual_hint") or item.get("fix") or ""),
                })
        for m in item.get("manifest_deps") or []:
            if isinstance(m, dict) and m.get("id"):
                handled = True
                manifests.setdefault(str(m["id"]), m)
                # If the manifest's package manager CLI itself is missing and installable, ensure we install it first!
                mgr = build_tools.describe_tool(str(m.get("tool") or ""))
                if mgr.get("installable") and not build_tools.default_which(str(m.get("tool") or "")):
                    auto_tools.setdefault(mgr["id"], mgr)
        for srv in item.get("start_servers") or []:
            if isinstance(srv, dict) and srv.get("id"):
                handled = True
                servers_to_start.setdefault(str(srv["id"]), srv)
        if not handled and iid.startswith(("version-", "android-", "unity-", "req-", "xcode", "simulator")):
            manual_items.append({
                "id": iid,
                "name": str(item.get("title") or iid),
                "detail": str(item.get("detail") or ""),
                "hint": str(item.get("fix") or ""),
            })

    auto_steps: list[dict[str, Any]] = []
    for t in auto_tools.values():
        auto_steps.append({
            "kind": "tool",
            "id": t["id"],
            "name": t["name"],
            "summary": t.get("summary") or f"Install {t['name']}",
            "size_hint": t.get("size_hint") or "",
        })
    for m in manifests.values():
        auto_steps.append({
            "kind": "manifest",
            "id": m["id"],
            "name": m["name"],
            "summary": m.get("summary") or f"Install {m['name']}",
            "size_hint": "",
        })
    for s in servers_to_start.values():
        auto_steps.append({
            "kind": "server",
            "id": s["id"],
            "name": f"Start {s.get('name') or s['id']}",
            "summary": f"Starts Docker Compose service `{s.get('name') or s['id']}`.",
            "size_hint": "",
        })

    total = len(auto_steps) + len(manual_items)
    if total == 0:
        return {"ready": True, "title": "", "detail": "", "button_label": "", "auto_steps": [], "manual_items": []}

    all_names = [s["name"] for s in auto_steps] + [m["name"] for m in manual_items]
    if total == 1 and len(auto_steps) == 1:
        only = auto_steps[0]
        title = f"{only['name']} isn't set up" if only["kind"] != "tool" else f"{only['name']} isn't installed"
        button_label = f"Install {only['name']}…" if only["kind"] in ("tool", "manifest") else f"{only['name']}…"
    elif total == 1 and len(manual_items) == 1:
        title = manual_items[0]["name"]
        button_label = ""
    else:
        title = f"{total} dependencies needed on this computer"
        button_label = "Set up environment…" if auto_steps else ""

    shown = ", ".join(all_names[:3]) + (f", and {len(all_names) - 3} more" if len(all_names) > 3 else "")
    detail = f"This project needs {shown} before builds and tests can run here."
    return {
        "ready": False,
        "title": title,
        "detail": detail,
        "button_label": button_label,
        "auto_steps": auto_steps,
        "manual_items": manual_items,
    }


def setup_environment(
    root: Path,
    config: Mapping[str, Any],
    say: Callable[[str], None] = lambda s: None,
    *,
    tool_installer: Callable[[str, Callable[[str], None]], dict[str, Any]] | None = None,
    exec_in_dir: ExecInDir | None = None,
    which: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Run all automatic environment setup steps (tools -> manifests -> Docker servers) in a single pass."""
    from orchestrator import preflight, servers, test_commands

    probe = which or build_tools.default_which
    run_cmd = exec_in_dir or _default_exec
    install_fn = tool_installer or (lambda tid, log: build_tools.install_tool(tid, log))

    def probe_run(argv: list[str]) -> tuple[int, str]:
        code, out, _ = run_cmd(argv, root, 15)
        return code, out

    items = preflight.checks(
        dict(config),
        machines=[],
        is_apple_app=False,
        disk_free_gb=None,
        run=probe_run,
        which=probe,
        github=False,
        root=root,
    )
    plan = summarize_environment_setup(items)
    steps = plan["auto_steps"]
    if not steps:
        if plan["manual_items"]:
            hints = "; ".join(f"{m['name']}: {m['hint']}" for m in plan["manual_items"] if m.get("hint"))
            msg = f"No automatic steps left. Manual setup needed — {hints}"
            say(f"Not done: {msg}")
            return {"ok": False, "completed": [], "failed": [m["name"] for m in plan["manual_items"]], "message": msg}
        msg = "Environment is already set up."
        say(f"Done: {msg}")
        return {"ok": True, "completed": [], "failed": [], "message": msg}

    completed: list[str] = []
    failed: list[str] = []

    # 1. Install missing host tools / Unity modules
    for step in [s for s in steps if s["kind"] == "tool"]:
        say(f"==> Setting up {step['name']} …")
        res = install_fn(step["id"], say)
        if res.get("ok"):
            completed.append(step["name"])
        else:
            failed.append(f"{step['name']} ({res.get('message') or 'failed'})")

    # 2. Hydrate unhydrated project manifests (re-probing after tool installs)
    if not failed:
        for m in unhydrated_manifests(root, config, which=probe):
            say(f"==> Installing {m['name']} (`{' '.join(m['argv'])}`) …")
            cwd = root / m.get("cwd", ".")
            code, output, timed_out = run_cmd(m["argv"], cwd, 900)
            tail = "\n".join(output.strip().splitlines()[-8:])
            if tail:
                say(tail)
            if timed_out or code != 0:
                reason = "timed out" if timed_out else f"exited with {code}"
                failed.append(f"{m['name']} ({reason})")
            else:
                completed.append(m["name"])

    # 3. Start required Docker servers
    if not failed:
        found_servers = servers.find(root)
        shell = servers.LocalShell(root)
        for step in [s for s in steps if s["kind"] == "server"]:
            target = next((srv for srv in found_servers if srv["id"] == step["id"]), None)
            if not target:
                continue
            say(f"==> Starting {step['name']} …")
            res = servers.start(target, shell, say)
            if res.get("ok"):
                completed.append(step["name"])
            else:
                failed.append(f"{step['name']} ({res.get('message') or 'failed'})")

    if failed:
        msg = f"Couldn't finish setting up: {', '.join(failed)}."
        say(f"Not done: {msg}")
        return {"ok": False, "completed": completed, "failed": failed, "message": msg}

    # Check if manual items still remain after automatic steps succeeded
    if plan["manual_items"]:
        rem = ", ".join(m["name"] for m in plan["manual_items"])
        msg = f"Installed {', '.join(completed)}. Still needs manual setup: {rem}."
        say(f"Done: {msg}")
        return {"ok": True, "completed": completed, "failed": [], "manual_remaining": plan["manual_items"], "message": msg}

    msg = f"Environment ready ({', '.join(completed)})."
    say(f"Done: {msg}")
    return {"ok": True, "completed": completed, "failed": [], "message": msg}
