"""A check-up of where a project stands and what's missing.

Product state, not tooling (the setup checklist covers CLIs and keys): is there a
product requirements, are platforms decided, are features and tests in place, can it ship, is
anything measuring it. `evaluate` is pure over a dict of facts the web server
gathers, so the rules are easy to read and test.
"""
from __future__ import annotations

from typing import Any

UNRELEASED_WARN = 20


def _item(id_: str, title: str, status: str, detail: str, **action: Any) -> dict[str, Any]:
    return {"id": id_, "title": title, "status": status, "detail": detail, **action}


def _n(count: int, word: str, plural: str | None = None) -> str:
    """"1 feature", "3 features": counts read as words, never "feature(s)"."""
    return f"{count} {word if count == 1 else plural or word + 's'}"


def evaluate(f: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    sections = f.get("prd_sections") or {}
    written = [k for k, v in sections.items() if v]
    if not written:
        items.append(_item("prd", "Product requirements", "todo", "Optional, but nothing says what this product is for, so every job guesses. Describe it in a few sentences, draft it from the project, or import a PRD.",
                           route="#/product", label="Add it"))
    else:
        updates = "It updates itself as jobs finish." if f.get("prd_auto_update") else "Automatic updates are off."
        items.append(_item("prd", "Product requirements", "ok", f"{len(written)} of 5 sections written. {updates}"))

    if f["recommend_pending"]:
        items.append(_item("platforms", "Platforms", "warn", "Platforms are still undecided. Ask for a recommendation in your next plan, then record the choice in the product requirements.",
                           job={"type": "feature", "summary": "Recommend the platforms for this product, with reasons"}, label="Get a recommendation"))
    elif f["platforms"] or f["detected"]:
        items.append(_item("platforms", "Platforms", "ok", ", ".join(f["platforms"]) or f"Detected: {f['detected']}"))
    else:
        items.append(_item("platforms", "Platforms", "todo", "Say what you're building for (iOS, Android, web, backend…) so setup, tests and delivery fit.",
                           route="#/product", label="Say what it is for"))

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
        items.append(_item("features", "Features", "todo", "List what it has to do under Core features, so jobs can be grouped.", route="#/product?section=features", label="Add features"))
    elif f["jobs_unassigned"]:
        items.append(_item("features", "Features", "warn", f"{_n(f['features'], 'feature')}; " + (f"1 open job isn't in one." if f["jobs_unassigned"] == 1 else f"{f['jobs_unassigned']} open jobs aren't in one."), route="#/", label="See jobs"))
    else:
        items.append(_item("features", "Features", "ok", f"{_n(f['features'], 'feature')}, every open job assigned."))

    if f["suites"] == 0:
        items.append(_item("tests", "Tests", "todo", "No tests found. Every job should add some.", route="#/new?type=coverage", label="Add tests"))
    elif f["cases_gap"]:
        items.append(_item("tests", "Tests", "warn", f"{_n(f['cases_gap'], 'planned automated case')} {'has' if f['cases_gap'] == 1 else 'have'} no test yet.", route="#/tests?cases=unassigned", label="See them"))
    else:
        items.append(_item("tests", "Tests", "ok", f"{_n(f['suites'], 'test file')}; every planned case is covered." if f["cases_total"] else f"{_n(f['suites'], 'test file')} found."))

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
        items.append(_item("delivery", "Getting builds to testers", "ok", f"{_n(f['builds_sent'], 'build')} sent."))

    if not f["tag"]:
        items.append(_item("release", "Releases", "todo", "No release tagged yet, so \"what's live\" has no anchor.", hint="git tag v0.1.0 && git push --tags"))
    elif f["unreleased"] is not None and f["unreleased"] > UNRELEASED_WARN:
        items.append(_item("release", "Releases", "warn", f"{f['unreleased']} changes since {f['tag']}. Time for a release?", route="#/delivery", label="See what's live"))
    else:
        items.append(_item("release", "Releases", "ok", f"Last release {f['tag']}."))

    if f["features_needing_kpis"]:
        items.append(_item("kpis", "Measuring it", "todo", f"{_n(f['features_needing_kpis'], 'feature')} with work in {'it has' if f['features_needing_kpis'] == 1 else 'them have'} no KPI, so you can't tell if {'it' if f['features_needing_kpis'] == 1 else 'they'} worked.", route="#/measure", label="Add KPIs"))
    elif f["kpis_total"] and not f["kpis_measured"]:
        items.append(_item("kpis", "Measuring it", "warn", "KPIs are defined but no results are logged yet.", route="#/measure", label="Log a result"))
    elif f["kpis_total"]:
        items.append(_item("kpis", "Measuring it", "ok", f"{f['kpis_measured']} of {_n(f['kpis_total'], 'KPI')} {'has' if f['kpis_measured'] == 1 else 'have'} results."))
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
