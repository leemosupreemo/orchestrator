"""A check-up of where a project stands and what's missing.

Product state, not tooling (the setup checklist covers CLIs and keys): is there a
brief, are platforms decided, are features and tests in place, can it ship, is
anything measuring it. `evaluate` is pure over a dict of facts the web server
gathers, so the rules are easy to read and test.
"""
from __future__ import annotations

import re
from typing import Any

from orchestrator.product_docs import REVIEW_AFTER_JOBS

BRIEF_JOB = ("Write docs/product-brief.md: what this product is, who it's for, the problem it solves, "
             "and what version 1 must do. Base it on the code and README.")
UNRELEASED_WARN = 20


def brief_platforms(brief_text: str) -> tuple[list[str], bool]:
    """(platforms listed, whether the brief says they're still to be decided)."""
    match = re.search(r"^## Platforms?\s*\n(.*?)(?=^## |\Z)", brief_text, re.M | re.S)
    if not match:
        return [], False
    body = match.group(1)
    platforms = [re.sub(r"^[-*]\s*", "", line).strip() for line in body.splitlines() if re.match(r"^\s*[-*]\s+\S", line)]
    return platforms, "_Not decided._" in body


def _item(id_: str, title: str, status: str, detail: str, **action: Any) -> dict[str, Any]:
    return {"id": id_, "title": title, "status": status, "detail": detail, **action}


def evaluate(f: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    items.append(_item("brief", "Product brief", "ok", "docs/product-brief.md says what this is and what version 1 must do.") if f["brief"]
                 else _item("brief", "Product brief", "todo", "Nothing says what this product is for, so every job guesses.",
                            job={"type": "quick", "summary": BRIEF_JOB}, label="Draft one"))

    docs = f.get("docs") or []
    missing = [d for d in docs if d["id"] != "brief" and not d["filled"]]
    if docs and not missing:
        items.append(_item("product-docs", "Product documents", "ok", "Users and non-goals, journey, screens, technical decisions and the current plan are written."))
    elif docs:
        names = ", ".join(d["title"].lower() for d in missing[:3]) + ("…" if len(missing) > 3 else "")
        items.append(_item("product-docs", "Product documents", "todo", f"{len(missing)} of {len(docs) - 1} aren't filled in yet ({names}). Builders plan against these, so gaps become guesses.",
                           route="#/product", label="Fill them in"))

    if f["recommend_pending"]:
        items.append(_item("platforms", "Platforms", "warn", "Platforms are still undecided. Ask for a recommendation in your next plan, then record the choice in the brief.",
                           job={"type": "feature", "summary": "Recommend the platforms for this product, with reasons"}, label="Get a recommendation"))
    elif f["platforms"] or f["detected"]:
        items.append(_item("platforms", "Platforms", "ok", ", ".join(f["platforms"]) or f"Detected: {f['detected']}"))
    else:
        items.append(_item("platforms", "Platforms", "todo", "Say what you're building for (iOS, Android, web, backend…) so setup, tests and delivery fit.",
                           job={"type": "quick", "summary": "Add a Platforms section to docs/product-brief.md listing what this product is built for."}, label="Add to the brief"))

    items.append(_item("instructions", "Instructions for AI helpers", "ok", "AGENTS.md (or equivalent) is present.") if f["agents"]
                 else _item("instructions", "Instructions for AI helpers", "todo", "No AGENTS.md or CLAUDE.md, so helpers don't know your conventions.",
                            job={"type": "quick", "summary": "Write AGENTS.md: how to build and test, code conventions, and what not to touch."}, label="Draft one"))

    if f["git_repo"] and f["remote"]:
        items.append(_item("repo", "Git and GitHub", "ok", "Version-controlled with a remote."))
    elif f["git_repo"]:
        items.append(_item("repo", "Git and GitHub", "todo", "Git is set up, but there's no remote. Jobs open issues and pull requests on GitHub.",
                           hint="gh repo create --source . --push"))
    else:
        items.append(_item("repo", "Git and GitHub", "todo", "This folder isn't a git repository.", hint="git init"))

    if f["features"] == 0:
        items.append(_item("features", "Features", "todo", "Group the work into features so you can see progress and overlap.", route="#/features", label="Add features"))
    elif f["jobs_unassigned"]:
        items.append(_item("features", "Features", "warn", f"{f['features']} feature(s); {f['jobs_unassigned']} open job(s) aren't in one.", route="#/features", label="Assign jobs"))
    else:
        items.append(_item("features", "Features", "ok", f"{f['features']} feature(s), every open job assigned."))

    if f["suites"] == 0:
        items.append(_item("tests", "Tests", "todo", "No tests found. Every job should add some.", route="#/new?type=coverage", label="Add tests"))
    elif f["cases_gap"]:
        items.append(_item("tests", "Tests", "warn", f"{f['cases_gap']} planned automated case(s) have no test yet.", route="#/tests?cases=unassigned", label="See them"))
    else:
        items.append(_item("tests", "Tests", "ok", f"{f['suites']} suite(s); every planned case is covered." if f["cases_total"] else f"{f['suites']} suite(s) found."))

    if not f["ci"]:
        items.append(_item("ci", "Automated builds (CI)", "todo", "Nothing runs your tests on every push.",
                           job={"type": "quick", "summary": "Add a CI workflow that builds the project and runs the tests on every push and pull request."}, label="Add CI"))
    elif f["pipeline_failing"]:
        items.append(_item("ci", "Automated builds (CI)", "warn", "The latest build on the main branch is failing.", route="#/delivery", label="See pipeline"))
    else:
        items.append(_item("ci", "Automated builds (CI)", "ok", "CI is configured."))

    if not f["distribution"]:
        items.append(_item("delivery", "Getting builds to testers", "todo", "No tester distribution is set up.", route="#/config/firebase", label="Set up Firebase"))
    elif not f["builds_sent"]:
        items.append(_item("delivery", "Getting builds to testers", "todo", "Set up, but nothing has been sent to testers yet.", route="#/delivery", label="Send a build"))
    else:
        items.append(_item("delivery", "Getting builds to testers", "ok", f"{f['builds_sent']} build(s) sent."))

    if not f["tag"]:
        items.append(_item("release", "Releases", "todo", "No release tagged yet, so \"what's live\" has no anchor.", hint="git tag v0.1.0 && git push --tags"))
    elif f["unreleased"] is not None and f["unreleased"] > UNRELEASED_WARN:
        items.append(_item("release", "Releases", "warn", f"{f['unreleased']} changes since {f['tag']}. Time for a release?", route="#/delivery", label="See what's live"))
    else:
        items.append(_item("release", "Releases", "ok", f"Last release {f['tag']}."))

    review = f.get("review")
    if review and not review["stale"]:
        items.append(_item("review", "Product review", "ok", f"Last review {review['days']} day(s) ago."))
    elif f["jobs_total"] >= REVIEW_AFTER_JOBS:
        detail = f"Last review was {review['days']} days ago." if review else "No review yet."
        items.append(_item("review", "Product review", "warn", f"{detail} A review checks drift from the brief, architecture, duplication and UX consistency.",
                           route="#/product", label="Run a review"))
    else:
        items.append(_item("review", "Product review", "ok", f"Worth doing after about {REVIEW_AFTER_JOBS} jobs."))

    if f["features_needing_kpis"]:
        items.append(_item("kpis", "Measuring it", "todo", f"{f['features_needing_kpis']} feature(s) with work in them have no KPI, so you can't tell if they worked.", route="#/measure", label="Add KPIs"))
    elif f["kpis_total"] and not f["kpis_measured"]:
        items.append(_item("kpis", "Measuring it", "warn", "KPIs are defined but no results are logged yet.", route="#/measure", label="Log a result"))
    elif f["kpis_total"]:
        items.append(_item("kpis", "Measuring it", "ok", f"{f['kpis_measured']} of {f['kpis_total']} KPI(s) have results."))
    else:
        items.append(_item("kpis", "Measuring it", "todo", "No KPIs yet.", route="#/measure", label="Set up measuring"))

    ok = sum(1 for i in items if i["status"] == "ok")
    nxt = next((i for i in items if i["status"] == "todo"), None) or next((i for i in items if i["status"] == "warn"), None)
    if f["tag"]:
        stage = "Released" + (" (changes pending)" if f["unreleased"] else "")
    elif f["builds_sent"]:
        stage = "With testers"
    elif f["jobs_total"]:
        stage = "Building"
    else:
        stage = "Getting started"
    return {"stage": stage, "ok": ok, "total": len(items), "items": items, "next": nxt["id"] if nxt else None}
