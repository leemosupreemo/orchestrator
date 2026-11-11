"""Ask AI: a conversation about the whole project, from Home.

Anyone can ask any question about the product or the code without starting a job. Three kinds of AI to ask:

    free      models that cost nothing to run (local models, free hosted ones, a subscription that includes them)
    balanced  good, paid models that aren't the most expensive: most questions
    complex   the strongest models, for hard questions

Which models fall in each comes from the model registry (its quality tier and cost), limited to the ones ready on this
computer. A tier never borrows from another: when none of its models can answer, the person is told, not upgraded quietly.

The model doesn't remember anything between turns. Every question is sent with the project as it is now (product
requirements, attached documents, notes, Git history, jobs) and the conversation so far, so any model, of any tier, picked
for any turn, knows the same things. The model runs in an empty folder and can't change anything.

The conversation is saved with the project (shared by its worktrees), so it is there on any device that opens it.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable

MAX_MESSAGE_CHARS = 4000
MAX_HISTORY_MESSAGES = 20
MAX_STORED_MESSAGES = 200
MAX_FALLBACKS = 2

TIERS = ("free", "balanced", "complex")
TIER_LABELS = {"free": "Free", "balanced": "Balanced", "complex": "Complex"}
TIER_HINTS = {
    "free": "No cost. Quick questions; answers may be simpler.",
    "balanced": "Good answers at a moderate cost. Right for most questions.",
    "complex": "The strongest models, for hard questions. Costs the most.",
}
# How much of the project goes with each question. Free models tend to have small context windows.
CONTEXT_BUDGET = {"free": 24_000, "balanced": 60_000, "complex": 90_000}

LlmFn = Callable[[str, list[str]], tuple[str, str]]  # (prompt, models to try in order) -> (answer, model that answered)


class ChatError(ValueError):
    pass


# --------------------------------------------------------------------------- which models answer

def classify(model: Any) -> str:
    """The tier a model belongs to: free costs nothing; complex is the top quality tier; the rest are balanced."""
    from orchestrator.scripts.model_registry import ModelTier
    if float(getattr(model, "cost_factor", 1.0) or 0.0) == 0.0:
        return "free"
    return "complex" if int(model.tier) == int(ModelTier.EXTREME) else "balanced"


def _order(tier: str, models: list[Any]) -> list[Any]:
    """Best first, within the tier: free and complex by quality; balanced the cheaper of the good ones first."""
    if tier == "balanced":
        return sorted(models, key=lambda m: (int(m.tier), float(m.cost_factor), m.id))
    return sorted(models, key=lambda m: (int(m.tier), -float(m.cost_factor), m.id))


def chat_tiers(models: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """For each tier: the model it uses, the ones it falls back to, and, when it has none, why."""
    groups: dict[str, list[Any]] = {t: [] for t in TIERS}
    for m in models:
        groups[classify(m)].append(m)
    out = {}
    for tier in TIERS:
        ordered = _order(tier, groups[tier])
        ids = [m.id for m in ordered]
        out[tier] = {
            "label": TIER_LABELS[tier], "hint": TIER_HINTS[tier],
            "model": ids[0] if ids else "", "models": ids[: 1 + MAX_FALLBACKS],
            "available": bool(ids),
            "reason": "" if ids else {
                "free": "No free model is set up. Install OpenCode or Ollama under AI tools.",
                "balanced": "No mid-priced model is set up. Connect an AI tool such as Claude, Codex or Gemini under AI tools.",
                "complex": "None of the strongest models is set up. Connect Claude or Codex under AI tools.",
            }[tier],
        }
    return out


def ready_models(root: Path) -> list[Any]:
    """The models this computer can run now (the same list jobs choose from)."""
    from orchestrator.project_setup import models_ready_here
    from orchestrator.scripts.model_registry import get_model
    found = {}
    for model_id in models_ready_here(root):
        meta = get_model(model_id)
        if meta:
            found[meta.id] = meta
    return list(found.values())


def default_tier(tiers: dict[str, dict[str, Any]]) -> str:
    return next((t for t in ("balanced", "free", "complex") if tiers[t]["available"]), "balanced")


# --------------------------------------------------------------------------- what the model is told

def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + "\n…(trimmed)"


def _quote(text: str, n: int) -> str:
    """A document inside the context: trimmed, its headings pushed below the context's own, so its "## Install" isn't read as a section of the project."""
    import re
    return re.sub(r"^(#{1,6})(\s)", lambda m: "#" * min(6, len(m.group(1)) + 4) + m.group(2), _clip(text, n), flags=re.M)


TEXT_SUFFIXES = {".md", ".txt", ".markdown", ".rst", ".json", ".yaml", ".yml", ".csv", ".html", ".xml"}


def _attached_documents(root: Path, limit: int) -> str:
    """Documents attached to the product requirements. The model can't open files, so readable ones are quoted."""
    from orchestrator import prd
    text = prd.read_text(root) or ""
    parts = []
    for item in prd.sources(text):
        path = prd.asset_folder(root, "source") / Path(item["target"]).name
        if path.suffix.lower() in TEXT_SUFFIXES and path.is_file():
            try:
                parts.append(f"#### {item['label']}\n{_quote(path.read_text(encoding='utf-8', errors='replace'), limit)}")
                continue
            except OSError:
                pass
        parts.append(f"#### {item['label']}\n(attached as {path.name}; its contents aren't readable as text)")
    return "\n\n".join(parts)


def _project_notes(root: Path, limit: int) -> str:
    from orchestrator import prd, project_docs
    parts = []
    for f in project_docs.project_files(root):
        if f["path"] == prd.PATH:
            continue  # the requirements are their own section
        try:
            parts.append(f"#### {f['path']}\n{_quote(project_docs.read_project_file(root, f['path']), limit)}")
        except Exception:
            continue
    return "\n\n".join(parts)


def _jobs(jobs: list[dict[str, Any]]) -> str:
    lines = []
    for j in jobs[:40]:
        state = (j.get("state") or {}).get("label") or j.get("status") or ""
        lines.append(f"- {j.get('title') or j.get('id')}{f' ({state})' if state else ''}{f' · branch {j['branch']}' if j.get('branch') else ''}")
    return "\n".join(lines)


def project_context(root: Path, tier: str = "balanced", jobs: list[dict[str, Any]] | None = None, name: str = "") -> str:
    """Everything known about the project, in priority order, within the tier's budget. Rebuilt for every question."""
    from orchestrator import prd
    budget = CONTEXT_BUDGET.get(tier, CONTEXT_BUDGET["balanced"])
    small = tier == "free"
    branch = _git(root, "branch", "--show-current") or "(detached)"
    sections = [
        ("Project", f"Name: {name or root.name}\nFolder: {root.name}\nCurrent branch: {branch}"),
        ("Product requirements (the source of truth for what is being built)", _quote(prd.read_text(root) or "", 8_000 if small else 24_000)),
        ("Documents attached to the requirements", _attached_documents(root, 3_000 if small else 12_000)),
        ("Recent commits (newest first)", _git(root, "log", "-n", "25" if small else "60", "--date=short", "--format=%h %ad %an: %s")),
        ("Uncommitted changes", _clip(_git(root, "status", "--short"), 2_000)),
        ("Branches", _clip(_git(root, "branch", "--format=%(refname:short)"), 1_500)),
        ("Jobs (planned, in progress and done)", _jobs(jobs or [])),
        ("Project notes and docs", _project_notes(root, 1_500 if small else 5_000)),
    ]
    out, used = [], 0
    for title, body in sections:
        body = (body or "").strip()
        if not body:
            continue
        block = f"## {title}\n{body}"
        if used + len(block) > budget:
            room = budget - used - len(title) - 20
            if room < 400:
                break
            block = f"## {title}\n{_clip(body, room)}"
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


def build_prompt(context: str, history: list[dict[str, Any]], question: str) -> str:
    transcript = "\n".join(f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('text', '')}"
                           for m in history[-MAX_HISTORY_MESSAGES:])
    return "\n\n".join(part for part in [
        "You answer questions about one software project for the people building it, inside an AI coding orchestrator. "
        "Everything known about the project is below: its product requirements, attached documents, notes, Git history and jobs. "
        "Answer from it, concisely and concretely. Say plainly when the project information doesn't settle something instead of guessing. "
        "You can't change code or files; when something should change, say what, and suggest starting a job for it from Home.",
        f"# The project\n\n{context}" if context else "",
        f"# Conversation so far\n{transcript}" if transcript else "",
        f"# New question\n{question}",
    ] if part)


# --------------------------------------------------------------------------- the conversation, saved with the project

def thread_path(root: Path) -> Path:
    from orchestrator import prd
    return prd.shared_runtime(root) / "chat" / "project.json"


def messages(root: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(thread_path(root).read_text())
    except (OSError, ValueError):
        return []
    return [m for m in (data.get("messages") or []) if isinstance(m, dict)] if isinstance(data, dict) else []


def _save(root: Path, thread: list[dict[str, Any]]) -> None:
    path = thread_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".chat-")
    with os.fdopen(fd, "w") as f:
        json.dump({"messages": thread[-MAX_STORED_MESSAGES:]}, f, indent=1)
    os.replace(tmp, path)


def clear(root: Path) -> None:
    try:
        thread_path(root).unlink()
    except FileNotFoundError:
        pass


def check_question(question: str) -> str:
    question = (question or "").strip()
    if not question:
        raise ChatError("Type a question first.")
    if len(question) > MAX_MESSAGE_CHARS:
        raise ChatError(f"Keep it under {MAX_MESSAGE_CHARS} characters.")
    return question


def ask(root: Path, question: str, tier: str, llm: LlmFn, tiers: dict[str, dict[str, Any]],
        jobs: list[dict[str, Any]] | None = None, name: str = "") -> list[dict[str, Any]]:
    """Answer with a model of `tier` only, and save both messages."""
    question = check_question(question)
    if tier not in TIERS:
        raise ChatError("Choose Free, Balanced or Complex.")
    choice = tiers.get(tier) or {}
    if not choice.get("available"):
        raise ChatError(choice.get("reason") or f"No {TIER_LABELS[tier]} model is set up.")
    asked_at = time.time()
    prompt = build_prompt(project_context(root, tier, jobs, name), messages(root), question)
    reply, used = llm(prompt, list(choice["models"]))
    reply = (reply or "").strip()
    if not reply:
        raise ChatError("The model returned an empty answer. Try again.")
    thread = messages(root) + [{"role": "user", "text": question, "t": asked_at, "tier": tier},
                               {"role": "assistant", "text": reply, "t": time.time(), "tier": tier, "model": used or choice["model"]}]
    _save(root, thread)
    return thread
