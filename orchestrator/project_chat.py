"""Ask AI: a conversation about the whole project, from Home.

Anyone can ask any question about the product or the code without starting a job. Three kinds of AI to ask:

    free      models that cost nothing to run (local models, free hosted ones, a subscription that includes them)
    balanced  good, paid models that aren't the most expensive: most questions
    complex   the strongest models, for hard questions

Which models fall in each comes from the model registry (its quality tier and cost), limited to the ones ready on this
computer. A tier never borrows from another: when none of its models can answer, the person is told, not upgraded quietly.

The model doesn't remember anything between turns. Every question is sent with the project as it is now (product
requirements, attached documents, notes and README, what it is built with, its folder structure and the start of its main
source files, Git history, jobs) and the conversation so far, so any model, of any tier, picked for any turn, knows the same
things. The model runs in an empty folder and can't change anything, so everything it should know is in the prompt. A
project whose requirements aren't written is described by its README, documents and code instead. A local model has a fixed
window, so what is sent is sized to fit it (see ollama_api).

The conversation is saved with the project (shared by its worktrees), so it is there on any device that opens it.
"""
from __future__ import annotations

import json
import re
import os
import subprocess
import tempfile
import threading
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


# --------------------------------------------------------------------------- is it really there?
# "Ready" means the tool is installed and signed in and its provider lists the model. That isn't the same as the model
# answering: a plan may not include it, a quota may be used up, a provider may list models it then refuses. So each
# model's last real result is kept, from a one-word test question (check_tiers) and from every real question: one that
# failed recently is left out of its tier with the reason, and ones that answered come first.

CHECK_FRESH = 6 * 3600      # how long a model that answered counts as checked
FAIL_HOLD = 30 * 60         # how long one that failed is left out: a sign-in being renewed or a busy provider is often back soon
KEY_HOLD = 24 * 3600        # a rejected API key stays rejected until it is replaced, so it is left out far longer (or until the sign-in file changes)
RETRY_AFTER = 4.0           # a check that fails is asked once more before it counts, so a blip doesn't take a model away
PROBE = "Reply with the single word OK."
MAX_PROBES_PER_TIER = 4


def checks_path() -> Path:
    from orchestrator.project_config import user_state_dir
    return user_state_dir() / "model_checks.json"  # the models are this computer's, whichever project asks


def load_checks() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(checks_path().read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


_CHECKS_LOCK = threading.Lock()  # the three tiers are checked at once: one read-change-write at a time


def record_check(model: str, ok: bool, reason: str = "", now: float | None = None) -> None:
    if not model:
        return
    with _CHECKS_LOCK:
        data = load_checks()
        data[model] = {"ok": bool(ok), "reason": "" if ok else (reason or "didn't answer")[:200], "at": now or time.time()}
        path = checks_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".checks-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)


KEY_REJECTED = "its API key is rejected (check AI tools)"
SHARED_KEY_EXEMPT = ("opencode", "ollama")  # these providers have models that need no key of their own


def provider_of(model_id: str) -> str:
    """The provider a model id names ("alibaba-token-plan/qwen3.7-max" -> "alibaba-token-plan"), "" when it names none."""
    head, sep, _ = model_id.partition("/")
    return head if sep and head not in SHARED_KEY_EXEMPT else ""


def credentials_file() -> Path:
    """Where OpenCode keeps its sign-ins (and its providers' API keys); it changes when someone signs in or out."""
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "auth.json"


def rejected_providers(checks: dict[str, dict[str, Any]], now: float | None = None) -> dict[str, dict[str, Any]]:
    """Providers whose API key was refused, with the check that showed it. Forgotten after KEY_HOLD, or as soon as the sign-in
    file is newer than the refusal (the key may have been replaced), so the provider gets another chance."""
    now = now or time.time()
    try:
        changed = credentials_file().stat().st_mtime
    except OSError:
        changed = 0.0
    found: dict[str, dict[str, Any]] = {}
    for model, check in checks.items():
        provider = provider_of(model)
        at = float(check.get("at") or 0)
        if provider and not check.get("ok") and check.get("reason") == KEY_REJECTED and now - at < KEY_HOLD and changed <= at:
            if provider not in found or at > float(found[provider].get("at") or 0):
                found[provider] = {**check, "model": model}
    return found


def provider_label(provider: str) -> str:
    return " ".join(word.capitalize() for word in provider.replace("_", "-").split("-"))


def provider_problems(models: Iterable[Any] | None = None, checks: dict[str, dict[str, Any]] | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """What to fix so an AI provider works again: [{provider, name, reason, models, since, fix}], one per provider whose key was refused."""
    checks = load_checks() if checks is None else checks
    ids = [m.id for m in (models or [])]
    out = []
    for provider, check in sorted(rejected_providers(checks, now).items()):
        covered = sorted({m for m in [*ids, *checks] if provider_of(m) == provider})
        out.append({"provider": provider, "name": provider_label(provider), "reason": KEY_REJECTED, "models": len(covered),
                    "since": check.get("at"), "fix": f"Run `opencode auth login` on this computer, choose {provider_label(provider)} and enter a valid key."})
    return out


def forget_provider(provider: str) -> int:
    """Drop every recorded failure for a provider (after its key was replaced), so its models are asked again. Returns how many."""
    if not provider:
        return 0
    with _CHECKS_LOCK:
        data = load_checks()
        gone = [m for m in data if provider_of(m) == provider]
        for m in gone:
            del data[m]
        if gone:
            path = checks_path()
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".checks-")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, path)
    return len(gone)


def failure_reason(text: str) -> str:
    """What went wrong with a model, in a few words, from its error."""
    lowered = re.sub(r"api[-_ ]?key", "api key", text.lower())  # "API-key", "api_key" and "apikey" are all "api key"
    for words, reason in ((("invalid api key", "invalid x-api key", "incorrect api key", "api key is invalid", "api key not valid", "api key was rejected"), KEY_REJECTED),
                          (("quota", "rate limit", "usage limit", "429", "credits"), "out of quota for now"),
                          (("not supported", "not available", "does not exist", "not found", "unknown model", "model_not_found", "isn't available"), "not offered to this sign-in"),
                          (("auth", "login", "sign in", "401", "403", "api key", "unauthorized"), "not signed in, or no access"),
                          (("timed out", "timeout", "took too long"), "didn't answer in time")):
        if any(w in lowered for w in words):
            return reason
    from orchestrator.scripts.model_registry import summarize_cli_error
    return (summarize_cli_error(text) if text.strip() else "didn't answer")[:140]


def _why_skipped(model_id: str, checks: dict[str, dict[str, Any]], rejected: set[str]) -> str:
    own = checks.get(model_id) or {}
    if not own.get("ok") and own.get("reason"):
        return own["reason"]
    return KEY_REJECTED if provider_of(model_id) in rejected else "didn't answer"


def chat_tiers(models: Iterable[Any], checks: dict[str, dict[str, Any]] | None = None, now: float | None = None) -> dict[str, dict[str, Any]]:
    """For each tier: the model it uses, the ones it falls back to, whether that model has been checked, the models left
    out because they failed recently (and why), and, when it has none, why."""
    checks = load_checks() if checks is None else checks
    now = now or time.time()
    # A rejected key is the provider's, not one model's: every model of that provider fails with it until the key is replaced.
    rejected = set(rejected_providers(checks, now))
    failed = lambda m: ((c := checks.get(m.id)) and not c.get("ok") and now - float(c.get("at") or 0) < FAIL_HOLD) or provider_of(m.id) in rejected
    answered = lambda m: (c := checks.get(m.id)) and c.get("ok") and now - float(c.get("at") or 0) < CHECK_FRESH
    groups: dict[str, list[Any]] = {t: [] for t in TIERS}
    for m in models:
        groups[classify(m)].append(m)
    out = {}
    for tier in TIERS:
        ordered = _order(tier, groups[tier])
        skipped = [{"model": m.id, "reason": _why_skipped(m.id, checks, rejected)} for m in ordered if failed(m)]
        ordered = sorted([m for m in ordered if not failed(m)], key=lambda m: not answered(m))  # stable: checked ones first
        ids = [m.id for m in ordered]
        first = checks.get(ids[0]) if ids else None
        skipped_summary = ", ".join(f"{x['model']} ({x['reason']})" for x in skipped[:2])
        out[tier] = {
            "label": TIER_LABELS[tier], "hint": TIER_HINTS[tier],
            "model": ids[0] if ids else "", "models": ids[: 1 + MAX_FALLBACKS],
            "candidates": ids[:MAX_PROBES_PER_TIER],
            "checked": bool(ids) and bool(answered(ordered[0])), "checked_at": (first or {}).get("at") if ids and answered(ordered[0]) else None,
            "skipped": skipped[:5],
            "available": bool(ids),
            "reason": "" if ids else (
                f"None of its models answered: {skipped_summary}." if skipped else {
                "free": "No free model is set up. Install OpenCode or Ollama under AI tools.",
                "balanced": "No mid-priced model is set up. Connect an AI tool such as Claude, Codex or Gemini under AI tools.",
                "complex": "None of the strongest models is set up. Connect Claude or Codex under AI tools.",
            }[tier]),
        }
    return out


def needs_check(tiers: dict[str, dict[str, Any]]) -> bool:
    """True when an available tier's model hasn't answered recently (so the page should check before relying on it)."""
    return any(t["available"] and not t["checked"] for t in tiers.values())


def check_tiers(models: list[Any], probe: Callable[[str], tuple[bool, str, str]], retry_after: float = RETRY_AFTER) -> dict[str, dict[str, Any]]:
    """Ask each tier's models the one-word test question, best first, until one answers; record every result.
    `probe(model)` returns (answered, reason, the model that actually answered). All three tiers are checked at once."""
    from concurrent.futures import ThreadPoolExecutor
    tiers = chat_tiers(models)

    def one(tier: str) -> None:
        refused: set[str] = set()  # providers whose key was just rejected: their other models would fail the same way
        for model in tiers[tier]["candidates"]:
            if provider_of(model) in refused:
                continue
            ok, reason, used = probe(model)
            if not ok:  # once more before it counts: a sign-in being renewed can refuse one call
                time.sleep(retry_after)
                ok, reason, used = probe(model)
            if ok and used and used != model:  # it ran a stand-in: this one isn't what answers
                ok, reason = False, f"answered by {used} instead"
            record_check(model, ok, reason)
            if ok:
                return
            if reason == KEY_REJECTED or failure_reason(reason) == KEY_REJECTED:
                refused.add(provider_of(model))
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(one, [t for t in TIERS if tiers[t]["available"]]))
    return chat_tiers(models)


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


def _project_notes(root: Path, limit: int, total: int | None = None) -> str:
    """README, AGENTS/CLAUDE notes and the markdown under docs/. The notes at the top (README first) get `limit` each, the docs half of it."""
    from orchestrator import prd, project_docs
    parts, used = [], 0
    for f in project_docs.project_files(root):
        if f["path"] == prd.PATH:
            continue  # the requirements are their own section
        try:
            part = f"#### {f['path']}\n{_quote(project_docs.read_project_file(root, f['path']), limit if '/' not in f['path'] else limit // 2)}"
        except Exception:
            continue
        if total is not None and parts and used + len(part) > total:
            break  # the rest of the docs wouldn't fit; the README and agent notes come first, so they are in
        parts.append(part)
        used += len(part)
    return "\n\n".join(parts)


def _jobs(jobs: list[dict[str, Any]]) -> str:
    lines = []
    for j in jobs[:40]:
        state = (j.get("state") or {}).get("label") or j.get("status") or ""
        lines.append(f"- {j.get('title') or j.get('id')}{f' ({state})' if state else ''}{f' · branch {j['branch']}' if j.get('branch') else ''}")
    return "\n".join(lines)


def project_context(root: Path, tier: str = "balanced", jobs: list[dict[str, Any]] | None = None, name: str = "",
                    max_chars: int | None = None) -> str:
    """Everything known about the project, in priority order, within the tier's budget (and `max_chars`, a local model's window).
    Rebuilt for every question. Without written requirements, the README, documents, build files, structure and code speak for it."""
    from orchestrator import prd, project_scan
    budget = min(CONTEXT_BUDGET.get(tier, CONTEXT_BUDGET["balanced"]), max_chars) if max_chars else CONTEXT_BUDGET.get(tier, CONTEXT_BUDGET["balanced"])
    small = tier == "free"
    branch = _git(root, "branch", "--show-current") or "(detached)"
    try:
        scanned = project_scan.scan(root, tier)
    except Exception:  # an unreadable repository must never stop a question
        scanned = {}
    requirements = _quote(prd.read_text(root) or "", min(8_000 if small else 24_000, int(budget * .22)))
    if not prd.is_filled(requirements):
        requirements = "(Not written yet. Work out what the project is from its README and notes, what it is built with, its structure and its code below, and say so when you do.)"
    share = lambda fraction: int(budget * fraction)  # no section may take more than its share: a big docs folder used to leave no room for the rest
    sections = [
        ("Project", f"Name: {name or root.name}\nFolder: {root.name}\nCurrent branch: {branch}"),
        ("Product requirements (the source of truth for what is being built)", requirements),
        ("Documents attached to the requirements", _clip(_attached_documents(root, 3_000 if small else 12_000), share(.10))),
        ("What it is built with", _clip(scanned.get("What it's built with", ""), share(.05))),
        ("Project notes and docs (README first)", _project_notes(root, 2_500 if small else 8_000, share(.25))),
        ("Project structure (folders, with how many files each holds)", _clip(scanned.get("Project structure", ""), share(.08))),
        ("Recent commits (newest first)", _clip(_git(root, "log", "-n", "25" if small else "60", "--date=short", "--format=%h %ad %an: %s"), share(.08))),
        ("Key source files (the start of each)", _clip(scanned.get("Key source files (the start of each)", ""), share(.22))),
        ("Jobs (planned, in progress and done)", _clip(_jobs(jobs or []), share(.05))),
        ("Uncommitted changes", _clip(_git(root, "status", "--short"), min(2_000, share(.03)))),
        ("Branches", _clip(_git(root, "branch", "--format=%(refname:short)"), min(1_500, share(.02)))),
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
                continue  # this one doesn't fit; a smaller one after it still might
            block = f"## {title}\n{_clip(body, room)}"
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


def context_cap(models: list[str]) -> int | None:
    """The most prompt, in characters, that every local model this turn may use can take in whole; None when none is local or it can't be known."""
    from orchestrator import ollama_api
    from orchestrator.scripts.model_registry import get_model
    caps = []
    for model_id in models:
        meta = get_model(model_id)
        local = ollama_api.model_for(model_id, getattr(meta, "required_clis", ()), getattr(meta, "family", "")) if meta else None
        window = ollama_api.window_chars(local) if local else None
        if window:
            caps.append(window)
    return min(caps) if caps else None


def build_prompt(context: str, history: list[dict[str, Any]], question: str) -> str:
    transcript = "\n".join(f"{'User' if m.get('role') == 'user' else 'Assistant'}: {m.get('text', '')}"
                           for m in history[-MAX_HISTORY_MESSAGES:])
    return "\n\n".join(part for part in [
        "You answer questions about one software project for the people building it, inside an AI coding orchestrator. "
        "Everything known about the project is below: its product requirements (when written), attached documents, README and notes, what it is built with, "
        "its folder structure, the start of its main source files, Git history and jobs. "
        "Answer from it, concisely and concretely. When the requirements aren't written, say what the project is from the README, documents and code. "
        "Say plainly when the project information doesn't settle something instead of guessing. "
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
    history = messages(root)
    cap = context_cap(list(choice["models"]))
    room = None if cap is None else max(3_000, cap - len(build_prompt("", history, question)) - 500)  # what is left for the project once the rest is counted
    prompt = build_prompt(project_context(root, tier, jobs, name, max_chars=room), history, question)
    reply, used = llm(prompt, list(choice["models"]))
    reply = (reply or "").strip()
    if not reply:
        raise ChatError("The model returned an empty answer. Try again.")
    if used:
        record_check(used, True)  # a real answer is the best check there is
    thread = messages(root) + [{"role": "user", "text": question, "t": asked_at, "tier": tier},
                               {"role": "assistant", "text": reply, "t": time.time(), "tier": tier, "model": used or choice["model"]}]
    _save(root, thread)
    return thread
