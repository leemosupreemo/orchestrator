"""Draft a product's feature map from its product requirements: epic-level features with their requirements (and user
stories where people use them), the technique that fits (a story map, a work breakdown or a short list), how much detail
to give, what each builds on, and the order to build them in.

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
MAX_REQUIREMENTS = 6
TECHNIQUES = {"story_map": "Story map", "wbs": "Work breakdown", "list": "Short list"}

# How much to write, by how much the person has told us. A thin brief gets a few big epics and questions, not invented detail.
DETAIL = {
    "sparse": {"epics": 6, "requirements": 2, "stories": 0,
               "words": "The requirements are brief. Propose only a few epics (3 to 6), each a name and one sentence. Add a requirement only if the text states it. "
                        "No user stories, and no code paths. List the questions whose answers would let you break it down further."},
    "moderate": {"epics": 10, "requirements": 4, "stories": 3,
                 "words": "The requirements have a fair amount of detail. Propose about 5 to 10 epics, each with a sentence, 2 to 4 requirements taken from the text, "
                          "and up to 3 user stories where a person uses it. Give code paths only where the project layout shows them."},
    "detailed": {"epics": 15, "requirements": 6, "stories": 8,
                 "words": "The requirements are detailed. Break the product down fully: up to 15 epics, each with 3 to 6 testable requirements drawn from the text "
                          "(keep the document's numbers and rules), user stories where a person uses it, and code paths where they are clear."},
}


class FeatureMapError(ValueError):
    pass


def detail_level(prd_text: str, extra_chars: int = 0) -> str:
    """"sparse", "moderate" or "detailed": how much the requirements (and any attached source document) actually say."""
    parts = prd.split(prd_text or "")
    bodies = [b for b in parts.values() if prd.is_filled(b)]
    chars = sum(len(b) for b in bodies)
    lines = [l for l in parts.get("features", "").splitlines() if l.strip() and not prd._GUIDANCE_LINE.fullmatch(l)]
    if extra_chars >= 3000 or chars >= 2500 or len(lines) >= 9:
        return "detailed"
    if chars >= 700 or len(lines) >= 4 or len(bodies) >= 4:
        return "moderate"
    return "sparse"


def prompt(prd_text: str, existing: list[dict[str, Any]], layout: str, level: str = "moderate", source_text: str = "", technique: str = "") -> str:
    have = "\n".join(f"- {f['name']}: {f.get('summary') or ''} (owns {', '.join(f.get('paths') or []) or 'no paths yet'})"
                     for f in existing) or "(none yet)"
    detail = DETAIL.get(level, DETAIL["moderate"])
    chosen = (f'The person chose the technique, so use "{technique}" ({TECHNIQUES[technique]}) and name it. Its meaning:'
              if technique in TECHNIQUES else "Choose the planning technique that fits what the requirements say, and name it:")
    attached = f"\n## Attached source document (the detail behind the requirements)\n{source_text.strip()[:30000]}\n" if source_text.strip() else ""
    return f"""You are planning a product's epic-level features from its product requirements, for the person to approve or pick from.

{chosen}
- "story_map": people use the product through flows (an app, site, game, tool). Make each main thing a person does an epic, in the order they do it, with the stories under it. Put each epic in a release slice: "MVP" (the smallest thing that works end to end), "Next" or "Later".
- "wbs": the work is mostly technical or deliverable-based (an API, library, command-line tool, data pipeline, infrastructure), or the requirements are organised by component. Break it into deliverables by component, each with its requirements. Use stories only where a person interacts with it. No slices.
- "list": the requirements are too thin for either. A short list of epics only, and the questions that would let you break it down.

{detail['words']}
Match the document's own words and level of detail. Do not invent facts, users or numbers it doesn't give.

Each epic should be something that can be built and shipped on its own: small enough for one or a few jobs, large enough to matter to a user.
Order matters: an epic should depend only on what it truly needs, so independent ones can be built in parallel. Two epics must not own the same code.
Do not repeat a feature that already exists; you may depend on one.

Reply with ONLY this JSON, no commentary:
{{"technique": "story_map" | "wbs" | "list", "rationale": "one plain sentence on why this technique fits",
  "open_questions": ["what you would need to know to break it down further"],
  "features": [{{"name": "...", "summary": "one sentence", "requirements": ["a testable statement"], "stories": ["As a ..., I can ..., so that ..."],
  "slice": "MVP" | "Next" | "Later" (story_map only), "paths": ["app/feature/"], "depends_on": ["name of another feature"],
  "serves": "which core feature or use case from the requirements"}}]}}

## Product requirements
{prd_text.strip()[:20000]}
{attached}
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


def parse(reply: str, existing: list[dict[str, Any]], level: str | None = None, technique: str = "") -> dict[str, Any]:
    """A model's reply, checked (see `validate`), with the technique it chose, why, and what it still needs to know."""
    try:
        data = prd.json_reply(reply)
    except prd.PrdError:
        data = {}
    proposal = validate(_features_from(reply), existing, level)
    technique = technique if technique in TECHNIQUES else data.get("technique") if data.get("technique") in TECHNIQUES else "list"
    if technique != "story_map":  # release slices belong to a story map
        for feature in proposal["features"]:
            feature["slice"] = ""
    proposal.update({"technique": technique, "technique_name": TECHNIQUES[technique], "rationale": _text(data.get("rationale"), 300),
                     "open_questions": _strings(data.get("open_questions"), 6, 240), "detail": level or ""})
    return proposal


def validate(raw: list[Any], existing: list[dict[str, Any]], level: str | None = None) -> dict[str, Any]:
    """Proposed features, checked and in build order (each with a `layer`), plus warnings to show the person.
    Used on the model's reply and again on what the page sends back to accept. `level` caps how much is kept."""
    detail = DETAIL.get(level or "detailed", DETAIL["detailed"])
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
                         "requirements": store.clean_requirements(entry.get("requirements"), detail["requirements"]),
                         "stories": store.clean_stories(entry.get("stories"), detail["stories"]),
                         "slice": store.clean_slice(entry.get("slice")),
                         "paths": _strings(entry.get("paths"), 12, 200), "serves": _text(entry.get("serves"), 300),
                         "depends_on": _strings(entry.get("depends_on"), 10, 80)})
        if len(proposed) == min(MAX_FEATURES, detail["epics"]):
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
                                    stories=feature["stories"], requirements=feature.get("requirements"), slice=feature.get("slice", "")))
    return created
