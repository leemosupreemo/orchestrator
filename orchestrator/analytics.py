"""Measure: KPIs per feature, the tracking plan they imply, and a learning log.

The loop this supports is build -> measure -> learn. A feature carries KPIs
(name, the event that measures it, a target). The KPIs become a tracking plan
that is handed to the planner so new work emits those events, and exported to
the repo. Results are logged by hand against a KPI with a decision (keep,
iterate, drop), which is the "learn" step; the provider connection is verified
by sending one test event to the provider's public ingestion endpoint. Reading
numbers back out of a provider needs per-provider API credentials and is not
done here.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse

EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,59}$")
DIRECTIONS = ("up", "down")
DECISIONS = ("", "keep", "iterate", "drop")
MAX_MEASUREMENTS = 200
TEST_EVENT = "orchestrator_test"


class AnalyticsError(ValueError):
    pass


PROVIDERS: dict[str, dict[str, Any]] = {
    "mixpanel": {"name": "Mixpanel", "key_label": "Project token", "hosts": {"us": "https://api.mixpanel.com", "eu": "https://api-eu.mixpanel.com"},
                 "docs": "https://docs.mixpanel.com/docs/tracking-methods/sdks"},
    "amplitude": {"name": "Amplitude", "key_label": "API key", "hosts": {"us": "https://api2.amplitude.com", "eu": "https://api.eu.amplitude.com"},
                  "docs": "https://amplitude.com/docs/sdks"},
    "posthog": {"name": "PostHog", "key_label": "Project API key", "hosts": {"us": "https://us.i.posthog.com", "eu": "https://eu.i.posthog.com"},
                "docs": "https://posthog.com/docs/libraries"},
}


def public_providers() -> list[dict[str, Any]]:
    return [{"id": k, "name": v["name"], "key_label": v["key_label"], "regions": list(v["hosts"]), "docs": v["docs"]} for k, v in PROVIDERS.items()]


def event_name(raw: str) -> str:
    name = (raw or "").strip()
    if not EVENT_RE.match(name):
        raise AnalyticsError("Event names use lowercase letters, numbers and underscores, like lobby_seat_claimed")
    return name


def clean_kpi(raw: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(raw.get("name", (existing or {}).get("name", ""))).strip()
    if not name or len(name) > 80:
        raise AnalyticsError("A KPI needs a name under 80 characters")
    direction = raw.get("direction", (existing or {}).get("direction") or "up")
    if direction not in DIRECTIONS:
        raise AnalyticsError("Direction must be up or down")
    target = raw.get("target", (existing or {}).get("target"))
    if target in ("", None):
        target = None
    else:
        try:
            target = float(target)
        except (TypeError, ValueError):
            raise AnalyticsError("The target must be a number")
    return {"id": (existing or {}).get("id") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "kpi",
            "name": name, "event": event_name(str(raw.get("event", (existing or {}).get("event", "")))),
            "unit": str(raw.get("unit", (existing or {}).get("unit", ""))).strip()[:20],
            "direction": direction, "target": target, "measurements": (existing or {}).get("measurements", [])}


def add_kpi(feature: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    kpi = clean_kpi(raw)
    kpis = feature.setdefault("kpis", [])
    taken = {k["id"] for k in kpis}
    base, n = kpi["id"], 2
    while kpi["id"] in taken:
        kpi["id"], n = f"{base}-{n}", n + 1
    kpis.append(kpi)
    return kpi


def find_kpi(feature: dict[str, Any], kpi_id: str) -> dict[str, Any]:
    for k in feature.get("kpis", []):
        if k["id"] == kpi_id:
            return k
    raise AnalyticsError("KPI not found")


def log_measurement(kpi: dict[str, Any], value: Any, note: str = "", decision: str = "", now: float | None = None) -> dict[str, Any]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AnalyticsError("The result must be a number")
    if decision not in DECISIONS:
        raise AnalyticsError("Decision must be keep, iterate or drop")
    entry = {"t": now if now is not None else time.time(), "value": number, "note": (note or "").strip()[:500], "decision": decision}
    kpi["measurements"] = (kpi.get("measurements", []) + [entry])[-MAX_MEASUREMENTS:]
    return entry


def status(kpi: dict[str, Any]) -> dict[str, Any]:
    """latest result vs target: no-data | no-target | on-track | behind."""
    ms = kpi.get("measurements") or []
    latest = ms[-1] if ms else None
    target, direction = kpi.get("target"), kpi.get("direction", "up")
    if latest is None:
        state = "no-data"
    elif target is None:
        state = "no-target"
    else:
        met = latest["value"] >= target if direction == "up" else latest["value"] <= target
        state = "on-track" if met else "behind"
    trend = None
    if len(ms) >= 2:
        delta = ms[-1]["value"] - ms[-2]["value"]
        better = delta > 0 if direction == "up" else delta < 0
        trend = "flat" if delta == 0 else ("better" if better else "worse")
    return {"state": state, "latest": latest, "trend": trend}


def tracking_plan(features: list[dict[str, Any]]) -> str:
    """Markdown listing every event the KPIs rely on, grouped by feature."""
    lines = ["# Tracking plan", "", "Events the product must emit so each feature's KPIs can be measured. Generated by Orchestrator (Measure).", ""]
    any_kpi = False
    for f in features:
        kpis = f.get("kpis") or []
        if not kpis:
            continue
        any_kpi = True
        lines += [f"## {f['name']}", ""]
        lines += ["| KPI | Event | Target |", "| --- | --- | --- |"]
        for k in kpis:
            goal = "—" if k.get("target") is None else f"{'at least' if k['direction'] == 'up' else 'at most'} {k['target']:g}{(' ' + k['unit']) if k.get('unit') else ''}"
            lines.append(f"| {k['name']} | `{k['event']}` | {goal} |")
        lines.append("")
    if not any_kpi:
        lines.append("No KPIs defined yet.")
    return "\n".join(lines).rstrip() + "\n"


def instrumentation_context(feature: dict[str, Any], provider_name: str | None) -> str:
    """Planner text: what this feature must track."""
    kpis = feature.get("kpis") or []
    if not kpis:
        return ""
    where = f" through the project's {provider_name} integration" if provider_name else " through the project's existing analytics layer"
    lines = ["", "", "## Measurement", f"This feature is judged by these KPIs. Emit each event{where} (reuse the existing wrapper; don't add a second SDK), and add a test case that checks each event is sent:"]
    for k in kpis:
        goal = "" if k.get("target") is None else f" (target: {'at least' if k['direction'] == 'up' else 'at most'} {k['target']:g}{(' ' + k['unit']) if k.get('unit') else ''})"
        lines.append(f"- `{k['event']}` for \"{k['name']}\"{goal}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- provider connection test


def valid_host(url: str) -> bool:
    p = urlparse(url or "")
    return p.scheme == "https" and bool(p.hostname) and not p.username


def test_request(provider: str, key: str, host: str, now: float) -> tuple[str, dict[str, Any]]:
    host = host.rstrip("/")
    if provider == "mixpanel":
        return f"{host}/track?verbose=1", [{"event": TEST_EVENT, "properties": {"token": key, "distinct_id": "orchestrator", "time": int(now)}}]
    if provider == "amplitude":
        return f"{host}/2/httpapi", {"api_key": key, "events": [{"user_id": "orchestrator", "event_type": TEST_EVENT, "time": int(now * 1000)}]}
    if provider == "posthog":
        return f"{host}/capture/", {"api_key": key, "event": TEST_EVENT, "distinct_id": "orchestrator"}
    raise AnalyticsError("Unknown provider")


def send_test(provider: str, key: str, host: str, opener: Callable[..., Any] = urllib.request.urlopen, now: float | None = None) -> None:
    if provider not in PROVIDERS:
        raise AnalyticsError("Unknown provider")
    if not key:
        raise AnalyticsError("Save a key first")
    if not valid_host(host):
        raise AnalyticsError("The host must be an https:// address")
    url, body = test_request(provider, key, host, now if now is not None else time.time())
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        with opener(request, timeout=10) as response:
            raw = response.read().decode("utf-8", "replace") if hasattr(response, "read") else ""
            code = response.status
    except urllib.error.HTTPError as exc:
        raise AnalyticsError(f"{PROVIDERS[provider]['name']} rejected the key ({exc.code})") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AnalyticsError(f"Couldn't reach {PROVIDERS[provider]['name']}: {getattr(exc, 'reason', exc)}") from exc
    if not 200 <= code < 300:
        raise AnalyticsError(f"{PROVIDERS[provider]['name']} answered {code}")
    if provider == "mixpanel":
        try:
            ok = json.loads(raw).get("status") == 1
        except (json.JSONDecodeError, AttributeError):
            ok = False
        if not ok:
            raise AnalyticsError("Mixpanel didn't accept the token. Check it and the region.")
