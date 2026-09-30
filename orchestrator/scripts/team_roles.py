from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any


FOUNDATION_ROLE_IDS = ("technical_lead", "implementation_engineer", "qa_engineer")
MAX_AUTOMATIC_ROLES = 7

ROLE_CATALOG: dict[str, dict[str, Any]] = {
    "technical_lead": {
        "label": "Technical Lead",
        "responsibility": "Own architectural coherence, resolve conflicts, and preserve the user goal.",
        "strong_signals": (), "weak_signals": (),
    },
    "implementation_engineer": {
        "label": "Implementation Engineer",
        "responsibility": "Implement the approved change and its tests.",
        "strong_signals": (), "weak_signals": (),
    },
    "qa_engineer": {
        "label": "QA Engineer",
        "responsibility": "Independently verify behavior against the original goal and acceptance criteria.",
        "strong_signals": (), "weak_signals": (),
    },
    "ux_designer": {
        "label": "UX Designer",
        "responsibility": "Protect interaction clarity, user journeys, states, and usability.",
        "strong_signals": ("ux", "user flow", "onboarding", "screen flow"),
        "weak_signals": ("workflow", "interaction", "navigation"),
    },
    "frontend_engineer": {
        "label": "Frontend Engineer",
        "responsibility": "Own presentation-layer behavior, components, state, and visual implementation.",
        "strong_signals": ("ui", "frontend", "swiftui", "react", "css"),
        "weak_signals": ("view", "screen", "component", "layout"),
    },
    "backend_engineer": {
        "label": "Backend Engineer",
        "responsibility": "Own service boundaries, APIs, server behavior, and integration contracts.",
        "strong_signals": ("backend", "api", "endpoint", "server", "webhook"),
        "weak_signals": ("service", "request", "response"),
    },
    "database_specialist": {
        "label": "Database Specialist",
        "responsibility": "Own data modeling, persistence, migrations, integrity, and query behavior.",
        "strong_signals": ("database", "migration", "schema", "sql", "sqlite", "postgres", "persistence"),
        "weak_signals": ("persist", "query"),
    },
    "security_engineer": {
        "label": "Security Engineer",
        "responsibility": "Review authentication, authorization, sensitive data, and abuse boundaries.",
        "strong_signals": ("security", "auth", "authentication", "authorization", "token", "secret", "encrypt"),
        "weak_signals": ("permission", "sensitive"),
    },
    "accessibility_specialist": {
        "label": "Accessibility Specialist",
        "responsibility": "Verify accessible semantics, navigation, contrast, and assistive-technology behavior.",
        "strong_signals": ("accessibility", "accessible", "voiceover", "screen reader", "a11y"),
        "weak_signals": ("contrast", "keyboard navigation"),
    },
    "performance_engineer": {
        "label": "Performance Engineer",
        "responsibility": "Measure and protect latency, throughput, memory, startup, and resource usage.",
        "strong_signals": ("performance", "latency", "throughput", "benchmark"),
        "weak_signals": ("slow", "memory", "optimize", "startup"),
    },
    "devops_engineer": {
        "label": "DevOps Engineer",
        "responsibility": "Own build, CI, deployment, infrastructure, and operational reliability.",
        "strong_signals": ("ci", "deploy", "deployment", "docker", "kubernetes", "infrastructure"),
        "weak_signals": ("pipeline", "release"),
    },
    "platform_specialist": {
        "label": "Platform Specialist",
        "responsibility": "Protect platform conventions, lifecycle behavior, compatibility, and native tooling.",
        "strong_signals": ("ios", "swiftui", "xcode", "android", "kotlin", "macos", "watchos"),
        "weak_signals": ("platform",),
    },
}


def available_role_ids() -> list[str]:
    return list(ROLE_CATALOG)


def _role_record(role_id: str, *, pinned: bool, reason: str) -> dict[str, Any]:
    definition = ROLE_CATALOG[role_id]
    return {
        "id": role_id,
        "label": definition["label"],
        "responsibility": definition["responsibility"],
        "required": role_id in FOUNDATION_ROLE_IDS,
        "pinned": pinned,
        "reason": reason,
    }


def _foundation_roles() -> list[dict[str, Any]]:
    reasons = {
        "technical_lead": "Foundation role for architectural coherence and user-goal alignment.",
        "implementation_engineer": "Foundation role for implementation ownership.",
        "qa_engineer": "Foundation role for independent verification.",
    }
    return [_role_record(role_id, pinned=False, reason=reasons[role_id]) for role_id in FOUNDATION_ROLE_IDS]


def _matches(text: str, signals: tuple[str, ...]) -> list[str]:
    normalized = text.casefold()
    return [
        signal
        for signal in signals
        if re.search(rf"(?<!\w){re.escape(signal)}(?!\w)", normalized)
    ]


def _automatic_candidates(text: str) -> list[tuple[int, int, str, list[str]]]:
    candidates: list[tuple[int, int, str, list[str]]] = []
    for order, role_id in enumerate(ROLE_CATALOG):
        if role_id in FOUNDATION_ROLE_IDS:
            continue
        definition = ROLE_CATALOG[role_id]
        strong = _matches(text, definition["strong_signals"])
        weak = _matches(text, definition["weak_signals"])
        if strong or len(weak) >= 2:
            matches = strong + weak
            candidates.append((-(len(strong) * 3 + len(weak)), order, role_id, matches))
    return sorted(candidates)


def _selection_reason(matches: list[str]) -> str:
    return f"Selected from job evidence: {', '.join(matches[:3])}."


def assemble_team(text: str, job_type: str, requested_roles: list[str] | None = None) -> dict:
    del job_type
    roles = _foundation_roles()
    handoffs: list[dict[str, Any]] = []

    if requested_roles is not None:
        requested = list(dict.fromkeys(
            role_id for role_id in requested_roles
            if role_id in ROLE_CATALOG and role_id not in FOUNDATION_ROLE_IDS
        ))
        seen = set(FOUNDATION_ROLE_IDS)
        remaining = max(0, MAX_AUTOMATIC_ROLES - len(roles) - len(requested))
        candidates = [candidate for candidate in _automatic_candidates(text) if candidate[2] not in requested]
        for _score, _order, role_id, matches in candidates[:remaining]:
            seen.add(role_id)
            roles.append(_role_record(role_id, pinned=False, reason=_selection_reason(matches)))
        for role_id in requested:
            if role_id in seen:
                for role in roles:
                    if role["id"] == role_id:
                        role["pinned"] = True
                        role["reason"] = "Pinned by the user; also supported by job evidence."
                        break
                continue
            seen.add(role_id)
            roles.append(_role_record(role_id, pinned=True, reason="Pinned by the user."))
        return {"mode": "custom", "auto_adjust": True, "selection_text": text, "roles": roles, "handoffs": handoffs}

    remaining = MAX_AUTOMATIC_ROLES - len(roles)
    for _score, _order, role_id, matches in _automatic_candidates(text)[:remaining]:
        roles.append(_role_record(role_id, pinned=False, reason=_selection_reason(matches)))
    return {"mode": "auto", "auto_adjust": True, "selection_text": text, "roles": roles, "handoffs": handoffs}


def _plan_text(plan: dict) -> str:
    parts: list[str] = []
    for field in ("title", "summary", "expected_behavior"):
        value = plan.get(field)
        if isinstance(value, str):
            parts.append(value)
    for field in ("risks", "constraints", "likely_files", "acceptance_criteria"):
        value = plan.get(field, [])
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
    for task in plan.get("tasks", []):
        if isinstance(task, dict):
            parts.extend(str(task.get(field, "")) for field in ("title", "description"))
            for field in ("likely_files", "acceptance_criteria", "tests"):
                parts.extend(str(item) for item in task.get(field, []))
    return "\n".join(parts)


def _handoff(
    handoff_type: str,
    roles: list[str],
    finding: str,
    evidence: str,
    impact: str,
    action_required: str,
    remaining_risk: str,
) -> dict[str, Any]:
    return {
        "type": handoff_type,
        "roles": roles,
        "finding": finding,
        "evidence": evidence,
        "impact": impact,
        "action_required": action_required,
        "remaining_risk": remaining_risk,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def refine_team(manifest: dict, plan: dict) -> dict:
    refined = deepcopy(manifest)
    if not refined.get("auto_adjust", refined.get("mode") == "auto"):
        return refined

    previous_roles = refined.get("roles", [])
    previous_ids = {role.get("id") for role in previous_roles}
    pinned = [role for role in previous_roles if role.get("pinned") and role.get("id") not in FOUNDATION_ROLE_IDS]
    pinned_ids = {role["id"] for role in pinned}
    evidence_text = "\n".join((str(refined.get("selection_text", "")), _plan_text(plan)))
    available_slots = max(0, MAX_AUTOMATIC_ROLES - len(FOUNDATION_ROLE_IDS) - len(pinned))
    automatic: list[dict[str, Any]] = []
    for _score, _order, role_id, matches in _automatic_candidates(evidence_text):
        if role_id in pinned_ids or len(automatic) >= available_slots:
            continue
        automatic.append(_role_record(role_id, pinned=False, reason=_selection_reason(matches)))
    refined["roles"] = _foundation_roles() + automatic + pinned
    current_ids = {role["id"] for role in refined["roles"]}
    changed_ids = sorted(previous_ids.symmetric_difference(current_ids))
    if changed_ids:
        refined.setdefault("handoffs", []).append(_handoff(
            "team_adjusted",
            changed_ids,
            "Planning evidence changed the recommended specialist coverage.",
            ", ".join(role["label"] for role in refined["roles"] if role["id"] in changed_ids),
            "The best-supported compact roster applies to subsequent handoffs.",
            "Use the adjusted specialists for implementation and review.",
            "Automatic role inference may still require user correction.",
        ))
    return refined


def _string_list(plan: dict, field: str) -> list[str]:
    value = plan.get(field, [])
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str) and value:
        return [value]
    return []


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def build_intent_brief(raw_input: str, plan: dict, existing_brief: dict | None = None) -> dict:
    existing = existing_brief or {}
    task_criteria: list[str] = []
    for task in plan.get("tasks", []):
        if isinstance(task, dict):
            task_criteria.extend(_string_list(task, "acceptance_criteria"))
    acceptance_criteria = _dedupe(_string_list(plan, "acceptance_criteria") + task_criteria)
    return {
        "user_request": existing.get("user_request") or raw_input.strip(),
        "current_request": raw_input.strip(),
        "user_outcome": str(plan.get("summary") or plan.get("title") or existing.get("user_outcome") or raw_input).strip(),
        "acceptance_criteria": acceptance_criteria or _string_list(existing, "acceptance_criteria"),
        "constraints": _dedupe(_string_list(existing, "constraints") + _string_list(plan, "constraints")),
        "assumptions": _string_list(plan, "assumptions") or _string_list(existing, "assumptions"),
        "risks": _string_list(plan, "risks") or _string_list(existing, "risks"),
        "non_goals": _string_list(plan, "non_goals") or _string_list(existing, "non_goals"),
    }


def format_team_context(team: dict) -> str:
    if not team or not team.get("roles"):
        return ""
    brief = team.get("intent_brief", {})
    lines = [
        "### ROLE-BASED TEAM CONTEXT",
        "All roles share this user-goal anchor. Do not optimize an intermediate artifact at its expense.",
        f"User request: {brief.get('user_request', 'Not recorded')}",
        f"User outcome: {brief.get('user_outcome', 'Not recorded')}",
        "Acceptance criteria:",
        *[f"- {item}" for item in brief.get("acceptance_criteria", [])],
        "Constraints:",
        *[f"- {item}" for item in brief.get("constraints", [])],
        "Assumptions:",
        *[f"- {item}" for item in brief.get("assumptions", [])],
        "Risks:",
        *[f"- {item}" for item in brief.get("risks", [])],
        "Non-goals:",
        *[f"- {item}" for item in brief.get("non_goals", [])],
        "Active engineering roles:",
    ]
    for role in team["roles"]:
        lines.append(f"- {role['label']}: {role['responsibility']} Reason: {role['reason']}")
    lines.extend(
        [
            "Structured handoffs must state:",
            "- Finding or decision",
            "- Evidence",
            "- Impact on the user goal",
            "- Action required from the next role",
            "- Remaining risk or disagreement",
            "The Technical Lead resolves overlap; QA validates against the Intent Brief independently.",
        ]
    )
    return "\n".join(lines)


def replace_team_roles(team: dict, role_ids: list[str]) -> dict:
    changed = deepcopy(team)
    replacement = assemble_team("", "feature", requested_roles=role_ids)
    changed["mode"] = "custom"
    changed["auto_adjust"] = False
    changed["roles"] = replacement["roles"]
    changed.setdefault("handoffs", []).append(
        _handoff(
            "team_changed",
            [role["id"] for role in changed["roles"]],
            "The user changed the role-based team.",
            "Explicit roster selection in the job console.",
            "The revised roster applies to subsequent workflow handoffs.",
            "Route future work through the selected roster.",
            "Manually excluding a specialist may leave a coverage gap.",
        )
    )
    changed["updated_at"] = datetime.now(timezone.utc).isoformat()
    return changed
