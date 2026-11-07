#!/usr/bin/env python3
from __future__ import annotations
from typing import Any
import re
from dataclasses import dataclass, field
from model_registry import get_all_models, ModelMetadata, ModelTier, ModelCapability, get_model, model_usable, runnable_here

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

    # Leave out models their provider can't run now, unless that would leave nothing to try.
    models_to_score = [m for m in models_to_score if model_usable(m)] or models_to_score

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

def builder_for_task(task: dict[str, Any], job: dict[str, Any]) -> str | None:
    """Use a cheaper coding model for a planner-marked small task, within the job allowed-model set."""
    configured_builder = job.get("builder")
    complexity = str(task.get("complexity") or "").strip().lower()
    if complexity not in {"small", "simple"}:
        return configured_builder

    allowed = job.get("allowed_models")
    candidates = [
        model for model_id in get_prioritized_models(ModelRole.BUILDER, allowed_models=allowed)
        if (model := get_model(model_id)) and ModelCapability.CODING in model.capabilities
    ]
    if not candidates:
        return configured_builder

    def is_free_or_local(model: ModelMetadata) -> bool:
        if model.family in {"ollama", "qwen"}:
            return True
        return model.family == "opencode" and (
            model.id == "opencode/big-pickle" or model.id.endswith("-free")
        )

    def selection_key(model: ModelMetadata) -> tuple[int, float, int, str]:
        free_or_local = is_free_or_local(model)
        known_cost = model.cost_factor if model.cost_factor > 0 else float("inf")
        tier = -int(model.tier) if free_or_local else int(model.tier)
        return (0 if free_or_local else 1, known_cost, tier, model.id)

    selected = min(candidates, key=selection_key)
    return selected.id


# --------------------------------------------------------------------------- Auto: the best model for each role, per job

AUTO = "auto"
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
_SIMPLE_SIGNALS = re.compile(r"\b(typo|rename|copy change|wording|label|tweak|bump|colou?r|spacing|padding)\b", re.IGNORECASE)
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
    """What the job needs, read from its type and brief by rules rather than a model call."""
    profile = JobProfile(job_type=job_type, needs_vision=stitch or job_type == "design")
    match = _COMPLEX_SIGNALS.search(text)
    if len(text) > _LONG_BRIEF_CHARS:  # needs a large context, but length alone doesn't make the job hard
        profile.needs_context = True
        profile.reasons.append("the brief is long, so the planner needs a large context")
    if match:
        profile.complexity = COMPLEX
        profile.reasons.append(f'the brief mentions "{match.group(0).lower()}"')
    elif job_type == "quick":
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
    if role in (ModelRole.PLANNER, ModelRole.ARCHITECT):
        caps = [ModelCapability.REASONING, ModelCapability.CONTEXT]
        return caps + [ModelCapability.VISION] if profile.needs_vision else caps
    if role == ModelRole.BUILDER:
        return [ModelCapability.CODING, ModelCapability.REASONING if profile.job_type == "bug" else ModelCapability.CONTEXT]
    return [ModelCapability.REASONING, ModelCapability.SPEED]


def _unproven(model: ModelMetadata) -> bool:
    """A small local model, or an agent running whatever model it was set up with (Hermes): its strength isn't known."""
    return bool({"ollama", "hermes"} & set(model.required_clis))


def _quality_tier(model: ModelMetadata) -> int:
    return ModelTier.LOW if _unproven(model) else model.tier  # shouldn't outrank a hosted model that is known to be strong


def _version(model: ModelMetadata) -> tuple[int, ...]:
    found = re.search(r"\d+(?:[.-]\d+)*", model.id)
    # A release date suffix (claude-haiku-4-5-20251001) names the same model, not a newer one.
    return tuple(int(part) for part in re.split(r"[.-]", found.group(0)) if len(part) < 8) if found else ()


def _role_key(model: ModelMetadata, role: str, profile: JobProfile) -> tuple:
    """Fit for a role: reach the target tier without overshooting, have what the role needs, then cost and recency."""
    target = _TARGET_TIER[profile.complexity][ModelRole.PLANNER if role == ModelRole.ARCHITECT else role]
    tier = _quality_tier(model)
    lacking = ((role == ModelRole.BUILDER and ModelCapability.CODING not in model.capabilities)
               or (role == ModelRole.PLANNER and profile.needs_vision and ModelCapability.VISION not in model.capabilities)
               or (role == ModelRole.PLANNER and profile.needs_context and ModelCapability.CONTEXT not in model.capabilities))
    return (lacking, max(0, tier - target), max(0, target - tier), _unproven(model),  # on a tie, a known hosted model wins
            -sum(cap in model.capabilities for cap in _role_capabilities(role, profile)), model.cost_factor,
            (*(-part for part in _version(model)), 1), model.id)  # the trailing 1 sorts 6.1 ahead of 6


def auto_candidates(allowed_models: list[str]) -> list[ModelMetadata]:
    """Allowed models that can run here now: the tool is installed, the provider offers it, and it writes code."""
    found = {}
    for entry in allowed_models:
        meta = get_model(entry)
        if meta and runnable_here(meta):
            found[meta.id] = meta
    return list(found.values())


def auto_assign_roles(allowed_models: list[str], profile: JobProfile | None = None) -> dict[str, str]:
    """The best architect, planner, builder and reviewer for this job from the allowed models; {} when none can run.

    Separate from get_prioritized_models, whose quality-first order drives runtime fallbacks and would give every
    role the most expensive model."""
    profile = profile or JobProfile()
    candidates = auto_candidates(allowed_models)
    if not candidates:
        return {}
    pick = lambda role: min(candidates, key=lambda m: _role_key(m, role, profile))
    planner, builder, architect = pick(ModelRole.PLANNER), pick(ModelRole.BUILDER), pick(ModelRole.ARCHITECT)
    target = _TARGET_TIER[profile.complexity][ModelRole.REVIEWER]
    # An independent review: a different model, from another provider when that costs no quality.
    reviewer = min(candidates, key=lambda m: (m.id == builder.id, max(0, _quality_tier(m) - target), _unproven(m),
                                              m.family == builder.family, _role_key(m, ModelRole.REVIEWER, profile)))
    return {ModelRole.ARCHITECT: architect.id, ModelRole.PLANNER: planner.id, ModelRole.BUILDER: builder.id,
            ModelRole.REVIEWER: reviewer.id}
