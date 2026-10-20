"""Auto-fill for a feature's KPI: ask a model for the one metric that best says the feature is working.

`prompt` builds the request from the feature, the product requirements and the feature's jobs; `parse` turns the answer into
the fields the Add KPI form takes, checked by the same rules as a KPI typed by hand (analytics.clean_kpi). The suggestion is
shown for review: nothing is saved until the person adds it.
"""
from __future__ import annotations

import json
import re
from typing import Any

from orchestrator import analytics


class KpiSuggestError(ValueError):
    pass


REMINDER = ("\n\nYour last answer could not be used. Reply with ONLY one JSON object, no prose and no code fence, with the keys "
            "name, event, direction, target, unit and why.")


def prompt(feature: dict[str, Any], prd_context: str = "", job_titles: list[str] | None = None) -> str:
    existing = [f"- {k.get('name')} (event {k.get('event')})" for k in feature.get("kpis") or []]
    jobs = [f"- {t}" for t in (job_titles or [])[:12]]
    return "\n".join([
        "You choose the success metric for one feature of a software product.",
        "Pick exactly ONE metric: the single measurement that best tells whether this feature is working for the people who use it.",
        "Prefer a behaviour you can count (something a person did) over a vague quality, and a rate or a count over a feeling.",
        "",
        f"Feature: {feature.get('name', '')}",
        f"What it is: {feature.get('summary') or '(no summary)'}",
        *( ["Code it owns: " + ", ".join(feature["paths"][:8])] if feature.get("paths") else [] ),
        *( ["", "Jobs that built it:", *jobs] if jobs else [] ),
        *( ["", "KPIs it already has (do not repeat these):", *existing] if existing else [] ),
        *( ["", "Product requirements:", prd_context[:4000]] if prd_context.strip() else [] ),
        "",
        "Answer with ONLY one JSON object and nothing else:",
        '{"name": "short plain-language name, under 60 characters",',
        ' "event": "analytics event that records it, lowercase_snake_case, past tense, like lobby_seat_claimed",',
        ' "direction": "up if more is better, down if less is better",',
        ' "target": a realistic number to aim for, or null if you cannot say,',
        ' "unit": "short unit such as %, s or per user, or an empty string",',
        ' "why": "one sentence on why this is the best single measure"}',
    ])


def _object_from(reply: str) -> dict[str, Any]:
    text = reply.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise KpiSuggestError("The model didn't suggest a KPI.")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        raise KpiSuggestError("The model's answer wasn't readable.")
    if not isinstance(value, dict):
        raise KpiSuggestError("The model's answer wasn't a single KPI.")
    return value


def parse(reply: str, feature: dict[str, Any]) -> dict[str, Any]:
    """The suggested KPI as form fields {name, event, direction, target, unit, why}. Raises KpiSuggestError if it can't be used."""
    raw = _object_from(reply)
    try:
        kpi = analytics.clean_kpi({k: raw.get(k) for k in ("name", "event", "direction", "target", "unit")})
    except analytics.AnalyticsError as exc:
        raise KpiSuggestError(str(exc))
    if kpi["event"] in {k.get("event") for k in feature.get("kpis") or []}:
        raise KpiSuggestError("The suggested event is already used by another KPI.")
    return {"name": kpi["name"], "event": kpi["event"], "direction": kpi["direction"], "target": kpi["target"], "unit": kpi["unit"],
            "why": str(raw.get("why") or "").strip()[:300]}
