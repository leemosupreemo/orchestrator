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
bug issue some all any more less also when where what which who how get gets set sets up quick freestyle""".split())

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


def _edits(a: str, b: str, limit: int) -> int:
    """Edit distance between two words, giving up (limit + 1) once it's over `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def same_word(a: str, b: str) -> bool:
    """The same word, allowing a typo in a long one: "vegetatation" is "vegetation", "lgoin" isn't "login" (too short)."""
    if a == b:
        return True
    shorter = min(len(a), len(b))
    limit = 2 if shorter >= 8 else 1 if shorter >= 5 else 0
    return bool(limit) and _edits(a, b, limit) <= limit


def _aligned(wa: set[str], wb: set[str]) -> set[str]:
    """`wb` with each word spelled as its near match in `wa`, so a typo counts as the word it meant."""
    return {next((x for x in wa if same_word(x, w)), w) for w in wb}


def similarity(a: str, b: str) -> float:
    """0 when two descriptions aren't the same work, otherwise how alike they are (up to 1)."""
    wa, wb = words(a), words(b)
    wb = _aligned(wa, wb)
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


# ---- overlap by files: two open jobs changing the same files end in conflicting branches, or one taking the other's work

def planned_files(job: dict[str, Any]) -> set[str]:
    """Files a job plans to change (its plan's and tasks' likely files) or has changed."""
    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    files = set(plan.get("likely_files") or []) if isinstance(plan.get("likely_files"), list) else set()
    for task in plan.get("tasks") or []:
        if isinstance(task, dict) and isinstance(task.get("likely_files"), list):
            files |= set(task["likely_files"])
    files |= set(job.get("ai_modified_files") or []) | set(job.get("ai_untracked_files") or [])
    return {f.strip().lstrip("./") for f in files if isinstance(f, str) and f.strip() and not f.strip().endswith("/")}


def file_overlaps(job_id: str, job: dict[str, Any], others: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Other open jobs that plan to change, or have changed, any of the same files: [{id, title, files}], most shared first.
    Nothing is refused: the job page says so, so the person can finish one first, merge them, or carry on knowing.
    Each also says what the job page needs to give one plain next step: `duplicate` (the same request twice, by title),
    `running` (that job is building now), `ready` (it waits to be merged) and `pr` (it has a pull request)."""
    from orchestrator.parallel_jobs import _alive  # parallel_jobs imports this module

    mine = planned_files(job)
    if not mine or job.get("status") in ("completed", "archived") or job.get("moved_to"):
        return []
    found = []
    for other_id, other in others:
        if other_id == job_id or other_id.startswith(f"{job_id}_task") or job_id.startswith(f"{other_id}_task"):
            continue
        if other.get("status") in ("completed", "archived") or other.get("moved_to"):
            continue
        shared = sorted(mine & planned_files(other))
        if shared:
            found.append({"id": other_id, "title": other.get("title") or other_id, "files": shared[:10], "count": len(shared),
                          "duplicate": bool(similarity(str(job.get("title") or ""), str(other.get("title") or ""))),
                          "running": _alive(other), "ready": other.get("status") == "review-needed",
                          "pr": bool(other.get("pr_number"))})
    return sorted(found, key=lambda o: -o["count"])[:MAX_MATCHES]

