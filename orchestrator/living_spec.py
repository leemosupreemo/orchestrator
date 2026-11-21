"""Canonical intermediate specification: the Living Feature Spec.

Every incoming request—from a one-line prompt to a full PRD with designs and
acceptance criteria—is normalized into the same structure with provenance
(`explicit`, `code_derived`, `inferred`), deterministic conflict detection
against the product's non-goals and approved architecture, evidence path
verification (flagging stale/renamed file paths), and rolling-wave story
decomposition (`ready_story` for Pass B task planning, `deferred` for backlog).
"""
from __future__ import annotations

from ast import literal_eval
from datetime import datetime
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from orchestrator import analytics, prd, reuse_brief

PROVENANCE_VALUES = ("explicit", "code_derived", "inferred")
DEPTH_LEVELS = ("sparse", "partial", "detailed")
JOB_KINDS = ("feature", "bug", "design", "coverage", "quick")
EVIDENCE_KINDS = ("code", "prd", "architecture", "attachment", "test")
SLICES = ("MVP", "Next", "Later")

_PROVENANCE_PREFIX = re.compile(r"^\s*\[(explicit|code_derived|code-derived|inferred)\]\s*(.*)$", re.IGNORECASE)
_PATH_TOKEN = re.compile(r"(?:^|[\s`'\"(])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.[A-Za-z0-9]{1,8})(?:$|[\s`'\"),.:;])")
_STOP_WORDS = {
    "no", "not", "never", "avoid", "without", "dont", "don't", "do", "must", "should", "will", "wont", "won't",
    "this", "that", "with", "from", "into", "only", "for", "the", "and", "or", "any", "all", "app", "project",
    "feature", "features", "support", "build", "add", "create", "make", "use", "using", "library", "system",
}


def _clean_text(value: Any, limit: int = 600) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _normalize_provenance(raw: Any, default: str = "inferred") -> str:
    val = str(raw or "").strip().lower().replace("-", "_")
    return val if val in PROVENANCE_VALUES else default


def normalize_item(
    raw: Any,
    default_provenance: str = "inferred",
    default_source: str = "agent_inference",
) -> dict[str, str] | None:
    """Turn a string or dict requirement/constraint/assumption into a provenance-tagged record."""
    if isinstance(raw, dict):
        text = _clean_text(raw.get("text") or raw.get("requirement") or raw.get("statement") or raw.get("title"))
        if not text:
            return None
        prov = _normalize_provenance(raw.get("provenance"), default_provenance)
        source = _clean_text(raw.get("source"), 200) or default_source
        out: dict[str, str] = {"text": text, "provenance": prov, "source": source}
        reason = _clean_text(raw.get("reason") or raw.get("rationale"), 300)
        if reason:
            out["reason"] = reason
        return out

    text = _clean_text(raw)
    if not text:
        return None
    match = _PROVENANCE_PREFIX.match(text)
    if match:
        prov = _normalize_provenance(match.group(1), default_provenance)
        text = _clean_text(match.group(2))
        if not text:
            return None
        source = "user_prompt" if prov == "explicit" else "repository" if prov == "code_derived" else default_source
        return {"text": text, "provenance": prov, "source": source}
    return {"text": text, "provenance": default_provenance, "source": default_source}


def _normalize_items(
    raw_list: Any,
    *,
    default_provenance: str = "inferred",
    default_source: str = "agent_inference",
    limit: int = 20,
) -> list[dict[str, str]]:
    items = raw_list if isinstance(raw_list, list) else [raw_list] if isinstance(raw_list, (str, dict)) else []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in items:
        norm = normalize_item(entry, default_provenance=default_provenance, default_source=default_source)
        if not norm:
            continue
        key = norm["text"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(norm)
        if len(out) >= limit:
            break
    return out


def assess_input_depth(
    raw_input: str,
    prd_text: str = "",
    attachments: list[Any] | None = None,
) -> dict[str, Any]:
    """Evaluate how well the input resolves key product and technical decisions.

    Rather than measuring document length alone, checks decision resolution across:
    1. explicit acceptance criteria / testable outcomes
    2. technical anchoring (files, symbols, routes, or code examples)
    3. constraints or non-goals (in prompt or PRD)
    4. design/reference attachments or UI flow specifics
    """
    text = (raw_input or "").strip()
    lower = text.lower()
    att = attachments or []

    has_ac = bool(
        re.search(r"\b(acceptance criteria|done when|expected behavior|expected result|given\b.*\bthen|must\s+\w+|should\s+\w+)", lower)
        or "->" in text
        or "→" in text
        or len([line for line in text.splitlines() if re.match(r"^\s*(?:[-*•]|\d+[.)])\s+\S+", line)]) >= 3
    )
    has_tech = bool(
        _PATH_TOKEN.search(text)
        or "```" in text
        or re.search(r"\b(function|class|method|endpoint|route|schema|table|view|controller|service)\b", lower)
    )
    prd_parts = prd.split(prd_text) if prd_text else {}
    has_constraints = bool(
        re.search(r"\b(constraint|non-goal|out of scope|do not|don't|must not|leave out|exclude)\b", lower)
        or prd.is_filled(prd_parts.get("not", ""))
    )
    has_designs = bool(
        att
        or re.search(r"\b(figma\.com|\.fig\b|mockup|wireframe|sketch|screenshot|### reference files)\b", lower)
    )

    dimensions = {
        "acceptance_criteria": has_ac,
        "technical_anchoring": has_tech,
        "constraints_or_non_goals": has_constraints,
        "designs_or_examples": has_designs,
    }
    resolved_count = sum(1 for v in dimensions.values() if v)
    if resolved_count >= 3 or (has_ac and has_tech and len(text) >= 400):
        depth = "detailed"
    elif resolved_count >= 1 or len(text) >= 220:
        depth = "partial"
    else:
        depth = "sparse"

    return {
        "depth": depth,
        "resolved_count": resolved_count,
        "dimensions": dimensions,
    }


def _meaningful_phrases(line: str) -> list[str]:
    """Extract forbidden feature phrases from a 'Not this' line (e.g. 'No network play: library only' -> 'network play')."""
    cleaned = re.sub(r"^[-*•\d.)\s]+", "", line.strip())
    head = re.split(r"[:;—–(]", cleaned, maxsplit=1)[0].strip()
    phrases: list[str] = []
    for candidate in (head, cleaned):
        m = re.match(r"^(?:no|not|never|avoid|without|don'?t\s+(?:add|build|include|use|support)|do\s+not\s+(?:add|build|include|use|support))\s+(.+)$",
                     candidate.strip(), flags=re.IGNORECASE)
        target = (m.group(1) if m else candidate).strip(" .!?,")
        words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", target) if w.lower() not in _STOP_WORDS and len(w) >= 3]
        if words:
            phrases.append(" ".join(words[:4]))
    return list(dict.fromkeys(phrases))


def _request_affirmatively_asks_for(text: str, phrase: str) -> bool:
    """True when `text` asks for `phrase` without negating it ('no <phrase>', 'without <phrase>')."""
    words = phrase.split()
    if not words:
        return False
    pattern = r"\b" + r"[\s_-]+".join(re.escape(w) for w in words) + r"\b"
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        start = max(0, match.start() - 35)
        prefix = text[start:match.start()].lower()
        if re.search(r"\b(no|not|never|without|avoid|exclude|excluding|don't|do not|leave out|stay out of)\s+$", prefix):
            continue
        return True
    return False


def _strip_injected_context(raw_input: str) -> str:
    """Keep only the user's request/attachments, stripping any appended Product context block."""
    text = raw_input or ""
    if "## Product context (source of truth" in text:
        text = text.split("## Product context (source of truth", 1)[0]
    return text.strip()


def detect_conflicts(
    raw_input: str,
    prd_text: str = "",
    arch_record: dict[str, Any] | None = None,
    explicit_items: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Detect contradictions between the user request, attached specs/designs, PRD non-goals, and approved architecture."""
    user_text = _strip_injected_context(raw_input)
    extra_texts = [item["text"] for item in (explicit_items or []) if isinstance(item, dict) and item.get("text")]
    combined_request = "\n".join([user_text, *extra_texts]).strip()
    if not combined_request:
        return []

    conflicts: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add_conflict(req_a: str, src_a: str, req_b: str, src_b: str, desc: str) -> None:
        key = (req_a.lower()[:120], req_b.lower()[:120])
        if key not in seen:
            seen.add(key)
            conflicts.append({
                "requirement_a": _clean_text(req_a, 300),
                "source_a": _clean_text(src_a, 120),
                "requirement_b": _clean_text(req_b, 300),
                "source_b": _clean_text(src_b, 120),
                "description": _clean_text(desc, 400),
            })

    # 1. Check against PRD "Not this" (non-goals)
    if prd_text:
        parts = prd.split(prd_text)
        not_this = parts.get("not", "")
        if prd.is_filled(not_this):
            for raw_line in not_this.splitlines():
                line = raw_line.strip()
                if not line or prd._GUIDANCE_LINE.fullmatch(line) or line.upper().startswith("TBD:"):
                    continue
                for phrase in _meaningful_phrases(line):
                    if _request_affirmatively_asks_for(combined_request, phrase):
                        add_conflict(
                            req_a=user_text.splitlines()[0] if user_text else phrase,
                            src_a="user_request",
                            req_b=line.lstrip("-*• ").strip(),
                            src_b="prd.md#Not this",
                            desc=f"The request asks for '{phrase}', which conflicts with the PRD non-goal: '{line.lstrip('-*• ').strip()}'.",
                        )
                        break

    # 2. Check against Approved Architecture (if status == "approved")
    if isinstance(arch_record, dict) and arch_record.get("status") == "approved":
        stack = arch_record.get("stack") if isinstance(arch_record.get("stack"), dict) else {}
        primary_lang = str(stack.get("primary_language") or arch_record.get("primary_language") or "").strip()
        if primary_lang:
            rival_langs = {"swift": ("kotlin", "flutter", "react native"), "python": ("rust", "golang"), "typescript": ("swiftui",)}
            for rival in rival_langs.get(primary_lang.lower(), ()):
                if re.search(rf"\b(?:rewrite|build|implement)\s+(?:in|with|using)\s+{re.escape(rival)}\b", combined_request, re.IGNORECASE):
                    add_conflict(
                        req_a=user_text.splitlines()[0] if user_text else f"Use {rival}",
                        src_a="user_request",
                        req_b=f"Approved primary language is {primary_lang}",
                        src_b="docs/architecture.json",
                        desc=f"The request specifies {rival}, which conflicts with the approved architecture ({primary_lang}).",
                    )

    # 3. Check internal contradictions between explicit sections in the request (e.g. Acceptance Criteria vs Design/Constraints)
    lines = [l.strip() for l in user_text.splitlines() if l.strip()]
    negated_targets: list[tuple[str, str]] = []
    for line in lines:
        m = re.search(r"\b(?:must not|do not|don't|never|no)\s+((?:allow|expose|include|show|use|require|support)?\s*[A-Za-z0-9_-]+(?:\s+[A-Za-z0-9_-]+){0,3})",
                      line, flags=re.IGNORECASE)
        if m:
            for phrase in _meaningful_phrases("No " + m.group(1)):
                if len(phrase) >= 4:
                    negated_targets.append((phrase, line))
    for phrase, neg_line in negated_targets:
        for line in lines:
            if line == neg_line:
                continue
            if _request_affirmatively_asks_for(line, phrase):
                add_conflict(
                    req_a=line,
                    src_a="user_request",
                    req_b=neg_line,
                    src_b="user_request#constraint",
                    desc=f"Conflicting explicit requirements in request: '{line}' vs '{neg_line}'.",
                )

    return conflicts


def _extract_referenced_paths(text: str) -> list[str]:
    found: list[str] = []
    for match in _PATH_TOKEN.finditer(text or ""):
        candidate = match.group(1).strip().lstrip("./")
        if candidate and not candidate.startswith(("http://", "https://", ".orchestrator/")) and candidate not in found:
            found.append(candidate)
    return found


def _find_replacement_candidates(missing_path: str, root: Path, defs: list[tuple[str, int, str]]) -> list[str]:
    stem = PurePosixPath(missing_path).stem.lower()
    suffix = PurePosixPath(missing_path).suffix.lower()
    tokens = reuse_brief.tokens(stem)
    candidates: list[str] = []

    # 1. Look for moved/renamed files in the repo with matching stem or tokens
    try:
        for p in root.rglob(f"*{suffix}" if suffix else "*"):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if rel.startswith((".git/", ".orchestrator/", "node_modules/", ".build/", "build/", "dist/")):
                continue
            p_stem = p.stem.lower()
            if p_stem == stem or (tokens and (tokens & reuse_brief.tokens(p_stem))):
                if rel not in candidates:
                    candidates.append(rel)
                    if len(candidates) >= 4:
                        break
    except OSError:
        pass

    # 2. Check symbol definitions via reuse_brief
    if len(candidates) < 4 and defs:
        ranked = reuse_brief.rank(defs, stem)
        for entry in ranked:
            path = str(entry.get("path") or "")
            if path and path not in candidates:
                candidates.append(path)
            if len(candidates) >= 4:
                break
    return candidates


def verify_evidence_paths(spec: dict[str, Any], root: Path | None) -> dict[str, Any]:
    """Verify file paths referenced in the spec/input against the actual repository tree.

    Flags outdated/renamed/deleted paths in `spec["stale_references"]` with candidate
    replacements from the repository so planners and builders do not target nonexistent paths.
    """
    if root is None or not Path(root).is_dir():
        return spec
    root = Path(root).resolve()
    defs: list[tuple[str, int, str]] | None = None

    def get_defs() -> list[tuple[str, int, str]]:
        nonlocal defs
        if defs is None:
            defs = reuse_brief.index(root)
        return defs

    verified_evidence: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = list(spec.get("stale_references") or [])
    stale_paths = {s.get("path") for s in stale if isinstance(s, dict)}

    for item in spec.get("evidence") or []:
        if isinstance(item, str):
            item = {"path": item, "kind": "code", "note": ""}
        if not isinstance(item, dict) or not item.get("path"):
            continue
        raw_path = str(item["path"]).strip().lstrip("/")
        if raw_path.startswith(("http://", "https://")):
            verified_evidence.append({**item, "exists": True})
            continue
        try:
            resolved = (root / raw_path).resolve()
            exists = (resolved == root or root in resolved.parents) and resolved.exists()
        except OSError:
            exists = False
        if exists:
            rel = resolved.relative_to(root).as_posix()
            verified_evidence.append({
                "path": rel,
                "kind": item.get("kind") if item.get("kind") in EVIDENCE_KINDS else "code",
                "note": _clean_text(item.get("note"), 240),
                "exists": True,
            })
        elif raw_path not in stale_paths:
            stale_paths.add(raw_path)
            replacements = _find_replacement_candidates(raw_path, root, get_defs())
            stale.append({
                "path": raw_path,
                "reason": "Referenced path does not exist in the repository (outdated or renamed).",
                "replacements": replacements,
            })
            for rep in replacements[:2]:
                if not any(e.get("path") == rep for e in verified_evidence):
                    verified_evidence.append({
                        "path": rep,
                        "kind": "code",
                        "note": f"Candidate replacement in repository for stale reference '{raw_path}'",
                        "exists": True,
                    })
            if replacements:
                spec.setdefault("assumptions", []).append({
                    "text": f"Replaced outdated reference `{raw_path}` with existing repository file `{replacements[0]}`",
                    "provenance": "code_derived",
                    "source": replacements[0],
                    "reason": "Referenced path does not exist in the repository.",
                })

    spec["evidence"] = verified_evidence
    spec["stale_references"] = stale
    return spec


def _normalize_decisions(raw_decisions: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw_decisions if isinstance(raw_decisions, list) else [], start=1):
        if not isinstance(entry, dict):
            continue
        question = _clean_text(entry.get("question") or entry.get("decision"), 300)
        if not question:
            continue
        confidence = str(entry.get("confidence") or "low").strip().lower()
        if confidence not in ("high", "low"):
            confidence = "low"
        impact = str(entry.get("impact") or "low").strip().lower()
        if impact not in ("high", "low"):
            impact = "low"
        reversibility = str(entry.get("reversibility") or "easy").strip().lower()
        if reversibility not in ("easy", "costly"):
            reversibility = "easy"
        resolvable_by = str(entry.get("resolvable_by") or "default_assumption").strip().lower()
        if resolvable_by not in ("default_assumption", "repo_inspection", "human_decision", "conflict_resolution"):
            resolvable_by = "human_decision" if impact == "high" and confidence == "low" else "default_assumption"
        evidence = [_clean_text(e, 200) for e in (entry.get("evidence") or []) if _clean_text(e, 200)][:6]
        out.append({
            "id": _clean_text(entry.get("id") or f"dec-{idx}", 40),
            "question": question,
            "proposed_default": _clean_text(entry.get("proposed_default") or entry.get("default"), 300),
            "confidence": confidence,
            "impact": impact,
            "reversibility": reversibility,
            "resolvable_by": resolvable_by,
            "evidence": evidence,
        })
    return out[:12]


def _normalize_stories(raw_stories: Any, default_title: str, default_summary: str, default_ac: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw_stories if isinstance(raw_stories, list) else [], start=1):
        if isinstance(entry, str) and entry.strip():
            entry = {"id": f"story-{idx}", "title": entry.strip(), "summary": entry.strip()}
        if not isinstance(entry, dict):
            continue
        title = _clean_text(entry.get("title") or entry.get("name"), 160)
        if not title:
            continue
        slice_val = next((s for s in SLICES if str(entry.get("slice") or "").strip().lower() == s.lower()), "MVP" if idx == 1 else "Next")
        ac = [_clean_text(a, 300) for a in (entry.get("acceptance_criteria") or []) if _clean_text(a, 300)]
        deps = [_clean_text(d, 80) for d in (entry.get("depends_on") or []) if _clean_text(d, 80)]
        out.append({
            "id": _clean_text(entry.get("id") or f"story-{idx}", 40),
            "title": title,
            "summary": _clean_text(entry.get("summary") or entry.get("description") or title, 400),
            "slice": slice_val,
            "acceptance_criteria": ac or (default_ac if idx == 1 else []),
            "depends_on": deps,
        })
    if not out and (default_title or default_summary):
        out.append({
            "id": "story-1",
            "title": _clean_text(default_title or default_summary.splitlines()[0], 160),
            "summary": _clean_text(default_summary or default_title, 400),
            "slice": "MVP",
            "acceptance_criteria": list(default_ac),
            "depends_on": [],
        })
    return out[:15]


def _derive_event_slug(text: str, suffix: str = "completed") -> str:
    cleaned = re.sub(r"^\[[^\]]+\]\s*", "", str(text or "").strip())
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", cleaned) if w.lower() not in _STOP_WORDS and len(w) >= 2]
    stem = "_".join(words[:4]) if words else "feature_action"
    if not stem[0].isalpha():
        stem = f"feature_{stem}"
    candidate = f"{stem}_{suffix}" if suffix and not stem.endswith(f"_{suffix}") else stem
    candidate = re.sub(r"_+", "_", candidate).strip("_")[:58]
    if len(candidate) < 2:
        candidate = f"feature_{suffix or 'completed'}"
    return candidate


def _normalize_success_criteria(
    data: dict[str, Any],
    *,
    user_text: str,
    prd_text: str,
    expected_outcome: str,
    intent: str,
    ac_items: list[dict[str, str]],
    feature_record: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Ensure every feature has success criteria (explicit or implied based on context and info provided)."""
    has_explicit_heading = bool(re.search(r"\b(success criteria|success measures|kpi|goal|target|metric)\b", user_text, re.IGNORECASE))
    items = _normalize_items(
        data.get("success_criteria") or data.get("success_measures"),
        default_provenance="explicit" if has_explicit_heading else "inferred",
        default_source="user_prompt" if has_explicit_heading else "context_inference",
    )

    # Include PRD 'Success measures' if filled
    if prd_text:
        success_sec = prd.split(prd_text).get("success", "")
        if prd.is_filled(success_sec):
            prd_sc = [
                {"text": l.strip().lstrip("-*• ").strip(), "provenance": "explicit", "source": "prd.md#Success measures"}
                for l in success_sec.splitlines()
                if l.strip() and not prd._GUIDANCE_LINE.fullmatch(l.strip()) and not l.strip().upper().startswith("TBD:")
            ]
            for sc in _normalize_items(prd_sc, default_provenance="explicit", default_source="prd.md#Success measures"):
                if sc["text"].lower() not in {x["text"].lower() for x in items}:
                    items.append(sc)

    # Include feature-defined KPIs as code/feature-derived success criteria
    if isinstance(feature_record, dict):
        for k in feature_record.get("kpis") or []:
            if isinstance(k, dict) and k.get("name") and k.get("event"):
                goal = "" if k.get("target") is None else f" ({'at least' if k.get('direction', 'up') == 'up' else 'at most'} {k['target']:g}{(' ' + k['unit']) if k.get('unit') else ''})"
                sc_text = f"{k['name']} tracked via `{k['event']}`{goal}"
                if sc_text.lower() not in {x["text"].lower() for x in items}:
                    items.append({"text": sc_text, "provenance": "code_derived", "source": f"feature:{feature_record.get('id', 'kpis')}"})

    # If still empty, infer implied success criteria from expected_outcome, intent, or acceptance_criteria
    if not items:
        implied_text = expected_outcome or intent or (ac_items[0]["text"] if ac_items else "Users can complete the feature flow end to end without errors")
        items.append({
            "text": implied_text,
            "provenance": "inferred",
            "source": "context_inference",
        })
        if ac_items and ac_items[0]["text"].lower() != implied_text.lower():
            items.append({
                "text": ac_items[0]["text"],
                "provenance": ac_items[0].get("provenance", "inferred"),
                "source": ac_items[0].get("source", "context_inference"),
            })
    return items[:10]


def _normalize_kpi_metrics(
    data: dict[str, Any],
    *,
    title: str,
    intent: str,
    success_criteria: list[dict[str, str]],
    runtime_root: Path | None = None,
    feature_record: dict[str, Any] | None = None,
    kpi_policy: dict[str, Any] | None = None,
    consume_recommendation: bool = True,
) -> dict[str, Any]:
    """Tie feature success criteria to a primary KPI and guardrail / counter-KPI metrics when analytics is connected.

    If no analytics app is connected (and the feature has no pre-configured KPIs), recommends once that
    KPI/analytics should be set up in Setup / Analytics, and then ignores KPIs until analytics is connected.
    """
    policy = kpi_policy if isinstance(kpi_policy, dict) else analytics.resolve_kpi_setup_policy(runtime_root, consume=consume_recommendation)
    feature_kpis = [k for k in ((feature_record or {}).get("kpis") or []) if isinstance(k, dict) and k.get("name") and k.get("event")]
    is_active = bool(policy.get("analytics_connected") or feature_kpis)

    if not is_active:
        if policy.get("mode") == "recommend_once":
            return {
                "status": "recommend_setup_once",
                "analytics_connected": False,
                "connected_apps": [],
                "primary_kpi": None,
                "guardrail_kpis": [],
                "recommendation": policy.get("recommendation", ""),
            }
        return {
            "status": "ignored_until_connected",
            "analytics_connected": False,
            "connected_apps": [],
            "primary_kpi": None,
            "guardrail_kpis": [],
            "recommendation": "",
        }

    connected_apps = list(policy.get("connected_apps") or ([] if not policy.get("provider_name") else [policy["provider_name"]]))
    primary_app = policy.get("provider_name") or (connected_apps[0] if connected_apps else "Connected Analytics")
    guardrail_app = connected_apps[-1] if connected_apps else primary_app
    raw_km = data.get("kpi_metrics") if isinstance(data.get("kpi_metrics"), dict) else {}
    tied_sc = success_criteria[0]["text"] if success_criteria else (intent or title)
    short_title = re.sub(r"^\[[^\]]+\]\s*", "", title).strip()[:45] or "Feature"

    # 1. Primary KPI
    primary_kpi: dict[str, Any] | None = None
    primary_feature_kpis = [k for k in feature_kpis if k.get("role") not in ("guardrail", "counter")]
    if primary_feature_kpis:
        fk = primary_feature_kpis[0]
        primary_kpi = {
            "name": fk["name"],
            "event": fk["event"],
            "direction": fk.get("direction", "up"),
            "target": fk.get("target"),
            "unit": fk.get("unit", ""),
            "role": "primary",
            "provenance": "code_derived",
            "tied_to_success_criterion": tied_sc,
            "analytics_app": primary_app,
        }
    elif isinstance(raw_km.get("primary_kpi"), dict) and raw_km["primary_kpi"].get("name"):
        pk = raw_km["primary_kpi"]
        ev_raw = str(pk.get("event") or "").strip().lower()
        ev = ev_raw if analytics.EVENT_RE.match(ev_raw) else _derive_event_slug(pk.get("name") or short_title, "completed")
        primary_kpi = {
            "name": _clean_text(pk.get("name"), 78),
            "event": ev,
            "direction": pk.get("direction") if pk.get("direction") in ("up", "down") else "up",
            "target": pk.get("target") if isinstance(pk.get("target"), (int, float)) else None,
            "unit": _clean_text(pk.get("unit"), 20),
            "role": "primary",
            "provenance": _normalize_provenance(pk.get("provenance"), "inferred"),
            "tied_to_success_criterion": tied_sc,
            "analytics_app": primary_app,
        }
    else:
        primary_kpi = {
            "name": f"{short_title} completion rate"[:78],
            "event": _derive_event_slug(short_title, "completed"),
            "direction": "up",
            "target": None,
            "unit": "%",
            "role": "primary",
            "provenance": "inferred",
            "tied_to_success_criterion": tied_sc,
            "analytics_app": primary_app,
        }

    # 2. Guardrail / Counter-KPI Metrics
    guardrail_kpis: list[dict[str, Any]] = []
    for fk in feature_kpis:
        if fk.get("role") in ("guardrail", "counter"):
            guardrail_kpis.append({
                "name": fk["name"],
                "event": fk["event"],
                "direction": fk.get("direction", "down"),
                "target": fk.get("target"),
                "unit": fk.get("unit", ""),
                "role": "guardrail",
                "provenance": "code_derived",
                "analytics_app": guardrail_app,
                "why": f"Guardrail / counter-KPI for {primary_kpi['name']}",
            })
        if isinstance(fk.get("guardrail"), dict) and fk["guardrail"].get("name") and fk["guardrail"].get("event"):
            g = fk["guardrail"]
            guardrail_kpis.append({
                "name": g["name"],
                "event": g["event"],
                "direction": g.get("direction", "down"),
                "target": g.get("target"),
                "unit": g.get("unit", ""),
                "role": "guardrail",
                "provenance": "code_derived",
                "analytics_app": guardrail_app,
                "why": f"Guardrail / counter-KPI for {fk['name']}",
            })

    raw_guardrails = raw_km.get("guardrail_kpis") or raw_km.get("counter_kpis") or []
    if isinstance(raw_guardrails, dict):
        raw_guardrails = [raw_guardrails]
    for rg in raw_guardrails if isinstance(raw_guardrails, list) else []:
        if not isinstance(rg, dict) or not rg.get("name"):
            continue
        ev_raw = str(rg.get("event") or "").strip().lower()
        ev = ev_raw if analytics.EVENT_RE.match(ev_raw) else _derive_event_slug(rg.get("name") or short_title, "error")
        if any(existing["event"] == ev for existing in guardrail_kpis):
            continue
        guardrail_kpis.append({
            "name": _clean_text(rg.get("name"), 78),
            "event": ev,
            "direction": rg.get("direction") if rg.get("direction") in ("up", "down") else "down",
            "target": rg.get("target") if isinstance(rg.get("target"), (int, float)) else None,
            "unit": _clean_text(rg.get("unit"), 20),
            "role": "guardrail",
            "provenance": _normalize_provenance(rg.get("provenance"), "inferred"),
            "analytics_app": guardrail_app,
            "why": _clean_text(rg.get("why") or f"Counter-KPI ensuring {primary_kpi['name']} does not increase errors or drop-off", 240),
        })

    if not guardrail_kpis:
        g_ev = _derive_event_slug(short_title, "error_or_dropoff")
        if g_ev == primary_kpi["event"]:
            g_ev = f"{g_ev[:50]}_guardrail"
        guardrail_kpis.append({
            "name": f"{short_title} error & drop-off rate"[:78],
            "event": g_ev,
            "direction": "down",
            "target": None,
            "unit": "%",
            "role": "guardrail",
            "provenance": "inferred",
            "analytics_app": guardrail_app,
            "why": f"Guardrail / counter-KPI ensuring improvements to '{primary_kpi['name']}' do not increase failures, latency, or abandonment.",
        })

    return {
        "status": "active",
        "analytics_connected": True,
        "connected_apps": connected_apps or [primary_app],
        "primary_kpi": primary_kpi,
        "guardrail_kpis": guardrail_kpis,
        "recommendation": "",
    }


def normalize(
    raw: dict[str, Any] | None,
    *,
    raw_input: str = "",
    prd_text: str = "",
    arch_record: dict[str, Any] | None = None,
    root: Path | None = None,
    runtime_dir: Path | None = None,
    feature_record: dict[str, Any] | None = None,
    kpi_policy: dict[str, Any] | None = None,
    consume_kpi_recommendation: bool = True,
    attachments: list[Any] | None = None,
) -> dict[str, Any]:
    """Produce a canonical LivingFeatureSpec from Pass A output, a plan dict, or raw input."""
    data = raw if isinstance(raw, dict) else {}
    user_text = _strip_injected_context(raw_input)
    first_line = next((l.strip() for l in user_text.splitlines() if l.strip()), "")
    first_line = re.sub(r"^(?:VISION|REQUEST[^:]*|SUMMARY|DESIGN VISION|COVERAGE FOCUS):\s*", "", first_line, flags=re.IGNORECASE).strip()

    depth_info = assess_input_depth(user_text, prd_text=prd_text, attachments=attachments)
    title = _clean_text(data.get("title") or first_line or "Feature Specification", 160)
    intent = _clean_text(data.get("intent") or data.get("summary") or first_line or user_text, 500)
    expected_outcome = _clean_text(data.get("expected_outcome") or data.get("user_benefit") or data.get("summary") or intent, 500)
    inferred_kind = str(data.get("inferred_job_type") or "feature").strip().lower()
    if inferred_kind not in JOB_KINDS:
        inferred_kind = "feature"

    # Extract functional requirements (from Pass A or synthesized from request/tasks)
    func_reqs = _normalize_items(
        data.get("functional_requirements") or data.get("requirements"),
        default_provenance="explicit",
        default_source="user_prompt",
    )
    if not func_reqs and first_line:
        func_reqs = [{"text": first_line, "provenance": "explicit", "source": "user_prompt"}]

    nonfunc_reqs = _normalize_items(
        data.get("nonfunctional_requirements"),
        default_provenance="inferred",
        default_source="agent_inference",
    )

    # Extract acceptance criteria (from top-level or tasks)
    raw_ac = list(data.get("acceptance_criteria") or [])
    if not raw_ac and isinstance(data.get("tasks"), list):
        for t in data["tasks"]:
            if isinstance(t, dict):
                raw_ac.extend(t.get("acceptance_criteria") or [])
    ac_items = _normalize_items(
        raw_ac,
        default_provenance="explicit" if depth_info["dimensions"]["acceptance_criteria"] else "inferred",
        default_source="user_prompt" if depth_info["dimensions"]["acceptance_criteria"] else "agent_inference",
    )

    constraints = _normalize_items(
        data.get("constraints"),
        default_provenance="explicit" if depth_info["dimensions"]["constraints_or_non_goals"] else "code_derived",
        default_source="user_prompt" if depth_info["dimensions"]["constraints_or_non_goals"] else "repository",
    )

    # Non-goals: combine explicit non_goals from model + PRD 'Not this'
    non_goals = _normalize_items(
        data.get("non_goals"),
        default_provenance="explicit",
        default_source="user_prompt",
    )
    if prd_text:
        not_section = prd.split(prd_text).get("not", "")
        if prd.is_filled(not_section):
            prd_non_goals = [
                {"text": l.strip().lstrip("-*• ").strip(), "provenance": "explicit", "source": "prd.md#Not this"}
                for l in not_section.splitlines()
                if l.strip() and not prd._GUIDANCE_LINE.fullmatch(l.strip()) and not l.strip().upper().startswith("TBD:")
            ]
            for item in _normalize_items(prd_non_goals, default_provenance="explicit", default_source="prd.md#Not this"):
                if item["text"].lower() not in {g["text"].lower() for g in non_goals}:
                    non_goals.append(item)

    assumptions = _normalize_items(
        data.get("assumptions"),
        default_provenance="inferred",
        default_source="agent_inference",
    )

    decisions = _normalize_decisions(data.get("decisions"))
    open_questions = [_clean_text(q, 300) for q in (data.get("open_questions") or []) if _clean_text(q, 300)]

    # If the planner used `clarification_needed` string without adding a decision entry, wrap it as a high-impact decision
    clar_raw = _clean_text(data.get("clarification_needed"), 500)
    if clar_raw and not any(d["question"].lower() == clar_raw.lower() for d in decisions):
        from orchestrator.scripts.common import classify_clarification
        clar_kind, clar_clean = classify_clarification(clar_raw)
        if clar_kind == "question" and clar_clean:
            decisions.append({
                "id": f"dec-{len(decisions) + 1}",
                "question": clar_clean,
                "proposed_default": "",
                "confidence": "low",
                "impact": "high",
                "reversibility": "costly",
                "resolvable_by": "human_decision",
                "evidence": [],
            })
            if clar_clean not in open_questions:
                open_questions.append(clar_clean)

    # Evidence: from model + any paths explicitly cited in raw_input or tasks
    raw_evidence = list(data.get("evidence") or [])
    for path_str in _extract_referenced_paths(user_text):
        if not any((e.get("path") if isinstance(e, dict) else e) == path_str for e in raw_evidence):
            raw_evidence.append({"path": path_str, "kind": "code", "note": "Referenced in request"})
    if isinstance(data.get("tasks"), list):
        for t in data["tasks"]:
            if isinstance(t, dict):
                for lf in t.get("likely_files") or []:
                    lf_clean = str(lf).strip().lstrip("./")
                    if lf_clean and not any((e.get("path") if isinstance(e, dict) else e) == lf_clean for e in raw_evidence):
                        raw_evidence.append({"path": lf_clean, "kind": "code", "note": "Target file in task plan"})

    # Conflicts: combine model-reported conflicts + deterministic conflict check
    model_conflicts: list[dict[str, str]] = []
    for c in data.get("conflicts") or []:
        if isinstance(c, dict) and (c.get("description") or c.get("requirement_a")):
            model_conflicts.append({
                "requirement_a": _clean_text(c.get("requirement_a"), 300),
                "source_a": _clean_text(c.get("source_a") or "user_request", 120),
                "requirement_b": _clean_text(c.get("requirement_b"), 300),
                "source_b": _clean_text(c.get("source_b") or "prd.md", 120),
                "description": _clean_text(c.get("description") or f"{c.get('requirement_a')} conflicts with {c.get('requirement_b')}", 400),
            })
    det_conflicts = detect_conflicts(
        user_text,
        prd_text=prd_text,
        arch_record=arch_record,
        explicit_items=[i for i in func_reqs + ac_items if i.get("provenance") == "explicit"],
    )
    all_conflicts = list(model_conflicts)
    existing_descs = {c["description"].lower() for c in all_conflicts}
    for dc in det_conflicts:
        if dc["description"].lower() not in existing_descs:
            all_conflicts.append(dc)

    stories = _normalize_stories(
        data.get("stories"),
        default_title=title,
        default_summary=intent,
        default_ac=[a["text"] for a in ac_items],
    )
    ready_id = _clean_text(data.get("ready_story_id"), 40)
    ready_story = next((s for s in stories if s["id"] == ready_id), stories[0] if stories else None)

    deferred: list[str] = []
    for d in data.get("deferred") or []:
        text = _clean_text(d.get("title") or d.get("summary") if isinstance(d, dict) else d, 300)
        if text and text not in deferred:
            deferred.append(text)
    if ready_story and len(stories) > 1:
        for s in stories:
            if s["id"] != ready_story["id"]:
                label = f"{s['title']}: {s['summary']}" if s.get("summary") and s["summary"] != s["title"] else s["title"]
                if label not in deferred and s["title"] not in deferred:
                    deferred.append(label)

    resolved_runtime = Path(runtime_dir) if runtime_dir is not None else (Path(root) / ".orchestrator" if root is not None else None)
    success_criteria = _normalize_success_criteria(
        data,
        user_text=user_text,
        prd_text=prd_text,
        expected_outcome=expected_outcome,
        intent=intent,
        ac_items=ac_items,
        feature_record=feature_record,
    )
    kpi_metrics = _normalize_kpi_metrics(
        data,
        title=title,
        intent=intent,
        success_criteria=success_criteria,
        runtime_root=resolved_runtime,
        feature_record=feature_record,
        kpi_policy=kpi_policy,
        consume_recommendation=consume_kpi_recommendation,
    )

    spec: dict[str, Any] = {
        "version": "1.0",
        "title": title,
        "inferred_job_type": inferred_kind,
        "planning_depth": depth_info["depth"],
        "depth_assessment": depth_info,
        "intent": intent,
        "expected_outcome": expected_outcome,
        "success_criteria": success_criteria,
        "kpi_metrics": kpi_metrics,
        "functional_requirements": func_reqs,
        "nonfunctional_requirements": nonfunc_reqs,
        "acceptance_criteria": ac_items,
        "constraints": constraints,
        "non_goals": non_goals,
        "assumptions": assumptions,
        "open_questions": open_questions,
        "decisions": decisions,
        "conflicts": all_conflicts,
        "evidence": raw_evidence,
        "stale_references": [],
        "stories": stories,
        "ready_story": ready_story,
        "deferred": deferred,
    }
    return verify_evidence_paths(spec, root)


def pass_a_prompt(
    template: str,
    raw_input: str,
    *,
    extra_input: str = "",
    revision_context: str = "",
) -> str:
    """Construct the Pass A LLM input string."""
    return (
        f"{template}\n"
        "- **Vertical slices, not layers**: Order stories and tasks so that after the FIRST slice something real runs end to end.\n\n"
        f"Raw input:\n{raw_input}{extra_input}{revision_context}\n"
    )


def pass_b_context(spec: dict[str, Any], risk_result: dict[str, Any]) -> str:
    """Format the synthesized Living Feature Spec and ready story for Pass B repository-aware task planning."""
    ready = spec.get("ready_story") or {"title": spec.get("title", ""), "summary": spec.get("intent", ""), "acceptance_criteria": []}
    lines = [
        "\n### PASS B: EXECUTION PLAN FROM LIVING FEATURE SPEC ###",
        "Pass A has synthesized the Living Feature Spec and evaluated ambiguity and risk.",
        "Decompose ONLY the Ready Story below into concrete implementation tasks (at most 5 tasks and ~12 files, as vertical slices) and test cases.",
        f"- **Intent**: {spec.get('intent', '')}",
        f"- **Expected Outcome**: {spec.get('expected_outcome', '')}",
        f"- **Planning Depth**: {spec.get('planning_depth', 'partial')}",
        f"- **Ready Story to Plan Now**: {ready.get('title')} — {ready.get('summary')}",
    ]
    if spec.get("functional_requirements"):
        lines.append("\n**Functional Requirements (with provenance):**")
        for r in spec["functional_requirements"]:
            lines.append(f"- [{r['provenance']}] {r['text']} (source: {r['source']})")
    if spec.get("success_criteria"):
        lines.append("\n**Feature Success Criteria (Explicit or Implied):**")
        for sc in spec["success_criteria"]:
            lines.append(f"- [{sc['provenance']}] {sc['text']} (source: {sc['source']})")
    km = spec.get("kpi_metrics") if isinstance(spec.get("kpi_metrics"), dict) else {}
    if km.get("status") == "active" and isinstance(km.get("primary_kpi"), dict):
        pk = km["primary_kpi"]
        apps = ", ".join(km.get("connected_apps") or []) or pk.get("analytics_app") or "Connected Analytics"
        lines.append(f"\n**KPI & Guardrail / Counter-KPI Metrics (Connected Analytics: {apps}):**")
        lines.append(f"- **Primary KPI**: `{pk['event']}` — {pk['name']} (direction: `{pk['direction']}`, tied to: {pk.get('tied_to_success_criterion', '')})")
        for gk in km.get("guardrail_kpis") or []:
            lines.append(f"- **Guardrail / Counter-KPI**: `{gk['event']}` — {gk['name']} (direction: `{gk['direction']}`, app: {gk.get('analytics_app', apps)}) — {gk.get('why', '')}")
    if spec.get("acceptance_criteria"):
        lines.append("\n**Acceptance Criteria to Satisfy & Cover with Test Cases:**")
        for a in spec["acceptance_criteria"]:
            lines.append(f"- [{a['provenance']}] {a['text']}")
    if spec.get("non_goals"):
        lines.append("\n**Non-Goals (MUST NOT be built):**")
        for ng in spec["non_goals"]:
            lines.append(f"- [{ng['provenance']}] {ng['text']}")
    if spec.get("assumptions"):
        lines.append("\n**Resolved Assumptions & Defaults:**")
        for a in spec["assumptions"]:
            reason = f" — {a['reason']}" if a.get("reason") else ""
            lines.append(f"- [inferred] {a['text']}{reason}")
    if risk_result.get("investigations"):
        lines.append("\n**Repository Investigation Evidence (from Risk Engine):**")
        for inv in risk_result["investigations"]:
            lines.append(f"- {inv['question']} → {inv['finding']}")
    if spec.get("stale_references"):
        lines.append("\n**Outdated / Stale Paths Detected in Input (DO NOT use missing paths; use replacements):**")
        for s in spec["stale_references"]:
            reps = f" → use {', '.join(s['replacements'])}" if s.get("replacements") else " → locate real path in repo"
            lines.append(f"- `{s['path']}` does not exist{reps}")
    if spec.get("deferred"):
        lines.append("\n**Deferred Follow-Up Stories (keep in top-level `deferred` array; do NOT create tasks for them in this job):**")
        for d in spec["deferred"]:
            lines.append(f"- {d}")
    return "\n".join(lines) + "\n"


def _note_key(text: str) -> str:
    return " ".join(text.split()).casefold()


def _note_with_reason(note: dict[str, str]) -> str:
    text, reason = note["text"], note.get("reason", "")
    return f"{text} ({reason})" if reason and reason not in text else text


_LOW_RISK_REASON = r"High-confidence, low-risk convention|Low-impact, reversible default after repo check|Low-impact, reversible choice for '.+'"


def _plan_note_items(*sources: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for source in sources:
        entries = source if isinstance(source, list) else [source]
        for raw in entries:
            if isinstance(raw, str):
                text = raw.strip()
                # Older plans stored an assumption or constraint as Python's dictionary representation.
                if len(text) <= 4096 and text.startswith(("{'text':", '{"text":')):
                    try:
                        raw = literal_eval(text)
                    except (SyntaxError, ValueError, RecursionError):
                        pass
            if isinstance(raw, dict):
                text = raw.get("text") or raw.get("description") or raw.get("title")
                reason = raw.get("reason") or raw.get("rationale")
                level = raw.get("severity") or raw.get("level") or raw.get("impact")
                note = {"reason": reason.strip()} if isinstance(reason, str) and reason.strip() else {}
                if isinstance(level, str) and level.lower() in {"high", "medium", "low"}:
                    note["level"] = level.lower()
            elif isinstance(raw, str):
                text, note = raw, {}
                # Some shared jobs only carry the older, flattened assumption list.
                match = re.fullmatch(rf"(.+) \(({_LOW_RISK_REASON})\)", text)
                if match:
                    text, reason = match.groups()
                    note["reason"] = reason
            else:
                continue
            if "reason" in note and re.fullmatch(_LOW_RISK_REASON, note["reason"]):
                note.setdefault("level", "low")
            if isinstance(text, str) and text.strip():
                items.append({"text": text.strip(), **note})
    aliases = {_note_key(_note_with_reason(note)): _note_key(note["text"])
               for note in items if note.get("reason")}
    unique: dict[str, dict[str, str]] = {}
    for note in items:
        key = _note_key(note["text"])
        canonical = aliases.get(key, key)
        if canonical == key:
            unique.setdefault(key, {}).update(note)
        else:
            unique.setdefault(canonical, {})
    return [note for note in unique.values() if note]


def plan_notes(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Readable, deduplicated job notes ordered by the recorded risk level."""
    plan, spec, risk = (value if isinstance(value, dict) else {}
                        for value in (job.get("plan"), job.get("spec"), job.get("risk_assessment")))
    assumptions = {_note_key(note["text"]): note
                   for note in _plan_note_items(plan.get("assumptions"), spec.get("assumptions"))}
    priorities: dict[str, list[dict[str, str]]] = {key: [] for key in ("high", "medium", "low", "unrated")}
    for note in _plan_note_items(plan.get("risks")):
        priorities[note.pop("level", "unrated")].append(note)
    for decision in risk.get("decisions") or spec.get("decisions") or plan.get("decisions") or []:
        if not isinstance(decision, dict):
            continue
        question = str(decision.get("question") or "").strip()
        default = str(decision.get("proposed_default") or "").strip()
        level = decision.get("impact")
        if decision.get("reversibility") == "costly":
            level = "high"
        if decision.get("action") == "clarify":
            note = {"text": question, **({"reason": f"Proposed default: {default}"} if default else {})}
        else:
            note = assumptions.pop(_note_key(default), {"text": default or question})
        if note["text"]:
            priorities[level if level in ("high", "medium", "low") else "unrated"].append(note)
    unranked = []
    for note in assumptions.values():
        level = note.pop("level", "")
        if level:
            priorities[level].append(note)
        else:
            unranked.append(note)

    ordered = [(f"{level.capitalize()} risk" if level != "unrated" else "Unrated risks", notes)
               for level, notes in priorities.items()]
    seen: set[str] = set()
    for _, notes in ordered:
        notes[:] = [note for key, note in {_note_key(note["text"]): note for note in notes}.items() if key not in seen]
        seen.update(_note_key(note["text"]) for note in notes)
    ordered.extend([
        ("Assumptions", [note for note in unranked if _note_key(note["text"]) not in seen]),
        ("Constraints", _plan_note_items(plan.get("constraints"), spec.get("constraints"))),
    ])
    return [{"label": label, "items": notes} for label, notes in ordered if notes]


def merge_spec_into_plan(plan: dict[str, Any], spec: dict[str, Any], risk_result: dict[str, Any]) -> dict[str, Any]:
    """Ensure the Pass B plan carries the Living Feature Spec's assumptions, constraints, deferred slices, and metadata."""
    for field in ("assumptions", "constraints"):
        if notes := _plan_note_items(plan.get(field), spec.get(field)):
            plan[field] = [_note_with_reason(note) if field == "assumptions" else note["text"] for note in notes]

    existing_deferred = [str(d).strip() for d in (plan.get("deferred") or []) if str(d).strip()]
    d_lower = {d.lower() for d in existing_deferred}
    for d in spec.get("deferred") or []:
        if str(d).strip() and str(d).strip().lower() not in d_lower:
            existing_deferred.append(str(d).strip())
            d_lower.add(str(d).strip().lower())
    if existing_deferred:
        plan["deferred"] = existing_deferred

    plan["planning_depth"] = spec.get("planning_depth", "partial")
    plan["gate_policy"] = risk_result.get("gate_policy", "human_approval")
    if spec.get("success_criteria"):
        plan["success_criteria"] = [item["text"] if isinstance(item, dict) else str(item) for item in spec["success_criteria"]]
    km = spec.get("kpi_metrics") if isinstance(spec.get("kpi_metrics"), dict) else {}
    if km:
        plan["kpi_metrics"] = km
        if km.get("status") == "recommend_setup_once" and km.get("recommendation"):
            plan["kpi_setup_recommendation"] = km["recommendation"]
        else:
            plan.pop("kpi_setup_recommendation", None)
    return plan


def to_markdown(spec: dict[str, Any], risk_result: dict[str, Any] | None = None) -> str:
    """Render the Living Feature Spec and Risk Assessment as a readable Markdown artifact."""
    lines = [
        f"# Living Feature Spec: {spec.get('title') or 'Untitled'}",
        "",
        f"- **Planning Depth**: `{spec.get('planning_depth', 'partial')}`",
        f"- **Intent**: {spec.get('intent') or '_Not specified_'}",
        f"- **Expected Outcome**: {spec.get('expected_outcome') or '_Not specified_'}",
    ]
    if risk_result:
        lines.append(f"- **Intent Gate**: `{'PASSED' if risk_result.get('intent_gate_passed') else 'NEEDS CLARIFICATION'}`")
        if risk_result.get("gate_policy"):
            lines.append(f"- **Gate Policy**: `{risk_result['gate_policy']}`")

    def section(heading: str, items: list[dict[str, str]]) -> None:
        if not items:
            return
        lines.extend(["", f"## {heading}"])
        for item in items:
            prov = item.get("provenance", "inferred")
            src = item.get("source", "")
            reason = f" — _{item['reason']}_" if item.get("reason") else ""
            src_note = f" (`{src}`)" if src else ""
            lines.append(f"- **[{prov}]** {item['text']}{src_note}{reason}")

    section("Success Criteria", spec.get("success_criteria") or [])
    km = spec.get("kpi_metrics") if isinstance(spec.get("kpi_metrics"), dict) else {}
    if km.get("status") == "active" and isinstance(km.get("primary_kpi"), dict):
        pk = km["primary_kpi"]
        apps = ", ".join(km.get("connected_apps") or []) or pk.get("analytics_app") or "Connected Analytics"
        lines.extend(["", f"## KPI & Guardrail Metrics ({apps})"])
        lines.append(f"- **Primary KPI**: **{pk['name']}** (`{pk['event']}`, direction: `{pk['direction']}`)")
        for gk in km.get("guardrail_kpis") or []:
            lines.append(f"- **Guardrail / Counter-KPI**: **{gk['name']}** (`{gk['event']}`, direction: `{gk['direction']}`) — _{gk.get('why', '')}_")
    elif km.get("status") == "recommend_setup_once" and km.get("recommendation"):
        lines.extend(["", "## KPI Setup Recommendation (One-Time)", f"- {km['recommendation']}"])

    section("Functional Requirements", spec.get("functional_requirements") or [])
    section("Nonfunctional Requirements", spec.get("nonfunctional_requirements") or [])
    section("Acceptance Criteria", spec.get("acceptance_criteria") or [])
    section("Constraints", spec.get("constraints") or [])
    section("Non-Goals", spec.get("non_goals") or [])
    section("Assumptions", spec.get("assumptions") or [])

    if spec.get("conflicts"):
        lines.extend(["", "## ⚠️ Explicit Requirement Conflicts"])
        for c in spec["conflicts"]:
            lines.append(f"- **{c['description']}** (`{c['source_a']}` vs `{c['source_b']}`)")

    if spec.get("decisions"):
        lines.extend(["", "## Ambiguity & Risk Decisions"])
        for d in spec["decisions"]:
            action = d.get("action") or d.get("resolvable_by") or "default_assumption"
            default_note = f" → Default: {d['proposed_default']}" if d.get("proposed_default") else ""
            lines.append(
                f"- **{d['question']}** (confidence: `{d['confidence']}`, impact: `{d['impact']}`, "
                f"reversibility: `{d['reversibility']}`, action: `{action}`){default_note}"
            )

    if spec.get("stale_references"):
        lines.extend(["", "## Stale / Outdated References"])
        for s in spec["stale_references"]:
            rep = f" (candidates: `{', '.join(s['replacements'])}`)" if s.get("replacements") else ""
            lines.append(f"- `{s['path']}`: {s['reason']}{rep}")

    if spec.get("stories"):
        lines.extend(["", "## Story Breakdown"])
        ready_id = (spec.get("ready_story") or {}).get("id")
        for s in spec["stories"]:
            tag = " *(Ready Slice)*" if s.get("id") == ready_id else " *(Deferred)*"
            lines.append(f"- **[{s.get('slice') or 'MVP'}] {s['title']}**{tag}: {s.get('summary', '')}")

    if spec.get("deferred"):
        lines.extend(["", "## Deferred Slices (Rolling-Wave Backlog)"])
        for d in spec["deferred"]:
            lines.append(f"- {d}")

    return "\n".join(lines).strip() + "\n"


def create_deferred_backlog_jobs(runtime: Path, parent_job: dict[str, Any]) -> list[str]:
    """Turn a job's `plan["deferred"]` slices into waiting backlog jobs (`status: "backlog"`).

    Like approved Epics, waiting backlog jobs never count as 'needs you' or send notifications,
    and sit ready in the Backlog filter until the person chooses to plan them.
    """
    runtime = Path(runtime)
    plan = parent_job.get("plan") if isinstance(parent_job.get("plan"), dict) else {}
    spec = parent_job.get("spec") if isinstance(parent_job.get("spec"), dict) else {}
    deferred = [str(d).strip() for d in (plan.get("deferred") or spec.get("deferred") or []) if str(d).strip()]
    if not deferred:
        return []

    folder = runtime / "jobs"
    folder.mkdir(parents=True, exist_ok=True)
    existing_ids = list(parent_job.get("deferred_job_ids") or [])
    existing_titles: set[str] = set()
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("status") == "backlog":
                existing_titles.add(str(data.get("title") or "").strip().lower())
                if data.get("deferred_from") == parent_job.get("job_id") and data.get("job_id") not in existing_ids:
                    existing_ids.append(data["job_id"])
        except (OSError, json.JSONDecodeError):
            continue

    now = datetime.now()
    created: list[str] = list(existing_ids)
    for item in deferred[:10]:
        title = item.split(":", 1)[0].strip()[:80] if ":" in item and len(item.split(":", 1)[0].strip()) >= 6 else item[:80]
        if title.lower() in existing_titles:
            continue
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:30] or "slice"
        job_id, n = f"{now:%Y%m%d-%H%M%S}-slice-{slug}", 2
        while (folder / f"{job_id}.json").exists():
            job_id, n = f"{now:%Y%m%d-%H%M%S}-slice-{slug}-{n}", n + 1
        backlog_job = {
            "job_id": job_id,
            "type": "epic",
            "title": title,
            "summary": item,
            "status": "backlog",
            "deferred_from": parent_job.get("job_id"),
            "feature": parent_job.get("feature") or None,
            "description": f"# Deferred Slice: {title}\n\n{item}\n\nDeferred from job `{parent_job.get('job_id')}` ({parent_job.get('title')}).\n",
            "external_links": list(parent_job.get("external_links") or []),
            "created_at": now.isoformat(timespec="seconds"),
            "updated_at": now.isoformat(timespec="seconds"),
        }
        target = folder / f"{job_id}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(backlog_job, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        existing_titles.add(title.lower())
        created.append(job_id)

    parent_job["deferred_job_ids"] = created
    return created


def _criterion_tokens(text: str) -> set[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", text or "")]
    return {reuse_brief._stem(w) for w in words if len(w) >= 3 and w not in _STOP_WORDS and w not in reuse_brief.STOP}


def validate_replan_invariants(original_spec: dict[str, Any] | None, revised_plan: dict[str, Any]) -> list[str]:
    """Verify that a mid-execution replan does not drop explicitly approved requirements or violate non-goals.

    Returns a list of violation messages (empty when safe).
    """
    if not isinstance(original_spec, dict) or not isinstance(revised_plan, dict):
        return []

    violations: list[str] = []
    # Collect all text in the revised plan (summary, acceptance criteria, tasks)
    plan_parts: list[str] = [str(revised_plan.get("summary") or "")]
    for ac in revised_plan.get("acceptance_criteria") or []:
        plan_parts.append(ac.get("text", "") if isinstance(ac, dict) else str(ac))
    for task in revised_plan.get("tasks") or []:
        if isinstance(task, dict):
            plan_parts.append(str(task.get("title") or ""))
            plan_parts.append(str(task.get("description") or ""))
            for ac in task.get("acceptance_criteria") or []:
                plan_parts.append(str(ac))
    combined_plan = "\n".join(plan_parts)
    covered_tokens = _criterion_tokens(combined_plan) | _criterion_tokens(" ".join(str(d) for d in (revised_plan.get("deferred") or [])))

    # 1. Explicit acceptance criteria must still be represented (at least half of their meaningful tokens covered)
    for item in original_spec.get("acceptance_criteria") or []:
        if not isinstance(item, dict) or item.get("provenance") != "explicit":
            continue
        req_text = str(item.get("text") or "").strip()
        req_tokens = _criterion_tokens(req_text)
        needed = max(1, (len(req_tokens) + 1) // 2)
        if req_tokens and len(req_tokens & covered_tokens) < needed:
            violations.append(f"Explicit acceptance criterion was dropped during replanning: '{req_text}'")

    # 2. Explicit non-goals must not be introduced by the revised plan
    for ng in original_spec.get("non_goals") or []:
        ng_text = ng.get("text", "") if isinstance(ng, dict) else str(ng)
        for phrase in _meaningful_phrases(ng_text):
            if _request_affirmatively_asks_for(combined_plan, phrase):
                violations.append(f"Revised plan introduces excluded non-goal '{ng_text}' (matched '{phrase}').")
                break

    return violations
