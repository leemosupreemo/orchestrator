"""Where a project's code is hosted, and what Orchestrator does with it.

Two modes, chosen by `code_host` in .orchestrator/project.json:

- "github": each job is a GitHub issue, finished work becomes a pull request, and Merge & complete merges it with
  the GitHub CLI (`gh`). This is how Orchestrator has always worked.
- "git": plain git, for GitLab, Bitbucket, Gitea, Azure DevOps, a self-hosted server or no remote at all. Jobs get
  local IDs, finished work is pushed to `origin` as a branch (with a link to open a merge request where the host has
  one), and Merge & complete merges the branch into the base branch on this computer.

Left out or "auto", the mode follows `origin`: GitHub when it points at github.com, plain git otherwise.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import quote

MODES = ("auto", "github", "git")
_REMOTE = re.compile(r"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?(?P<host1>[^/:]+)(?::\d+)?/|(?:[^@]+@)?(?P<host2>[^:/]+):)(?P<path>.+?)(?:\.git)?/?$")


def origin_url(root: Path) -> str:
    try:
        result = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(root), capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def parse_remote(url: str) -> tuple[str, str] | None:
    """(hostname, owner/repo path) of a git remote URL, for https, ssh:// and scp-style (git@host:path) forms."""
    match = _REMOTE.match(url.strip())
    if not match:
        return None
    host = (match.group("host1") or match.group("host2") or "").lower()
    return (host, match.group("path").strip("/")) if host and match.group("path") else None


def host_kind(url: str) -> str:
    """github | gitlab | bitbucket | azure | other | none"""
    parsed = parse_remote(url) if url else None
    if not parsed:
        return "none"
    host = parsed[0]
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if "gitlab" in host:
        return "gitlab"
    if host == "bitbucket.org":
        return "bitbucket"
    if host in ("dev.azure.com", "ssh.dev.azure.com") or host.endswith(".visualstudio.com"):
        return "azure"
    return "other"


def resolve_mode(setting: str | None, remote: str) -> str:
    """The effective mode: "github" or "git"."""
    setting = (setting or "auto").strip().lower()
    if setting in ("github", "git"):
        return setting
    return "github" if host_kind(remote) == "github" else "git"


def review_request_url(remote: str, branch: str, base: str) -> str:
    """A link that opens a pull or merge request for `branch` on the remote's host, or "" when there's no known form."""
    parsed = parse_remote(remote) if remote else None
    if not parsed or not branch:
        return ""
    host, path = parsed
    kind = host_kind(remote)
    web = f"https://{host}/{path}"
    if kind == "github":
        return f"{web}/compare/{quote(base, safe='')}...{quote(branch, safe='')}?expand=1"
    if kind == "gitlab":
        return (f"{web}/-/merge_requests/new?merge_request%5Bsource_branch%5D={quote(branch, safe='')}"
                f"&merge_request%5Btarget_branch%5D={quote(base, safe='')}")
    if kind == "bitbucket":
        return f"{web}/pull-requests/new?source={quote(branch, safe='')}&dest={quote(base, safe='')}"
    return ""
