"""Immutable source snapshots for verification on project-authorized workers."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

LOG = logging.getLogger(__name__)
REF_PREFIX = "refs/orchestrator/verification/"


class CandidateError(RuntimeError):
    """The exact candidate could not be prepared or made available."""


@dataclass(frozen=True)
class Candidate:
    commit: str
    tree: str


def _git(root: Path, *args: str, overrides: dict[str, str] | None = None,
         input_text: str | None = None) -> str:
    env = {**os.environ, **(overrides or {}), "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], env=env, capture_output=True, text=True,
            encoding="utf-8", errors="surrogateescape", timeout=120, check=True, input=input_text,
        )
    except subprocess.CalledProcessError as exc:
        raise CandidateError(f"git {args[0]} failed: {exc.stderr.strip()}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateError(f"git {args[0]} could not finish: {exc}") from exc
    return result.stdout


def _object_id(value: str) -> bool:
    return re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is not None


def create(root: Path, base_commit: str, job_paths: Iterable[str]) -> Candidate:
    """Snapshot explicitly owned worktree paths on top of a pinned job HEAD."""
    if not _object_id(base_commit) or _git(root, "rev-parse", "HEAD").strip() != base_commit:
        raise CandidateError("Candidate base must be the current job HEAD.")
    paths = sorted(set(job_paths))
    for path in paths:
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or not parsed.parts or ".." in parsed.parts or "\0" in path:
            raise CandidateError(f"Invalid candidate path: {path!r}")
        if (root / path).is_dir() and not (root / path).is_symlink():
            raise CandidateError(f"Candidate path must name a file: {path!r}")
    base_tree = _git(root, "rev-parse", f"{base_commit}^{{tree}}").strip()
    if not paths:
        return Candidate(base_commit, base_tree)
    with tempfile.TemporaryDirectory(prefix="orchestrator-verification-index-") as directory:
        env = {"GIT_INDEX_FILE": str(Path(directory) / "index"), "GIT_LITERAL_PATHSPECS": "1"}
        _git(root, "read-tree", base_commit, overrides=env)
        _git(root, "add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul",
             overrides=env, input_text="\0".join(paths) + "\0")
        tree = _git(root, "write-tree", overrides=env).strip()
    if _git(root, "rev-parse", "HEAD").strip() != base_commit:
        raise CandidateError("Job HEAD moved while preparing verification.")
    if tree == base_tree:
        return Candidate(base_commit, base_tree)
    commit = _git(root, "commit-tree", tree, "-p", base_commit, "-m", "Orchestrator verification candidate").strip()
    return Candidate(commit, tree)


def worktree_candidate(root: Path, *, job_owned: bool = False) -> Candidate:
    """Snapshot a clean checkout or a checkout known to contain only job edits."""
    head = _git(root, "rev-parse", "HEAD").strip()
    status = _git(root, "status", "--porcelain=v1", "-z", "--no-renames", "--untracked-files=all")
    if status and not job_owned:
        raise CandidateError("Uncommitted changes in a shared checkout cannot be attributed to this job.")
    entries = status.split("\0")[:-1]
    if any("U" in entry[:2] or entry[:2] in ("AA", "DD") for entry in entries):
        raise CandidateError("The verification checkout contains unresolved merges.")
    return create(root, head, [entry[3:] for entry in entries])


def _cleanup(root: Path, *args: str) -> None:
    try:
        _git(root, *args)
    except CandidateError as exc:
        LOG.warning("Could not clean up verification candidate state: %s", exc)


@contextmanager
def publish(root: Path, remote: str, candidate: Candidate) -> Iterator[str]:
    """Publish a unique, temporary project Git ref and delete only our value."""
    if not _object_id(candidate.commit) or not _object_id(candidate.tree):
        raise CandidateError("Invalid verification candidate object ID.")
    ref = REF_PREFIX + uuid4().hex
    try:
        _git(root, "push", f"--force-with-lease={ref}:", "--", remote, f"{candidate.commit}:{ref}")
        yield ref
    finally:
        _cleanup(root, "push", f"--force-with-lease={ref}:{candidate.commit}", "--", remote, f":{ref}")


@contextmanager
def checkout(root: Path, remote: str, ref: str, candidate: Candidate, destination: Path) -> Iterator[Path]:
    """Verify the advertised commit and tree before exposing a detached checkout."""
    if not re.fullmatch(re.escape(REF_PREFIX) + r"[0-9a-f]{32}", ref):
        raise CandidateError("Invalid verification candidate ref.")
    if destination.exists() or destination.is_symlink():
        raise CandidateError("Verification checkout destination already exists.")
    fetch_ref = REF_PREFIX + uuid4().hex
    try:
        _git(root, "fetch", "--no-tags", "--no-write-fetch-head", "--", remote, f"{ref}:{fetch_ref}")
        commit = _git(root, "rev-parse", f"{fetch_ref}^{{commit}}").strip()
        tree = _git(root, "rev-parse", f"{commit}^{{tree}}").strip()
        if Candidate(commit, tree) != candidate:
            raise CandidateError("Fetched verification revision does not match the candidate.")
        if any(row.startswith("160000 ") for row in _git(root, "ls-tree", "-r", tree).splitlines()):
            raise CandidateError("Verification checkout requires submodule materialization.")
        _git(root, "worktree", "add", "--detach", "--", str(destination), commit,
             overrides={"GIT_LFS_SKIP_SMUDGE": "1"})
        try:
            paths = _git(destination, "ls-files", "-z")
            attributes = _git(destination, "check-attr", "-z", "--stdin", "filter", input_text=paths).split("\0")
            for path, _, value in zip(attributes[::3], attributes[1::3], attributes[2::3]):
                entry = destination / path
                if value == "lfs" and entry.is_file() and not entry.is_symlink():
                    with entry.open("rb") as source:
                        if source.readline(128).rstrip() == b"version https://git-lfs.github.com/spec/v1":
                            raise CandidateError("Verification checkout requires Git LFS materialization.")
            yield destination
        finally:
            _cleanup(root, "worktree", "remove", "--force", "--", str(destination))
    finally:
        _cleanup(root, "update-ref", "-d", fetch_ref)
