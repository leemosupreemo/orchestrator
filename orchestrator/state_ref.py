"""Shared project state kept in the project's own repository, on one hidden ref: refs/orchestrator/state.

Orchestrator's computers need the same product requirements (and, in later phases, jobs and setup) for a project. Rather than
store that content on Orchestrator's servers, it travels where the code already does: the project's `origin`. The ref holds a
commit whose tree mirrors the shared files (`product/prd.md`, `product/designs/...`, `product/source/...`). It is never checked
out, never in a branch, and not fetched by an ordinary clone. See docs/state-sync.md.

- `fetch` brings the ref down (None when nobody has shared anything yet); `read`/`files` look inside it.
- `commit` builds a new commit on top of what was fetched, in a scratch index (the working tree, index and branches are never
  touched); `push` sends it WITHOUT --force, so a push based on an older state is rejected: that is the compare-and-swap that
  stops two computers overwriting each other. The caller fetches, merges and tries again.
- Pushes use --no-verify: the repository's own pre-push hooks (a test run) are for code, not for this.
- Sharing is per project and only after the person says yes (`consent`): anyone who can read the repository can read the ref,
  and on a public repository that is everyone.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

REF = "refs/orchestrator/state"
TRACKING_REF = "refs/orchestrator/fetched-state"   # where the fetched copy is kept locally
_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
        # State commits are Orchestrator's, not the person's: no name or email of theirs goes into the repository's objects.
        "GIT_AUTHOR_NAME": "Orchestrator", "GIT_AUTHOR_EMAIL": "orchestrator@localhost",
        "GIT_COMMITTER_NAME": "Orchestrator", "GIT_COMMITTER_EMAIL": "orchestrator@localhost"}
COMPUTER_TRAILER = "Orchestrator-Computer"
_REJECTED = ("[rejected]", "non-fast-forward", "fetch first", "stale info", "cannot lock ref", "[remote rejected]")


class StateRefError(RuntimeError):
    """git couldn't reach or write the remote (offline, no rights, a host that refuses custom refs). Said in git's words."""


def _git(repo: Path, *args: str, env: dict[str, str] | None = None, timeout: int = 60, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, timeout=timeout, input=data,
                          env={**os.environ, **_ENV, **(env or {})})


def _words(res: subprocess.CompletedProcess) -> str:
    text = (res.stderr or res.stdout or b"").decode("utf-8", "replace").strip()
    return re.sub(r"(https?://)[^/@\s]+@", r"\1", text)[-300:]  # never a token from a remote URL


def remote_url(repo: Path) -> str:
    res = _git(repo, "remote", "get-url", "origin", timeout=10)
    return res.stdout.decode().strip() if res.returncode == 0 else ""


def fetch(repo: Path) -> str | None:
    """Bring the shared state down; its commit, or None when the remote has no state yet."""
    res = _git(repo, "fetch", "--no-tags", "--quiet", "origin", f"+{REF}:{TRACKING_REF}", timeout=120)
    if res.returncode != 0:
        words = _words(res)
        if "couldn't find remote ref" in words.lower():
            _git(repo, "update-ref", "-d", TRACKING_REF)
            return None
        raise StateRefError(words or "git fetch failed")
    sha = _git(repo, "rev-parse", "--verify", "--quiet", TRACKING_REF).stdout.decode().strip()
    return sha or None


def read(repo: Path, commit: str | None, path: str) -> bytes | None:
    if not commit:
        return None
    res = _git(repo, "cat-file", "blob", f"{commit}:{path}")
    return res.stdout if res.returncode == 0 else None


def files(repo: Path, commit: str | None, prefix: str) -> list[str]:
    if not commit:
        return []
    res = _git(repo, "ls-tree", "-r", "--name-only", commit, "--", prefix)
    return [line for line in res.stdout.decode("utf-8", "replace").splitlines() if line]


def computer(repo: Path, commit: str | None) -> str:
    """Which computer made a state commit (from its message), or ""."""
    if not commit:
        return ""
    body = _git(repo, "show", "-s", "--format=%B", commit).stdout.decode("utf-8", "replace")
    match = re.search(rf"^{COMPUTER_TRAILER}: (.+)$", body, re.M)
    return match.group(1).strip() if match else ""


def commit(repo: Path, parent: str | None, changes: dict[str, bytes | None], message: str, computer_name: str) -> str:
    """A new state commit on top of `parent` with `changes` (path -> content, None removes it). Built in a scratch index."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if parent:
            res = _git(repo, "read-tree", parent, env=env)
            if res.returncode != 0:
                raise StateRefError(_words(res))
        for path, content in changes.items():
            if content is None:
                _git(repo, "update-index", "--force-remove", "--", path, env=env)
                continue
            blob = _git(repo, "hash-object", "-w", "--stdin", data=content)
            if blob.returncode != 0:
                raise StateRefError(_words(blob))
            res = _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob.stdout.decode().strip()},{path}", env=env)
            if res.returncode != 0:
                raise StateRefError(_words(res))
        tree = _git(repo, "write-tree", env=env)
        if tree.returncode != 0:
            raise StateRefError(_words(tree))
    args = ["commit-tree", tree.stdout.decode().strip(), "-m", f"{message}\n\n{COMPUTER_TRAILER}: {computer_name}"]
    if parent:
        args[2:2] = ["-p", parent]
    made = _git(repo, *args)
    if made.returncode != 0:
        raise StateRefError(_words(made))
    return made.stdout.decode().strip()


def push(repo: Path, sha: str) -> bool:
    """Send a state commit. False when the remote moved on since the fetch (rejected, nothing written); StateRefError for
    anything else."""
    res = _git(repo, "push", "--no-verify", "--porcelain", "origin", f"{sha}:{REF}", timeout=120)
    if res.returncode == 0:
        _git(repo, "update-ref", TRACKING_REF, sha)
        return True
    words = _words(res) + (res.stdout or b"").decode("utf-8", "replace")
    if any(marker in words for marker in _REJECTED):
        return False
    raise StateRefError(_words(res) or "git push failed")


# ---- consent: nothing is shared until the person says so, per project

def _consent_file(root: Path) -> Path:
    from orchestrator.prd import shared_runtime
    return shared_runtime(Path(root)) / "state" / "repo_sync.json"


def consent(root: Path) -> bool | None:
    """True or False once the person decided for this project, None while they haven't."""
    try:
        value = json.loads(_consent_file(root).read_text(encoding="utf-8")).get("enabled")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, bool) else None


def set_consent(root: Path, enabled: bool) -> None:
    path = _consent_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabled": bool(enabled), "at": time.time()}), encoding="utf-8")


_visibility_cache: dict[str, tuple[float, bool | None]] = {}


def is_public(root: Path, url: str | None = None, run=subprocess.run) -> bool | None:
    """Whether the remote is a public repository: True/False where the host can say (GitHub, through `gh`), else None."""
    from orchestrator.code_host import parse_remote
    url = remote_url(root) if url is None else url
    parsed = parse_remote(url) if url else None
    if not parsed or parsed[0] != "github.com":
        return None
    cached = _visibility_cache.get(parsed[1])
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    try:
        res = run(["gh", "repo", "view", parsed[1], "--json", "visibility", "-q", ".visibility"], capture_output=True, text=True, timeout=15)
        answer = {"PUBLIC": True, "PRIVATE": False, "INTERNAL": False}.get(res.stdout.strip().upper()) if res.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        answer = None
    _visibility_cache[parsed[1]] = (time.time(), answer)
    return answer


def status(root: Path) -> dict[str, Any]:
    """For the Product page: whether this project can share and whether it does. state: no_remote, ask, on or off."""
    url = remote_url(Path(root))
    from orchestrator.code_host import parse_remote
    parsed = parse_remote(url) if url else None
    where = f"{parsed[0]}/{parsed[1]}" if parsed else url
    if not url:
        return {"state": "no_remote", "remote": "", "public": None}
    decided = consent(root)
    return {"state": "ask" if decided is None else ("on" if decided else "off"), "remote": where, "public": is_public(root, url)}
