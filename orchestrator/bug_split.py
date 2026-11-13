"""Pasted notes about several bugs, separated into one bug each.

`prompt` asks a model to split the text; `parse` turns its answer into a short list of bugs ({summary, repro, expected}) the New job
form can create jobs from. The text is the person's own: nothing is dropped silently (a bug the model skipped is why the form shows the
list for review before anything is created).
"""
from __future__ import annotations

import json
import re
from typing import Any

MAX_BUGS = 12
SUMMARY_CHARS = 200

REMINDER = ("\n\nYour last answer could not be used. Reply with ONLY one JSON object, no prose and no code fence, shaped like "
            '{"bugs": [{"summary": "...", "repro": "...", "expected": "..."}]}.')


class BugSplitError(ValueError):
    pass


def prompt(text: str, existing_titles: list[str] | None = None) -> str:
    known = [f"- {t}" for t in (existing_titles or [])[:30]]
    return "\n".join([
        "Someone pasted notes about problems in their software. They may describe one bug or several: a list, an email, chat messages, test notes.",
        "Separate them into distinct bugs, one job each.",
        "",
        "Rules:",
        "- One entry per separate problem. If two sentences describe the same problem, they are one bug. If one sentence names two unrelated problems, they are two.",
        f"- A short plain summary for each, under {SUMMARY_CHARS} characters, that says what goes wrong. Do not number them or add labels.",
        "- Keep each bug's own repro steps and expected behaviour with it, in the person's words. Leave repro or expected empty when they didn't say.",
        "- Do not invent steps, causes or fixes. Do not merge a vague complaint into a specific bug.",
        "- Questions, feature requests and praise are not bugs: leave them out.",
        f"- At most {MAX_BUGS} bugs. If there is only one bug, return exactly one.",
        *( ["", "Bugs already open (do not repeat these):", *known] if known else [] ),
        "",
        "Notes:",
        '"""',
        text.strip()[:20000],
        '"""',
        "",
        "Answer with ONLY one JSON object and nothing else:",
        '{"bugs": [{"summary": "...", "repro": "steps, one per line, or an empty string", "expected": "what should happen, or an empty string"}]}',
    ])


def _object_from(reply: str) -> Any:
    text = reply.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise BugSplitError("The model didn't separate the bugs.")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[min(starts):])
    except json.JSONDecodeError:
        raise BugSplitError("The model's answer wasn't readable.")
    return value


def _line(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def parse(reply: str) -> dict[str, Any]:
    """{"bugs": [{summary, repro, expected}]}: at least one, at most MAX_BUGS, duplicates removed. Raises BugSplitError if unusable."""
    value = _object_from(reply)
    raw = value.get("bugs") if isinstance(value, dict) else value
    if not isinstance(raw, list):
        raise BugSplitError("The model's answer wasn't a list of bugs.")
    bugs, seen = [], set()
    for item in raw:
        if isinstance(item, str):
            item = {"summary": item}
        if not isinstance(item, dict):
            continue
        summary = _line(item.get("summary") or item.get("title"), SUMMARY_CHARS)
        key = re.sub(r"[^a-z0-9]+", " ", summary.lower()).strip()
        if not summary or key in seen:
            continue
        seen.add(key)
        # Steps keep their line breaks: they are read one per line.
        steps = "\n".join(l.strip() for l in str(item.get("repro") or "").splitlines() if l.strip())[:4000]
        bugs.append({"summary": summary, "repro": steps, "expected": _line(item.get("expected"), 1000)})
    if not bugs:
        raise BugSplitError("The model didn't find a bug in that text.")
    return {"bugs": bugs[:MAX_BUGS]}
