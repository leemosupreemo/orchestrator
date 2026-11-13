"""Is this new job one we already have?

Starting the same work twice wastes model time and ends in two branches changing the same code. Before a job is
created, its description is compared with the jobs still open (not done or archived), by the words that carry meaning:
"Add a login page" and "Login page with Google sign-in" are the same piece of work; "Fix the login bug" and "Add a login
page" are not. A close match is put to the person (open that one, or create this anyway), never refused outright.

Word overlap is cheap, needs no model, and is right for the common case: the same request made twice, or a suggestion
accepted after a job for it was already started. It doesn't catch the same work in different words.
"""
from __future__ import annotations

import re
from typing import Any

# Words every job description has, which say nothing about which work it is.
STOP = set("""a an the and or but of to in on for with by at from into onto as is are be it its this that these those
then than so via per add adds added adding make makes making fix fixes fixed fixing update updates updated updating
implement implements implemented create creates created change changes changed improve improves improved new
support supports use uses using allow allows let lets should can could would need needs want wants please job task feature
bug issue some all any more less also when where what which who how get gets set sets up""".split())

MIN_SHARED = 2          # at least two meaningful words in common...
MIN_COVERAGE = 0.75     # ...covering most of the shorter description...
MIN_JACCARD = 0.34      # ...and a fair part of both together
MAX_MATCHES = 3


def words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in STOP or len(w) < 2:
            continue
        for suffix in ("ing", "ed", "s"):  # "pages" and "page", "logging" and "log" are the same word here
            if len(w) > len(suffix) + 2 and w.endswith(suffix):
                w = w[: -len(suffix)]
                break
        out.add(w)
    return out


def similarity(a: str, b: str) -> float:
    """0 when two descriptions aren't the same work, otherwise how alike they are (up to 1)."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    shared = wa & wb
    if wa == wb:
        return 1.0
    if len(shared) < MIN_SHARED:
        return 0.0
    coverage = len(shared) / min(len(wa), len(wb))
    jaccard = len(shared) / len(wa | wb)
    return round((coverage + jaccard) / 2, 3) if coverage >= MIN_COVERAGE and jaccard >= MIN_JACCARD else 0.0


def is_open(job: dict[str, Any]) -> bool:
    return (job.get("state") or {}).get("group") != "done" and not job.get("archived")


def similar_jobs(description: str, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Open jobs that look like the same work as `description`, closest first: [{id, title, state, score}]."""
    found = []
    for j in jobs:
        if not is_open(j):
            continue
        score = similarity(description, j.get("title") or "")
        if score:
            found.append({"id": j.get("id"), "title": j.get("title") or j.get("id"),
                          "state": (j.get("state") or {}).get("label") or "", "score": score})
    return sorted(found, key=lambda m: -m["score"])[:MAX_MATCHES]


def description_of(params: dict[str, Any]) -> str:
    """What a New job request says it is about."""
    return str(params.get("title") or params.get("summary") or "").strip()
