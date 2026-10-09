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
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
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
                 "docs": "https://docs.mixpanel.com/docs/tracking-methods/sdks",
                 "dashboards": {"us": "https://mixpanel.com/project", "eu": "https://eu.mixpanel.com/project"}},
    "amplitude": {"name": "Amplitude", "key_label": "API key", "hosts": {"us": "https://api2.amplitude.com", "eu": "https://api.eu.amplitude.com"},
                  "docs": "https://amplitude.com/docs/sdks",
                  "dashboards": {"us": "https://analytics.amplitude.com", "eu": "https://analytics.eu.amplitude.com"}},
    "posthog": {"name": "PostHog", "key_label": "Project API key", "hosts": {"us": "https://us.i.posthog.com", "eu": "https://eu.i.posthog.com"},
                "docs": "https://posthog.com/docs/libraries",
                "dashboards": {"us": "https://us.posthog.com", "eu": "https://eu.posthog.com"}},
}


def public_providers() -> list[dict[str, Any]]:
    return [{"id": k, "name": v["name"], "key_label": v["key_label"], "regions": list(v["hosts"]), "docs": v["docs"],
             "dashboards": v.get("dashboards", {})} for k, v in PROVIDERS.items()]


def probe_provider_mcp(provider_id: str) -> list[dict[str, Any]]:
    home = Path.home()
    results: list[dict[str, Any]] = []
    pid = provider_id.lower()
    pkg = f"{pid}-mcp"

    # Google (Antigravity CLI / Gemini)
    agy_dir = home / ".gemini" / "antigravity-cli" / "mcp" / pid
    gemini_settings = home / ".gemini" / "settings.json"
    google_installed = agy_dir.is_dir()
    detected_via = f"~/.gemini/antigravity-cli/mcp/{pid}" if google_installed else None
    if not google_installed and gemini_settings.exists():
        try:
            if pid in gemini_settings.read_text(encoding="utf-8").lower():
                google_installed = True
                detected_via = "~/.gemini/settings.json"
        except Exception:
            pass
    results.append({
        "id": "google",
        "org": "Google (Antigravity / Gemini)",
        "cli": "agy",
        "installed": google_installed,
        "detected_via": detected_via,
        "cmd": f"agy mcp add {pid} -- npx -y {pkg}",
    })

    # Anthropic (Claude Code)
    claude_installed = False
    detected_claude = None
    claude_paths = [
        home / ".claude.json",
        home / ".claude" / "claude_desktop_config.json",
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
    ]
    for cp in claude_paths:
        if cp.exists():
            try:
                cdata = json.loads(cp.read_text(encoding="utf-8"))
                servers = cdata.get("mcpServers", {}) or {}
                if pid in servers or any(pid in str(k).lower() for k in servers):
                    claude_installed = True
                    detected_claude = f"~/{cp.name}"
                    break
            except Exception:
                pass
    results.append({
        "id": "anthropic",
        "org": "Anthropic (Claude Code)",
        "cli": "claude",
        "installed": claude_installed,
        "detected_via": detected_claude,
        "cmd": f"claude mcp add {pid} -- npx -y {pkg}",
    })

    # OpenAI (Codex)
    codex_cfg = home / ".codex" / "config.toml"
    codex_installed = False
    detected_codex = None
    if codex_cfg.exists():
        try:
            if pid in codex_cfg.read_text(encoding="utf-8").lower():
                codex_installed = True
                detected_codex = "~/.codex/config.toml"
        except Exception:
            pass
    results.append({
        "id": "openai",
        "org": "OpenAI (Codex)",
        "cli": "codex",
        "installed": codex_installed,
        "detected_via": detected_codex,
        "cmd": f"codex mcp add {pid} -- npx -y {pkg}",
    })

    # OpenCode
    opencode_cfg = home / ".opencode" / "mcp.json"
    opencode_installed = False
    detected_opencode = None
    if opencode_cfg.exists():
        try:
            if pid in opencode_cfg.read_text(encoding="utf-8").lower():
                opencode_installed = True
                detected_opencode = "~/.opencode/mcp.json"
        except Exception:
            pass
    results.append({
        "id": "opencode",
        "org": "OpenCode",
        "cli": "opencode",
        "installed": opencode_installed,
        "detected_via": detected_opencode,
        "cmd": f"opencode mcp add {pid} -- npx -y {pkg}",
    })

    # Ollama
    results.append({
        "id": "ollama",
        "org": "Ollama (Local models)",
        "cli": "ollama",
        "installed": False,
        "detected_via": None,
        "cmd": f"npx -y {pkg}",
    })

    return results


def probe_mixpanel_mcp() -> list[dict[str, Any]]:
    return probe_provider_mcp("mixpanel")


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


# ---------------------------------------------------------------------------- telemetry tracking


def format_event_payload(provider: str, key: str, host: str, event: str, properties: dict[str, Any], now: float) -> tuple[str, Any]:
    host = host.rstrip("/")
    props = dict(properties)
    props.setdefault("time", int(now))
    props.setdefault("distinct_id", os.environ.get("USER", "orchestrator"))
    if provider == "mixpanel":
        return f"{host}/track?verbose=1", [{"event": event, "properties": {"token": key, **props}}]
    if provider == "amplitude":
        return f"{host}/2/httpapi", {"api_key": key, "events": [{"user_id": props.get("distinct_id", "orchestrator"), "event_type": event, "time": int(now * 1000), "event_properties": props}]}
    if provider == "posthog":
        return f"{host}/capture/", {"api_key": key, "event": event, "distinct_id": props.get("distinct_id", "orchestrator"), "properties": props}
    raise AnalyticsError("Unknown provider")


def _send_payload(url: str, payload: Any, opener: Callable[..., Any]) -> bool:
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with opener(req, timeout=5) as resp:
            return bool(resp and getattr(resp, "status", 200) < 400)
    except Exception:
        return False


def track_event(event: str, properties: dict[str, Any] | None = None, runtime_root: Path | None = None,
                async_send: bool = True, opener: Callable[..., Any] = urllib.request.urlopen, now: float | None = None) -> bool:
    """Non-blocking telemetry dispatch to the configured analytics provider (e.g. Mixpanel)."""
    try:
        if runtime_root is None:
            from orchestrator.project_config import find_project_root, runtime_dir
            root = find_project_root()
            runtime_root = runtime_dir(root) if root else Path(".orchestrator")
        settings_file = runtime_root / "config" / "settings.json"
        if not settings_file.is_file():
            return False
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
        provider = settings.get("analytics_provider")
        key = settings.get("analytics_key")
        if not provider or not key or provider not in PROVIDERS:
            return False
        region = settings.get("analytics_region", "us")
        host = PROVIDERS[provider]["hosts"].get(region, "")
        if not valid_host(host):
            return False
        url, payload = format_event_payload(provider, key, host, event, properties or {}, now if now is not None else time.time())
        if async_send:
            threading.Thread(target=_send_payload, args=(url, payload, opener), daemon=True).start()
            return True
        return _send_payload(url, payload, opener)
    except Exception:
        return False


def track_session(distinct_id: str | None = None, source: str = "web", runtime_root: Path | None = None, **kwargs: Any) -> bool:
    """Track session start (for calculating signin rate & bounce rate)."""
    props: dict[str, Any] = {"source": source}
    if distinct_id:
        props["distinct_id"] = distinct_id
    return track_event("session_started", props, runtime_root=runtime_root, **kwargs)


def track_signin(method: str = "token", distinct_id: str | None = None, runtime_root: Path | None = None, **kwargs: Any) -> bool:
    """Track successful sign-in."""
    props: dict[str, Any] = {"method": method}
    if distinct_id:
        props["distinct_id"] = distinct_id
    return track_event("user_signed_in", props, runtime_root=runtime_root, **kwargs)


def track_wizard(must_do_total: int, must_do_completed: int, optional_total: int, optional_completed: int,
                 optional_items: list[str] | None = None, machines_count: int = 1,
                 llms_count: int = 1, llms_list: list[str] | None = None,
                 duration_s: float | None = None, mode: str = "cli",
                 distinct_id: str | None = None, runtime_root: Path | None = None, **kwargs: Any) -> bool:
    """Track wizard setup completion with must-do vs optional separation, machine/LLM counts."""
    props: dict[str, Any] = {
        "mode": mode,
        "must_do_total": must_do_total,
        "must_do_completed": must_do_completed,
        "must_do_all_passed": must_do_completed >= must_do_total,
        "optional_total": optional_total,
        "optional_completed": optional_completed,
        "optional_items": optional_items or [],
        "machines_count": machines_count,
        "llms_count": llms_count,
        "llms_list": llms_list or [],
    }
    if duration_s is not None:
        props["setup_duration_s"] = round(duration_s, 2)
    if distinct_id:
        props["distinct_id"] = distinct_id
    return track_event("wizard_completed", props, runtime_root=runtime_root, **kwargs)


def track_job_run(job_id: str, job_type: str = "feature-plan",
                  status: str = "started", duration_s: float | None = None,
                  planner_model: str | None = None, builder_model: str | None = None,
                  reviewer_model: str | None = None, job_seq: int = 1,
                  failure_reason: str | None = None, distinct_id: str | None = None,
                  runtime_root: Path | None = None, **kwargs: Any) -> bool:
    """Track job start and completion with models, sequence, success vs failure, and job type."""
    event = "job_started" if status == "started" else "job_completed"
    props: dict[str, Any] = {
        "job_id": job_id,
        "job_type": job_type,
        "job_sequence_number": job_seq,
        "is_first_job": job_seq == 1,
        "has_run_multiple_jobs": job_seq >= 2,
    }
    if planner_model:
        props["planner_model"] = planner_model
    if builder_model:
        props["builder_model"] = builder_model
    if reviewer_model:
        props["reviewer_model"] = reviewer_model
    if status != "started":
        props["status"] = status  # e.g. "succeeded" or "failed"
    if duration_s is not None:
        props["duration_seconds"] = round(duration_s, 2)
    if failure_reason:
        props["failure_reason"] = failure_reason
    if distinct_id:
        props["distinct_id"] = distinct_id
    return track_event(event, props, runtime_root=runtime_root, **kwargs)



