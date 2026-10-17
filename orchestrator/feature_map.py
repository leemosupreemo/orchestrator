"""Draft a product's feature map from its product requirements: features with user stories, the code they'll own,
what each builds on, and the order to build them in.

The model proposes; this module checks the proposal before anyone sees it (unique names, dependencies that exist,
no circles, overlapping code flagged) and puts it in build order. Nothing is saved until the person accepts some or
all of it, and accepting creates the features in dependency order through the normal feature store.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator import features as store
from orchestrator import prd

MAX_FEATURES = 15
MAX_STORIES = 8


class FeatureMapError(ValueError):
    pass


def prompt(prd_text: str, existing: list[dict[str, Any]], layout: str) -> str:
    have = "\n".join(f"- {f['name']}: {f.get('summary') or ''} (owns {', '.join(f.get('paths') or []) or 'no paths yet'})"
                     for f in existing) or "(none yet)"
    return f"""You are planning a product's features from its product requirements.

Break the product into features that can each be built and shipped on their own: small enough for one or a few jobs,
large enough to matter to a user. For each feature write user stories ("As a <who>, I can <do what>, so that <why>"),
the code paths it should own (folders or files; new ones are fine), and which other features it depends on.
Order matters: a feature should depend only on what it truly needs, so independent features can be built in parallel.
Two features must not own the same code. Do not repeat a feature that already exists; you may depend on one.
Prefer the first features to give something a user can try end to end, however small.

Reply with ONLY this JSON, no commentary:
{{"features": [{{"name": "...", "summary": "one sentence", "stories": ["As a ..."], "paths": ["app/feature/"],
  "depends_on": ["name of another feature"], "serves": "which core feature or use case from the requirements"}}]}}

## Product requirements
{prd_text.strip()[:20000]}

## Features that already exist
{have}

## Project layout
{layout.strip()[:6000] or "(new project: nothing yet)"}
"""


def _features_from(reply: str) -> list[Any]:
    try:
        data = prd.json_reply(reply)
    except prd.PrdError:
        data = {}
    if not isinstance(data.get("features"), list) or not data["features"]:
        raise FeatureMapError("The model didn't answer with a list of features. Try again.")
    return data["features"]


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _strings(value: Any, limit: int, each: int) -> list[str]:
    out: list[str] = []
    for item in value if isinstance(value, list) else []:
        text = _text(item, each)
        if text and text not in out:
            out.append(text)
    return out[:limit]


def parse(reply: str, existing: list[dict[str, Any]]) -> dict[str, Any]:
    """A model's reply, checked: see `validate`."""
    return validate(_features_from(reply), existing)


def validate(raw: list[Any], existing: list[dict[str, Any]]) -> dict[str, Any]:
    """Proposed features, checked and in build order (each with a `layer`), plus warnings to show the person.
    Used on the model's reply and again on what the page sends back to accept."""
    existing_by_name = {f["name"].strip().lower(): f for f in existing}
    warnings: list[str] = []
    proposed: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not _text(entry.get("name"), 80):
            continue
        name = _text(entry.get("name"), 80)
        if name.lower() in existing_by_name:
            warnings.append(f"\"{name}\" already exists, so it isn't proposed again.")
            continue
        if any(f["name"].lower() == name.lower() for f in proposed):
            raise FeatureMapError(f"\"{name}\" is listed twice. Try again.")
        proposed.append({"name": name, "summary": _text(entry.get("summary"), 500),
                         "stories": store.clean_stories(entry.get("stories"), MAX_STORIES),
                         "paths": _strings(entry.get("paths"), 12, 200), "serves": _text(entry.get("serves"), 300),
                         "depends_on": _strings(entry.get("depends_on"), 10, 80)})
        if len(proposed) == MAX_FEATURES:
            break
    if not proposed:
        raise FeatureMapError("Every proposed feature already exists.")
    # Everything below works on one graph keyed by name: existing features plus proposed ones.
    names = {f["name"].lower(): f["name"] for f in existing} | {f["name"].lower(): f["name"] for f in proposed}
    for feature in proposed:
        kept = []
        for dep in feature["depends_on"]:
            if dep.lower() in names and dep.lower() != feature["name"].lower():
                kept.append(names[dep.lower()])
            else:
                warnings.append(f"\"{feature['name']}\" depended on \"{dep}\", which isn't a feature; that was dropped.")
        feature["depends_on"] = list(dict.fromkeys(kept))
    by_id = {f["id"]: f["name"] for f in existing}
    graph = [{"id": f["name"], "name": f["name"], "paths": f.get("paths") or [],
              "depends_on": [by_id.get(d, d) for d in f.get("depends_on") or []]} for f in existing]
    graph += [{"id": f["name"], "name": f["name"], "paths": f["paths"], "depends_on": f["depends_on"]} for f in proposed]
    for feature in proposed:
        if store.would_cycle(graph, feature["name"], feature["depends_on"]):
            raise FeatureMapError(f"The proposed features depend on each other in a circle (through \"{feature['name']}\"). Try again.")
    depth = store.layers(graph)
    for feature in proposed:
        feature["layer"] = depth[feature["name"]]
    proposed_names = {f["name"] for f in proposed}
    for overlap in store.overlaps(graph, []):
        if proposed_names & set(overlap["features"]):  # clashes only among existing features aren't this draft's business
            warnings.append(f"{' and '.join(overlap['names'])} would overlap: {'; '.join(overlap['reasons'])}.")
    position = {f["name"]: i for i, f in enumerate(proposed)}
    proposed.sort(key=lambda f: (f["layer"], position[f["name"]]))
    return {"features": proposed, "warnings": list(dict.fromkeys(warnings))}


def accept(runtime: Path, proposed: list[dict[str, Any]], chosen: list[str]) -> list[dict[str, Any]]:
    """Create the chosen features, dependencies first. Refuses if one depends on a proposed feature not chosen."""
    chosen_keys = {c.strip().lower() for c in chosen}
    picked = [f for f in proposed if f["name"].lower() in chosen_keys]
    proposed_keys = {f["name"].lower() for f in proposed}
    for feature in picked:
        missing = [d for d in feature["depends_on"] if d.lower() in proposed_keys and d.lower() not in chosen_keys]
        if missing:
            raise FeatureMapError(f"\"{feature['name']}\" builds on {', '.join(missing)}; choose that too, or leave both out.")
    created: list[dict[str, Any]] = []
    for feature in sorted(picked, key=lambda f: f["layer"]):
        ids = {f["name"].lower(): f["id"] for f in store.load(runtime)}
        created.append(store.create(runtime, feature["name"], feature["summary"], feature["paths"],
                                    [ids[d.lower()] for d in feature["depends_on"] if d.lower() in ids], feature["serves"],
                                    stories=feature["stories"]))
    return created
