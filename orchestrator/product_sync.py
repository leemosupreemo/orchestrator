"""Keep a project's product requirements document the same on all the computers working on it, through the project's own
repository (refs/orchestrator/state, see orchestrator/state_ref.py and docs/state-sync.md), not Orchestrator's servers.

The document is `.orchestrator/product/prd.md`: git-ignored, so before this each computer had its own, and a job continued on
another computer was judged against a different (older, or no) product description. Once the person agrees to share it for a
project (asked once, on the Product page), each computer, every CHECK_SECONDS:

- pushes its document when it changed since the last sync;
- pulls the shared one when that changed and this one didn't;
- when both changed, merges them section by section (see `merge`): a section only one side changed takes that side's text; a
  section both changed differently keeps BOTH texts, the other computer's under "Also written on <name>:". Nothing is dropped.

Every change that arrives is a version in this computer's own history (source "sync", undoable) and is announced, never silent.
The design and source files the document points to travel too (a union: a file one computer has is copied to the others; a
file is never overwritten or deleted), and so does its "Source documents" list (merged). Each computer keeps its own history
and settings. A project without a remote (`origin`) stays on its computer. A push built on an older state is rejected by git
and the round starts again from what is there, so two computers can't overwrite each other.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

from orchestrator import state_ref, sync_health
from orchestrator.prd import (PATH as SEED_PATH, SECTIONS, Prd, PrdChanged, PrdError, replace_section, sections_view, set_sources,
                              sources, split)

CHECK_SECONDS = 60
MAX_PROJECTS = 10
DOC = "product/prd.md"
ASSET_KINDS = ("designs", "source")
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_FILES = 200
ALSO_WRITTEN = "**Also written on {name}:**"


def digest(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def has_content(text: str | None) -> bool:
    """Whether any section has real writing in it (a fresh template doesn't, and is never pushed over someone's document)."""
    return any(s["filled"] for s in sections_view(text))


def _union_sources(text: str, *others: str | None) -> str:
    """`text` with a "Source documents" list holding every document any of the texts lists, once each."""
    seen, items = set(), []
    for t in (text, *others):
        for item in sources(t):
            if item["target"] not in seen:
                seen.add(item["target"])
                items.append(item)
    return set_sources(text, items) if items else text


def merge(base: str | None, local: str, remote: str, remote_name: str) -> tuple[str, list[str]]:
    """Three-way merge by section. `base` is the text the two last agreed on (None if they never did). Returns (text, titles of
    the sections both sides changed differently). A section only one side changed takes that side's text; both changed: this
    computer's text stays and the other's follows it, marked, so nothing the person wrote is lost. Source lists are joined."""
    before, mine, theirs = split(base or ""), split(local), split(remote)
    text, conflicts = local, []
    for section in SECTIONS:
        sid = section["id"]
        b, l, r = before.get(sid, ""), mine.get(sid, ""), theirs.get(sid, "")
        if l == r or not r or r == b:
            continue                                   # same, nothing there to add, or only this computer changed it: keep ours
        if not l or l == b:
            text = replace_section(text, sid, r)       # nothing here, or only the other computer changed it
        else:
            text = replace_section(text, sid, f"{l}\n\n{ALSO_WRITTEN.format(name=remote_name)}\n\n{r}")
            conflicts.append(section["title"])
    return _union_sources(text, remote), conflicts


def _is_untouched_seed(root: Path, local: str) -> bool:
    """Whether this computer's document is still the project's tracked starting copy (docs/product/prd.md), copied in the first
    time it was read and never edited. It is an ancestor of the shared one, not this person's work: never pushed or merged."""
    try:
        return (Path(root) / SEED_PATH).read_text(encoding="utf-8") == local
    except OSError:
        return False


# ---- what was last agreed

def _state_path(prd: Prd) -> Path:
    return prd.path.parent / "sync.json"


def read_state(prd: Prd) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(prd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and {"commit", "local_hash", "base_text"} <= set(data) else {}


def _write_state(prd: Prd, *, commit: str, text: str, local_text: str | None) -> None:
    """Remember the shared commit, its text (the base of the next merge) and a hash of this computer's document now."""
    state = {"commit": commit, "base_text": text, "local_hash": digest(local_text), "at": time.time()}
    path = _state_path(prd)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sync-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# ---- design and source files: a union, never overwritten or deleted

def _local_files(prd: Prd) -> dict[str, Path]:
    out = {}
    for kind in ASSET_KINDS:
        folder = prd.asset_folder(kind)
        if folder.is_dir():
            for path in sorted(folder.iterdir()):
                if path.is_file() and not path.is_symlink() and not path.name.startswith(".") and path.stat().st_size <= MAX_FILE_BYTES:
                    out[f"product/{kind}/{path.name}"] = path
    return dict(list(out.items())[:MAX_FILES])


def _pull_files(prd: Prd, root: Path, head: str | None) -> list[str]:
    """Copy in the shared files this computer doesn't have. Returns their shared paths."""
    have = _local_files(prd)
    pulled = []
    for shared in (p for kind in ASSET_KINDS for p in state_ref.files(root, head, f"product/{kind}/")):
        kind, name = shared.split("/")[1], shared.split("/")[-1]
        if shared in have or name.startswith(".") or "/" in shared.removeprefix(f"product/{kind}/"):
            continue
        content = state_ref.read(root, head, shared)
        if content is None or len(content) > MAX_FILE_BYTES:
            continue
        target = prd.path.parent / kind / name
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        pulled.append(shared)
    return pulled


def _files_to_push(prd: Prd, root: Path, head: str | None) -> dict[str, bytes]:
    there = set(p for kind in ASSET_KINDS for p in state_ref.files(root, head, f"product/{kind}/"))
    return {shared: path.read_bytes() for shared, path in _local_files(prd).items() if shared not in there}


# ---- one round

def computer_name() -> str:
    try:
        from orchestrator import account
        return str((account.load_machine() or {}).get("name") or socket.gethostname().split(".")[0])
    except Exception:
        return socket.gethostname().split(".")[0]


def sync_project(root: Path, *, name: str | None = None) -> str:
    """One round for one project. Returns what happened: off (not shared), no remote, idle, adopted (already the same),
    pushed, pulled, merged, files (only design or source files moved), or busy (something changed under it: the next round
    starts again). Raises StateRefError when git can't reach or write the remote."""
    root = Path(root)
    if state_ref.consent(root) is not True:
        return "off"
    if not state_ref.remote_url(root):
        return "no remote"
    name = name or computer_name()
    prd = Prd(root, root / ".orchestrator")
    for _ in range(3):  # a computer that pushed in between sends us round again from the newer state
        raw = prd.read()
        filled = has_content(raw)
        state = read_state(prd)
        if not state and filled and _is_untouched_seed(root, raw):
            filled = False
        head = state_ref.fetch(root)
        remote_bytes = state_ref.read(root, head, DOC)
        remote = remote_bytes.decode("utf-8", "replace") if remote_bytes is not None else None
        sender = state_ref.computer(root, head) or "another computer"
        pulled_files = _pull_files(prd, root, head)
        changes: dict[str, bytes | None] = dict(_files_to_push(prd, root, head))

        if remote is None:
            outcome = "pushed" if filled else ("files" if changes else "idle")
            push_text = raw if filled else None
        elif filled and state and state["commit"] == head and state["local_hash"] == digest(raw):
            outcome, push_text = ("files" if changes or pulled_files else "idle"), None
        elif filled and digest(raw) == digest(remote):
            outcome, push_text = "adopted", None
        else:
            local_changed = bool(state) and filled and digest(raw) != state["local_hash"]
            remote_changed = bool(state) and head != state["commit"]
            if state and local_changed and not remote_changed:
                outcome, push_text = "pushed", raw
            else:
                if not filled or (state and not local_changed):
                    outcome, text, summary = "pulled", _union_sources(remote, raw), f"Synced from {sender}"
                else:  # first time here with a different document, or both changed since the last sync
                    text, conflicts = merge(state.get("base_text") if state else None, raw, remote, sender)
                    outcome = "merged"
                    summary = (f"Merged with {sender}'s version; both kept in {', '.join(conflicts)}" if conflicts
                               else f"Merged with {sender}'s version")
                try:
                    version = prd.write_if_current(raw, text, "sync", summary)
                except (PrdChanged, PrdError):
                    return "busy"
                if version:
                    prd.announce(summary, version["id"])
                raw = prd.read()
                push_text = raw if raw != remote else None   # this computer added something (a merge, its source list)

        if push_text is not None:
            changes[DOC] = push_text.encode("utf-8")
        if not changes:
            if head:
                _write_state(prd, commit=head, text=remote or raw or "", local_text=raw)
            return outcome
        sha = state_ref.commit(root, head, changes, "Product requirements" if DOC in changes else "Product files", name)
        if state_ref.push(root, sha):
            _write_state(prd, commit=sha, text=push_text if push_text is not None else (remote or ""), local_text=raw)
            return outcome if outcome not in ("idle", "adopted") else "files"
    return "busy"


def project_roots(root: Path | None = None) -> list[Path]:
    """This computer's recent projects, one per product document, up to MAX_PROJECTS."""
    from orchestrator.project_config import load_recent_projects
    found = [Path(root)] if root else []
    found += [Path(e["root"]).expanduser() for e in load_recent_projects(prune_missing=False).get("projects", []) if e.get("root")]
    out, seen = [], set()
    for path in found:
        if path.is_dir():
            key = str(Prd(path, path / ".orchestrator").path)   # a project's worktrees share one document
            if key not in seen:
                seen.add(key)
                out.append(path)
    return out[:MAX_PROJECTS]


def sync_all(root: Path | None = None) -> dict[str, str]:
    """A round for every project: {project folder: outcome, or "error: ..." for one that failed}."""
    results: dict[str, str] = {}
    for path in project_roots(root):
        try:
            results[str(path)] = sync_project(path)
        except Exception as exc:  # offline, no rights, an unreadable file: that project waits, the others go on
            results[str(path)] = f"error: {exc}"
    return results


def run_round(root: Path | None) -> int:
    """One scheduled round for the server's thread; records how it went for the sync-problems notice on Home."""
    results = sync_all(root)
    failed = [text.removeprefix("error: ") for text in results.values() if text.startswith("error:")]
    if any(text not in ("off", "no remote") for text in results.values()):
        sync_health.record("product", not failed, failed[0] if failed else "")
    return CHECK_SECONDS
