"""Do a feature's jobs work together?

Each job is built and tested alone on its own branch. This merges all of a feature's job branches onto the base
branch in a throwaway git worktree (your checkout is never touched) and runs the project's build and test commands
on the result. It reports which branches merge cleanly, which conflict and in which files, and whether the combined
code builds and passes. The result is stored with the heads it used, so it can say when it is out of date.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

TAIL = 2500  # characters of command output kept


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def branch_heads(root: Path, branches: list[str]) -> dict[str, str]:
    """Branch -> commit, for branches that exist."""
    out = {}
    for b in branches:
        r = _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}")
        if r.returncode == 0:
            out[b] = r.stdout.strip()
    return out


def result_path(runtime: Path, feature_id: str) -> Path:
    return runtime / "integration" / f"{feature_id}.json"


def load_result(runtime: Path, feature_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(result_path(runtime, feature_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def is_stale(result: dict[str, Any] | None, base_head: str, heads: dict[str, str]) -> bool:
    """True when the base or a branch has moved since the check ran."""
    return bool(result) and (result.get("base_head") != base_head or result.get("heads") != heads)


def _run_command(command: str, cwd: Path, timeout: int) -> dict[str, Any]:
    try:
        res = subprocess.run(command, cwd=cwd, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ran": True, "ok": False, "timed_out": True, "tail": f"Timed out after {timeout} s."}
    text = ((res.stdout or "") + (res.stderr or "")).strip()
    return {"ran": True, "ok": res.returncode == 0, "tail": text[-TAIL:]}


def verify(root: Path, base: str, branches: list[str], build_command: str | None, test_command: str | None,
           timeout: int = 900, log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    heads = branch_heads(root, branches)
    base_head = _git(root, "rev-parse", f"refs/heads/{base}").stdout.strip() or _git(root, "rev-parse", base).stdout.strip()
    result: dict[str, Any] = {"at": time.time(), "base": base, "base_head": base_head, "heads": heads, "missing": [b for b in branches if b not in heads],
                              "merged": [], "conflicts": [], "build": {"ran": False}, "test": {"ran": False}, "status": "pass"}
    if not base_head:
        return {**result, "status": "error", "error": f"The base branch {base} wasn't found."}
    if len(heads) < 2:
        return {**result, "status": "nothing", "error": "Fewer than two of this feature's jobs have a branch, so there is nothing to combine."}

    tmp = Path(tempfile.mkdtemp(prefix="orchestrator-verify-"))
    work = tmp / "work"
    try:
        add = _git(root, "worktree", "add", "--detach", str(work), base_head)
        if add.returncode != 0:
            return {**result, "status": "error", "error": "Couldn't make a scratch checkout: " + add.stderr.strip()[-200:]}
        env_user = ["-c", "user.name=Orchestrator", "-c", "user.email=orchestrator@localhost"]
        for branch, sha in heads.items():
            log(f"Merging {branch}...")
            m = subprocess.run(["git", *env_user, "merge", "--no-ff", "--no-edit", sha], cwd=work, capture_output=True, text=True)
            if m.returncode == 0:
                result["merged"].append(branch)
                continue
            files = [f for f in _git(work, "diff", "--name-only", "--diff-filter=U").stdout.splitlines() if f]
            result["conflicts"].append({"branch": branch, "files": files})
            subprocess.run(["git", "merge", "--abort"], cwd=work, capture_output=True)
        if result["conflicts"]:
            result["status"] = "conflict"  # a partly merged tree says nothing useful about build or tests
            return result
        for key, command in (("build", build_command), ("test", test_command)):
            if not command:
                continue
            log(f"Running {key}: {command}")
            result[key] = _run_command(command, work, timeout)
            if not result[key]["ok"]:
                result["status"] = f"{key}-failed"
                break
        return result
    finally:
        _git(root, "worktree", "remove", "--force", str(work))
        shutil.rmtree(tmp, ignore_errors=True)


def save_result(runtime: Path, feature_id: str, result: dict[str, Any]) -> None:
    path = result_path(runtime, feature_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
