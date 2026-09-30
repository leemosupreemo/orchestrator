#!/usr/bin/env python3
from __future__ import annotations
from typing import Any
from model_registry import get_all_models, ModelMetadata, ModelTier, ModelCapability, get_model
from execution_capabilities import ExecutionMode, profile_for_model, supports_mode

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

def score_model(model: ModelMetadata, role: str, required_execution_mode: str | None = None) -> float:
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

    if required_execution_mode:
        requested_mode = ExecutionMode(required_execution_mode)
        if supports_mode(profile_for_model(model.id), requested_mode):
            score += 60
        else:
            score -= 60
        
    return score

def get_prioritized_models(role: str | None = None, allowed_models: list[str] | None = None, preferred_family: str | None = None, required_execution_mode: str | None = None) -> list[str]:
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
        score = score_model(m, role, required_execution_mode=required_execution_mode)
        
        # Boost preferred family
        if preferred_family and m.family == preferred_family:
            score += 25
            
        scored_models.append((m, score))
        
    # Sort by score (descending)
    scored_models.sort(key=lambda x: x[1], reverse=True)
    
    return [m[0].id for m in scored_models]
