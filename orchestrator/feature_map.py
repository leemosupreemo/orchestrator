"""Draft a product's feature map from its product requirements: features with user stories, the code they'll own,
what each builds on, and the order to build them in.

The model proposes; this module checks the proposal before anyone sees it (unique names, dependencies that exist,
no circles, overlapping code flagged) and puts it in build order. Nothing is saved until the person accepts some or
all of it, and accepting creates the features in dependency order through the normal feature store.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from orchestrator import features as store

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


def _json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1]) if start != -1 and end > start else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("features"), list) or not data["features"]:
        raise FeatureMapError("The model didn't answer with a list of features. Try again.")
    return data


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _strings(value: Any, limit: int, each: int) -> list[str]:
    items = value if isinstance(value, list) else []
    out = []
    for item in items:
        text = _text(item, each)
        if text and text not in out:
            out.append(text)
    return out[:limit]


def _layers(names: list[str], deps: dict[str, list[str]], existing_depth: dict[str, int]) -> dict[str, int]:
    depth: dict[str, int] = {}

    def visit(name: str, trail: tuple[str, ...]) -> int:
        if name in depth:
            return depth[name]
        if name in trail:
            raise FeatureMapError(f"The proposed features depend on each other in a circle ({' → '.join(trail + (name,))}). Try again.")
        below = [visit(d, trail + (name,)) if d in deps else existing_depth.get(d, 0) for d in deps[name]]
        depth[name] = 1 + max(below, default=-1)
        return depth[name]

    for name in names:
        visit(name, ())
    return depth


def parse(reply: str, existing: list[dict[str, Any]]) -> dict[str, Any]:
    """The checked proposal: features in build order, each with a `layer`, plus warnings to show the person."""
    raw = _json(reply)["features"]
    existing_by_name = {f["name"].strip().lower(): f for f in existing}
    warnings: list[str] = []
    proposed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = _text(entry.get("name"), 80)
        key = name.lower()
        if not name:
            continue
        if key in existing_by_name:
            warnings.append(f"\"{name}\" already exists, so it isn't proposed again.")
            continue
        if key in seen:
            raise FeatureMapError(f"The model proposed \"{name}\" twice. Try again.")
        seen.add(key)
        proposed.append({"name": name, "summary": _text(entry.get("summary"), 500),
                         "stories": _strings(entry.get("stories"), MAX_STORIES, 400),
                         "paths": _strings(entry.get("paths"), 12, 200), "serves": _text(entry.get("serves"), 300),
                         "depends_on": _strings(entry.get("depends_on"), 10, 80)})
        if len(proposed) == MAX_FEATURES:
            break
    if not proposed:
        raise FeatureMapError("Every feature the model proposed already exists.")
    proposed_names = {f["name"].lower(): f["name"] for f in proposed}
    deps: dict[str, list[str]] = {}
    for feature in proposed:
        kept = []
        for dep in feature["depends_on"]:
            if dep.lower() in proposed_names and dep.lower() != feature["name"].lower():
                kept.append(proposed_names[dep.lower()])
            elif dep.lower() in existing_by_name:
                kept.append(existing_by_name[dep.lower()]["name"])
            else:
                warnings.append(f"\"{feature['name']}\" depended on \"{dep}\", which isn't a feature; that was dropped.")
        feature["depends_on"] = list(dict.fromkeys(kept))
        deps[feature["name"]] = feature["depends_on"]
    existing_layers = store.layers(existing) if existing else {}
    existing_depth = {f["name"]: existing_layers.get(f["id"], 0) for f in existing}
    depth = _layers([f["name"] for f in proposed], deps, existing_depth)
    for feature in proposed:
        feature["layer"] = depth[feature["name"]]
    claims = [(f["name"], p) for f in existing for p in f.get("paths") or []] + [(f["name"], p) for f in proposed for p in f["paths"]]
    flagged = set()
    for i, (a, pa) in enumerate(claims):
        for b, pb in claims[i + 1:]:
            if a != b and store._covers(pa, pb) and (a, b) not in flagged:
                flagged.add((a, b))
                warnings.append(f"\"{a}\" and \"{b}\" would both own {pa if pa == pb else f'{pa} and {pb}'}.")
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
        made = store.create(runtime, feature["name"], feature["summary"], feature["paths"],
                            [ids[d.lower()] for d in feature["depends_on"] if d.lower() in ids], feature["serves"],
                            stories=feature["stories"])
        created.append(made)
    return created
