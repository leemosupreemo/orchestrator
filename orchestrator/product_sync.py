"""Keep a project's product requirements document the same on all of an account's computers.

The document is `.orchestrator/product/prd.md`: git-ignored, so before this each computer had its own, and a job continued on
another computer was judged against a different (older, or no) product description. The account now keeps one copy per project
(`machine/product` and `machine/product/put` on the control plane: a version number, replaced only by a computer that built on the
current version), and each computer, every CHECK_SECONDS:

- pushes its document when it changed since the last sync;
- pulls the account's when that changed and this one didn't;
- when both changed, merges them section by section (see `merge`): a section only one side changed takes that side's text; a
  section both changed differently keeps BOTH texts, the other computer's under "Also written on <name>:". Nothing is dropped.

Every change that arrives is a version in this computer's own history (source "sync", undoable) and is announced, never silent.
What is shared is the document's text. Not shared: its history (each computer keeps its own), the "Source documents" list and the
design and source files it points to (they are files on one computer), and its settings. A project with no repository address
can't be matched across computers and is skipped. These functions are pure or take the control-plane call as an argument.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from orchestrator import account, sync_health
from orchestrator.prd import PATH as SEED_PATH, SECTIONS, Prd, PrdChanged, PrdError, replace_section, sections_view, set_sources, split

CHECK_SECONDS = 60
UNAVAILABLE_WAIT = 3600  # a control plane that doesn't carry this yet is asked again in an hour
MAX_PROJECTS = 10
ALSO_WRITTEN = "**Also written on {name}:**"


class Unavailable(Exception):
    """The control plane has no product endpoints (it hasn't been updated yet): don't ask again for a while."""


def digest(text: str) -> str:
    """The same 16 characters the control plane stores as a document's hash."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def shareable(text: str) -> str:
    """The document as shared: without its "Source documents" list, which names files that exist on one computer only."""
    return set_sources(text, [])


def has_content(text: str | None) -> bool:
    """Whether any section has real writing in it (a fresh template doesn't, and is never pushed over someone's document)."""
    return any(s["filled"] for s in sections_view(text))


def merge(base: str | None, local: str, remote: str, remote_name: str) -> tuple[str, list[str]]:
    """Three-way merge by section. `base` is the text the two last agreed on (None if they never did). Returns (text, titles of
    the sections both sides changed differently). A section only one side changed takes that side's text; both changed: this
    computer's text stays and the other's follows it, marked, so nothing the person wrote is lost."""
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
    return text, conflicts


def _is_untouched_seed(root: Path, local: str) -> bool:
    """Whether this computer's document is still the project's tracked starting copy (docs/product/prd.md), copied in the first
    time it was read and never edited. It is somebody else's ancestor, not this person's work, so it must neither be pushed
    nor merged against a document that grew from it."""
    try:
        return shareable((Path(root) / SEED_PATH).read_text(encoding="utf-8")) == local
    except OSError:
        return False


def _state_path(prd: Prd) -> Path:
    return prd.path.parent / "sync.json"


def read_state(prd: Prd) -> dict[str, Any]:
    try:
        data = json.loads(_state_path(prd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and "local_hash" in data and "version" in data else {}


def _write_state(prd: Prd, *, version: int, text: str, local_text: str) -> dict[str, Any]:
    """Remember what was agreed: the account's version, its text (the base of the next merge) and a hash of this computer's
    document as it is now, to tell later whether it changed. Normalising on write can make the two differ, hence two texts."""
    state = {"version": version, "base_text": text, "local_hash": digest(shareable(local_text)), "at": time.time()}
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
    return state


def _ask(call: Callable[..., dict[str, Any]], path: str, body: dict[str, Any], machine: dict[str, Any]) -> dict[str, Any]:
    try:
        return call(path, body, machine=machine)
    except account.AccountError as exc:
        if exc.status == 404 and "not found" in str(exc).lower():
            raise Unavailable(str(exc)) from exc
        raise


def sync_project(machine: dict[str, Any], root: Path, repo: str, *, call: Callable[..., dict[str, Any]] = account.call) -> str:
    """One round for one project. Returns what happened: idle, adopted (already the same), pushed, pulled, merged, or busy (the
    document changed under it: the next round starts again from what is there)."""
    prd = Prd(root, Path(root) / ".orchestrator")
    for _ in range(3):  # a computer that pushed in between sends us round again with the new version
        raw = prd.read()
        local = shareable(raw) if raw is not None else None
        filled = has_content(local)
        remote = _ask(call, "machine/product", {"project": repo}, machine).get("product")
        state = read_state(prd)
        if not state and filled and _is_untouched_seed(root, local):
            filled = False

        if remote is None:
            if not filled:
                return "idle"
            reply = _ask(call, "machine/product/put", {"project": repo, "text": local, "base_version": 0}, machine)
            if reply.get("ok"):
                _write_state(prd, version=reply["product"]["version"], text=local, local_text=raw)
                return "pushed"
            continue

        if filled and state and state["version"] == remote["version"] and state["local_hash"] == digest(local):
            return "idle"                                # unchanged on both sides since the last round
        if filled and digest(local) == remote["hash"]:   # both hold the same text already: just remember that
            _write_state(prd, version=remote["version"], text=remote["text"], local_text=raw)
            return "adopted"

        local_changed = bool(state) and filled and digest(local) != state["local_hash"]
        remote_changed = bool(state) and remote["version"] != state["version"]
        name = remote.get("machine_name") or "another computer"

        if state and filled and not local_changed and not remote_changed:
            return "idle"
        if state and local_changed and not remote_changed:
            outcome, text, summary = "pushed", local, ""
        elif not filled or (state and not local_changed):
            outcome, text, summary = "pulled", remote["text"], f"Synced from {name}"
        else:  # first time here with a different document, or both changed since the last sync
            text, conflicts = merge(state.get("base_text") if state else None, local, remote["text"], name)
            outcome = "merged"
            summary = (f"Merged with {name}'s version; both kept in {', '.join(conflicts)}" if conflicts
                       else f"Merged with {name}'s version")

        if outcome != "pushed":
            try:
                version = prd.write_if_current(raw, text, "sync", summary)
            except (PrdChanged, PrdError):
                return "busy"
            if version:
                prd.announce(summary, version["id"])
            raw = prd.read()
            text = shareable(raw)
            if outcome == "pulled":
                _write_state(prd, version=remote["version"], text=remote["text"], local_text=raw)
                return "pulled"
        reply = _ask(call, "machine/product/put", {"project": repo, "text": text, "base_version": remote["version"]}, machine)
        if reply.get("ok"):
            _write_state(prd, version=reply["product"]["version"], text=text, local_text=raw if outcome != "pushed" else (raw or text))
            return outcome
    return "busy"


def project_roots(root: Path | None = None) -> list[tuple[Path, str]]:
    """(project folder, repository address) for this computer's recent projects, one per product document, up to MAX_PROJECTS."""
    from orchestrator.project_config import load_recent_projects
    found: list[tuple[Path, str | None]] = [(Path(root), None)] if root else []
    for entry in load_recent_projects(prune_missing=False).get("projects", []):
        if entry.get("root"):
            found.append((Path(entry["root"]).expanduser(), entry.get("github_repo")))
    out, seen = [], set()
    for path, github_repo in found:
        if not path.is_dir():
            continue
        repo = account.project_repo(path, github_repo)
        key = str(Prd(path, path / ".orchestrator").path)   # a project's worktrees share one document
        if repo and key not in seen:
            seen.add(key)
            out.append((path, repo))
    return out[:MAX_PROJECTS]


def sync_all(machine: dict[str, Any], root: Path | None = None, *, call: Callable[..., dict[str, Any]] = account.call) -> dict[str, str]:
    """A round for every project. {repo: outcome}; a project that failed says why instead. Raises Unavailable when the control
    plane doesn't offer this yet."""
    results: dict[str, str] = {}
    for path, repo in project_roots(root):
        try:
            results[repo] = sync_project(machine, path, repo, call=call)
        except Unavailable:
            raise
        except account.AccountError as exc:
            results[repo] = f"error: {exc}"
        except Exception as exc:  # one project's trouble (an unreadable file) must not stop the others
            results[repo] = f"error: {exc}"
    return results


def run_round(machine: dict[str, Any], root: Path | None, *, call: Callable[..., dict[str, Any]] = account.call) -> int:
    """One scheduled round, for the server's thread: sync every project, record how it went for the sync-problems notice on
    Home, and say how many seconds to wait before the next."""
    try:
        results = sync_all(machine, root, call=call)
    except Unavailable:
        return UNAVAILABLE_WAIT
    except Exception as exc:
        sync_health.record("product", False, str(exc))
        return CHECK_SECONDS
    failed = [text for text in results.values() if text.startswith("error:")]
    sync_health.record("product", not failed, failed[0].removeprefix("error: ") if failed else "")
    return CHECK_SECONDS
