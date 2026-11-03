"""Create a release anchor on the base branch without changing the working tree."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


class ReleaseError(ValueError):
    pass


def _git(root: Path, *args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError("Git did not finish. Check repository access and try again.") from exc


def _base_commit(root: Path, base: str) -> str:
    result = _git(root, "rev-parse", "--verify", f"refs/heads/{base}^{{commit}}")
    if result.returncode:
        raise ReleaseError("The base branch has no local commit to tag. Sync it from Git setup first.")
    return result.stdout.strip()


def options(root: Path, base: str) -> dict[str, Any]:
    commit = _base_commit(root, base)
    tags = _git(root, "tag", "--list").stdout.splitlines()
    versions = [tuple(map(int, match.groups())) for tag in tags if (match := re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag))]
    version = max(versions) if versions else None
    suggested = f"v{version[0]}.{version[1]}.{version[2] + 1}" if version else "v0.1.0"
    return {"branch": base, "commit": commit, "subject": _git(root, "show", "-s", "--format=%s", commit).stdout.strip(),
            "suggested_tag": suggested, "can_push": _git(root, "remote", "get-url", "origin").returncode == 0}


def create(root: Path, base: str, tag: str, expected_commit: str, *, push: bool) -> dict[str, Any]:
    if not tag or len(tag) > 120 or tag.startswith("-") or _git(root, "check-ref-format", f"refs/tags/{tag}").returncode:
        raise ReleaseError("Enter a valid release tag, such as v0.1.0, without spaces.")
    commit = _base_commit(root, base)
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", expected_commit) or commit != expected_commit:
        raise ReleaseError("The base branch changed. Reopen Create release tag to review its latest commit.")
    if push and _git(root, "remote", "get-url", "origin").returncode:
        raise ReleaseError("No origin remote is configured. Save the tag locally or add a remote in Git setup.")
    ref = f"refs/tags/{tag}"
    existing = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    created = existing.returncode != 0
    if not created and existing.stdout.strip() != commit:
        raise ReleaseError("That tag already points to another commit. Choose a different version.")
    if created and _git(root, "tag", "--", tag, commit).returncode:
        raise ReleaseError("The tag could not be created. It may already exist; reopen the release dialog.")
    result = {"tag": tag, "commit": commit, "branch": base, "created": created, "pushed": False, "warning": ""}
    if push:
        try:
            published = _git(root, "push", "--no-follow-tags", "origin", f"{ref}:{ref}", timeout=45)
            result["pushed"] = published.returncode == 0
        except ReleaseError:
            pass
        if not result["pushed"]:
            result["warning"] = "The tag is saved locally, but publishing failed. Check remote access in Git setup, then retry with the same version."
    return result
