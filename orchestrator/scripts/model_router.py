#!/usr/bin/env python3
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any
from model_availability import is_available
from model_registry import get_all_models, ModelMetadata, ModelTier, ModelCapability, get_model

class ModelRole:
    PLANNER = "planner"
    BUILDER = "builder"
    REVIEWER = "reviewer"
    DEBUGGER = "debugger"
    VERIFIER = "verifier"
    ARCHITECT = "architect"

ROLE_REQUIREMENTS = {
    ModelRole.PLANNER: {
        "min_tier": ModelTier.HIGH,
        "preferred_capabilities": [ModelCapability.REASONING, ModelCapability.CONTEXT]
    },
    ModelRole.BUILDER: {
        "min_tier": ModelTier.MEDIUM,
        "preferred_capabilities": [ModelCapability.CODING, ModelCapability.CONTEXT]
    },
    ModelRole.REVIEWER: {
        "min_tier": ModelTier.LOW,
        "preferred_capabilities": [ModelCapability.SPEED]
    },
    ModelRole.DEBUGGER: {
        "min_tier": ModelTier.HIGH,
        "preferred_capabilities": [ModelCapability.REASONING, ModelCapability.CODING]
    },
    ModelRole.VERIFIER: {
        "min_tier": ModelTier.EXTREME,
        "preferred_capabilities": [ModelCapability.REASONING, ModelCapability.CONTEXT]
    },
    ModelRole.ARCHITECT: {
        "min_tier": ModelTier.EXTREME,
        "preferred_capabilities": [ModelCapability.REASONING, ModelCapability.CONTEXT, ModelCapability.CODING]
    }
}

def score_model(model: ModelMetadata, role: str) -> float:
    reqs = ROLE_REQUIREMENTS.get(role)
    if not reqs:
        return 0.0
    
    score = 0.0
    
    # 1. Tier score (lower tier is better/higher priority for quality)
    # We want Extreme (1) to be higher than High (2)
    score += (5 - int(model.tier)) * 20
    
    # 2. Capability match
    for cap in reqs["preferred_capabilities"]:
        if cap in model.capabilities:
            score += 10
            
    # 3. Penalty for being below min_tier
    if int(model.tier) > int(reqs["min_tier"]):
        score -= 50
        
    return score

def get_prioritized_models(role: str | None = None, allowed_models: list[str] | None = None, preferred_family: str | None = None) -> list[str]:
    """
    Returns a list of model IDs prioritized by suitability for the role.
    - role: The role to prioritize for (planner, builder, etc.)
    - allowed_models: Restriction list (IDs or aliases)
    - preferred_family: If specified, models in this family get a boost
    """
    all_models = get_all_models()
    
    # Filter by allowed_models if provided
    if allowed_models:
        filtered = []
        for m_id in allowed_models:
            m = get_model(m_id)
            if m:
                filtered.append(m)
        models_to_score = filtered
    else:
        models_to_score = all_models

    if not role:
        # Default fallback list logic (High tier first, then Medium, then Low)
        sorted_models = sorted(models_to_score, key=lambda m: (m.tier, m.id))
        return [m.id for m in sorted_models]

    # Score and sort
    scored_models = []
    for m in models_to_score:
        score = score_model(m, role)
        
        # Boost preferred family
        if preferred_family and m.family == preferred_family:
            score += 25
            
        scored_models.append((m, score))
        
    # Sort by score (descending)
    scored_models.sort(key=lambda x: x[1], reverse=True)
    
    return [m[0].id for m in scored_models]


AUTO_PRESET = "auto"

SIMPLE, STANDARD, COMPLEX = "simple", "standard", "complex"

# The tier each role should reach, so a small job doesn't pay for a frontier model.
_TARGET_TIER = {
    SIMPLE: {ModelRole.PLANNER: ModelTier.MEDIUM, ModelRole.BUILDER: ModelTier.MEDIUM, ModelRole.REVIEWER: ModelTier.LOW},
    STANDARD: {ModelRole.PLANNER: ModelTier.HIGH, ModelRole.BUILDER: ModelTier.HIGH, ModelRole.REVIEWER: ModelTier.MEDIUM},
    COMPLEX: {ModelRole.PLANNER: ModelTier.EXTREME, ModelRole.BUILDER: ModelTier.EXTREME, ModelRole.REVIEWER: ModelTier.HIGH},
}

_COMPLEX_SIGNALS = re.compile(
    r"race condition|deadlock|memory leak|concurren|thread|intermittent|flaky|data loss|corrupt|"
    r"security|vulnerab|architect|refactor|rewrite|migrat|redesign|across the (app|codebase)", re.IGNORECASE)
_SIMPLE_SIGNALS = re.compile(
    r"\b(typo|rename|copy change|wording|label|tweak|bump|colou?r|spacing|padding)\b", re.IGNORECASE)
_LONG_BRIEF_CHARS = 4000
_SHORT_BRIEF_CHARS = 200


@dataclass
class JobProfile:
    job_type: str = "feature"
    complexity: str = STANDARD
    needs_vision: bool = False
    needs_context: bool = False
    reasons: list[str] = field(default_factory=list)


def profile_job(job_type: str, text: str = "", stitch: bool = False) -> JobProfile:
    """Reads what the job needs from its type and brief, using rules rather than a model call."""
    profile = JobProfile(job_type=job_type, needs_vision=stitch or job_type == "design")
    match = _COMPLEX_SIGNALS.search(text)
    # A long brief needs a model with a large context, but length alone doesn't make the job hard.
    if len(text) > _LONG_BRIEF_CHARS:
        profile.needs_context = True
        profile.reasons.append("the brief is long, so the planner needs a large context")
    if match:
        profile.complexity = COMPLEX
        profile.reasons.append(f'the brief mentions "{match.group(0).lower()}"')
    if profile.complexity != COMPLEX:
        if job_type == "quick":
            profile.complexity = SIMPLE
            profile.reasons.append("it is a quick job")
        elif len(text) <= _SHORT_BRIEF_CHARS and _SIMPLE_SIGNALS.search(text):
            profile.complexity = SIMPLE
            profile.reasons.append("it is a short, small change")
    if profile.needs_vision:
        profile.reasons.append("it involves visual design")
    if job_type == "bug":
        profile.reasons.append("bugs need a builder that can reason about causes")
    if not profile.reasons:
        profile.reasons.append("it looks like a standard job")
    return profile


def _role_capabilities(role: str, profile: JobProfile) -> list[ModelCapability]:
    if role == ModelRole.PLANNER:
        capabilities = [ModelCapability.REASONING, ModelCapability.CONTEXT]
        return capabilities + [ModelCapability.VISION] if profile.needs_vision else capabilities
    if role == ModelRole.BUILDER:
        if profile.job_type == "bug":
            return [ModelCapability.CODING, ModelCapability.REASONING]
        return [ModelCapability.CODING, ModelCapability.CONTEXT]
    return [ModelCapability.REASONING, ModelCapability.SPEED]


def _quality_tier(model: ModelMetadata) -> int:
    # A small local model shouldn't outrank a hosted one because of a default tier.
    return ModelTier.LOW if "ollama" in model.required_clis else model.tier


def _version(model: ModelMetadata) -> tuple[int, ...]:
    found = re.search(r"\d+(?:[.-]\d+)*", model.id)
    return tuple(int(part) for part in re.split(r"[.-]", found.group(0))) if found else ()


def _role_key(model: ModelMetadata, role: str, profile: JobProfile) -> tuple:
    """Sorts models by fit: reach the target tier without overshooting, match the role, then cost and recency."""
    target = _TARGET_TIER[profile.complexity][role]
    tier = _quality_tier(model)
    capabilities = _role_capabilities(role, profile)
    required = (
        (role == ModelRole.BUILDER and ModelCapability.CODING not in model.capabilities)
        or (role == ModelRole.PLANNER and profile.needs_vision and ModelCapability.VISION not in model.capabilities)
        or (role == ModelRole.PLANNER and profile.needs_context and ModelCapability.CONTEXT not in model.capabilities)
    )
    return (
        required,
        max(0, tier - target),
        max(0, target - tier),
        -sum(cap in model.capabilities for cap in capabilities),
        model.cost_factor,
        # The trailing 1 sorts 6.1 ahead of 6, and named versions ahead of account defaults.
        (*(-part for part in _version(model)), 1),
        model.id,
    )


def usable_models(allowed_models: list[str], snapshot: dict[str, Any]) -> list[ModelMetadata]:
    """Allowed models the account can use, or ones that couldn't be checked if none are confirmed."""
    resolved = {meta.id: meta for meta in map(get_model, allowed_models) if meta}
    confirmed = [m for m in resolved.values() if is_available(m, snapshot) is True]
    return confirmed or [m for m in resolved.values() if is_available(m, snapshot) is None]


def auto_assign_roles(allowed_models: list[str], snapshot: dict[str, Any], profile: JobProfile | None = None) -> dict[str, str]:
    """
    Picks the best planner, builder and reviewer for this job from the usable allowed models.

    This is separate from get_prioritized_models, whose quality-first order drives
    runtime fallbacks and would assign the most expensive model to every role.
    Returns an empty dict when none of the allowed models can be used.
    """
    profile = profile or JobProfile()
    candidates = usable_models(allowed_models, snapshot)
    if not candidates:
        return {}
    planner = min(candidates, key=lambda m: _role_key(m, ModelRole.PLANNER, profile))
    builder = min(candidates, key=lambda m: _role_key(m, ModelRole.BUILDER, profile))
    # An independent review needs a different model, and another provider if that costs no quality.
    target = _TARGET_TIER[profile.complexity][ModelRole.REVIEWER]
    reviewer = min(candidates, key=lambda m: (
        m.id == builder.id, max(0, _quality_tier(m) - target), "ollama" in m.required_clis, m.family == builder.family,
        _role_key(m, ModelRole.REVIEWER, profile)))
    return {ModelRole.PLANNER: planner.id, ModelRole.BUILDER: builder.id, ModelRole.REVIEWER: reviewer.id}


def fit_to_allowed(model_id: str, allowed_models: list[str]) -> str:
    """Resolves aliases on both sides, so an id still matches an allowed entry like "codex"."""
    meta = get_model(model_id)
    resolved_id = meta.id if meta else model_id
    allowed_ids = {(get_model(entry).id if get_model(entry) else entry) for entry in allowed_models}
    return resolved_id if resolved_id in allowed_ids else allowed_models[0]
