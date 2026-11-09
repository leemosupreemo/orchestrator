#!/usr/bin/env python3
"""Publish a shareable preview of a web project's branch on a Firebase Hosting preview channel.

It checks out the branch's latest commit in a temporary worktree (your checkout and uncommitted work are left alone), runs the
project's build if it has one, and deploys it to a channel named after the branch, which expires after 7 days. The link is
saved in .orchestrator/output/previews/ so App delivery can list it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

EXPIRES = "7d"


def channel_name(branch: str) -> str:
    """A Firebase channel id for a branch: lowercase letters, digits and dashes, short enough for the preview URL."""
    slug = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")[:36].strip("-")
    return slug or "preview"


def firebase_project(root: Path, config: dict[str, Any]) -> str:
    if config.get("firebase_project"):
        return str(config["firebase_project"])
    try:
        return str(json.loads((root / ".firebaserc").read_text(encoding="utf-8")).get("projects", {}).get("default") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def has_hosting(root: Path) -> bool:
    try:
        return bool(json.loads((root / "firebase.json").read_text(encoding="utf-8")).get("hosting"))
    except (OSError, ValueError, AttributeError):
        return False


def build_commands(folder: Path, config: dict[str, Any]) -> list[list[str]]:
    """The project's own build: `preview_build_command` from project.json, else `npm run build` when package.json has one."""
    if config.get("preview_build_command"):
        return [["/bin/sh", "-c", str(config["preview_build_command"])]]
    try:
        scripts = json.loads((folder / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
    except (OSError, ValueError, AttributeError):
        return []
    if "build" not in scripts:
        return []
    install = ["npm", "ci"] if (folder / "package-lock.json").is_file() else ["npm", "install"]
    return [install, ["npm", "run", "build"]]


def preview_url(deploy_output: str) -> tuple[str, str]:
    """(url, expireTime) from `firebase hosting:channel:deploy --json`."""
    data = json.loads(deploy_output[deploy_output.index("{"):])
    for site in (data.get("result") or {}).values():
        if isinstance(site, dict) and site.get("url"):
            return site["url"], site.get("expireTime") or ""
    raise ValueError(data.get("error") or "no preview URL in Firebase's reply")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--root", default=os.environ.get("ORCHESTRATOR_PROJECT_ROOT") or os.getcwd())
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        config = json.loads((root / ".orchestrator" / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}

    project = firebase_project(root, config)
    if shutil.which("firebase") is None:
        print("❌ The Firebase CLI isn't installed. Install it with: npm install -g firebase-tools, then sign in with: firebase login")
        return 1
    if not has_hosting(root) or not project:
        print("❌ Firebase Hosting isn't set up for this project. Run: firebase init hosting (it creates firebase.json and .firebaserc).")
        return 1
    try:
        commit = git(root, "rev-parse", "--short", args.branch)
        subject = git(root, "log", "-1", "--format=%s", args.branch)
    except subprocess.CalledProcessError:
        print(f"❌ There's no branch called {args.branch} in this repository.")
        return 1

    channel = channel_name(args.branch)
    print(f"Previewing {args.branch} at {commit}: {subject}")
    folder = Path(tempfile.mkdtemp(prefix="orchestrator-preview-"))
    try:
        subprocess.run(["git", "worktree", "add", "--detach", str(folder), args.branch], cwd=root, check=True,
                       stdout=subprocess.DEVNULL)
        for command in build_commands(folder, config):
            print(f"$ {' '.join(command)}", flush=True)
            if subprocess.run(command, cwd=folder).returncode != 0:
                print(f"❌ The build failed ({' '.join(command)}). Fix the error above, or set preview_build_command in .orchestrator/project.json.")
                return 1
        print(f"Deploying to the Firebase preview channel '{channel}' ({EXPIRES})…", flush=True)
        deploy = subprocess.run(["firebase", "hosting:channel:deploy", channel, "--project", project, "--expires", EXPIRES, "--json"],
                                cwd=folder, capture_output=True, text=True)
        try:
            url, expires = preview_url(deploy.stdout)
        except ValueError as exc:
            print(deploy.stdout[-2000:] + deploy.stderr[-2000:])
            hint = " Sign in with: firebase login" if "auth" in (deploy.stdout + deploy.stderr).lower() else ""
            print(f"❌ Firebase didn't publish the preview: {exc}.{hint}")
            return 1
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(folder)], cwd=root, capture_output=True)
        shutil.rmtree(folder, ignore_errors=True)

    out = root / ".orchestrator" / "output" / "previews"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{channel}.json").write_text(json.dumps({"branch": args.branch, "commit": commit, "subject": subject, "url": url,
                                                      "expires": expires, "channel": channel, "at": time.time()}, indent=2))
    print(f"✅ Preview ready: {url}")
    if expires:
        print(f"   It expires {expires[:10]}. Publish again to refresh it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
