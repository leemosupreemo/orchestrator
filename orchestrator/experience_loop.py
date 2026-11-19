"""Prototype-First Progressive Specification: the Human Experience Loop (`Build -> Experience -> Refine`).

Separates the two iteration loops in the orchestrator:
1. Autonomous Engineering Loop (`debug_job.py`):
   - Triggered by compiler errors, failing tests, or code review issues.
   - Owned by AI agents; does not modify product requirements; user is not interrupted.
2. Human Experience Loop (`experience_loop.py`):
   - Triggered when the user experiences a working version (`v0.1`, `v0.2`, ...) and reacts to it.
   - Classifies every reaction via a Change-Impact Classifier:
     * `visual_or_copy`      -> targeted implementation patch; spec invariants unchanged
     * `behavior_change`     -> updates LivingFeatureSpec acceptance criteria, supersedes prior assumption, locks regression tests
     * `new_capability`      -> creates a new rolling-wave story slice in the backlog
     * `fundamental_rethink` -> triggers Pass A + Pass B architectural & story replan
     * `undo`                -> safely reverts the last iteration commit/checkpoint and restores prior spec snapshot
   - Enforces an Architectural Decay / Cruft Guardrail: tracks file churn across iterations and injects a
     consolidation/refactor directive when the same files are repeatedly patched (>= 3 times).
   - Tracks Intent-Convergence telemetry: Time to First Experience (TTFE), Time per Feedback Iteration,
     Iterations to Acceptance, Rework/Regression Rate, and User Interaction Burden.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from orchestrator import living_spec

FEEDBACK_TYPES = (
    "visual_or_copy",
    "behavior_change",
    "new_capability",
    "fundamental_rethink",
    "undo",
)

DEFAULT_ACTIONS = {
    "visual_or_copy": "targeted_implementation_change",
    "behavior_change": "update_acceptance_criteria_and_code",
    "new_capability": "create_new_story_slice",
    "fundamental_rethink": "revisit_architecture_and_stories",
    "undo": "revert_last_change",
}

CHURN_REFACTOR_THRESHOLD = 3

_UNDO_PATTERNS = re.compile(
    r"^\s*(?:undo|revert|roll\s*back|go\s+back|restore\s+previous|undo\s+that|revert\s+that|take\s+that\s+back)\b|"
    r"\b(?:undo\s+the\s+last|revert\s+the\s+last|back\s+to\s+v0\.\d+)\b",
    re.IGNORECASE,
)

_FUNDAMENTAL_PATTERNS = re.compile(
    r"\b(?:fundamental\s+rethink|start\s+over|scrap\s+this|rearchitect|re-architect|completely\s+different\s+approach|"
    r"wrong\s+architecture|replace\s+the\s+whole|redesign\s+from\s+scratch|switch\s+architecture)\b",
    re.IGNORECASE,
)

_NEW_CAPABILITY_PATTERNS = re.compile(
    r"\b(?:also\s+work\s+for|also\s+support|also\s+add\s+a\s+new|add\s+another\s+mode|add\s+a\s+new\s+screen|"
    r"expand\s+this\s+to|in\s+addition\s+add|new\s+capability|support\s+exporting|add\s+multiplayer|for\s+abstract\s+mode\s+too)\b",
    re.IGNORECASE,
)

_VISUAL_OR_COPY_PATTERNS = re.compile(
    r"\b(?:font|color|colour|padding|margin|spacing|alignment|icon|label|wording|copy|typo|text\s+size|"
    r"darker|lighter|bigger|smaller|button\s+style|border|rounded|shadow|opacity|animation\s+speed|tooltip|placeholder)\b",
    re.IGNORECASE,
)

_BEHAVIOR_PATTERNS = re.compile(
    r"\b(?:shouldn't\s+count|should\s+not\s+count|don't\s+count|do\s+not\s+count|normalize|normalise|"
    r"penalize|penalise|tie-breaker|ties|ranking|score|calculation|sort\s+by|filter\s+out|when\s+user|"
    r"behave\s+like|instead\s+of|allow|restrict|limit|validation|rule|logic|faster|slower|retry|retries)\b",
    re.IGNORECASE,
)


def _parse_iso(ts: Any) -> float | None:
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    if not isinstance(ts, str) or not ts.strip():
        return None
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_version_label(job: dict[str, Any]) -> str:
    """Return the human-facing version label (`0.1`, `0.2`, ...) for a job."""
    versions = job.get("experience_versions") if isinstance(job.get("experience_versions"), list) else []
    if versions and isinstance(versions[-1], dict) and versions[-1].get("version"):
        return str(versions[-1]["version"])
    iteration = int(job.get("experience_iteration") or (1 if job.get("status") in ("review-needed", "completed") else 0))
    return f"0.{max(1, iteration)}" if iteration > 0 else "0.1"


def classify_feedback(feedback: str, job: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run Change-Impact Analysis on user feedback to decide the safest convergence action."""
    text = " ".join(str(feedback or "").split()).strip()
    if not text:
        return {
            "feedback_type": "behavior_change",
            "default_action": DEFAULT_ACTIONS["behavior_change"],
            "updates_spec": True,
            "creates_story": False,
            "rebuilds_plan": False,
            "is_undo": False,
            "summary": "Empty feedback defaulted to behavior refinement.",
        }

    if _UNDO_PATTERNS.search(text):
        return {
            "feedback_type": "undo",
            "default_action": DEFAULT_ACTIONS["undo"],
            "updates_spec": True,
            "creates_story": False,
            "rebuilds_plan": False,
            "is_undo": True,
            "summary": "Revert the last experience iteration and restore the previous specification state.",
        }

    if _FUNDAMENTAL_PATTERNS.search(text):
        return {
            "feedback_type": "fundamental_rethink",
            "default_action": DEFAULT_ACTIONS["fundamental_rethink"],
            "updates_spec": True,
            "creates_story": True,
            "rebuilds_plan": True,
            "is_undo": False,
            "summary": "Revisit architecture and re-run two-pass specification and story decomposition.",
        }

    if _NEW_CAPABILITY_PATTERNS.search(text) and not _BEHAVIOR_PATTERNS.search(text.split(".")[0]):
        return {
            "feedback_type": "new_capability",
            "default_action": DEFAULT_ACTIONS["new_capability"],
            "updates_spec": True,
            "creates_story": True,
            "rebuilds_plan": False,
            "is_undo": False,
            "summary": "Queue a new rolling-wave story slice and evaluate dependencies without bloating the current slice.",
        }

    has_visual = bool(_VISUAL_OR_COPY_PATTERNS.search(text))
    has_behavior = bool(_BEHAVIOR_PATTERNS.search(text))
    if has_visual and not has_behavior:
        return {
            "feedback_type": "visual_or_copy",
            "default_action": DEFAULT_ACTIONS["visual_or_copy"],
            "updates_spec": False,
            "creates_story": False,
            "rebuilds_plan": False,
            "is_undo": False,
            "summary": "Targeted visual or copy adjustment; existing acceptance criteria remain locked.",
        }

    return {
        "feedback_type": "behavior_change",
        "default_action": DEFAULT_ACTIONS["behavior_change"],
        "updates_spec": True,
        "creates_story": False,
        "rebuilds_plan": False,
        "is_undo": False,
        "summary": "Update Living Feature Spec acceptance criteria, supersede conflicting assumptions, and update code/tests.",
    }


def assess_iteration_churn(job: dict[str, Any], candidate_files: list[str] | None = None) -> dict[str, Any]:
    """Detect when repeated incremental patches indicate refactoring is safer than another narrow patch."""
    counts: dict[str, int] = {}
    versions = job.get("experience_versions") if isinstance(job.get("experience_versions"), list) else []
    for v in versions:
        if not isinstance(v, dict) or v.get("feedback_type") == "undo":
            continue
        for f in v.get("files_changed") or []:
            path = str(f).strip()
            if path:
                counts[path] = counts.get(path, 0) + 1

    # Also count current job's modified files if not yet recorded in versions
    if not versions:
        for f in (job.get("ai_modified_files") or []) + (job.get("ai_untracked_files") or []):
            path = str(f).strip()
            if path:
                counts[path] = counts.get(path, 0) + 1

    for f in candidate_files or []:
        path = str(f).strip()
        if path:
            counts[path] = counts.get(path, 0) + 1

    hot_files = sorted(f for f, c in counts.items() if c >= CHURN_REFACTOR_THRESHOLD)
    non_undo_iterations = sum(1 for v in versions if isinstance(v, dict) and v.get("feedback_type") != "undo")
    refactor_recommended = bool(hot_files or non_undo_iterations >= CHURN_REFACTOR_THRESHOLD)
    directive = ""
    if refactor_recommended:
        targets = ", ".join(f"`{f}`" for f in hot_files[:4]) if hot_files else "the repeatedly modified components"
        directive = (
            f"Architectural guardrail: {targets} have been patched across {max(non_undo_iterations, CHURN_REFACTOR_THRESHOLD)} "
            "iterations. Consolidate and refactor the underlying logic cleanly rather than layering another narrow conditional "
            "patch, while keeping all locked regression behaviors passing."
        )
    return {
        "refactor_recommended": refactor_recommended,
        "hot_files": hot_files,
        "file_touch_counts": counts,
        "iteration_count": non_undo_iterations,
        "directive": directive,
    }


def record_first_experience(job: dict[str, Any], *, commit_sha: str = "", now_iso: str | None = None) -> dict[str, Any]:
    """Mark a job as having reached its first playable experience (`v0.1`) and compute TTFE."""
    ts = now_iso or _now_iso()
    if not job.get("first_experience_at"):
        job["first_experience_at"] = ts
        created_ts = _parse_iso(job.get("created_at"))
        first_ts = _parse_iso(ts)
        if created_ts is not None and first_ts is not None and first_ts >= created_ts:
            job["ttfe_seconds"] = round(first_ts - created_ts, 2)

    versions = job.setdefault("experience_versions", [])
    if not versions:
        spec = job.get("spec") if isinstance(job.get("spec"), dict) else {}
        files = sorted(set((job.get("ai_modified_files") or []) + (job.get("ai_untracked_files") or [])))
        versions.append({
            "version": "0.1",
            "iteration": 1,
            "loop": "experience",
            "feedback": "",
            "feedback_type": "initial_prototype",
            "action_taken": "initial_slice_built",
            "summary": str((job.get("plan") or {}).get("summary") or job.get("title") or "Initial playable slice"),
            "commit": commit_sha,
            "files_changed": files,
            "assumed_hypotheses": list(spec.get("product_hypotheses") or []),
            "spec_snapshot": copy.deepcopy(spec),
            "refactor_recommended": False,
            "timestamp": ts,
        })
        job["experience_iteration"] = 1
        job["iteration_version"] = "0.1"
    return job


def apply_feedback_to_spec(
    spec: dict[str, Any],
    feedback: str,
    classification: dict[str, Any],
) -> dict[str, Any]:
    """Progressively evolve the LivingFeatureSpec from user reaction while locking previously accepted behavior."""
    updated = copy.deepcopy(spec) if isinstance(spec, dict) else {}
    text = " ".join(str(feedback or "").split()).strip()
    if not text:
        return updated

    fb_type = classification.get("feedback_type", "behavior_change")
    fb_tokens = living_spec._criterion_tokens(text)

    # 1. Lock existing acceptance criteria that are NOT superseded by this feedback
    existing_ac = list(updated.get("acceptance_criteria") or [])
    locked: list[dict[str, str]] = list(updated.get("locked_behaviors") or [])
    locked_texts = {item.get("text", "").lower() for item in locked if isinstance(item, dict)}

    kept_ac: list[dict[str, str]] = []
    for item in existing_ac:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        item_tokens = living_spec._criterion_tokens(item["text"])
        overlap = len(item_tokens & fb_tokens)
        is_superseded = bool(
            fb_type in ("behavior_change", "fundamental_rethink")
            and item.get("provenance") == "inferred"
            and item_tokens
            and overlap >= max(1, len(item_tokens) // 2)
        )
        if is_superseded:
            continue
        kept_ac.append(item)
        if item["text"].lower() not in locked_texts:
            locked.append({
                "text": item["text"],
                "provenance": item.get("provenance", "explicit"),
                "source": item.get("source", "accepted_iteration"),
            })
            locked_texts.add(item["text"].lower())

    # 2. Supersede matching inferred assumptions or product hypotheses when user corrects behavior
    if fb_type in ("behavior_change", "fundamental_rethink"):
        new_assumptions = []
        for a in updated.get("assumptions") or []:
            a_text = a.get("text", "") if isinstance(a, dict) else str(a)
            a_tokens = living_spec._criterion_tokens(a_text)
            if a_tokens and len(a_tokens & fb_tokens) >= max(1, len(a_tokens) // 2):
                continue
            new_assumptions.append(a)
        updated["assumptions"] = new_assumptions

        for h in updated.get("product_hypotheses") or []:
            if isinstance(h, dict):
                h_tokens = living_spec._criterion_tokens(f"{h.get('question', '')} {h.get('chosen_default', '')}")
                if h_tokens and len(h_tokens & fb_tokens) >= 1:
                    h["status"] = "refined_by_user"
                    h["user_correction"] = text

    # 3. Record the user's reaction as explicit requirements / acceptance criteria or a new deferred story slice
    if fb_type == "new_capability":
        deferred = list(updated.get("deferred") or [])
        if text not in deferred:
            deferred.append(text)
        updated["deferred"] = deferred
        stories = list(updated.get("stories") or [])
        stories.append({
            "id": f"story-{len(stories) + 1}",
            "title": text[:120],
            "summary": text,
            "slice": "Next",
            "acceptance_criteria": [text],
            "depends_on": [stories[0]["id"]] if stories and isinstance(stories[0], dict) and stories[0].get("id") else [],
        })
        updated["stories"] = stories
    elif fb_type in ("behavior_change", "fundamental_rethink", "visual_or_copy"):
        sentences = [s.strip(" .") for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip(" .")]
        for sentence in sentences or [text]:
            entry = {
                "text": sentence,
                "provenance": "explicit",
                "source": "user_experience_feedback",
            }
            if fb_type != "visual_or_copy":
                if sentence.lower() not in {x.get("text", "").lower() for x in kept_ac}:
                    kept_ac.append(entry)
                func_reqs = list(updated.get("functional_requirements") or [])
                if sentence.lower() not in {x.get("text", "").lower() for x in func_reqs if isinstance(x, dict)}:
                    func_reqs.append(entry)
                updated["functional_requirements"] = func_reqs

    updated["acceptance_criteria"] = kept_ac
    updated["locked_behaviors"] = locked
    history = list(updated.get("feedback_history") or [])
    history.append({
        "feedback": text,
        "feedback_type": fb_type,
        "action": classification.get("default_action", ""),
        "timestamp": _now_iso(),
    })
    updated["feedback_history"] = history
    return updated


def apply_experience_feedback(
    job: dict[str, Any],
    feedback: str,
    *,
    root: Path | None = None,
    runtime_dir: Path | None = None,
    now_iso: str | None = None,
) -> dict[str, Any]:
    """Process a user's reaction to the working preview (`Build -> Experience -> Refine`).

    Updates `job["spec"]`, `job["plan"]`, and `job["experience_versions"]`, runs the
    Change-Impact Classifier and Cruft/Refactor Guardrail, and returns a summary dict
    describing the next execution action (`"build_refinement"`, `"replan"`, `"backlog_slice_created"`, or `"reverted"`).
    """
    ts = now_iso or _now_iso()
    record_first_experience(job, now_iso=ts)
    classification = classify_feedback(feedback, job)
    fb_type = classification["feedback_type"]
    versions: list[dict[str, Any]] = job.setdefault("experience_versions", [])

    # Handle "Undo that": revert last iteration commit (if git repo available) and restore previous spec snapshot
    if fb_type == "undo":
        reverted_commit = ""
        if len(versions) >= 2:
            last_ver = versions[-1]
            prev_ver = versions[-2]
            if isinstance(prev_ver.get("spec_snapshot"), dict) and prev_ver["spec_snapshot"]:
                job["spec"] = copy.deepcopy(prev_ver["spec_snapshot"])
                job["plan"] = living_spec.merge_spec_into_plan(
                    job.get("plan") or {},
                    job["spec"],
                    job.get("risk_assessment") or {},
                )
            commit_to_revert = str(last_ver.get("commit") or "").strip()
            if commit_to_revert and root and (Path(root) / ".git").exists():
                try:
                    res = subprocess.run(
                        ["git", "revert", "--no-edit", commit_to_revert],
                        cwd=str(root),
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    if res.returncode == 0:
                        reverted_commit = commit_to_revert
                except Exception:
                    pass
        next_num = len(versions) + 1
        ver_label = f"0.{next_num}"
        entry = {
            "version": ver_label,
            "iteration": next_num,
            "loop": "experience",
            "feedback": feedback,
            "feedback_type": "undo",
            "action_taken": "reverted_last_iteration",
            "summary": f"Reverted iteration to previous state{f' (commit {reverted_commit[:7]})' if reverted_commit else ''}.",
            "commit": reverted_commit,
            "files_changed": [],
            "spec_snapshot": copy.deepcopy(job.get("spec") or {}),
            "refactor_recommended": False,
            "timestamp": ts,
        }
        versions.append(entry)
        job["experience_iteration"] = next_num
        job["iteration_version"] = ver_label
        return {
            "version": ver_label,
            "iteration": next_num,
            "classification": classification,
            "next_execution": "reverted",
            "refactor_recommended": False,
            "reverted_commit": reverted_commit,
            "entry": entry,
        }

    # Evolve the LivingFeatureSpec with the user's feedback
    current_spec = job.get("spec") if isinstance(job.get("spec"), dict) else living_spec.normalize(
        job.get("plan") or {},
        raw_input=str(job.get("raw_input") or job.get("title") or ""),
        root=root,
        runtime_dir=runtime_dir,
        consume_kpi_recommendation=False,
    )
    updated_spec = apply_feedback_to_spec(current_spec, feedback, classification)
    job["spec"] = updated_spec

    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
    if fb_type in ("behavior_change", "fundamental_rethink"):
        plan["acceptance_criteria"] = [a["text"] for a in updated_spec.get("acceptance_criteria") or [] if isinstance(a, dict)]
    plan = living_spec.merge_spec_into_plan(plan, updated_spec, job.get("risk_assessment") or {})
    job["plan"] = plan

    # Check architectural churn / refactor guardrail
    candidate_files = list(plan.get("likely_files") or [])
    for t in plan.get("tasks") or []:
        if isinstance(t, dict):
            candidate_files.extend(t.get("likely_files") or [])
    churn = assess_iteration_churn(job, candidate_files)
    job["churn_guardrail"] = churn

    # If new capability, create rolling-wave deferred backlog job immediately
    created_backlog: list[str] = []
    resolved_runtime = Path(runtime_dir) if runtime_dir is not None else (Path(root) / ".orchestrator" if root is not None else None)
    if fb_type == "new_capability" and resolved_runtime is not None:
        try:
            created_backlog = living_spec.create_deferred_backlog_jobs(resolved_runtime, job)
        except Exception:
            created_backlog = []

    next_num = len(versions) + 1
    ver_label = f"0.{next_num}"
    if fb_type == "new_capability":
        next_exec = "backlog_slice_created"
    elif fb_type == "fundamental_rethink":
        next_exec = "replan"
    else:
        next_exec = "build_refinement"

    entry = {
        "version": ver_label,
        "iteration": next_num,
        "loop": "experience",
        "feedback": feedback,
        "feedback_type": fb_type,
        "action_taken": classification["default_action"],
        "summary": classification["summary"],
        "commit": "",
        "files_changed": list(dict.fromkeys(candidate_files))[:12],
        "spec_snapshot": copy.deepcopy(updated_spec),
        "refactor_recommended": churn["refactor_recommended"],
        "refactor_directive": churn["directive"],
        "created_backlog_jobs": created_backlog,
        "timestamp": ts,
    }
    versions.append(entry)
    job["experience_iteration"] = next_num
    job["iteration_version"] = ver_label
    return {
        "version": ver_label,
        "iteration": next_num,
        "classification": classification,
        "next_execution": next_exec,
        "refactor_recommended": churn["refactor_recommended"],
        "refactor_directive": churn["directive"],
        "created_backlog_jobs": created_backlog,
        "entry": entry,
    }


def format_refinement_prompt(job: dict[str, Any], feedback: str, outcome: dict[str, Any]) -> str:
    """Build the targeted refinement instruction for the builder, including locked regression invariants and refactor guardrails."""
    cls = outcome.get("classification") or {}
    spec = job.get("spec") if isinstance(job.get("spec"), dict) else {}
    lines = [
        f"### EXPERIENCE REFINEMENT (Version v{outcome.get('version', '0.2')} · {cls.get('feedback_type', 'behavior_change')}) ###",
        f"User Reaction: {feedback}",
        f"Change-Impact Action: {cls.get('default_action', '')} — {cls.get('summary', '')}",
    ]
    if outcome.get("refactor_recommended") and outcome.get("refactor_directive"):
        lines.extend(["", f"### 🧹 ARCHITECTURAL REFACTOR GUARDRAIL ###", outcome["refactor_directive"]])
    locked = spec.get("locked_behaviors") or []
    if locked:
        lines.append("\n### LOCKED BEHAVIORS FROM PREVIOUS ITERATIONS (MUST NOT REGRESS) ###")
        for item in locked[:12]:
            text = item.get("text") if isinstance(item, dict) else str(item)
            if text:
                lines.append(f"- {text}")
    ac = spec.get("acceptance_criteria") or []
    if ac:
        lines.append("\n### UPDATED ACCEPTANCE CRITERIA ###")
        for item in ac[:12]:
            text = item.get("text") if isinstance(item, dict) else str(item)
            prov = item.get("provenance", "explicit") if isinstance(item, dict) else "explicit"
            if text:
                lines.append(f"- [{prov}] {text}")
    return "\n".join(lines)


def experience_summary(job: dict[str, Any]) -> dict[str, Any]:
    """Structured experience-workspace state exposed to the web UI for a job."""
    spec = job.get("spec") if isinstance(job.get("spec"), dict) else {}
    versions = job.get("experience_versions") if isinstance(job.get("experience_versions"), list) else []
    version_label = current_version_label(job)
    iteration_num = int(job.get("experience_iteration") or (len(versions) if versions else 1))
    hypotheses = list(spec.get("product_hypotheses") or [])
    if not hypotheses:
        for idx, a in enumerate((spec.get("assumptions") or [])[:5], start=1):
            if isinstance(a, dict) and a.get("text"):
                hypotheses.append({
                    "id": f"hyp-{idx}",
                    "question": a.get("reason") or "Assumed default for initial version",
                    "chosen_default": a["text"],
                    "status": "active_hypothesis",
                })
    churn = job.get("churn_guardrail") if isinstance(job.get("churn_guardrail"), dict) else assess_iteration_churn(job)
    return {
        "version": version_label,
        "iteration": iteration_num,
        "preview_ready": job.get("status") == "review-needed",
        "ttfe_seconds": job.get("ttfe_seconds"),
        "first_experience_at": job.get("first_experience_at"),
        "product_hypotheses": hypotheses,
        "locked_behaviors": list(spec.get("locked_behaviors") or []),
        "versions": [
            {
                "version": v.get("version"),
                "iteration": v.get("iteration"),
                "feedback": v.get("feedback", ""),
                "feedback_type": v.get("feedback_type", ""),
                "action_taken": v.get("action_taken", ""),
                "summary": v.get("summary", ""),
                "commit": v.get("commit", ""),
                "refactor_recommended": bool(v.get("refactor_recommended")),
                "timestamp": v.get("timestamp", ""),
            }
            for v in versions if isinstance(v, dict)
        ],
        "churn_guardrail": {
            "refactor_recommended": bool(churn.get("refactor_recommended")),
            "hot_files": list(churn.get("hot_files") or []),
            "directive": str(churn.get("directive") or ""),
        },
    }


def compute_convergence_metrics(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the 5 prototype-first Intent-Convergence Metrics across project job history:
    1. Time to First Experience (TTFE)
    2. Time per Feedback Iteration
    3. Iterations to Acceptance
    4. Rework & Regression Rate
    5. User Interaction Burden (questions + manual approvals before first experience)
    """
    ttfe_samples: list[float] = []
    iteration_durations: list[float] = []
    iterations_to_accept: list[int] = []
    total_refinements = 0
    regressed_or_undone_refinements = 0
    interaction_burdens: list[int] = []

    for j in jobs:
        if not isinstance(j, dict):
            continue
        # 1. TTFE
        ttfe = j.get("ttfe_seconds")
        if isinstance(ttfe, (int, float)) and not isinstance(ttfe, bool) and ttfe >= 0:
            ttfe_samples.append(float(ttfe))
        else:
            c_ts = _parse_iso(j.get("created_at"))
            f_ts = _parse_iso(j.get("first_experience_at"))
            if c_ts is not None and f_ts is not None and f_ts >= c_ts:
                ttfe_samples.append(f_ts - c_ts)

        # 2 & 4. Iteration durations & Rework/Regression Rate
        versions = [v for v in (j.get("experience_versions") or []) if isinstance(v, dict)]
        for idx, v in enumerate(versions):
            dur = v.get("duration_seconds")
            if isinstance(dur, (int, float)) and dur >= 0:
                iteration_durations.append(float(dur))
            elif idx > 0:
                prev_ts = _parse_iso(versions[idx - 1].get("timestamp"))
                cur_ts = _parse_iso(v.get("timestamp"))
                if prev_ts is not None and cur_ts is not None and cur_ts >= prev_ts:
                    iteration_durations.append(cur_ts - prev_ts)
            if v.get("feedback_type") and v.get("feedback_type") != "initial_prototype":
                total_refinements += 1
                if v.get("feedback_type") == "undo" or v.get("regressed") or v.get("refactor_recommended"):
                    regressed_or_undone_refinements += 1

        # 3. Iterations to Acceptance (for completed/merged jobs)
        if j.get("status") == "completed":
            iters = int(j.get("experience_iteration") or (len(versions) if versions else 1))
            iterations_to_accept.append(max(1, iters))

        # 5. User Interaction Burden (human questions + manual gate approvals before first experience)
        manual_questions = sum(
            1 for c in (j.get("clarification_history") or [])
            if isinstance(c, dict) and not c.get("auto")
        )
        manual_approval = 0 if (j.get("auto_passed_gates") or j.get("yolo") or j.get("is_yolo") or j.get("type") == "quick-fix") else (1 if j.get("approved") else 0)
        interaction_burdens.append(manual_questions + manual_approval)

    avg_ttfe = round(sum(ttfe_samples) / len(ttfe_samples), 1) if ttfe_samples else None
    avg_iter_time = round(sum(iteration_durations) / len(iteration_durations), 1) if iteration_durations else None
    avg_iters_accept = round(sum(iterations_to_accept) / len(iterations_to_accept), 2) if iterations_to_accept else None
    rework_rate = round((regressed_or_undone_refinements / total_refinements) * 100, 1) if total_refinements > 0 else 0.0
    avg_burden = round(sum(interaction_burdens) / len(interaction_burdens), 2) if interaction_burdens else 0.0

    return {
        "avg_ttfe_seconds": avg_ttfe,
        "ttfe_samples": len(ttfe_samples),
        "avg_iteration_seconds": avg_iter_time,
        "avg_iterations_to_accept": avg_iters_accept,
        "rework_regression_rate": rework_rate,
        "total_experience_refinements": total_refinements,
        "avg_interaction_burden": avg_burden,
    }
