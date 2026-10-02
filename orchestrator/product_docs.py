"""The documents that define a product, kept in the repo so every job can read them.

An LLM does its best work with a hierarchy of context: vision, then users and use
cases, then the journey, then screens, then technical decisions, then the current
plan, then the task. These files are that hierarchy. Each planner is handed the
filled-in ones (`context_block`), and each can be viewed, edited and refined with
the model from the Product area of the web UI (`refine_prompt`, `questions_prompt`).

Nothing here calls a model; the web server runs the prompts.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

MAX_DOC_CHARS = 100_000
BRIEF_CONTEXT_CHARS = 5000
OTHER_CONTEXT_CHARS = 2500
REVIEW_DIR = "docs/product/reviews"
REVIEW_STALE_DAYS = 14

DOCS: list[dict[str, Any]] = [
    {"id": "brief", "title": "Product brief", "path": "docs/product-brief.md",
     "purpose": "What it is, who it's for, the problem it solves, and what version 1 must do.", "template": None},
    {"id": "use-cases", "title": "Users, use cases and non-goals", "path": "docs/product/use-cases.md",
     "purpose": "The primary user, the 3-5 things they must be able to do, the edge cases that matter, and what version 1 will not do.",
     "template": """# Users, use cases and non-goals

## Primary user

_Who is this mainly for? One or two sentences._

## Core use cases

_3-5 things people must be able to do, one per line: "As a ..., I can ... so that ..."._

## Edge cases that matter

_Situations that would break trust if handled badly: offline, empty states, conflicts, errors._

## Non-goals for version 1

_What this will NOT do yet. Be specific. Builders read this so they don't build it by accident._
"""},
    {"id": "journey", "title": "User journey", "path": "docs/product/journey.md",
     "purpose": "The happy path from never having heard of it to repeat use, and where people might give up.",
     "template": """# User journey

## Finding it and starting

_How does someone first hear of it, and what is the very first thing they do?_

## First value

_What is the first moment it clearly works for them? How few steps get there?_

## Repeat use

_Why would they come back? What does the second and tenth visit look like?_

## Where people might quit

_Unnecessary steps, confusing moments, things we are assuming they will understand._

## Assumptions to check

_What are we taking for granted about the user?_
"""},
    {"id": "screens", "title": "Screens and navigation", "path": "docs/product/screens.md",
     "purpose": "Every screen, why it exists, and how people move between them.",
     "template": """# Screens and navigation

## Screen inventory

_One entry per screen: name, which use case it supports, its main actions._

## Navigation model

_How do people move around? Tabs, a stack, a single flow? What is the home screen?_

## Rules every screen follows

_Shared behaviour: loading, empty and error states, accessibility, tone of voice._
"""},
    {"id": "architecture", "title": "Technical decisions", "path": "docs/product/architecture-decisions.md",
     "purpose": "The stack, data model, auth, services and deployment, split into hard-to-change and easy-to-change decisions.",
     "template": """# Technical decisions

## Hard to change later

_Platform and language, data model, how users are identified, where data lives, third-party services you depend on. Spend your attention here._

## Easy to change later

_Choices you can safely revisit: libraries, styling, naming, internal structure._

## Services and deployment

_What runs where? Analytics, crash reporting, hosting, CI._

## Open questions

_Decisions not made yet, and what would help make them._
"""},
    {"id": "plan", "title": "Current plan", "path": "docs/product/plan.md",
     "purpose": "What is being built now, what is next, and what is later. One working slice at a time.",
     "template": """# Current plan

## Now

_The one slice being built: the smallest version of the core experience that works end to end._

## Next

_The slices after it, in order._

## Later

_Ideas parked on purpose._

## Done recently

_What shipped, so the model can see what already exists._
"""},
]
BY_ID = {d["id"]: d for d in DOCS}


class ProductDocError(ValueError):
    pass


def get(doc_id: str) -> dict[str, Any]:
    if doc_id not in BY_ID:
        raise ProductDocError("Unknown document")
    return BY_ID[doc_id]


def path_of(root: Path, doc_id: str) -> Path:
    return root / get(doc_id)["path"]


def read(root: Path, doc_id: str) -> str | None:
    try:
        return path_of(root, doc_id).read_text(encoding="utf-8")
    except OSError:
        return None


def write(root: Path, doc_id: str, text: str) -> Path:
    if not isinstance(text, str) or not text.strip():
        raise ProductDocError("A document can't be empty")
    if len(text) > MAX_DOC_CHARS:
        raise ProductDocError(f"Keep it under {MAX_DOC_CHARS // 1000} thousand characters")
    path = path_of(root, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def is_filled(text: str | None) -> bool:
    """More than headings and the guidance lines in a fresh template."""
    if not text:
        return False
    real = [l.strip() for l in text.splitlines()
            if l.strip() and not l.lstrip().startswith("#") and not re.fullmatch(r"_.*_", l.strip()) and l.strip().lower() not in ("tbd", "- tbd")]
    return len(real) >= 3


def listing(root: Path) -> list[dict[str, Any]]:
    out = []
    for d in DOCS:
        path = root / d["path"]
        text = read(root, d["id"])
        out.append({"id": d["id"], "title": d["title"], "path": d["path"], "purpose": d["purpose"], "exists": text is not None,
                    "filled": is_filled(text), "words": len(text.split()) if text else 0, "updated": path.stat().st_mtime if text is not None else None})
    return out


def scaffold(root: Path) -> list[str]:
    """Create the documents that don't exist yet from their templates. Never overwrites anything."""
    created = []
    for d in DOCS:
        if d["template"] and not (root / d["path"]).exists():
            write(root, d["id"], d["template"])
            created.append(d["id"])
    return created


def context_block(root: Path) -> str:
    """What every planner should read first: the filled-in product documents, trimmed."""
    parts = []
    for d in DOCS:
        text = read(root, d["id"])
        if not is_filled(text):
            continue
        limit = BRIEF_CONTEXT_CHARS if d["id"] == "brief" else OTHER_CONTEXT_CHARS
        body = text.strip()
        if len(body) > limit:
            body = body[:limit].rstrip() + "\n...(trimmed)"
        parts.append(f"### {d['title']} (`{d['path']}`)\n{body}")
    if not parts:
        return ""
    return ("\n\n## Product context (source of truth)\n"
            "These documents define the product. Serve the use cases, stay inside the non-goals, and do not contradict them "
            "without saying so in the plan's assumptions or risks.\n\n" + "\n\n".join(parts) + "\n")


def _json_reply(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ProductDocError("The model didn't answer in the expected form. Try again.")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise ProductDocError("The model didn't answer in the expected form. Try again.")
    if not isinstance(data, dict):
        raise ProductDocError("The model didn't answer in the expected form. Try again.")
    return data


def _others(root: Path, doc_id: str) -> str:
    parts = []
    for d in DOCS:
        if d["id"] == doc_id:
            continue
        text = read(root, d["id"])
        if is_filled(text):
            parts.append(f"## {d['title']}\n{text.strip()[:OTHER_CONTEXT_CHARS]}")
    return "\n\n".join(parts) or "(none yet)"


def questions_prompt(root: Path, doc_id: str, instruction: str = "") -> str:
    d = get(doc_id)
    current = read(root, doc_id) or d["template"] or ""
    return f"""You are a product manager helping define a product. Before anything is written, find out what you need to know.

Document being worked on: {d['title']} ({d['purpose']})
{('What the person wants: ' + instruction) if instruction.strip() else ''}

Current document:
{current}

Other product documents:
{_others(root, doc_id)}

Ask at most 6 questions. Only ask what materially changes the product or this document, and skip anything the documents already answer. Prefer concrete questions a person can answer in a sentence. For each, say why it matters.

Reply with ONLY JSON: {{"questions": [{{"question": "...", "why": "..."}}]}}"""


def refine_prompt(root: Path, doc_id: str, instruction: str, answers: list[dict[str, str]] | None = None) -> str:
    d = get(doc_id)
    current = read(root, doc_id) or d["template"] or ""
    qa = "\n".join(f"- Q: {a.get('question', '')}\n  A: {a.get('answer', '')}" for a in (answers or []) if str(a.get("answer", "")).strip())
    return f"""You are a product manager and designer keeping the product's source-of-truth documents accurate.

Document: {d['title']} ({d['purpose']})

Current document:
{current}

Other product documents:
{_others(root, doc_id)}

Request: {instruction.strip() or 'Fill in the empty sections from what the other documents say, and tighten the rest.'}
{('Answers from the person:' + chr(10) + qa) if qa else ''}

Rules:
- Return the COMPLETE updated document in Markdown, keeping the existing headings and order unless asked to change them.
- Do not invent facts. Where something is unknown, write "TBD:" and the question that would settle it.
- Be specific and brief. Use short lists. No marketing language.
- Scope: keep version 1 small. Anything not clearly needed goes under non-goals, not into the plan.

Reply with ONLY JSON: {{"summary": "one or two sentences on what changed and why", "markdown": "the complete document"}}"""


def parse_questions(reply: str) -> list[dict[str, str]]:
    items = _json_reply(reply).get("questions")
    out = []
    for q in items if isinstance(items, list) else []:
        if isinstance(q, dict) and str(q.get("question", "")).strip():
            out.append({"question": str(q["question"]).strip()[:400], "why": str(q.get("why", "")).strip()[:400]})
    if not out:
        raise ProductDocError("The model had no questions. Go ahead and describe what you want.")
    return out[:6]


def parse_proposal(reply: str) -> dict[str, str]:
    data = _json_reply(reply)
    markdown = data.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ProductDocError("The model returned no document. Try again.")
    if len(markdown) > MAX_DOC_CHARS:
        raise ProductDocError("The proposed document is too long. Ask for a shorter one.")
    return {"markdown": markdown.strip() + "\n", "summary": str(data.get("summary", "")).strip()[:500]}


def unified_diff(old: str, new: str) -> list[str]:
    return list(difflib.unified_diff(old.splitlines(), new.splitlines(), "current", "proposed", lineterm="", n=2))


# --------------------------------------------------------------------------- product review


def review_instruction(root: Path) -> str:
    stamp = time.strftime("%Y-%m-%d")
    names = ", ".join(f"`{d['path']}`" for d in DOCS if read(root, d["id"]) is not None) or "the product documents"
    return (f"Review the product as it exists now against what it is meant to be. Do NOT change any code. Read {names} and the codebase, "
            f"then write your findings to `{REVIEW_DIR}/{stamp}.md` with these sections: "
            "1) Drift from the brief and use cases (what was built that nobody asked for, what was asked for and is missing); "
            "2) Architecture (decisions that are hard to change and look shaky, tech debt, anything that fights the stated decisions); "
            "3) Duplication and dead code worth removing; "
            "4) UX consistency (screens, wording and behaviour that disagree); "
            "5) Recommended next three slices, each small enough to run end to end. "
            "Be specific: name files and screens. Keep it under two pages.")


def last_review(root: Path) -> dict[str, Any] | None:
    folder = root / REVIEW_DIR
    files = sorted(folder.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
    if not files:
        return None
    age = (time.time() - files[0].stat().st_mtime) / 86400
    return {"path": str(files[0].relative_to(root)), "days": int(age), "stale": age > REVIEW_STALE_DAYS}
