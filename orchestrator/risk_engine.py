"""Deterministic Ambiguity & Risk Engine and Lightweight Gate Policy.

Evaluates uncertain decisions along three qualitative dimensions:
- Confidence:    "high" | "low" (supported by explicit sources or repo evidence)
- Impact:        "low"  | "high" (how much getting it wrong matters to product/users/security)
- Reversibility: "easy" | "costly" (how expensive it is to change later)

Applies an explicit decision policy:
- Conflicting explicit requirements       -> Stop & clarify conflict (Intent Gate)
- High confidence, low risk               -> Proceed automatically
- Low confidence, low impact, reversible  -> Choose a reasonable default and document it in assumptions
- Low confidence, technically investigable-> Inspect the repository for evidence; proceed if resolved
- Low confidence, high impact / costly    -> Request targeted human input (Intent Gate)

Also evaluates the Plan Gate and determines when a clear, low-risk, reversible change
can auto-pass the Intent and Plan gates.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
from typing import Any

from orchestrator import plan_size, reuse_brief, scope_check, slice_check

UNSAFE_OR_IRREVERSIBLE_PATTERNS = re.compile(
    r"\b("
    r"privacy|private|personal data|pii|expose account|public profile|auth|authentication|authorization|permission|role|token|secret|credential|"
    r"billing|payment|subscription|charge|price|pricing|"
    r"delete data|drop table|destructive|irreversible"
    r")\b",
    re.IGNORECASE,
)

PRODUCT_HYPOTHESIS_PATTERNS = re.compile(
    r"\b("
    r"rank|ranking|eligible score|tie|ties|tie-breaker|fairness|scoring rule|retry count|retries count|"
    r"sort order|filter default|bonus points|multiplier|threshold"
    r")\b",
    re.IGNORECASE,
)

HIGH_IMPACT_PATTERNS = re.compile(
    r"\b("
    r"privacy|private|personal data|pii|expose account|public profile|auth|authentication|authorization|permission|role|token|secret|credential|"
    r"billing|payment|subscription|charge|price|pricing|"
    r"rank|ranking|eligible score|tie|ties|tie-breaker|fairness|scoring rule|retry count|retries count|"
    r"delete data|drop table|destructive|irreversible"
    r")\b",
    re.IGNORECASE,
)

COSTLY_REVERSIBILITY_PATTERNS = re.compile(
    r"\b("
    r"schema|migration|database|data model|persistent format|wire protocol|api_version|breaking change|public api|"
    r"encryption|keychain|third-party dependency|architecture overhaul"
    r")\b",
    re.IGNORECASE,
)

LOW_RISK_UI_PATTERNS = re.compile(
    r"\b("
    r"button style|button styling|color|colour|spacing|padding|margin|icon|badge|tooltip|placeholder|copy|label| wording|"
    r"loading state|empty state|error toast|local variable|helper name"
    r")\b",
    re.IGNORECASE,
)

INVESTIGABLE_PATTERNS = re.compile(
    r"\b("
    r"existing|already exist|which file|which module|where is|how does current|current implementation|"
    r"existing model|existing service|existing endpoint|test framework|test runner|reuse"
    r")\b",
    re.IGNORECASE,
)


def classify_decision_risk(decision: dict[str, Any]) -> dict[str, Any]:
    """Apply deterministic guardrails to a decision's qualitative risk labels."""
    d = dict(decision)
    text = f"{d.get('question', '')} {d.get('proposed_default', '')}".strip()

    confidence = str(d.get("confidence") or "low").strip().lower()
    if confidence not in ("high", "low"):
        confidence = "low"
    impact = str(d.get("impact") or "low").strip().lower()
    if impact not in ("high", "low"):
        impact = "low"
    reversibility = str(d.get("reversibility") or "easy").strip().lower()
    if reversibility not in ("easy", "costly"):
        reversibility = "easy"
    resolvable_by = str(d.get("resolvable_by") or "default_assumption").strip().lower()

    # Guardrail 1: High-impact product/privacy/security/fairness topics cannot be silently treated as low-impact
    if HIGH_IMPACT_PATTERNS.search(text):
        impact = "high"
    # Guardrail 2: Schema, migration, protocol, and public API decisions are costly to reverse
    if COSTLY_REVERSIBILITY_PATTERNS.search(text):
        reversibility = "costly"
    # Guardrail 3: Purely cosmetic / conventional UI styling defaults with no high-impact signal stay low-impact & reversible
    if LOW_RISK_UI_PATTERNS.search(text) and not HIGH_IMPACT_PATTERNS.search(text) and not COSTLY_REVERSIBILITY_PATTERNS.search(text):
        impact = "low"
        reversibility = "easy"
    # Guardrail 4: Questions about existing repo structure are technically investigable
    if INVESTIGABLE_PATTERNS.search(d.get("question", "")) and resolvable_by == "default_assumption" and confidence == "low":
        resolvable_by = "repo_inspection"

    d.update(confidence=confidence, impact=impact, reversibility=reversibility, resolvable_by=resolvable_by)
    return d


def investigate_decision_in_repo(decision: dict[str, Any], root: Path | None) -> dict[str, Any] | None:
    """Inspect the repository for definitions or files that answer a technically investigable decision."""
    if root is None or not Path(root).is_dir():
        return None
    root = Path(root).resolve()
    query_text = f"{decision.get('question', '')} {decision.get('proposed_default', '')}"
    defs = reuse_brief.index(root)
    matches = reuse_brief.rank(defs, query_text, decision.get("evidence") or [])
    if not matches:
        return None
    top = matches[:3]
    summary = ", ".join(f"`{m['name']}` ({m['path']}:{m['line']})" for m in top)
    paths = list(dict.fromkeys(m["path"] for m in top))
    return {
        "question": decision.get("question", ""),
        "finding": f"Found matching repository definitions: {summary}",
        "evidence": paths,
    }


def _add_assumption(spec: dict[str, Any], text: str, reason: str, source: str = "risk_engine") -> None:
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return
    assumptions = spec.setdefault("assumptions", [])
    existing = {(a.get("text", "") if isinstance(a, dict) else str(a)).lower() for a in assumptions}
    if text.lower() not in existing:
        assumptions.append({
            "text": text,
            "provenance": "inferred",
            "source": source,
            "reason": " ".join(str(reason or "").split()).strip(),
        })


def _add_hypothesis(spec: dict[str, Any], decision_id: str, question: str, chosen_default: str, category: str = "product_hypothesis") -> dict[str, str]:
    hypotheses = spec.setdefault("product_hypotheses", [])
    entry = {
        "id": decision_id or f"hyp-{len(hypotheses) + 1}",
        "question": " ".join(str(question or "").split()).strip(),
        "chosen_default": " ".join(str(chosen_default or "").split()).strip(),
        "category": category,
        "status": "active_hypothesis",
    }
    if not any(isinstance(h, dict) and h.get("question", "").lower() == entry["question"].lower() for h in hypotheses):
        hypotheses.append(entry)
    return entry


def evaluate(spec: dict[str, Any], *, root: Path | None = None, prototype_first: bool = False) -> dict[str, Any]:
    """Evaluate all conflicts and uncertain decisions in `spec` and determine Intent Gate outcome.

    When `prototype_first=True` (or `spec.get("prototype_first")`), reversible product-behavior
    questions (such as initial ranking, scoring, or tie-breaker rules) with a defensible default
    act by default as a v0.1 Product Hypothesis rather than blocking Time to First Experience (TTFE),
    while unsafe or irreversible decisions (privacy/PII, security/auth, billing, destructive data
    loss, or costly schema/protocol changes) still pause at the Intent Gate.
    """
    use_prototype_first = bool(prototype_first or spec.get("prototype_first"))
    conflicts = list(spec.get("conflicts") or [])
    raw_decisions = list(spec.get("decisions") or [])

    evaluated_decisions: list[dict[str, Any]] = []
    investigations: list[dict[str, Any]] = []
    auto_assumed: list[dict[str, Any]] = []
    product_hypotheses: list[dict[str, str]] = []
    clarifications_needed: list[dict[str, Any]] = []

    for raw in raw_decisions:
        d = classify_decision_risk(raw)
        text = f"{d.get('question', '')} {d.get('proposed_default', '')}".strip()
        is_unsafe_or_costly = bool(UNSAFE_OR_IRREVERSIBLE_PATTERNS.search(text) or COSTLY_REVERSIBILITY_PATTERNS.search(text))
        is_product_hypothesis = bool(PRODUCT_HYPOTHESIS_PATTERNS.search(text) and not is_unsafe_or_costly)

        # 1. High confidence and low risk -> Proceed automatically
        if d["confidence"] == "high" and d["impact"] == "low":
            d["action"] = "proceed"
            if d.get("proposed_default"):
                _add_assumption(spec, d["proposed_default"], "High-confidence, low-risk convention", source="risk_engine")
                product_hypotheses.append(_add_hypothesis(spec, d.get("id", ""), d["question"], d["proposed_default"], "convention"))
            evaluated_decisions.append(d)
            continue

        # 2. Technically investigable -> Inspect repository first
        if d["resolvable_by"] == "repo_inspection":
            inv = investigate_decision_in_repo(d, root)
            if inv:
                d["action"] = "investigated_and_resolved"
                d["evidence"] = list(dict.fromkeys([*(d.get("evidence") or []), *inv["evidence"]]))
                investigations.append(inv)
                resolution_text = d.get("proposed_default") or inv["finding"]
                _add_assumption(spec, resolution_text, inv["finding"], source="repo_inspection")
                evaluated_decisions.append(d)
                continue
            if d["impact"] == "low" and d["reversibility"] == "easy" and d.get("proposed_default"):
                d["action"] = "assume_default"
                _add_assumption(spec, d["proposed_default"], "Low-impact, reversible default after repo check", source="risk_engine")
                product_hypotheses.append(_add_hypothesis(spec, d.get("id", ""), d["question"], d["proposed_default"], "default_assumption"))
                auto_assumed.append(d)
                evaluated_decisions.append(d)
                continue

        # 3. Low confidence, low impact, reversible -> Choose reasonable default & document it
        if d["impact"] == "low" and d["reversibility"] == "easy":
            default_choice = d.get("proposed_default") or f"Follow existing codebase conventions for: {d['question']}"
            d["action"] = "assume_default"
            d["proposed_default"] = default_choice
            _add_assumption(spec, default_choice, f"Low-impact, reversible choice for '{d['question']}'", source="risk_engine")
            product_hypotheses.append(_add_hypothesis(spec, d.get("id", ""), d["question"], default_choice, "default_assumption"))
            auto_assumed.append(d)
            evaluated_decisions.append(d)
            continue

        # 4. Prototype-First TTFE optimization: reversible product-behavior hypotheses act by default for v0.1
        if use_prototype_first and is_product_hypothesis and d.get("proposed_default"):
            d["action"] = "assume_hypothesis"
            _add_assumption(
                spec,
                d["proposed_default"],
                f"v0.1 product hypothesis for '{d['question']}' (refine after experiencing preview)",
                source="prototype_hypothesis",
            )
            product_hypotheses.append(_add_hypothesis(spec, d.get("id", ""), d["question"], d["proposed_default"], "product_hypothesis"))
            auto_assumed.append(d)
            evaluated_decisions.append(d)
            continue

        # 5. High confidence with an explicit proposed default that isn't marked human_decision
        if d["confidence"] == "high" and d.get("proposed_default") and d["resolvable_by"] != "human_decision":
            d["action"] = "proceed"
            _add_assumption(spec, d["proposed_default"], f"High-confidence interpretation for '{d['question']}'", source="risk_engine")
            product_hypotheses.append(_add_hypothesis(spec, d.get("id", ""), d["question"], d["proposed_default"], "high_confidence"))
            evaluated_decisions.append(d)
            continue

        # 6. Low confidence + (high impact or costly reversibility) -> Request targeted human clarification
        d["action"] = "clarify"
        clarifications_needed.append(d)
        evaluated_decisions.append(d)

    spec["decisions"] = evaluated_decisions

    # Build targeted human question if conflicts or high-impact clarifications exist
    question_lines: list[str] = []
    if conflicts:
        for idx, c in enumerate(conflicts, start=1):
            question_lines.append(
                f"{idx}. Conflicting requirements between {c.get('source_a', 'request')} and {c.get('source_b', 'PRD')}: "
                f"{c.get('description')}. Which requirement should take precedence?"
            )
    offset = len(question_lines)
    for idx, d in enumerate(clarifications_needed, start=1):
        q = d["question"]
        if d.get("proposed_default"):
            q = f"{q} (Proposed default: {d['proposed_default']})"
        if len(clarifications_needed) + offset > 1 and not re.match(r"^\d+[.)]\s", q):
            question_lines.append(f"{offset + idx}. {q}")
        else:
            question_lines.append(q)

    clarification_question = "\n".join(question_lines).strip()
    intent_gate_passed = not conflicts and not clarifications_needed

    if conflicts:
        overall_situation = "conflicting_requirements"
        behavior = "stop_conflict"
    elif clarifications_needed:
        overall_situation = "low_confidence_high_impact"
        behavior = "clarify"
    elif investigations:
        overall_situation = "low_confidence_investigated"
        behavior = "investigate"
    elif auto_assumed:
        overall_situation = "low_confidence_low_impact_reversible"
        behavior = "assume_default"
    else:
        overall_situation = "high_confidence_low_risk"
        behavior = "proceed"

    spec["open_questions"] = [d["question"] for d in clarifications_needed]

    return {
        "intent_gate_passed": intent_gate_passed,
        "situation": overall_situation,
        "behavior": behavior,
        "conflicts": conflicts,
        "decisions": evaluated_decisions,
        "investigations": investigations,
        "auto_assumed": auto_assumed,
        "product_hypotheses": product_hypotheses,
        "clarifications_needed": clarifications_needed,
        "clarification_question": clarification_question,
    }


def _touches_manifests(plan: dict[str, Any]) -> bool:
    files: set[str] = set()
    for f in plan.get("likely_files") or []:
        if str(f).strip():
            files.add(PurePosixPath(str(f).strip()).name)
    for task in plan.get("tasks") or []:
        if isinstance(task, dict):
            for f in task.get("likely_files") or []:
                if str(f).strip():
                    files.add(PurePosixPath(str(f).strip()).name)
    return bool(files & scope_check.MANIFESTS)


def evaluate_plan_gate(
    spec: dict[str, Any],
    risk_result: dict[str, Any],
    plan: dict[str, Any],
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the Plan Gate and determine whether the job auto-passes without manual approval.

    A plan auto-passes when:
    - Intent Gate passed (no conflicts, no unresolved high-impact/costly decisions)
    - No high-impact or costly-reversibility decisions were left to low-confidence guesswork
    - Plan size is Small (<= 3 tasks, <= 6 files, not too_large)
    - Slice check has zero warnings (tasks are vertical slices with acceptance criteria)
    - Test case check has zero problems
    - No dependency/build manifest files are modified
    - Senior Architect verification (if run) approved the plan
    """
    size = plan_size.measure(plan)
    slices = slice_check.problems(plan)
    tc_problems = list(plan.get("test_case_problems") or [])
    touches_manifest = _touches_manifests(plan)
    verifier_status = (verification or {}).get("status")
    verifier_ok = verification is None or verifier_status == "approved"

    has_costly_decision = any(
        d.get("impact") == "high" or d.get("reversibility") == "costly"
        for d in (risk_result.get("decisions") or [])
    )

    plan_gate_passed = (
        bool(risk_result.get("intent_gate_passed"))
        and not size["too_large"]
        and not slices
        and not tc_problems
        and verifier_ok
    )

    auto_pass = (
        plan_gate_passed
        and not has_costly_decision
        and size["label"] == "Small"
        and not touches_manifest
    )

    gate_policy = "auto_pass" if auto_pass else "intent_gate" if not risk_result.get("intent_gate_passed") else "human_approval"
    return {
        **risk_result,
        "plan_gate_passed": plan_gate_passed,
        "auto_pass": auto_pass,
        "gate_policy": gate_policy,
        "plan_checks": {
            "size_label": size["label"],
            "too_large": size["too_large"],
            "slice_warnings": slices,
            "test_case_problems": tc_problems,
            "touches_manifest": touches_manifest,
            "verifier_status": verifier_status or "not_needed",
        },
    }


def should_run_architect_verifier(
    spec: dict[str, Any],
    risk_result: dict[str, Any],
    plan: dict[str, Any],
    planner_meta: Any = None,
) -> bool:
    """Return True only when the Senior Architect verifier LLM adds value beyond the deterministic Plan Gate.

    Skips the extra verifier LLM call when:
    - The planner is already an Extreme-tier model, OR
    - The planner is a High-tier paid model AND the deterministic Plan Gate auto-passes
      (Small size, working vertical slices, 100% test-case coverage, no manifest edits,
      no high-impact/costly decisions, single-story slice, and every task grounded in concrete files & tests).
    """
    if planner_meta is None:
        return False
    tier_val = int(getattr(planner_meta, "tier", 1))
    if tier_val <= 1:  # ModelTier.EXTREME
        return False
    cost_factor = float(getattr(planner_meta, "cost_factor", 1.0) or 0.0)
    if cost_factor <= 0.0 or tier_val >= 3:
        # Free or medium/low-tier models benefit from Senior Architect verification
        return True
    pre_gate = evaluate_plan_gate(spec, risk_result, plan, verification=None)
    if not pre_gate.get("auto_pass"):
        return True
    if plan.get("deferred") or spec.get("deferred") or len(spec.get("stories") or []) > 1:
        return True
    tasks = [t for t in (plan.get("tasks") or []) if isinstance(t, dict)]
    if not tasks or len(tasks) > 3:
        return True
    for task in tasks:
        if not (task.get("likely_files") and task.get("tests")):
            return True
    return False

