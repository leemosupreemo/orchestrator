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

import hashlib
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


def fetched(repo: Path) -> str | None:
    """The shared state as last fetched, without asking the remote (for showing it, not for building on it)."""
    sha = _git(repo, "rev-parse", "--verify", "--quiet", TRACKING_REF).stdout.decode().strip()
    return sha or None


def committed_at(repo: Path, commit: str | None) -> float:
    if not commit:
        return 0.0
    stamp = _git(repo, "show", "-s", "--format=%ct", commit).stdout.decode().strip()
    return float(stamp) if stamp.isdigit() else 0.0


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
        _record_push(repo, sha)
        return True
    words = _words(res) + (res.stdout or b"").decode("utf-8", "replace")
    if any(marker in words for marker in _REJECTED):
        return False
    raise StateRefError(_words(res) or "git push failed")


# ---- consent: nothing is shared until the person says so, per project

def _consent_file(root: Path) -> Path:
    from orchestrator.prd import shared_runtime
    return shared_runtime(Path(root)) / "state" / "repo_sync.json"


SCOPES = ("product", "jobs")   # each asked separately: jobs carry the person's answers and plans


def _consents(root: Path) -> dict[str, Any]:
    try:
        data = json.loads(_consent_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "enabled" in data and "product" not in data:   # the first version had one answer, for the product requirements
        data["product"] = data["enabled"]
    return data


def consent(root: Path, scope: str = "product") -> bool | None:
    """True or False once the person decided for this project and kind of state, None while they haven't."""
    value = _consents(root).get(scope)
    return value if isinstance(value, bool) else None


def set_consent(root: Path, enabled: bool, scope: str = "product") -> None:
    if scope not in SCOPES:
        raise ValueError(f"Unknown sharing scope: {scope}")
    data = {k: v for k, v in _consents(root).items() if k in SCOPES}
    data[scope] = bool(enabled)
    data["at"] = time.time()
    path = _consent_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


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
        return {"state": "no_remote", "jobs": "no_remote", "remote": "", "public": None}
    word = lambda decided: "ask" if decided is None else ("on" if decided else "off")  # noqa: E731
    return {"state": word(consent(root)), "jobs": word(consent(root, "jobs")), "remote": where, "public": is_public(root, url)}


# ---- the nudge: the account passes on which commit is newest (a key and a hash, never content), so others fetch at once

def project_key(root: Path) -> str:
    """A project's name on the account: a hash of its repository, the same on every computer that has that remote."""
    from orchestrator import account
    name = account.project_repo(Path(root)) or remote_url(Path(root))
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:32] if name else ""


def _head_file(root: Path) -> Path:
    from orchestrator.prd import shared_runtime
    return shared_runtime(Path(root)) / "state" / "state_head.json"


def _record_push(root: Path, sha: str) -> None:
    try:
        path = _head_file(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sha": sha, "at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def pushed_head(root: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(_head_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("sha"), str) else None


def shares_anything(root: Path) -> bool:
    return any(consent(root, scope) is True for scope in SCOPES)


def local_heads(roots: list[Path]) -> dict[str, dict[str, Any]]:
    """What this computer reports with its heartbeat: for each shared project, the commit it last pushed and when."""
    out = {}
    for root in roots:
        head = pushed_head(root) if shares_anything(root) else None
        key = project_key(root) if head else ""
        if key:
            out[key] = {"sha": head["sha"], "at": float(head.get("at") or 0)}
    return out


def behind(roots: list[Path], heads: Any) -> bool:
    """Whether the account knows a newer commit than this computer has for any shared project: time to fetch now."""
    if not isinstance(heads, dict) or not heads:
        return False
    for root in roots:
        if not shares_anything(root):
            continue
        newest = heads.get(project_key(root))
        if isinstance(newest, str) and newest not in (fetched(root), (pushed_head(root) or {}).get("sha")):
            return True
    return False
